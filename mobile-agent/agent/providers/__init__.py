"""Pluggable vision/reasoning providers."""
from __future__ import annotations

from agent.providers.base import (
    ProviderError,
    ProviderResponse,
    QuotaExceeded,
    RequestUsage,
    VisionProvider,
)


__all__ = [
    "ProviderError",
    "ProviderResponse",
    "QuotaExceeded",
    "RequestUsage",
    "VisionProvider",
    "make_provider",
]


def make_provider(name: str, **kwargs) -> VisionProvider:
    """Factory: instantiate the named provider with kwargs.

    Currently only 'gemini' is supported. Add new providers here when they
    land — keep `agent/computer_use.py` and the orchestrator decoupled from
    concrete implementations.
    """
    if name == "gemini":
        from agent.providers.gemini import GeminiProvider

        return GeminiProvider(**kwargs)
    raise ValueError(
        f"Unsupported VISION_PROVIDER: {name!r}. Supported: 'gemini'."
    )
