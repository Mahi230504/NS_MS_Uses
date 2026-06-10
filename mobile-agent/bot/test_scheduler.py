"""Unit tests for the recurrence math, spec builder, and scheduler loop."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.persistence import ScheduleRepository, ScheduleRow, TaskRepository
from bot.scheduler import (
    Scheduler,
    build_schedule_spec,
    compute_next_run,
    describe_schedule,
    parse_hhmm,
    parse_weekday,
)

IST = "Asia/Kolkata"
# 2026-06-10 08:00 IST == 02:30 UTC (Wednesday)
NOW = datetime(2026, 6, 10, 2, 30, tzinfo=timezone.utc)


class TestParsing:
    def test_parse_hhmm(self) -> None:
        assert parse_hhmm("09:00") == 540
        assert parse_hhmm("14:30") == 870
        assert parse_hhmm("9am") is None
        assert parse_hhmm("25:00") is None
        assert parse_hhmm(None) is None

    def test_parse_weekday(self) -> None:
        assert parse_weekday("Sunday") == 6
        assert parse_weekday("mon") == 0
        assert parse_weekday("nope") is None


class TestComputeNextRun:
    def test_daily_future_today(self) -> None:
        d = compute_next_run(freq="daily", at_minute=540, tz=IST, after=NOW)
        assert d == datetime(2026, 6, 10, 3, 30, tzinfo=timezone.utc)  # 09:00 IST

    def test_daily_past_rolls_tomorrow(self) -> None:
        d = compute_next_run(freq="daily", at_minute=7 * 60, tz=IST, after=NOW)
        assert d == datetime(2026, 6, 11, 1, 30, tzinfo=timezone.utc)  # 07:00 IST +1d

    def test_weekly_next_sunday(self) -> None:
        d = compute_next_run(freq="weekly", at_minute=540, weekday=6, tz=IST, after=NOW)
        local = d.astimezone(ZoneInfo(IST))
        assert local.strftime("%Y-%m-%d %a %H:%M") == "2026-06-14 Sun 09:00"

    def test_monthly_clamps_short_month(self) -> None:
        # Day 31 in June (30 days) clamps to the 30th.
        d = compute_next_run(freq="monthly", at_minute=540, day_of_month=31, tz=IST, after=NOW)
        assert d.astimezone(ZoneInfo(IST)).day == 30

    def test_monthly_past_day_rolls_next_month(self) -> None:
        # Day 1 already passed on the 10th -> July 1.
        d = compute_next_run(freq="monthly", at_minute=540, day_of_month=1, tz=IST, after=NOW)
        local = d.astimezone(ZoneInfo(IST))
        assert (local.month, local.day) == (7, 1)


class TestBuildScheduleSpec:
    def test_weekly_ok(self) -> None:
        spec = build_schedule_spec(
            freq="weekly", time_str="9:00", weekday_name="sunday", tz=IST, now=NOW
        )
        assert spec["at_minute"] == 540 and spec["weekday"] == 6

    def test_weekly_requires_weekday(self) -> None:
        assert build_schedule_spec(
            freq="weekly", time_str="9:00", weekday_name=None, tz=IST, now=NOW
        ) is None

    def test_monthly_bad_day(self) -> None:
        assert build_schedule_spec(
            freq="monthly", time_str="9:00", day_of_month=99, tz=IST, now=NOW
        ) is None

    def test_bad_freq_or_time(self) -> None:
        assert build_schedule_spec(freq="yearly", time_str="9:00", tz=IST, now=NOW) is None
        assert build_schedule_spec(freq="daily", time_str="nope", tz=IST, now=NOW) is None

    def test_describe(self) -> None:
        assert describe_schedule("daily", 540, None, None) == "every day at 09:00"
        assert "Sunday" in describe_schedule("weekly", 540, 6, None)
        assert "day 1" in describe_schedule("monthly", 540, None, 1)


def _row(**kw) -> ScheduleRow:
    base = dict(
        id=1, user_id=1, name="Milk", freq="daily", at_minute=540, weekday=None,
        day_of_month=None, tz=IST, app_id="blinkit", task_id="order", param="milk",
        raw_description="order milk", launch_package="com.grofers.customerapp",
        pay_automatically=0, next_run_at=NOW.isoformat(), last_run_at=None,
        last_state=None, enabled=1, created_at=NOW.isoformat(),
    )
    base.update(kw)
    return ScheduleRow(**base)


class _FakeRepo:
    def __init__(self, rows: list[ScheduleRow]) -> None:
        self.rows = rows
        self.advanced: list[tuple[int, str, bool]] = []

    async def list_due(self, now_iso: str) -> list[ScheduleRow]:
        return [r for r in self.rows if r.enabled and r.next_run_at <= now_iso]

    async def advance(self, sid, *, next_run_at, last_run_at, enabled) -> None:
        self.advanced.append((sid, next_run_at, enabled))
        self.rows = [
            dataclasses.replace(
                r, next_run_at=next_run_at, enabled=1 if enabled else 0,
                last_run_at=last_run_at,
            ) if r.id == sid else r
            for r in self.rows
        ]


class TestSchedulerTick:
    async def test_fires_due_and_advances_to_future(self) -> None:
        repo = _FakeRepo([_row(next_run_at=NOW.isoformat())])
        launched: list[int] = []

        async def launch(row):
            launched.append(row.id)
            return True

        n = await Scheduler(repo, launch, clock=lambda: NOW).tick()
        assert n == 1 and launched == [1]
        # advanced to a strictly future next_run_at, still enabled.
        sid, nxt, enabled = repo.advanced[0]
        assert enabled is True and nxt > NOW.isoformat()
        # No longer due at NOW.
        assert await repo.list_due(NOW.isoformat()) == []

    async def test_once_disables_after_fire(self) -> None:
        repo = _FakeRepo([_row(freq="once", next_run_at=NOW.isoformat())])

        async def launch(row):
            return True

        await Scheduler(repo, launch, clock=lambda: NOW).tick()
        assert repo.advanced[0][2] is False  # disabled
        assert repo.rows[0].enabled == 0

    async def test_advances_before_launch_even_if_launch_fails(self) -> None:
        repo = _FakeRepo([_row()])

        async def boom(row):
            raise RuntimeError("launch failed")

        # tick must not raise; advance happened before the failing launch.
        await Scheduler(repo, boom, clock=lambda: NOW).tick()
        assert repo.advanced and await repo.list_due(NOW.isoformat()) == []

    async def test_catch_up_fires_once_not_repeatedly(self) -> None:
        # next_run_at far in the past -> fires once, jumps to a single future slot.
        past = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc).isoformat()
        repo = _FakeRepo([_row(next_run_at=past)])
        count = {"n": 0}

        async def launch(row):
            count["n"] += 1
            return True

        await Scheduler(repo, launch, clock=lambda: NOW).tick()
        assert count["n"] == 1
        assert repo.advanced[0][1] > NOW.isoformat()  # future


class TestScheduleRepository:
    async def test_round_trip_and_due(self, tmp_path) -> None:
        base = TaskRepository(tmp_path / "t.db")
        await base.initialize()
        repo = ScheduleRepository(tmp_path / "t.db")
        sid = await repo.insert(
            user_id=1, name="Milk", freq="daily", at_minute=540, tz=IST,
            raw_description="order milk", next_run_at=NOW.isoformat(),
            app_id="blinkit", task_id="order", param="milk",
            launch_package="com.grofers.customerapp", pay_automatically=True,
        )
        due = await repo.list_due(NOW.isoformat())
        assert len(due) == 1 and due[0].pay_automatically == 1
        # Advance past now -> no longer due.
        future = datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat()
        await repo.advance(sid, next_run_at=future, last_run_at=NOW.isoformat())
        assert await repo.list_due(NOW.isoformat()) == []
        assert len(await repo.list_for(1)) == 1
        assert await repo.delete(1, sid) is True
        assert await repo.get(sid) is None
