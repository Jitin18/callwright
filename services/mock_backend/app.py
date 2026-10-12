"""
Mock enterprise backend: CRM (customer service platform), TaskDesk (tasks/cases),
Telephony (warm transfer), Comms (SMS/email links) and the Knowledge Base.

In production each of these is a separate FreshCart system. Here they share one
FastAPI process but keep separate route prefixes so the MCP gateway maps onto
them exactly as it would onto real services.

Run:  uvicorn services.mock_backend.app:app --port 8100
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import random
import re
import uuid
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import fixtures, rag

ARCHIVE_CUTOFF = date(2023, 12, 31)
ARCHIVE_START = date(2015, 1, 1)
POINT_VALUE_GBP = 0.005  # 200 points = £1

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Build (or load) the vector index before accepting requests, so the first search is fast.
    await asyncio.to_thread(rag.kb.build, fixtures.KNOWLEDGE_BASE)
    yield


app = FastAPI(title="FreshCart Mock Enterprise Backend", version="1.0", lifespan=lifespan)


class Store:
    """In-memory system of record. Mutations persist for the life of the process."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in fixtures.ACCOUNTS}
        self.by_card = {a["card_number"]: a["account_id"] for a in self.accounts.values()}
        self.stores = copy.deepcopy(fixtures.STORES)
        self.vouchers = copy.deepcopy(fixtures.VOUCHERS)
        self.bookings = copy.deepcopy(fixtures.SUPPLIER_BOOKINGS)
        self.kb = copy.deepcopy(fixtures.KNOWLEDGE_BASE)
        self.contacts: list[dict] = []
        self.taskdesk_tasks: list[dict] = []
        self.fraud_cases: list[dict] = []
        self.transfers: list[dict] = []
        self.comms: list[dict] = []
        self.fraud_line_available = os.getenv("FRAUD_LINE_AVAILABLE", "true").lower() == "true"
        self.online_orders = copy.deepcopy(fixtures.ONLINE_ORDERS)
        self.online_by_ref = {o["order_ref"]: o for o in self.online_orders.values()}
        self.faults: dict[str, dict] = {}


db = Store()


# ---------------------------------------------------------------- latency sim
@app.middleware("http")
async def simulate_system_latency(request: Request, call_next):
    """Real CRM/TaskDesk calls are not instant. Configurable so demos show parallelism honestly."""
    if not request.url.path.startswith("/admin"):
        lo = int(os.getenv("MOCK_LATENCY_MS_MIN", "120"))
        hi = int(os.getenv("MOCK_LATENCY_MS_MAX", "450"))
        await asyncio.sleep(random.uniform(lo, hi) / 1000)
    return await call_next(request)


# ---------------------------------------------------------------- helpers
def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _similar(a: str | None, b: str | None) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _account(account_id: str) -> dict:
    acc = db.accounts.get(account_id)
    if not acc:
        raise HTTPException(404, f"Account {account_id} not found")
    return acc


def _posted_balance(acc: dict) -> int:
    return sum(t["points"] for t in acc["transactions"] if t["status"] == "posted")


def _pending_points(acc: dict) -> int:
    return sum(t["points"] for t in acc["transactions"] if t["status"] == "pending")


def _public_txn(t: dict) -> dict:
    store = db.stores.get(t.get("store_id") or "", {})
    out = {k: v for k, v in t.items() if v is not None}
    out["store_name"] = store.get("name")
    out["store_region"] = store.get("region")
    out["shown_in_red"] = t["points"] < 0
    return out


def _find_by_card(card_number: str) -> Optional[dict]:
    digits = re.sub(r"\D", "", card_number or "")
    if len(digits) == 11:
        digits = fixtures.CARD_PREFIX + digits
    aid = db.by_card.get(digits)
    return db.accounts.get(aid) if aid else None


# ---------------------------------------------------------------- CRM: accounts & ID&V
@app.get("/crm/accounts/locate")
def locate_account(card_number: str):
    acc = _find_by_card(card_number)
    if not acc:
        return {"found": False}
    return {"found": True, "account_id": acc["account_id"], "account_type": acc["account_type"],
            "holder_count": len(acc["holders"])}


