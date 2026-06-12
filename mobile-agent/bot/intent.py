"""Unified intent layer — classifies a free-text/voice message into one of
several structured intents and dispatches accordingly.

Today's `bot.router.Router` answers exactly one question: "does this map to a
single (app, task, param)?". Cross-app comparison ("cheapest pizza on Swiggy or
Zomato") needs a fundamentally different shape — 2+ candidate apps and a ranking
key — so we put an `IntentClassifier` in *front* of the router. It returns a
discriminated-union `Intent`; the handler dispatches on `intent.kind`.

The union is deliberately open: feature #4 (saved tasks) and #5 (scheduled) will
add `SaveIntent` / `RunSavedIntent` / `ScheduleIntent` members + an `IntentKind`
value without touching comparison or single-app code.

Cloud actions extend the same union: `EmailIntent` (Gmail send) and
`MeetingIntent` (Calendar event + Meet link) run off-device, and
`ScheduleIntent` generalizes to carry either a device route or a cloud payload.

`Router` is unchanged and stays the single-app fallback: when the classifier
isn't sure (or its app/task ids don't resolve), we delegate to `router.route()`,
which is already battle-tested and covered by `bot/test_router.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol, Union
from zoneinfo import ZoneInfo

from agent.providers._parse import extract_json_object
from bot.apps import APPS, App, apps_in_category, get_app, get_task
from bot.router import Route, Router


# Default ceiling on how many apps a single comparison probes. Each probe is a
# full agent run (tens of seconds on one phone), so we bound it. Overridable via
# the IntentClassifier constructor (wired from settings in main.py).
DEFAULT_MAX_CANDIDATES = 3


class IntentKind(str, Enum):
    SINGLE = "single"      # one (app, task, param) — the existing router path
    COMPARE = "compare"    # feature #6 — probe N apps, rank, pick
    SAVE = "save"          # feature #4 — save the last run as a quick task
    RUN_SAVED = "run_saved"  # feature #4 — run a saved quick task by name
    SCHEDULE = "schedule"  # feature #5 — schedule a recurring/one-shot task
    EMAIL = "email"        # cloud action — send via the connected Gmail account
    MEETING = "meeting"    # cloud action — Calendar event + Meet link + invites
    UNKNOWN = "unknown"    # nothing matched — fall through to the freeform agent


class RankingKey(str, Enum):
    CHEAPEST = "cheapest"  # lowest price wins
    FASTEST = "fastest"    # shortest ETA wins
    BEST = "best"          # model/heuristic composite

    @classmethod
    def parse(cls, raw: object) -> "RankingKey":
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.CHEAPEST


@dataclass(frozen=True)
class SingleIntent:
    """A normal one-app task — wraps the existing Route."""

    route: Route
    kind: IntentKind = field(default=IntentKind.SINGLE, init=False)


@dataclass(frozen=True)
class CompareIntent:
    """Compare an item across 2+ same-category apps, then rank."""

    category: str
    candidates: tuple[App, ...]   # 2..N apps, same category, capped
    item: str                     # normalized thing to price/measure
    ranking_key: RankingKey = RankingKey.CHEAPEST
    kind: IntentKind = field(default=IntentKind.COMPARE, init=False)


@dataclass(frozen=True)
class SaveIntent:
    """Save the user's most recent run as a named quick task."""

    name: str
    kind: IntentKind = field(default=IntentKind.SAVE, init=False)


@dataclass(frozen=True)
class RunSavedIntent:
    """Run a previously saved quick task by (fuzzy) name."""

    name: str
    kind: IntentKind = field(default=IntentKind.RUN_SAVED, init=False)


@dataclass(frozen=True)
class EmailIntent:
    """Send an email NOW via the connected Gmail account (no phone involved)."""

    to: tuple[str, ...]           # raw recipients: addresses and/or bare names
    subject: str
    body: str
    kind: IntentKind = field(default=IntentKind.EMAIL, init=False)


@dataclass(frozen=True)
class MeetingIntent:
    """Create a Google Calendar event with a Meet link and invites."""

    title: str
    attendees: tuple[str, ...]    # addresses and/or names
    start_local: str              # "YYYY-MM-DD HH:MM" local wall-clock
    duration_minutes: int = 30
    description: str = ""
    kind: IntentKind = field(default=IntentKind.MEETING, init=False)


