"""
Journey engine tests against the REAL mock backend through the REAL MCP gateway.

No LLM is simulated here: the engine only ever receives typed slot values, so each
test supplies those values directly — exactly what the extractor hands over after
normalisation in production. Each test walks one branch of an SOP flowchart and
asserts the outcome and the path taken.
"""
import asyncio

import httpx
import pytest

from services.agent_runtime.journeys.dsl import Ctx
from services.agent_runtime.journeys.engine import Engine, JourneyRun
from services.agent_runtime.journeys.registry import JOURNEYS, SUBAGENTS
from services.agent_runtime.mcp_hub import McpHub, ScopedToolbox
from services.agent_runtime.pii import Vault

CARDS = {  # last 11 digits, as a member would read them
    "priya": "12345678901", "oliver": "20456789012", "margaret": "30567890123", "james": "40678901234",
    "aisha": "50789012345", "tom": "60890123456", "sophie": "70901234567", "evans": "80012345678",
    "ben": "90123456789"}


class Harness:
    def __init__(self, hub, caller_number=None):
        self.hub = hub
        self.caller_number = caller_number
        self.vault = Vault()
        self.idv: dict = {}
        self.calls: list[tuple[str, str]] = []

    async def _call(self, scope, tool, args, run, node):
        tb = ScopedToolbox(self.hub, SUBAGENTS[run.journey.subagent].scopes, run.journey.subagent)
        self.calls.append((scope, tool))
        return await tb.call(scope, tool, args)

    def engine(self):
        return Engine(self._call, lambda run, node, phase: None)

    def run(self, jid, params=None):
        ctx = Ctx(session_id="test", channel="voice", vault=self.vault, idv=self.idv, params=params or {},
                  caller_number=self.caller_number)
        return JourneyRun(JOURNEYS[jid], ctx)

    async def drive(self, jid, answers: list[dict], params=None) -> JourneyRun:
        eng, run = self.engine(), self.run(jid, params)
        self.tells = []  # everything the journey asked the composer to say, across all turns
        await eng.advance(run)
        self.tells += run.tells
        for a in answers:
            assert run.status == "awaiting_input", f"expected question, got {run.status} at {run.node_id}"
            eng.supply(run, a)
            await eng.advance(run)
            self.tells += run.tells
        return run


async def _with_hub(services, fn, caller_number=None):
    await httpx.AsyncClient().post(services["backend"] + "/admin/reset")
    hub = McpHub(services["gateway"])
    await hub.start()
    try:
        return await fn(Harness(hub, caller_number))
    finally:
        await hub.stop()


def std_idv(card, name, dob):
    return [{"card_number": "60012300" + card}, {"full_name": name, "dob": dob}]


def test_j1_view_balance(services):
    async def t(h):
        idv = await h.drive("idv.standard", std_idv(CARDS["priya"], "Priya Sharma", "1988-04-12"))
        assert idv.status == "completed" and h.idv["level"] == "standard"
        run = await h.drive("rewards.balance.view", [])
        assert run.outcome == "balance_disclosed"
        facts = run.tells[0]["facts"]
        assert facts["points_balance"] == 3820 and facts["card_last4"] == "8901"
    asyncio.run(_with_hub(services, t))


def test_idv_fails_after_retry_then_refuses(services):
    async def t(h):
        idv = await h.drive("idv.standard", [{"card_number": "60012300" + CARDS["priya"]},
                                             {"full_name": "Priya Sharma", "dob": "1990-01-01"},
                                             {"full_name": "Priya Sharma", "dob": "1991-01-01"}])
        assert idv.status == "failed" and idv.outcome == "idv_failed"
        run = await h.drive("rewards.balance.view", [])
        assert run.outcome == "refused_disclosure" and ("loyalty_accounts", "get_balance") not in h.calls
    asyncio.run(_with_hub(services, t))


def test_j2_deductions(services):
    async def t(h):
        await h.drive("idv.standard", std_idv(CARDS["oliver"], "Oliver Bennett", "1979-11-03"))
        run = await h.drive("rewards.balance.dispute", [{"expected_points": 3800, "expectation_given": True}])
        assert run.outcome == "deductions_explained"
    asyncio.run(_with_hub(services, t))


