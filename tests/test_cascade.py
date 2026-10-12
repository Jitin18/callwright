"""
The voice cascade engine (cascade.py) against a scripted Groq HTTP transport.

The transport only replays Groq's wire format (SSE chat chunks, a WAV, a transcription JSON); there is no fake
model. These tests check the engine's side of the protocol CallBridge relies on: event order, tool calls,
sentence-first output (so guardrails act before audio exists), barge-in, ordering of transcripts, and history.
"""
import asyncio
import base64
import json
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from services.agent_runtime.cascade import (CascadeEngine, SentenceSplitter, Vad, chat_messages, load_vad_model,
                                            pcm_to_wav, session_config, tts_chunks, wav_to_pcm)
from services.agent_runtime.config import settings
from services.agent_runtime.tools import instructions, tool_definitions

ROOT = Path(__file__).resolve().parents[1]


def _sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps({"choices": [{"delta": c}]}).encode() + b"\n\n" for c in chunks) \
        + b"data: [DONE]\n\n"


def _tone_wav(rate: int = 22050, ms: int = 300) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    return pcm_to_wav((np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes(), rate)


class Groq:
    """Replays Groq responses. chat: a list of SSE bodies, one per chat request, in order."""

    def __init__(self, chat: list[bytes], tts_delay: float = 0.0, stt: list[dict] | None = None,
                 limited: tuple[str, ...] = ()) -> None:
        self.chat, self.tts_delay, self.stt, self.limited = list(chat), tts_delay, list(stt or []), limited
        self.requests: list[tuple[str, dict]] = []

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/chat/completions"):
            body = json.loads(req.content)
            self.requests.append((path, body))
            if body["model"] in self.limited:          # Groq's per-model tokens-per-minute limit
                return httpx.Response(429, json={"error": {"message": "Rate limit reached for model. "
                                                                      "Please try again in 1.5s."}})
            return httpx.Response(200, content=self.chat.pop(0), headers={"content-type": "text/event-stream"})
        if path.endswith("/audio/speech"):
            self.requests.append((path, json.loads(req.content)))
            await asyncio.sleep(self.tts_delay)
            return httpx.Response(200, content=_tone_wav())
        if path.endswith("/audio/transcriptions"):
            body = self.stt.pop(0)
            await asyncio.sleep(body.pop("_delay", 0))
            return httpx.Response(200, json=body)
        return httpx.Response(404)


async def _engine(groq: Groq, voice_out: bool = True) -> CascadeEngine:
    e = CascadeEngine(settings.voice)
    e._http = httpx.AsyncClient(base_url="https://groq.test/openai/v1", transport=httpx.MockTransport(groq))
    await e.send(session_config(settings.voice, instructions(), tool_definitions(), voice_out))
    assert (await e._out.get())["type"] == "session.updated"
    return e


async def _until_done(e: CascadeEngine) -> list[dict]:
    out = []
    while True:
        ev = await asyncio.wait_for(e._out.get(), timeout=3)
        out.append(ev)
        if ev["type"] == "response.done":
            return out


# ===================================================================== helpers
def test_wav_decoding_resamples_to_24k_mono():
    pcm = wav_to_pcm(_tone_wav(rate=22050, ms=300))
    assert abs(len(pcm) // 2 - 7200) <= 1                       # 300 ms at 24 kHz
    stereo = np.zeros(4800, dtype="<i2").tobytes()
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(48000), w.writeframes(stereo)
    assert len(wav_to_pcm(buf.getvalue())) // 2 == 1200          # 2400 stereo frames at 48 kHz → 1200 at 24 kHz


def test_sentences_and_tts_chunks():
    sp = SentenceSplitter()
    assert sp.feed("Sure, let me pull") == []
    assert sp.feed(" that up. Your balance is 3,820 points. And") == ["Sure, let me pull that up.",
                                                                      "Your balance is 3,820 points."]
    assert sp.flush() == ["And"]
    long = "I can see the order, " * 15 + "and it was delivered."
    pieces = tts_chunks("**" + long + "**", 200)
    assert all(len(p) <= 200 for p in pieces) and "*" not in "".join(pieces)
    assert " ".join(pieces) == " ".join(long.split())


def test_every_tool_call_has_an_answer_in_the_chat_history():
    items = [{"id": "a", "msg": {"role": "user", "content": "balance please"}},
             {"id": "b", "msg": {"role": "assistant", "content": None, "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "route_request", "arguments": "{}"}}]}},
             {"id": "d", "msg": {"role": "user", "content": "hello?"}}]                 # spoke while it ran
    msgs = chat_messages("sys", items, "say you're still working on it")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user", "system"]
    assert "still running" in msgs[3]["content"]
    items.append({"id": "e", "msg": {"role": "tool", "tool_call_id": "c1", "content": '{"facts": []}'}})
    assert chat_messages("sys", items, None)[3]["content"] == '{"facts": []}'


# ===================================================================== responses
def test_reply_with_ack_and_tool_call():
    """An acknowledgement and a function call in one response: text first, then the call, then audio, then done."""
    groq = Groq([_sse({"content": "Sure, let me "}, {"content": "check that. "},
                      {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "route_request",
                                                                               "arguments": '{"journeys":'}}]},
                      {"tool_calls": [{"index": 0, "function": {"arguments": ' [{"journey": "rewards.balance.view"}]}'}}]})])

    async def run():
        e = await _engine(groq)
        await e.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "What's my balance?"}]}})
        await e.send({"type": "response.create"})
        evs = await _until_done(e)
        kinds = [ev["type"] for ev in evs]
        assert kinds[0] == "response.created" and kinds[-1] == "response.done"
        first_text = kinds.index("response.audio_transcript.delta")
        call = kinds.index("response.function_call_arguments.done")
        assert first_text < kinds.index("response.audio.delta") and call < kinds.index("response.audio_transcript.done")
        fc = evs[call]
        assert fc["call_id"] == "call_1" and json.loads(fc["arguments"])["journeys"][0]["journey"] == "rewards.balance.view"
        body = groq.requests[0][1]
        assert body["model"] == settings.voice.model and body["tools"][0]["function"]["name"] == "route_request"
        assert body["messages"][-1] == {"role": "user", "content": "What's my balance?"}
        assert e.items[-1]["msg"]["tool_calls"][0]["id"] == "call_1"
        tts = [b for p, b in groq.requests if p.endswith("/audio/speech")]
        assert tts[0]["input"] == "Sure, let me check that." and tts[0]["voice"] == settings.voice.voice_name
    asyncio.run(run())


