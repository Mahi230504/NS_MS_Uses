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

`Router` is unchanged and stays the single-app fallback: when the classifier
isn't sure (or its app/task ids don't resolve), we delegate to `router.route()`,
which is already battle-tested and covered by `bot/test_router.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Union

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
    UNKNOWN = "unknown"    # nothing matched — fall through to the freeform agent
    # Reserved for upcoming features (#4/#5): SAVE, RUN_SAVED, SCHEDULE.


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
class UnknownIntent:
    kind: IntentKind = field(default=IntentKind.UNKNOWN, init=False)


Intent = Union[SingleIntent, CompareIntent, UnknownIntent]


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
  "intent": "single" | "compare" | "unknown",
  "category": "<groceries|food|mobility|shopping|media|tools|payments> or null",
  "app_ids": ["<id>", ...],
  "task_id": "<id> or null",
  "param": "<string> or null",
  "item": "<string> or null",
  "ranking_key": "cheapest" | "fastest" | "best" | null
}

Rules:
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
    ) -> None:
        self._provider = provider
        self._router = router
        self._max_candidates = max(2, int(max_candidates))
        self._listing = _category_listing()

    async def classify(self, text: str) -> Intent:
        """Return a structured Intent for the message."""
        if not text or not text.strip():
            return UnknownIntent()
        user_prompt = (
            f"{self._listing}\n\n"
            f"User message: {text.strip()}\n\n"
            "Output JSON only."
        )
        try:
            raw = await self._provider.complete_text(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=200,
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
