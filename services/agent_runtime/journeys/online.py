"""
FreshCart Online (Online) journeys — from the client's "Refund Query: Customer-IVR journey" board
("AS IS Process — Online IVR"). The IVR's keypad menus become natural conversation; the
decisions, verification factors, messages and systems stay exactly as the SOP defines them.

    idv.online_order          IDV 1 order number (+ read-back) → IDV 2 phone (only if the calling
                           number differs from the order) → IDV 3 postcode
    online.delivery_status    "Delivery Status" lane: cancelled / not delivered / delivered messages,
                           "Does customer need to speak to agent?" → transfer or end call
    online.refund_query       "Technical Help" lane: deflection message → "Does customer need link?"
                           → Refund SMS (Message Broker) → "Anything else?"

Systems: order validation/status = Order System · refund SMS = Message Broker → SMS Gateway ·
human agent = ContactHub (delivery team).
"""
from __future__ import annotations

from ..config import settings
from .dsl import Ask, Call, Decide, Effect, End, Journey, Reset, Slot, Tell
from .engine import idv_satisfied
from .slots import ORDER_NUMBER, PHONE, POSTCODE

ONLINE_QUEUE = "online_delivery"  # ContactHub "Online Human Agent (delivery team)"


def _retry_rule(result_key: str, ok_field: str, counter: str):
    """Yes → 'yes'; first failure → 'retry' (After Attempt 1); second → 'no' (After Attempt 2)."""
    def rule(c):
        if c.data.get(result_key, {}).get(ok_field):
            return "yes"
        return "retry" if c.count(counter) < settings.idv_max_retries else "no"
    return rule


def _spaced(number: str | None) -> str:
    return " ".join(number or "")


def _transfer_nodes(prefix: str, summary):
    """'Transfer to Online human Agent' → Call Transfer → Receive Call (ContactHub)."""
    return [
        Call(f"{prefix}transfer", "Transfer to Online human Agent", "system", scope="case_management", tool="warm_transfer",
             args=lambda c: {"session_id": c.session_id, "queue": ONLINE_QUEUE, "summary": summary(c)},
             save_as="transfer", next=f"{prefix}tell_transfer"),
        Tell(f"{prefix}tell_transfer", "Call Transfer", "agent", kind="online_transfer_to_agent",
             facts=lambda c: {"connected": c.data["transfer"].get("connected"),
                              "estimated_wait_seconds": c.data["transfer"].get("estimated_wait_seconds")},
             next=f"{prefix}end_transfer"),
        End(f"{prefix}end_transfer", "Receive Call (Online Human Agent)", "system", outcome="transferred_to_online_agent",
            status="handoff"),
    ]


