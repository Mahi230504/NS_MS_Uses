"""Vision provider shim. Stable public API for ad-hoc callers (e.g. test_local.py).

The orchestrator wires a `VisionProvider` instance directly; this module is the
escape hatch for code that just wants `await get_next_action(...)` without
plumbing a provider through. It reads `GEMINI_API_KEY` / `GEMINI_MODEL` /
`VISION_PROVIDER` from the environment (load `.env` yourself first).
"""
from __future__ import annotations

import os

from agent.providers import VisionProvider, make_provider


_provider: VisionProvider | None = None


def _default_provider() -> VisionProvider:
    global _provider
    if _provider is None:
        name = os.environ.get("VISION_PROVIDER", "gemini")
        if name == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY not set. Add it to .env or export it."
                )
            model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            _provider = make_provider("gemini", api_key=api_key, model=model)
        else:
            raise RuntimeError(
                f"VISION_PROVIDER={name!r} is set but only 'gemini' is wired in."
            )
    return _provider


async def get_next_action(
    screenshot_bytes: bytes,
    task_description: str,
    step_history: list[dict],
) -> dict:
    """Send screenshot+task to the configured provider, return parsed action.

    Usage info (token counts, quota remaining) is discarded by this shim. Use
    the underlying provider directly if you need it.
    """
    response = await _default_provider().get_next_action(
        screenshot_bytes=screenshot_bytes,
        task_description=task_description,
        step_history=step_history,
    )
    return response.action
