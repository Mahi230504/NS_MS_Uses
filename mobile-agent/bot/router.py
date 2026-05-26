"""Free-form intent router.

Turns natural-language messages like "order milk on blinkit", "play jazz on
spotify", "uber to airport" into structured `(app_id, task_id, param)` tuples
that the orchestrator can run directly — bypassing the inline-keyboard menu.

The menu still exists for discoverability via /start. The router exists for
the daily-driver path: one Telegram message, no taps.

When the user's input doesn't map cleanly to a supported (app, task) pair,
the router returns None and the handler falls back to the existing freeform
agent path (i.e. the model decides everything from scratch).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from agent.providers._parse import extract_json_object
from bot.apps import APPS, App, TaskTemplate, get_app, get_task


@dataclass(frozen=True)
class Route:
    """A successful structured match."""

    app: App
    task: TaskTemplate
    param: str | None


class _TextCompleter(Protocol):
    async def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 200,
    ) -> str: ...


_SYSTEM_PROMPT = """You are an intent router for a mobile-automation agent.

The user will send you a free-form message. Your job: map it to ONE of the
supported (app, task) pairs listed below, and extract any free-text parameter
the task needs.

Output JSON ONLY in this exact shape:
  {"app_id": "<id> or null", "task_id": "<id> or null", "param": "<string> or null"}

Rules:
- If the user's intent matches a supported app+task, fill in both ids + the
  param (extracted from the user's message).
- If the user mentions an item or query but no app, infer the most likely app
  (e.g. "order milk" → blinkit, "play jazz" → spotify, "navigate to office"
  → maps). Default to the most appropriate app for the verb.
- For tasks that need a param (`param_prompt` is non-empty in the registry):
  extract the param from the user's message. If they didn't provide one,
  return null for param (the bot will ask).
- If the user's intent doesn't match any supported pair, return
  {"app_id": null, "task_id": null, "param": null}.
- Output ONLY the JSON object. No code fences, no commentary.
"""


def _registry_listing() -> str:
    """Compact, model-readable summary of every supported app+task pair."""
    lines: list[str] = []
    for app in APPS:
        for t in app.tasks:
            needs = " (needs param)" if t.needs_param else ""
            lines.append(f"  - app_id={app.id} task_id={t.id} → {app.name}: {t.label}{needs}")
    return "Supported (app, task) pairs:\n" + "\n".join(lines)


class Router:
    def __init__(self, provider: _TextCompleter) -> None:
        self._provider = provider
        # Built once at construction; the registry is static.
        self._listing = _registry_listing()

    async def route(self, text: str) -> Route | None:
        """Return a Route, or None if the message doesn't map to a structured task."""
        if not text or not text.strip():
            return None
        user_prompt = (
            f"{self._listing}\n\n"
            f"User message: {text.strip()}\n\n"
            "Output JSON only."
        )
        try:
            raw = await self._provider.complete_text(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_tokens=150,
            )
        except Exception:
            return None
        return self._parse_response(raw)

    @staticmethod
    def _parse_response(raw: str) -> Route | None:
        """Tolerantly extract a Route from a model reply.

        Accepts plain JSON, JSON in markdown fences, JSON with surrounding
        prose — anything `extract_json_object` can find. Returns None if the
        reply doesn't decode or if the route doesn't reference a real app/task.
        """
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
        if not isinstance(data, dict):
            return None
        app_id = data.get("app_id")
        task_id = data.get("task_id")
        param = data.get("param")
        if not app_id or not task_id:
            return None
        app = get_app(str(app_id))
        if app is None:
            return None
        task = get_task(app, str(task_id))
        if task is None:
            return None
        param_str = None if param in (None, "") else str(param).strip() or None
        return Route(app=app, task=task, param=param_str)
