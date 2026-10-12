"""
Call outcomes, deterministically: the disposition logged for every call, and the handover package a colleague
receives on a warm transfer (reason, summary, redacted transcript). No LLM: both come from journey outcomes.

Dispositions
    automated               every journey completed by the assistant, no transfer
    deflected               ended by sending the customer to self-service (online refund link, knowledge answer,
                            app/website registration)
    transferred_to_advisor  handed to a colleague before anything was completed
    partial_automation      something was completed, then a transfer was needed
    partial_deflection      something was deflected to self-service, then a transfer was needed
    failed_idv              identity could not be verified (and no transfer)
    abandoned               the caller left before any outcome
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session

DEFLECTED = {"answered_from_knowledge_base", "redirected_to_ask_kb", "refund_query_resolved",
             "refund_query_more_help", "digital_registration_guided"}
IDV_FAILED = {"idv_failed", "idv_enhanced_failed", "account_not_found", "refused_disclosure", "online_refused_disclosure"}
# Steps on the way to an outcome, not outcomes themselves.
NEUTRAL = {"idv_passed", "idv_enhanced_passed", "online_idv_passed", "items_issue_to_refund_query",
           "negative_balance_to_dispute"}
DISPOSITIONS = ("automated", "deflected", "transferred_to_advisor", "partial_automation", "partial_deflection",
                "abandoned", "failed_idv")


def journey_outcomes(s: "Session") -> list[dict]:
    return [{"journey": r.journey.id, "title": r.journey.title, "subagent": r.journey.subagent, "status": r.status,
             "outcome": r.outcome} for r in s.runs]


def disposition(s: "Session") -> str:
    done = [r for r in s.runs if r.done]
    handed_off = any(r.status == "handoff" for r in done) or s.transferred
    business = [r for r in done if r.status != "handoff" and r.outcome not in NEUTRAL and r.status != "error"]
    # A journey that already gave the customer what they called for counts, even if they hung up or said
    # "that's all" before the SOP's closing question (e.g. delivery status told, "anything else?" unanswered).
    business += [r for r in s.runs if not r.done and r.served]
    deflected = [r for r in business if r.outcome in DEFLECTED]
    failed = [r for r in business if r.outcome in IDV_FAILED]
    automated = [r for r in business if (r.outcome not in DEFLECTED and r.outcome not in IDV_FAILED)
                 and not (not r.done and r.journey.id in ("knowledge.ask", "online.refund_query"))]
    deflected += [r for r in business if not r.done and r.journey.id in ("knowledge.ask", "online.refund_query")]
    if handed_off:
        if deflected:
            return "partial_deflection"
        if automated:
            return "partial_automation"
        return "transferred_to_advisor"
    if automated and deflected:   # both happened: the last finished journey says how the call ended
        last = business[-1]
        return "deflected" if last.outcome in DEFLECTED else "automated"
    if automated:
        return "automated"
    if deflected:
        return "deflected"
    if failed:
        return "failed_idv"
    return "abandoned"


def verified(s: "Session") -> str:
    return s.idv.get("level") or ("online order" if s.idv.get("online_verified") else "not verified")


def summary_text(s: "Session", reason: str | None = None) -> str:
    """One short paragraph a colleague can read in five seconds."""
    asked = ", ".join(dict.fromkeys(s.requests)) or "nothing specific yet"
    done = [f"{r.journey.title} ({(r.outcome or r.status).replace('_', ' ')})" for r in s.runs
            if r.done and r.outcome not in NEUTRAL and r.status != "handoff"]
    open_ = [r.journey.title for r in s.runs if not r.done]
    parts = [f"Customer called about: {asked}.", f"Identity: {verified(s)}."]
    if done:
        parts.append("Done: " + "; ".join(done) + ".")
    if open_:
        parts.append("Still open: " + "; ".join(open_) + ".")
    parts.append(f"Customer mood: {s.sentiment}.")
    if reason:
        parts.append(f"Why transferred: {reason}")
    return " ".join(parts)


def handover(s: "Session", queue: str, reason: str) -> dict:
    """What the colleague receives with a warm transfer: why, a summary, and the (redacted) conversation."""
    return {"queue": queue, "reason": reason, "summary": summary_text(s, reason), "verified": verified(s),
            "customer_mood": s.sentiment, "journeys": journey_outcomes(s),
            "transcript": [f"{h['role']}: {h['text']}" for h in s.history[-40:]]}
