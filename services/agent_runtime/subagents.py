"""
Subagents: the specialist subagents the Orchestrator delegates to (ID&V, FreshCart Rewards, Online, Sign-up,
Knowledge, Escalation).

A subagent is a WORKFLOW agent, not an LLM agent. It executes its SOP journeys step by step with the
deterministic engine and reaches backend systems only through its own least-privilege MCP toolbox.
Every step of the current SOPs is either a tool call, a rule, a fact to tell or a question to ask;
the language understanding for the questions is done once, by the Voice Live model (the coordinator),
and validated here by code. So no subagent calls an LLM.

Isolation, per call:
  * state   – a subagent only sees the journey runs it owns (plus the shared verification result)
  * tools   – ScopedToolbox: the Online subagent physically cannot call FreshCart Rewards tools, and so on
  * notes   – its domain notes (prompts/subagent_<key>.md) reach the coordinator only inside that
              subagent's own question, never in the global instructions

If a future SOP step needs open-ended understanding the coordinator can't do in one hop (for example
reasoning over hundreds of transactions), that one subagent gets a small model here; nothing else changes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .config import PROMPTS_DIR
from .events import now_ms
from .journeys.dsl import Node
from .journeys.engine import Engine, JourneyRun
from .journeys.registry import SUBAGENTS, SubagentSpec
from .mcp_hub import McpHub, ScopedToolbox

if TYPE_CHECKING:
    from .session import Session

SENSITIVE_KEYS = {"card_number", "full_name", "dob", "address", "email", "mobile", "security_answer", "memorable_word",
                  "destination", "phone", "postcode", "caller_number"}

_NOTES: dict[str, str] = {}


class SystemUnavailable(Exception):
    """A backend system failed or timed out (5xx / timeout), as opposed to a business answer like 'not found'."""


def subagent_notes(key: str) -> str:
    """The subagent's domain notes (markdown in /prompts), without the heading line."""
    if key not in _NOTES:
        text = (PROMPTS_DIR / SUBAGENTS[key].prompt_file).read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        _NOTES[key] = "\n".join(lines)
    return _NOTES[key]


class Subagent:
    """One specialist subagent for one call."""

    def __init__(self, spec: SubagentSpec, hub: McpHub, s: "Session",
                 on_transfer: Optional[Callable[[dict], dict]] = None) -> None:
        self.spec, self.s = spec, s
        self.toolbox = ScopedToolbox(hub, spec.scopes, spec.display)
        self.runs: list[JourneyRun] = []          # the only journey state this subagent can see
        # Supplied by the Orchestrator: builds the colleague's handover package (reason, summary, transcript).
        self.on_transfer = on_transfer

    @property
    def key(self) -> str:
        return self.spec.key

    def adopt(self, run: JourneyRun) -> None:
        self.runs.append(run)

    def supply(self, run: JourneyRun, values: dict, invalid: dict) -> None:
        """Validated answers to this subagent's pending question."""
        self._engine(lambda _: None).supply(run, values, invalid)

    async def execute(self, run: JourneyRun, on_step: Callable[[str, int], None]) -> list[dict]:
        """Walk the journey until it needs the customer or ends. Returns the facts to tell.
        on_step(label, ms) is called for every system step (progress updates, latency)."""
        assert run in self.runs, f"{self.key} subagent does not own run {run.run_id}"
        await self._engine(on_step).advance(run)
        return list(run.tells)

    def _engine(self, on_step: Callable[[str, int], None]) -> Engine:
        s, bus = self.s, self.s.bus

        async def call_tool(scope: str, tool: str, args: dict, run: JourneyRun, node: Node) -> dict:
            call_id = f"{run.run_id}:{node.id}:{tool}"
            if tool == "warm_transfer" and self.on_transfer:
                args = {**args, "handover": self.on_transfer(args)}
            bus.emit("tool.call", id=call_id, agent=self.key, scope=scope, tool=tool, node=node.id, label=node.label,
                     args={k: ("⟨redacted⟩" if k in SENSITIVE_KEYS else "⟨handover package⟩" if k == "handover"
                               else s.redactor.deep(v)) for k, v in args.items()})
            t = now_ms()
            try:
                result = await self.toolbox.call(scope, tool, args)
            except PermissionError as e:
                bus.emit("tool.result", id=call_id, agent=self.key, tool=tool, ok=False, ms=0, summary=str(e))
                raise
            ms = round(now_ms() - t)
            on_step(node.label, ms)
            bus.emit("tool.result", id=call_id, agent=self.key, tool=tool, ok=not result.get("error"), ms=ms,
                     summary=summarise(tool, result))
            if result.get("error") and (result.get("status_code") or 500) >= 500 and tool != "log_contact_outcome":
                # An outage is not an answer: never let "CRM is down" read as "no account found" and use up
                # the customer's retry. The journey stops with a system issue the coordinator apologises for.
                raise SystemUnavailable(f"{tool}: {result.get('detail') or 'unavailable'}")
            return result

        def on_node(run: JourneyRun, node: Node, phase: str) -> None:
            bus.emit("journey.node", run_id=run.run_id, journey=run.journey.id, node=node.id, label=node.label,
                     kind=type(node).__name__.lower(), phase=phase)

        return Engine(call_tool, on_node)


def summarise(tool: str, r: dict) -> str:
    """One-line, redacted summary of a tool result for the live trace."""
    if r.get("error"):
        return f"✕ {r.get('detail') or 'error'}"
    match tool:
        case "locate_account":
            return "Account found" if r.get("found") else "No account for that number"
        case "verify_identity":
            ok = sum(1 for v in r.get("matched", {}).values() if v)
            return f"{'Verified' if r.get('passed') else 'Not verified'} ({ok}/{len(r.get('matched', {}))} matched)"
        case "get_balance":
            return f"{r.get('points_balance'):,} pts · {r.get('status')}"
        case "get_transactions" | "get_archive_transactions":
            return f"{len(r.get('transactions', []))} transactions"
        case "validate_order":
            return "Order found" if r.get("valid") else "Order number not recognised"
        case "check_caller_number":
            return "Calling number matches order" if r.get("matches") else "Different calling number"
        case "verify_phone" | "verify_postcode":
            return "Match" if r.get("match") else "No match"
        case "get_order_status":
            return f"Status: {str(r.get('status', '')).replace('_', ' ')}"
        case "send_refund_link":
            return f"SMS queued to ••{r.get('sent_to_last4')}"
        case "warm_transfer":
            return "Connected" if r.get("connected") else "Line unavailable"
        case "create_task" | "create_fraud_case":
            return f"{r.get('task_id') or r.get('case_id')} raised"
        case "create_member":
            return f"Member created · card ending {r.get('card_last4')}"
        case "log_contact_outcome":
            return f"Logged {r.get('contact_id')}"
        case "search_knowledge":
            return f"{len(r.get('results', []))} article(s)"
        case "get_document":
            return r.get("title", "document")
        case _:
            return "OK"
