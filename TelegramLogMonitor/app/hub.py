"""In-process pub/sub that fans new messages out to connected dashboard clients."""
from __future__ import annotations

import asyncio


class Hub:
    def __init__(self, queue_size: int = 500):
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; it will catch up by reloading the list

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
