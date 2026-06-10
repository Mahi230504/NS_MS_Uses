"""Unit tests for the in-process dashboard EventBus."""
from __future__ import annotations

import asyncio

import pytest

from bot.events import EventBus


async def _next(sub, timeout: float = 1.0):
    return await asyncio.wait_for(sub.__anext__(), timeout)


class TestEventBus:
    async def test_live_publish_delivered(self) -> None:
        bus = EventBus()
        sub = bus.subscribe()
        bus.publish({"type": "step", "n": 1})
        ev = await _next(sub)
        assert ev["n"] == 1
        sub.close()

    async def test_subscribe_replays_ring_backlog(self) -> None:
        bus = EventBus()
        bus.publish({"type": "step", "n": 1})
        bus.publish({"type": "step", "n": 2})
        sub = bus.subscribe()  # after the publishes
        assert (await _next(sub))["n"] == 1
        assert (await _next(sub))["n"] == 2
        sub.close()

    async def test_fanout_to_multiple_subscribers(self) -> None:
        bus = EventBus()
        s1, s2 = bus.subscribe(), bus.subscribe()
        bus.publish({"type": "y"})
        assert (await _next(s1))["type"] == "y"
        assert (await _next(s2))["type"] == "y"
        s1.close()
        s2.close()

    async def test_publish_never_blocks_or_raises_when_full(self) -> None:
        bus = EventBus(queue_maxsize=2)
        sub = bus.subscribe()
        # Far more than the queue can hold — must not raise/block.
        for i in range(100):
            bus.publish({"type": "step", "i": i})
        # Keep-newest: the most recent event survives.
        drained = [await _next(sub) for _ in range(2)]
        assert any(e["i"] == 99 for e in drained)
        sub.close()

    async def test_latest_snapshot(self) -> None:
        bus = EventBus()
        assert bus.latest_snapshot() is None
        bus.publish({"type": "state", "state": "running"})
        assert bus.latest_snapshot()["state"] == "running"

    async def test_close_ends_iteration(self) -> None:
        bus = EventBus()
        sub = bus.subscribe()
        sub.close()
        with pytest.raises(StopAsyncIteration):
            await _next(sub)

    async def test_closed_sub_not_fed(self) -> None:
        bus = EventBus()
        sub = bus.subscribe()
        sub.close()
        bus.publish({"type": "x"})  # must not touch the closed subscriber
        assert bus.subscriber_count == 0