def test_j2_dormant(services):
    async def t(h):
        await h.drive("idv.standard", std_idv(CARDS["margaret"], "Margaret Doyle", "1954-02-27"))
        run = await h.drive("rewards.balance.dispute", [{"expectation_given": True}])
        assert run.outcome == "dormancy_process"
    asyncio.run(_with_hub(services, t))


def test_j2_pending(services):
    async def t(h):
        await h.drive("idv.standard", std_idv(CARDS["james"], "James Okafor", "1992-07-19"))
        run = await h.drive("rewards.balance.dispute", [{"expectation_given": True}])
        assert run.outcome == "pending_points_advised"
    asyncio.run(_with_hub(services, t))


def test_j2_archive_request(services):
    async def t(h):
        await h.drive("idv.standard", std_idv(CARDS["aisha"], "Aisha Rahman", "1985-09-30"))
        run = await h.drive("rewards.balance.dispute", [{"expected_points": 5000, "expectation_given": True}])
        assert run.outcome == "archive_request_raised" and run.tells[-1]["facts"]["task_id"].startswith("NOVA-T-")
    asyncio.run(_with_hub(services, t))


def enh(card, name, dob, address, answer, word):
    return [{"card_number": "60012300" + card}, {"full_name": name, "dob": dob}, {"address": address},
            {"security_answer": answer}, {"memorable_word": word}]


def test_j3_fraud_transfer_non_local(services):
    async def t(h):
        idv = await h.drive("idv.enhanced", enh(CARDS["tom"], "Tom Hughes", "1990-01-08", "5 Gloucester Road BS7 8AA",
                                                "fiesta", "orchard"))
        assert idv.status == "completed" and h.idv["level"] == "enhanced"
        run = await h.drive("rewards.balance.negative", [{"disputes_redemption": True}])
        assert run.outcome == "warm_transferred_to_fraud" and run.status == "handoff"
    asyncio.run(_with_hub(services, t))


def test_j3_fraud_case_when_line_unavailable(services):
    async def t(h):
        await httpx.AsyncClient().post(services["backend"] + "/admin/fraud-line", json={"available": False})
        await h.drive("idv.enhanced", enh(CARDS["tom"], "Tom Hughes", "1990-01-08", "BS7 8AA", "Fiesta", "orchard"))
        run = await h.drive("rewards.balance.negative", [{"disputes_redemption": True}])
        assert run.outcome == "fraud_case_raised"
    asyncio.run(_with_hub(services, t))


def test_j3_police_local_under_threshold(services):
    async def t(h):
        await h.drive("idv.enhanced", enh(CARDS["sophie"], "Sophie Clarke", "1996-12-15", "90 Upper Street N1 0NP",
                                          "Amelie", "velvet"))
        run = await h.drive("rewards.balance.negative", [{"disputes_redemption": True}])
        assert run.outcome == "police_advised"
    asyncio.run(_with_hub(services, t))


def test_j3_secondary_holder_not_colleague_deactivates(services):
    async def t(h):
        await h.drive("idv.enhanced", enh(CARDS["evans"], "Rachel Evans", "1977-10-09", "12 Mill Lane LS6 2BB",
                                          "Elm Street", "saffron"))
        assert h.idv["holder_role"] == "secondary"
        run = await h.drive("rewards.balance.negative", [{"disputes_redemption": True},
                                                        {"caller_is_store_colleague": False}])
        assert run.outcome == "awaiting_primary"
    asyncio.run(_with_hub(services, t))


def test_j3_primary_online_resolved_with_household(services):
    async def t(h):
        await h.drive("idv.enhanced", enh(CARDS["evans"], "David Evans", "1975-05-21", "LS6 2BB", "paperboy", "granite"))
        run = await h.drive("rewards.balance.negative", [{"disputes_redemption": True}, {"still_disputing": False}])
        assert run.outcome == "dispute_withdrawn_household"
    asyncio.run(_with_hub(services, t))