def test_text_only_session_never_synthesises():
    groq = Groq([_sse({"content": "Hello there."})])

    async def run():
        e = await _engine(groq, voice_out=False)
        await e.send({"type": "response.create", "response": {"instructions": "greet", "tool_choice": "none"}})
        evs = await _until_done(e)
        assert [ev["type"] for ev in evs] == ["response.created", "response.text.delta", "response.text.done",
                                              "response.done"]
        body = groq.requests[0][1]
        assert "tools" not in body and "tool_choice" not in body and body["messages"][-1] == {"role": "system", "content": "greet"}
        assert not any(p.endswith("/audio/speech") for p, _ in groq.requests)
    asyncio.run(run())


def test_cancel_before_audio_keeps_only_what_was_heard():
    """An output rail cancels on the sentence text: its audio is never produced, and history keeps what was heard."""
    groq = Groq([_sse({"content": "Your card ends 1234 5678 9012. Anything else?"})], tts_delay=0.3)

    async def run():
        e = await _engine(groq)
        await e.send({"type": "response.create"})
        assert (await e._out.get())["type"] == "response.created"
        delta = await asyncio.wait_for(e._out.get(), timeout=2)
        assert delta["type"] == "response.audio_transcript.delta"
        await e.send({"type": "response.cancel"})              # the bridge's output rail
        evs = await _until_done(e)
        assert not any(ev["type"] == "response.audio.delta" for ev in evs)
        assert evs[-1]["response"]["status"] == "cancelled"
        assert not any(i["msg"]["role"] == "assistant" and i["msg"].get("content") for i in e.items)
    asyncio.run(run())


def test_second_response_while_one_is_active_is_rejected():
    groq = Groq([_sse({"content": "One moment please."})], tts_delay=0.2)

    async def run():
        e = await _engine(groq)
        await e.send({"type": "response.create"})
        await e.send({"type": "response.create"})
        evs = await _until_done(e)
        assert any(ev["type"] == "error" and ev["error"]["code"] == "conversation_already_has_active_response"
                   for ev in evs)
    asyncio.run(run())


# ===================================================================== caller audio
def test_transcripts_arrive_in_the_order_spoken():
    """The second utterance's transcription returns first; the bridge still sees them in order."""
    groq = Groq([], stt=[{"text": "my card is 1234", "segments": [{"text": "my card is 1234", "no_speech_prob": 0.01}],
                          "_delay": 0.2},
                         {"text": "5678", "segments": [{"text": "5678", "no_speech_prob": 0.02}]}])

    async def run():
        e = await _engine(groq)
        first = e._spawn(e._transcribe("item_a", b"\0\0" * 2400, None))
        e._spawn(e._transcribe("item_b", b"\0\0" * 2400, first))
        got = [(await asyncio.wait_for(e._out.get(), 2)) for _ in range(2)]
        assert [g["item_id"] for g in got] == ["item_a", "item_b"]
        assert [i["msg"]["content"] for i in e.items] == ["my card is 1234", "5678"]
    asyncio.run(run())


