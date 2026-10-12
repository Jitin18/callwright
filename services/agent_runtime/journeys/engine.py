"""
Deterministic journey engine. Given a journey run and any newly supplied slot
values, walk the graph until the journey needs the customer again or ends.

No LLM is called from here. Tool calls go through the subagent's scoped Toolbox.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .dsl import Ask, Call, CallEach, Ctx, Decide, Effect, End, Journey, Node, Reset, Slot, Tell

LEVEL_RANK = {None: 0, "standard": 1, "enhanced": 2}


@dataclass
class Question:
    node_id: str
    purpose: str
    slots: list[Slot]
    missing: list[str]
    invalid: dict[str, dict]           # field → {"kind": "incomplete" | "invalid" | "unclear", "reason": str}
    context: dict
    partial: dict[str, str] = field(default_factory=dict)   # field → what was heard so far (incomplete answers)

    def spec(self, ctx: Ctx) -> list[dict]:
        out = []
        for s in self.slots:
            if s.name not in self.missing:
                continue
            item = {"name": s.name, "kind": s.kind, "description": s.description, "secret": s.secret}
            if s.parse_hint:
                item["parse_hint"] = s.parse_hint
            if s.volunteered_only:
                item["only_if_volunteered"] = True
            if s.choices:
                item["choices"] = [{"value": c.value, "description": c.description} for c in s.choices(ctx)]
            out.append(item)
        return out

    @property
    def long_input(self) -> bool:
        """The customer will read something out slowly (digits, an address, a security answer)."""
        return any(s.long_input for s in self.slots if s.name in self.missing)


@dataclass
class JourneyRun:
    journey: Journey
    ctx: Ctx
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    node_id: str = ""
    status: str = "running"          # running | awaiting_input | blocked | completed | failed | handoff | redirected | error
    question: Optional[Question] = None
    tells: list[dict] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    outcome: Optional[str] = None
    blocked_on: Optional[str] = None  # run_id of the ID&V run this journey waits for
    served: bool = False              # has told the customer its business facts (for the call disposition)

    def __post_init__(self) -> None:
        self.node_id = self.node_id or self.journey.start

    @property
    def done(self) -> bool:
        return self.status in ("completed", "failed", "handoff", "redirected", "error")


ToolCaller = Callable[[str, str, dict, "JourneyRun", Node], Awaitable[dict]]
NodeHook = Callable[["JourneyRun", Node, str], None]


class Engine:
    MAX_STEPS = 80

    def __init__(self, call_tool: ToolCaller, on_node: NodeHook) -> None:
        self.call_tool = call_tool
        self.on_node = on_node

    def supply(self, run: JourneyRun, values: dict[str, Any], invalid: dict[str, str] | None = None) -> None:
        """Apply validated slot values for the question currently being asked."""
        if not run.question:
            return
        by_name = {s.name: s for s in run.question.slots}
        for name, value in values.items():
            if name in by_name and value is not None:
                run.ctx.set_slot(by_name[name], value)
        run.question.invalid = dict(invalid or {})

    async def advance(self, run: JourneyRun) -> JourneyRun:
        """Run until the journey needs input or ends. Returns the same run, mutated."""
        run.tells = []
        run.status = "running"
        ctx = run.ctx
        for _ in range(self.MAX_STEPS):
            node = run.journey.by_id[run.node_id]
            self.on_node(run, node, "enter")
            run.visited.append(node.id)

            if isinstance(node, Ask):
                if any(ctx.slot(esc) is True for esc in node.escapes):
                    esc = next(e for e in node.escapes if ctx.slot(e) is True)
                    run.node_id = node.escapes[esc]
                    continue
                missing = [s.name for s in node.slots if s.required and ctx.slot(s.name) is None]
                done = node.satisfied(ctx) if node.satisfied else not missing
                if not done:
                    same = run.question is not None and run.question.node_id == node.id
                    # Every unfilled slot is offered, including optional escapes ("I've lost my card",
                    # "I'd rather speak to someone"); completeness above still counts required ones only.
                    run.question = Question(node.id, node.purpose, node.slots,
                                            [s.name for s in node.slots if ctx.slot(s.name) is None],
                                            run.question.invalid if same else {},
                                            node.context(ctx) if node.context else {},
                                            run.question.partial if same else {})
                    run.status = "awaiting_input"
                    self.on_node(run, node, "waiting")
                    return run
                run.question = None
                run.node_id = node.next

            elif isinstance(node, Call):
                result = await self.call_tool(node.scope, node.tool, node.args(ctx), run, node)
                ctx.data[node.save_as] = result
                run.node_id = node.next

            elif isinstance(node, CallEach):
                items = node.items(ctx)
                results = await asyncio.gather(*[self.call_tool(node.scope, node.tool, a, run, node) for a in items])
                ctx.data[node.save_as] = list(results)
                run.node_id = node.next

            elif isinstance(node, Decide):
                branch = node.rule(ctx)
                if branch not in node.branches:
                    raise RuntimeError(f"{run.journey.id}:{node.id} rule returned unknown branch {branch!r}")
                self.on_node(run, node, f"branch:{branch}")
                run.node_id = node.branches[branch]

            elif isinstance(node, Effect):
                node.apply(ctx)
                run.node_id = node.next

            elif isinstance(node, Tell):
                run.tells.append({"journey": run.journey.id, "kind": node.kind, "facts": node.facts(ctx)})
                if not run.journey.id.startswith("idv.") and node.kind not in ("refuse_disclosure", "system_issue"):
                    run.served = True
                run.node_id = node.next

            elif isinstance(node, Reset):
                for s in node.slots:
                    ctx.clear_slot(s)
                ctx.counters[node.counter] = ctx.count(node.counter) + 1
                run.node_id = node.next

            elif isinstance(node, End):
                run.outcome = node.outcome
                run.status = node.status
                run.question = None
                if node.log:
                    await self.call_tool(_log_scope(run), "log_contact_outcome", {
                        "session_id": ctx.session_id, "journey": run.journey.id, "outcome": node.outcome,
                        "account_id": ctx.idv.get("account_id"),
                        "note": node.note(ctx) if node.note else None}, run, node)
                self.on_node(run, node, "end")
                return run
            else:  # pragma: no cover
                raise RuntimeError(f"Unknown node type {type(node)}")
        raise RuntimeError(f"{run.journey.id}: exceeded {self.MAX_STEPS} steps (cycle?)")


def _log_scope(run: JourneyRun) -> str:
    if run.journey.id == "idv.online_order":
        return "order_verification"
    return {"idv": "member_identity", "rewards": "loyalty_accounts", "enrolment": "member_enrolment",
            "knowledge": "knowledge_base", "escalation": "case_management",
            "online": "order_service"}.get(run.journey.subagent, "case_management")


IDV_LEVEL_OF_JOURNEY = {"idv.standard": "standard", "idv.enhanced": "enhanced", "idv.online_order": "online_order"}


def idv_satisfied(ctx: Ctx, level: str) -> bool:
    """FreshCart Rewards levels are ranked (enhanced covers standard). Online order verification is its own track."""
    if level == "online_order":
        return bool(ctx.idv.get("online_verified"))
    return LEVEL_RANK.get(ctx.idv.get("level")) >= LEVEL_RANK[level]


def idv_covers(have: str, need: str) -> bool:
    """Does an ID&V journey of level `have` satisfy a requirement of level `need`?"""
    if "online_order" in (have, need):
        return have == need
    return LEVEL_RANK[have] >= LEVEL_RANK[need]
