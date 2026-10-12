"""Cross-cutting journeys: Knowledge Base knowledge answers and human escalation."""
from __future__ import annotations

from .dsl import Call, End, Journey, Tell

KNOWLEDGE_ASK = Journey(
    id="knowledge.ask", title="Knowledge Base", section="General", subagent="knowledge", start="search",
    source="FreshCart Rewards › Points Balance › Journey 2 (Redirect to Knowledge Base)",
    description="General FreshCart Rewards or FreshCart policy questions that don't need account access "
                "(how points work, posting times, activation, T&Cs, lost cards in general).",
    nodes=[
        Call("search", "Search Knowledge Base", "system", scope="knowledge_base", tool="search_knowledge",
             args=lambda c: {"query": c.params.get("query") or ""}, save_as="kb", next="answer"),
        Tell("answer", "Answer from knowledge base", "agent", kind="knowledge_answer",
             facts=lambda c: {"question": c.params.get("query"), "documents": c.data["kb"].get("results", [])},
             next="end"),
        End("end", "End", "system", outcome="answered_from_knowledge_base"),
    ],
)

HUMAN = Journey(
    id="escalation.human", title="Transfer to a colleague", section="General", subagent="escalation", start="transfer",
    source="Warm transfer pattern (Journey 3)",
    description="Caller explicitly asks for a human, or the request is out of scope for the assistant.",
    nodes=[
        Call("transfer", "Warm transfer to colleague", "system", scope="case_management", tool="warm_transfer",
             args=lambda c: {"session_id": c.session_id, "queue": "general",
                             "summary": c.params.get("query") or "Caller requested a colleague."},
             save_as="transfer", next="tell"),
        Tell("tell", "Transferring", "agent", kind="warm_transfer_general",
             facts=lambda c: {"connected": c.data["transfer"].get("connected"),
                              "estimated_wait_seconds": c.data["transfer"].get("estimated_wait_seconds")},
             next="end"),
        End("end", "End (Transferred)", "system", outcome="warm_transferred_general", status="handoff"),
    ],
)