def test_j3_not_disputed_reassured(services):
    async def t(h):
        await h.drive("idv.enhanced", enh(CARDS["ben"], "Ben Walsh", "1983-03-04", "44 Park Row BS1 5LJ",
                                          "Mrs Patel", "falcon"))
        run = await h.drive("rewards.balance.negative", [{"disputes_redemption": False},
                                                        {"recognises_transaction": False}])
        assert run.outcome == "member_reassured"
    asyncio.run(_with_hub(services, t))


def test_step_up_standard_to_enhanced_reuses_details(services):
    async def t(h):
        await h.drive("idv.standard", std_idv(CARDS["tom"], "Tom Hughes", "1990-01-08"))
        run = await h.drive("idv.enhanced", [{"address": "BS7 8AA"}, {"security_answer": "Fiesta"},
                                             {"memorable_word": "orchard"}])
        assert run.status == "completed" and "ask_card" not in run.visited
    asyncio.run(_with_hub(services, t))


def test_j4_recent_and_archive(services):
    async def t(h):
        await h.drive("idv.standard", std_idv(CARDS["priya"], "Priya Sharma", "1988-04-12"))
        recent = await h.drive("rewards.history", [{"target_date": "2026-03-01"}])
        red = recent.tells[0]["facts"]["redemptions"]
        assert any(r.get("voucher", {}).get("partner") == "DineClub" for r in red)
        old = await h.drive("rewards.history", [{"target_date": "2019-05-01"}])
        assert "archive" in old.visited
        assert any("supplier_booking" in r for r in old.tells[0]["facts"]["redemptions"])
    asyncio.run(_with_hub(services, t))


def test_signup_analogue_no_contact(services):
    async def t(h):
        run = await h.drive("enrolment.signup", [
            {"registration_route": "phone"}, {"confirm_proceed": True},
            {"full_name": "Grace Miller", "dob": "1991-06-02"}, {"address": "7 Rose Lane, Bath BA1 2AB"},
            {"has_contact": False}, {"terms_consent": True}])
        assert run.outcome == "analogue_registration_complete"
        assert "leave_blank" in run.visited
    asyncio.run(_with_hub(services, t))


def test_signup_duplicate_detected(services):
    async def t(h):
        run = await h.drive("enrolment.signup", [
            {"registration_route": "phone"}, {"confirm_proceed": True},
            {"full_name": "Priya Sharma", "dob": "1988-04-12"}, {"address": "14 Redland Grove BS6 6PR"},
            {"has_contact": True}, {"email": "priya@example.com"}, {"terms_consent": True}, {"marketing_opt_in": False}])
        assert run.outcome == "duplicate_account"
    asyncio.run(_with_hub(services, t))


def test_signup_digital_with_link(services):
    async def t(h):
        run = await h.drive("enrolment.signup", [{"registration_route": "app"}, {"wants_link": True},
                                                 {"mobile": "07700900123"}])
        assert run.outcome == "digital_registration_guided" and ("member_enrolment", "send_link") in h.calls
    asyncio.run(_with_hub(services, t))


def test_scope_isolation(services):
    async def t(h):
        tb = ScopedToolbox(h.hub, SUBAGENTS["knowledge"].scopes, "knowledge")
        with pytest.raises(PermissionError):
            await tb.call("loyalty_accounts", "get_balance", {"account_id": "ACC-1001"})
    asyncio.run(_with_hub(services, t))


# ------------------------------------------------------------------ FreshCart Online (Online IVR board)
def online_idv(order, correct=True, phone=None, postcode=None):
    steps = [{"order_number": order}, {"order_number_correct": correct}]
    if phone:
        steps.append({"phone": phone})
    if postcode:
        steps.append({"postcode": postcode})
    return steps


def test_online_idv_skips_phone_when_calling_number_matches(services):
    async def t(h):
        run = await h.drive("idv.online_order", online_idv("7004128855", postcode="SE17PB"))
        assert run.status == "completed" and h.idv["online_verified"] and "ask_phone" not in run.visited
    asyncio.run(_with_hub(services, t, caller_number="+447700900111"))