def test_whisper_no_speech_segments_are_dropped():
    groq = Groq([], stt=[{"text": "Thank you.", "segments": [{"text": "Thank you.", "no_speech_prob": 0.9}]}])

    async def run():
        e = await _engine(groq)
        await e._transcribe("item_n", b"\0\0" * 2400, None)
        ev = await e._out.get()
        assert ev["type"] == "conversation.item.input_audio_transcription.completed" and ev["transcript"] == ""
        assert not e.items
    asyncio.run(run())


def test_barge_in_cancels_the_reply():
    groq = Groq([_sse({"content": "Let me read you the full terms and conditions now."})], tts_delay=0.3)

    async def run():
        e = await _engine(groq)
        e._vad = type("V", (), {"feed": lambda self, pcm, td: [("start", None)]})()
        await e.send({"type": "response.create"})
        await asyncio.sleep(0.05)
        await e.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(b"\0\0" * 768).decode()})
        evs = await _until_done(e)
        assert any(ev["type"] == "input_audio_buffer.speech_started" for ev in evs)
        assert evs[-1]["response"]["status"] == "cancelled"
    asyncio.run(run())


def test_silero_vad_finds_speech_in_a_recorded_call():
    path = ROOT / "recordings" / "audio_card_read_with_pauses.wav"
    if not path.exists():
        pytest.skip("recordings/ not present")
    with wave.open(str(path)) as w:
        pcm = w.readframes(w.getnframes())
    vad = Vad(load_vad_model())
    td = {"threshold": 0.5, "min_speech_ms": 160, "silence_duration_ms": 500}
    silence = vad.feed(b"\0\0" * 24000, td)
    assert silence == []                                         # one second of silence: nothing
    events = []
    for i in range(0, len(pcm), 4800):                           # 100 ms frames, like the browser
        events += [k for k, _ in vad.feed(pcm[i:i + 4800], td)]
    assert events.count("start") >= 2 and events.count("stop") >= 1
    assert all(a != b for a, b in zip(events, events[1:]))     # start / stop alternate


def test_a_reply_without_tools_sees_earlier_tool_calls_as_notes():
    """Found live: gpt-oss on Groq calls a tool anyway when the history has tool calls and no tools are offered."""
    items = [{"id": "a", "msg": {"role": "assistant", "content": "Sure.", "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "route_request", "arguments": "{}"}}]}},
             {"id": "b", "msg": {"role": "tool", "tool_call_id": "c1", "content": '{"facts": []}'}}]
    msgs = chat_messages("sys", items, "check in", tools=False)
    assert not any(m.get("tool_calls") or m["role"] == "tool" for m in msgs)
    assert [m["role"] for m in msgs] == ["system", "assistant", "system", "system"]
    assert "route_request" in msgs[2]["content"] and '{"facts": []}' in msgs[2]["content"]


def test_rate_limited_model_falls_back_to_the_next_at_once():
    groq = Groq([_sse({"content": "Hello."})], limited=(settings.voice.model,))

    async def run():
        e = await _engine(groq, voice_out=False)
        t0 = asyncio.get_running_loop().time()
        await e.send({"type": "response.create"})
        evs = await _until_done(e)
        assert evs[-1]["response"]["status"] == "completed"
        assert asyncio.get_running_loop().time() - t0 < 1             # no waiting for the limited model
        assert [b["model"] for _, b in groq.requests] == [settings.voice.model, settings.voice.fallback_models[0]]
        assert "reasoning_effort" in groq.requests[1][1]              # gpt-oss options follow the model
    asyncio.run(run())


def test_later_sentences_are_packed_into_fewer_tts_requests():
    groq = Groq([_sse({"content": "Thanks. Your balance is 3,820 points. That's worth about 19 pounds. "
                                  "Anything else?"})])

    async def run():
        e = await _engine(groq)
        await e.send({"type": "response.create"})
        evs = await _until_done(e)
        tts = [b["input"] for p, b in groq.requests if p.endswith("/audio/speech")]
        assert tts == ["Thanks.", "Your balance is 3,820 points. That's worth about 19 pounds. Anything else?"]
        assert sum(ev["type"] == "response.audio_transcript.delta" for ev in evs) == 4   # rails still per sentence
    asyncio.run(run())
