"""
The voice cascade (STT → LLM → TTS), behind the realtime event protocol CallBridge speaks.

    caller PCM16 24 kHz  ──► Silero VAD (local): turn detection, barge-in
      end of speech      ──► Groq Whisper (speech to text) ──► transcript event
    response.create      ──► Groq chat completions = the COORDINATOR (streamed text + function calls)
      each sentence      ──► Groq Orpheus (text to speech) ──► PCM16 24 kHz audio deltas, in order

CallBridge sends and receives the same events it would with a speech-to-speech service:
  in : session.update, input_audio_buffer.append, conversation.item.create / delete, response.create / cancel
  out: session.updated, input_audio_buffer.speech_started / speech_stopped,
       conversation.item.input_audio_transcription.completed / failed, response.created, response.audio.delta,
       response.audio_transcript.delta / done (response.text.* on a text-only session),
       response.function_call_arguments.done, response.done, error
so guardrails, merging, barge-in and timing logic are unchanged. Only this module knows Groq's API.

Each sentence's text is emitted before it is synthesised, so the output guardrails (which check every finished
sentence) cancel a bad sentence before any of its audio exists. Unlike speech to speech, nothing is heard first.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import itertools
import json
import logging
import re
import struct
import wave
from collections import deque
from typing import Any, AsyncIterator, Optional

import httpx
import numpy as np

from .config import VoiceSettings

log = logging.getLogger("cascade")

RATE = 24000                    # PCM16 mono to and from the caller
VAD_RATE = 16000
VAD_BLOCK = 768                 # 32 ms at 24 kHz → 512 samples at 16 kHz (Silero's frame)
BLOCK_MS = 32
PREROLL_MS = 320                # audio kept from before speech was detected (the first syllable)
MAX_UTTERANCE_MS = 30000
AUDIO_CHUNK = 9600              # bytes per audio.delta (200 ms)
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s+")
_MARKDOWN = re.compile(r"[*_#`>|]+")


# ===================================================================== session configuration
def turn_detection(cfg: VoiceSettings, long_input: bool = False) -> dict[str, Any]:
    """Local Silero VAD. long_input: the customer is reading out digits / an address, so wait longer for the end
    of their turn."""
    return {
        "type": "silero_vad",
        "threshold": cfg.vad_threshold,
        "min_speech_ms": cfg.vad_min_speech_ms,
        "silence_duration_ms": cfg.vad_silence_ms_long_input if long_input else cfg.vad_silence_ms,
        "interrupt_response": True,      # barge-in
        "create_response": not cfg.guard_before_response,
    }


def turn_detection_update(cfg: VoiceSettings, long_input: bool) -> dict[str, Any]:
    """Mid-session change of turn detection only (session.update merges the fields it is given)."""
    return {"type": "session.update", "session": {"turn_detection": turn_detection(cfg, long_input)}}


def session_config(cfg: VoiceSettings, instructions: str, tools: list[dict],
                   voice_out: bool = True) -> dict[str, Any]:
    """The session.update payload. voice_out=False: a text-only channel (chat, cheap logic tests): no TTS."""
    return {"type": "session.update", "session": {
        "modalities": ["text", "audio"] if voice_out else ["text"],
        "instructions": instructions,
        "voice": cfg.voice_name,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": cfg.model_temperature,
        "turn_detection": turn_detection(cfg),
    }}


# ===================================================================== audio helpers
def pcm_to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or not len(x):
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def wav_to_pcm(data: bytes, rate: int = RATE) -> bytes:
    """Any PCM16 / float32 WAV (incl. a streamed header with an unknown data size) → PCM16 mono at `rate`."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, samples = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", data[body:body + 16])
        elif cid == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            samples = data[body:end]
            break
        pos = body + size + (size & 1)
    if fmt is None or samples is None:
        raise ValueError("WAV without fmt/data")
    tag, channels, src_rate, _, _, bits = fmt
    if tag == 3 and bits == 32:
        x = np.frombuffer(samples[:len(samples) // 4 * 4], dtype="<f4").astype(np.float32)
    elif bits == 16:
        x = np.frombuffer(samples[:len(samples) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
    else:
        raise ValueError(f"unsupported WAV format {tag}/{bits}-bit")
    if channels > 1:
        x = x[:len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    x = resample(x, src_rate, rate)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


# ===================================================================== text helpers
def tts_chunks(sentence: str, max_chars: int) -> list[str]:
    """Split a sentence into pieces the TTS accepts (Orpheus: 200 characters), at commas first, then spaces."""
    text = " ".join(_MARKDOWN.sub("", sentence).split())
    out: list[str] = []
    while len(text) > max_chars:
        cut = max((text.rfind(p, 0, max_chars) for p in (", ", "; ", " – ", " - ")), default=-1)
        cut = cut + 1 if cut > max_chars // 3 else text.rfind(" ", 0, max_chars)
        cut = cut if cut > 0 else max_chars
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


class SentenceSplitter:
    """Streaming LLM text → finished sentences (a sentence ends at . ! ? followed by whitespace)."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out = []
        while (m := _SENTENCE_END.search(self.buf)):
            out.append(self.buf[:m.end()].strip())
            self.buf = self.buf[m.end():]
        return [s for s in out if s]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


def chat_messages(system: str, items: list[dict], instruction: Optional[str], tools: bool = True) -> list[dict]:
    """Conversation items → chat messages. Every assistant tool call is followed by its result (or, while the
    tool is still running, by a placeholder): chat APIs reject a tool call with no answer, and a progress line
    can be asked for while tools run. Per-response instructions go last, as a system message.
    tools=False (a reply that must not call a tool): earlier tool exchanges become plain notes. Found live:
    gpt-oss on Groq calls a tool anyway when the history has tool calls, and Groq rejects the reply."""
    outputs = {i["msg"]["tool_call_id"]: i["msg"] for i in items if i["msg"]["role"] == "tool"}
    msgs: list[dict] = [{"role": "system", "content": system}]
    for item in items:
        m = item["msg"]
        if m["role"] == "tool":
            continue
        calls = m.get("tool_calls") or []
        if not tools and calls:
            if m.get("content"):
                msgs.append({"role": "assistant", "content": m["content"]})
            for call in calls:
                out = outputs.get(call["id"])
                msgs.append({"role": "system", "content": f"(Earlier you called {call['function']['name']}"
                                                           f"({call['function']['arguments']}). Result: "
                                                           f"{out['content'] if out else 'still running'})"})
            continue
        msgs.append(m)
        for call in calls:
            msgs.append(outputs.get(call["id"]) or {"role": "tool", "tool_call_id": call["id"],
                                                     "content": json.dumps({"status": "still running"})})
    if instruction:
        msgs.append({"role": "system", "content": instruction})
    return msgs


def chat_tools(tools: list[dict]) -> list[dict]:
    """Realtime-style function definitions → chat-completions tools."""
    return [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters") if k in t}}
            for t in tools]


# ===================================================================== voice activity detection
class Vad:
    """Silero VAD on 32 ms frames with hysteresis: speech starts after `min_speech_ms` above the threshold and
    ends after `silence_duration_ms` below it. Returns ("start", None) / ("stop", utterance PCM) transitions."""

    def __init__(self, model: Any) -> None:
        self.model = model
        self.pending = bytearray()
        self.preroll: deque[bytes] = deque(maxlen=PREROLL_MS // BLOCK_MS)
        self.utterance = bytearray()
        self.speaking = False
        self.run_ms = self.silence_ms = 0

    def prob(self, block: bytes) -> float:
        import torch
        x = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
        return float(self.model(torch.from_numpy(resample(x, RATE, VAD_RATE)[:512].copy()), VAD_RATE).item())

    def feed(self, pcm: bytes, td: dict[str, Any]) -> list[tuple[str, Optional[bytes]]]:
        self.pending += pcm
        threshold = float(td.get("threshold", 0.5))
        off = max(0.05, threshold - 0.15)
        out: list[tuple[str, Optional[bytes]]] = []
        while len(self.pending) >= VAD_BLOCK * 2:
            block = bytes(self.pending[:VAD_BLOCK * 2])
            del self.pending[:VAD_BLOCK * 2]
            p = self.prob(block)
            if not self.speaking:
                self.preroll.append(block)
                self.run_ms = self.run_ms + BLOCK_MS if p >= threshold else (0 if p < off else self.run_ms)
                if self.run_ms >= int(td.get("min_speech_ms", 160)):
                    self.speaking, self.silence_ms = True, 0
                    self.utterance = bytearray(b"".join(self.preroll))
                    self.preroll.clear()
                    out.append(("start", None))
                continue
            self.utterance += block
            self.silence_ms = self.silence_ms + BLOCK_MS if p < off else 0
            if self.silence_ms >= int(td.get("silence_duration_ms", 500)) \
                    or len(self.utterance) >= MAX_UTTERANCE_MS * RATE * 2 // 1000:
                out.append(("stop", bytes(self.utterance)))
                self.speaking, self.run_ms, self.utterance = False, 0, bytearray()
                with contextlib.suppress(Exception):
                    self.model.reset_states()
        return out


def load_vad_model() -> Any:
    from silero_vad import load_silero_vad
    return load_silero_vad(onnx=True)


# ===================================================================== the engine
class CascadeEngine:
    """One call's voice pipeline. Same surface CallBridge used for Voice Live: open / send / events / close."""

    def __init__(self, cfg: VoiceSettings) -> None:
        self.cfg = cfg
        self.session: dict[str, Any] = {}
        self.items: list[dict] = []                  # {"id", "msg"}: the conversation the coordinator sees
        self._out: asyncio.Queue = asyncio.Queue()
        self._http: Optional[httpx.AsyncClient] = None
        self._vad: Optional[Vad] = None
        self._ids = itertools.count(1)
        self._response: Optional[asyncio.Task] = None
        self._stt_last: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    # ---------------------------------------------------------------- lifecycle
    async def open(self) -> None:
        self._http = httpx.AsyncClient(base_url=self.cfg.groq_base_url,
                                       headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
                                       timeout=httpx.Timeout(self.cfg.llm_timeout_s, connect=5))
        self._vad = Vad(await asyncio.to_thread(load_vad_model))
        log.info("cascade ready: stt=%s llm=%s tts=%s/%s", self.cfg.transcription_model, self.cfg.model,
                 self.cfg.tts_model, self.cfg.voice_name)

    async def close(self) -> None:
        self._closed = True
        for t in [self._response, *self._tasks]:
            if t:
                t.cancel()
        if self._http:
            await self._http.aclose()
            self._http = None
        self._out.put_nowait(None)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (ev := await self._out.get()) is not None:
            yield ev

    def _emit(self, type_: str, **data: Any) -> None:
        if not self._closed:
            self._out.put_nowait({"type": type_, **data})

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    @property
    def responding(self) -> bool:
        return bool(self._response and not self._response.done())

    # ---------------------------------------------------------------- client events
    async def send(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "session.update":
            for k, v in (ev.get("session") or {}).items():
                self.session[k] = {**self.session.get(k, {}), **v} if isinstance(v, dict) else v
            self._emit("session.updated", session=self.session)
        elif t == "input_audio_buffer.append":
            self._audio(base64.b64decode(ev.get("audio", "")))
        elif t == "conversation.item.create":
            item = ev.get("item") or {}
            if item.get("type") == "function_call_output":
                self.items.append({"id": self._id("item"), "msg": {
                    "role": "tool", "tool_call_id": item["call_id"], "content": item.get("output", "")}})
            elif item.get("type") == "message":
                text = " ".join(c.get("text", "") for c in item.get("content", []))
                self.items.append({"id": item.get("id") or self._id("item"),
                                   "msg": {"role": item.get("role", "user"), "content": text}})
        elif t == "conversation.item.delete":
            self.items = [i for i in self.items if i["id"] != ev.get("item_id")]
        elif t == "response.create":
            if self.responding:
                self._emit("error", error={"code": "conversation_already_has_active_response",
                                           "message": "a response is already in progress"})
                return
            self._response = asyncio.create_task(self._respond(ev.get("response") or {}))
        elif t == "response.cancel":
            if self.responding:
                self._response.cancel()

    # ---------------------------------------------------------------- caller audio → VAD → STT
    def _audio(self, pcm: bytes) -> None:
        if not self._vad or not pcm:
            return
        td = self.session.get("turn_detection") or {}
        for kind, utterance in self._vad.feed(pcm, td):
            if kind == "start":
                self._emit("input_audio_buffer.speech_started")
                if td.get("interrupt_response", True) and self.responding:
                    self._response.cancel()                 # barge-in
            else:
                item_id = self._id("item")
                self._emit("input_audio_buffer.speech_stopped", item_id=item_id)
                self._stt_last = self._spawn(self._transcribe(item_id, utterance, self._stt_last))

    async def _transcribe(self, item_id: str, pcm: bytes, prev: Optional[asyncio.Task]) -> None:
        text, error = "", None
        try:
            r = await self._http.post("/audio/transcriptions", files={"file": ("speech.wav", pcm_to_wav(pcm),
                                                                                "audio/wav")},
                                      data={"model": self.cfg.transcription_model, "response_format": "verbose_json",
                                            "language": self.cfg.transcription_language, "temperature": "0",
                                            "prompt": self.cfg.transcription_prompt})
            r.raise_for_status()
            body = r.json()
            segments = body.get("segments")
            if segments:      # drop what Whisper itself marks as probably not speech ("Thank you." on noise)
                text = " ".join(s.get("text", "").strip() for s in segments
                                if s.get("no_speech_prob", 0) < self.cfg.no_speech_max)
            else:
                text = body.get("text", "")
            text = text.strip()
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {_http_detail(e)}"
        if prev:                 # transcripts reach the bridge in the order the customer spoke
            with contextlib.suppress(BaseException):
                await prev
        if error:
            self._emit("conversation.item.input_audio_transcription.failed", item_id=item_id,
                       error={"message": error})
            return
        if text:
            self.items.append({"id": item_id, "msg": {"role": "user", "content": text}})
        self._emit("conversation.item.input_audio_transcription.completed", item_id=item_id, transcript=text)
        if text and (self.session.get("turn_detection") or {}).get("create_response") and not self.responding:
            self._response = asyncio.create_task(self._respond({}))

    # ---------------------------------------------------------------- response: LLM → sentences → TTS
    async def _respond(self, opts: dict[str, Any]) -> None:
        rid = self._id("resp")
        audio_out = "audio" in self.session.get("modalities", ["audio"])
        kind = "audio_transcript" if audio_out else "text"
        self._emit("response.created", response={"id": rid})
        splitter, speaker = SentenceSplitter(), _Speaker(self) if audio_out else None
        text, calls, status, emitted = "", {}, "completed", []

        def sentence(s: str) -> None:
            emitted.append(s)
            self._emit(f"response.{kind}.delta", response_id=rid, delta=s + " ")  # output rails check it first
            if speaker:
                speaker.say(s)

        try:
            async for delta in self._chat(opts):
                if delta.get("content"):
                    text += delta["content"]
                    for s in splitter.feed(delta["content"]):
                        sentence(s)
                for tc in delta.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    c["id"] = tc.get("id") or c["id"]
                    fn = tc.get("function") or {}
                    c["name"] += fn.get("name") or ""
                    c["arguments"] += fn.get("arguments") or ""
            for s in splitter.flush():
                sentence(s)
            # Tool calls are known now: record them and let the bridge start them while the ack is still playing.
            tool_calls = [{"id": c["id"] or self._id("call"), "type": "function",
                           "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                          for _, c in sorted(calls.items())]
            msg: dict[str, Any] = {"role": "assistant", "content": text.strip() or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if msg["content"] or tool_calls:
                self.items.append({"id": self._id("item"), "msg": msg})
            for c in tool_calls:
                self._emit("response.function_call_arguments.done", response_id=rid, call_id=c["id"],
                           name=c["function"]["name"], arguments=c["function"]["arguments"])
            if speaker:
                await speaker.finish()
        except asyncio.CancelledError:
            status = "cancelled"
            heard = " ".join(speaker.spoken if speaker else emitted)
            if speaker:
                speaker.cancel()
            # keep history in line with what was actually heard (like auto-truncate); tool calls already
            # handed to the bridge stay, so their results still have a call to answer
            for item in self.items[::-1]:
                if item["msg"]["role"] == "assistant" and item["msg"].get("content") == (text.strip() or None):
                    item["msg"]["content"] = heard or None
                    break
            text = heard
        except Exception as e:  # noqa: BLE001
            status = "failed"
            if speaker:
                speaker.cancel()
            log.warning("response failed: %s", e)
            self._emit("error", error={"code": "llm_error", "message": f"{type(e).__name__}: {_http_detail(e)}"})
        if text.strip():
            done = {"transcript": text.strip()} if audio_out else {"text": text.strip()}
            self._emit(f"response.{kind}.done", response_id=rid, **done)
        self._emit("response.done", response={"id": rid, "status": status})

    async def _chat(self, opts: dict[str, Any]) -> AsyncIterator[dict]:
        cfg, s = self.cfg, self.session
        tool_choice = opts.get("tool_choice") or s.get("tool_choice", "auto")
        tools = bool(s.get("tools")) and tool_choice != "none"   # "none": send no tools at all (see chat_messages)
        base: dict[str, Any] = {
            "stream": True, "temperature": s.get("temperature", cfg.model_temperature),
            "messages": chat_messages(s.get("instructions", ""), self.items, opts.get("instructions"), tools),
        }
        if tools:
            base["tools"] = chat_tools(s["tools"])
            base["tool_choice"] = tool_choice
        # Rate limited (429) → the next model at once (each has its own per-minute budget); every model limited
        # or a server error → wait as long as Groq asks, then go round again.
        models = [cfg.model, *[m for m in cfg.fallback_models if m != cfg.model]]
        tries = [(rnd, m) for rnd in range(3) for m in models]
        for n, (rnd, model) in enumerate(tries):
            body = {**base, "model": model}
            if cfg.reasoning_effort and "gpt-oss" in model:
                body.update(reasoning_effort=cfg.reasoning_effort, include_reasoning=False)
            async with self._http.stream("POST", "/chat/completions", json=body) as r:
                if r.status_code in (429, 500, 502, 503) and n < len(tries) - 1:
                    await r.aread()
                    log.info("llm %s: %s, trying again", model, r.status_code)
                    if r.status_code != 429 or tries[n + 1][0] != rnd:
                        await asyncio.sleep(_retry_after(r))
                    continue
                if r.status_code >= 400:
                    await r.aread()
                    r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise RuntimeError(str(chunk["error"])[:300])
                    for choice in chunk.get("choices") or []:
                        yield choice.get("delta") or {}
                return

    async def tts(self, text: str) -> bytes:
        for attempt in range(3):
            r = await self._http.post("/audio/speech", json={
                "model": self.cfg.tts_model, "voice": self.session.get("voice") or self.cfg.voice_name,
                "input": text, "response_format": "wav"})
            if r.status_code not in (429, 500, 502, 503) or attempt == 2:
                break
            await asyncio.sleep(_retry_after(r))
        r.raise_for_status()
        return await asyncio.to_thread(wav_to_pcm, r.content)


class _Speaker:
    """Ordered TTS: synthesised a few requests ahead of playback, played in order. The first sentence goes at
    once (time to first audio); later ones are packed up to the TTS limit, so a reply costs fewer requests
    (Groq's free tier allows 100 TTS requests a day)."""

    def __init__(self, engine: CascadeEngine) -> None:
        self.engine = engine
        self.sem = asyncio.Semaphore(max(1, engine.cfg.tts_parallel))
        self.queue: asyncio.Queue = asyncio.Queue()
        self.jobs: list[asyncio.Task] = []
        self.spoken: list[str] = []
        self.pending = ""
        self.player = asyncio.create_task(self._play())

    def say(self, sentence: str) -> None:
        limit = self.engine.cfg.tts_max_chars
        for piece in tts_chunks(sentence, limit):
            if not self.jobs:
                self._queue(piece)
            elif self.pending and len(self.pending) + 1 + len(piece) > limit:
                self._queue(self.pending)
                self.pending = piece
            else:
                self.pending = f"{self.pending} {piece}".strip()

    def _queue(self, text: str) -> None:
        job = asyncio.create_task(self._synth(text))
        self.jobs.append(job)
        self.queue.put_nowait((text, job))

    async def _synth(self, text: str) -> bytes:
        async with self.sem:
            return await self.engine.tts(text)

    async def _play(self) -> None:
        while (item := await self.queue.get()) is not None:
            text, job = item
            try:
                pcm = await job
            except Exception as e:  # noqa: BLE001 — one failed sentence must not silence the rest
                self.engine._emit("error", error={"code": "tts_error",
                                                  "message": f"{type(e).__name__}: {_http_detail(e)}"})
                continue
            self.spoken.append(text)
            for i in range(0, len(pcm), AUDIO_CHUNK):
                self.engine._emit("response.audio.delta", delta=base64.b64encode(pcm[i:i + AUDIO_CHUNK]).decode())

    async def finish(self) -> None:
        if self.pending:
            self._queue(self.pending)
            self.pending = ""
        self.queue.put_nowait(None)
        await self.player

    def cancel(self) -> None:
        for t in [self.player, *self.jobs]:
            t.cancel()


def _retry_after(r: httpx.Response, cap: float = 10.0) -> float:
    """Seconds to wait before retrying: the retry-after header, or Groq's "Please try again in 1.234s"."""
    with contextlib.suppress(ValueError):
        return min(cap, float(r.headers.get("retry-after", "")))
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", r.text)
    if m:
        secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + 60 * int(m.group(1) or 0)
        return min(cap, secs + 0.1)
    return 0.5


def _http_detail(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        with contextlib.suppress(Exception):
            return f"{e.response.status_code} {e.response.text[:200]}"
    return str(e)[:200]
