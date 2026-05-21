"""In-memory pair-code issuer with TTL.

Admin issues a 6-digit code via /issue_pair_code; the new user redeems it via
/pair <code> within the TTL window. Codes are single-use and never persisted.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


PAIR_CODE_TTL_SECONDS = 5 * 60
PAIR_CODE_DIGITS = 6


@dataclass(frozen=True)
class PairCode:
    code: str
    issued_at: float  # time.monotonic()
    name_hint: str    # human label shown when /users lists the entry


class PairCodeIssuer:
    """One outstanding code at a time (single-admin assumption)."""

    def __init__(self, ttl_seconds: float = PAIR_CODE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._current: PairCode | None = None
        self._now = time.monotonic  # injectable for tests

    def issue(self, name_hint: str = "") -> PairCode:
        code = "".join(secrets.choice("0123456789") for _ in range(PAIR_CODE_DIGITS))
        self._current = PairCode(code=code, issued_at=self._now(), name_hint=name_hint)
        return self._current

    def redeem(self, code: str) -> PairCode | None:
        """Return the code metadata if `code` matches and hasn't expired. Else None.

        Successful redeem consumes the code (single-use).
        """
        current = self._current
        if current is None:
            return None
        if self._now() - current.issued_at > self._ttl:
            self._current = None
            return None
        # constant-time compare to avoid trivial timing leaks
        if not secrets.compare_digest(current.code, code):
            return None
        self._current = None
        return current

    def time_remaining(self) -> float:
        if self._current is None:
            return 0.0
        return max(0.0, self._ttl - (self._now() - self._current.issued_at))

    # Test hook: override the monotonic source.
    def _set_clock(self, fn) -> None:
        self._now = fn
