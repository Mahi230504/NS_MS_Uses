"""OpenRouter vision provider — OpenAI-compatible API, model-agnostic.

OpenRouter (https://openrouter.ai) exposes many models — OpenAI, Anthropic,
Google, Meta, Mistral — behind one OpenAI-compatible endpoint. We pick the
model via OPENROUTER_MODEL; common choices for vision:

  google/gemini-2.5-flash           (cheap, fast)
  google/gemini-2.0-flash-exp:free  (free tier, daily-rate-limited)
  openai/gpt-4o-mini                (cheap, fast)
  anthropic/claude-3.5-sonnet       (best reasoning, pricier)

Auth: a single API key, billed as you go. Free credits ($1) come with new
accounts and unlock standard rate limits.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from openai import AsyncOpenAI, APIError, RateLimitError

from agent.providers._parse import extract_json_object, salvage_truncated_json
from agent.providers.base import (
    ProviderError,
    ProviderResponse,
    QuotaExceeded,
    RequestUsage,
)
from agent.providers.throttle import Throttle
from config import prompts


DEFAULT_MODEL = "google/gemini-2.5-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# 4096 is well within Gemini-2.5-Flash's 8K output budget and gives plenty of
# headroom for the model's "thinking" tokens (which OpenRouter counts against
# this budget for reasoning models) before the JSON action is emitted. 1024
# was too tight in practice: real runs got cut off mid-JSON
# (e.g. '{"action":"tap","x":265,"y":1287, "...'). When truncation still
# happens despite the bump, `_parse_action` falls back to salvage.
MAX_OUTPUT_TOKENS = 4096
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Paid-tier defaults. OpenRouter's per-account rate limits are model-dependent
# and generally well above 60 RPM for paid accounts; the local throttle just
# acts as a safety cap against runaway loops. The orchestrator's loop
# detection (hard-aborts after a few cycles) is the real cost guard now.
DEFAULT_RPM = 60
DEFAULT_RPD = 5000


class OpenRouterProvider:
    """VisionProvider implementation backed by OpenRouter via the openai SDK."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        rpm: int = DEFAULT_RPM,
        rpd: int = DEFAULT_RPD,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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

        messages = self._build_messages(screenshot_bytes, prompts.SYSTEM_PROMPT, user_text)
        action, usage = await self._call(messages, MAX_OUTPUT_TOKENS, require_json=True)
        return ProviderResponse(action=action, usage=usage)

    async def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 200,
    ) -> str:
        """Text-only completion. Used by the bot's intent router.

        Returns the model's raw reply (stripped). No image, no JSON forcing
        unless the caller requested it via system_prompt — keeps the surface
        narrow and reusable for routing, summarisation, etc.
        """
        rpm_remaining, rpd_remaining = await self._throttle.acquire()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
        except RateLimitError as e:
            raise QuotaExceeded(f"OpenRouter quota exhausted: {e}") from e
        except APIError as e:
            raise ProviderError(f"complete_text failed: {e}") from e
        return (response.choices[0].message.content or "").strip()

    async def classify_yes_no(
        self, screenshot_bytes: bytes, question: str
    ) -> bool:
        system = (
            "You are a binary classifier. Output only the single word 'yes' "
            "or 'no'. No punctuation, no explanation."
        )
        user_text = f"{question}\n\nAnswer with exactly one word: yes or no."
        messages = self._build_messages(screenshot_bytes, system, user_text)

        rpm_remaining, rpd_remaining = await self._throttle.acquire()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=10,
                temperature=0.0,
            )
        except RateLimitError as e:
            raise QuotaExceeded(f"OpenRouter quota exhausted: {e}") from e
        except APIError as e:
            raise ProviderError(f"classify_yes_no failed: {e}") from e

        text = (response.choices[0].message.content or "").strip().lower()
        return text.startswith("yes")

    # ------------------------------------------------------------------
    # Internals

    @staticmethod
    def _build_messages(
        screenshot_bytes: bytes, system: str, user_text: str
    ) -> list[dict]:
        img_b64 = base64.standard_b64encode(screenshot_bytes).decode("ascii")
        # OpenAI vision schema: content is a list of typed parts.
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ]

    async def _call(
        self,
        messages: list[dict],
        max_tokens: int,
        *,
        require_json: bool,
    ) -> tuple[dict, RequestUsage]:
        last_error: BaseException | None = None
        for attempt in range(MAX_RETRIES + 1):
            rpm_remaining, rpd_remaining = await self._throttle.acquire()
            try:
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                }
                if require_json:
                    # Many OpenRouter-routed models honour this; harmless on
                    # models that ignore it (we still re-parse defensively).
                    kwargs["response_format"] = {"type": "json_object"}
                response = await self._client.chat.completions.create(**kwargs)
            except RateLimitError as e:
                # Treat 429 as retryable but cap; persistent 429 → QuotaExceeded.
                last_error = e
                if attempt == MAX_RETRIES:
                    raise QuotaExceeded(
                        f"OpenRouter rate limit hit, retries exhausted: {e}"
                    ) from e
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2**attempt))
                continue
            except APIError as e:
                code = getattr(e, "status_code", 0) or 0
                if code != 0 and code < 500 and code != 429:
                    raise ProviderError(f"OpenRouter API error {code}: {e}") from e
                last_error = e
                if attempt == MAX_RETRIES:
                    raise ProviderError(
                        f"OpenRouter failed after {MAX_RETRIES} retries: {e}"
                    ) from e
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2**attempt))
                continue

            text = (response.choices[0].message.content or "").strip()
            if not text:
                raise ProviderError("OpenRouter returned an empty response")
            action = self._parse_action(text)
            usage = self._build_usage(response, rpm_remaining, rpd_remaining)
            return action, usage

        raise ProviderError(f"Retries exhausted: {last_error}")

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
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        obj = extract_json_object(text)
        if obj is not None:
            return json.loads(obj)
        # Last resort: the response was truncated by the output token budget
        # (a Gemini-via-OpenRouter failure mode where "thinking" tokens eat
        # the budget before the JSON closes). Try to close the partial object
        # at the last complete element boundary so the step degrades instead
        # of failing the whole task.
        salvaged = salvage_truncated_json(text)
        if salvaged is not None:
            return json.loads(salvaged)
        raise ProviderError(
            f"No JSON object found in response (first 200 chars): {text[:200]!r}"
        )

    @staticmethod
    def _build_usage(
        response: Any, rpm_remaining: int, rpd_remaining: int
    ) -> RequestUsage:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            rpm_remaining=rpm_remaining,
            rpd_remaining=rpd_remaining,
        )
