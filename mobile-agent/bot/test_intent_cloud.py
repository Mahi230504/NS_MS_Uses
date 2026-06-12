"""Unit tests for the cloud-action intents (email / meeting / cloud schedule)."""
from __future__ import annotations

from datetime import datetime, timezone

from bot.intent import (
    EmailIntent,
    IntentClassifier,
    IntentKind,
    MeetingIntent,
    ScheduleIntent,
    SingleIntent,
    UnknownIntent,
)
from bot.router import Router


class _FakeProvider:
    """Returns canned replies in order (last repeats); records calls."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies) or [""]
        self.calls: list[str] = []
        self.max_tokens: list[int] = []

    async def complete_text(
        self, system_prompt: str, user_prompt: str, max_tokens: int = 200
    ) -> str:
        self.calls.append(user_prompt)
        self.max_tokens.append(max_tokens)
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._replies[idx]


# A fixed UTC instant that is 2026-06-11T00:00 Thursday in Asia/Kolkata.
def _utc_clock() -> datetime:
    return datetime(2026, 6, 10, 18, 30, tzinfo=timezone.utc)


def _classifier(*replies: str) -> tuple[IntentClassifier, _FakeProvider]:
    provider = _FakeProvider(*replies)
    c = IntentClassifier(provider, Router(provider), clock=_utc_clock)
    return c, provider


class TestEmailIntent:
    async def test_email_with_names_and_addresses(self) -> None:
        c, _ = _classifier(
            '{"intent":"email","email_to":["alice@x.com","ayush"],'
            '"email_subject":"Demo ready","email_body":"The demo is ready."}'
        )
        r = await c.classify("email alice@x.com and ayush that the demo is ready")
        assert isinstance(r, EmailIntent)
        assert r.kind is IntentKind.EMAIL
        # Names pass through verbatim — contact resolution happens downstream.
        assert r.to == ("alice@x.com", "ayush")
        assert r.subject == "Demo ready"
        assert r.body == "The demo is ready."

    async def test_subject_derived_from_body(self) -> None:
        c, _ = _classifier(
            '{"intent":"email","email_to":["bob"],"email_subject":null,'
            '"email_body":"The demo is ready for tomorrow\'s review and the '
            'deck is attached here"}'
        )
        r = await c.classify("email bob the demo update")
        assert isinstance(r, EmailIntent)
        assert r.subject == "The demo is ready for tomorrow's review and the deck is"
        assert len(r.subject) <= 60

    async def test_email_without_recipients_falls_back_to_router(self) -> None:
        # Builder fails (no recipients) → the proven router gets a shot and
        # parses its own contract shape from the second canned reply.
        c, _ = _classifier(
            '{"intent":"email","email_to":[],"email_subject":"hi","email_body":"x"}',
            '{"app_id":"spotify","task_id":"play","param":"jazz"}',
        )
        r = await c.classify("email nobody")
        assert isinstance(r, SingleIntent)
        assert r.route.app.id == "spotify"

    async def test_email_without_subject_or_body_degrades(self) -> None:
        c, _ = _classifier(
            '{"intent":"email","email_to":["alice"],"email_subject":null,'
            '"email_body":null}',
            "no json here either",
        )
        r = await c.classify("email alice")
        assert isinstance(r, UnknownIntent)


class TestMeetingIntent:
    async def test_meeting_with_resolved_relative_time(self) -> None:
        # The model resolved "tomorrow 3pm" against the Now line (Jun 11).
        c, _ = _classifier(
            '{"intent":"meeting","meeting_title":"Launch sync",'
            '"meeting_attendees":["ayush","alice@x.com"],'
            '"meeting_start":"2026-06-12 15:00","meeting_duration_minutes":null}'
        )
        r = await c.classify("schedule a meet with ayush tomorrow at 3pm about the launch")
        assert isinstance(r, MeetingIntent)
        assert r.kind is IntentKind.MEETING
        assert r.title == "Launch sync"
        assert r.attendees == ("ayush", "alice@x.com")
        assert r.start_local == "2026-06-12 15:00"
        assert r.duration_minutes == 30  # default when unstated

    async def test_meeting_explicit_duration(self) -> None:
        c, _ = _classifier(
            '{"intent":"meeting","meeting_title":"Retro","meeting_attendees":[],'
            '"meeting_start":"2026-06-15 10:00","meeting_duration_minutes":45}'
        )
        r = await c.classify("45 minute retro monday 10am")
        assert isinstance(r, MeetingIntent)
        assert r.duration_minutes == 45 and r.attendees == ()

    async def test_meeting_unresolved_start_falls_back_to_router(self) -> None:
        # "tomorrow 3pm" is not "YYYY-MM-DD HH:MM" → builder fails → router.
        c, _ = _classifier(
            '{"intent":"meeting","meeting_title":"Sync",'
            '"meeting_attendees":["ayush"],"meeting_start":"tomorrow 3pm"}',
            '{"app_id":null,"task_id":null,"param":null}',
        )
        r = await c.classify("meet ayush tomorrow 3pm")
        assert isinstance(r, UnknownIntent)


class TestCloudSchedule:
    async def test_scheduled_email_one_off(self) -> None:
        c, _ = _classifier(
            '{"intent":"schedule","app_ids":[],"task_id":null,'
            '"schedule_freq":"once","schedule_time":"08:00",'
            '"schedule_date":"2026-06-12","email_to":["team@x.co","ayush"],'
            '"email_subject":"Standup notes","email_body":"Notes attached."}'
        )
        r = await c.classify("tomorrow 8am email the team the standup notes")
        assert isinstance(r, ScheduleIntent)
        assert r.route is None
        assert r.action_kind == "email"
        assert isinstance(r.payload, EmailIntent)
        assert r.payload.to == ("team@x.co", "ayush")
        assert r.payload.subject == "Standup notes"
        assert r.freq == "once" and r.time_str == "08:00"
        assert r.date_str == "2026-06-12"

    async def test_scheduled_email_recurring(self) -> None:
        c, _ = _classifier(
            '{"intent":"schedule","app_ids":[],'
            '"schedule_freq":"weekly","schedule_time":"17:00",'
            '"schedule_weekday":"friday","schedule_date":null,'
            '"email_to":["alice"],"email_subject":"Weekly report",'
            '"email_body":"Report for the week."}'
        )
        r = await c.classify("every friday 5pm email alice the weekly report")
        assert isinstance(r, ScheduleIntent)
        assert r.action_kind == "email" and r.route is None
        assert r.weekday_name == "friday" and r.date_str is None

    async def test_device_schedule_regression(self) -> None:
        # The existing device path is untouched: route filled, no cloud fields.
        c, _ = _classifier(
            '{"intent":"schedule","app_ids":["blinkit"],"task_id":"order",'
            '"param":"milk","schedule_freq":"daily","schedule_time":"09:00",'
            '"pay_automatically":false}'
        )
        r = await c.classify("order milk on blinkit every day at 9am")
        assert isinstance(r, ScheduleIntent)
        assert r.route is not None and r.route.app.id == "blinkit"
        assert r.action_kind == "device"
        assert r.payload is None and r.date_str is None

    async def test_device_schedule_threads_date_str(self) -> None:
        c, _ = _classifier(
            '{"intent":"schedule","app_ids":["blinkit"],"task_id":"order",'
            '"param":"milk","schedule_freq":"once","schedule_time":"08:00",'
            '"schedule_date":"2026-06-15"}'
        )
        r = await c.classify("order milk on blinkit monday at 8am")
        assert isinstance(r, ScheduleIntent)
        assert r.action_kind == "device" and r.date_str == "2026-06-15"


class TestNowContext:
    async def test_now_line_in_prompt(self) -> None:
        c, provider = _classifier('{"intent":"unknown","app_ids":[]}')
        await c.classify("what's the weather")
        assert provider.calls[0].startswith(
            "Now: 2026-06-11T00:00 (Asia/Kolkata); today is Thursday."
        )

    async def test_classify_uses_450_tokens(self) -> None:
        c, provider = _classifier('{"intent":"unknown","app_ids":[]}')
        await c.classify("hello")
        assert provider.max_tokens[0] == 450

    async def test_custom_tz(self) -> None:
        provider = _FakeProvider('{"intent":"unknown","app_ids":[]}')
        c = IntentClassifier(
            provider, Router(provider), tz="UTC", clock=_utc_clock
        )
        await c.classify("hello")
        assert provider.calls[0].startswith(
            "Now: 2026-06-10T18:30 (UTC); today is Wednesday."
        )
