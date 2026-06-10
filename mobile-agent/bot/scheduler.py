"""In-process scheduler for recurring/scheduled tasks (feature #5).

A hand-rolled async loop (no APScheduler dependency) that wakes every minute,
fires due schedules, and advances each one's absolute-UTC `next_run_at`. It is
restart-safe because SQLite holds the only state: on startup the existing rows
are simply due or not. It calls a single launcher callback (Handlers.
launch_scheduled), inheriting the one-task-per-user guard and the done-callback.

The recurrence math is pure and timezone-aware: schedules are expressed in the
user's local tz (e.g. Asia/Kolkata) and `next_run_at` is the absolute UTC
instant of the next occurrence. `compute_next_run` and `build_schedule_spec`
have no I/O so they're trivially testable with an injected clock.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

_log = logging.getLogger("mobile_agent.scheduler")

VALID_FREQ = frozenset({"once", "daily", "weekly", "monthly"})
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


def parse_weekday(name: object) -> int | None:
    if name is None:
        return None
    return _WEEKDAYS.get(str(name).strip().lower())


def parse_hhmm(value: object) -> int | None:
    """'HH:MM' (or 'H:MM') -> minutes since midnight, or None if invalid."""
    if value is None:
        return None
    s = str(value).strip()
    if ":" not in s:
        return None
    hh, _, mm = s.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _local_at(year: int, month: int, day: int, at_minute: int, zi: ZoneInfo) -> datetime:
    hh, mm = divmod(at_minute, 60)
    return datetime(year, month, day, hh, mm, tzinfo=zi)


def compute_next_run(
    *,
    freq: str,
    at_minute: int,
    tz: str,
    after: datetime,
    weekday: int | None = None,
    day_of_month: int | None = None,
) -> datetime:
    """Next UTC datetime strictly after `after` matching the recurrence.

    `after` must be timezone-aware. The recurrence is evaluated in `tz` (local
    wall-clock), then converted to UTC. For 'once' this returns the next
    occurrence of the time-of-day (today if still future, else tomorrow) — the
    scheduler disables a one-shot after it fires.
    """
    zi = ZoneInfo(tz)
    local = after.astimezone(zi)

    if freq == "weekly":
        wd = weekday if weekday is not None else local.weekday()
        days_ahead = (wd - local.weekday()) % 7
        cand = _local_at(local.year, local.month, local.day, at_minute, zi) + timedelta(days=days_ahead)
        if cand <= local:
            cand = cand + timedelta(days=7)
        return cand.astimezone(timezone.utc)

    if freq == "monthly":
        dom = day_of_month or local.day
        y, m = local.year, local.month
        cand = _local_at(y, m, min(dom, calendar.monthrange(y, m)[1]), at_minute, zi)
        if cand <= local:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            cand = _local_at(y, m, min(dom, calendar.monthrange(y, m)[1]), at_minute, zi)
        return cand.astimezone(timezone.utc)

    # daily / once
    cand = _local_at(local.year, local.month, local.day, at_minute, zi)
    if cand <= local:
        cand = cand + timedelta(days=1)
    return cand.astimezone(timezone.utc)


def build_schedule_spec(
    *,
    freq: str,
    time_str: object,
    tz: str,
    now: datetime,
    weekday_name: object = None,
    day_of_month: object = None,
) -> dict | None:
    """Validate + normalize a natural-language schedule into storable fields.

    Returns {at_minute, weekday, day_of_month, next_run_at} (next_run_at is a
    UTC ISO string), or None if the inputs are invalid/incomplete. Does NO
    natural-language parsing of its own — the LLM supplies freq/time/weekday/
    day_of_month; this only validates and does the calendar/UTC math.
    """
    freq = (freq or "").strip().lower()
    if freq not in VALID_FREQ:
        return None
    at_minute = parse_hhmm(time_str)
    if at_minute is None:
        return None

    weekday: int | None = None
    if freq == "weekly":
        weekday = parse_weekday(weekday_name)
        if weekday is None:
            return None

    dom: int | None = None
    if freq == "monthly":
        try:
            dom = int(day_of_month)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not (1 <= dom <= 31):
            return None

    try:
        nxt = compute_next_run(
            freq=freq, at_minute=at_minute, tz=tz, after=now,
            weekday=weekday, day_of_month=dom,
        )
    except Exception:
        return None
    return {
        "at_minute": at_minute,
        "weekday": weekday,
        "day_of_month": dom,
        "next_run_at": nxt.isoformat(),
    }


def describe_schedule(freq: str, at_minute: int, weekday: int | None,
                      day_of_month: int | None) -> str:
    """Human phrase like 'every day at 09:00' for confirmations."""
    hh, mm = divmod(at_minute, 60)
    t = f"{hh:02d}:{mm:02d}"
    if freq == "daily":
        return f"every day at {t}"
    if freq == "weekly":
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
        wd = names[weekday] if weekday is not None and 0 <= weekday < 7 else "?"
        return f"every {wd} at {t}"
    if freq == "monthly":
        return f"on day {day_of_month} each month at {t}"
    return f"once at {t}"


LauncherCb = Callable[[object], Awaitable[bool]]


class Scheduler:
    """Minute-resolution async scheduler. Started on the bot's event loop."""

    def __init__(
        self,
        repo,
        launcher: LauncherCb,
        *,
        tick_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._launch = launcher
        self._tick_seconds = tick_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            _log.info("scheduler started (tick=%ss)", self._tick_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("scheduler tick failed")
            await asyncio.sleep(self._tick_seconds)

    async def tick(self) -> int:
        """Fire all due schedules once. Returns how many were fired."""
        now = self._clock()
        due = await self._repo.list_due(now.isoformat())
        fired = 0
        for row in due:
            # Advance next_run_at BEFORE launching so a crash or a launch
            # failure can't refire the same slot — missed runs fire at most
            # once (we jump straight to the next future occurrence).
            last_run = now.isoformat()
            if row.freq == "once":
                await self._repo.advance(
                    row.id, next_run_at=row.next_run_at,
                    last_run_at=last_run, enabled=False,
                )
            else:
                try:
                    nxt = compute_next_run(
                        freq=row.freq, at_minute=row.at_minute, tz=row.tz,
                        after=now, weekday=row.weekday,
                        day_of_month=row.day_of_month,
                    )
                    next_iso = nxt.isoformat()
                except Exception:
                    _log.exception("compute_next_run failed for schedule %s", row.id)
                    next_iso = row.next_run_at
                await self._repo.advance(
                    row.id, next_run_at=next_iso, last_run_at=last_run,
                    enabled=True,
                )
            try:
                await self._launch(row)
                fired += 1
            except Exception:
                _log.exception("scheduled launch failed for schedule %s", row.id)
        return fired