class VerifyRequest(BaseModel):
    account_id: str
    level: Literal["standard", "enhanced"]
    full_name: Optional[str] = None
    dob: Optional[str] = None  # ISO yyyy-mm-dd
    address: Optional[str] = None
    security_answer: Optional[str] = None
    memorable_word: Optional[str] = None
    holder_id: Optional[str] = None


@app.post("/crm/idv/verify")
def verify_identity(req: VerifyRequest):
    """Compares supplied answers with the record. Never returns stored secrets."""
    acc = _account(req.account_id)
    holders = acc["holders"]
    if req.holder_id:
        holders = [h for h in holders if h["holder_id"] == req.holder_id] or holders
    best = max(holders, key=lambda h: _similar(h["full_name"], req.full_name))

    matched = {
        "full_name": _similar(best["full_name"], req.full_name) >= 0.85,
        "dob": (req.dob or "") == best["dob"],
    }
    if req.level == "enhanced":
        addr = _norm(req.address)
        matched["address"] = bool(addr) and _norm(best["postcode"]) in addr
        matched["security_answer"] = _similar(best["security_answer"], req.security_answer) >= 0.85
        matched["memorable_word"] = _similar(best["memorable_word"], req.memorable_word) >= 0.9
    passed = all(matched.values())
    return {"passed": passed, "level": req.level, "matched": matched,
            "holder_id": best["holder_id"] if passed else None,
            "holder_role": best["role"] if passed else None,
            "holder_first_name": best["full_name"].split()[0] if passed else None}


@app.get("/crm/idv/security-question")
def security_question(account_id: str, full_name: Optional[str] = None):
    acc = _account(account_id)
    h = max(acc["holders"], key=lambda h: _similar(h["full_name"], full_name)) if full_name else acc["holders"][0]
    return {"holder_id": h["holder_id"], "question": h["security_question"]}


@app.get("/crm/accounts/{account_id}/balance")
def get_balance(account_id: str):
    acc = _account(account_id)
    bal = _posted_balance(acc)
    return {"account_id": account_id, "points_balance": bal, "balance_value_gbp": round(bal * POINT_VALUE_GBP, 2),
            "pending_points": _pending_points(acc), "card_last4": acc["card_number"][-4:],
            "status": acc["status"], "account_type": acc["account_type"], "last_activity": acc["last_activity"]}


@app.get("/crm/accounts/{account_id}/holders")
def get_holders(account_id: str):
    acc = _account(account_id)
    return {"holders": [{"holder_id": h["holder_id"], "first_name": h["full_name"].split()[0], "role": h["role"],
                         "region": h["region"]} for h in acc["holders"]]}


@app.get("/crm/accounts/{account_id}/transactions")
def get_transactions(account_id: str, date_from: Optional[date] = None, date_to: Optional[date] = None,
                     days: int = 120):
    """Transaction Details → date range. Only covers dates after 31/12/2023."""
    acc = _account(account_id)
    date_to = date_to or date.today()
    date_from = date_from or (date_to - timedelta(days=days))
    if date_from <= ARCHIVE_CUTOFF:
        raise HTTPException(400, "Date range search only covers transactions after 31/12/2023. Use the archive view.")
    rows = [t for t in acc["transactions"] if date_from <= date.fromisoformat(t["date"]) <= date_to]
    return {"account_id": account_id, "date_from": str(date_from), "date_to": str(date_to),
            "transactions": [_public_txn(t) for t in sorted(rows, key=lambda t: t["date"], reverse=True)]}


@app.get("/crm/accounts/{account_id}/transactions/archive")
def get_archive(account_id: str):
    """'View all previous transactions' → Jan 2015 to Dec 2023."""
    acc = _account(account_id)
    rows = [t for t in acc["transactions"] if ARCHIVE_START <= date.fromisoformat(t["date"]) <= ARCHIVE_CUTOFF]
    return {"account_id": account_id, "date_from": str(ARCHIVE_START), "date_to": str(ARCHIVE_CUTOFF),
            "transactions": [_public_txn(t) for t in sorted(rows, key=lambda t: t["date"], reverse=True)]}


