"""
Orchestrator: turns the coordinator's decisions (Voice Live function calls) into subagent work.

    route_request(journeys)    → hand each journey to its subagent (ID&V first when required), in parallel
    submit_answer(answers)     → validate the answer to the pending question, hand it to the subagent that asked
    transfer_to_colleague      → Escalation subagent (warm transfer)
    end_call                   → log a summary, close the call after the goodbye

Every method returns the same structured result for the coordinator to speak from:
    {"facts": [...], "ask": {...} | None, "outcomes": [...], "close_call": bool}

No LLM is called here or in the subagents: the coordinator decides WHAT the customer wants and what they
said; this module and the subagents decide everything else, deterministically, from the SOPs.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from .guardrails import allowed_numbers
from .journeys.dsl import Ctx, End
from .journeys.engine import IDV_LEVEL_OF_JOURNEY, JourneyRun, idv_covers, idv_satisfied
from .journeys.registry import IDV_JOURNEY, JOURNEYS, ROUTABLE, SUBAGENTS
from . import outcomes
from .mcp_hub import McpHub
from .normalizers import FINDERS, NOTHING, find_postcode, normalise, normalise_bool
from .session import Session
from .subagents import SENSITIVE_KEYS, Subagent, subagent_notes


_PERSON = re.compile(r"\b(person|human|someone|somebody|colleague|agent|advisor|adviser|operator|representative|"
                     r"manager|supervisor|real (?:person|people)|speak to|talk to|put me through|transfer me)\b", re.I)
_OFFERED = re.compile(r"\b(colleague|someone|somebody|advisor|adviser|agent|person|put you through|transfer)\b", re.I)


def wants_a_person(s: Session) -> bool:
    """The customer asked for a person, or said yes right after the agent offered one."""
    said = s.last_transcript or ""
    if _PERSON.search(said):
        return True
    last_agent = next((h["text"] for h in reversed(s.history) if h["role"] == "assistant"), "")
    return bool(_OFFERED.search(last_agent)) and normalise_bool(said) is True


class TurnCtx:
    """Scratch state for one coordinator function call."""

    def __init__(self, s: Session, known: Optional[dict] = None) -> None:
        self.s = s
        self.known = known or {}           # details the customer already gave in the same sentence
        self.facts: list[dict] = []
        self.touched: list[JourneyRun] = []
        self.activity: list[str] = []      # human-readable steps, used for progress updates
        self.tool_ms = 0
        self.prefilled: set[str] = set()
        self.end_call = False

    def step(self, label: str, ms: int) -> None:
        self.activity.append(label)
        self.tool_ms += ms


class Orchestrator:
    def __init__(self, hub: McpHub) -> None:
        self.hub = hub
        self.current: dict[str, TurnCtx] = {}   # session id → call in progress (for progress updates)

    def subagent(self, s: Session, key: str) -> Subagent:
        if key not in s.subagents:
            s.subagents[key] = Subagent(SUBAGENTS[key], self.hub, s, on_transfer=lambda args: self._on_transfer(s, args))
        return s.subagents[key]

    def _on_transfer(self, s: Session, args: dict) -> dict:
        """A subagent is about to warm-transfer: record why, and build the colleague's handover package."""
        reason = args.get("summary") or "Transfer requested"
        s.transfer = {"queue": args.get("queue"), "reason": reason}
        return outcomes.handover(s, args.get("queue") or "general", reason)

    @staticmethod
    def _after_transfer(s: Session) -> dict:
        return {"error": "This call has already been transferred to a colleague, who takes over from here. "
                         "Don't start anything new and don't say goodbye.", "transferred": True,
                "facts": [], "ask": None, "outcomes": [], "close_call": False}

    # ================================================================ tool: route_request
    async def route(self, s: Session, journeys: list[dict], known_answers: Optional[dict] = None,
                    mood: Optional[str] = None) -> dict:
        self._mood(s, mood)
        if s.transferred:
            return self._after_transfer(s)
        if not journeys:
            return {"error": "No journey was chosen. If the customer hasn't finished saying what they need, don't "
                             "call a tool: just let them carry on.", **self._result(s, TurnCtx(s))}
        valid = {j.id for j in ROUTABLE}
        active = {r.journey.id for r in s.runs if not r.done}
        kept, seen = [], set()
        for it in journeys or []:
            jid = it.get("journey")
            if jid == "escalation.human" and not wants_a_person(s):
                # The same consent rule as transfer_to_colleague: routing straight to escalation can't bypass it.
                s.bus.emit("coordinator.dropped_intent", journey=jid, reason="customer has not asked for a person")
                continue
            if jid in valid and jid not in active and jid not in seen:
                kept.append(it)
                seen.add(jid)
            else:
                s.bus.emit("coordinator.dropped_intent", journey=jid)
        s.bus.emit("coordinator.plan", intents=[k["journey"] for k in kept], sentiment=s.sentiment)
        s.requests.extend(JOURNEYS[k["journey"]].title for k in kept)
        if not kept:
            return {"error": "None of those journeys can be started (unknown, already in progress, or a colleague the "
                             "customer hasn't asked for: offer one and wait for a clear yes). If the customer is "
                             "answering the pending question, use submit_answer.",
                    **self._result(s, TurnCtx(s))}
        tc = TurnCtx(s, known_answers)
        self.current[s.id] = tc
        s.bus.emit("agent.state", agent="orchestrator", state="delegating", label=f"{len(kept)} journey(s)")
        try:
            await asyncio.gather(*[self._start(tc, it["journey"], it.get("query")) for it in kept])
        finally:
            self.current.pop(s.id, None)
            s.bus.emit("agent.state", agent="orchestrator", state="idle")
        return self._result(s, tc)

    # ================================================================ tool: submit_answer
    async def answer(self, s: Session, answers: dict, mood: Optional[str] = None) -> dict:
        self._mood(s, mood)
        if s.transferred:
            return self._after_transfer(s)
        run = s.pending_run()
        if not run or not run.question:
            return {"error": "No question is pending. If the customer wants something new, call route_request.",
                    **self._result(s, TurnCtx(s))}
        tc = TurnCtx(s)
        self.current[s.id] = tc
        try:
            values, invalid = self._validate(run, answers or {}, s.last_transcript)
            s.bus.emit("subagent.extracted", agent=run.journey.subagent, run_id=run.run_id,
                       fields={k: s.redactor.value(k, v) if k in SENSITIVE_KEYS or self._secret(run, k) else v
                               for k, v in values.items()}, invalid=invalid)
            s.bus.emit("handoff", source="coordinator", target=run.journey.subagent, journey=run.journey.id,
                       run_id=run.run_id, title=run.journey.title, resume=True)
            self.subagent(s, run.journey.subagent).supply(run, values, invalid)
            await self._advance(tc, run)
        finally:
            self.current.pop(s.id, None)
        return self._result(s, tc)

    # ================================================================ tool: transfer_to_colleague
    async def transfer(self, s: Session, reason: str) -> dict:
        if s.transferred:
            return self._after_transfer(s)
        if not wants_a_person(s):
            # Consent is checked by code, not left to the model: found live, "Okay, thanks" after a failed
            # identity check was once treated as a request for a colleague.
            return {"error": "The customer hasn't asked for a colleague. If a colleague could help, offer it and wait "
                             "for a clear yes; otherwise carry on.", **self._result(s, TurnCtx(s))}
        s.requests.append("Speak to a colleague")
        tc = TurnCtx(s)
        await self._start(tc, "escalation.human", reason or "Customer asked for a colleague.")
        return self._result(s, tc)

    # ================================================================ tool: end_call
    async def end_call(self, s: Session) -> dict:
        if s.transferred:   # a transfer is not an ending: the colleague takes over, so no goodbye
            return self._after_transfer(s)
        s.closing = True
        await self.log_outcome(s, ended_by="assistant")
        return {"ok": True, "guidance": "Thank the customer warmly and say goodbye."}

    async def log_outcome(self, s: Session, ended_by: str) -> dict:
        """Log the call outcome once (CRM contact history via MCP): disposition, transfer reason, journeys."""
        if s.outcome is not None:
            return s.outcome
        summary = self._summary(s)
        s.outcome = {**summary, "ended_by": ended_by}
        s.bus.emit("call.summary", **s.outcome)
        try:
            await self.hub.call("case_management", "log_contact_outcome", {
                "session_id": s.id, "journey": "call", "outcome": "call_complete",
                "account_id": s.idv.get("account_id"), "note": summary["headline"],
                "disposition": summary["disposition"], "transfer_reason": summary["transfer_reason"],
                "journeys": summary["journeys"], "ended_by": ended_by})
        except Exception as e:  # noqa: BLE001 — a logging failure must not break the call
            s.bus.emit("error", where="log_outcome", message=str(e)[:200])
        return s.outcome

    # ================================================================ delegation to subagents
    def _new_run(self, s: Session, journey_id: str, params: dict) -> JourneyRun:
        journey = JOURNEYS[journey_id]
        run = JourneyRun(journey, Ctx(session_id=s.id, channel=s.channel, vault=s.vault, idv=s.idv, params=params,
                                      caller_number=s.caller_number))
        s.runs.append(run)
        self.subagent(s, journey.subagent).adopt(run)
        return run

    async def _start(self, tc: TurnCtx, journey_id: str, query: Optional[str]) -> None:
        s = tc.s
        # "known" = details from the routing sentence; kept on the run so a journey released later
        # (after ID&V) can still skip questions the customer already answered.
        run = self._new_run(s, journey_id, {"query": query, "is_balance_query": True, "known": dict(tc.known),
                                            "utterance": s.last_transcript})
        journey = run.journey
        s.bus.emit("handoff", source="coordinator", target=journey.subagent, journey=journey.id, run_id=run.run_id,
                   title=journey.title, graph=journey.graph())
        need = journey.requires_idv
        if need and not idv_satisfied(run.ctx, need):
            run.status = "blocked"
            s.bus.emit("journey.status", run_id=run.run_id, journey=journey.id, status="blocked",
                       detail=f"waiting for {need} ID&V")
            idv_run = self._active_idv(s, need)
            if idv_run is None:  # ID&V runs once per call and is shared by every journey that needs it
                idv_run = self._new_run(s, IDV_JOURNEY[need], {"known": dict(tc.known), "utterance": s.last_transcript})
                run.blocked_on = idv_run.run_id
                s.bus.emit("handoff", source="orchestrator", target="idv", journey=idv_run.journey.id,
                           run_id=idv_run.run_id, title=idv_run.journey.title, graph=idv_run.journey.graph(),
                           reason=f"{journey.title} requires {need} ID&V")
                await self._advance(tc, idv_run, prefill=True)
            else:
                run.blocked_on = idv_run.run_id
            return
        await self._advance(tc, run, prefill=True)

    async def _advance(self, tc: TurnCtx, run: JourneyRun, prefill: bool = False) -> None:
        s = tc.s
        subagent = self.subagent(s, run.journey.subagent)
        s.bus.emit("agent.state", agent=subagent.key, state="working", label=run.journey.title)
        try:
            tc.facts.extend(await subagent.execute(run, tc.step))
            # First question of a new journey: the answer may already be in the customer's sentence.
            if prefill and run.status == "awaiting_input" and run.run_id not in tc.prefilled:
                tc.prefilled.add(run.run_id)
                known = {**run.ctx.params.get("known", {}), **tc.known}
                # Pre-fill reads the sentence the request was made in, not the current one: a journey released
                # after ID&V must not take "twelfth of April 1988" (a date of birth) as its search date.
                said = run.ctx.params.get("utterance", s.last_transcript)
                values, _ = self._validate(run, known, said, prefill=True)
                if values:
                    subagent.supply(run, values, {})
                    tc.facts.extend(await subagent.execute(run, tc.step))
        except Exception as e:  # noqa: BLE001 — a journey error becomes a fact, never a crash
            run.status = "error"
            s.bus.emit("error", where=f"journey:{run.journey.id}", message=f"{type(e).__name__}: {str(e)[:200]}")
            tc.facts.append({"kind": "system_issue", "facts": {"journey": run.journey.title}})
        if run not in tc.touched:
            tc.touched.append(run)
        s.bus.emit("journey.status", run_id=run.run_id, journey=run.journey.id, status=run.status,
                   outcome=run.outcome, node=run.node_id)
        s.bus.emit("agent.state", agent=subagent.key, state="idle")
        s.bus.emit("handoff.return", source=subagent.key, target="orchestrator", run_id=run.run_id, status=run.status)

        end = run.journey.by_id.get(run.node_id)
        if run.done and isinstance(end, End):
            if end.end_call:
                tc.end_call = True
            if end.chain:  # the SOP continues in another journey (delivered + item issue → refund query)
                await self._start(tc, end.chain, None)
        if run.status == "handoff":
            reason = (s.transfer or {}).get("reason") or f"{run.journey.title}: transfer"
            s.bus.emit("handover.card", run_id=run.run_id, journey=run.journey.title,
                       **outcomes.handover(s, (s.transfer or {}).get("queue") or "general", reason))

        if run.status == "error" and run.journey.id.startswith("idv."):
            # A system outage during ID&V is not a failed identity check: journeys waiting on it end as system
            # issues too (never "we couldn't verify you"), and the customer can simply try again.
            for r in s.runs:
                if r.status == "blocked" and r.blocked_on == run.run_id:
                    r.status, r.blocked_on = "error", None
                    s.bus.emit("journey.status", run_id=r.run_id, journey=r.journey.id, status="error",
                               outcome="system_unavailable", node=r.node_id)
            return
        if run.done and run.journey.id.startswith("idv."):  # release every journey waiting on this ID&V
            blocked = [r for r in s.runs if r.status == "blocked" and r.blocked_on == run.run_id]
            for r in blocked:
                r.status, r.blocked_on = "running", None
            await asyncio.gather(*[self._advance(tc, r, prefill=True) for r in blocked])

    def _active_idv(self, s: Session, level: str) -> Optional[JourneyRun]:
        for r in s.runs:
            have = IDV_LEVEL_OF_JOURNEY.get(r.journey.id)
            if have and not r.done and idv_covers(have, level):
                return r
        return None

    # ================================================================ answers
    def _validate(self, run: JourneyRun, answers: dict, transcript: str,
                  prefill: bool = False) -> tuple[dict, dict]:
        """Coordinator-proposed answers → validated values.

        For card/order numbers, dates of birth, postcodes and phone numbers the customer's own words win: a
        deterministic finder reads the transcript first and the model's typed value is the fallback. A value
        given in pieces ("1 2 3 4 5 6 7" … "8 9 0 1") is joined across turns. A partial value is INCOMPLETE,
        not wrong: it isn't stored, nothing is verified and no retry is used.
        """
        q = run.question
        if not q:
            return {}, {}
        slots = {sl.name: sl for sl in q.slots}
        values: dict[str, Any] = {}
        invalid: dict[str, dict] = {}

        def problem(name: str, kind: str, reason: str) -> None:
            if not prefill:
                invalid[name] = {"kind": kind, "reason": reason}

        for f in q.spec(run.ctx):
            name, kind = f["name"], f["kind"]
            slot = slots[name]
            if prefill and f.get("secret"):
                continue  # never pre-fill security answers
            raw = answers.get(name)
            found = NOTHING
            finder = FINDERS.get(kind) if slot.required else None   # e.g. never fill a range's end from "in March"
            if finder and transcript:
                found = finder(transcript)
                prior = q.partial.get(name)
                if not found.ok and prior and not prefill:
                    joined = finder(f"{prior} {transcript}")      # the rest of an answer given in pieces
                    if joined.ok or joined.incomplete:
                        found = joined
            if found.ok:
                v = found.value
            else:
                v, why = normalise(kind, raw, [c["value"] for c in f.get("choices", [])]) \
                    if raw not in (None, "") else (None, None)
                if v is None:
                    if found.incomplete:
                        if not prefill:
                            q.partial[name] = found.partial
                        problem(name, "incomplete", found.reason)
                    elif why:
                        problem(name, "unclear" if kind == "yes_no" else "invalid", why)
                    continue
            if kind == "address":
                if q.partial.get(name) and not prefill:
                    v = f"{q.partial[name]}, {v}"
                if not find_postcode(str(v)).ok:                     # verification needs the postcode
                    if not prefill:
                        q.partial[name] = str(v)
                    problem(name, "incomplete", "the postcode wasn't heard yet")
                    continue
            if slot.conflict and transcript and not prefill:
                why = slot.conflict(transcript, v)
                if why:
                    problem(name, "unclear", why)
                    continue
            values[name] = v
            q.partial.pop(name, None)
        return values, invalid

    # ================================================================ results for the coordinator
    def _result(self, s: Session, tc: TurnCtx) -> dict:
        done = [{"journey": r.journey.title, "status": r.status, "outcome": r.outcome}
                for r in tc.touched if r.done]
        transferred = any(r.status == "handoff" for r in tc.touched)
        if transferred:
            # The colleague takes over the whole call (the handover package carries the rest): no further
            # questions, no goodbye and no end_call. call.py closes the demo session after the message plays.
            s.transferred = True
        closing = not transferred and tc.end_call and \
            not any(r.status in ("awaiting_input", "blocked") for r in s.runs)
        ask = None if transferred else self._question(s, prefer=tc.touched)
        result = {"facts": tc.facts, "ask": ask, "outcomes": done, "close_call": closing}
        if transferred:
            result["transferred"] = True
        s.allowed_numbers |= allowed_numbers(result)
        return result

    def _question(self, s: Session, prefer: Optional[list[JourneyRun]] = None) -> Optional[dict]:
        waiting = [r for r in (prefer or []) if r.status == "awaiting_input"] or \
                  [r for r in s.runs if r.status == "awaiting_input"]
        if not waiting:
            return None
        run = waiting[-1]
        q = run.question
        others = [r.journey.title for r in s.runs if r.status in ("awaiting_input", "blocked") and r is not run]
        return {"journey": run.journey.title, "subagent": SUBAGENTS[run.journey.subagent].display, "purpose": q.purpose,
                "fields": q.spec(run.ctx), "invalid": q.invalid or None, "context": q.context or None,
                "internal_notes": subagent_notes(run.journey.subagent) or None, "also_in_progress": others or None}

    def progress(self, s: Session) -> Optional[str]:
        """What the subagents are doing right now, for a spoken progress update."""
        tc = self.current.get(s.id)
        return tc.activity[-1] if tc and tc.activity else None

    # ================================================================ helpers
    def _mood(self, s: Session, mood: Optional[str]) -> None:
        if mood and mood != s.sentiment:
            s.sentiment = mood
            s.bus.emit("sentiment", value=mood)

    @staticmethod
    def _secret(run: JourneyRun, field: str) -> bool:
        return any(sl.name == field and sl.secret for sl in (run.question.slots if run.question else []))

    def _summary(self, s: Session) -> dict:
        """Deterministic call summary from journey outcomes (no LLM)."""
        done = [f"{r.journey.title}: {(r.outcome or r.status).replace('_', ' ')}" for r in s.runs if r.done]
        disposition = outcomes.disposition(s)
        headline = (f"Transferred: {s.transfer['reason']}" if s.transfer else
                    (done[-1] if done else "Call ended before any journey completed"))
        return {"headline": headline, "disposition": disposition, "verified": outcomes.verified(s),
                "transfer_reason": (s.transfer or {}).get("reason"), "journeys": outcomes.journey_outcomes(s),
                "summary": outcomes.summary_text(s, (s.transfer or {}).get("reason")),
                "done_so_far": done[-4:], "customer_mood": s.sentiment}