@dataclass(frozen=True)
class ScheduleIntent:
    """Schedule a device task OR a cloud action to run on a recurrence."""

    route: Route | None           # the task to run when it fires (None = cloud)
    freq: str                     # once | daily | weekly | monthly
    time_str: str                 # "HH:MM" local
    weekday_name: str | None = None
    day_of_month: int | None = None
    pay_automatically: bool = False
    name: str | None = None
    action_kind: str = "device"   # 'device' | 'email'
    payload: EmailIntent | None = None
    date_str: str | None = None   # "YYYY-MM-DD" for dated one-offs
    kind: IntentKind = field(default=IntentKind.SCHEDULE, init=False)


@dataclass(frozen=True)
class UnknownIntent:
    kind: IntentKind = field(default=IntentKind.UNKNOWN, init=False)


Intent = Union[
    SingleIntent, CompareIntent, SaveIntent, RunSavedIntent,
    ScheduleIntent, EmailIntent, MeetingIntent, UnknownIntent,
]


class _TextCompleter(Protocol):
    async def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 200,
    ) -> str: ...


_SYSTEM_PROMPT = """You are an intent classifier for a mobile-automation agent.

Classify the user's message into exactly one intent and extract its fields.

Output JSON ONLY, in this exact shape (no code fences, no prose):
{
  "intent": "single" | "compare" | "save" | "run_saved" | "schedule" | "email" | "meeting" | "unknown",
  "category": "<groceries|food|mobility|shopping|media|tools|payments> or null",
  "app_ids": ["<id>", ...],
  "task_id": "<id> or null",
  "param": "<string> or null",
  "item": "<string> or null",
  "ranking_key": "cheapest" | "fastest" | "best" | null,
  "name": "<string> or null",
  "schedule_freq": "once" | "daily" | "weekly" | "monthly" | null,
  "schedule_time": "<HH:MM 24h local> or null",
  "schedule_weekday": "monday".."sunday" | null,
  "schedule_day_of_month": <1-31> | null,
  "schedule_date": "<YYYY-MM-DD> or null",
  "pay_automatically": true | false | null,
  "email_to": ["<address or bare first name>", ...],
  "email_subject": "<string> or null",
  "email_body": "<string> or null",
  "meeting_title": "<string> or null",
  "meeting_attendees": ["<address or bare first name>", ...],
  "meeting_start": "<YYYY-MM-DD HH:MM> or null",
  "meeting_duration_minutes": <minutes> | null
}

Rules:
- "schedule" when the user wants a task to run automatically later or on a
  repeating schedule — e.g. "order milk on blinkit every day at 9am", "every
  sunday 9am reorder groceries on zepto", "recharge my number on the 1st each
  month". Fill the task fields (`app_ids`=[one], `task_id`, `param`) AND the
  schedule_* fields: schedule_freq, schedule_time (24h HH:MM), and
  schedule_weekday (weekly) / schedule_day_of_month (monthly). Set
  pay_automatically=true ONLY if the user explicitly said to pay/charge
  automatically; otherwise false. Whenever the user names a specific date or
  relative day for a one-off ("tomorrow", "on june 14th"), resolve it via the
  Now line and fill schedule_date. A scheduled WhatsApp/device task stays
  exactly as above (app_ids/task_id/param).
- "schedule" with an email action — e.g. "tomorrow 8am email the team the
  standup notes", "every friday email alice the report" — fill the schedule_*
  fields AND the email_* fields; leave app_ids and task_id null.
- "email" when the user wants an email sent NOW — via the connected Gmail
  account, NOT the Gmail Android app. Recipients in email_to may be raw
  addresses or bare first names (the system resolves names to saved contacts —
  pass names through verbatim). If the user gave only the gist, COMPOSE a
  clear 1-3 sentence email_body and a concise email_subject. Prefer "email"
  over a "single" intent on the gmail app unless the user explicitly asks to
  open the app.
- "meeting" when the user wants a Google Calendar event with a Meet link —
  e.g. "schedule a meet with Ayush tomorrow at 3pm about the launch". Fill
  meeting_title, meeting_attendees (addresses or bare names), meeting_start as
  an absolute "YYYY-MM-DD HH:MM" resolved from the Now line, and
  meeting_duration_minutes (30 unless the user stated a duration).
- "save" when the user wants to bookmark the thing they JUST did as a reusable
  quick task — e.g. "save this as my sunday order", "remember this as morning
  coffee". Put the chosen label in `name`.
- "run_saved" when the user wants to run a previously saved quick task by name —
  e.g. "run my sunday order", "do my morning coffee". Put the name in `name`.
- "compare" when the user wants to choose between apps by price/time/quality —
  e.g. "cheapest pizza on swiggy or zomato", "compare cab fare on uber and ola",
  "which is cheaper for milk, blinkit or zepto". For compare: fill `category`,
  `app_ids` (the apps named, ideally >=2), `item` (the product/destination,
  brand-normalized), and `ranking_key` (price words -> "cheapest"; time words
  like fastest/quickest/soonest -> "fastest"; else "best"). If the user names a
  category but not specific apps ("which app is cheapest for milk"), set
  `category` and leave `app_ids` empty — the system fills the candidates.
- "single" for a normal one-app task: fill `app_ids` with the one app,
  `task_id`, and `param`.
- "unknown" when nothing matches.
- `category`, `app_ids`, and `task_id` MUST come from the registry below.
- Output ONLY the JSON object.
"""


