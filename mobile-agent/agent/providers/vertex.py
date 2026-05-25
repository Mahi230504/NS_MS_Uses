"""Vertex AI vision provider (Gemini via Google Cloud, billed against the
project's credits — including student / free-trial credit).

Differences vs the AI-Studio GeminiProvider:
  - Auth is Application Default Credentials (service-account JSON pointed to
    by GOOGLE_APPLICATION_CREDENTIALS), not an API key.
  - Client init takes `project` and `location` instead of `api_key`.
  - The Throttle defaults are looser because Vertex doesn't enforce the same
    free-tier RPM/RPD ceilings; quotas come from your GCP project.

Same request/response shape, same JSON action schema, same retry logic.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agent.providers._parse import extract_json_object
from agent.providers.base import (
    ProviderError,
    ProviderResponse,
    QuotaExceeded,
    RequestUsage,
)
from agent.providers.throttle import Throttle
from config import prompts


DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_LOCATION = "us-central1"
MAX_OUTPUT_TOKENS = 1024
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Looser caps — Vertex enforces project-level quotas server-side. Local
# throttle is mostly a fail-safe against accidental runaway loops.
DEFAULT_RPM = 60
DEFAULT_RPD = 10_000


class VertexProvider:
    """VisionProvider backed by Vertex AI Gemini via the google-genai SDK.

    Authentication: relies on Application Default Credentials. Set
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json in your
    environment before construction. The SDK picks it up automatically.
    """

    def __init__(
        self,
        project: str,
        location: str = DEFAULT_LOCATION,
        model: str = DEFAULT_MODEL,
        rpm: int = DEFAULT_RPM,
        rpd: int = DEFAULT_RPD,
    ) -> None:
        if not project:
            raise ValueError("VertexProvider needs a non-empty `project`.")
        self._client = genai.Client(vertexai=True, project=project, location=location)
        self._model = model
        self._throttle = Throttle(rpm=rpm, rpd=rpd)

    async def get_next_action(
        self,
        screenshot_bytes: bytes,
        task_description: str,
        step_history: list[dict],
        screen_size: tuple[int, int] | None = None,
        skill_hint: str | None = None,
        ui_tree: str | None = None,
    ) -> ProviderResponse:
        screen_line = (
            f"Device screen: {screen_size[0]}x{screen_size[1]} px\n\n"
            if screen_size is not None
            else ""
        )
        skill_block = (
            f"App-specific guidance:\n{skill_hint}\n\n" if skill_hint else ""
        )
        tree_block = f"{ui_tree}\n\n" if ui_tree else ""
        user_text = (
            f"{screen_line}"
            f"{skill_block}"
            f"{tree_block}"
            f"Task: {task_description}\n\n"
            f"Step history (most recent last):\n{self._format_history(step_history)}\n\n"
            "What is the next action? Output a single JSON object only."
        )

        contents = [
            types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
            user_text,
        ]
        config = types.GenerateContentConfig(
            system_instruction=prompts.SYSTEM_PROMPT,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
        )

        last_error: BaseException | None = None
        for attempt in range(MAX_RETRIES + 1):
            rpm_remaining, rpd_remaining = await self._throttle.acquire()
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=config,
                )
                action = self._parse_action(response.text or "")
                usage = self._build_usage(response, rpm_remaining, rpd_remaining)
                return ProviderResponse(action=action, usage=usage)
            except QuotaExceeded:
                raise
            except genai_errors.APIError as e:
                last_error = e
                code = getattr(e, "code", 0) or 0
                if code != 429 and code < 500:
                    raise ProviderError(f"Vertex API error {code}: {e}") from e
                if attempt == MAX_RETRIES:
                    raise ProviderError(
                        f"Vertex failed after {MAX_RETRIES} retries: {e}"
                    ) from e
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2**attempt))
            except (ProviderError, ValueError):
                raise
            except Exception as e:
                last_error = e
                if attempt == MAX_RETRIES:
                    raise ProviderError(f"Vertex call failed: {e}") from e
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2**attempt))

        raise ProviderError(f"Retries exhausted: {last_error}")

    async def classify_yes_no(
        self,
        screenshot_bytes: bytes,
        question: str,
    ) -> bool:
        contents = [
            types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
            f"{question}\n\nAnswer with exactly one word: yes or no.",
        ]
        config = types.GenerateContentConfig(
            system_instruction=(
                "You are a binary classifier. Output only the single word 'yes' "
                "or 'no'. No punctuation, no explanation."
            ),
            max_output_tokens=10,
        )
        rpm_remaining, rpd_remaining = await self._throttle.acquire()
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except QuotaExceeded:
            raise
        except genai_errors.APIError as e:
            raise ProviderError(f"classify_yes_no failed: {e}") from e

        text = (response.text or "").strip().lower()
        return text.startswith("yes")

    @staticmethod
    def _format_history(step_history: list[dict]) -> str:
        if not step_history:
            return "(no prior steps)"
        lines = []
        for i, step in enumerate(step_history, 1):
            action = step.get("action", {})
            result = step.get("result", "")
            lines.append(f"{i}. {json.dumps(action, default=str)} -> {result}")
        return "\n".join(lines)

    @staticmethod
    def _parse_action(text: str) -> dict:
        text = text.strip()
        if not text:
            raise ProviderError("Vertex returned an empty response")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        obj = extract_json_object(text)
        if obj is None:
            raise ProviderError(
                f"No JSON object found in response (first 200 chars): {text[:200]!r}"
            )
        return json.loads(obj)

    @staticmethod
    def _build_usage(
        response: Any, rpm_remaining: int, rpd_remaining: int
    ) -> RequestUsage:
        meta = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(meta, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
        return RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            rpm_remaining=rpm_remaining,
            rpd_remaining=rpd_remaining,
        )
