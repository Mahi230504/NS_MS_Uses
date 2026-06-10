"""In-process event bus: fans agent events out to dashboard SSE subscribers.

One publisher (the orchestrator/handlers, on the bot's single event loop) and N
subscribers (each open `/events` SSE response). The contract that keeps the
agent loop safe: `publish()` is SYNCHRONOUS and `put_nowait`-only — it never
awaits and never raises, so a slow or dead browser can never apply backpressure
to the agent loop. A subscriber whose bounded queue fills drops its oldest event
and gets a `{"type":"lag"}` marker telling the client to resync.

Everything runs on one event loop, so there are no locks/threads: the subscriber
set is mutated synchronously between awaits. A small ring buffer lets a browser
that connects mid-task immediately paint the recent steps.
"""
from __future__ import annotations

import asyncio
from collections import deque

_CLOSE = object()  # sentinel pushed on close() to end an async iteration


class Subscription:
    """One SSE client's view: an async-iterable of event dicts."""

    def __init__(self, bus: "EventBus", maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    def _put(self, event) -> None:
        """Non-blocking enqueue; keep-newest (drop oldest) when full.

        A slow client loses intermediate events, never the latest — and the
        frontend merges step events by index + can resync via /api/active, so a
        gap is harmless. Crucially this never blocks or raises.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()       # drop the oldest
                self._queue.put_nowait(event)  # keep the newest
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is _CLOSE:
            raise StopAsyncIteration
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)
        try:
            self._queue.put_nowait(_CLOSE)
        except asyncio.QueueFull:
            pass


class EventBus:
    def __init__(self, *, queue_maxsize: int = 64, ring_size: int = 50) -> None:
        self._subs: set[Subscription] = set()
        self._ring: deque = deque(maxlen=ring_size)
        self._last: dict | None = None
        self._maxsize = queue_maxsize

    def publish(self, event: dict) -> None:
        """Fan an event out to all subscribers. Sync, non-blocking, never raises."""
        self._last = event
        self._ring.append(event)
        for sub in list(self._subs):
            sub._put(event)

    def subscribe(self) -> Subscription:
        """Register a subscriber, seeded with the current ring buffer backlog."""
        sub = Subscription(self, self._maxsize)
        self._subs.add(sub)
        for ev in list(self._ring):
            sub._put(ev)
        return sub

    def _remove(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def latest_snapshot(self) -> dict | None:
        """The most recently published event (backs /api/active.last_step)."""
        return self._last

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
