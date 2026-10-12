"""
Agent runtime API (FastAPI).

    WS  /ws               live call. Client → server: {"type":"start","caller_number"?}, binary PCM16 24 kHz
                          mono audio frames, {"type":"text","text"}, {"type":"end"}.
                          Server → client: JSON events (audio arrives as "audio.delta", base64 PCM16 24 kHz).
    GET /api/health       liveness + whether the voice cascade (Groq) / Content Safety are configured
    GET /api/journeys     journey graphs + subagent roster
    GET /api/config       STT, LLM and TTS models and the voice in use
    GET /api/admin/state  CRM contact log, TaskDesk tasks/cases, transfers (engineer view)

Run:  uvicorn services.agent_runtime.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .call import CallBridge
from .config import ROOT, settings
from .guardrails import Guardrails
from .journeys.registry import JOURNEYS, SUBAGENTS
from .mcp_hub import McpHub
from .orchestrator import Orchestrator
from .session import Session
from .topic import topic_guard

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("runtime")
state: dict = {}


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    hub = McpHub()
    await hub.start()
    # Topic guard (input guardrail): loads the local embedding model; the call still works if it can't.
    await asyncio.to_thread(topic_guard.load)
    v = settings.voice
    state.update(hub=hub, journeys=Orchestrator(hub), guard=Guardrails())
    log.info("Runtime ready · cascade %s (stt %s · llm %s · tts %s/%s) · Content Safety %s",
             "configured" if v.configured else "NOT configured (GROQ_API_KEY)", v.transcription_model, v.model,
             v.tts_model, v.voice_name, "on" if settings.content_safety.configured else "off")
    yield
    await state["guard"].close()
    await hub.stop()


app = FastAPI(title="Callwright Agent Runtime (Groq cascade)", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health():
    return {"ok": True, "voice_configured": settings.voice.configured,
            "content_safety_configured": settings.content_safety.configured}


@app.get("/api/journeys")
async def journeys():
    return {"journeys": [j.graph() for j in JOURNEYS.values()],
            "subagents": [{"key": w.key, "display": w.display, "scopes": list(w.scopes)} for w in SUBAGENTS.values()]}


@app.get("/api/config")
async def config():
    v = settings.voice
    return {"pipeline": "cascade", "transcription": v.transcription_model, "model": v.model,
            "tts": v.tts_model, "voice": v.voice_name, "vad": "silero",
            "guard_before_response": v.guard_before_response, "channel": settings.channel}


@app.get("/api/admin/state")
async def admin_state():
    async with httpx.AsyncClient() as c:
        return (await c.get(f"{settings.backend_admin_url}/admin/state")).json()


@app.post("/api/admin/fraud-line")
async def fraud_line(body: dict):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{settings.backend_admin_url}/admin/fraud-line", json=body)).json()


@app.get("/api/recordings/{name}")
async def recording(name: str):
    """A recorded call's (already redacted) event stream, for ?replay= in the UI. No audio."""
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise HTTPException(400, "bad name")
    path = ROOT / "recordings" / f"{name}.jsonl"
    if not path.exists():
        raise HTTPException(404, "no such recording")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if '"audio.delta"' not in ln]
    return PlainTextResponse("\n".join(lines))


@app.post("/api/admin/fault")
async def fault(body: dict):
    """Test hook: make backend calls fail or time out (see mock_backend /admin/fault)."""
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{settings.backend_admin_url}/admin/fault", json=body)).json()


@app.post("/api/admin/reset")
async def reset_backend():
    """Restore the mock systems of record to the demo data (between test scenarios)."""
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{settings.backend_admin_url}/admin/reset")).json()


@app.websocket("/ws")
async def call(ws: WebSocket):
    await ws.accept()
    s = Session()
    queue = s.bus.subscribe()
    bridge: CallBridge | None = None

    async def writer():
        while True:
            evt = await queue.get()
            await ws.send_text(json.dumps(evt, default=str))
            if evt["type"] == "call.ended":
                await asyncio.sleep(0.2)

    w = asyncio.create_task(writer())
    s.bus.emit("session.ready", session_id=s.id, channel=s.channel,
               voice_configured=settings.voice.configured)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                if bridge:
                    await bridge.audio(msg["bytes"])
                continue
            data = json.loads(msg.get("text") or "{}")
            kind = data.get("type")
            if kind == "start":
                s.caller_number = (data.get("caller_number") or "").strip() or None  # ANI from telephony
                # voice=false: text-only channel (chat, or cheap logic tests); identical logic, no audio out
                bridge = CallBridge(s, state["journeys"], state["guard"],
                                    voice_out=data.get("voice", True) is not False)
                if not settings.voice.configured:
                    s.bus.emit("error", where="config", message="GROQ_API_KEY is not set")
                    continue
                try:
                    await bridge.start()
                except Exception as e:  # noqa: BLE001
                    log.exception("voice pipeline start failed")
                    s.bus.emit("error", where="voice", message=f"{type(e).__name__}: {str(e)[:200]}")
            elif kind == "text" and data.get("text") and bridge:
                asyncio.create_task(bridge.text(data["text"]))
            elif kind == "end" and bridge:
                await bridge.hang_up()
    except WebSocketDisconnect:
        pass
    finally:
        if bridge:
            if not s.ended:   # the caller dropped off mid-call: still log the outcome
                with contextlib.suppress(Exception):
                    await bridge.hang_up("caller_disconnected")
            await bridge.stop()
        w.cancel()
        s.bus.unsubscribe(queue)


_dist = ROOT / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
