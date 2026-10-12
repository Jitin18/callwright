"""
FreshCart Rewards › Register a new card › Journey 1: Customer Sign-Up Flow – Digital and Analogue.
No ID&V: the caller is not yet a member.
"""
from __future__ import annotations

from .dsl import Ask, Call, Choice, Decide, Effect, End, Journey, Slot, Tell
from .slots import ADDRESS, DOB, EMAIL, FULL_NAME, MOBILE

ROUTES = [Choice("app", "Register themselves in the FreshCart app"),
          Choice("website", "Register themselves on the FreshCart Rewards website"),
          Choice("phone", "Register over the phone now with the assistant (physical card posted)")]


def _created_ok(c) -> str:
    r = c.data.get("member", {})
    if r.get("error") and r.get("status_code") == 409:
        return "duplicate"
    return "error" if r.get("error") else "ok"


SIGNUP = Journey(
    id="enrolment.signup", title="Customer sign-up: digital and analogue", section="Register a new card",
    subagent="enrolment", start="ask_route", source="FreshCart Rewards › Register a new card › Journey 1",
    description="Caller wants to join FreshCart Rewards, register for a new FreshCart Rewards card, or sign up as a new member.",
    nodes=[
        Ask("ask_route", "Customer: App, website or phone?", "customer",
            slots=[Slot("registration_route", "choice", "How the customer wants to register.",
                        choices=lambda c: ROUTES)],
            purpose="Offer the ways to join (app, website, or over the phone now) and ask which they prefer.",
            next="route"),
        Decide("route", "App or website?", "agent",
               rule=lambda c: "digital" if c.slot("registration_route") in ("app", "website") else "analogue",
               branches={"digital": "digital_doc", "analogue": "limits_doc"}),
        # ---- digital
        Call("digital_doc", "Digital: prompts for personal info", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-APP"}, save_as="policy", next="tell_digital"),
        Tell("tell_digital", "Explain digital registration steps", "agent", kind="digital_registration_steps",
             facts=lambda c: {"route": c.slot("registration_route"), "policy": c.data["policy"].get("body")},
             next="ask_link"),
        Ask("ask_link", "Offer a link by text", "customer",
            slots=[Slot("wants_link", "yes_no", "True if the customer wants a registration link sent by text message.")],
            purpose="Offer to text them a link to register.", next="link"),
        Decide("link", "Send link?", "agent", rule=lambda c: "yes" if c.slot("wants_link") else "no",
               branches={"yes": "ask_mobile_link", "no": "end_digital"}),
        Ask("ask_mobile_link", "Capture mobile for link", "customer",
            slots=[Slot("mobile", "mobile", "The UK mobile number to text the link to.", shared=True, sensitive=True)],
            purpose="Ask for the mobile number to text the link to.", next="send_link"),
        Call("send_link", "Send registration link", "system", scope="member_enrolment", tool="send_link",
             args=lambda c: {"channel": "sms", "destination": c.slot("mobile"),
                             "link_type": "rewards_app_download" if c.slot("registration_route") == "app"
                             else "rewards_web_register"}, save_as="sent", next="tell_sent"),
        Tell("tell_sent", "Confirm link sent", "agent", kind="link_sent",
             facts=lambda c: {"status": c.data["sent"].get("status")}, next="end_digital"),
        End("end_digital", "Colleague: Logs contact outcome in CRM → End digital journey", "system",
            outcome="digital_registration_guided"),
        # ---- analogue
        Call("limits_doc", "Colleague: Explains limitations", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-PHYS-LIMITS"}, save_as="limits", next="tell_limits"),
        Tell("tell_limits", "Colleague: Explains limitations", "agent", kind="phone_registration_limitations",
             facts=lambda c: {"policy": c.data["limits"].get("body")}, next="ask_proceed"),
        Ask("ask_proceed", "Customer: Confirms to proceed", "customer",
            slots=[Slot("confirm_proceed", "yes_no", "True if the customer wants to go ahead registering by phone.")],
            purpose="Ask whether they'd like to go ahead registering over the phone.", next="proceed"),
        Decide("proceed", "Confirms to proceed?", "agent", rule=lambda c: "yes" if c.slot("confirm_proceed") else "no",
               branches={"yes": "ask_details", "no": "end_declined"}),
        End("end_declined", "End (declined)", "system", outcome="declined_after_limitations"),
        Ask("ask_details", "Colleague: Opens CRM, prompts for details", "agent", slots=[FULL_NAME, DOB],
            purpose="Ask for their full name and date of birth for the new account.", next="ask_address"),
        Ask("ask_address", "Capture address", "agent", slots=[ADDRESS],
            purpose="Ask for their home address including postcode, where the card will be posted.", next="ask_contact"),
        Ask("ask_contact", "Customer has email/mobile?", "customer",
            slots=[Slot("has_contact", "yes_no", "True if the customer has and is willing to give an email or mobile.")],
            purpose="Ask whether they'd like to add an email address or mobile number to the account.",
            next="contact"),
        Decide("contact", "Customer has email/mobile?", "agent",
               rule=lambda c: "yes" if c.slot("has_contact") else "no",
               branches={"yes": "capture_contact", "no": "leave_blank"}),
        Ask("capture_contact", "Colleague: Captures email/mobile", "agent", slots=[EMAIL, MOBILE],
            purpose="Ask for their email address and/or mobile number.",
            satisfied=lambda c: bool(c.slot("email") or c.slot("mobile")), next="terms_doc"),
        Effect("leave_blank", "Colleague: Leaves blank and unticks", "agent",
               apply=lambda c: c.local.update(marketing_opt_in=False), next="terms_doc"),
        Call("terms_doc", "Colleague: Explains T&Cs", "system", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-TANDC"}, save_as="terms", next="tell_terms"),
        Tell("tell_terms", "Colleague: Explains T&Cs", "agent", kind="terms_and_conditions",
             facts=lambda c: {"policy": c.data["terms"].get("body")}, next="ask_consent"),
        Ask("ask_consent", "Gets consent", "customer",
            slots=[Slot("terms_consent", "yes_no", "True if the customer explicitly agrees to the terms and conditions.")],
            purpose="Ask whether they agree to the terms and conditions just explained.", next="consent"),
        Decide("consent", "Consent given?", "agent", rule=lambda c: "yes" if c.slot("terms_consent") else "no",
               branches={"yes": "marketing", "no": "end_no_consent"}),
        End("end_no_consent", "End (no consent)", "system", outcome="declined_terms"),
        Decide("marketing", "Contact details given?", "agent",
               rule=lambda c: "ask" if (c.slot("email") or c.slot("mobile")) else "skip",
               branches={"ask": "ask_marketing", "skip": "save"}),
        Ask("ask_marketing", "Marketing preference", "customer",
            slots=[Slot("marketing_opt_in", "yes_no", "True only if the customer opts in to FreshCart Rewards offers by "
                                                      "email/text.")],
            purpose="Ask whether they'd like to receive FreshCart Rewards offers by email or text.", next="save"),
        Call("save", "Colleague: Clicks save in CRM", "crm", scope="member_enrolment", tool="create_member",
             args=lambda c: {"full_name": c.slot("full_name"), "dob": c.slot("dob"), "address": c.slot("address"),
                             "email": c.slot("email"), "mobile": c.slot("mobile"),
                             "marketing_opt_in": bool(c.slot("marketing_opt_in")), "terms_accepted": True},
             save_as="member", next="created"),
        Decide("created", "Saved in CRM?", "crm", rule=_created_ok,
               branches={"ok": "activation_doc", "duplicate": "tell_duplicate", "error": "tell_error"}),
        Tell("tell_duplicate", "Existing account found", "agent", kind="duplicate_account",
             facts=lambda c: {"detail": c.data["member"].get("detail")}, next="end_duplicate"),
        End("end_duplicate", "End (existing member)", "system", outcome="duplicate_account"),
        Tell("tell_error", "Registration could not be saved", "agent", kind="system_error",
             facts=lambda c: {"detail": c.data["member"].get("detail")}, next="end_error"),
        End("end_error", "End (system error)", "system", outcome="registration_error", status="failed"),
        Call("activation_doc", "CRM: Auto-generates card pack", "crm", scope="knowledge_base", tool="get_document",
             args=lambda c: {"doc_id": "KB-ACTIVATE"}, save_as="activation", next="advise"),
        Tell("advise", "Colleague: Advises customer", "agent", kind="registration_complete",
             facts=lambda c: {"card_last4": c.data["member"].get("card_last4"),
                              "card_pack": c.data["member"].get("card_pack"),
                              "activation": c.data["activation"].get("body")}, next="end_analogue"),
        End("end_analogue", "Notes history + logs outcome → End analogue journey", "system",
            outcome="analogue_registration_complete",
            note=lambda c: f"New member {c.data['member'].get('account_id')} registered by phone; card pack generated."),
    ],
)
