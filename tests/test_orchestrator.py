"""
Orchestrator tests: the exact functions the LLM coordinator calls (route_request, submit_answer,
end_call), run against the real journey engine, real MCP gateway and real mock backend.
No LLM is involved or simulated: we call the tool handlers with the arguments a model would send.
"""
import asyncio

import httpx

from services.agent_runtime.mcp_hub import McpHub
from services.agent_runtime.orchestrator import Orchestrator
from services.agent_runtime.session import Session
from services.agent_runtime.tools import instructions, tool_definitions
from services.agent_runtime.cascade import session_config
from services.agent_runtime.config import settings


async def _run(services, fn, caller=None):
    await httpx.AsyncClient().post(services["backend"] + "/admin/reset")
    hub = McpHub(services["gateway"])
    await hub.start()
    try:
        s = Session()
        s.caller_number = caller
        return await fn(Orchestrator(hub), s)
    finally:
        await hub.stop()


def test_route_blocks_on_idv_then_answers_resume_balance(services):
    async def t(o, s):
        s.last_transcript = "what's my rewards balance"
        r = await o.route(s, [{"journey": "rewards.balance.view"}])
        assert r["ask"]["journey"] == "Standard ID&V" and r["ask"]["fields"][0]["name"] == "card_number"
        # the card number is taken from the transcript by code, whatever the model typed
        s.last_transcript = "six zero zero one two three zero zero one two three four five six seven eight nine zero one"
        r = await o.answer(s, {"card_number": "garbled"})
        assert [f["name"] for f in r["ask"]["fields"]] == ["full_name", "dob"]
        s.last_transcript = "Priya Sharma, twelfth of April 1988"
        r = await o.answer(s, {"full_name": "Priya Sharma", "dob": "1988-04-12"})
        balance = next(f for f in r["facts"] if f["kind"] == "balance_details")
        assert balance["facts"]["points_balance"] == 3820 and r["ask"] is None
        assert 3820.0 in s.allowed_numbers  # grounding allow-list now contains the balance
    asyncio.run(_run(services, t))


def test_two_journeys_share_one_idv(services):
    async def t(o, s):
        s.last_transcript = "balance and my march redemptions, card 12345678901"
        r = await o.route(s, [{"journey": "rewards.balance.view"}, {"journey": "rewards.history"}])
        idv_runs = [x for x in s.runs if x.journey.id.startswith("idv.")]
        assert len(idv_runs) == 1 and r["ask"]["fields"][0]["name"] == "full_name"  # card prefilled
        s.last_transcript = "Priya Sharma 12 April 1988"
        r = await o.answer(s, {"full_name": "Priya Sharma", "dob": "1988-04-12"})
        assert any(f["kind"] == "balance_details" for f in r["facts"])
        # "march" in the routing sentence pre-fills the history date: both answered, nothing left to ask
        assert any(f["kind"] == "transaction_review" for f in r["facts"]) and r["ask"] is None
    asyncio.run(_run(services, t))


def test_invalid_answer_is_explained(services):
    async def t(o, s):
        s.last_transcript = "where is my online order"
        await o.route(s, [{"journey": "online.delivery_status"}])
        s.last_transcript = "it's one two three"
        r = await o.answer(s, {"order_number": "123"})
        assert "order_number" in (r["ask"]["invalid"] or {})
    asyncio.run(_run(services, t))


def test_online_known_answers_prefill_and_chain_and_close(services):
    async def t(o, s):
        s.last_transcript = "my online order 7004128855 had missing items"
        r = await o.route(s, [{"journey": "online.delivery_status"}], known_answers={"order_number": "7004128855"})
        assert r["ask"]["context"]["order_number_to_read_back"].startswith("7 0 0 4")
        s.last_transcript = "yes"
        await o.answer(s, {"order_number_correct": True})
        s.last_transcript = "SE1 7PB"
        r = await o.answer(s, {"postcode": "SE1 7PB"})
        assert any(f["kind"] == "delivery_status_delivered" for f in r["facts"])
        r = await o.answer(s, {"items_issue": "yes"})           # string yes is coerced
        assert r["ask"]["journey"] == "Online refund query"         # chained journey is asking
        await o.answer(s, {"wants_link": True})
        r = await o.answer(s, {"anything_else": False})
        assert r["close_call"] is True
        out = await o.end_call(s)
        assert out["ok"] and s.closing
    asyncio.run(_run(services, t, caller="07700900111"))


