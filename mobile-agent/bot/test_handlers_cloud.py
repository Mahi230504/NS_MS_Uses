"""Tests for cloud-action wiring in the handlers (Gmail send / Meet invites)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from agent.persistence import ContactRow, ScheduleRepository, TaskRepository
from agent.state_machine import Task, TaskState
from bot.handlers import Handlers
from bot.intent import EmailIntent, MeetingIntent, ScheduleIntent
from services.google_auth import GoogleAuthError
from services.google_workspace import CloudActionError

IST = "Asia/Kolkata"


class _FakeClassifier:
    def __init__(self, intent):
        self._intent = intent

    async def classify(self, text):
        return self._intent


class _FakeGmail:
    """Records sends; raises the canned exception instead when given one."""

    def __init__(self, raises: Exception | None = None):
        self.sent: list[dict] = []
        self._raises = raises

    async def send_email(self, *, to, subject, body):
        if self._raises is not None:
            raise self._raises
        self.sent.append({"to": list(to), "subject": subject, "body": body})
        return {"id": "m1", "thread_id": "t1", "to": list(to), "subject": subject}


class _FakeGcal:
    """Records create_meeting calls; canned result/raise like _FakeGmail."""

    def __init__(self, meet_link="https://meet.google.com/abc-defg-hij",
                 raises: Exception | None = None):
        self.created: list[dict] = []
        self._meet_link = meet_link
        self._raises = raises

    async def create_meeting(self, *, title, start_local, duration_minutes,
                             tz, attendees, description=""):
        if self._raises is not None:
            raise self._raises
        self.created.append({
            "title": title, "start_local": start_local,
            "duration_minutes": duration_minutes, "tz": tz,
            "attendees": list(attendees), "description": description,
        })
        return {
            "event_id": "e1", "meet_link": self._meet_link,
            "html_link": "https://calendar.google.com/x",
            "start_iso": "2026-06-12T15:00:00+05:30",
            "end_iso": "2026-06-12T15:30:00+05:30",
        }


class _FakeCloudRepo:
    def __init__(self):
        self.rows: list[dict] = []

    async def insert(self, *, user_id, kind, payload, result, status,
                     error=None):
        self.rows.append({
            "user_id": user_id, "kind": kind, "payload": payload,
            "result": result, "status": status, "error": error,
        })
        return len(self.rows)


class _FakeContacts:
    """In-memory name → email book mirroring ContactRepository semantics."""

    def __init__(self, book: dict[str, str] | None = None):
        self.book = {k.strip().lower(): v for k, v in (book or {}).items()}

    async def upsert(self, *, user_id, name, email):
        self.book[name.strip().lower()] = email

    async def get(self, user_id, name):
        email = self.book.get(name.strip().lower())
        if email is None:
            return None
        return ContactRow(
            id=1, user_id=user_id, name=name.strip().lower(), email=email,
            created_at="2026-01-01T00:00:00+00:00",
        )

    async def list_for(self, user_id):
        return [
            ContactRow(id=i, user_id=user_id, name=n, email=e,
                       created_at="2026-01-01T00:00:00+00:00")
            for i, (n, e) in enumerate(sorted(self.book.items()), start=1)
        ]

    async def delete(self, user_id, name):
        return self.book.pop(name.strip().lower(), None) is not None


class _FakeDirectory:
    """In-memory Workspace directory: query → list of (displayName, email)."""

    def __init__(self, book: dict[str, list] | None = None,
                 raises: Exception | None = None):
        self.book = {k.strip().lower(): v for k, v in (book or {}).items()}
        self._raises = raises
        self.calls: list[str] = []

    async def resolve_name(self, name):
        from services.google_workspace import DirectoryMatch

        self.calls.append(name)
        if self._raises is not None:
            raise self._raises
        return [
            DirectoryMatch(name=d, email=e)
            for d, e in self.book.get(name.strip().lower(), [])
        ]


def _email_intent(to=("a@x.com",), subject="Demo ready",
                  body="The demo is ready."):
    return EmailIntent(to=to, subject=subject, body=body)


def _meeting_intent(attendees=("ayush",)):
    return MeetingIntent(
        title="Launch sync", attendees=attendees,
        start_local="2026-06-12 15:00",
    )


def _email_schedule_intent(to=("ayush",)):
    return ScheduleIntent(
        route=None, freq="daily", time_str="08:00", action_kind="email",
        payload=EmailIntent(to=to, subject="Standup notes", body="Notes here."),
    )


async def _setup(tmp_path, *, intent=None, gmail=None, gcal=None,
                 cloud_repo=None, contacts=None, directory=None):
    base = TaskRepository(tmp_path / "t.db")
    await base.initialize()
    sched = ScheduleRepository(tmp_path / "t.db")
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    orch = MagicMock()
    orch.run_task = AsyncMock(
        return_value=Task(user_id=42, description="x", state=TaskState.DONE)
    )
    users = MagicMock()
    users.is_allowed.return_value = True
    h = Handlers(
        application=app, orchestrator=orch, hitl=MagicMock(), users=users,
        pairing=MagicMock(), admin_id=1, repo=base,
        classifier=_FakeClassifier(intent) if intent is not None else None,
        schedules=sched, timezone_name=IST,
        gmail=gmail, gcal=gcal, cloud_repo=cloud_repo, contacts=contacts,
        directory=directory,
    )
    return h, app, orch, sched


def _update(user_id, text):
    u = MagicMock()
    u.effective_user.id = user_id
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


async def _drain():
    """Let a fire-and-forget cloud task run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


