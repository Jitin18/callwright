"""
MCP gateway — exposes the enterprise backend as MCP tools, split into SCOPES.

Each scope is a separate MCP server on its own HTTP path. A subagent is only
given the URLs of the scopes it needs, so least privilege is enforced by the
network boundary, not by prompt instructions:

    /member_identity/mcp     FreshCart Rewards identity & verification (ID&V)
    /loyalty_accounts/mcp    balance, transactions, vouchers, account actions
    /member_enrolment/mcp    new member registration, app/web links
    /case_management/mcp     TaskDesk tasks, fraud cases, warm transfer
    /knowledge_base/mcp      Knowledge Base search and documents (RAG)
    /order_verification/mcp  FreshCart Online order verification (IDV 1–3)
    /order_service/mcp       FreshCart Online order status + refund SMS

`log_contact_outcome` is registered on every scope because every journey ends by
logging to CRM.

Run:  uvicorn services.mcp_gateway.server:app --port 8200
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, Literal, Optional

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8100")
_http = httpx.AsyncClient(base_url=BACKEND, timeout=float(os.getenv("BACKEND_TIMEOUT_S", "6")))


async def _call(method: str, path: str, **kw) -> dict[str, Any]:
    try:
        r = await _http.request(method, path, **kw)
    except httpx.TimeoutException:
        return {"error": True, "status_code": 504, "detail": "backend timed out"}
    except httpx.HTTPError as e:
        return {"error": True, "status_code": 502, "detail": f"backend unreachable: {type(e).__name__}"}
    if r.status_code >= 400:
        # Surface backend business errors to the caller as structured data, not exceptions,
        # so the journey engine can branch on them deterministically.
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text
        return {"error": True, "status_code": r.status_code, "detail": detail}
    return r.json()


def _add_common(mcp: FastMCP) -> None:
    @mcp.tool
    async def log_contact_outcome(session_id: str, journey: str, outcome: str,
                                  account_id: Optional[str] = None, note: Optional[str] = None,
                                  disposition: Optional[str] = None, transfer_reason: Optional[str] = None,
                                  journeys: Optional[list[dict]] = None, ended_by: Optional[str] = None) -> dict:
        """Log the contact outcome / note history on the member record in CRM (with the call disposition)."""
        return await _call("POST", "/crm/contacts", json={
            "session_id": session_id, "journey": journey, "outcome": outcome, "account_id": account_id,
            "note": note, "disposition": disposition, "transfer_reason": transfer_reason, "journeys": journeys,
            "ended_by": ended_by})


# ------------------------------------------------------------------ member_identity
member_identity = FastMCP("member_identity", instructions="Identity and verification tools for FreshCart Rewards members.")
_add_common(member_identity)


@member_identity.tool
async def locate_account(card_number: str) -> dict:
    """Locate a FreshCart Rewards account on CRM by card number (full 19 digits or last 11)."""
    return await _call("GET", "/crm/accounts/locate", params={"card_number": card_number})


@member_identity.tool
async def verify_identity(account_id: str, level: Literal["standard", "enhanced"], full_name: str, dob: str,
                          address: Optional[str] = None, security_answer: Optional[str] = None,
                          memorable_word: Optional[str] = None) -> dict:
    """Verify caller identity against the CRM record. Returns per-field match results only."""
    return await _call("POST", "/crm/idv/verify", json={
        "account_id": account_id, "level": level, "full_name": full_name, "dob": dob, "address": address,
        "security_answer": security_answer, "memorable_word": memorable_word})


@member_identity.tool
async def get_security_question(account_id: str, full_name: Optional[str] = None) -> dict:
    """Get the security QUESTION (never the answer) for the caller on this account."""
    params = {"account_id": account_id}
    if full_name:
        params["full_name"] = full_name
    return await _call("GET", "/crm/idv/security-question", params=params)


# ------------------------------------------------------------------ loyalty_accounts
loyalty_accounts = FastMCP("loyalty_accounts", instructions="FreshCart Rewards account data and account actions.")
_add_common(loyalty_accounts)


@loyalty_accounts.tool
async def get_balance(account_id: str) -> dict:
    """Reveal points balance, card last 4, account status and type."""
    return await _call("GET", f"/crm/accounts/{account_id}/balance")


@loyalty_accounts.tool
async def get_holders(account_id: str) -> dict:
    """List account holders and their roles (primary/secondary)."""
    return await _call("GET", f"/crm/accounts/{account_id}/holders")


@loyalty_accounts.tool
async def get_transactions(account_id: str, date_from: Optional[str] = None, date_to: Optional[str] = None,
                           days: int = 120) -> dict:
    """Transaction details for a date range after 31/12/2023 (ISO dates)."""
    params: dict[str, Any] = {"days": days}
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    return await _call("GET", f"/crm/accounts/{account_id}/transactions", params=params)


@loyalty_accounts.tool
async def get_archive_transactions(account_id: str) -> dict:
    """'View all previous transactions' — historical Jan 2015 to Dec 2023."""
    return await _call("GET", f"/crm/accounts/{account_id}/transactions/archive")


@loyalty_accounts.tool
async def get_voucher(code: str) -> dict:
    """Open redemption details for a voucher code."""
    return await _call("GET", f"/crm/vouchers/{code}")


@loyalty_accounts.tool
async def get_supplier_booking(ref: str) -> dict:
    """Open redemption details for a supplier booking reference."""
    return await _call("GET", f"/crm/supplier-bookings/{ref}")


@loyalty_accounts.tool
async def get_store(store_id: str) -> dict:
    """Store directory lookup (name, town, region)."""
    return await _call("GET", f"/crm/stores/{store_id}")


@loyalty_accounts.tool
async def set_reason_code(account_id: str, reason_code: Literal["CANNOT_REDEEM"], txn_id: Optional[str] = None,
                          note: Optional[str] = None) -> dict:
    """Change the account reason code (e.g. CANNOT_REDEEM to protect the account)."""
    return await _call("POST", f"/crm/accounts/{account_id}/reason-code",
                       json={"reason_code": reason_code, "txn_id": txn_id, "note": note})


@loyalty_accounts.tool
async def deactivate_account(account_id: str, reason: str) -> dict:
    """Deactivate the account and trigger notification to the primary holder."""
    return await _call("POST", f"/crm/accounts/{account_id}/deactivate", json={"reason": reason})


# ------------------------------------------------------------------ member_enrolment
member_enrolment = FastMCP("member_enrolment", instructions="New FreshCart Rewards member registration.")
_add_common(member_enrolment)


@member_enrolment.tool
async def create_member(full_name: str, dob: str, address: str, terms_accepted: bool,
                        email: Optional[str] = None, mobile: Optional[str] = None,
                        marketing_opt_in: bool = False) -> dict:
    """Save a new member in CRM. CRM auto-generates and dispatches the card pack."""
    return await _call("POST", "/crm/members", json={
        "full_name": full_name, "dob": dob, "address": address, "email": email, "mobile": mobile,
        "marketing_opt_in": marketing_opt_in, "terms_accepted": terms_accepted})


@member_enrolment.tool
async def send_link(channel: Literal["sms", "email"], destination: str,
                    link_type: Literal["rewards_app_download", "rewards_web_register"]) -> dict:
    """Send the customer a link to register digitally."""
    return await _call("POST", "/comms/send-link", json={"channel": channel, "destination": destination,
                                                         "link_type": link_type})


# ------------------------------------------------------------------ case_management
case_management = FastMCP("case_management", instructions="TaskDesk tasks, fraud cases and warm transfers.")
_add_common(case_management)


@case_management.tool
async def create_task(account_id: str, task_type: Literal["ARCHIVE_REQUEST", "PRODUCT_SPECIALIST_REVIEW"],
                      summary: str, txn_id: Optional[str] = None) -> dict:
    """Raise a task on TaskDesk for a specialist team."""
    return await _call("POST", "/taskdesk/tasks", json={"account_id": account_id, "task_type": task_type,
                                                   "summary": summary, "txn_id": txn_id})


@case_management.tool
async def create_fraud_case(account_id: str, summary: str, txn_id: Optional[str] = None,
                            points_disputed: Optional[int] = None) -> dict:
    """Raise a fraud case via TaskDesk."""
    return await _call("POST", "/taskdesk/fraud-cases", json={"account_id": account_id, "summary": summary,
                                                          "txn_id": txn_id, "points_disputed": points_disputed})


@case_management.tool
async def warm_transfer(session_id: str, queue: Literal["fraud", "general", "lost_stolen", "online_delivery"],
                        summary: str, handover: Optional[dict] = None) -> dict:
    """Attempt a warm transfer of the live call to a human queue. `handover` is the colleague's screen-pop:
    reason, call summary and the redacted transcript."""
    return await _call("POST", "/telephony/warm-transfer", json={"session_id": session_id, "queue": queue,
                                                                 "summary": summary, "handover": handover})


# ------------------------------------------------------------------ knowledge_base
knowledge_base = FastMCP("knowledge_base", instructions="Knowledge Base articles and policy documents (RAG).")
_add_common(knowledge_base)


@knowledge_base.tool
async def search_knowledge(query: str, limit: int = 3) -> dict:
    """Search the knowledge base."""
    return await _call("GET", "/kb/search", params={"q": query, "limit": limit})


@knowledge_base.tool
async def get_document(doc_id: str) -> dict:
    """Fetch a knowledge base document by id."""
    return await _call("GET", f"/kb/documents/{doc_id}")


# ------------------------------------------------------------------ order_verification
order_verification = FastMCP("order_verification", instructions="FreshCart Online order verification: order number, phone, postcode.")
_add_common(order_verification)


@order_verification.tool
async def validate_order(order_number: str) -> dict:
    """IDV 1 — validate a Online order number with Order System. Returns {valid, order_ref}."""
    return await _call("POST", "/online/idv/validate-order", json={"order_number": order_number})


@order_verification.tool
async def check_caller_number(order_ref: str, caller_number: Optional[str] = None) -> dict:
    """Does the calling number match the phone on the order? Returns {matches} only."""
    return await _call("POST", "/online/idv/caller-check", json={"order_ref": order_ref, "caller_number": caller_number})


@order_verification.tool
async def verify_phone(order_ref: str, phone: str) -> dict:
    """IDV 2 — phone number on the order. Returns {match} only."""
    return await _call("POST", "/online/idv/verify-phone", json={"order_ref": order_ref, "phone": phone})


@order_verification.tool
async def verify_postcode(order_ref: str, postcode: str) -> dict:
    """IDV 3 — delivery postcode on the order. Returns {match} only."""
    return await _call("POST", "/online/idv/verify-postcode", json={"order_ref": order_ref, "postcode": postcode})


# ------------------------------------------------------------------ order_service
order_service = FastMCP("order_service", instructions="FreshCart Online order status and refund communications.")
_add_common(order_service)


@order_service.tool
async def get_order_status(order_ref: str) -> dict:
    """Delivery status for a verified order (cancelled / not_delivered / delivered, slot, dates)."""
    return await _call("GET", f"/online/orders/{order_ref}/status")


@order_service.tool
async def send_refund_link(order_ref: str) -> dict:
    """Send the online-refund link by SMS to the phone on the order (Message Broker)."""
    return await _call("POST", "/online/comms/refund-link", json={"order_ref": order_ref})


# ------------------------------------------------------------------ app
SCOPES = {server.name: server for server in (
    member_identity, loyalty_accounts, member_enrolment, case_management, knowledge_base,
    order_verification, order_service)}
_apps = {name: server.http_app(path="/mcp") for name, server in SCOPES.items()}


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with contextlib.AsyncExitStack() as stack:
        for sub in _apps.values():
            await stack.enter_async_context(sub.lifespan(sub))
        yield


async def health(_request):
    return JSONResponse({"ok": True, "scopes": list(SCOPES)})


app = Starlette(routes=[Route("/health", health)] + [Mount(f"/{n}", app=a) for n, a in _apps.items()],
                lifespan=lifespan)