@app.get("/crm/vouchers/{code}")
def get_voucher(code: str):
    v = db.vouchers.get(code)
    if not v:
        raise HTTPException(404, "Voucher not found")
    return v


@app.get("/crm/supplier-bookings/{ref}")
def get_booking(ref: str):
    b = db.bookings.get(ref)
    if not b:
        raise HTTPException(404, "Booking not found")
    return b


@app.get("/crm/stores/{store_id}")
def get_store(store_id: str):
    s = db.stores.get(store_id)
    if not s:
        raise HTTPException(404, "Store not found")
    return s


class ReasonCodeRequest(BaseModel):
    reason_code: str
    txn_id: Optional[str] = None
    note: Optional[str] = None


@app.post("/crm/accounts/{account_id}/reason-code")
def set_reason_code(account_id: str, req: ReasonCodeRequest):
    acc = _account(account_id)
    acc["reason_code"] = req.reason_code
    acc["redemption_blocked"] = req.reason_code == "CANNOT_REDEEM"
    return {"account_id": account_id, "reason_code": req.reason_code, "redemption_blocked": acc["redemption_blocked"]}


class DeactivateRequest(BaseModel):
    reason: str


@app.post("/crm/accounts/{account_id}/deactivate")
def deactivate(account_id: str, req: DeactivateRequest):
    acc = _account(account_id)
    acc["status"] = "deactivated"
    primary = next(h for h in acc["holders"] if h["role"] == "primary")
    return {"account_id": account_id, "status": "deactivated", "primary_holder_first_name": primary["full_name"].split()[0],
            "primary_notified_via": "letter"}


class CreateMemberRequest(BaseModel):
    full_name: str
    dob: str
    address: str
    email: Optional[str] = None
    mobile: Optional[str] = None
    marketing_opt_in: bool = False
    terms_accepted: bool


@app.post("/crm/members")
def create_member(req: CreateMemberRequest):
    if not req.terms_accepted:
        raise HTTPException(422, "Terms must be accepted before registration")
    for acc in db.accounts.values():
        for h in acc["holders"]:
            if _similar(h["full_name"], req.full_name) >= 0.9 and h["dob"] == req.dob \
                    and _norm(h["postcode"]) in _norm(req.address):
                raise HTTPException(409, "A FreshCart Rewards account already exists for these details")
    suffix = "".join(random.choice("0123456789") for _ in range(11))
    card_number = fixtures.CARD_PREFIX + suffix
    aid = f"ACC-{random.randint(2000, 9999)}"
    new = {"account_id": aid, "card_number": card_number, "account_type": "Standard collector", "status": "pending_activation",
           "last_activity": str(date.today()),
           "holders": [{"holder_id": f"H-{aid[4:]}", "full_name": req.full_name, "dob": req.dob,
                        "address_line1": req.address, "postcode": "", "region": None, "security_question": None,
                        "security_answer": None, "memorable_word": None, "role": "primary"}],
           "transactions": [], "email": req.email, "mobile": req.mobile, "marketing_opt_in": req.marketing_opt_in}
    db.accounts[aid] = new
    db.by_card[card_number] = aid
    eta = date.today() + timedelta(days=7)
    return {"account_id": aid, "card_last4": card_number[-4:], "status": "pending_activation",
            "card_pack": {"generated": True, "dispatch": "Royal Mail 2nd class", "expected_by": str(eta + timedelta(days=3))}}


class ContactLogRequest(BaseModel):
    account_id: Optional[str] = None
    session_id: str
    journey: str
    outcome: str
    note: Optional[str] = None
    disposition: Optional[str] = None          # call-level: automated | deflected | transferred_to_advisor | ...
    transfer_reason: Optional[str] = None
    journeys: Optional[list[dict]] = None
    ended_by: Optional[str] = None


