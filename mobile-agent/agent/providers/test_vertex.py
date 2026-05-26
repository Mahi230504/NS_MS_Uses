"""Unit tests for VertexProvider (mocked google-genai client)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.providers.base import ProviderError, ProviderResponse, QuotaExceeded
from agent.providers.vertex import VertexProvider


def _make_provider(
    monkeypatch, response_text: str, *, usage_in: int = 50, usage_out: int = 5
) -> VertexProvider:
    fake_response = SimpleNamespace(
        text=response_text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=usage_in, candidates_token_count=usage_out
        ),
    )
    fake_models = MagicMock()
    fake_models.generate_content = AsyncMock(return_value=fake_response)
    fake_client = SimpleNamespace(aio=SimpleNamespace(models=fake_models))

    def fake_ctor(*, vertexai: bool, project: str, location: str):
        # Capture call shape on the returned client so tests can assert on it.
        fake_client._init_kwargs = {
            "vertexai": vertexai,
            "project": project,
            "location": location,
        }
        return fake_client

    monkeypatch.setattr("agent.providers.vertex.genai.Client", fake_ctor)
    return VertexProvider(project="test-project", location="us-central1")


class TestVertexProvider:
    async def test_returns_parsed_action(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, '{"action": "tap", "x": 10, "y": 20}')
        resp = await vp.get_next_action(b"png", "find shoes", [])
        assert isinstance(resp, ProviderResponse)
        assert resp.action == {"action": "tap", "x": 10, "y": 20}
        assert resp.usage.input_tokens == 50
        assert resp.usage.output_tokens == 5

    async def test_client_constructed_in_vertex_mode(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, '{"action": "wait", "reason": "loading"}')
        assert vp._client._init_kwargs == {
            "vertexai": True,
            "project": "test-project",
            "location": "us-central1",
        }

    async def test_empty_response_raises(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, "")
        with pytest.raises(ProviderError):
            await vp.get_next_action(b"x", "t", [])

    async def test_unparseable_response_raises(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, "this is not json at all")
        with pytest.raises(ProviderError):
            await vp.get_next_action(b"x", "t", [])

    async def test_truncated_json_is_salvaged(self, monkeypatch) -> None:
        vp = _make_provider(
            monkeypatch, '{"action": "tap", "x": 265, "y": 1287, "'
        )
        resp = await vp.get_next_action(b"x", "t", [])
        assert resp.action == {"action": "tap", "x": 265, "y": 1287}

    async def test_classify_yes(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, "yes")
        assert await vp.classify_yes_no(b"png", "is this a payment screen?") is True

    async def test_classify_no(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, "no")
        assert await vp.classify_yes_no(b"png", "is this a payment screen?") is False

    async def test_429_retries_then_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr("agent.providers.vertex.BASE_BACKOFF_SECONDS", 0.0)

        from google.genai import errors as genai_errors

        success = SimpleNamespace(
            text='{"action": "wait", "reason": "ok"}',
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
        )
        err = genai_errors.APIError(429, {"error": {"message": "rate limited"}})
        gen = AsyncMock(side_effect=[err, err, success])
        fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen)))
        monkeypatch.setattr(
            "agent.providers.vertex.genai.Client",
            lambda **_: fake_client,
        )

        vp = VertexProvider(project="p")
        resp = await vp.get_next_action(b"x", "t", [])
        assert resp.action["action"] == "wait"
        assert gen.await_count == 3

    async def test_quota_exceeded_propagates(self, monkeypatch) -> None:
        vp = _make_provider(monkeypatch, '{"action": "wait", "reason": "ok"}')
        vp._throttle._rpd = 0
        with pytest.raises(QuotaExceeded):
            await vp.get_next_action(b"x", "t", [])

    def test_empty_project_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "agent.providers.vertex.genai.Client",
            lambda **_: SimpleNamespace(aio=SimpleNamespace(models=MagicMock())),
        )
        with pytest.raises(ValueError):
            VertexProvider(project="")
