"""Async RPM/RPD throttle for free-tier providers."""
from __future__ import annotations

import asyncio
import time
from collections import deque

from agent.providers.base import QuotaExceeded


class Throttle:
    """Rolling-window throttle: blocks on RPM, raises on RPD.

    `acquire()` is the only public API. It blocks until a slot is available
    under the requests-per-minute ceiling, or raises QuotaExceeded if the
    requests-per-day ceiling is hit (which is not survivable in this session).
    """

    def __init__(self, rpm: int, rpd: int) -> None:
        self._rpm = rpm
        self._rpd = rpd
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> tuple[int, int]:
        """Reserve a slot. Returns (rpm_remaining, rpd_remaining) after reservation."""
        async with self._lock:
            now = time.monotonic()
            self._drain(now)

            if len(self._day) >= self._rpd:
                raise QuotaExceeded(
                    f"Daily quota exhausted ({self._rpd} requests/day). "
                    "Try again tomorrow or upgrade the provider tier."
                )

            while len(self._minute) >= self._rpm:
                wait_for = max(0.0, self._minute[0] + 60.0 - now) + 0.05
                await asyncio.sleep(wait_for)
                now = time.monotonic()
                self._drain(now)

            self._minute.append(now)
            self._day.append(now)
            return (
                self._rpm - len(self._minute),
                self._rpd - len(self._day),
            )

    def _drain(self, now: float) -> None:
        while self._minute and now - self._minute[0] > 60.0:
            self._minute.popleft()
        while self._day and now - self._day[0] > 86_400.0:
            self._day.popleft()