# ------------------------------------------------------------------ IDV 1–3
ONLINE_IDV = Journey(
    id="idv.online_order", title="Online order verification", section="Verification", subagent="idv", start="ask_order",
    source="Online IVR: messages 4–7, 'IDV process' lane",
    description="Order-based verification for FreshCart Online. Started automatically — never route to this.",
    nodes=[
        Ask("ask_order", "Ask Order Number", "customer", slots=[ORDER_NUMBER],
            purpose="Ask for their FreshCart Online order number, which is on their confirmation email.",
            next="confirm_order"),
        Ask("confirm_order", "Ask about correctness of Order Number", "customer",
            slots=[Slot("order_number_correct", "yes_no", "True if the customer confirms the order number read back "
                                                           "to them is correct; false if they say it's wrong.")],
            purpose="Read the order number back to them, digit by digit, and check you've got it right.",
            context=lambda c: {"order_number_to_read_back": _spaced(c.slot("order_number"))}, next="correct"),
        Decide("correct", "Is Order Number Correct?", "agent",
               rule=lambda c: "yes" if c.slot("order_number_correct") else
               ("retry" if c.count("entry") < settings.idv_max_retries else "no"),
               branches={"yes": "validate", "retry": "reenter", "no": "transfer"}),
        Reset("reenter", "Re-enter order number (max 2 attempts)", "agent",
              slots=["order_number", "order_number_correct"], counter="entry", next="ask_order"),
        Call("validate", "Validate Order Number (IDV 1)", "crm", scope="order_verification", tool="validate_order",
             args=lambda c: {"order_number": c.slot("order_number")}, save_as="order_check", next="valid"),
        Decide("valid", "Is Order Number Valid?", "agent", rule=_retry_rule("order_check", "valid", "order"),
               branches={"yes": "record_order", "retry": "retry_order", "no": "transfer"}),
        Reset("retry_order", "After Attempt 1", "agent", slots=["order_number", "order_number_correct"],
              counter="order", next="tell_invalid_order"),
        Tell("tell_invalid_order", "Order number not found", "agent", kind="online_order_not_found_retry",
             facts=lambda c: {"attempts_remaining": 1}, next="ask_order"),
        Effect("record_order", "Order located in Order System", "crm",
               apply=lambda c: c.idv.update(online_order_ref=c.data["order_check"]["order_ref"]), next="caller_check"),
        Call("caller_check", "Compare calling number with order", "crm", scope="order_verification", tool="check_caller_number",
             args=lambda c: {"order_ref": c.idv["online_order_ref"], "caller_number": c.caller_number},
             save_as="caller", next="phone_needed"),
        Decide("phone_needed", "Calling number different from number used in order?", "agent",
               rule=lambda c: "no" if c.data["caller"].get("matches") else "yes",
               branches={"yes": "ask_phone", "no": "ask_postcode"}),
        Ask("ask_phone", "Ask Phone Number", "customer", slots=[PHONE],
            purpose="Explain the number they're calling from doesn't match the order, and ask for the phone number "
                    "on the order.", next="verify_phone"),
        Call("verify_phone", "Validate Phone Number (IDV 2)", "crm", scope="order_verification", tool="verify_phone",
             args=lambda c: {"order_ref": c.idv["online_order_ref"], "phone": c.slot("phone")},
             save_as="phone_check", next="phone_ok"),
        Decide("phone_ok", "Is Phone Number Correct?", "agent", rule=_retry_rule("phone_check", "match", "phone"),
               branches={"yes": "ask_postcode", "retry": "retry_phone", "no": "transfer"}),
        Reset("retry_phone", "Phone did not match: ask again", "agent", slots=["phone"], counter="phone",
              next="tell_phone_retry"),
        Tell("tell_phone_retry", "Explain details didn't match", "agent", kind="idv_mismatch_retry",
             facts=lambda c: {"attempts_remaining": 1}, next="ask_phone"),
        Ask("ask_postcode", "Ask Postcode", "customer", slots=[POSTCODE],
            purpose="Ask for the delivery postcode on the order.", next="verify_postcode"),
        Call("verify_postcode", "Validate Postcode (IDV 3)", "crm", scope="order_verification", tool="verify_postcode",
             args=lambda c: {"order_ref": c.idv["online_order_ref"], "postcode": c.slot("postcode")},
             save_as="postcode_check", next="postcode_ok"),
        Decide("postcode_ok", "Is Postcode Correct?", "agent", rule=_retry_rule("postcode_check", "match", "postcode"),
               branches={"yes": "record_pass", "retry": "retry_postcode", "no": "transfer"}),
        Reset("retry_postcode", "Postcode did not match: ask again", "agent", slots=["postcode"], counter="postcode",
              next="tell_postcode_retry"),
        Tell("tell_postcode_retry", "Explain details didn't match", "agent", kind="idv_mismatch_retry",
             facts=lambda c: {"attempts_remaining": 1}, next="ask_postcode"),
        Effect("record_pass", "Order verified (IDV 1–3)", "system",
               apply=lambda c: c.idv.update(online_verified=True), next="end_pass"),
        End("end_pass", "Verification complete", "system", outcome="online_idv_passed", log=False),
        *_transfer_nodes("", lambda c: "Caller could not complete Online order verification in the assistant."),
    ],
)


def _gate(yes: str):
    return Decide("gate_idv", "Is order verification (IDV 1–3) complete?", "agent",
                  rule=lambda c: "yes" if idv_satisfied(c, "online_order") else "no",
                  branches={"yes": yes, "no": "refuse"})


def _refuse():
    return [
        Tell("refuse", "Cannot share order details", "agent", kind="refuse_disclosure",
             facts=lambda c: {"reason": "order could not be verified", "next": "offer_colleague_only_if_they_want"}, next="end_failed"),
        End("end_failed", "End (not verified)", "system", outcome="online_refused_disclosure", status="failed"),
    ]


def _status_facts(c) -> dict:
    return {k: v for k, v in c.data["status"].items() if k != "order_ref"}


