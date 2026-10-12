"""
Journey DSL. A journey is an SOP flowchart, encoded as data.

Node ids and labels mirror the boxes on the SOP flowcharts so the UI can
render the live SOP and light up exactly where the call is.

Node kinds
    Ask     – needs information from the customer (the only place an LLM is involved:
              the Voice Live model proposes typed answers; code validates them)
    Call    – deterministic MCP tool call (single)
    CallEach– deterministic MCP tool calls fanned out in parallel
    Decide  – deterministic branch on data (the "diamonds")
    Effect  – deterministic state change (e.g. record verification result)
    Tell    – structured facts the voice model must convey to the customer
    Reset   – clear slots and count a retry
    End     – terminal state; logs the contact outcome to CRM
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

SlotKind = Literal["card_number", "order_number", "name", "dob", "date", "address", "postcode", "email", "mobile",
                   "phone", "yes_no", "integer", "choice", "text", "secret"]
Lane = Literal["customer", "agent", "crm", "taskdesk", "specialist", "system"]


@dataclass
class Choice:
    value: str
    description: str


@dataclass
class Slot:
    name: str
    kind: SlotKind
    description: str              # what the value is, in plain words (never a format instruction)
    shared: bool = False          # lives in the session vault; reused across journeys (e.g. card, name, dob)
    sensitive: bool = False       # PII: stored in vault, redacted everywhere else
    secret: bool = False          # security answers: never echoed, never logged
    choices: Optional[Callable[["Ctx"], list[Choice]]] = None
    required: bool = True
    parse_hint: str = ""          # INTERNAL: how to pass the value in submit_answer; never said to the customer
    long_input: bool = False      # read out slowly (digits, addresses, secrets): wait longer before ending the turn
    volunteered_only: bool = False  # never asked about: filled only if the customer brings it up (e.g. lost card)
    # Returns a reason when the customer's words contradict the proposed value (ask one clarifying question).
    conflict: Optional[Callable[[str, Any], Optional[str]]] = None


@dataclass
class Node:
    id: str
    label: str
    lane: Lane


@dataclass
class Ask(Node):
    slots: list[Slot] = field(default_factory=list)
    purpose: str = ""
    next: str = ""
    escapes: dict[str, str] = field(default_factory=dict)  # yes_no slot == True → jump to node
    context: Optional[Callable[["Ctx"], dict]] = None       # facts that help the customer answer
    satisfied: Optional[Callable[["Ctx"], bool]] = None     # custom completeness rule


@dataclass
class Call(Node):
    scope: str = ""
    tool: str = ""
    args: Callable[["Ctx"], dict] = lambda c: {}
    save_as: str = ""
    next: str = ""


@dataclass
class CallEach(Node):
    scope: str = ""
    tool: str = ""
    items: Callable[["Ctx"], list[dict]] = lambda c: []   # list of args dicts
    save_as: str = ""
    next: str = ""


@dataclass
class Decide(Node):
    rule: Callable[["Ctx"], str] = lambda c: ""
    branches: dict[str, str] = field(default_factory=dict)  # branch label → node id


@dataclass
class Effect(Node):
    apply: Callable[["Ctx"], None] = lambda c: None
    next: str = ""


@dataclass
class Tell(Node):
    kind: str = ""
    facts: Callable[["Ctx"], dict] = lambda c: {}
    next: str = ""


@dataclass
class Reset(Node):
    slots: list[str] = field(default_factory=list)
    counter: str = ""
    next: str = ""


@dataclass
class End(Node):
    outcome: str = ""
    status: Literal["completed", "failed", "handoff", "redirected"] = "completed"
    log: bool = True
    note: Optional[Callable[["Ctx"], str]] = None
    chain: Optional[str] = None       # journey id the orchestrator starts next (e.g. delivered → refund)
    end_call: bool = False            # the SOP ends the call here ("End Call") once nothing else is open


@dataclass
class Journey:
    id: str
    title: str
    section: str
    subagent: str
    description: str                      # what the coordinator reads to route
    start: str
    nodes: list[Node]
    requires_idv: Optional[Literal["standard", "enhanced", "online_order"]] = None
    source: str = ""                      # which SOP flowchart this encodes

    def __post_init__(self) -> None:
        self.by_id = {n.id: n for n in self.nodes}
        assert self.start in self.by_id, f"{self.id}: start node missing"
        for n in self.nodes:
            for target in _targets(n):
                assert target in self.by_id, f"{self.id}: {n.id} → unknown node {target}"

    def graph(self) -> dict:
        """Serialisable graph for the UI's live SOP view."""
        nodes, edges = [], []
        for n in self.nodes:
            nodes.append({"id": n.id, "label": n.label, "lane": n.lane, "kind": type(n).__name__.lower(),
                          "terminal": isinstance(n, End),
                          "status": getattr(n, "status", None), "chain": getattr(n, "chain", None)})
            if isinstance(n, Decide):
                edges += [{"from": n.id, "to": t, "label": lbl} for lbl, t in n.branches.items()]
            elif isinstance(n, Ask):
                edges.append({"from": n.id, "to": n.next, "label": None})
                edges += [{"from": n.id, "to": t, "label": s} for s, t in n.escapes.items()]
            elif not isinstance(n, End):
                edges.append({"from": n.id, "to": n.next, "label": None})
        return {"id": self.id, "title": self.title, "section": self.section, "subagent": self.subagent,
                "requires_idv": self.requires_idv, "start": self.start, "nodes": nodes, "edges": edges}


def _targets(n: Node) -> list[str]:
    if isinstance(n, Decide):
        return list(n.branches.values())
    if isinstance(n, Ask):
        return [n.next, *n.escapes.values()]
    if isinstance(n, End):
        return []
    return [getattr(n, "next")]


class Ctx:
    """Everything a journey can read. Slots are typed, validated values only."""

    def __init__(self, *, session_id: str, channel: str, vault, idv: dict, params: dict,
                 caller_number: Optional[str] = None) -> None:
        self.session_id = session_id
        self.channel = channel
        self.caller_number = caller_number  # calling line identity (ANI) from telephony
        self.vault = vault            # shared slots (PII) live here
        self.local: dict[str, Any] = {}  # journey-local slots
        self.data: dict[str, Any] = {}   # tool results
        self.counters: dict[str, int] = {}
        self.idv = idv                # session-level verification state (mutable dict)
        self.params = params          # from coordinator (e.g. knowledge query)

    def slot(self, name: str) -> Any:
        if name in self.local:
            return self.local[name]
        return self.vault.get(name)

    def set_slot(self, slot: Slot, value: Any) -> None:
        if slot.shared or slot.sensitive:
            self.vault.put(slot.name, value)
        else:
            self.local[slot.name] = value

    def clear_slot(self, name: str) -> None:
        self.local.pop(name, None)
        self.vault.drop(name)

    def count(self, name: str) -> int:
        return self.counters.get(name, 0)