def test_route_rejects_unknown_and_duplicate_journeys(services):
    async def t(o, s):
        r = await o.route(s, [{"journey": "idv.standard"}, {"journey": "made.up"}])
        assert "error" in r
        await o.route(s, [{"journey": "knowledge.ask", "query": "how long do bonus points take"}])
        r = await o.answer(s, {"x": 1})
        assert "error" in r  # nothing pending after a knowledge answer
    asyncio.run(_run(services, t))


def test_known_date_survives_idv_and_both_journeys_answer_in_one_turn(services):
    """Example 2: 'balance, and what did I spend points on in March?' â€” one shared ID&V, then both answers."""
    async def t(o, s):
        s.last_transcript = "what's my balance and what did I spend my points on in March, card 12345678901"
        r = await o.route(s, [{"journey": "rewards.balance.view"}, {"journey": "rewards.history"}],
                          known_answers={"card_number": "12345678901", "target_date": "2026-03-01"})
        assert r["ask"]["journey"] == "Standard ID&V" and [f["name"] for f in r["ask"]["fields"]] == ["full_name", "dob"]
        s.last_transcript = "Priya Sharma, twelfth of April 1988"
        r = await o.answer(s, {"full_name": "Priya Sharma", "dob": "1988-04-12"})
        kinds = {f["kind"] for f in r["facts"]}
        assert {"balance_details", "transaction_review"} <= kinds and r["ask"] is None
        review = next(f for f in r["facts"] if f["kind"] == "transaction_review")["facts"]
        assert any(x.get("voucher", {}).get("partner") == "DineClub" for x in review["redemptions"])
    asyncio.run(_run(services, t))


def test_fraud_transfer_is_a_handover_not_a_goodbye(services):
    """Example 6 â†’ fraud transfer: `transferred`, no close_call, no further questions; end_call is refused;
    the colleague gets reason + summary + transcript; the outcome is logged with a disposition."""
    async def t(o, s):
        s.last_transcript = "my balance is minus 1250 and I didn't spend those points, card 60890123456"
        s.add_history("customer", s.last_transcript)
        r = await o.route(s, [{"journey": "rewards.balance.negative"}], known_answers={"card_number": "60890123456"})
        assert [f["name"] for f in r["ask"]["fields"]] == ["full_name", "dob"]
        for answers in ({"full_name": "Tom Hughes", "dob": "1990-01-08"}, {"address": "5 Gloucester Road, Bristol BS7 8AA"},
                        {"security_answer": "Fiesta"}, {"memorable_word": "orchard"}):
            s.last_transcript = " ".join(answers.values())
            r = await o.answer(s, answers)
        assert r["ask"]["fields"][0]["name"] == "disputes_redemption"
        assert r["ask"]["context"]["points_balance"] == -1250
        s.last_transcript = "yes I dispute it"
        r = await o.answer(s, {"disputes_redemption": True})
        assert r["outcomes"][-1]["outcome"] == "warm_transferred_to_fraud"
        assert r["transferred"] is True and r["close_call"] is False and r["ask"] is None
        tell = next(f for f in r["facts"] if f["kind"] == "transfer_to_fraud_team")["facts"]
        assert tell == {"transfer_to": "our fraud team", "they_will": "help you with this",
                        "ask_customer_to": "please stay on the line"}            # simple wording only
        assert "error" in await o.end_call(s) and not s.closing                   # no goodbye after a transfer
        assert "error" in await o.transfer(s, "again")                            # never a second transfer
        out = await o.log_outcome(s, ended_by="transferred")
        assert out["disposition"] == "transferred_to_advisor" and "Disputes redemption" in out["transfer_reason"] \
            or "disputes redemption" in out["transfer_reason"].lower()
        state = (await httpx.AsyncClient().get(services["backend"] + "/admin/state")).json()
        handover = state["transfers"][-1]["handover"]
        assert handover["reason"] and "Customer called about: Negative / minus balance" in handover["summary"]
        assert any("didn't spend those points" in line for line in handover["transcript"])
        assert state["contacts"][-1]["disposition"] == "transferred_to_advisor"
    asyncio.run(_run(services, t))