def _sent_texts(app) -> list[str]:
    return [c.kwargs.get("text", "") for c in app.bot.send_message.call_args_list]


class TestTypedEmail:
    async def test_typed_email_sends_immediately(self, tmp_path) -> None:
        gmail, repo = _FakeGmail(), _FakeCloudRepo()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail, cloud_repo=repo,
        )
        upd = _update(42, "email a@x.com that the demo is ready")
        await h.message(upd, MagicMock())
        assert gmail.sent == [{
            "to": ["a@x.com"], "subject": "Demo ready",
            "body": "The demo is ready.",
        }]
        reply = upd.message.reply_text.call_args.args[0]
        assert reply == '📧 Sent "Demo ready" to a@x.com.'
        assert "Confirm?" not in reply
        # Logged as history with status ok.
        assert repo.rows[0]["kind"] == "email" and repo.rows[0]["status"] == "ok"
        # Never touches the device slot.
        orch.run_task.assert_not_called()

    async def test_contact_names_resolved(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Ayush", "b@x.com")),
            gmail=gmail, contacts=_FakeContacts({"ayush": "ayush@x.com"}),
        )
        await h.message(_update(42, "email ayush and b@x.com"), MagicMock())
        assert gmail.sent[0]["to"] == ["ayush@x.com", "b@x.com"]

    async def test_missing_contact_line(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("ayush",)),
            gmail=gmail, contacts=_FakeContacts(),
        )
        upd = _update(42, "email ayush hi")
        await h.message(upd, MagicMock())
        reply = upd.message.reply_text.call_args.args[0]
        assert "I don't have an email for: ayush" in reply
        assert "/contact add" in reply
        assert gmail.sent == []


