"""OpenAI SDK wrapper for repowiki.

Targets OpenAI-compatible endpoints only: the key and base URL come from
OPENAI_API_KEY / OPENAI_BASE_URL (see config.py), and switching models is just
a different model name. Provider quirks handled here:
  - gpt-5 family rejects `max_tokens`, so `max_completion_tokens` is sent first;
  - some models reject `temperature`;
  - OpenAI-compatible proxies (e.g. LiteLLM) may throttle per host, so 429s get a
    long jittered backoff instead of counting as failed attempts.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncGenerator

import httpx
from openai import APIError, AsyncOpenAI, RateLimitError

logger = logging.getLogger(__name__)

_RATE_TRIES = 8

# model -> parameters that model answered 400 on; consulted before every later
# request so a long run doesn't pay one wasted round-trip per call
_UNSUPPORTED: dict[str, set[str]] = {}


def _rate_backoff(hit: int) -> float:
    """seconds before rate-limit retry `hit`: 15, 30, 60, 120, 240, then 300 (jittered)."""
    base = min(300.0, 15.0 * (2 ** (hit - 1)))
    return base * (0.75 + 0.5 * random.random())


def _is_param_error(exc: Exception, param: str) -> bool:
    msg = str(exc).lower()
    return "400" in msg and param in msg


class LLMClient:
    """async LLM client backed by the OpenAI SDK."""

    def __init__(self, model: str, api_key: str = "", api_base: str = ""):
        self.model = model
        # reads need room for long pages; connects get slack for slow proxies
        self.client = AsyncOpenAI(
            api_key=api_key or None,
            base_url=api_base or None,
            timeout=httpx.Timeout(900.0, connect=30.0),
            max_retries=5,
        )
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def _create(self, messages: list[dict], max_tokens: int, temperature: float, **extra):
        """chat.completions.create, dropping parameters this model is known to reject."""
        bad = _UNSUPPORTED.setdefault(self.model, set())
        kwargs: dict = {"model": self.model, "messages": messages, **extra}
        if "max_completion_tokens" in bad:
            kwargs["max_tokens"] = max_tokens
        else:
            kwargs["max_completion_tokens"] = max_tokens
        if "temperature" not in bad:
            kwargs["temperature"] = temperature

        while True:
            try:
                return await self.client.chat.completions.create(**kwargs)
            except APIError as e:
                if "temperature" in kwargs and _is_param_error(e, "temperature"):
                    bad.add("temperature")
                    logger.warning("%s rejects temperature; omitting it", self.model)
                    kwargs.pop("temperature")
                    continue
                if "max_completion_tokens" in kwargs and _is_param_error(
                    e, "max_completion_tokens"
                ):
                    bad.add("max_completion_tokens")
                    logger.warning("%s rejects max_completion_tokens; using max_tokens", self.model)
                    kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
                    continue
                raise

    async def _create_with_rate_retry(self, messages, max_tokens, temperature, **extra):
        rate_hits = 0
        while True:
            try:
                return await self._create(messages, max_tokens, temperature, **extra)
            except RateLimitError as e:
                # some proxies answer 429 when a budget/quota is exhausted;
                # that never clears by waiting, so fail now instead of backing off
                if "budget" in str(e).lower():
                    raise
                rate_hits += 1
                if rate_hits > _RATE_TRIES:
                    raise
                wait = _rate_backoff(rate_hits)
                logger.warning(
                    "rate limited (%d/%d); retrying in %.0fs", rate_hits, _RATE_TRIES, wait
                )
                await asyncio.sleep(wait)

    async def complete(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> str:
        """non-streaming completion, returns the full response text."""
        extra = {"response_format": response_format} if response_format else {}
        try:
            resp = await self._create_with_rate_retry(messages, max_tokens, temperature, **extra)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return f"[LLM Error: {e}]"

        usage = resp.usage
        if usage:
            self.total_input_tokens += usage.prompt_tokens or 0
            self.total_output_tokens += usage.completion_tokens or 0

        return resp.choices[0].message.content or ""

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        """streaming completion, yields text chunks."""
        try:
            resp = await self._create_with_rate_retry(
                messages, max_tokens, temperature, stream=True
            )
            async for chunk in resp:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        except Exception as e:
            logger.error("LLM stream failed: %s", e)
            yield f"[LLM Error: {e}]"
