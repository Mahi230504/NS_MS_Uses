"""Unit tests for GmailService/CalendarService: MIME, request shape, errors."""
from __future__ import annotations

import base64
import json
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit

import pytest

from services.google_auth import GoogleAuthError
from services.google_workspace import (
    CalendarService,
    CloudActionError,
    DirectoryMatch,
    DirectoryService,
    GmailService,
)


class FakeHttp:
    """Scripted HTTP seam: records every call, pops queued (status, payload)."""

    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def __call__(self, method, url, *, data=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "data": data, "headers": headers}
        )
        return self.responses.pop(0)


class FakeAuth:
    """GoogleAuthManager stand-in: hands out one token or raises."""

    def __init__(self, token: str = "at-1", error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.calls = 0

    async def get_access_token(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.token


class TestGmailSend:
    async def test_send_builds_correct_mime_and_returns_ids(self) -> None:
        http = FakeHttp([(200, {"id": "m-1", "threadId": "t-1"})])
        gmail = GmailService(FakeAuth(), http=http)

        result = await gmail.send_email(
            to=["alice@x.com", "bob@y.com"],
            subject="Demo is ready",
            body="The demo is live — link inside.",
        )
        assert result == {
            "id": "m-1",
            "thread_id": "t-1",
            "to": ["alice@x.com", "bob@y.com"],
            "subject": "Demo is ready",
        }

        (call,) = http.calls
        assert call["method"] == "POST"
        assert call["url"] == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        assert call["headers"]["Authorization"] == "Bearer at-1"
        assert call["headers"]["Content-Type"] == "application/json"

        # Decode the raw payload back into a real message and check it.
        raw = json.loads(call["data"])["raw"]
        msg = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(raw)
        )
        assert msg["To"] == "alice@x.com, bob@y.com"
        assert msg["Subject"] == "Demo is ready"
        assert msg.get_content().strip() == "The demo is live — link inside."

    async def test_http_error_maps_to_cloud_action_error(self) -> None:
        http = FakeHttp(
            [(403, {"error": {"message": "Insufficient Permission", "code": 403}})]
        )
        gmail = GmailService(FakeAuth(), http=http)
        with pytest.raises(CloudActionError, match="HTTP 403.*Insufficient Permission"):
            await gmail.send_email(to=["a@x.com"], subject="s", body="b")

    async def test_not_connected_propagates_auth_error(self) -> None:
        http = FakeHttp()
        gmail = GmailService(
            FakeAuth(error=GoogleAuthError("Google account is not connected")),
            http=http,
        )
        with pytest.raises(GoogleAuthError, match="not connected"):
            await gmail.send_email(to=["a@x.com"], subject="s", body="b")
        assert http.calls == []


class TestCalendarCreateMeeting:
    async def test_create_builds_correct_event_body(self) -> None:
        http = FakeHttp(
            [(200, {
                "id": "ev-1",
                "htmlLink": "https://calendar.google.com/event?eid=ev-1",
                "hangoutLink": "https://meet.google.com/abc-defg-hij",
            })]
        )
        gcal = CalendarService(FakeAuth(), http=http)

        result = await gcal.create_meeting(
            title="Launch sync",
            start_local="2026-06-15 15:00",
            duration_minutes=45,
            tz="Asia/Kolkata",
            attendees=["ayush@x.com", "alice@x.com"],
            description="Quick sync about the launch.",
        )
        assert result == {
            "event_id": "ev-1",
            "meet_link": "https://meet.google.com/abc-defg-hij",
            "html_link": "https://calendar.google.com/event?eid=ev-1",
            "start_iso": "2026-06-15T15:00:00+05:30",
            "end_iso": "2026-06-15T15:45:00+05:30",
        }

        (call,) = http.calls
        parts = urlsplit(call["url"])
        assert parts.path == "/calendar/v3/calendars/primary/events"
        q = parse_qs(parts.query)
        assert q["conferenceDataVersion"] == ["1"]
        assert q["sendUpdates"] == ["all"]
        assert call["headers"]["Authorization"] == "Bearer at-1"

        event = json.loads(call["data"])
        assert event["summary"] == "Launch sync"
        assert event["description"] == "Quick sync about the launch."
        assert event["start"] == {
            "dateTime": "2026-06-15T15:00:00", "timeZone": "Asia/Kolkata"
        }
        assert event["end"] == {
            "dateTime": "2026-06-15T15:45:00", "timeZone": "Asia/Kolkata"
        }
        assert event["attendees"] == [
            {"email": "ayush@x.com"}, {"email": "alice@x.com"}
        ]
        request = event["conferenceData"]["createRequest"]
        assert request["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
        assert len(request["requestId"]) == 32  # uuid4().hex

    async def test_meet_link_falls_back_to_entry_points(self) -> None:
        http = FakeHttp(
            [(200, {
                "id": "ev-2",
                "conferenceData": {
                    "entryPoints": [{"entryPointType": "video",
                                     "uri": "https://meet.google.com/xyz"}]
                },
            })]
        )
        gcal = CalendarService(FakeAuth(), http=http)
        result = await gcal.create_meeting(
            title="t", start_local="2026-06-15 15:00", duration_minutes=30,
            tz="Asia/Kolkata", attendees=[],
        )
        assert result["meet_link"] == "https://meet.google.com/xyz"

    async def test_meet_link_empty_when_google_omits_it(self) -> None:
        http = FakeHttp([(200, {"id": "ev-3"})])
        gcal = CalendarService(FakeAuth(), http=http)
        result = await gcal.create_meeting(
            title="t", start_local="2026-06-15 15:00", duration_minutes=30,
            tz="Asia/Kolkata", attendees=[],
        )
        assert result["meet_link"] == ""

    async def test_unparseable_start_raises_before_any_call(self) -> None:
        http = FakeHttp()
        auth = FakeAuth()
        gcal = CalendarService(auth, http=http)
        with pytest.raises(CloudActionError, match="couldn't parse meeting time"):
            await gcal.create_meeting(
                title="t", start_local="tomorrow 3pm", duration_minutes=30,
                tz="Asia/Kolkata", attendees=[],
            )
        assert http.calls == [] and auth.calls == 0

    async def test_bad_timezone_raises(self) -> None:
        gcal = CalendarService(FakeAuth(), http=FakeHttp())
        with pytest.raises(CloudActionError, match="couldn't parse meeting time"):
            await gcal.create_meeting(
                title="t", start_local="2026-06-15 15:00", duration_minutes=30,
                tz="Not/AZone", attendees=[],
            )

    async def test_http_error_maps_to_cloud_action_error(self) -> None:
        http = FakeHttp([(400, {"error": {"message": "Invalid attendee email"}})])
        gcal = CalendarService(FakeAuth(), http=http)
        with pytest.raises(CloudActionError, match="HTTP 400.*Invalid attendee email"):
            await gcal.create_meeting(
                title="t", start_local="2026-06-15 15:00", duration_minutes=30,
                tz="Asia/Kolkata", attendees=["nope"],
            )

    async def test_not_connected_propagates_auth_error(self) -> None:
        gcal = CalendarService(
            FakeAuth(error=GoogleAuthError("Google account is not connected")),
            http=FakeHttp(),
        )
        with pytest.raises(GoogleAuthError, match="not connected"):
            await gcal.create_meeting(
                title="t", start_local="2026-06-15 15:00", duration_minutes=30,
                tz="Asia/Kolkata", attendees=[],
            )


class TestDirectorySearch:
    async def test_search_builds_request_and_parses_matches(self) -> None:
        http = FakeHttp([(200, {
            "people": [
                {
                    "names": [{"displayName": "Mohan Shrivastava"}],
                    "emailAddresses": [
                        {"value": "mohan.shrivastava@x.co"},
                        {"value": "alt@x.co"},
                    ],
                },
                {
                    "names": [{"displayName": "Mohan Kumar"}],
                    "emailAddresses": [{"value": "mohan.kumar@x.co"}],
                },
            ]
        })])
        directory = DirectoryService(FakeAuth(), http=http)

        matches = await directory.resolve_name("Mohan")
        # First (primary) address per person; one match per person.
        assert matches == [
            DirectoryMatch(name="Mohan Shrivastava", email="mohan.shrivastava@x.co"),
            DirectoryMatch(name="Mohan Kumar", email="mohan.kumar@x.co"),
        ]

        (call,) = http.calls
        assert call["method"] == "GET"
        parts = urlsplit(call["url"])
        assert parts.path == "/v1/people:searchDirectoryPeople"
        q = parse_qs(parts.query)
        assert q["query"] == ["Mohan"]
        assert q["readMask"] == ["names,emailAddresses"]
        assert q["sources"] == [
            "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
            "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT",
        ]
        assert call["headers"]["Authorization"] == "Bearer at-1"

    async def test_no_people_returns_empty(self) -> None:
        http = FakeHttp([(200, {})])
        directory = DirectoryService(FakeAuth(), http=http)
        assert await directory.resolve_name("Nobody") == []

    async def test_person_without_email_is_skipped(self) -> None:
        http = FakeHttp([(200, {
            "people": [
                {"names": [{"displayName": "No Email"}]},
                {"names": [{"displayName": "Has Email"}],
                 "emailAddresses": [{"value": "has@x.co"}]},
            ]
        })])
        directory = DirectoryService(FakeAuth(), http=http)
        assert await directory.resolve_name("x") == [
            DirectoryMatch(name="Has Email", email="has@x.co")
        ]

    async def test_blank_name_makes_no_call(self) -> None:
        http, auth = FakeHttp(), FakeAuth()
        directory = DirectoryService(auth, http=http)
        assert await directory.resolve_name("   ") == []
        assert http.calls == [] and auth.calls == 0

    async def test_http_error_maps_to_cloud_action_error(self) -> None:
        http = FakeHttp([(403, {"error": {"message": "Directory not shared"}})])
        directory = DirectoryService(FakeAuth(), http=http)
        with pytest.raises(CloudActionError, match="HTTP 403.*Directory not shared"):
            await directory.resolve_name("Mohan")

    async def test_not_connected_propagates_auth_error(self) -> None:
        http = FakeHttp()
        directory = DirectoryService(
            FakeAuth(error=GoogleAuthError("Google account is not connected")),
            http=http,
        )
        with pytest.raises(GoogleAuthError, match="not connected"):
            await directory.resolve_name("Mohan")
        assert http.calls == []