class TestDirectoryResolution:
    async def test_directory_resolves_name_not_in_local_book(self, tmp_path) -> None:
        gmail = _FakeGmail()
        directory = _FakeDirectory({"mohan": [("Mohan S", "mohan@ns.co")]})
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Mohan",)),
            gmail=gmail, contacts=_FakeContacts(), directory=directory,
        )
        await h.message(_update(42, "email mohan hi"), MagicMock())
        assert gmail.sent[0]["to"] == ["mohan@ns.co"]
        assert directory.calls == ["Mohan"]

    async def test_directory_hit_is_cached_locally(self, tmp_path) -> None:
        gmail = _FakeGmail()
        contacts = _FakeContacts()
        directory = _FakeDirectory({"mohan": [("Mohan S", "mohan@ns.co")]})
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Mohan",)),
            gmail=gmail, contacts=contacts, directory=directory,
        )
        await h.message(_update(42, "email mohan hi"), MagicMock())
        # The resolved address is written back into the local book under the
        # spoken name, so a later lookup is offline/instant.
        row = await contacts.get(42, "mohan")
        assert row is not None and row.email == "mohan@ns.co"

    async def test_local_contact_short_circuits_directory(self, tmp_path) -> None:
        gmail = _FakeGmail()
        directory = _FakeDirectory({"ayush": [("Ayush X", "wrong@ns.co")]})
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Ayush",)),
            gmail=gmail, contacts=_FakeContacts({"ayush": "ayush@x.com"}),
            directory=directory,
        )
        await h.message(_update(42, "email ayush hi"), MagicMock())
        assert gmail.sent[0]["to"] == ["ayush@x.com"]
        assert directory.calls == []  # never consulted — local book won

    async def test_ambiguous_directory_match_reports_missing(self, tmp_path) -> None:
        gmail = _FakeGmail()
        directory = _FakeDirectory({"mohan": [
            ("Mohan Shrivastava", "ms@ns.co"),
            ("Mohan Kumar", "mk@ns.co"),
        ]})
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Mohan",)),
            gmail=gmail, contacts=_FakeContacts(), directory=directory,
        )
        upd = _update(42, "email mohan hi")
        await h.message(upd, MagicMock())
        assert gmail.sent == []
        assert "I don't have an email for: Mohan" in upd.message.reply_text.call_args.args[0]

    async def test_exact_name_among_many_resolves(self, tmp_path) -> None:
        gmail = _FakeGmail()
        directory = _FakeDirectory({"mohan": [
            ("Mohan Shrivastava", "ms@ns.co"),
            ("Mohan", "exact@ns.co"),
        ]})
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Mohan",)),
            gmail=gmail, contacts=_FakeContacts(), directory=directory,
        )
        await h.message(_update(42, "email mohan hi"), MagicMock())
        assert gmail.sent[0]["to"] == ["exact@ns.co"]

    async def test_directory_error_degrades_to_missing(self, tmp_path) -> None:
        gmail = _FakeGmail()
        directory = _FakeDirectory(raises=CloudActionError("Directory not shared"))
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(to=("Mohan",)),
            gmail=gmail, contacts=_FakeContacts(), directory=directory,
        )
        upd = _update(42, "email mohan hi")
        await h.message(upd, MagicMock())
        assert gmail.sent == []
        assert "I don't have an email for: Mohan" in upd.message.reply_text.call_args.args[0]

    async def test_gmail_not_configured(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path, intent=_email_intent())
        upd = _update(42, "email a@x.com hi")
        await h.message(upd, MagicMock())
        reply = upd.message.reply_text.call_args.args[0]
        assert reply == (
            "Email isn't set up — connect Google from the dashboard "
            "Settings page."
        )

    async def test_auth_error_returns_friendly_line(self, tmp_path) -> None:
        gmail = _FakeGmail(raises=GoogleAuthError("Google isn't connected."))
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail,
        )
        upd = _update(42, "email a@x.com hi")
        await h.message(upd, MagicMock())
        assert upd.message.reply_text.call_args.args[0] == "Google isn't connected."

    async def test_send_failure_logged_as_error(self, tmp_path) -> None:
        repo = _FakeCloudRepo()
        gmail = _FakeGmail(raises=CloudActionError("Gmail said no (403)"))
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail, cloud_repo=repo,
        )
        upd = _update(42, "email a@x.com hi")
        await h.message(upd, MagicMock())
        reply = upd.message.reply_text.call_args.args[0]
        assert "Couldn't send the email: Gmail said no (403)" in reply
        assert repo.rows[0]["status"] == "error"


