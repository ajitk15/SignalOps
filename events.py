"""In-process event bus: orchestrator publishes, FastAPI WebSocket relays.

Kept deliberately simple (asyncio pub/sub, no external broker) since this is
a single-process sample app.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    type: str  # poll_started | poll_result | poll_failed | agent_started | agent_completed | incident_created
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class EventBus:
    def __init__(self, history_limit: int = 500) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: list[Event] = []
        self._history_limit = history_limit

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: Event) -> None:
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history.pop(0)
        for q in list(self._subscribers):
            q.put_nowait(event)

    def recent(self, limit: int = 100) -> list[Event]:
        return self._history[-limit:]


bus = EventBus()
