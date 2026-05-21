"""Unit tests for GeminiProvider (mocked client)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.providers.base import ProviderError, ProviderResponse, QuotaExceeded
from agent.providers.gemini import GeminiProvider


def _make_provider(monkeypatch, response_text: str, *, usage_in: int = 100, usage_out: int = 20) -> GeminiProvider:
    """Build a GeminiProvider whose underlying client is a mock."""
    fake_response = SimpleNamespace(
        text=response_text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=usage_in, candidates_token_count=usage_out
        ),
    )
    fake_models = MagicMock()
    fake_models.generate_content = AsyncMock(return_value=fake_response)
    fake_client = SimpleNamespace(aio=SimpleNamespace(models=fake_models))

    def fake_client_ctor(api_key: str):
        return fake_client

    monkeypatch.setattr("agent.providers.gemini.genai.Client", fake_client_ctor)
    return GeminiProvider(api_key="fake")


class TestGeminiProvider:
    async def test_returns_parsed_action(self, monkeypatch) -> None:
        gp = _make_provider(monkeypatch, '{"action": "tap", "x": 10, "y": 20}')
        resp = await gp.get_next_action(b"png-bytes", "find shoes", [])
        assert isinstance(resp, ProviderResponse)
        assert resp.action == {"action": "tap", "x": 10, "y": 20}
        assert resp.usage.input_tokens == 100
        assert resp.usage.output_tokens == 20
        assert resp.usage.rpd_remaining >= 0

    async def test_handles_fenced_response(self, monkeypatch) -> None:
        gp = _make_provider(
            monkeypatch,
            '```json\n{"action": "wait", "reason": "loading"}\n```',
        )
        resp = await gp.get_next_action(b"x", "t", [])
        assert resp.action["action"] == "wait"

    async def test_empty_response_raises(self, monkeypatch) -> None:
        gp = _make_provider(monkeypatch, "")
        with pytest.raises(ProviderError):
            await gp.get_next_action(b"x", "t", [])

    async def test_unparseable_response_raises(self, monkeypatch) -> None:
        gp = _make_provider(monkeypatch, "this is not json at all")
        with pytest.raises(ProviderError):
            await gp.get_next_action(b"x", "t", [])

    async def test_429_retries_then_succeeds(self, monkeypatch) -> None:
        # Speed-up retry backoff.
        monkeypatch.setattr("agent.providers.gemini.BASE_BACKOFF_SECONDS", 0.0)

        from google.genai import errors as genai_errors

        # 429 error twice, then success.
        success = SimpleNamespace(
            text='{"action": "wait", "reason": "ok"}',
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
        )
        err = genai_errors.APIError(429, {"error": {"message": "rate limited"}})
        gen = AsyncMock(side_effect=[err, err, success])
        fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen)))
        monkeypatch.setattr("agent.providers.gemini.genai.Client", lambda api_key: fake_client)

        gp = GeminiProvider(api_key="fake")
        resp = await gp.get_next_action(b"x", "t", [])
        assert resp.action["action"] == "wait"
        assert gen.await_count == 3

    async def test_4xx_non_429_raises_immediately(self, monkeypatch) -> None:
        from google.genai import errors as genai_errors

        err = genai_errors.APIError(400, {"error": {"message": "bad request"}})
        gen = AsyncMock(side_effect=err)
        fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen)))
        monkeypatch.setattr("agent.providers.gemini.genai.Client", lambda api_key: fake_client)

        gp = GeminiProvider(api_key="fake")
        with pytest.raises(ProviderError):
            await gp.get_next_action(b"x", "t", [])
        assert gen.await_count == 1  # no retries on 4xx

    async def test_quota_exceeded_propagates(self, monkeypatch) -> None:
        gp = _make_provider(monkeypatch, '{"action": "wait", "reason": "ok"}')
        # Exhaust the throttle's RPD by directly manipulating it.
        gp._throttle._rpd = 0
        with pytest.raises(QuotaExceeded):
            await gp.get_next_action(b"x", "t", [])

    def test_format_history(self) -> None:
        out = GeminiProvider._format_history(
            [
                {"action": {"action": "tap", "x": 1, "y": 2}, "result": "tapped"},
                {"action": {"action": "wait", "reason": "load"}, "result": "waited 1s"},
            ]
        )
        assert "1." in out and "2." in out
        assert "tap" in out
        assert "waited 1s" in out

    def test_format_history_empty(self) -> None:
        assert GeminiProvider._format_history([]) == "(no prior steps)"
