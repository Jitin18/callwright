"""
Standard and Enhanced ID&V — the verification gate used by every member journey.

Standard ID&V (all member interactions): FreshCart Rewards card number, full name, full DOB.
Enhanced ID&V (negative/minus balance): full name, DOB, address, security answer, memorable word.
Source: SOP flowcharts "Journey 1 – Viewing points balance" note and "Journey 3 – Negative/Minus Balance" note.
"""
from __future__ import annotations

from ..config import settings
from .dsl import Ask, Call, Decide, Effect, End, Journey, Reset, Tell
from .slots import ADDRESS, CARD, CARD_LOST, DOB, FULL_NAME, MEMORABLE_WORD, SECURITY_ANSWER


def _locate_nodes(p: str, after: str) -> list:
    return [
        Ask(f"{p}ask_card", "Member: Provide FreshCart Rewards card number", "customer", slots=[CARD, CARD_LOST],
            purpose="Ask for their FreshCart Rewards card number so you can find their account.",
            escapes={"card_lost": f"{p}lost_card"}, next=f"{p}locate"),
        Call(f"{p}locate", "Colleague: Input card number into CRM", "crm", scope="member_identity", tool="locate_account",
             args=lambda c: {"card_number": c.slot("card_number")}, save_as="located", next=f"{p}found"),
        Decide(f"{p}found", "Account located on CRM?", "agent",
               rule=lambda c: "yes" if c.data["located"].get("found") else ("retry" if c.count("locate") < 1 else "no"),
               branches={"yes": f"{p}record_account", "retry": f"{p}retry_card", "no": f"{p}not_found"}),
        Reset(f"{p}retry_card", "Re-check card number", "agent", slots=["card_number"], counter="locate",
              next=f"{p}tell_not_found_retry"),
        Tell(f"{p}tell_not_found_retry", "No account for that number", "agent", kind="card_not_found_retry",
             facts=lambda c: {"attempts_remaining": 1}, next=f"{p}ask_card"),
        Tell(f"{p}not_found", "Account not found", "agent", kind="account_not_found", facts=lambda c: {},
             next=f"{p}end_not_found"),
        End(f"{p}end_not_found", "End State (Failed)", "system", outcome="account_not_found", status="failed"),
        Effect(f"{p}record_account", "Locate account on CRM", "crm",
               apply=lambda c: c.idv.update(account_id=c.data["located"]["account_id"]), next=after),
        Call(f"{p}lost_card", "Lost / stolen card: see process", "taskdesk", scope="case_management", tool="warm_transfer",
             args=lambda c: {"session_id": c.session_id, "queue": "lost_stolen",
                             "summary": "Caller cannot provide FreshCart Rewards card number (lost or stolen)."},
             save_as="transfer", next=f"{p}tell_lost"),
        Tell(f"{p}tell_lost", "Transfer to lost / stolen card team", "agent", kind="lost_card_transfer",
             facts=lambda c: {"connected": c.data["transfer"].get("connected"),
                              "estimated_wait_seconds": c.data["transfer"].get("estimated_wait_seconds")},
             next=f"{p}end_lost"),
        End(f"{p}end_lost", "End (Transferred)", "system", outcome="lost_stolen_transfer", status="handoff"),
    ]


def _verdict(level: str):
    def rule(c):
        v = c.data.get("verification", {})
        if v.get("passed"):
            return "yes"
        return "retry" if c.count("verify") < settings.idv_max_retries else "no"
    return rule


def _record(level: str):
    def apply(c):
        v = c.data["verification"]
        c.idv.update(level=level, holder_id=v["holder_id"], holder_role=v["holder_role"],
                     first_name=v["holder_first_name"])
    return apply


