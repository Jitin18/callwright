"""
CRM / TaskDesk / Telephony system-of-record fixtures.

This is NOT LLM content. It stands in for FreshCart systems of record (the CRM
member database, store directory, voucher ledger and knowledge base) so the agent
runtime has something real to call through MCP. Every record is chosen so that a
specific branch of the SOP flowcharts is reachable during a live demo — see
docs/CONTEXT.md → "Demo accounts".

Card format (configurable in services/agent_runtime/config.py): 19 digits,
prefix 60012300 + 11 digits. Customers may say the full 19 or just the last 11.
"""

CARD_PREFIX = "60012300"


def card(suffix11: str) -> str:
    assert len(suffix11) == 11 and suffix11.isdigit()
    return CARD_PREFIX + suffix11


STORES = {
    "S-BRS-01": {"store_id": "S-BRS-01", "name": "FreshCart Clifton", "town": "Bristol", "region": "Bristol"},
    "S-BRS-02": {"store_id": "S-BRS-02", "name": "FreshCart Winterstoke Road", "town": "Bristol", "region": "Bristol"},
    "S-MAN-01": {"store_id": "S-MAN-01", "name": "FreshCart Manchester Fort", "town": "Manchester", "region": "Manchester"},
    "S-LDN-01": {"store_id": "S-LDN-01", "name": "FreshCart Holborn Circus", "town": "London", "region": "London"},
    "S-LDN-02": {"store_id": "S-LDN-02", "name": "FreshCart Nine Elms", "town": "London", "region": "London"},
    "S-LDS-01": {"store_id": "S-LDS-01", "name": "FreshCart Moor Allerton", "town": "Leeds", "region": "Leeds"},
    "S-GLA-01": {"store_id": "S-GLA-01", "name": "FreshCart Glasgow Darnley", "town": "Glasgow", "region": "Glasgow"},
    "ONLINE": {"store_id": "ONLINE", "name": "FreshCart Online", "town": None, "region": None},
}

VOUCHERS = {
    "TC-8H2K-4471": {"code": "TC-8H2K-4471", "partner": "DineClub", "description": "DineClub 3-month membership",
                     "points": 1500, "value_gbp": 7.50, "issued_at": "2026-03-03", "expires_at": "2026-06-03", "status": "used"},
    "AR-Q7P2-1193": {"code": "AR-Q7P2-1193", "partner": "HomeStore", "description": "HomeStore £10 eVoucher",
                     "points": 2000, "value_gbp": 10.00, "issued_at": "2025-11-19", "expires_at": "2026-11-19", "status": "unused"},
    "EB-2231-ZX90": {"code": "EB-2231-ZX90", "partner": "eBay", "description": "eBay £5 voucher",
                     "points": 1000, "value_gbp": 5.00, "issued_at": "2021-08-14", "expires_at": "2022-08-14", "status": "expired"},
    "CAF-99K1-0007": {"code": "CAF-99K1-0007", "partner": "Caffè Nero", "description": "Caffè Nero £5 eGift",
                      "points": 1000, "value_gbp": 5.00, "issued_at": "2026-08-22", "expires_at": "2027-02-22", "status": "unused"},
}

SUPPLIER_BOOKINGS = {
    "BA-NCT-55120": {"ref": "BA-NCT-55120", "partner": "British Airways AeroMiles conversion", "points": 3000,
                     "converted_to": "750 AeroMiles", "processed_at": "2019-05-02"},
}


def _t(txn_id, date, kind, points, desc, store="S-BRS-01", status="posted", channel="in_store", **extra):
    return {"txn_id": txn_id, "date": date, "type": kind, "points": points, "description": desc,
            "store_id": store, "status": status, "channel": channel, **extra}


def _holder(holder_id, name, dob, line1, postcode, region, q, a, word, role="primary"):
    return {"holder_id": holder_id, "full_name": name, "dob": dob, "address_line1": line1, "postcode": postcode,
            "region": region, "security_question": q, "security_answer": a, "memorable_word": word, "role": role}