# ------------------------------------------------------------------ Delivery status
DELIVERY_STATUS = Journey(
    id="online.delivery_status", title="Online delivery status", section="FreshCart Online", subagent="online",
    requires_idv="online_order", start="gate_idv", source="Online IVR: 'Delivery Status' lane, message 10",
    description="FreshCart Online customer asking where their online grocery order is, when it will arrive, its "
                "delivery slot, or whether it was cancelled or delivered.",
    nodes=[
        _gate("get_status"),
        Call("get_status", "IVR shares Delivery Status information", "crm", scope="order_service", tool="get_order_status",
             args=lambda c: {"order_ref": c.idv["online_order_ref"]}, save_as="status", next="status_kind"),
        Decide("status_kind", "Delivery status?", "agent", rule=lambda c: c.data["status"].get("status", "unknown"),
               branches={"cancelled": "tell_cancelled", "not_delivered": "tell_due", "delivered": "tell_delivered",
                         "unknown": "transfer"}),
        Tell("tell_cancelled", "Play the Delivery Status message: Cancelled", "agent",
             kind="delivery_status_cancelled", facts=_status_facts, next="ask_agent"),
        Tell("tell_due", "Play the Delivery Status message: Not Delivered", "agent",
             kind="delivery_status_due", facts=_status_facts, next="ask_agent"),
        Tell("tell_delivered", "Play the Delivery Status message: Delivered", "agent",
             kind="delivery_status_delivered", facts=_status_facts, next="ask_items"),
        Ask("ask_items", "Calling about an issue with the items?", "customer",
            slots=[Slot("items_issue", "yes_no", "True if the customer is calling about a problem with items in the "
                                                 "order (missing, damaged, wrong, substituted, refund).")],
            purpose="Ask whether they're calling about an issue with the items in the order.", next="items"),
        Decide("items", "Issue with the items?", "agent", rule=lambda c: "yes" if c.slot("items_issue") else "no",
               branches={"yes": "end_to_refund", "no": "ask_agent"}),
        End("end_to_refund", "Continue to Refund Query", "system", outcome="items_issue_to_refund_query",
            chain="online.refund_query"),
        Ask("ask_agent", "Does Customer need to speak to Agent?", "customer",
            slots=[Slot("wants_agent", "yes_no", "True if the customer wants to talk to someone further about this "
                                                 "order; false if they're happy with the update.")],
            purpose="Ask whether they'd like to talk to a colleague further about this order.", next="agent"),
        Decide("agent", "Does Customer need to speak to Agent?", "agent",
               rule=lambda c: "yes" if c.slot("wants_agent") else "no",
               branches={"yes": "transfer", "no": "end_call"}),
        End("end_call", "End Call", "system", outcome="delivery_status_given", end_call=True),
        *_transfer_nodes("", lambda c: f"Verified Online customer, order status "
                                       f"{c.data.get('status', {}).get('status')}; wants to discuss the order."),
        *_refuse(),
    ],
)


# ------------------------------------------------------------------ Refund query
REFUND_QUERY = Journey(
    id="online.refund_query", title="Online refund query", section="FreshCart Online", subagent="online",
    requires_idv="online_order", start="gate_idv", source="Online IVR: 'Technical Help' lane (Refund Query), messages 1 & 9",
    description="FreshCart Online customer with a problem with items in an online grocery order (missing, damaged, "
                "wrong or substituted items) or wanting a refund for an online order.",
    nodes=[
        _gate("deflect_doc"),
        Call("deflect_doc", "Choose Refund option", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-ONLINE-REFUND-SELFSERVE"}, save_as="deflection", next="tell_deflect"),
        Tell("tell_deflect", "Play Deflectional Message", "agent", kind="refund_online_deflection",
             facts=lambda c: {"policy": c.data["deflection"].get("body")}, next="ask_link"),
        Ask("ask_link", "Ask customer whether they need Link to refund via Chat", "customer",
            slots=[Slot("wants_link", "yes_no", "True if the customer wants the refund link sent by text message."),
                   Slot("wants_agent", "yes_no", "True ONLY if the customer says they'd rather speak to a colleague.",
                        required=False)],
            purpose="Offer to text them a link to request the refund online, or connect them to a colleague if "
                    "they'd rather speak to someone.",
            escapes={"wants_agent": "transfer"}, next="need_link"),
        Decide("need_link", "Does Customer need Link?", "agent", rule=lambda c: "yes" if c.slot("wants_link") else "no",
               branches={"yes": "link_doc", "no": "ask_other"}),
        Call("link_doc", "Refund SMS template", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-ONLINE-REFUND-LINK"}, save_as="link_policy", next="send_link"),
        Call("send_link", "Send Link via SMS", "system", scope="order_service", tool="send_refund_link",
             args=lambda c: {"order_ref": c.idv["online_order_ref"]}, save_as="sms", next="tell_sent"),
        Tell("tell_sent", "SMS with Link", "agent", kind="refund_link_sent",
             facts=lambda c: {"status": c.data["sms"].get("status"), "sent_to_last4": c.data["sms"].get("sent_to_last4"),
                              "policy": c.data["link_policy"].get("body")}, next="ask_other"),
        Ask("ask_other", "Ask customer if they need any other help", "customer",
            slots=[Slot("anything_else", "yes_no", "True if the customer needs help with anything else; false if not.")],
            purpose="Ask whether there's anything else they need help with today.", next="other"),
        Decide("other", "Anything Else?", "agent", rule=lambda c: "yes" if c.slot("anything_else") else "no",
               branches={"yes": "end_more", "no": "end_call"}),
        End("end_more", "Back to menu (anything else)", "system", outcome="refund_query_more_help"),
        End("end_call", "End Call", "system", outcome="refund_query_resolved", end_call=True),
        *_transfer_nodes("", lambda c: "Verified Online customer with an item issue; prefers to speak to a colleague "
                                       "about a refund."),
        *_refuse(),
    ],
)
