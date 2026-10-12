"""
FreshCart Rewards › Points Balance journeys (SOP flowcharts Journey 1–4).

Every Decide below is a diamond on the SOP flowchart. Where the
flowchart is ambiguous we encode the logically consistent reading and flag it in
docs/CONTEXT.md ("SOP interpretation decisions").
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .dsl import Ask, Call, CallEach, Choice, Decide, Effect, End, Journey, Slot, Tell
from .engine import idv_satisfied

ARCHIVE_CUTOFF = date(2023, 12, 31)
FRAUD_POINTS_THRESHOLD = 2000


# ------------------------------------------------------------------ helpers
def _txns(c, key="transactions") -> list[dict]:
    return (c.data.get(key) or {}).get("transactions", []) or []


def _brief(t: dict) -> dict:
    return {k: t.get(k) for k in ("txn_id", "date", "type", "points", "description", "store_name", "channel", "status")
            if t.get(k) is not None}


def _balance_facts(c) -> dict:
    b = c.data["balance"]
    if b["points_balance"] < 0:   # said plainly; Journey 3 follows
        return {"points_balance": b["points_balance"], "balance_is_negative": True, "card_last4": b["card_last4"],
                "next": "a few extra security questions so we can look into the minus balance"}
    return {"points_balance": b["points_balance"], "balance_value_gbp": b["balance_value_gbp"],
            "pending_points": b["pending_points"], "card_last4": b["card_last4"], "account_status": b["status"],
            "account_type": b["account_type"], "member_first_name": c.idv.get("first_name")}


def _gate(level: str, yes: str):
    return Decide("gate_idv", f"Is {level.title()} ID&V successful?", "agent",
                  rule=lambda c: "yes" if idv_satisfied(c, level) else "no",
                  branches={"yes": yes, "no": "refuse"})


def _refuse():
    return [
        Tell("refuse", "Refuse disclosure / Follow failure proc", "agent", kind="refuse_disclosure",
             facts=lambda c: {"reason": "identity not verified", "next": "offer_colleague_only_if_they_want"}, next="end_failed"),
        End("end_failed", "End State (Failed)", "system", outcome="refused_disclosure", status="failed"),
    ]


def _get_balance(next_id: str, label="Reveal points balance, card no, status & type"):
    return Call("get_balance", label, "crm", scope="loyalty_accounts", tool="get_balance",
                args=lambda c: {"account_id": c.idv["account_id"]}, save_as="balance", next=next_id)


# ------------------------------------------------------------------ J1
VIEW_BALANCE = Journey(
    id="rewards.balance.view", title="Viewing points balance, account type and status", section="Points balance",
    subagent="rewards", requires_idv="standard", start="gate_idv",
    source="FreshCart Rewards › Points Balance › Journey 1",
    description="Member wants to know their FreshCart Rewards points balance, account type, card or account status.",
    nodes=[
        _gate("standard", "get_balance"),
        _get_balance("communicate"),
        Tell("communicate", "Communicate details to customer", "agent", kind="balance_details",
             facts=_balance_facts, next="negative"),
        # Merge flow (client feedback): a negative balance continues straight into the Journey 3 process.
        # The customer isn't asked again for anything already given; only the extra enhanced checks are asked.
        Decide("negative", "Is the balance negative?", "agent",
               rule=lambda c: "yes" if c.data["balance"]["points_balance"] < 0 else "no",
               branches={"yes": "end_negative", "no": "end_success"}),
        End("end_success", "End State (Success)", "system", outcome="balance_disclosed"),
        End("end_negative", "Negative balance: continue to Journey 3", "system", outcome="negative_balance_to_dispute",
            chain="rewards.balance.negative"),
        *_refuse(),
    ],
)


# ------------------------------------------------------------------ J2
def _red(c):
    return [t for t in _txns(c) if t["points"] < 0 and t["status"] == "posted"]


def _pending(c):
    return [t for t in _txns(c) if t["status"] == "pending" and t["points"] > 0]


BALANCE_DISPUTE = Journey(
    id="rewards.balance.dispute", title="Member believes the balance should be higher", section="Points balance",
    subagent="rewards", requires_idv="standard", start="is_balance_query",
    source="FreshCart Rewards › Points Balance › Journey 2",
    description="Member thinks their points balance is wrong or lower than expected, or points are missing.",
    nodes=[
        Decide("is_balance_query", "Is this a points balance query?", "agent",
               rule=lambda c: "yes" if c.params.get("is_balance_query", True) else "no",
               branches={"yes": "gate_idv", "no": "kb"}),
        Call("kb", "Redirect to Knowledge Base", "system", scope="knowledge_base", tool="search_knowledge",
             args=lambda c: {"query": c.params.get("query") or "points balance"}, save_as="kb", next="tell_kb"),
        Tell("tell_kb", "Answer from Knowledge Base", "agent", kind="knowledge_answer",
             facts=lambda c: {"documents": c.data["kb"].get("results", [])}, next="end_kb"),
        End("end_kb", "Redirect to Knowledge Base (End)", "system", outcome="redirected_to_ask_kb",
            status="redirected"),
        _gate("standard", "get_balance"),
        _get_balance("tell_balance", "Reveal balance and establish expectation"),
        Tell("tell_balance", "Reveal balance", "agent", kind="balance_details", facts=_balance_facts,
             next="ask_expectation"),
        Ask("ask_expectation", "Establish expectation", "customer",
            slots=[Slot("expected_points", "integer", "Roughly how many points the member expected to have.",
                        required=False),
                   Slot("expectation_given", "yes_no", "True once the member has said what they expected or why "
                        "they think points are missing (even without a number).")],
            purpose="Ask roughly what balance they expected, or what points they think are missing.",
            satisfied=lambda c: c.slot("expectation_given") is not None or c.slot("expected_points") is not None,
            next="inspect"),
        Call("inspect", "Inspect transaction details", "crm", scope="loyalty_accounts", tool="get_transactions",
             args=lambda c: {"account_id": c.idv["account_id"], "days": 365}, save_as="transactions", next="red_points"),
        Decide("red_points", "Are there red points (deductions)?", "agent",
               rule=lambda c: "yes" if _red(c) else "no",
               branches={"yes": "dormant", "no": "pending"}),
        Decide("dormant", "Is account dormant?", "agent",
               rule=lambda c: "yes" if c.data["balance"]["status"] == "dormant" else "no",
               branches={"yes": "dormancy_doc", "no": "explain_deductions"}),
        Tell("explain_deductions", "Explain deductions and log outcome", "agent", kind="deductions_explained",
             facts=lambda c: {"deductions": [_brief(t) for t in _red(c)], "points_balance": c.data["balance"]["points_balance"],
                              "expected_points": c.slot("expected_points")}, next="end_deductions"),
        End("end_deductions", "End", "system", outcome="deductions_explained",
            note=lambda c: f"Explained {len(_red(c))} deduction(s)."),
        Call("dormancy_doc", "Follow dormancy process", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-DORMANCY"}, save_as="policy", next="tell_dormancy"),
        Tell("tell_dormancy", "Explain dormancy process", "agent", kind="dormancy_process",
             facts=lambda c: {"last_activity": c.data["balance"]["last_activity"],
                              "deductions": [_brief(t) for t in _red(c)], "policy": c.data["policy"].get("body")},
             next="end_dormancy"),
        End("end_dormancy", "Follow dormancy process (End)", "system", outcome="dormancy_process"),
        Decide("pending", "Are there pending points?", "agent",
               rule=lambda c: "yes" if _pending(c) else "no",
               branches={"yes": "pending_doc", "no": "archive_request"}),
        Call("pending_doc", "Check posting times", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-PENDING"}, save_as="policy", next="tell_pending"),
        Tell("tell_pending", "Advise of pending points and log", "agent", kind="pending_points",
             facts=lambda c: {"pending": [_brief(t) for t in _pending(c)], "policy": c.data["policy"].get("body")},
             next="end_pending"),
        End("end_pending", "End", "system", outcome="pending_points_advised"),
        Call("archive_request", "Raise archive request task on TaskDesk", "taskdesk", scope="case_management", tool="create_task",
             args=lambda c: {"account_id": c.idv["account_id"], "task_type": "ARCHIVE_REQUEST",
                             "summary": f"Member expects ~{c.slot('expected_points') or 'unspecified'} points; "
                                        f"balance {c.data['balance']['points_balance']}. No deductions or pending "
                                        f"points found in last 12 months."},
             save_as="task", next="tell_archive"),
        Tell("tell_archive", "Specialist will investigate archive request", "specialist", kind="archive_request_raised",
             facts=lambda c: {"task_id": c.data["task"].get("task_id"), "team": c.data["task"].get("assigned_team"),
                              "sla_working_days": c.data["task"].get("sla_working_days")},
             next="end_archive"),
        End("end_archive", "End", "system", outcome="archive_request_raised"),
        *_refuse(),
    ],
)


# ------------------------------------------------------------------ J3
_DENIES = re.compile(r"\b(never (used|made|spent|been|bought)|didn'?t (make|use|spend|do|buy)|wasn'?t me|not me|"
                     r"haven'?t (used|been|spent)|i'?ve never|no idea what that is)\b", re.I)
_ACCEPTS = re.compile(r"\b(don'?t dispute|do not dispute|not disputing|no dispute|i recogni[sz]e (it|that|them)|"
                      r"that was me|i did (make|use|spend) (it|that)|i made (it|that))\b", re.I)


def _dispute_conflict(transcript: str, value) -> str | None:
    """Catch 'I don't dispute it … I never used my card in Manchester': ask, don't guess."""
    denies, accepts = _DENIES.search(transcript), _ACCEPTS.search(transcript)
    if denies and accepts:
        return "they said they don't dispute it but also that they didn't make it"
    if value is False and denies:
        return "they said no, but also that they didn't make the redemption"
    if value is True and accepts:
        return "they said yes, but also that they recognise the redemption"
    return None


def _redemptions(c):
    return [t for t in _txns(c) if t["type"] == "redeem"]


def _disputed(c):
    return c.data.get("disputed_txn") or {}


def _holder_region(c):
    for h in (c.data.get("holders") or {}).get("holders", []):
        if h["holder_id"] == c.idv.get("holder_id"):
            return h.get("region")
    return None


def _pick_disputed(c):
    reds = _redemptions(c)
    chosen = c.slot("disputed_txn_id")
    c.data["disputed_txn"] = next((t for t in reds if t["txn_id"] == chosen), reds[0] if reds else {})


def _fraud_summary(c):
    t = _disputed(c)
    return (f"Member disputes redemption {t.get('txn_id')} of {abs(t.get('points', 0))} points on {t.get('date')} "
            f"at {t.get('store_name')}. Caller passed enhanced ID&V.")


NEGATIVE_BALANCE = Journey(
    id="rewards.balance.negative", title="Negative / minus balance", section="Points balance",
    subagent="rewards", requires_idv="enhanced", start="gate_idv",
    source="FreshCart Rewards › Points Balance › Journey 3",
    description="Member's points balance is negative/minus, or they dispute a redemption they did not make "
                "(possible fraud or unauthorised use).",
    nodes=[
        _gate("enhanced", "get_balance"),
        _get_balance("history", "Locate account on CRM"),
        Call("history", "Access recent activity", "crm", scope="loyalty_accounts", tool="get_transactions",
             args=lambda c: {"account_id": c.idv["account_id"], "days": 90}, save_as="transactions", next="ask_disputed"),
        Ask("ask_disputed", "Disputed redemption?", "customer",
            slots=[Slot("disputes_redemption", "yes_no",
                        "Whether the member says they (or their household) did not make one of these redemptions.",
                        parse_hint="true = they dispute it (didn't make it); false = they recognise it. If their "
                                   "words point both ways, pass \"unclear\".",
                        conflict=_dispute_conflict)],
            purpose="Tell the member their balance and the recent redemptions, then ask whether they dispute any of them.",
            context=lambda c: {"points_balance": c.data["balance"]["points_balance"],
                               "recent_redemptions": [_brief(t) for t in _redemptions(c)]},
            next="disputed"),
        Decide("disputed", "Disputed redemption?", "agent",
               rule=lambda c: "yes" if c.slot("disputes_redemption") else "no",
               branches={"yes": "how_many", "no": "access_history"}),
        # ---- not disputed
        Tell("access_history", "Access transaction history", "agent", kind="negative_balance_history",
             facts=lambda c: {"points_balance": c.data["balance"]["points_balance"],
                              "deductions": [_brief(t) for t in _txns(c) if t["points"] < 0]},
             next="ask_recognise"),
        Ask("ask_recognise", "Recognise transaction?", "customer",
            slots=[Slot("recognises_transaction", "yes_no",
                        "True if the member recognises the transactions that caused the negative balance.")],
            purpose="Ask whether they recognise the transactions just described.", next="recognise"),
        Decide("recognise", "Recognise transaction?", "agent",
               rule=lambda c: "yes" if c.slot("recognises_transaction") else "no",
               branches={"yes": "specialist_task", "no": "delay_doc"}),
        Call("specialist_task", "Raise task for product specialist", "taskdesk", scope="case_management", tool="create_task",
             args=lambda c: {"account_id": c.idv["account_id"], "task_type": "PRODUCT_SPECIALIST_REVIEW",
                             "summary": f"Negative balance {c.data['balance']['points_balance']}; member recognises "
                                        f"the transactions and requests review."},
             save_as="task", next="tell_task"),
        Tell("tell_task", "Task raised", "agent", kind="specialist_task_raised",
             facts=lambda c: {"task_id": c.data["task"].get("task_id"), "team": c.data["task"].get("assigned_team"),
                              "sla_working_days": c.data["task"].get("sla_working_days")}, next="end_task"),
        End("end_task", "End (Task raised)", "system", outcome="specialist_task_raised"),
        Call("delay_doc", "Check guidance", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-DELAY"}, save_as="policy", next="tell_delay"),
        Tell("tell_delay", "Explain delay and reassure", "agent", kind="explain_delay_reassure",
             facts=lambda c: {"policy": c.data["policy"].get("body"),
                              "deductions": [_brief(t) for t in _txns(c) if t["points"] < 0]}, next="end_reassured"),
        End("end_reassured", "End (Member reassured)", "system", outcome="member_reassured"),
        # ---- disputed
        Decide("how_many", "Which redemption?", "agent",
               rule=lambda c: "many" if len(_redemptions(c)) > 1 else "one",
               branches={"many": "ask_which", "one": "pick"}),
        Ask("ask_which", "Identify disputed redemption", "customer",
            slots=[Slot("disputed_txn_id", "choice", "Which redemption the member disputes.",
                        choices=lambda c: [Choice(t["txn_id"], f"{t['date']}: {abs(t['points'])} points, "
                                                              f"{t['description']} ({t.get('store_name')})")
                                           for t in _redemptions(c)])],
            purpose="Ask which of the recent redemptions they dispute.", next="pick"),
        Effect("pick", "Record disputed redemption", "system", apply=_pick_disputed, next="holders"),
        Call("holders", "Check account holders", "crm", scope="loyalty_accounts", tool="get_holders",
             args=lambda c: {"account_id": c.idv["account_id"]}, save_as="holders", next="primary"),
        Decide("primary", "Primary account holder?", "agent",
               rule=lambda c: "yes" if c.idv.get("holder_role") == "primary" else "no",
               branches={"yes": "in_store", "no": "ask_colleague"}),
        Ask("ask_colleague", "Store colleague?", "customer",
            slots=[Slot("caller_is_store_colleague", "yes_no",
                        "True if the caller says they are a FreshCart store colleague.")],
            purpose="Ask whether the caller is a FreshCart store colleague.", next="store_colleague"),
        Decide("store_colleague", "Store colleague?", "agent",
               rule=lambda c: "yes" if c.slot("caller_is_store_colleague") else "no",
               branches={"yes": "reason_code", "no": "deactivate"}),
        Call("reason_code", "Change reason code to cannot redeem", "crm", scope="loyalty_accounts", tool="set_reason_code",
             args=lambda c: {"account_id": c.idv["account_id"], "reason_code": "CANNOT_REDEEM",
                             "txn_id": _disputed(c).get("txn_id")}, save_as="reason", next="tell_protected"),
        Tell("tell_protected", "Account protected", "agent", kind="account_protected",
             facts=lambda c: {"redemption_blocked": c.data["reason"].get("redemption_blocked")}, next="end_protected"),
        End("end_protected", "End (Account protected)", "system", outcome="account_protected"),
        Call("deactivate", "Deactivate account and advise primary", "crm", scope="loyalty_accounts", tool="deactivate_account",
             args=lambda c: {"account_id": c.idv["account_id"], "reason": "Disputed redemption reported by secondary holder"},
             save_as="deactivation", next="tell_deactivated"),
        Tell("tell_deactivated", "Advise primary holder", "agent", kind="account_deactivated",
             facts=lambda c: {"status": c.data["deactivation"].get("status"),
                              "primary_notified_via": c.data["deactivation"].get("primary_notified_via")},
             next="end_awaiting_primary"),
        End("end_awaiting_primary", "End (Awaiting primary)", "system", outcome="awaiting_primary"),
        Decide("in_store", "In-store redemption?", "agent",
               rule=lambda c: "yes" if _disputed(c).get("channel") == "in_store" else "no",
               branches={"yes": "store", "no": "household"}),
        # ---- online / app redemption
        Tell("household", "Check details and ask household", "agent", kind="redemption_details_check_household",
             facts=lambda c: {"disputed_redemption": _brief(_disputed(c))}, next="ask_still"),
        Ask("ask_still", "Still disputing?", "customer",
            slots=[Slot("still_disputing", "yes_no",
                        "True if, after hearing the details and checking with their household, the member still "
                        "disputes the redemption.")],
            purpose="Ask whether, having heard the details and checked with anyone in their household, they still "
                    "dispute it.", next="still"),
        Decide("still", "Still disputing?", "agent",
               rule=lambda c: "yes" if c.slot("still_disputing") else "no",
               branches={"yes": "voice_contact", "no": "tell_resolved"}),
        Tell("tell_resolved", "Note comms and log outcome", "agent", kind="dispute_resolved",
             facts=lambda c: {"disputed_redemption": _brief(_disputed(c))}, next="end_resolved"),
        End("end_resolved", "End", "system", outcome="dispute_withdrawn_household"),
        # ---- in-store redemption
        Call("store", "Look up redemption store", "crm", scope="loyalty_accounts", tool="get_store",
             args=lambda c: {"store_id": _disputed(c).get("store_id")}, save_as="store", next="not_local"),
        Decide("not_local", "Store is not local?", "agent",
               rule=lambda c: "yes" if c.data["store"].get("region") != _holder_region(c) else "no",
               branches={"yes": "voice_contact", "no": "over_threshold"}),
        Decide("over_threshold", f"More than {FRAUD_POINTS_THRESHOLD} points?", "agent",
               rule=lambda c: "yes" if abs(_disputed(c).get("points", 0)) > FRAUD_POINTS_THRESHOLD else "no",
               branches={"yes": "fraud_case", "no": "police_doc"}),
        Call("police_doc", "Check guidance", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-POLICE"}, save_as="policy", next="tell_police"),
        Tell("tell_police", "Advise member to contact police", "agent", kind="advise_police",
             facts=lambda c: {"disputed_redemption": _brief(_disputed(c)), "policy": c.data["policy"].get("body")},
             next="end_police"),
        End("end_police", "End (Police advised)", "system", outcome="police_advised"),
        # ---- fraud routing (shared)
        Decide("voice_contact", "Voice contact?", "agent",
               rule=lambda c: "yes" if c.channel == "voice" else "no",
               branches={"yes": "transfer", "no": "fraud_case"}),
        Call("transfer", "Contact fraud helpline", "system", scope="case_management", tool="warm_transfer",
             args=lambda c: {"session_id": c.session_id, "queue": "fraud", "summary": _fraud_summary(c)},
             save_as="transfer", next="got_through"),
        Decide("got_through", "Got through to fraud team?", "agent",
               rule=lambda c: "yes" if c.data["transfer"].get("connected") else "no",
               branches={"yes": "tell_transfer", "no": "fraud_case"}),
        # Customer-facing wording is deliberately simple (client feedback): every SOP branch above still runs.
        Tell("tell_transfer", "Warm transfer call to fraud", "agent", kind="transfer_to_fraud_team",
             facts=lambda c: {"transfer_to": "our fraud team", "they_will": "help you with this",
                              "ask_customer_to": "please stay on the line"}, next="end_transferred"),
        End("end_transferred", "End (Transferred)", "system", outcome="warm_transferred_to_fraud", status="handoff"),
        Call("fraud_case", "Raise fraud case via TaskDesk", "taskdesk", scope="case_management", tool="create_fraud_case",
             args=lambda c: {"account_id": c.idv["account_id"], "summary": _fraud_summary(c),
                             "txn_id": _disputed(c).get("txn_id"), "points_disputed": abs(_disputed(c).get("points", 0))},
             save_as="case", next="tell_case"),
        Tell("tell_case", "Fraud case raised", "agent", kind="fraud_case_raised",
             facts=lambda c: {"case_id": c.data["case"].get("case_id"), "team": c.data["case"].get("assigned_team"),
                              "contact_within_working_days": c.data["case"].get("contact_within_working_days")},
             next="end_case"),
        End("end_case", "End (Case raised)", "system", outcome="fraud_case_raised"),
        *_refuse(),
    ],
)


# ------------------------------------------------------------------ J4
def _set_range(c):
    target = date.fromisoformat(c.slot("target_date"))
    end = date.fromisoformat(c.slot("target_date_end")) if c.slot("target_date_end") else None
    start = target if end else target - timedelta(days=30)
    end = end or target + timedelta(days=30)
    c.data["range"] = {"date_from": str(max(start, ARCHIVE_CUTOFF + timedelta(days=1))),
                       "date_to": str(min(end, date.today()))}


def _classify(c):
    target = date.fromisoformat(c.slot("target_date"))
    rows = _txns(c)
    near = [t for t in rows if abs((date.fromisoformat(t["date"]) - target).days) <= 60]
    rows = near or rows
    c.data["review"] = {
        "redemptions": [t for t in rows if t["points"] < 0 and t["type"] == "redeem"],
        "returns": [t for t in rows if t["points"] < 0 and t["type"] in ("return", "expiry")],
        "earned": [t for t in rows if t["points"] > 0],
    }


def _voucher_facts(c):
    vouchers = {v.get("code"): v for v in c.data.get("vouchers", []) if not v.get("error")}
    bookings = {b.get("ref"): b for b in c.data.get("bookings", []) if not b.get("error")}
    out = []
    for t in c.data["review"]["redemptions"]:
        item = _brief(t)
        if t.get("voucher_code") in vouchers:
            item["voucher"] = vouchers[t["voucher_code"]]
        if t.get("supplier_booking_ref") in bookings:
            item["supplier_booking"] = bookings[t["supplier_booking_ref"]]
        out.append(item)
    return out


HISTORY = Journey(
    id="rewards.history", title="Redemption / transaction history", section="Points balance",
    subagent="rewards", requires_idv="standard", start="gate_idv",
    source="FreshCart Rewards › Points Balance › Journey 4",
    description="Member wants to see or understand past redemptions, vouchers, points spent or transaction history.",
    nodes=[
        _gate("standard", "ask_period"),
        Ask("ask_period", "Colleague: Click 'Transaction Details'", "customer",
            slots=[Slot("target_date", "date", "When the transaction or redemption they're asking about happened.",
                        parse_hint="yyyy-mm-dd; first day of the month if they named a month; resolve relative dates "
                                   "against today's date."),
                   Slot("target_date_end", "date", "The end of the period, if they gave a range.", required=False,
                        parse_hint="yyyy-mm-dd; leave out if no range.")],
            purpose="Ask roughly when the transaction or redemption they're asking about happened.", next="post_2023"),
        Decide("post_2023", "Is target date post 31/12/2023?", "agent",
               rule=lambda c: "yes" if date.fromisoformat(c.slot("target_date")) > ARCHIVE_CUTOFF else "no",
               branches={"yes": "set_range", "no": "archive"}),
        Effect("set_range", "Colleague: Enter 'To and From' dates", "agent", apply=_set_range, next="find"),
        Call("find", "CRM: Retrieve transactions for date range", "crm", scope="loyalty_accounts", tool="get_transactions",
             args=lambda c: {"account_id": c.idv["account_id"], **c.data["range"]}, save_as="transactions", next="grid"),
        Call("archive", "CRM: Retrieve transactions Jan 2015 – Dec 2023", "crm", scope="loyalty_accounts",
             tool="get_archive_transactions", args=lambda c: {"account_id": c.idv["account_id"]},
             save_as="transactions", next="grid"),
        Effect("grid", "CRM: Display transaction details grid", "crm", apply=_classify, next="vouchers"),
        CallEach("vouchers", "Check for Voucher code: open redemption details", "crm", scope="loyalty_accounts", tool="get_voucher",
                 items=lambda c: [{"code": t["voucher_code"]} for t in c.data["review"]["redemptions"]
                                  if t.get("voucher_code")], save_as="vouchers", next="bookings"),
        CallEach("bookings", "Check for Supplier Booking Ref", "crm", scope="loyalty_accounts", tool="get_supplier_booking",
                 items=lambda c: [{"ref": t["supplier_booking_ref"]} for t in c.data["review"]["redemptions"]
                                  if t.get("supplier_booking_ref")], save_as="bookings", next="answer"),
        Tell("answer", "Colleague: Answer query and complete review", "agent", kind="transaction_review",
             facts=lambda c: {"period_searched": c.data.get("range") or "Jan 2015 to Dec 2023 archive",
                              "redemptions": _voucher_facts(c),
                              "returns_points_removed": [_brief(t) for t in c.data["review"]["returns"]],
                              "earned": [_brief(t) for t in c.data["review"]["earned"]]},
             next="end_review"),
        End("end_review", "Complete review", "system", outcome="history_query_answered"),
        *_refuse(),
    ],
)