def test_merge_flow_balance_turns_into_dispute(services):
    """Balance check comes out negative â†’ told plainly â†’ straight into the negative-balance process: only the
    extra enhanced checks are asked, nothing is asked twice, one ID&V run per level, then fraud transfer."""
    async def t(o, s):
        asked: list[str] = []

        def note(r):
            if r.get("ask"):
                asked.extend(f["name"] for f in r["ask"]["fields"])
            return r
        s.last_transcript = "what's my rewards balance"
        note(await o.route(s, [{"journey": "rewards.balance.view"}]))
        s.last_transcript = "it's 6 0 8 9 0 1 2 3 4 5 6"
        note(await o.answer(s, {"card_number": "60890123456"}))
        s.last_transcript = "Tom Hughes, eighth of January nineteen ninety"
        r = note(await o.answer(s, {"full_name": "Tom Hughes", "dob": "1990-01-08"}))
        bal = next(f for f in r["facts"] if f["kind"] == "balance_details")["facts"]
        assert bal["points_balance"] == -1250 and bal["balance_is_negative"] is True
        assert [f["name"] for f in r["ask"]["fields"]] == ["address"]          # step-up reuses card, name, DOB
        # the customer volunteers the dispute while the step-up is pending: no duplicate journey
        s.last_transcript = "I didn't spend those points"
        dup = await o.route(s, [{"journey": "rewards.balance.negative"}])
        assert "error" in dup and dup["ask"]["fields"][0]["name"] == "address"
        for said, answers in (("5 Gloucester Road Bristol BS7 8AA", {"address": "5 Gloucester Road, Bristol BS7 8AA"}),
                              ("Fiesta", {"security_answer": "Fiesta"}), ("orchard", {"memorable_word": "orchard"})):
            s.last_transcript = said
            r = note(await o.answer(s, answers))
        s.last_transcript = "yes I dispute it, I've never been to Manchester"
        r = note(await o.answer(s, {"disputes_redemption": True}))
        assert r["transferred"] is True
        assert len(asked) == len(set(asked)), f"asked twice: {asked}"
        idv = [x.journey.id for x in s.runs if x.journey.id.startswith("idv.")]
        assert idv == ["idv.standard", "idv.enhanced"]
    asyncio.run(_run(services, t))


def test_contradictory_dispute_answer_asks_instead_of_guessing(services):
    async def t(o, s):
        s.last_transcript = "card 60890123456"
        await o.route(s, [{"journey": "rewards.balance.negative"}], known_answers={"card_number": "60890123456"})
        for answers in ({"full_name": "Tom Hughes", "dob": "1990-01-08"}, {"address": "5 Gloucester Road, Bristol BS7 8AA"},
                        {"security_answer": "Fiesta"}, {"memorable_word": "orchard"}):
            s.last_transcript = " ".join(answers.values())
            await o.answer(s, answers)
        s.last_transcript = "No, I don't dispute it. But I never used my card in Manchester."
        r = await o.answer(s, {"disputes_redemption": False})
        assert r["ask"]["invalid"]["disputes_redemption"]["kind"] == "unclear"
        assert not any(t["tool"] == "get_holders" for t in [])  # nothing ran: still at the same question
        assert r["ask"]["fields"][0]["name"] == "disputes_redemption"
        s.last_transcript = "unclear"                       # the coordinator may also flag it itself
        r = await o.answer(s, {"disputes_redemption": "unclear"})
        assert r["ask"]["invalid"]["disputes_redemption"]["kind"] == "unclear"
        s.last_transcript = "Sorry, yes, I'm disputing it, that wasn't me"
        r = await o.answer(s, {"disputes_redemption": True})
        assert r["transferred"] is True
    asyncio.run(_run(services, t))


def test_partial_card_is_incomplete_not_wrong_and_joins_with_the_rest(services):
    async def t(o, s):
        s.last_transcript = "what's my balance"
        await o.route(s, [{"journey": "rewards.balance.view"}])
        s.last_transcript = "1 2 3 4 5 6 7"
        r = await o.answer(s, {"card_number": "1234567"})
        assert r["ask"]["invalid"]["card_number"]["kind"] == "incomplete"
        idv = next(x for x in s.runs if x.journey.id == "idv.standard")
        assert idv.ctx.count("locate") == 0 and "locate" not in idv.visited   # nothing verified, no retry used
        s.last_transcript = "8 9 0 1"
        r = await o.answer(s, {"card_number": "8901"})
        assert [f["name"] for f in r["ask"]["fields"]] == ["full_name", "dob"]  # joined: account found
    asyncio.run(_run(services, t))