class TestTypedMeeting:
    async def test_typed_meeting_creates_event(self, tmp_path) -> None:
        gcal = _FakeGcal()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_meeting_intent(), gcal=gcal,
            contacts=_FakeContacts({"ayush": "ayush@x.com"}),
        )
        upd = _update(42, "schedule a meet with ayush tomorrow at 3pm")
        await h.message(upd, MagicMock())
        assert gcal.created[0]["attendees"] == ["ayush@x.com"]
        assert gcal.created[0]["tz"] == IST
        reply = upd.message.reply_text.call_args.args[0]
        assert reply.startswith("📅 Meet scheduled: Launch sync — ")
        assert "15:00" in reply
        assert "Invite sent to 1 people." in reply
        assert "https://meet.google.com/abc-defg-hij" in reply

    async def test_meet_link_omitted_when_absent(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(
            tmp_path, intent=_meeting_intent(attendees=()),
            gcal=_FakeGcal(meet_link=""),
        )
        upd = _update(42, "meet tomorrow 3pm")
        await h.message(upd, MagicMock())
        reply = upd.message.reply_text.call_args.args[0]
        assert "meet.google.com" not in reply
        assert "Invite sent" not in reply  # no attendees

    async def test_gcal_not_configured(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path, intent=_meeting_intent())
        upd = _update(42, "meet ayush tomorrow 3pm")
        await h.message(upd, MagicMock())
        assert "Meetings aren't set up" in upd.message.reply_text.call_args.args[0]


class TestVoiceStaging:
    async def test_voice_stages_without_sending(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail,
        )
        msg = await h.handle_external_trigger(42, "email a@x.com the demo is ready")
        # The literal "Confirm?" is the phone client's prompt token.
        assert msg.endswith("Confirm?")
        assert 'Email to a@x.com — "Demo ready"' in msg
        assert gmail.sent == []
        assert h._pending[42].cloud is not None

    async def test_confirm_executes_and_notifies(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail,
        )
        await h.handle_external_trigger(42, "email a@x.com the demo is ready")
        msg = await h.handle_external_confirm(42, True)
        assert msg == "On it — I'll confirm on Telegram."
        assert "Confirm?" not in msg
        await _drain()
        assert len(gmail.sent) == 1
        # The result line lands on Telegram — and never re-triggers the
        # phone's confirm prompt.
        result_lines = [t for t in _sent_texts(app) if t.startswith("📧")]
        assert result_lines == ['📧 Sent "Demo ready" to a@x.com.']
        assert all("Confirm?" not in t for t in result_lines)

    async def test_deny_cancels(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail,
        )
        await h.handle_external_trigger(42, "email a@x.com the demo is ready")
        msg = await h.handle_external_confirm(42, False)
        assert msg == "Okay, cancelled."
        await _drain()
        assert gmail.sent == []

    async def test_confirm_bypasses_running_guard(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_intent(), gmail=gmail,
        )
        await h.handle_external_trigger(42, "email a@x.com the demo is ready")
        # A device task starts mid-confirm — the cloud action must not be
        # blocked by (or occupy) the one-task-per-user slot.
        running = MagicMock(done=MagicMock(return_value=False))
        h._running[42] = running
        msg = await h.handle_external_confirm(42, True)
        assert msg == "On it — I'll confirm on Telegram."
        await _drain()
        assert len(gmail.sent) == 1
        assert h._running[42] is running  # untouched

    async def test_voice_meeting_preview_mentions_attendees(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(
            tmp_path, intent=_meeting_intent(), gcal=_FakeGcal(),
            contacts=_FakeContacts({"ayush": "ayush@x.com"}),
        )
        msg = await h.handle_external_trigger(42, "meet ayush tomorrow 3pm")
        assert msg.endswith("Confirm?")
        assert 'Meet "Launch sync"' in msg and "ayush" in msg


class TestScheduledEmail:
    async def test_create_resolves_contacts_and_stores_payload(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_schedule_intent(to=("Ayush",)),
            contacts=_FakeContacts({"ayush": "ayush@x.com"}),
        )
        upd = _update(42, "every day at 8am email ayush the standup notes")
        await h.message(upd, MagicMock())
        rows = await sched.list_for(42)
        assert len(rows) == 1
        assert rows[0].action_kind == "email"
        assert rows[0].name == "Email: Standup notes"
        assert rows[0].launch_package is None and rows[0].app_id is None
        # Contact names are resolved at CREATION time, not at fire time.
        payload = json.loads(rows[0].payload_json)
        assert payload == {
            "to": ["ayush@x.com"], "subject": "Standup notes",
            "body": "Notes here.",
        }
        assert "✉ Scheduled" in upd.message.reply_text.call_args.args[0]

    async def test_create_missing_contact_refuses(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(
            tmp_path, intent=_email_schedule_intent(to=("ayush",)),
            contacts=_FakeContacts(),
        )
        upd = _update(42, "every day at 8am email ayush the standup notes")
        await h.message(upd, MagicMock())
        assert "I don't have an email for: ayush" in (
            upd.message.reply_text.call_args.args[0]
        )
        assert await sched.list_for(42) == []

    async def test_listing_marks_cloud_rows(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path)
        await sched.insert(
            user_id=42, name="Email: Standup notes", freq="daily",
            at_minute=480, tz=IST, raw_description="Email ayush@x.com",
            next_run_at="2026-01-01T00:00:00+00:00", action_kind="email",
            payload_json=json.dumps({"to": ["ayush@x.com"]}),
        )
        upd = _update(42, "/schedules")
        await h.list_schedules(upd, MagicMock())
        assert "✉ Email: Standup notes" in upd.message.reply_text.call_args.args[0]


class TestLaunchScheduledCloud:
    async def _insert_cloud_row(self, sched):
        sid = await sched.insert(
            user_id=42, name="Standup", freq="daily", at_minute=480, tz=IST,
            raw_description="Email ayush@x.com: Standup notes",
            next_run_at="2026-01-01T00:00:00+00:00", action_kind="email",
            payload_json=json.dumps({
                "to": ["ayush@x.com"], "subject": "Standup notes",
                "body": "Notes here.",
            }),
        )
        return sid

    async def test_cloud_fire_ignores_running_guard(self, tmp_path) -> None:
        gmail = _FakeGmail()
        h, app, orch, sched = await _setup(tmp_path, gmail=gmail)
        sid = await self._insert_cloud_row(sched)
        # A device task is mid-flight — a cloud fire must run anyway.
        h._running[42] = MagicMock(done=MagicMock(return_value=False))
        ok = await h.launch_scheduled(await sched.get(sid))
        assert ok is True
        assert gmail.sent[0]["to"] == ["ayush@x.com"]
        orch.run_task.assert_not_called()
        # Wires the last_state column.
        assert (await sched.get(sid)).last_state == "done"
        notified = [t for t in _sent_texts(app) if "Standup" in t]
        assert any(t.startswith("⏰ Scheduled") and "📧" in t for t in notified)

    async def test_cloud_fire_failure_sets_failed(self, tmp_path) -> None:
        gmail = _FakeGmail(raises=CloudActionError("quota exceeded"))
        h, app, orch, sched = await _setup(tmp_path, gmail=gmail)
        sid = await self._insert_cloud_row(sched)
        ok = await h.launch_scheduled(await sched.get(sid))
        assert ok is True
        assert (await sched.get(sid)).last_state == "failed"


class TestContactCommand:
    async def test_add_and_list(self, tmp_path) -> None:
        contacts = _FakeContacts()
        h, app, orch, sched = await _setup(tmp_path, contacts=contacts)
        ctx = MagicMock()
        ctx.args = ["add", "Ayush", "ayush@x.com"]
        upd = _update(42, "/contact add Ayush ayush@x.com")
        await h.contact_cmd(upd, ctx)
        assert contacts.book == {"ayush": "ayush@x.com"}
        assert "ayush@x.com" in upd.message.reply_text.call_args.args[0]

        ctx.args = ["list"]
        upd = _update(42, "/contact list")
        await h.contact_cmd(upd, ctx)
        assert "• ayush — ayush@x.com" in upd.message.reply_text.call_args.args[0]

    async def test_remove(self, tmp_path) -> None:
        contacts = _FakeContacts({"ayush": "ayush@x.com"})
        h, app, orch, sched = await _setup(tmp_path, contacts=contacts)
        ctx = MagicMock()
        ctx.args = ["remove", "Ayush"]
        upd = _update(42, "/contact remove Ayush")
        await h.contact_cmd(upd, ctx)
        assert contacts.book == {}
        assert "Removed" in upd.message.reply_text.call_args.args[0]

    async def test_usage_on_bad_args(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path, contacts=_FakeContacts())
        ctx = MagicMock()
        ctx.args = ["add", "ayush"]  # no email
        upd = _update(42, "/contact add ayush")
        await h.contact_cmd(upd, ctx)
        assert "Usage:" in upd.message.reply_text.call_args.args[0]

    async def test_disabled_without_repo(self, tmp_path) -> None:
        h, app, orch, sched = await _setup(tmp_path)
        ctx = MagicMock()
        ctx.args = ["list"]
        upd = _update(42, "/contact list")
        await h.contact_cmd(upd, ctx)
        assert "aren't enabled" in upd.message.reply_text.call_args.args[0]
