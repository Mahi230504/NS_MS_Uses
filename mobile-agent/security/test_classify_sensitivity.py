"""Tests for HitlGate.classify_sensitivity (vision-augmented HITL + cache)."""
from __future__ import annotations

import pytest

from security.hitl_gate import HitlGate


class _FakeClassifier:
    def __init__(self, verdicts: list[bool] | bool = True) -> None:
        self._verdicts = (
            [verdicts] if isinstance(verdicts, bool) else list(verdicts)
        )
        self.calls = 0
        self.fail = False

    async def classify_yes_no(self, screenshot_bytes: bytes, question: str) -> bool:
        self.calls += 1
        if self.fail:
            raise RuntimeError("classify failure")
        if not self._verdicts:
            return False
        return self._verdicts.pop(0)


class TestClassifySensitivity:
    async def test_cache_hit_skips_provider(self) -> None:
        gate = HitlGate()
        clf = _FakeClassifier(True)
        ph = "deadbeef"
        assert await gate.classify_sensitivity(b"x", ph, clf) is True
        # Second call with same phash → cached, no new provider call.
        assert await gate.classify_sensitivity(b"x", ph, clf) is True
        assert clf.calls == 1

    async def test_distinct_phashes_each_query_once(self) -> None:
        gate = HitlGate()
        clf = _FakeClassifier([True, False])
        assert await gate.classify_sensitivity(b"x", "a" * 16, clf) is True
        assert await gate.classify_sensitivity(b"y", "b" * 16, clf) is False
        assert clf.calls == 2

    async def test_classifier_failure_returns_false(self) -> None:
        gate = HitlGate()
        clf = _FakeClassifier(True)
        clf.fail = True
        assert await gate.classify_sensitivity(b"x", "ph", clf) is False
        # Failure is NOT cached — next call retries.
        clf.fail = False
        assert await gate.classify_sensitivity(b"x", "ph", clf) is True
        assert clf.calls == 2