def test_partial_postcode_and_address_without_postcode(services):
    async def t(o, s):
        s.last_transcript = "where's my order 7004128855"
        await o.route(s, [{"journey": "online.delivery_status"}], known_answers={"order_number": "7004128855"})
        s.last_transcript = "yes"
        await o.answer(s, {"order_number_correct": True})
        s.last_transcript = "it's S E one"
        r = await o.answer(s, {"postcode": "SE1"})
        assert r["ask"]["invalid"]["postcode"]["kind"] == "incomplete"
        s.last_transcript = "seven P B"
        r = await o.answer(s, {"postcode": "7PB"})
        assert any(f["kind"] == "delivery_status_delivered" for f in r["facts"])
    asyncio.run(_run(services, t, caller="07700900111"))

    async def t2(o, s):
        s.last_transcript = "card 60890123456"
        await o.route(s, [{"journey": "rewards.balance.negative"}], known_answers={"card_number": "60890123456"})
        s.last_transcript = "Tom Hughes 8 Jan 1990"
        await o.answer(s, {"full_name": "Tom Hughes", "dob": "1990-01-08"})
        s.last_transcript = "5 Gloucester Road, Bristol"
        r = await o.answer(s, {"address": "5 Gloucester Road, Bristol"})
        assert r["ask"]["invalid"]["address"]["kind"] == "incomplete"
        s.last_transcript = "B S 7, 8 A A"
        r = await o.answer(s, {"address": "BS7 8AA"})
        assert r["ask"]["fields"][0]["name"] == "security_answer"
    asyncio.run(_run(services, t2))


def test_dob_in_any_natural_form_verifies(services):
    async def t(o, s):
        s.last_transcript = "card 12345678901"
        await o.route(s, [{"journey": "rewards.balance.view"}], known_answers={"card_number": "12345678901"})
        s.last_transcript = "Priya Sharma, twelve oh four eighty eight"
        r = await o.answer(s, {"full_name": "Priya Sharma", "dob": "twelve oh four eighty eight"})
        assert any(f["kind"] == "balance_details" for f in r["facts"])
    asyncio.run(_run(services, t))


def test_dispositions(services):
    from services.agent_runtime import outcomes

    async def automated(o, s):
        s.last_transcript = "card 12345678901"
        await o.route(s, [{"journey": "rewards.balance.view"}], known_answers={"card_number": "12345678901"})
        s.last_transcript = "Priya Sharma 12/04/1988"
        await o.answer(s, {"full_name": "Priya Sharma", "dob": "1988-04-12"})
        assert outcomes.disposition(s) == "automated"
        s.last_transcript = "can I speak to someone about my account"
        await o.transfer(s, "Customer wants to discuss their account with a person")
        assert outcomes.disposition(s) == "partial_automation"
    asyncio.run(_run(services, automated))

    async def deflected(o, s):
        await o.route(s, [{"journey": "knowledge.ask", "query": "how long do bonus points take"}])
        assert outcomes.disposition(s) == "deflected"
        s.last_transcript = "I'd still like to talk to someone"
        await o.transfer(s, "Customer still wants to talk to someone")
        assert outcomes.disposition(s) == "partial_deflection"
    asyncio.run(_run(services, deflected))

    async def failed(o, s):
        s.last_transcript = "card 12345678901"
        await o.route(s, [{"journey": "rewards.balance.view"}], known_answers={"card_number": "12345678901"})
        for _ in range(2):
            s.last_transcript = "Priya Sharma 1 January 1990"
            r = await o.answer(s, {"full_name": "Priya Sharma", "dob": "1990-01-01"})
        assert any(f["kind"] == "refuse_disclosure" for f in r["facts"])
        assert not any(f["kind"] == "balance_details" for f in r["facts"])   # nothing disclosed
        assert outcomes.disposition(s) == "failed_idv"
    asyncio.run(_run(services, failed))

    async def abandoned(o, s):
        s.last_transcript = "where's my order"
        await o.route(s, [{"journey": "online.delivery_status"}])
        out = await o.log_outcome(s, ended_by="caller_hung_up")
        assert out["disposition"] == "abandoned" and out["ended_by"] == "caller_hung_up"
        assert (await o.log_outcome(s, ended_by="again")) is out                # logged once
    asyncio.run(_run(services, abandoned))

    async def transferred(o, s):
        s.last_transcript = "I lost my card"
        await o.route(s, [{"journey": "rewards.balance.view"}])
        r = await o.answer(s, {"card_lost": True})
        assert r["transferred"] and outcomes.disposition(s) == "transferred_to_advisor"
        assert s.transfer["queue"] == "lost_stolen"
    asyncio.run(_run(services, transferred))


