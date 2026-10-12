"""
One live call: bridges the caller's audio (browser or telephony) and the voice cascade (cascade.py:
Silero VAD → Groq Whisper → Groq LLM coordinator → Groq Orpheus TTS), and connects the coordinator's
function calls to the Orchestrator.

    caller audio (PCM16 24 kHz) ──► cascade  (turn detection, speech to text)
    transcript                   ──► [merge split answers] ──► input guardrails ──► response.create
    model function call          ──► Orchestrator ──► subagents (SOP journeys + MCP) ──► function_call_output
    model text (per sentence)    ──► output guardrails (cancel + rephrase on violation, before it is synthesised)
    synthesised audio            ──► caller

Conversation timing (client feedback):
  * long input   – while the pending question expects digits / an address / a security answer, turn detection
                   waits longer (VAD_SILENCE_MS_LONG_INPUT) and transcripts that arrive close together
                   are merged into one answer before guardrails and tools see them.
  * post-ack     – after an acknowledgement or progress line, the answer waits until that line has finished
                   playing plus POST_ACK_DELAY_MS, so it doesn't feel rushed. Barge-in cancels the wait.
  * transfers    – the transfer message is the last thing said: no goodbye. With DEMO_CUT_CALL_ON_TRANSFER the
                   demo session closes once it has played; otherwise it stays up for the real transfer.
  * silence      – if the caller goes quiet after a question, a gentle check-in; then a polite close.
  * echo         – speech detected while the agent is talking that only repeats the agent's own words (its voice
                   leaking from the speaker into the mic) is dropped, and the agent picks up where it was cut off.
                   The browser also gates its mic while the agent speaks (frontend/src/lib/audio.ts).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections import deque
from typing import Any, Optional

from .config import settings
from .events import now_ms
from .guardrails import Guardrails, allowed_numbers
from .orchestrator import Orchestrator
from .normalizers import FINDERS, find_postcode
from .session import Session
from .tools import GREETING_INSTRUCTIONS, instructions, tool_definitions
from .cascade import CascadeEngine, session_config, turn_detection_update

log = logging.getLogger("call")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
PCM_BYTES_PER_MS = 48  # PCM16 mono 24 kHz = 48,000 bytes per second

BLOCKED_SAFETY = ("The customer's last message was blocked by a safety check. Politely say you can't help with "
                  "that, without explaining the check, and offer help with FreshCart Rewards or FreshCart Online.")
BLOCKED_TOPIC = ("The customer asked about something outside FreshCart customer service. In one or two short, "
                 "friendly sentences, say it's not something you can help with on this line, and offer help with "
                 "FreshCart Rewards or FreshCart Online. If a question is still pending, gently come back to it. Don't give "
                 "any opinion or information on the topic itself.")
RESUME = ("You were cut off by background noise, not by the customer. Pick up naturally where you stopped and finish "
          "what you were saying. Don't start again from the beginning and don't mention the interruption.")

_WORD = re.compile(r"[a-z0-9']+")
# Short replies a customer really does say over the agent: never treated as echo.
_NEVER_ECHO = {"yes", "yeah", "yep", "no", "nope", "stop", "wait", "hang", "sorry", "hello", "hi", "hey", "pardon"}
ECHO_WINDOW_MS = 20000      # compare with what the agent said in the last 20 s
ECHO_TAIL_MS = 800          # the agent's voice can still be heard this long after playback ends
NOISE_MAX_MS = 700          # untranscribable sound shorter than this is noise, not a request to repeat


class CallBridge:
    def __init__(self, s: Session, orch: Orchestrator, guard: Guardrails,
                 voice_out: bool = True) -> None:
        self.s, self.orch, self.guard = s, orch, guard
        self.voice_out = voice_out
        self.vl = CascadeEngine(settings.voice)
        self.ready = asyncio.Event()           # session.updated received
        self.idle = asyncio.Event()            # no response is being generated
        self.idle.set()
        self.transcribed = asyncio.Event()     # the current customer turn has its transcript
        self.transcribed.set()
        self.barge = asyncio.Event()           # the customer started speaking (cancels the post-ack pause)
        self.pending_calls: dict[str, asyncio.Task] = {}
        self.outputs: list[tuple[str, dict]] = []
        self._flush_lock = asyncio.Lock()
        self.speech_buffer = ""                 # current response transcript (for output rails)
        self.cancelled_for: Optional[list[str]] = None
        self.t_speech_end: Optional[float] = None
        self.first_audio_sent = False
        self.turn_tool_ms = 0
        self.goodbye_requested = False           # end_call handled; next finished response is the goodbye
        self.response_spoke = False              # the latest response produced speech
        # long input: turn detection mode + transcript merging
        self.long_input = False
        self._merge: list[tuple[str, Optional[str]]] = []
        self._merge_task: Optional[asyncio.Task] = None
        self._t_speech_stopped = 0.0
        self._t_speech_started = 0.0
        # echo: the customer's speech started while the agent was audible; what the agent said recently
        self._interrupted = False
        self._cut = False
        self._agent_said: deque[tuple[float, str]] = deque(maxlen=2000)   # (time, transcript delta)
        # playback estimate (the browser plays audio as it arrives)
        self._resp_bytes = 0
        self._resp_first_audio: Optional[float] = None
        self._resp_had_call = False
        self._progress_response = False
        self.playback_end = 0.0                  # now_ms() when the last spoken response finishes playing
        self.turn_ack_spoken = False             # an ack / progress line was spoken in this customer turn
        self.post_ack_delay_ms = 0               # applied before the current answer (reported in turn.done)
        # the pending question must be asked (found live: the model sometimes gave the facts and stopped)
        self.expect_ask: Optional[str] = None
        self._resp_text = ""
        # transfers and silence
        self.transfer_message_pending = False
        self._silence_task: Optional[asyncio.Task] = None
        self._reprompts = 0
        self._pump: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        """Run work that may wait (pauses, waiting for a reply to finish) off the event pump. The pump must never
        wait: found live, a post-ack pause inside it hid the customer's barge-in and then deadlocked the call."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ================================================================ lifecycle
    async def start(self) -> None:
        await self.vl.open()
        await self.vl.send(session_config(settings.voice, instructions(), tool_definitions(), self.voice_out))
        self._pump = asyncio.create_task(self._events())
        await asyncio.wait_for(self.ready.wait(), timeout=15)
        await self._respond(GREETING_INSTRUCTIONS, tools=False)

    async def stop(self) -> None:
        for t in [*self.pending_calls.values(), *self._tasks, self._merge_task, self._silence_task, self._pump]:
            if t:
                t.cancel()
        await self.vl.close()

    # ================================================================ from the caller
    async def audio(self, pcm16: bytes) -> None:
        if self.ready.is_set() and not self.s.ended:
            await self.vl.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16).decode()})

    async def text(self, text: str) -> None:
        """Typed input (chat / accessibility / tests): same guardrails, then the model replies."""
        self.expect_ask = None
        self._cancel_silence()
        self._reprompts = 0
        self.barge.set()
        self.s.last_transcript = text
        self.turn_ack_spoken = False
        if await self._guard_turn(text, item_ids=[]):
            await self.vl.send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
            await self._respond()

    async def hang_up(self, ended_by: str = "caller_hung_up") -> None:
        """The caller (or the connection) ended the call: log the outcome once, then close."""
        if not self.s.ended:
            await self.orch.log_outcome(self.s, ended_by=ended_by)
        self._finish(ended_by)

    # ================================================================ from the cascade
    async def _events(self) -> None:
        reason = "voice_closed"
        try:
            async for ev in self.vl.events():
                try:
                    await self._handle(ev)
                except Exception:  # noqa: BLE001 — one bad event must not drop the call
                    log.exception("event handling failed: %s", ev.get("type"))
        except Exception as e:  # noqa: BLE001
            reason = "voice_error"
            self.s.bus.emit("error", where="voice", message=f"{type(e).__name__}: {str(e)[:200]}")
        # The voice pipeline is gone: never leave the caller on a dead line.
        if not self.s.ended:
            self.s.bus.emit("error", where="voice", message="Voice pipeline closed; ending the call")
            await self.orch.log_outcome(self.s, ended_by=reason)
            self._finish(reason)

    async def _handle(self, ev: dict[str, Any]) -> None:
        t = ev.get("type", "")
        bus = self.s.bus
        if t == "session.updated":
            td = (ev.get("session") or {}).get("turn_detection") or {}
            if self.ready.is_set():
                bus.emit("vad.updated", silence_ms=td.get("silence_duration_ms"), long_input=self.long_input)
            else:
                bus.emit("voice.ready", voice=settings.voice.voice_name, model=settings.voice.model,
                         silence_ms=td.get("silence_duration_ms"))
            self.ready.set()

        # ---- the caller
        elif t == "input_audio_buffer.speech_started":
            self._t_speech_started = now_ms()
            # the agent was (or may still be) audible: this could be its own voice coming back through the mic.
            # _cut: it was actually mid-reply, so barge-in stopped it.
            self._cut = not self.idle.is_set() or now_ms() < self.playback_end
            self._interrupted = self._cut or now_ms() < self.playback_end + ECHO_TAIL_MS
            self.transcribed.clear()
            self.barge.set()
            self._cancel_silence()
            self._reprompts = 0
            if self._merge_task:                          # more of the same answer is coming: keep waiting
                self._merge_task.cancel()
                self._merge_task = None
            bus.emit("audio.clear")                       # barge-in: stop playback immediately
            bus.emit("agent.state", agent="coordinator", state="listening", label="Customer speaking")
        elif t == "input_audio_buffer.speech_stopped":
            self.t_speech_end, self.first_audio_sent, self.turn_tool_ms = now_ms(), False, 0
            self._t_speech_stopped = now_ms()
        elif t == "conversation.item.input_audio_transcription.completed":
            text = (ev.get("transcript") or "").strip()
            self.transcribed.set()
            interrupted, cut = self._interrupted, self._cut
            self._interrupted = self._cut = False
            if text and interrupted and self._is_echo(text):
                bus.emit("echo.ignored", words=len(text.split()))
                if ev.get("item_id"):
                    await self.vl.send({"type": "conversation.item.delete", "item_id": ev["item_id"]})
                text = ""
            if not text:
                if self._merge:
                    self._schedule_merge()
                elif cut:
                    self._spawn(self._resume())
                return
            if self.long_input and settings.voice.guard_before_response:
                self._merge.append((text, ev.get("item_id")))
                self._schedule_merge()
                return
            self._spawn(self._customer_turn(text, [ev.get("item_id")]))
        elif t == "conversation.item.input_audio_transcription.failed":
            self.transcribed.set()
            cut = self._cut
            self._interrupted = self._cut = False
            if self._merge:
                self._schedule_merge()
            elif cut:
                self._spawn(self._resume())             # a noise cut the agent off: carry on, don't ask to repeat
            elif self._t_speech_stopped - self._t_speech_started >= NOISE_MAX_MS:
                self._spawn(self._respond("You didn't catch that. Briefly and politely ask the customer to say it "
                                          "again.", tools=False))
            else:
                bus.emit("noise.ignored", ms=round(self._t_speech_stopped - self._t_speech_started))

        # ---- the model's response
        elif t == "response.created":
            self.idle.clear()
            self.speech_buffer, self.cancelled_for, self.response_spoke, self._resp_text = "", None, False, ""
            self._resp_bytes, self._resp_first_audio, self._resp_had_call = 0, None, False
            bus.emit("agent.state", agent="coordinator", state="speaking", label="Replying")
        elif t in ("response.audio.delta", "response.output_audio.delta"):
            if not self.cancelled_for:
                delta = ev.get("delta", "")
                self._resp_bytes += len(delta) * 3 // 4
                if self._resp_first_audio is None:
                    self._resp_first_audio = now_ms()
                if not self.first_audio_sent and self.t_speech_end:
                    self.first_audio_sent = True
                    bus.emit("turn.done", turn_id=f"t{self.s.turn_no}", latency={
                        "first_audio": round(now_ms() - self.t_speech_end), "tools": self.turn_tool_ms,
                        "post_ack_delay_ms": self.post_ack_delay_ms})
                bus.emit("audio.delta", audio=delta)
        elif t in ("response.audio_transcript.delta", "response.output_audio_transcript.delta", "response.text.delta"):
            delta = ev.get("delta", "")
            self._agent_said.append((now_ms(), delta))
            await self._check_speech(delta)
        elif t in ("response.audio_transcript.done", "response.output_audio_transcript.done", "response.text.done"):
            said = (ev.get("transcript") or ev.get("text") or "").strip()
            if said and not self.cancelled_for:
                self.response_spoke = True
                self._resp_text += " " + said
                bus.emit("assistant.say", turn_id=f"t{self.s.turn_no}", kind="reply",
                         text=self.s.redactor.assistant_text(said))
                self.s.add_history("assistant", self.s.redactor.assistant_text(said))
        elif t == "response.function_call_arguments.done":
            self._resp_had_call = True
            self._tool_call(ev["call_id"], ev.get("name", ""), ev.get("arguments") or "{}")
        elif t == "response.done":
            if self._resp_first_audio is not None:
                self.playback_end = max(self.playback_end, self._resp_first_audio + self._resp_bytes / PCM_BYTES_PER_MS)
            if self.response_spoke and (self._resp_had_call or self._progress_response or self.pending_calls):
                self.turn_ack_spoken = True          # an acknowledgement / progress line: pause before the answer
            self._progress_response = False
            self.idle.set()
            bus.emit("agent.state", agent="coordinator", state="idle")
            self._spawn(self._after_response())
        elif t == "error":
            err = ev.get("error", {})
            bus.emit("error", where="voice", message=f"{err.get('code')}: {err.get('message')}")
            if err.get("code") != "conversation_already_has_active_response":
                self.idle.set()  # a rejected response.create must not leave the call waiting forever

    # ================================================================ customer turns
    def _schedule_merge(self) -> None:
        """Long input: an answer that's already complete in format (a full card / order / phone number, a full
        postcode) is checked at once. A partial one waits longer for the rest; anything else waits the normal
        window. More speech from the customer cancels the wait (speech_started)."""
        if self._merge_task:
            self._merge_task.cancel()
        complete = self._answer_complete(self._merged_text(self._merge))
        if complete:
            wait = 0.0
        else:
            window = settings.voice.merge_window_incomplete_ms if complete is False \
                else settings.voice.merge_window_ms
            wait = max(0.0, self._t_speech_stopped + window - now_ms()) / 1000
        self.s.bus.emit("answer.format", complete=complete, wait_ms=round(wait * 1000))
        self._merge_task = asyncio.create_task(self._merge_after(wait))

    @staticmethod
    def _merged_text(parts: list[tuple[str, Optional[str]]]) -> str:
        """One answer from its fragments. Transcription ends each fragment with a full stop ("1234567." + "8901."),
        which would split the number in two: a pause, not the end of a sentence."""
        texts = [p for p, _ in parts]
        return " ".join([re.sub(r"[.!?…]+$", "", p) for p in texts[:-1]] + texts[-1:])

    def _answer_complete(self, text: str) -> Optional[bool]:
        """True: every long value the pending question wants is complete in what was heard (joined with any part
        heard earlier). False: part of one was heard, more is coming. None: can't tell (a security answer, or no
        digits / postcode at all, e.g. a question back) — use the normal window."""
        run = self.s.pending_run()
        q = run.question if run else None
        if not q or not text:
            return None
        slots = [sl for sl in q.slots if sl.name in q.missing and sl.long_input and sl.required]
        if not slots:
            return None
        for sl in slots:
            prior = q.partial.get(sl.name, "")
            joined = f"{prior} {text}".strip()
            if sl.kind == "address":
                if not find_postcode(joined).ok:
                    return False if re.search(r"\d", joined) else None
                continue
            finder = FINDERS.get(sl.kind)
            if not finder:
                return None                          # security answer / memorable word: can't tell from the words
            found = finder(text)
            if not found.ok and prior:
                found = finder(joined)
            if not found.ok:
                return False if found.incomplete else None
            if sl.kind in ("phone", "mobile") and len(found.value) < 11:
                return False                         # 10 digits is valid in the UK but usually means more is coming
        return True

    async def _merge_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        parts, self._merge, self._merge_task = self._merge, [], None
        if not parts:
            return
        text = self._merged_text(parts)
        if len(parts) > 1:
            self.s.bus.emit("turn.merged", fragments=len(parts))
        await self._customer_turn(text, [i for _, i in parts])

    async def _customer_turn(self, text: str, item_ids: list[Optional[str]]) -> None:
        self.expect_ask = None
        self.s.last_transcript = text
        self.turn_ack_spoken = False
        if await self._guard_turn(text, item_ids=item_ids) and settings.voice.guard_before_response:
            await self._respond()

    # ================================================================ echo
    def _is_echo(self, text: str) -> bool:
        """The transcript only repeats what the agent itself said just now: its voice came back through the mic."""
        cutoff = now_ms() - ECHO_WINDOW_MS
        said = set(_WORD.findall("".join(d for t, d in self._agent_said if t >= cutoff).lower()))
        words = _WORD.findall(text.lower())
        if not words or not said:
            return False
        if len(words) <= 3 and any(w in _NEVER_ECHO for w in words):
            return False
        hit = sum(w in said for w in words) / len(words)
        return hit >= (0.7 if len(words) >= 3 else 1.0)

    async def _resume(self) -> None:
        """A false interruption (echo or noise) stopped the agent: let it finish what it was saying."""
        if self.s.ended or not self.idle.is_set() or self.pending_calls or self.outputs or self.s.transferred:
            return                                   # something else is already speaking or about to
        self.s.bus.emit("resume")
        await self._respond(RESUME, tools=not self._resp_had_call)

    # ================================================================ input guardrails
    async def _guard_turn(self, text: str, item_ids: list[Optional[str]]) -> bool:
        s = self.s
        s.turn_no += 1
        s.allowed_numbers |= allowed_numbers(text)
        pending = s.pending_run()
        missing = [sl for sl in pending.question.slots if sl.name in pending.question.missing] if pending else []
        if any(sl.sensitive for sl in missing):
            shown = "⟨customer gave personal details: " + ", ".join(sl.name.replace("_", " ") for sl in missing) + "⟩"
        else:
            shown = s.redactor.text(text)
        s.bus.emit("turn.started", turn_id=f"t{s.turn_no}", text=shown, source="voice")
        s.add_history("customer", shown)
        verdict = await self.guard.check_input(s, text)
        blocked = verdict.blocked
        if not blocked:
            return True
        if not settings.voice.guard_before_response:
            await self.vl.send({"type": "response.cancel"})
            s.bus.emit("audio.clear")
        for item_id in filter(None, item_ids):
            await self.vl.send({"type": "conversation.item.delete", "item_id": item_id})
        await self._respond(BLOCKED_TOPIC if blocked.name == "topic" else BLOCKED_SAFETY, tools=False)
        return False

    # ================================================================ output guardrails
    async def _check_speech(self, delta: str) -> None:
        if self.cancelled_for or not delta:
            return
        self.speech_buffer += delta
        parts = _SENTENCE.split(self.speech_buffer)
        for sentence in parts[:-1]:
            violations = self.guard.check_sentence(self.s, sentence)
            if violations:
                self.cancelled_for = violations
                await self.vl.send({"type": "response.cancel"})
                self.s.bus.emit("audio.clear")
                return
        self.speech_buffer = parts[-1]

    # ================================================================ tool calls
    def _tool_call(self, call_id: str, name: str, arguments: str) -> None:
        self.pending_calls[call_id] = asyncio.create_task(self._run_tool(call_id, name, arguments))

    async def _run_tool(self, call_id: str, name: str, arguments: str) -> None:
        s = self.s
        progress = asyncio.create_task(self._progress_update())
        t0 = now_ms()
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        s.bus.emit("agent.state", agent="coordinator", state="thinking", label=name)
        # card/order numbers are read from the transcript by code, so wait for it briefly.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.transcribed.wait(), timeout=3)
        try:
            async with s.lock:
                if name == "route_request":
                    result = await self.orch.route(s, args.get("journeys", []), args.get("known_answers"),
                                                   args.get("customer_mood"))
                elif name == "submit_answer":
                    result = await self.orch.answer(s, args.get("answers", {}), args.get("customer_mood"))
                elif name == "transfer_to_colleague":
                    result = await self.orch.transfer(s, args.get("reason", ""))
                elif name == "end_call":
                    result = await self.orch.end_call(s)
                else:
                    result = {"error": f"unknown tool {name}"}
        except Exception as e:  # noqa: BLE001
            s.bus.emit("error", where=f"tool:{name}", message=f"{type(e).__name__}: {str(e)[:200]}")
            result = {"error": "A system problem occurred. Apologise and offer to transfer to a colleague."}
        finally:
            progress.cancel()
            s.bus.emit("agent.state", agent="coordinator", state="idle")
        self.turn_tool_ms += round(now_ms() - t0)
        self.outputs.append((call_id, result))
        self.pending_calls.pop(call_id, None)
        await self._flush()

    async def _progress_update(self) -> None:
        """If the systems are slow, say one short line about what's happening (generated, not canned)."""
        await asyncio.sleep(settings.progress_update_ms / 1000)
        await self.idle.wait()
        doing = self.orch.progress(self.s)
        if doing and self.pending_calls:
            self._progress_response = True
            await self._respond(f"In one short sentence, let the customer know you're still working on it: "
                                f"currently '{doing}'. Don't mention systems by name and don't ask anything.",
                                tools=False)

    async def _post_ack_pause(self) -> None:
        """Let the acknowledgement finish playing, then pause POST_ACK_DELAY_MS. Barge-in cancels at once."""
        self.post_ack_delay_ms = 0
        if not self.turn_ack_spoken:
            return
        self.turn_ack_spoken = False
        wait = self.playback_end + settings.post_ack_delay_ms - now_ms()
        if wait <= 0:
            return      # the acknowledgement ended long ago: no extra wait
        self.barge.clear()
        t0 = now_ms()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.barge.wait(), timeout=wait / 1000)
        self.post_ack_delay_ms = round(now_ms() - t0)
        self.s.bus.emit("post_ack_pause", ms=self.post_ack_delay_ms, cancelled=self.barge.is_set())

    async def _flush(self) -> None:
        """Send tool results back only when every call from the response is done and nothing is speaking."""
        async with self._flush_lock:
            if self.pending_calls or not self.outputs or not self.idle.is_set():
                return
            outputs, self.outputs = self.outputs, []
            await self._post_ack_pause()
            for call_id, result in outputs:
                await self.vl.send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call_id, "output": json.dumps(result, default=str)}})
            asks = [r["ask"] for _, r in outputs if isinstance(r.get("ask"), dict)]
            self.expect_ask = asks[-1].get("purpose") if asks else None
            if any(r.get("transferred") and not r.get("error") for _, r in outputs):
                self.transfer_message_pending = True     # the next response is the transfer message
            if self.s.closing:
                if self.response_spoke and all(r.get("ok") for _, r in outputs):
                    self._finish("assistant")  # the goodbye was already spoken in the same response as end_call
                    return
                self.goodbye_requested = True
            await self._set_long_input()
            await self._respond()

    async def _set_long_input(self) -> None:
        """Wait longer for the end of the customer's turn while they read out digits, an address or a secret."""
        run = self.s.pending_run()
        want = bool(run and run.question and run.question.long_input) and not self.s.transferred
        if want != self.long_input:
            self.long_input = want
            await self.vl.send(turn_detection_update(settings.voice, want))
            self.s.bus.emit("vad.mode", long_input=want,
                            silence_ms=settings.voice.vad_silence_ms_long_input if want
                            else settings.voice.vad_silence_ms)

    async def _after_response(self) -> None:
        if self.cancelled_for:  # output rail stopped the reply: ask the model to say it properly
            v, self.cancelled_for = self.cancelled_for, None
            await self._respond("Your last reply was stopped by a safety check because you must "
                                + "; ".join(v) + ". Say it again correctly using only facts from tool results.")
            return
        if self.transfer_message_pending and self.response_spoke and not self.pending_calls and not self.outputs:
            self.transfer_message_pending = False
            await self.orch.log_outcome(self.s, ended_by="transferred")
            if settings.demo_cut_call_on_transfer:
                await asyncio.sleep(max(0.0, self.playback_end - now_ms()) / 1000 + 0.5)
                self._finish("transferred")
            return
        if self.goodbye_requested and not self.pending_calls and not self.outputs:
            self._finish("assistant")
            return
        if self.expect_ask and self.response_spoke and not self.pending_calls and not self.outputs:
            purpose, self.expect_ask = self.expect_ask, None
            if "?" not in self._resp_text:
                self.s.bus.emit("ask.reminder", purpose=purpose)
                await self._respond("You gave the information but didn't ask the pending question. Ask it now, "
                                    f"naturally, in one short sentence: {purpose}", tools=False)
                return
        await self._flush()
        self._arm_silence()

    # ================================================================ silence
    def _cancel_silence(self) -> None:
        if self._silence_task:
            self._silence_task.cancel()
            self._silence_task = None

    def _arm_silence(self) -> None:
        if self.s.ended or self.s.closing or self.s.transferred or self.pending_calls or self.outputs:
            return
        self._cancel_silence()
        self._silence_task = asyncio.create_task(self._silence_watch())

    async def _silence_watch(self) -> None:
        wait = max(0.0, self.playback_end - now_ms()) / 1000 + settings.silence_reprompt_s
        await asyncio.sleep(wait)
        self._silence_task = None
        if self.s.ended or not self.idle.is_set():
            return
        self._reprompts += 1
        self.s.bus.emit("silence", reprompt=self._reprompts)
        if self._reprompts <= settings.silence_max_reprompts:
            await self._respond("The customer has gone quiet. In one short, gentle sentence, check they're still "
                                "there (don't repeat the whole question). No rush.", tools=False)
            return
        self.s.closing = True
        await self.orch.log_outcome(self.s, ended_by="caller_silent")
        self.goodbye_requested = True
        await self._respond("The customer hasn't responded for a while. In one short sentence, say you'll end the "
                            "call for now and that they're welcome to call back any time. Goodbye.", tools=False)

    # ================================================================ helpers
    async def _respond(self, instruction: Optional[str] = None, tools: bool = True) -> None:
        """Ask the model to speak. Waits until no other response is active (one at a time)."""
        await self.idle.wait()
        if self.s.ended:
            return
        response: dict[str, Any] = {}
        if instruction:
            response["instructions"] = instruction
        if not tools:
            response["tool_choice"] = "none"
        self.idle.clear()
        await self.vl.send({"type": "response.create", "response": response} if response else {"type": "response.create"})

    def _finish(self, reason: str = "") -> None:
        if not self.s.ended:
            self.s.ended = True
            self._cancel_silence()
            self.s.bus.emit("call.ended", reason=reason)
