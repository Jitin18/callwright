"""Per-call session state. One Session per browser/telephony connection."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .config import settings
from .events import EventBus
from .journeys.engine import JourneyRun
from .pii import Redactor, Vault


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    channel: str = settings.channel
    caller_number: Optional[str] = None                # calling line identity (ANI); used by Online IDV 2
    vault: Vault = field(default_factory=Vault)
    idv: dict = field(default_factory=dict)            # level, account_id, holder_id, holder_role, first_name
    runs: list[JourneyRun] = field(default_factory=list)
    subagents: dict = field(default_factory=dict)        # subagent key → Subagent (this call's subagents)
    history: list[dict] = field(default_factory=list)  # redacted: {"role": "customer"|"assistant", "text": str}
    sentiment: str = "neutral"
    turn_no: int = 0
    ended: bool = False
    closing: bool = False                               # end_call requested; goodbye in progress
    transferred: bool = False                           # handed to a colleague: no goodbye, no more journeys
    transfer: Optional[dict] = None                     # {"queue", "reason"} of the warm transfer
    outcome: Optional[dict] = None                      # the logged call outcome (disposition etc.), once
    requests: list = field(default_factory=list)        # journey titles the customer asked for, in order
    last_transcript: str = ""                           # latest customer words (from Voice Live)
    allowed_numbers: set = field(default_factory=set)   # grounding allow-list for the output rails
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.bus = EventBus(self.id)
        self.redactor = Redactor(self.vault)

    def new_turn(self) -> str:
        self.turn_no += 1
        return f"t{self.turn_no}"

    def pending_run(self) -> Optional[JourneyRun]:
        """The journey whose question the assistant is currently waiting on (most recently asked)."""
        waiting = [r for r in self.runs if r.status == "awaiting_input"]
        return waiting[-1] if waiting else None

    def add_history(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text})