def test_customer_who_got_their_answer_is_not_abandoned(services):
    """Delivery status told, then 'that's all' before the SOP's closing question: served, so automated."""
    from services.agent_runtime import outcomes

    async def t(o, s):
        s.last_transcript = "where's order 7004128855"
        await o.route(s, [{"journey": "online.delivery_status"}], known_answers={"order_number": "7004128855"})
        s.last_transcript = "yes"
        await o.answer(s, {"order_number_correct": True})
        s.last_transcript = "SE1 7PB"
        r = await o.answer(s, {"postcode": "SE1 7PB"})
        assert r["ask"]["fields"][0]["name"] == "items_issue"            # the SOP's follow-up is still open
        assert (await o.log_outcome(s, ended_by="assistant"))["disposition"] == "automated"
    asyncio.run(_run(services, t, caller="07700900111"))


def test_backend_outage_is_a_system_issue_not_a_wrong_answer(services):
    """CRM down while locating the card: no 'account not found', no retry used, the next try works."""
    async def t(o, s):
        await httpx.AsyncClient().post(services["backend"] + "/admin/fault",
                                       json={"path_prefix": "/crm/accounts/locate", "mode": "error", "count": 1})
        s.last_transcript = "what's my balance"
        await o.route(s, [{"journey": "rewards.balance.view"}])
        s.last_transcript = "12345678901"
        r = await o.answer(s, {"card_number": "12345678901"})
        assert any(f["kind"] == "system_issue" for f in r["facts"])
        assert not any(f["kind"] in ("card_not_found_retry", "account_not_found") for f in r["facts"])
        # the customer tries again: a fresh journey, the system has recovered
        s.last_transcript = "can you try again, what's my balance"
        r = await o.route(s, [{"journey": "rewards.balance.view"}], known_answers={"card_number": "12345678901"})
        assert [f["name"] for f in r["ask"]["fields"]] == ["full_name", "dob"]
    asyncio.run(_run(services, t))


def test_backend_timeout_is_a_system_issue(services):
    async def t(o, s):
        await httpx.AsyncClient().post(services["backend"] + "/admin/fault",
                                       json={"path_prefix": "/online", "mode": "timeout", "count": 1})
        s.last_transcript = "where's order 7004128855"
        await o.route(s, [{"journey": "online.delivery_status"}], known_answers={"order_number": "7004128855"})
        s.last_transcript = "yes"
        r = await o.answer(s, {"order_number_correct": True})
        assert any(f["kind"] == "system_issue" for f in r["facts"])
        await httpx.AsyncClient().post(services["backend"] + "/admin/fault", json={"path_prefix": ""})
    asyncio.run(_run(services, t, caller="07700900111"))


def test_subagents_are_isolated_subagents(services):
    async def t(o, s):
        s.last_transcript = "where is my online order"
        r = await o.route(s, [{"journey": "online.delivery_status"}])
        assert r["ask"]["subagent"] == "ID&V Agent" and "FreshCart Online order numbers" in r["ask"]["internal_notes"]
        assert "Something missing" not in r["ask"]["internal_notes"]           # Online subagent's notes stay with the Online subagent
        assert [x.journey.id for x in s.subagents["idv"].runs] == ["idv.online_order"]
        assert [x.journey.id for x in s.subagents["online"].runs] == ["online.delivery_status"]
        assert s.subagents["online"].toolbox.scopes == ("order_service", "case_management", "knowledge_base")
    asyncio.run(_run(services, t))


