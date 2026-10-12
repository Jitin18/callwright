"""
Per-session event bus. Every observable thing in the system — agent activity,
handoffs, tool calls, journey node transitions, guardrail verdicts, speech — is an
event. The UI renders these; nothing else is sent to the browser.

Events MUST already be redacted when published (see pii.Redactor).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._subscribers: list[asyncio.Queue] = []
        self._t0 = time.perf_counter()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def emit(self, type_: str, **data: Any) -> None:
        evt = {"type": type_, "t": round((time.perf_counter() - self._t0) * 1000), **data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass


def now_ms() -> float:
    return time.perf_counter() * 1000