def _category_listing() -> str:
    """Apps grouped by category + the per-(app,task) listing, for the prompt."""
    by_cat: dict[str, list[str]] = {}
    for app in APPS:
        by_cat.setdefault(app.category, []).append(app.id)
    cat_lines = [f"  {cat}: {', '.join(ids)}" for cat, ids in by_cat.items()]
    task_lines: list[str] = []
    for app in APPS:
        for t in app.tasks:
            needs = " (needs param)" if t.needs_param else ""
            task_lines.append(
                f"  - app_id={app.id} task_id={t.id} -> {app.name}: {t.label}{needs}"
            )
    return (
        "Apps by category:\n"
        + "\n".join(cat_lines)
        + "\n\nPer-app tasks (for single-app intents):\n"
        + "\n".join(task_lines)
    )


class IntentClassifier:
    """Classifies a message into an Intent; delegates single/unknown to Router."""

    def __init__(
        self,
        provider: _TextCompleter,
        router: Router,
        *,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        tz: str = "Asia/Kolkata",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._router = router
        self._max_candidates = max(2, int(max_candidates))
        self._listing = _category_listing()
        self._tz = tz
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now_line(self) -> str:
        """Context line so the model can resolve "tomorrow 3pm" to absolutes."""
        local = self._clock().astimezone(ZoneInfo(self._tz))
        return (
            f"Now: {local.strftime('%Y-%m-%dT%H:%M')} ({self._tz}); "
            f"today is {local.strftime('%A')}."
        )

    async def classify(self, text: str) -> Intent:
        """Return a structured Intent for the message."""
        if not text or not text.strip():
            return UnknownIntent()
        user_prompt = (
            f"{self._now_line()}\n\n"
            f"{self._listing}\n\n"
            f"User message: {text.strip()}\n\n"
            "Output JSON only."
        )
        try:
            raw = await self._provider.complete_text(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=450,
            )
        except Exception:
            # Provider hiccup — fall back to the single-app router (which has
            # its own try/except and returns None on failure).
            return await self._single_or_unknown(text)

        data = _parse_json(raw)
        if data is None:
            return await self._single_or_unknown(text)

        intent = str(data.get("intent", "")).strip().lower()
        if intent == "compare":
            compare = self._build_compare(data)
            if compare is not None:
                return compare
            # Not enough comparable apps — treat as a single-app request.
            return await self._single_or_unknown(text)

        if intent == "save":
            name = _clean_str(data.get("name"))
            if name:
                return SaveIntent(name=name)
            return await self._single_or_unknown(text)

        if intent == "run_saved":
            name = _clean_str(data.get("name"))
            if name:
                return RunSavedIntent(name=name)
            return await self._single_or_unknown(text)

        if intent == "schedule":
            sched = self._build_schedule(data)
            if sched is not None:
                return sched
            return await self._single_or_unknown(text)

        if intent == "email":
            email = self._build_email(data)
            if email is not None:
                return email
            return await self._single_or_unknown(text)

        if intent == "meeting":
            meeting = self._build_meeting(data)
            if meeting is not None:
                return meeting
            return await self._single_or_unknown(text)

        if intent == "single":
            single = self._build_single(data)
            if single is not None:
                return single

        # "unknown", an unhandled value, or a single that didn't resolve:
        # defer to the proven router before giving up.
        return await self._single_or_unknown(text)

    # ------------------------------------------------------------------

    def _build_compare(self, data: dict) -> CompareIntent | None:
        item = _clean_str(data.get("item")) or _clean_str(data.get("param"))
        if not item:
            return None
        category = _clean_str(data.get("category"))
        app_ids = data.get("app_ids")
        named = [
            get_app(str(a))
            for a in (app_ids if isinstance(app_ids, list) else [])
            if get_app(str(a)) is not None
        ]
        # Pick the comparison's category: the model's, else the first named app's.
        target_cat = category or (named[0].category if named else None)
        if not target_cat:
            return None
        candidates = self._resolve_candidates(target_cat, named)
        if len(candidates) < 2:
            return None
        return CompareIntent(
            category=target_cat,
            candidates=candidates,
            item=item,
            ranking_key=RankingKey.parse(data.get("ranking_key")),
        )

    def _resolve_candidates(
        self, category: str, named: list[App]
    ) -> tuple[App, ...]:
        """Candidate apps for the comparison, capped at max_candidates.

        Start from the named apps that belong to `category`; if fewer than two,
        top up with the category's other registered apps (registry order). This
        handles both "swiggy or zomato" (use those) and "which app is cheapest
        for milk" (expand the whole category).
        """
        ordered: list[App] = []
        for app in named:
            if app.category == category and app not in ordered:
                ordered.append(app)
        if len(ordered) < 2:
            for app in apps_in_category(category):
                if app not in ordered:
                    ordered.append(app)
        return tuple(ordered[: self._max_candidates])

    def _build_email(self, data: dict) -> EmailIntent | None:
        to = _str_tuple(data.get("email_to"))
        if not to:
            return None
        subject = _clean_str(data.get("email_subject"))
        body = _clean_str(data.get("email_body"))
        if not subject and not body:
            return None
        return EmailIntent(
            to=to,
            subject=subject or _derive_subject(body or ""),
            body=body or "",
        )

    def _build_meeting(self, data: dict) -> MeetingIntent | None:
        title = _clean_str(data.get("meeting_title"))
        start = _clean_str(data.get("meeting_start"))
        if not title or not start:
            return None
        try:
            datetime.strptime(start, "%Y-%m-%d %H:%M")
        except ValueError:
            return None  # the model didn't resolve to an absolute start
        dur_raw = data.get("meeting_duration_minutes")
        try:
            duration = int(dur_raw) if dur_raw is not None else 30
        except (TypeError, ValueError):
            duration = 30
        return MeetingIntent(
            title=title,
            attendees=_str_tuple(data.get("meeting_attendees")),
            start_local=start,
            duration_minutes=duration if duration > 0 else 30,
        )

    def _build_schedule(self, data: dict) -> ScheduleIntent | None:
        freq = _clean_str(data.get("schedule_freq"))
        time_str = _clean_str(data.get("schedule_time"))
        if not freq or not time_str:
            return None
        dom_raw = data.get("schedule_day_of_month")
        try:
            dom = int(dom_raw) if dom_raw is not None else None
        except (TypeError, ValueError):
            dom = None
        common = dict(
            freq=freq.lower(),
            time_str=time_str,
            weekday_name=_clean_str(data.get("schedule_weekday")),
            day_of_month=dom,
            pay_automatically=bool(data.get("pay_automatically")),
            name=_clean_str(data.get("name")),
            date_str=_clean_str(data.get("schedule_date")),
        )
        email = self._build_email(data)
        if email is not None:
            # Cloud schedule — no device route; the payload IS the action.
            return ScheduleIntent(
                route=None, action_kind="email", payload=email, **common
            )
        single = self._build_single(data)
        if single is None:
            return None  # can't schedule a task we can't resolve
        return ScheduleIntent(route=single.route, **common)

    def _build_single(self, data: dict) -> SingleIntent | None:
        app_ids = data.get("app_ids")
        first = next(
            (str(a) for a in (app_ids if isinstance(app_ids, list) else []) if a),
            None,
        )
        if not first:
            return None
        app = get_app(first)
        if app is None:
            return None
        task = get_task(app, str(data.get("task_id") or ""))
        if task is None:
            return None
        param = _clean_str(data.get("param"))
        return SingleIntent(route=Route(app=app, task=task, param=param))

    async def _single_or_unknown(self, text: str) -> Intent:
        route = await self._router.route(text)
        return SingleIntent(route=route) if route is not None else UnknownIntent()


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    candidate = raw.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        obj = extract_json_object(candidate)
        if obj is None:
            return None
        try:
            data = json.loads(obj)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _clean_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    return s or None


def _str_tuple(value: object) -> tuple[str, ...]:
    """Tolerant list-of-strings → tuple (drops blanks; non-lists → empty)."""
    if not isinstance(value, list):
        return ()
    return tuple(s for s in (str(v).strip() for v in value) if s)


def _derive_subject(body: str, limit: int = 60) -> str:
    """First words of the body, cut at a word boundary, <= limit chars."""
    words = body.split()
    subject = ""
    for word in words:
        candidate = f"{subject} {word}".strip()
        if len(candidate) > limit:
            break
        subject = candidate
    return subject or body[:limit].strip()