def test_knowledge_subagent_uses_vector_rag(services):
    async def t(o, s):
        s.last_transcript = "can I turn my points into air miles"
        r = await o.route(s, [{"journey": "knowledge.ask", "query": "can I turn my points into air miles"}])
        docs = next(f for f in r["facts"] if f["kind"] == "knowledge_answer")["facts"]["documents"]
        assert docs[0]["doc_id"] == "KB-AEROMILES" and docs[0]["score"] > 0.3
    asyncio.run(_run(services, t))


def test_cascade_session_config_and_tools():
    cfg = session_config(settings.voice, instructions(), tool_definitions())
    sess = cfg["session"]
    assert cfg["type"] == "session.update" and sess["turn_detection"]["type"] == "silero_vad"
    assert sess["voice"] == settings.voice.voice_name and sess["modalities"] == ["text", "audio"]
    assert sess["turn_detection"]["create_response"] is (not settings.voice.guard_before_response)
    names = {t["name"] for t in sess["tools"]}
    assert names == {"route_request", "submit_answer", "transfer_to_colleague", "end_call"}
    enum = sess["tools"][0]["parameters"]["properties"]["journeys"]["items"]["properties"]["journey"]["enum"]
    assert "online.refund_query" in enum and not any(j.startswith("idv.") for j in enum)
    assert "{journey_catalogue}" not in sess["instructions"]


def test_transfer_needs_the_customers_consent(services):
    """Found live: 'Okay, thanks' after a failed identity check was treated as a request for a colleague."""
    async def t(o, s):
        s.add_history("assistant", "I'm sorry, I can't share the account information right now.")
        s.last_transcript = "Okay, thanks."
        r = await o.transfer(s, "identity failed")
        assert "error" in r and not s.transferred
        s.add_history("assistant", "Would you like me to put you through to a colleague who can help?")
        s.last_transcript = "Yes please."
        r = await o.transfer(s, "identity failed; customer accepted a colleague")
        assert r["transferred"] is True
    asyncio.run(_run(services, t))

    async def asked(o, s):
        s.last_transcript = "Can I just speak to a real person?"
        assert (await o.transfer(s, "asked for a person"))["transferred"] is True
    asyncio.run(_run(services, asked))


def test_routing_to_escalation_needs_consent_too(services):
    """Found live: 'I'd rather not give you my card number' was routed straight to the escalation journey."""
    async def t(o, s):
        s.last_transcript = "I'd rather not give you my card number."
        r = await o.route(s, [{"journey": "escalation.human", "query": "customer won't give card"}])
        assert "error" in r and not s.transferred
        s.last_transcript = "Can I talk to a person then?"
        r = await o.route(s, [{"journey": "escalation.human", "query": "customer asked for a person"}])
        assert r["transferred"] is True
    asyncio.run(_run(services, t))


def test_idv_outage_is_not_a_failed_identity_check(services):
    """Found live: CRM down during ID&V released the waiting balance journey into 'we couldn't verify you'."""
    async def t(o, s):
        await httpx.AsyncClient().post(services["backend"] + "/admin/fault",
                                       json={"path_prefix": "/crm/accounts/locate", "mode": "error", "count": 1})
        s.last_transcript = "what's my balance"
        await o.route(s, [{"journey": "rewards.balance.view"}])
        s.last_transcript = "12345678901"
        r = await o.answer(s, {"card_number": "12345678901"})
        kinds = [f["kind"] for f in r["facts"]]
        assert "system_issue" in kinds and "refuse_disclosure" not in kinds
        view = next(x for x in s.runs if x.journey.id == "rewards.balance.view")
        assert view.status == "error" and view.outcome is None
    asyncio.run(_run(services, t))


def test_month_in_the_routing_sentence_prefills_history(services):
    """Even if the coordinator forgets known_answers, 'in March' in the customer's words fills the date."""
    async def t(o, s):
        s.last_transcript = "what's my balance and what did I spend my points on in March, card 12345678901"
        await o.route(s, [{"journey": "rewards.balance.view"}, {"journey": "rewards.history"}])
        s.last_transcript = "Priya Sharma, twelfth of April 1988"
        r = await o.answer(s, {"full_name": "Priya Sharma", "dob": "1988-04-12"})
        assert {"balance_details", "transaction_review"} <= {f["kind"] for f in r["facts"]} and r["ask"] is None
    asyncio.run(_run(services, t))