@app.post("/crm/contacts")
def log_contact(req: ContactLogRequest):
    entry = {"contact_id": f"CT-{uuid.uuid4().hex[:8].upper()}", "logged_at": datetime.utcnow().isoformat(), **req.model_dump()}
    db.contacts.append(entry)
    return entry


# ---------------------------------------------------------------- TaskDesk
class TaskDeskTaskRequest(BaseModel):
    account_id: str
    task_type: Literal["ARCHIVE_REQUEST", "PRODUCT_SPECIALIST_REVIEW"]
    summary: str
    txn_id: Optional[str] = None


@app.post("/taskdesk/tasks")
def create_task(req: TaskDeskTaskRequest):
    team = "FreshCart Rewards Specialist Team" if req.task_type == "ARCHIVE_REQUEST" else "FreshCart Rewards Product Specialists"
    t = {"task_id": f"NOVA-T-{random.randint(100000, 999999)}", "assigned_team": team, "sla_working_days": 5,
         "created_at": datetime.utcnow().isoformat(), **req.model_dump()}
    db.taskdesk_tasks.append(t)
    return t


class FraudCaseRequest(BaseModel):
    account_id: str
    txn_id: Optional[str] = None
    summary: str
    points_disputed: Optional[int] = None


@app.post("/taskdesk/fraud-cases")
def create_fraud_case(req: FraudCaseRequest):
    c = {"case_id": f"NOVA-F-{random.randint(100000, 999999)}", "assigned_team": "FreshCart Rewards Fraud Team",
         "contact_within_working_days": 2, "created_at": datetime.utcnow().isoformat(), **req.model_dump()}
    db.fraud_cases.append(c)
    return c


# ---------------------------------------------------------------- Telephony
class TransferRequest(BaseModel):
    session_id: str
    queue: Literal["fraud", "general", "lost_stolen", "online_delivery"]
    summary: str
    handover: Optional[dict] = None            # ContactHub screen-pop: reason, summary, redacted transcript


@app.post("/telephony/warm-transfer")
def warm_transfer(req: TransferRequest):
    connected = db.fraud_line_available if req.queue == "fraud" else True
    rec = {"transfer_id": f"TR-{uuid.uuid4().hex[:6].upper()}", "connected": connected,
           "queue": req.queue, "estimated_wait_seconds": random.randint(20, 90) if connected else None,
           **req.model_dump()}
    db.transfers.append(rec)
    return rec


# ---------------------------------------------------------------- Comms
class SendLinkRequest(BaseModel):
    channel: Literal["sms", "email"]
    destination: str
    link_type: Literal["rewards_app_download", "rewards_web_register"]


@app.post("/comms/send-link")
def send_link(req: SendLinkRequest):
    rec = {"message_id": f"MSG-{uuid.uuid4().hex[:8]}", "status": "queued", **req.model_dump()}
    db.comms.append(rec)
    return {"message_id": rec["message_id"], "status": "queued", "channel": req.channel}


# ---------------------------------------------------------------- Knowledge base (Knowledge Base)
@app.get("/kb/search")
def kb_search(q: str, limit: int = 3):
    """Semantic search over the RAG Knowledge Base (Chroma + sentence-transformers, see rag.py)."""
    return {"results": rag.kb.search(q, limit), "retrieval": "vector"}


@app.get("/kb/documents/{doc_id}")
def kb_doc(doc_id: str):
    for d in db.kb:
        if d["doc_id"] == doc_id:
            return d
    raise HTTPException(404, "Document not found")


# ---------------------------------------------------------------- Online (FreshCart Online)
# Order System order validation + order status, and the Message Broker refund SMS.
def _order(order_ref: str) -> dict:
    o = db.online_by_ref.get(order_ref)
    if not o:
        raise HTTPException(404, "Order not found")
    return o


def _digits(v: str | None) -> str:
    d = re.sub(r"\D", "", v or "")
    return "0" + d[2:] if d.startswith("44") else d


class ValidateOrder(BaseModel):
    order_number: str