ACCOUNTS = [
    # J1 + J4: healthy account, voucher redemptions across both date eras
    {"account_id": "ACC-1001", "card_number": card("12345678901"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-20",
     "holders": [_holder("H-1001", "Priya Sharma", "1988-04-12", "14 Redland Grove", "BS6 6PR", "Bristol",
                         "What was the name of your first school?", "St Michaels", "lantern")],
     "transactions": [
         _t("T1001-01", "2019-05-02", "redeem", -3000, "AeroMiles conversion", "ONLINE", channel="online", supplier_booking_ref="BA-NCT-55120"),
         _t("T1001-02", "2021-08-14", "redeem", -1000, "eBay voucher", "ONLINE", channel="online", voucher_code="EB-2231-ZX90"),
         _t("T1001-03", "2022-12-10", "earn", 6200, "Christmas shop and fuel"),
         _t("T1001-04", "2023-06-30", "earn", 2100, "Grocery shops Q2"),
         _t("T1001-05", "2025-11-19", "redeem", -2000, "HomeStore eVoucher", "ONLINE", channel="app", voucher_code="AR-Q7P2-1193"),
         _t("T1001-06", "2026-01-15", "earn", 1320, "Weekly shops January"),
         _t("T1001-07", "2026-03-03", "redeem", -1500, "DineClub membership", "ONLINE", channel="app", voucher_code="TC-8H2K-4471"),
         _t("T1001-08", "2026-06-11", "earn", 1760, "Weekly shops"),
         _t("T1001-09", "2026-08-22", "redeem", -1000, "Caffè Nero eGift", "ONLINE", channel="app", voucher_code="CAF-99K1-0007"),
         _t("T1001-10", "2026-09-20", "earn", 940, "Weekly shop Clifton"),
     ]},
    # J2: lower than expected because of return deductions (red points), not dormant
    {"account_id": "ACC-1002", "card_number": card("20456789012"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-18",
     "holders": [_holder("H-1002", "Oliver Bennett", "1979-11-03", "8 Kings Road", "SW3 4TR", "London",
                         "What is your mother's maiden name?", "Hartley", "copper")],
     "transactions": [
         _t("T1002-01", "2026-07-02", "earn", 2400, "TV purchase HomeStore in FreshCart", "S-LDN-01"),
         _t("T1002-02", "2026-07-20", "earn", 880, "Weekly shops", "S-LDN-01"),
         _t("T1002-03", "2026-09-05", "return", -2400, "Points removed: TV returned and refunded", "S-LDN-01"),
         _t("T1002-04", "2026-09-18", "earn", 610, "Weekly shop", "S-LDN-02"),
     ]},
    # J2: dormant account with deductions
    {"account_id": "ACC-1003", "card_number": card("30567890123"), "account_type": "Standard collector",
     "status": "dormant", "last_activity": "2024-06-14",
     "holders": [_holder("H-1003", "Margaret Doyle", "1954-02-27", "3 Abbey Close", "LS17 6AA", "Leeds",
                         "What was the name of your first pet?", "Biscuit", "harbour")],
     "transactions": [
         _t("T1003-01", "2024-03-02", "earn", 1850, "Grocery shops", "S-LDS-01"),
         _t("T1003-02", "2024-06-14", "earn", 420, "Grocery shop", "S-LDS-01"),
         _t("T1003-03", "2026-06-14", "expiry", -2270, "Points expired: account dormant 24 months", "ONLINE", channel="system"),
     ]},
    # J2: pending points explain the gap
    {"account_id": "ACC-1004", "card_number": card("40678901234"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-25",
     "holders": [_holder("H-1004", "James Okafor", "1992-07-19", "27 Deansgate Quay", "M3 4LZ", "Manchester",
                         "What city were you born in?", "Lagos", "meridian")],
     "transactions": [
         _t("T1004-01", "2026-08-30", "earn", 1200, "Weekly shops", "S-MAN-01"),
         _t("T1004-02", "2026-09-25", "earn", 3600, "Bonus points promotion: fuel and grocery", "S-MAN-01", status="pending"),
     ]},
    # J2: nothing explains the gap -> archive request on TaskDesk
    {"account_id": "ACC-1005", "card_number": card("50789012345"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-10",
     "holders": [_holder("H-1005", "Aisha Rahman", "1985-09-30", "61 Byres Road", "G12 8TN", "Glasgow",
                         "What was your childhood nickname?", "Sunny", "thistle")],
     "transactions": [
         _t("T1005-01", "2026-05-12", "earn", 900, "Weekly shops", "S-GLA-01"),
         _t("T1005-02", "2026-09-10", "earn", 450, "Weekly shop", "S-GLA-01"),
     ]},
    # J3: negative balance, disputed in-store redemption at a NON-local store (> 2000) -> fraud transfer
    {"account_id": "ACC-1006", "card_number": card("60890123456"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-21",
     "holders": [_holder("H-1006", "Tom Hughes", "1990-01-08", "5 Gloucester Road", "BS7 8AA", "Bristol",
                         "What was the make of your first car?", "Fiesta", "orchard")],
     "transactions": [
         _t("T1006-01", "2026-09-01", "earn", 1250, "Weekly shops", "S-BRS-02"),
         _t("T1006-02", "2026-09-19", "redeem", -2500, "In-store points redemption at till", "S-MAN-01"),
     ]},
    # J3: negative balance, disputed in-store redemption at a LOCAL store (< 2000) -> police advice
    {"account_id": "ACC-1007", "card_number": card("70901234567"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-22",
     "holders": [_holder("H-1007", "Sophie Clarke", "1996-12-15", "90 Upper Street", "N1 0NP", "London",
                         "What is your favourite film?", "Amelie", "velvet")],
     "transactions": [
         _t("T1007-01", "2026-09-02", "earn", 1200, "Weekly shops", "S-LDN-01"),
         _t("T1007-02", "2026-09-22", "redeem", -1800, "In-store points redemption at till", "S-LDN-02"),
     ]},
    # J3: household account. Rachel (secondary) -> non-primary branch. David (primary) -> online redemption branch.
    {"account_id": "ACC-1008", "card_number": card("80012345678"), "account_type": "Household account",
     "status": "active", "last_activity": "2026-09-23",
     "holders": [
         _holder("H-1008A", "David Evans", "1975-05-21", "12 Mill Lane", "LS6 2BB", "Leeds",
                 "What was your first job?", "Paperboy", "granite"),
         _holder("H-1008B", "Rachel Evans", "1977-10-09", "12 Mill Lane", "LS6 2BB", "Leeds",
                 "What street did you grow up on?", "Elm Street", "saffron", role="secondary"),
     ],
     "transactions": [
         _t("T1008-01", "2026-09-01", "earn", 900, "Weekly shops", "S-LDS-01"),
         _t("T1008-02", "2026-09-23", "redeem", -2000, "Online points redemption: HomeStore eVoucher", "ONLINE", channel="online", voucher_code=None),
     ]},
    # J3: negative balance NOT disputed: return processed after the points were spent
    {"account_id": "ACC-1009", "card_number": card("90123456789"), "account_type": "Standard collector",
     "status": "active", "last_activity": "2026-09-24",
     "holders": [_holder("H-1009", "Ben Walsh", "1983-03-04", "44 Park Row", "BS1 5LJ", "Bristol",
                         "What was the name of your primary school teacher?", "Mrs Patel", "falcon")],
     "transactions": [
         _t("T1009-01", "2026-08-28", "earn", 1500, "Garden furniture purchase", "S-BRS-01"),
         _t("T1009-02", "2026-09-04", "redeem", -1500, "Points spent at till", "S-BRS-01"),
         _t("T1009-03", "2026-09-24", "return", -1500, "Points removed: garden furniture returned", "S-BRS-01", status="pending"),
         _t("T1009-04", "2026-09-24", "return", -700, "Points removed: refunded item", "S-BRS-01"),
     ]},
]

# Knowledge base ("Knowledge Base"). DEMO CONTENT — every document must be replaced
# with approved wording before any client-facing use.
KNOWLEDGE_BASE = [
    {"doc_id": "KB-PHYS-LIMITS", "title": "Registering over the phone: physical card limitations",
     "tags": ["signup", "analogue", "limitations"],
     "body": "[DEMO CONTENT] Cards registered over the phone are posted as a physical card pack and take 7 to 10 days to arrive. "
             "The card must be activated in store before points can be spent. Personalised app offers are only available "
             "once the member also registers in the FreshCart app."},
    {"doc_id": "KB-TANDC", "title": "FreshCart Rewards terms and conditions summary for verbal consent",
     "tags": ["signup", "terms", "consent"],
     "body": "[DEMO CONTENT] By registering, the member agrees to the FreshCart Rewards terms and conditions and privacy notice, "
             "available at freshcart.example/rewards. Points have no cash value, can be removed if a purchase is refunded, and may expire "
             "after 12 months of account inactivity."},
    {"doc_id": "KB-DORMANCY", "title": "Dormancy process",
     "tags": ["balance", "dormant"],
     "body": "[DEMO CONTENT] Accounts with no activity for an extended period are marked dormant and points may expire. "
             "The member can reactivate the account by collecting points again. Expired points are not reinstated automatically; "
             "a reinstatement request can be reviewed by the specialist team."},
    {"doc_id": "KB-PENDING", "title": "Points posting times",
     "tags": ["balance", "pending"],
     "body": "[DEMO CONTENT] Base points usually post within 24 hours. Bonus and promotional points can take up to 28 days "
             "after the qualifying transaction to appear in the balance."},
    {"doc_id": "KB-POLICE", "title": "Disputed in-store redemption at a local store (under threshold)",
     "tags": ["fraud", "police", "negative"],
     "body": "[DEMO CONTENT] For disputed in-store redemptions at the member's local store below the fraud threshold, "
             "advise the member to report the incident to the police and keep the crime reference number, which the "
             "member can share with FreshCart Rewards to support a review."},
    {"doc_id": "KB-FRAUD", "title": "Fraud cases and warm transfers",
     "tags": ["fraud", "negative"],
     "body": "[DEMO CONTENT] Suspected fraudulent redemptions are handled by the fraud team. Where the fraud line is "
             "available the call is warm-transferred; otherwise a fraud case is raised and the fraud team contacts the "
             "member within 2 working days."},
    {"doc_id": "KB-ACTIVATE", "title": "Activating a new physical card",
     "tags": ["signup", "activation"],
     "body": "[DEMO CONTENT] New physical cards are activated by presenting the card at any FreshCart till or "
             "self-checkout on the first shop."},
    {"doc_id": "KB-LOSTCARD", "title": "Lost or stolen card",
     "tags": ["idv", "lost"],
     "body": "[DEMO CONTENT] If the member cannot provide the card number because the card is lost or stolen, the "
             "lost or stolen card process applies and a colleague completes verification using alternative details."},
    {"doc_id": "KB-APP", "title": "Registering digitally",
     "tags": ["signup", "digital"],
     "body": "[DEMO CONTENT] Customers can register in the FreshCart app (App Store or Google Play) or at freshcart.example/rewards by "
             "choosing Register and entering their email, name, date of birth and address. Digital members get their "
             "card in the app immediately."},
    {"doc_id": "KB-DELAY", "title": "Negative balance caused by later refunds",
     "tags": ["negative", "returns"],
     "body": "[DEMO CONTENT] A balance can go below zero when points earned on a purchase were spent before that "
             "purchase was refunded. Future points collected will bring the balance back above zero."},
]


# ---------------------------------------------------------------------------
# FreshCart Online (Online) — online order management (Order System).
# Order numbers are 10 digits (configurable: ORDER_NUMBER_LENGTH).
# Phone and postcode are the IDV 2 / IDV 3 factors from the Online IVR flowchart.
ONLINE_ORDERS = {
    "7004128855": {"order_ref": "ONL-ORD-128855", "order_number": "7004128855", "customer_name": "Hannah Moore",
                   "phone": "07700900111", "postcode": "SE1 7PB", "status": "delivered",
                   "delivery_date": "2026-09-26", "slot_start": "10:00", "slot_end": "11:00",
                   "delivered_at": "2026-09-26T10:42", "item_count": 38},
    "7004133907": {"order_ref": "ONL-ORD-133907", "order_number": "7004133907", "customer_name": "Daniel Price",
                   "phone": "07700900222", "postcode": "BS8 1TH", "status": "not_delivered",
                   "delivery_date": "2026-10-02", "slot_start": "17:00", "slot_end": "18:00",
                   "delivered_at": None, "item_count": 24},
    "7004120016": {"order_ref": "ONL-ORD-120016", "order_number": "7004120016", "customer_name": "Grace Adeyemi",
                   "phone": "07700900333", "postcode": "M20 2WS", "status": "cancelled",
                   "delivery_date": "2026-09-25", "slot_start": "08:00", "slot_end": "09:00",
                   "delivered_at": None, "item_count": 0, "cancelled_at": "2026-09-24T19:05"},
}

KNOWLEDGE_BASE += [
    {"doc_id": "KB-ONLINE-REFUND-SELFSERVE", "title": "Online refunds can be requested online (IVR deflection message)",
     "tags": ["online", "refund", "deflection"],
     "body": "[DEMO CONTENT] If you're calling to discuss getting a refund, you can now "
             "request refunds online: go to help.freshcart.example and search for refund requests. If you'd still "
             "like to speak to someone, a colleague can help."},
    {"doc_id": "KB-ONLINE-REFUND-LINK", "title": "Refund link by SMS",
     "tags": ["online", "refund", "sms"],
     "body": "[DEMO CONTENT] The refund link is sent by text message to the mobile number on the order, using the "
             "FreshCart online-refund message template. The link opens the online refund request form for that order."},
]

# General FAQ articles for the Knowledge subagent (RAG). DEMO CONTENT, to be replaced with approved wording.
KNOWLEDGE_BASE += [
    {"doc_id": "KB-POINTS-VALUE", "title": "What FreshCart Rewards points are worth",
     "tags": ["points", "value", "spend"],
     "body": "[DEMO CONTENT] Every 200 FreshCart Rewards points are worth £1 to spend. Members can spend points at the till in "
             "FreshCart and HomeStore in multiples of 500 points, or save them up for bigger rewards."},
    {"doc_id": "KB-AEROMILES", "title": "Converting FreshCart Rewards points to AeroMiles",
     "tags": ["aeromiles", "convert", "partners", "spend"],
     "body": "[DEMO CONTENT] FreshCart Rewards points can be converted to AeroMiles in the FreshCart app or at freshcart.example/rewards. 400 FreshCart Rewards "
             "points convert to 250 AeroMiles. Conversions cannot be reversed once confirmed and usually appear in the "
             "AeroMiles account within 3 working days."},
    {"doc_id": "KB-MISSING-POINTS", "title": "Claiming missing points",
     "tags": ["missing", "points", "claim", "receipt"],
     "body": "[DEMO CONTENT] If points from a shop haven't appeared after 28 days, members can claim them in the "
             "FreshCart app under 'Missing points' within 6 months of the shop. They need the receipt showing the "
             "transaction date, store and amount."},
    {"doc_id": "KB-MEMBER-PRICES", "title": "Member Prices",
     "tags": ["member prices", "offers", "discount"],
     "body": "[DEMO CONTENT] Member Prices are lower prices on selected products for FreshCart Rewards members. The member "
             "scans their FreshCart Rewards card or app at the till or adds their card to their online account before checkout "
             "for the discount to apply."},
    {"doc_id": "KB-CHANGE-DETAILS", "title": "Changing name, address or email on a FreshCart Rewards account",
     "tags": ["account", "address", "email", "update", "details"],
     "body": "[DEMO CONTENT] Members can update their email, address and phone number in the FreshCart app under "
             "Account settings. A change of name needs a colleague and may require proof of the change."},
    {"doc_id": "KB-ONLINE-SLOT-CHANGE", "title": "Changing or cancelling an online grocery order",
     "tags": ["online", "slot", "cancel", "amend", "delivery"],
     "body": "[DEMO CONTENT] Online grocery orders can be amended or cancelled on the FreshCart website or app "
             "until 11pm the night before delivery. The delivery slot can be changed if another slot is available; "
             "a different delivery charge may apply."},
    {"doc_id": "KB-ONLINE-SUBS", "title": "Substitutions in online grocery orders",
     "tags": ["online", "substitution", "refund", "items"],
     "body": "[DEMO CONTENT] If an item is unavailable a similar product may be substituted. Substitutions are "
             "listed on the delivery note and customers can hand back any they don't want to the driver for a "
             "refund. Customers can turn substitutions off for any item before the order is picked."},
    {"doc_id": "KB-BONUS-POINTS", "title": "Bonus points offers",
     "tags": ["bonus", "offers", "points", "personalised"],
     "body": "[DEMO CONTENT] Personalised bonus points offers are in the FreshCart app and must be saved before "
             "shopping. Bonus points can take up to 28 days after the qualifying shop to appear in the balance."},
]
