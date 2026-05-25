"""VisionProvider Protocol + shared types."""
from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable


class RequestUsage(NamedTuple):
    input_tokens: int
    output_tokens: int
    rpm_remaining: int
    rpd_remaining: int


class ProviderResponse(NamedTuple):
    action: dict
    usage: RequestUsage


class ProviderError(RuntimeError):
    """Raised when a provider call fails (after any retries)."""


class QuotaExceeded(ProviderError):
    """Raised when the provider's free-tier daily quota is exhausted."""


@runtime_checkable
class VisionProvider(Protocol):
    """Contract every vision provider must satisfy.

    Implementations send the screenshot + task + history to a vision-capable
    LLM and return a parsed action plus usage info. Implementations own their
    own retry, throttling, and parsing concerns.
    """

    async def get_next_action(
        self,
        screenshot_bytes: bytes,
        task_description: str,
        step_history: list[dict],
        screen_size: tuple[int, int] | None = None,
        skill_hint: str | None = None,
        ui_tree: str | None = None,
    ) -> ProviderResponse: ...

    async def classify_yes_no(
        self,
        screenshot_bytes: bytes,
        question: str,
    ) -> bool:
        """Minimal vision call: send the screenshot + a yes/no question.

        Implementations must constrain output to a tiny budget so this stays
        cheap (it runs on every loop iteration when used as a HITL guard).
        Returns True iff the model answers "yes" (case-insensitive).
        """
        ...
