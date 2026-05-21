"""Unit tests for the async RPM/RPD throttle."""
from __future__ import annotations

import asyncio

import pytest

from agent.providers.base import QuotaExceeded
from agent.providers.throttle import Throttle


class TestThrottle:
    async def test_acquire_returns_remaining(self) -> None:
        t = Throttle(rpm=3, rpd=10)
        rpm_left, rpd_left = await t.acquire()
        assert rpm_left == 2
        assert rpd_left == 9

    async def test_rpd_exceeded_raises(self) -> None:
        t = Throttle(rpm=100, rpd=2)
        await t.acquire()
        await t.acquire()
        with pytest.raises(QuotaExceeded):
            await t.acquire()

    async def test_rpm_blocks_then_releases(self, monkeypatch) -> None:
        # Use monkeypatched time so this stays fast.
        clock = {"t": 1000.0}

        def fake_monotonic() -> float:
            return clock["t"]

        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock["t"] += s  # advance the fake clock as if we slept

        monkeypatch.setattr("agent.providers.throttle.time.monotonic", fake_monotonic)
        monkeypatch.setattr("agent.providers.throttle.asyncio.sleep", fake_sleep)

        t = Throttle(rpm=2, rpd=100)
        await t.acquire()  # at t=1000
        await t.acquire()  # at t=1000
        # Third acquire must wait ~60s for the first to age out.
        await t.acquire()
        assert sleeps, "expected at least one sleep before the third acquire"
        assert sum(sleeps) >= 60.0

    async def test_concurrent_acquires_serialize(self) -> None:
        t = Throttle(rpm=10, rpd=10)
        results = await asyncio.gather(*(t.acquire() for _ in range(5)))
        # Every caller got a distinct slot (rpm_remaining strictly decreasing).
        rpm_lefts = [r[0] for r in results]
        assert rpm_lefts == sorted(rpm_lefts, reverse=True)
        assert len(set(rpm_lefts)) == 5
