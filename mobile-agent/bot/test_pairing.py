"""Unit tests for the pair-code issuer."""
from __future__ import annotations

import re

from bot.pairing import PAIR_CODE_DIGITS, PairCodeIssuer


class TestPairCodeIssuer:
    def test_code_format(self) -> None:
        iss = PairCodeIssuer(ttl_seconds=300)
        pc = iss.issue(name_hint="alice")
        assert re.fullmatch(r"\d{" + str(PAIR_CODE_DIGITS) + "}", pc.code)
        assert pc.name_hint == "alice"

    def test_redeem_valid(self) -> None:
        iss = PairCodeIssuer(ttl_seconds=300)
        pc = iss.issue()
        redeemed = iss.redeem(pc.code)
        assert redeemed is not None
        assert redeemed.code == pc.code

    def test_redeem_is_single_use(self) -> None:
        iss = PairCodeIssuer(ttl_seconds=300)
        pc = iss.issue()
        assert iss.redeem(pc.code) is not None
        assert iss.redeem(pc.code) is None  # second attempt fails

    def test_redeem_with_no_outstanding_code(self) -> None:
        iss = PairCodeIssuer(ttl_seconds=300)
        assert iss.redeem("123456") is None

    def test_wrong_code(self) -> None:
        iss = PairCodeIssuer(ttl_seconds=300)
        pc = iss.issue()
        wrong = "0" * len(pc.code)
        if wrong == pc.code:
            wrong = "1" * len(pc.code)
        assert iss.redeem(wrong) is None

    def test_expiry_clears_code(self) -> None:
        clock = {"t": 1000.0}
        iss = PairCodeIssuer(ttl_seconds=10)
        iss._set_clock(lambda: clock["t"])
        pc = iss.issue()
        clock["t"] += 11  # past TTL
        assert iss.redeem(pc.code) is None

    def test_time_remaining_after_issue(self) -> None:
        clock = {"t": 500.0}
        iss = PairCodeIssuer(ttl_seconds=60)
        iss._set_clock(lambda: clock["t"])
        iss.issue()
        clock["t"] += 20
        assert iss.time_remaining() == 40