def test_online_idv_asks_phone_when_calling_number_differs(services):
    async def t(h):
        run = await h.drive("idv.online_order", online_idv("7004128855", phone="07700900111", postcode="SE1 7PB"))
        assert run.status == "completed" and "ask_phone" in run.visited
    asyncio.run(_with_hub(services, t, caller_number="07999000000"))


def test_online_idv_reenter_after_wrong_readback(services):
    async def t(h):
        run = await h.drive("idv.online_order", [{"order_number": "7004128856"}, {"order_number_correct": False},
                                              {"order_number": "7004128855"}, {"order_number_correct": True},
                                              {"phone": "07700900111"}, {"postcode": "SE1 7PB"}])
        assert run.status == "completed" and "reenter" in run.visited
    asyncio.run(_with_hub(services, t))


def test_online_invalid_order_twice_transfers_to_agent(services):
    async def t(h):
        run = await h.drive("idv.online_order", [{"order_number": "7000000001"}, {"order_number_correct": True},
                                              {"order_number": "7000000002"}, {"order_number_correct": True}])
        assert run.status == "handoff" and run.outcome == "transferred_to_online_agent"
        assert "retry_order" in run.visited
    asyncio.run(_with_hub(services, t))


def test_online_delivery_not_delivered_no_agent_ends_call(services):
    async def t(h):
        await h.drive("idv.online_order", online_idv("7004133907", postcode="BS8 1TH"))
        run = await h.drive("online.delivery_status", [{"wants_agent": False}])
        assert run.outcome == "delivery_status_given"
        assert run.journey.by_id[run.node_id].end_call
        assert h.tells[0]["facts"]["slot_start"] == "17:00"
    asyncio.run(_with_hub(services, t, caller_number="07700900222"))


def test_online_delivery_cancelled_wants_agent(services):
    async def t(h):
        await h.drive("idv.online_order", online_idv("7004120016", postcode="M20 2WS"))
        run = await h.drive("online.delivery_status", [{"wants_agent": True}])
        assert run.status == "handoff" and h.tells[0]["kind"] == "delivery_status_cancelled"
    asyncio.run(_with_hub(services, t, caller_number="07700900333"))


def test_online_delivered_item_issue_chains_to_refund(services):
    async def t(h):
        await h.drive("idv.online_order", online_idv("7004128855", postcode="SE1 7PB"))
        run = await h.drive("online.delivery_status", [{"items_issue": True}])
        assert run.journey.by_id[run.node_id].chain == "online.refund_query"
    asyncio.run(_with_hub(services, t, caller_number="07700900111"))


def test_online_refund_link_sent_then_done(services):
    async def t(h):
        await h.drive("idv.online_order", online_idv("7004128855", postcode="SE1 7PB"))
        run = await h.drive("online.refund_query", [{"wants_link": True}, {"anything_else": False}])
        assert run.outcome == "refund_query_resolved" and ("order_service", "send_refund_link") in h.calls
        sent = next(x for x in h.tells if x["kind"] == "refund_link_sent")
        assert sent["facts"]["sent_to_last4"] == "0111"
    asyncio.run(_with_hub(services, t, caller_number="07700900111"))


def test_online_refund_prefers_agent(services):
    async def t(h):
        await h.drive("idv.online_order", online_idv("7004128855", postcode="SE1 7PB"))
        run = await h.drive("online.refund_query", [{"wants_link": False, "wants_agent": True}])
        assert run.status == "handoff"
    asyncio.run(_with_hub(services, t, caller_number="07700900111"))


def test_online_refund_refused_without_verification(services):
    async def t(h):
        run = await h.drive("online.refund_query", [])
        assert run.outcome == "online_refused_disclosure" and ("order_service", "send_refund_link") not in h.calls
    asyncio.run(_with_hub(services, t))


def test_online_subagent_cannot_reach_rewards_scope(services):
    async def t(h):
        tb = ScopedToolbox(h.hub, SUBAGENTS["online"].scopes, "online")
        with pytest.raises(PermissionError):
            await tb.call("order_verification", "verify_postcode", {"order_ref": "x", "postcode": "y"})
    asyncio.run(_with_hub(services, t))
