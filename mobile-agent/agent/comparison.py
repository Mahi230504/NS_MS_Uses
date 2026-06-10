"""Cross-app comparison engine (feature #6).

Given a list of candidate apps and an item, runs a READ-ONLY price probe on each
app SEQUENTIALLY (the device drives one app at a time), collects a structured
`Quote` per app, ranks them, and returns a `ComparisonResult`. The actual order
is NOT placed here — the caller presents the ranking and hands the chosen app to
the existing single-app order flow.

Layering: this module is bot-agnostic. It takes plain `ProbeTarget`s and a
ranking-key string, not a `bot.intent.CompareIntent`, so `agent/` keeps no
dependency on `bot/`. The handler converts the intent into probe targets.

Each probe is `Orchestrator.run_task(task, launch_package=…, read_only=True)`,
which forbids add-to-cart/checkout/pay/book taps and asks the model to finish
with a `report` action carrying {price, currency, eta, available, item_name,
notes}. If a probe finishes without a usable report, we best-effort salvage a
quote from its final summary via a text-completion call (when a provider that
supports `complete_text` was supplied).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from agent.providers._parse import extract_json_object
from agent.state_machine import Task, TaskState

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids import coupling
    from agent.orchestrator import Orchestrator

_log = logging.getLogger("mobile_agent.comparison")


# Self-contained read-only probe instruction. Works for both COMMERCE apps
# (which additionally get prompts.PROBE_ADDENDUM injected) and GENERIC apps
# (mobility fare checks), so the report schema lives here too.
_PROBE_TEMPLATE = (
    "READ-ONLY PRICE CHECK — do NOT buy, add to cart, book, confirm, or pay. "
    "Goal: find the PRICE of '{item}' in this app. Steps: (1) search for "
    "'{item}'; (2) the search results / restaurant cards often do NOT show the "
    "price (they show ratings or a delivery time) — so TAP the most relevant "
    "result to OPEN it; (3) read the item's actual price on the opened page. "
    "Do NOT tap ADD / Buy / Checkout / Pay / Place Order / Book / Confirm / "
    "Request — opening a product/dish to read its price is fine, committing is "
    'not. Finish ONLY with: {"action":"report","data":{"price":<number or '
    'null>,"currency":"INR","eta":"<string or null>","available":true,'
    '"item_name":"<what you found>","notes":"<short or empty>"}} — never finish '
    'with "done". Report price=null ONLY after you have opened a result and '
    "there is genuinely no price; if '{item}' isn't available here, report "
    "available=false."
)

_SALVAGE_SYSTEM = (
    "Extract a structured price quote from the text. Output JSON ONLY: "
    '{"price":<number or null>,"currency":"INR","eta":"<string or null>",'
    '"available":<true|false>,"item_name":"<string or null>"}. '
    "Use a plain number for price (no currency symbol). No prose, no fences."
)

# Pull the first integer out of an ETA string ("~30 mins", "10-15 min" -> 30/10).
_ETA_NUM_RE = re.compile(r"(\d+)")
# Keep digits + a single decimal point when coercing a price string.
_PRICE_CLEAN_RE = re.compile(r"[^\d.]")

ProgressCb = Callable[[str], Awaitable[None]]
AbortCb = Callable[[], bool]


@dataclass(frozen=True)
class ProbeTarget:
    """One app to probe — the minimal slice the engine needs from an App."""

    app_id: str
    app_name: str
    package: str


@dataclass
class Quote:
    """A single app's result for the compared item."""

    app_id: str
    app_name: str
    ok: bool
    price: float | None = None
    currency: str = "INR"
    eta: str | None = None
    available: bool | None = None
    item_name: str | None = None
    notes: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "app_name": self.app_name,
            "ok": self.ok,
            "price": self.price,
            "currency": self.currency,
            "eta": self.eta,
            "available": self.available,
            "item_name": self.item_name,
            "notes": self.notes,
            "failure_reason": self.failure_reason,
        }


@dataclass
class ComparisonResult:
    item: str
    category: str
    ranking_key: str
    quotes: list[Quote] = field(default_factory=list)   # candidate order
    ranked: list[Quote] = field(default_factory=list)   # ok+metric, sorted
    winner: Quote | None = None

    def quotes_as_dicts(self) -> list[dict]:
        return [q.to_dict() for q in self.quotes]


