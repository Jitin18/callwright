"""Journey + subagent registry. Adding Online refunds = new journey file + one SubagentSpec here."""
from __future__ import annotations

from dataclasses import dataclass

from .dsl import Journey
from .enrolment import SIGNUP
from .general import KNOWLEDGE_ASK, HUMAN
from .online import DELIVERY_STATUS, ONLINE_IDV, REFUND_QUERY
from .idv import ENHANCED, STANDARD
from .rewards import BALANCE_DISPUTE, HISTORY, NEGATIVE_BALANCE, VIEW_BALANCE


@dataclass(frozen=True)
class SubagentSpec:
    key: str
    display: str
    scopes: tuple[str, ...]         # MCP scopes this subagent may reach (least privilege)
    prompt_file: str                # extractor addendum prompt


SUBAGENTS: dict[str, SubagentSpec] = {
    "idv": SubagentSpec("idv", "ID&V Agent", ("member_identity", "order_verification", "case_management"), "subagent_idv.md"),
    "rewards": SubagentSpec("rewards", "FreshCart Rewards Agent", ("loyalty_accounts", "case_management", "knowledge_base"), "subagent_rewards.md"),
    "enrolment": SubagentSpec("enrolment", "Enrolment Agent", ("member_enrolment", "knowledge_base"), "subagent_enrolment.md"),
    "knowledge": SubagentSpec("knowledge", "Knowledge Agent", ("knowledge_base",), "subagent_knowledge.md"),
    "escalation": SubagentSpec("escalation", "Escalation Agent", ("case_management",), "subagent_escalation.md"),
    "online": SubagentSpec("online", "FreshCart Online Agent", ("order_service", "case_management", "knowledge_base"), "subagent_online.md"),
}

JOURNEYS: dict[str, Journey] = {j.id: j for j in (
    STANDARD, ENHANCED, ONLINE_IDV, VIEW_BALANCE, BALANCE_DISPUTE, NEGATIVE_BALANCE, HISTORY, SIGNUP,
    DELIVERY_STATUS, REFUND_QUERY, KNOWLEDGE_ASK, HUMAN)}

ROUTABLE = [j for j in JOURNEYS.values() if not j.id.startswith("idv.")]
IDV_JOURNEY = {"standard": "idv.standard", "enhanced": "idv.enhanced", "online_order": "idv.online_order"}
