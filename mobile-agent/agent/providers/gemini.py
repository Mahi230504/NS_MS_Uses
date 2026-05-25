"""Google Gemini vision provider (uses google-genai SDK, free tier compatible)."""
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
MAX_OUTPUT_TOKENS = 1024
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Free-tier defaults. Google's daily limit varies wildly per model:
#   gemini-2.5-flash   : 5 RPM, 20  RPD
#   gemini-2.0-flash   : 10 RPM, 200 RPD
#   gemini-1.5-flash-8b: 15 RPM, 1500 RPD
# These constants are the strictest among them so we never over-spend. Paid
# tier callers override via the constructor.
DEFAULT_RPM = 5
DEFAULT_RPD = 20


class GeminiProvider:
    """VisionProvider implementation backed by google-genai."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        rpm: int = DEFAULT_RPM,
        rpd: int = DEFAULT_RPD,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
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
        # Screen dims drive coordinate accuracy — without them the model can't
        # know whether to emit 1080x1920 or 1440x2960 coords for a tap.
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
                    raise ProviderError(f"Gemini API error {code}: {e}") from e
                if attempt == MAX_RETRIES:
                    raise ProviderError(
                        f"Gemini failed after {MAX_RETRIES} retries: {e}"
                    ) from e
                # Honour the server's retry hint when present (e.g. RetryInfo
                # says "retry in 35s") — otherwise fall back to exponential.
                hinted = _parse_retry_delay_seconds(e)
                delay = hinted if hinted is not None else BASE_BACKOFF_SECONDS * (2**attempt)
                await asyncio.sleep(delay)
            except (ProviderError, ValueError):
                # Parsing errors are not retryable.
                raise
            except Exception as e:
                last_error = e
                if attempt == MAX_RETRIES:
                    raise ProviderError(f"Gemini call failed: {e}") from e
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
        # Respects the same throttle as the main get_next_action loop.
        rpm_remaining, rpd_remaining = await self._throttle.acquire()
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except QuotaExceeded:
            raise
        except genai_errors.APIError as e:
            # Conservative on classify failures: treat as 'no' rather than
            # spuriously prompting the user. The keyword pre-filter is still in
            # play, so the user isn't fully unprotected.
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
            raise ProviderError("Gemini returned an empty response")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # response_mime_type='application/json' should make the above succeed;
        # this branch is belt-and-suspenders for fenced/prose-wrapped output.
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


def _parse_retry_delay_seconds(error: Exception) -> float | None:
    """Extract `retry in Ns` from a 429 error if Google attached RetryInfo.

    Google's APIError carries a `details` payload with type
    `google.rpc.RetryInfo` containing `retryDelay` like "35s" or "35.5s".
    Falls back gracefully when the shape doesn't match.
    """
    import re

    # Cheap & robust: regex the stringified error for `retry in <num>s` or
    # `retryDelay: '<num>s'` — both forms appear in the genai SDK's repr.
    text = str(error)
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", text, re.IGNORECASE)
    if not m:
        m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text)
    if not m:
        return None
    try:
        # Add a small cushion so the next call doesn't race the window edge.
        return float(m.group(1)) + 1.0
    except ValueError:
        return None