STANDARD = Journey(
    id="idv.standard", title="Standard ID&V", section="Verification", subagent="idv", start="ask_card",
    source="Journey 1 note: Standard ID&V must be asked and passed on all member interactions",
    description="Standard identity verification (card number, full name, date of birth). Started automatically "
                "by the system when a member journey needs it — never route to this directly.",
    nodes=[
        *_locate_nodes("", "ask_identity"),
        Ask("ask_identity", "Initiate Standard ID&V", "agent", slots=[FULL_NAME, DOB],
            purpose="Security check: ask for their full name and date of birth.", next="verify"),
        Call("verify", "Verify answers against CRM", "crm", scope="member_identity", tool="verify_identity",
             args=lambda c: {"account_id": c.idv["account_id"], "level": "standard",
                             "full_name": c.slot("full_name"), "dob": c.slot("dob")},
             save_as="verification", next="passed"),
        Decide("passed", "Is Standard ID&V successful?", "agent", rule=_verdict("standard"),
               branches={"yes": "record_pass", "retry": "retry_identity", "no": "refuse"}),
        Reset("retry_identity", "Details did not match: ask again", "agent", slots=["full_name", "dob"],
              counter="verify", next="tell_retry"),
        Tell("tell_retry", "Explain details didn't match", "agent", kind="idv_mismatch_retry",
             facts=lambda c: {"attempts_remaining": settings.idv_max_retries - c.count("verify") + 1},
             next="ask_identity"),
        Effect("record_pass", "Record verified caller", "system", apply=_record("standard"), next="end_pass"),
        End("end_pass", "ID&V passed", "system", outcome="idv_passed", log=False),
        Tell("refuse", "Refuse disclosure / Follow failure proc", "agent", kind="refuse_disclosure",
             facts=lambda c: {"reason": "identity could not be verified", "next": "offer_colleague_only_if_they_want"}, next="end_failed"),
        End("end_failed", "End State (Failed)", "system", outcome="idv_failed", status="failed"),
    ],
)


ENHANCED = Journey(
    id="idv.enhanced", title="Enhanced ID&V", section="Verification", subagent="idv", start="already_located",
    source="Journey 3 note: Enhanced ID&V – full name, DOB, address, security answer, memorable word",
    description="Enhanced identity verification. Started automatically by the system for high-risk journeys — "
                "never route to this directly.",
    nodes=[
        Decide("already_located", "Account already located?", "agent",
               rule=lambda c: "yes" if c.idv.get("account_id") else "no",
               branches={"yes": "ask_identity", "no": "ask_card"}),
        *_locate_nodes("", "ask_identity"),
        Ask("ask_identity", "Confirm name and date of birth", "agent", slots=[FULL_NAME, DOB],
            purpose="Security check: ask for their full name and date of birth.", next="ask_address"),
        Ask("ask_address", "Confirm address", "agent", slots=[ADDRESS],
            purpose="A few more security questions: ask for their home address.",
            next="get_question"),
        Call("get_question", "Retrieve security question", "crm", scope="member_identity", tool="get_security_question",
             args=lambda c: {"account_id": c.idv["account_id"], "full_name": c.slot("full_name")},
             save_as="security_question", next="ask_security"),
        Ask("ask_security", "Security answer", "agent", slots=[SECURITY_ANSWER],
            purpose="Ask their security question (it is in context, read it as written) and take their answer.",
            context=lambda c: {"security_question": c.data["security_question"].get("question")},
            next="ask_word"),
        Ask("ask_word", "Memorable word", "agent", slots=[MEMORABLE_WORD],
            purpose="Ask for their memorable word.", next="verify"),
        Call("verify", "Confirm enhanced ID&V", "crm", scope="member_identity", tool="verify_identity",
             args=lambda c: {"account_id": c.idv["account_id"], "level": "enhanced", "full_name": c.slot("full_name"),
                             "dob": c.slot("dob"), "address": c.slot("address"),
                             "security_answer": c.slot("security_answer"), "memorable_word": c.slot("memorable_word")},
             save_as="verification", next="passed"),
        Decide("passed", "Is Enhanced ID&V successful?", "agent", rule=_verdict("enhanced"),
               branches={"yes": "record_pass", "retry": "retry_identity", "no": "refuse"}),
        Effect("retry_identity", "Details did not match: ask again", "agent",
               apply=lambda c: _reset_enhanced(c), next="tell_retry"),
        Tell("tell_retry", "Explain details didn't match", "agent", kind="idv_mismatch_retry",
             facts=lambda c: {"attempts_remaining": settings.idv_max_retries - c.count("verify") + 1},
             next="ask_identity"),
        Effect("record_pass", "Record verified caller", "system", apply=_record("enhanced"), next="end_pass"),
        End("end_pass", "Enhanced ID&V passed", "system", outcome="idv_enhanced_passed", log=False),
        Tell("refuse", "Refuse disclosure / Follow failure proc", "agent", kind="refuse_disclosure",
             facts=lambda c: {"reason": "enhanced identity verification failed", "next": "offer_colleague_only_if_they_want"}, next="end_failed"),
        End("end_failed", "End State (Failed)", "system", outcome="idv_enhanced_failed", status="failed"),
    ],
)


def _reset_enhanced(c) -> None:
    matched = c.data.get("verification", {}).get("matched", {})
    for f in ("address", "security_answer", "memorable_word"):
        c.clear_slot(f)
    for f in ("full_name", "dob"):
        if not matched.get(f, True):
            c.clear_slot(f)
    c.counters["verify"] = c.count("verify") + 1
