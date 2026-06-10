"""Unit tests for the cross-app comparison engine."""
from __future__ import annotations

import asyncio

import pytest

from agent.comparison import (
    ComparisonEngine,
    ProbeTarget,
    Quote,
    _coerce_number,
    _eta_minutes,
    _rank,
)
from agent.state_machine import Task, TaskState


class _FakeOrch:
    """Stand-in orchestrator: scripts each package's run_task outcome."""

    def __init__(self, scripted: dict[str, dict]) -> None:
        self._scripted = scripted
        self.calls: list[tuple[str | None, bool, str]] = []

    async def run_task(self, task: Task, *, launch_package=None, read_only=False):
        self.calls.append((launch_package, read_only, task.description))
        outcome = self._scripted.get(launch_package, {})
        task.report = outcome.get("report")
        task.state = outcome.get("state", TaskState.DONE)
        task.final_summary = outcome.get("summary", "")
        task.failure_reason = outcome.get("failure")
        if outcome.get("raise"):
            raise outcome["raise"]
        return task


def _targets() -> list[ProbeTarget]:
    return [
        ProbeTarget("zomato", "Zomato", "com.application.zomato"),
        ProbeTarget("swiggy", "Swiggy", "in.swiggy.android"),
        ProbeTarget("dominos", "Domino's", "com.Dominos"),
    ]


class TestRunSequential:
    async def test_probes_run_read_only_in_order(self) -> None:
        orch = _FakeOrch(
            {
                "com.application.zomato": {"report": {"price": 229}},
                "in.swiggy.android": {"report": {"price": 249}},
                "com.Dominos": {"report": {"price": 289}},
            }
        )
        eng = ComparisonEngine(orch)
        res = await eng.run(
            targets=_targets(), item="pizza", category="food",
            ranking_key="cheapest", user_id=1,
        )
        assert [c[0] for c in orch.calls] == [
            "com.application.zomato", "in.swiggy.android", "com.Dominos",
        ]
        assert all(read_only is True for _, read_only, _ in orch.calls)
        assert "pizza" in orch.calls[0][2]

    async def test_progress_called_per_app(self) -> None:
        orch = _FakeOrch({t.package: {"report": {"price": 10}} for t in _targets()})
        eng = ComparisonEngine(orch)
        seen: list[str] = []

        async def progress(msg: str) -> None:
            seen.append(msg)

        await eng.run(
            targets=_targets(), item="x", category="food",
            ranking_key="cheapest", user_id=1, on_progress=progress,
        )
        assert len(seen) == 3


class TestRanking:
    async def test_cheapest_winner(self) -> None:
        orch = _FakeOrch(
            {
                "com.application.zomato": {"report": {"price": 229, "eta": "30 mins"}},
                "in.swiggy.android": {"report": {"price": "₹249", "eta": "25 min"}},
                "com.Dominos": {"state": TaskState.TIMED_OUT, "failure": "timeout"},
            }
        )
        res = await ComparisonEngine(orch).run(
            targets=_targets(), item="pizza", category="food",
            ranking_key="cheapest", user_id=1,
        )
        assert res.winner.app_id == "zomato"
        assert [q.app_id for q in res.ranked] == ["zomato", "swiggy"]
        # Failed probe is still present in the full quote list.
        assert any(not q.ok for q in res.quotes)

    async def test_fastest_winner(self) -> None:
        orch = _FakeOrch(
            {
                "com.application.zomato": {"report": {"price": 229, "eta": "30 mins"}},
                "in.swiggy.android": {"report": {"price": 249, "eta": "25 min"}},
                "com.Dominos": {"report": {"price": 200, "eta": "40 min"}},
            }
        )
        res = await ComparisonEngine(orch).run(
            targets=_targets(), item="pizza", category="food",
            ranking_key="fastest", user_id=1,
        )
        assert res.winner.app_id == "swiggy"  # 25 min beats 30/40

    async def test_all_probes_fail(self) -> None:
        orch = _FakeOrch(
            {t.package: {"state": TaskState.FAILED, "failure": "nope"} for t in _targets()}
        )
        res = await ComparisonEngine(orch).run(
            targets=_targets(), item="pizza", category="food",
            ranking_key="cheapest", user_id=1,
        )
        assert res.winner is None
        assert res.ranked == []
        assert all(not q.ok for q in res.quotes)

    async def test_unavailable_excluded_from_ranking(self) -> None:
        orch = _FakeOrch(
            {
                "com.application.zomato": {"report": {"available": False, "price": None}},
                "in.swiggy.android": {"report": {"price": 249}},
                "com.Dominos": {"report": {"price": 289}},
            }
        )
        res = await ComparisonEngine(orch).run(
            targets=_targets(), item="pizza", category="food",
            ranking_key="cheapest", user_id=1,
        )
        assert res.winner.app_id == "swiggy"
        assert "zomato" not in {q.app_id for q in res.ranked}