@app.post("/online/idv/validate-order")
def online_validate_order(req: ValidateOrder):
    """IDV 1: is this a real order number? (Order System)"""
    o = db.online_orders.get(re.sub(r"\D", "", req.order_number))
    return {"valid": bool(o), "order_ref": o["order_ref"] if o else None}


class CallerCheck(BaseModel):
    order_ref: str
    caller_number: Optional[str] = None


@app.post("/online/idv/caller-check")
def online_caller_check(req: CallerCheck):
    """Does the calling line match the phone on the order? Never returns the stored number."""
    o = _order(req.order_ref)
    return {"matches": bool(req.caller_number) and _digits(req.caller_number) == _digits(o["phone"])}


class VerifyPhone(BaseModel):
    order_ref: str
    phone: str


@app.post("/online/idv/verify-phone")
def online_verify_phone(req: VerifyPhone):
    """IDV 2."""
    return {"match": _digits(req.phone) == _digits(_order(req.order_ref)["phone"])}


class VerifyPostcode(BaseModel):
    order_ref: str
    postcode: str


@app.post("/online/idv/verify-postcode")
def online_verify_postcode(req: VerifyPostcode):
    """IDV 3."""
    return {"match": _norm(req.postcode) == _norm(_order(req.order_ref)["postcode"])}


@app.get("/online/orders/{order_ref}/status")
def online_order_status(order_ref: str):
    o = _order(order_ref)
    keep = ("order_ref", "status", "delivery_date", "slot_start", "slot_end", "delivered_at", "item_count", "cancelled_at")
    return {k: o.get(k) for k in keep if o.get(k) is not None} | {"order_number_last4": o["order_number"][-4:]}


class RefundLink(BaseModel):
    order_ref: str


@app.post("/online/comms/refund-link")
def online_refund_link(req: RefundLink):
    """Refund SMS via the Message Broker (online-refund template) → SMS Gateway → SMS provider."""
    o = _order(req.order_ref)
    rec = {"message_id": f"Messaging-{uuid.uuid4().hex[:8].upper()}", "status": "queued", "template": "online-refund",
           "route": "Message Broker → SMS Gateway", "channel": "sms", "sent_to_last4": o["phone"][-4:],
           "order_ref": o["order_ref"]}
    db.comms.append(rec)
    return {k: v for k, v in rec.items() if k != "order_ref"}


# ---------------------------------------------------------------- admin (demo operator only; not exposed via MCP)
@app.get("/admin/state")
def admin_state():
    return {"contacts": db.contacts, "taskdesk_tasks": db.taskdesk_tasks, "fraud_cases": db.fraud_cases,
            "transfers": db.transfers, "comms": db.comms, "fraud_line_available": db.fraud_line_available}


class Fault(BaseModel):
    path_prefix: str                       # e.g. "/crm/accounts" ("" clears all faults)
    mode: Literal["error", "timeout"] = "error"
    count: int = 1                         # how many requests fail before the system recovers


@app.post("/admin/fault")
def set_fault(req: Fault):
    """Test hook: make the next N requests under a path fail (HTTP 503) or hang (timeout)."""
    if not req.path_prefix:
        db.faults.clear()
    else:
        db.faults[req.path_prefix] = {"mode": req.mode, "count": req.count}
    return {"faults": db.faults}


@app.middleware("http")
async def _faults(request: Request, call_next):
    for prefix, f in list(db.faults.items()):
        if request.url.path.startswith(prefix) and f["count"] > 0:
            f["count"] -= 1
            if f["count"] <= 0:
                db.faults.pop(prefix, None)
            if f["mode"] == "timeout":
                await asyncio.sleep(30)
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503)
    return await call_next(request)


class FraudLine(BaseModel):
    available: bool


@app.post("/admin/fraud-line")
def set_fraud_line(req: FraudLine):
    db.fraud_line_available = req.available
    return {"fraud_line_available": db.fraud_line_available}


@app.post("/admin/reset")
def reset():
    db.reset()
    return {"reset": True}


@app.get("/health")
def health():
    return {"ok": True}
