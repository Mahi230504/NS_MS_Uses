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
    """Factory: instantiate the named provider with kwargs."""
    if name == "gemini":
        from agent.providers.gemini import GeminiProvider

        return GeminiProvider(**kwargs)
    if name == "openrouter":
        from agent.providers.openrouter import OpenRouterProvider

        return OpenRouterProvider(**kwargs)
    raise ValueError(
        f"Unsupported VISION_PROVIDER: {name!r}. Supported: 'gemini', 'openrouter'."
    )