class ComparisonEngine:
    """Runs sequential read-only probes and ranks the results."""

    def __init__(
        self,
        orchestrator: "Orchestrator",
        *,
        probe_timeout_s: int | None = None,
        salvage_provider: object | None = None,
    ) -> None:
        self._orch = orchestrator
        self._probe_timeout = probe_timeout_s
        # Optional object exposing async complete_text(system_prompt, user_prompt,
        # max_tokens) — used to salvage a quote when a probe forgets to `report`.
        self._salvage = (
            salvage_provider
            if salvage_provider is not None
            and hasattr(salvage_provider, "complete_text")
            else None
        )

    async def run(
        self,
        *,
        targets: list[ProbeTarget],
        item: str,
        category: str,
        ranking_key: str,
        user_id: int,
        on_progress: ProgressCb | None = None,
        should_abort: AbortCb | None = None,
    ) -> ComparisonResult:
        quotes: list[Quote] = []
        total = len(targets)
        for i, target in enumerate(targets, start=1):
            if should_abort is not None and should_abort():
                _log.info("comparison aborted before probing %s", target.app_id)
                break
            if on_progress is not None:
                await on_progress(f"🔎 Checking {target.app_name} ({i}/{total})…")
            quotes.append(await self._probe_one(target, item, user_id))

        ranked = _rank(quotes, ranking_key)
        return ComparisonResult(
            item=item,
            category=category,
            ranking_key=ranking_key,
            quotes=quotes,
            ranked=ranked,
            winner=ranked[0] if ranked else None,
        )

    # ------------------------------------------------------------------

    async def _probe_one(
        self, target: ProbeTarget, item: str, user_id: int
    ) -> Quote:
        description = _PROBE_TEMPLATE.replace("{item}", item)
        task = Task(user_id=user_id, description=description)
        try:
            await self._orch.run_task(
                task, launch_package=target.package, read_only=True
            )
        except asyncio.CancelledError:
            # Abort mid-comparison — let it propagate to cancel the whole run.
            raise
        except Exception as e:  # pragma: no cover - defensive
            _log.warning("probe %s raised: %s", target.app_id, e)
            return Quote(
                target.app_id, target.app_name, ok=False,
                failure_reason=f"probe error: {e}",
            )

        report = task.report if isinstance(task.report, dict) else None
        if report and _report_has_signal(report):
            return _quote_from_report(target, report)

        if task.state == TaskState.DONE:
            salvaged = await self._salvage_quote(target, task)
            if salvaged is not None:
                return salvaged
            # Reported nothing useful, but the run didn't fail — surface a soft
            # "no price" rather than a hard failure.
            return Quote(
                target.app_id, target.app_name, ok=True,
                available=(report or {}).get("available"),
                item_name=(report or {}).get("item_name"),
                notes="finished without a price",
            )

        # FAILED / TIMED_OUT
        return Quote(
            target.app_id, target.app_name, ok=False,
            failure_reason=task.failure_reason or task.state.value,
        )

    async def _salvage_quote(self, target: ProbeTarget, task: Task) -> Quote | None:
        """Best-effort: extract a quote from the task's final summary text."""
        if self._salvage is None:
            return None
        text = (task.final_summary or "").strip()
        if not text:
            return None
        try:
            raw = await self._salvage.complete_text(  # type: ignore[attr-defined]
                system_prompt=_SALVAGE_SYSTEM,
                user_prompt=f"Text:\n{text}\n\nOutput JSON only.",
                max_tokens=150,
            )
        except Exception:
            return None
        data = _parse_json(raw)
        if data is None:
            return None
        _log.info("salvaged quote for %s from final summary", target.app_id)
        return _quote_from_report(target, data)


# ----------------------------------------------------------------------
# Pure helpers (no I/O) — easy to unit-test.

def _report_has_signal(report: dict) -> bool:
    """True if a report dict carries something worth turning into a Quote."""
    return (
        report.get("price") is not None
        or report.get("available") is not None
        or bool(report.get("item_name"))
    )


def _quote_from_report(target: ProbeTarget, report: dict) -> Quote:
    price = _coerce_number(report.get("price"))
    available = report.get("available")
    if available is None:
        available = price is not None
    return Quote(
        app_id=target.app_id,
        app_name=target.app_name,
        ok=True,
        price=price,
        currency=str(report.get("currency") or "INR").strip() or "INR",
        eta=_clean(report.get("eta")),
        available=bool(available),
        item_name=_clean(report.get("item_name")),
        notes=_clean(report.get("notes")),
    )


def _rank(quotes: list[Quote], ranking_key: str) -> list[Quote]:
    """Return the rankable, ok quotes sorted best-first for the key.

    Quotes that are unavailable, failed, or lack the ranking metric are kept in
    `ComparisonResult.quotes` (so they're still shown) but excluded from the
    ranked list / winner selection.
    """
    ok = [q for q in quotes if q.ok and q.available is not False]
    key = (ranking_key or "cheapest").lower()
    if key == "fastest":
        cand = [q for q in ok if _eta_minutes(q.eta) is not None]
        cand.sort(key=lambda q: _eta_minutes(q.eta) or 0)
        return cand
    # cheapest / best -> price ascending
    cand = [q for q in ok if q.price is not None]
    cand.sort(key=lambda q: q.price if q.price is not None else float("inf"))
    if not cand and key == "best":
        # Nothing had a price; fall back to whatever's available, in order.
        return list(ok)
    return cand


def _eta_minutes(eta: str | None) -> int | None:
    if not eta:
        return None
    m = _ETA_NUM_RE.search(str(eta))
    return int(m.group(1)) if m else None


def _coerce_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = _PRICE_CLEAN_RE.sub("", str(value))
    if not s or s == ".":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean(value: object) -> str | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    return s or None


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