class TestReportMapping:
    async def test_report_fields_map_to_quote(self) -> None:
        orch = _FakeOrch(
            {
                "com.application.zomato": {
                    "report": {
                        "price": "₹229.50", "currency": "INR", "eta": "~30 mins",
                        "available": True, "item_name": "Margherita", "notes": "regular",
                    }
                }
            }
        )
        res = await ComparisonEngine(orch).run(
            targets=[_targets()[0]], item="pizza", category="food",
            ranking_key="cheapest", user_id=1,
        )
        q = res.quotes[0]
        assert q.ok and q.price == 229.5 and q.eta == "~30 mins"
        assert q.item_name == "Margherita" and q.available is True

    async def test_done_without_report_is_soft_no_price(self) -> None:
        orch = _FakeOrch(
            {"p": {"state": TaskState.DONE, "report": None, "summary": "looked around"}}
        )
        res = await ComparisonEngine(orch).run(
            targets=[ProbeTarget("blinkit", "Blinkit", "p")], item="milk",
            category="groceries", ranking_key="cheapest", user_id=1,
        )
        q = res.quotes[0]
        assert q.ok is True and q.price is None  # not a hard failure
        assert res.winner is None  # no price → not rankable


class TestSalvage:
    async def test_salvages_quote_from_summary(self) -> None:
        class _Prov:
            async def complete_text(self, system_prompt, user_prompt, max_tokens=150):
                return '{"price":99,"currency":"INR","available":true,"item_name":"Amul Milk"}'

        orch = _FakeOrch(
            {"p": {"state": TaskState.DONE, "report": None,
                   "summary": "Found Amul milk for about 99 rupees"}}
        )
        eng = ComparisonEngine(orch, salvage_provider=_Prov())
        res = await eng.run(
            targets=[ProbeTarget("blinkit", "Blinkit", "p")], item="milk",
            category="groceries", ranking_key="cheapest", user_id=1,
        )
        assert res.winner is not None and res.winner.price == 99.0


class TestAbort:
    async def test_should_abort_stops_before_next_probe(self) -> None:
        orch = _FakeOrch({t.package: {"report": {"price": 10}} for t in _targets()})
        eng = ComparisonEngine(orch)
        # Abort once the first probe is done.
        state = {"done_one": False}

        def should_abort() -> bool:
            return state["done_one"]

        async def progress(_msg: str) -> None:
            state["done_one"] = True  # flips after the first progress tick

        res = await eng.run(
            targets=_targets(), item="x", category="food", ranking_key="cheapest",
            user_id=1, on_progress=progress, should_abort=should_abort,
        )
        # First probe ran; abort checked before probe #2.
        assert len(orch.calls) == 1
        assert len(res.quotes) == 1

    async def test_cancelled_error_propagates(self) -> None:
        orch = _FakeOrch({"p": {"raise": asyncio.CancelledError()}})
        eng = ComparisonEngine(orch)
        with pytest.raises(asyncio.CancelledError):
            await eng.run(
                targets=[ProbeTarget("blinkit", "Blinkit", "p")], item="milk",
                category="groceries", ranking_key="cheapest", user_id=1,
            )


class TestComparisonPersistence:
    async def test_insert_set_chosen_and_list(self, tmp_path) -> None:
        from agent.persistence import TaskRepository

        repo = TaskRepository(tmp_path / "t.db")
        await repo.initialize()
        cid = await repo.insert_comparison(
            user_id=7, query="milk", category="groceries", ranking_key="cheapest",
            quotes=[{"app_id": "blinkit", "price": 99}, {"app_id": "zepto", "price": 105}],
            winner_app_id="blinkit",
        )
        await repo.set_comparison_chosen(cid, "blinkit")
        rows = await repo.list_recent_comparisons(7)
        assert len(rows) == 1
        row = rows[0]
        assert row.winner_app_id == "blinkit"
        assert row.chosen_app_id == "blinkit"
        assert row.ordered_at is not None
        assert len(row.quotes()) == 2

    async def test_list_scoped_per_user(self, tmp_path) -> None:
        from agent.persistence import TaskRepository

        repo = TaskRepository(tmp_path / "t.db")
        await repo.initialize()
        await repo.insert_comparison(
            user_id=1, query="a", category="food", ranking_key="cheapest",
            quotes=[], winner_app_id=None,
        )
        assert await repo.list_recent_comparisons(2) == []
        assert len(await repo.list_recent_comparisons(1)) == 1


class TestPureHelpers:
    def test_eta_minutes(self) -> None:
        assert _eta_minutes("~30 mins") == 30
        assert _eta_minutes("10-15 min") == 10
        assert _eta_minutes(None) is None
        assert _eta_minutes("soon") is None

    def test_coerce_number(self) -> None:
        assert _coerce_number("₹229") == 229.0
        assert _coerce_number("249.50") == 249.5
        assert _coerce_number(300) == 300.0
        assert _coerce_number(None) is None
        assert _coerce_number("free") is None
        assert _coerce_number(True) is None

    def test_rank_skips_unpriced(self) -> None:
        quotes = [
            Quote("a", "A", ok=True, price=None),
            Quote("b", "B", ok=True, price=50.0),
            Quote("c", "C", ok=False, failure_reason="x"),
        ]
        ranked = _rank(quotes, "cheapest")
        assert [q.app_id for q in ranked] == ["b"]
