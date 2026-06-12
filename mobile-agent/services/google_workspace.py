"""Gmail send + Calendar/Meet creation over raw REST (no google-api-python-client).

Both services lean on GoogleAuthManager for a live access token (it refreshes
itself) and share the same injectable HTTP seam shape as the auth module, so
tests run with zero network. GoogleAuthError propagates untouched — handlers
turn "not connected" into a friendly line; everything HTTP-shaped is wrapped
in CloudActionError with a user-facing message.
"""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.google_auth import GoogleAuthManager, _AiohttpSeam


GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
CALENDAR_EVENTS_URL = (
    "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    "?conferenceDataVersion=1&sendUpdates=all"
)
PEOPLE_SEARCH_URL = "https://people.googleapis.com/v1/people:searchDirectoryPeople"


class CloudActionError(Exception):
    """Cloud action failed; the message is user-facing (sent verbatim)."""


class GmailService:
    """Sends plain-text email as the connected account via the Gmail REST API."""

    def __init__(self, auth: GoogleAuthManager, *, http: Callable | None = None) -> None:
        self._auth = auth
        self._http = http if http is not None else _AiohttpSeam()

    async def send_email(self, *, to: list[str], subject: str, body: str) -> dict:
        """Returns {"id": str, "thread_id": str, "to": list[str], "subject": str}."""
        token = await self._auth.get_access_token()

        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        status, payload = await self._http(
            "POST",
            GMAIL_SEND_URL,
            data=json.dumps({"raw": raw}),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if not 200 <= status < 300:
            raise CloudActionError(
                f"Gmail send failed (HTTP {status}): {_google_reason(payload)}"
            )
        return {
            "id": str(payload.get("id", "")),
            "thread_id": str(payload.get("threadId", "")),
            "to": list(to),
            "subject": subject,
        }

    async def close(self) -> None:
        if isinstance(self._http, _AiohttpSeam):
            await self._http.close()


class CalendarService:
    """Creates Calendar events with a Meet link on the connected account."""

    def __init__(self, auth: GoogleAuthManager, *, http: Callable | None = None) -> None:
        self._auth = auth
        self._http = http if http is not None else _AiohttpSeam()

    async def create_meeting(
        self,
        *,
        title: str,
        start_local: str,
        duration_minutes: int,
        tz: str,
        attendees: list[str],
        description: str = "",
    ) -> dict:
        """start_local is "YYYY-MM-DD HH:MM" wall-clock in tz.

        Returns {"event_id", "meet_link", "html_link", "start_iso", "end_iso"}.
        """
        try:
            zone = ZoneInfo(tz)
            start = datetime.strptime(start_local, "%Y-%m-%d %H:%M").replace(
                tzinfo=zone
            )
        except (ValueError, ZoneInfoNotFoundError):
            raise CloudActionError(
                f"couldn't parse meeting time {start_local!r} in timezone {tz!r}"
            ) from None
        end = start + timedelta(minutes=duration_minutes)

        token = await self._auth.get_access_token()
        event = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": tz,
            },
            "end": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": tz,
            },
            "attendees": [{"email": e} for e in attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        status, payload = await self._http(
            "POST",
            CALENDAR_EVENTS_URL,
            data=json.dumps(event),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if not 200 <= status < 300:
            raise CloudActionError(
                f"Calendar event failed (HTTP {status}): {_google_reason(payload)}"
            )
        return {
            "event_id": str(payload.get("id", "")),
            "meet_link": _meet_link(payload),
            "html_link": str(payload.get("htmlLink", "")),
            "start_iso": start.isoformat(),
            "end_iso": end.isoformat(),
        }

    async def close(self) -> None:
        if isinstance(self._http, _AiohttpSeam):
            await self._http.close()


@dataclass(frozen=True)
class DirectoryMatch:
    """One person found in the Workspace directory."""

    name: str   # displayName, or the query when Google omits a name
    email: str


class DirectoryService:
    """Resolves a name to colleague email(s) via the People API directory search.

    Read-only: searches the connected account's Workspace domain directory
    (DOMAIN_PROFILE + DOMAIN_CONTACT). Requires the directory.readonly scope —
    a connected account from before this scope existed must reconnect. The
    domain admin must also allow directory/contact visibility, otherwise the
    search returns no people even though the call succeeds.
    """

    # Repeated `sources` params; both cover full-time profiles and shared
    # domain contacts. readMask limits the response to what we actually use.
    _SOURCES = (
        "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
        "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT",
    )

    def __init__(self, auth: GoogleAuthManager, *, http: Callable | None = None) -> None:
        self._auth = auth
        self._http = http if http is not None else _AiohttpSeam()

    async def resolve_name(self, name: str) -> list[DirectoryMatch]:
        """Return every directory person matching `name` (empty list if none).

        Raises GoogleAuthError when the account isn't connected and
        CloudActionError on an HTTP error — callers that want graceful
        degradation (treat the name as unknown) catch both.
        """
        query = name.strip()
        if not query:
            return []
        token = await self._auth.get_access_token()
        params = urlencode(
            {
                "query": query,
                "readMask": "names,emailAddresses",
                "sources": list(self._SOURCES),
                "pageSize": 10,
            },
            doseq=True,
        )
        status, payload = await self._http(
            "GET",
            f"{PEOPLE_SEARCH_URL}?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if not 200 <= status < 300:
            raise CloudActionError(
                f"Directory lookup failed (HTTP {status}): {_google_reason(payload)}"
            )
        matches: list[DirectoryMatch] = []
        for person in payload.get("people") or []:
            if not isinstance(person, dict):
                continue
            names = person.get("names") or []
            display = ""
            if names and isinstance(names[0], dict):
                display = str(names[0].get("displayName", ""))
            for entry in person.get("emailAddresses") or []:
                value = entry.get("value") if isinstance(entry, dict) else None
                if value:
                    matches.append(
                        DirectoryMatch(name=display or query, email=str(value))
                    )
                    break  # first (primary) address per person
        return matches

    async def close(self) -> None:
        if isinstance(self._http, _AiohttpSeam):
            await self._http.close()


def _meet_link(payload: dict) -> str:
    """hangoutLink, else the first conferenceData entry point; "" when absent."""
    link = payload.get("hangoutLink")
    if link:
        return str(link)
    conference = payload.get("conferenceData") or {}
    for entry in conference.get("entryPoints") or []:
        uri = entry.get("uri") if isinstance(entry, dict) else None
        if uri:
            return str(uri)
    return ""


def _google_reason(payload: dict) -> str:
    """Google's error.message when parseable, else the raw body."""
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return str(payload.get("raw") or payload)
