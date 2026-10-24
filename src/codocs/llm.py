"""cached async llm completion on top of repowiki's LLMClient and Cache."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

from codocs.config import Settings
from codocs.repowiki.core.cache import Cache
from codocs.repowiki.llm.client import LLMClient

logger = logging.getLogger(__name__)

_CACHE_TTL = 10 * 365 * 24 * 3600
_RETRY_NUDGE = "That reply could not be used. Return only the JSON object requested, nothing else."


class LLMError(RuntimeError):
    """the llm returned an error, an empty reply, or a reply cut off at max_tokens."""


class _Client(LLMClient):
    """repowiki's client that also remembers why the last completion stopped."""

    last_finish_reason: str | None = None

    async def _create_with_rate_retry(self, messages, max_tokens, temperature, **extra):
        self.last_finish_reason = None
        resp = await super()._create_with_rate_retry(messages, max_tokens, temperature, **extra)
        choices = getattr(resp, "choices", None) or []
        if choices:
            self.last_finish_reason = getattr(choices[0], "finish_reason", None)
        return resp


class LLM:
    """cached completion: identical requests are answered from the sqlite cache."""

    def __init__(self, settings: Settings, cache_path: str | None = None,
                 refresh: bool | None = None):
        self.settings = settings
        self.cache_path = Path(cache_path or settings.cache_path).expanduser()
        self.refresh = settings.refresh_llm if refresh is None else refresh
        self._cache: Cache | None = None
        self._client: LLMClient | None = None
        self._others: dict[str, LLMClient] = {}  # clients of `model=` overrides
        self.calls = 0
        self.cache_hits = 0

    async def __aenter__(self) -> LLM:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = Cache(self.cache_path)
        await self._cache.init()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._cache is not None:
            await self._cache.close()
            self._cache = None

    @property
    def client(self) -> LLMClient:
        """the underlying client, created on first use so cache-only runs need no key."""
        if self._client is None:
            s = self.settings
            self._client = _Client(s.model, api_key=s.api_key, api_base=s.api_base)
        return self._client

    def _client_for(self, model: str | None) -> LLMClient:
        """the main client, or (for another model) a second client created on first use."""
        if not model or model == self.settings.model:
            return self.client
        if model not in self._others:
            s = self.settings
            self._others[model] = _Client(model, api_key=s.api_key, api_base=s.api_base)
        return self._others[model]

    @property
    def tokens(self) -> dict:
        clients = [c for c in (self._client, *self._others.values()) if c is not None]
        return {
            "input": sum(c.total_input_tokens for c in clients),
            "output": sum(c.total_output_tokens for c in clients),
        }

    def _key(self, system: str, user: str, temperature: float, max_tokens: int,
             model: str | None = None) -> str:
        blob = json.dumps(
            [model or self.settings.model, system, user, temperature, max_tokens],
            ensure_ascii=False,
        )
        return "codocs-llm:" + hashlib.sha256(blob.encode()).hexdigest()

    async def complete(
        self,
        system: str,
        user: str,
        *,
        tag: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        validate: Callable[[str], bool] | None = None,
        model: str | None = None,
    ) -> str:
        """system + user -> reply text; raises LLMError on an error, empty or truncated reply.

        *model*, when given and not the settings' model, answers with that model (a second
        client, created on first use); it is part of the cache key.

        with refresh, cached replies are not read (fresh replies still overwrite them).
        with `validate`, a cached reply that fails it counts as a miss, a fresh reply that
        fails it is asked for once more, and only a valid reply is cached (LLMError if the
        second reply fails too).
        """
        if temperature is None:
            temperature = self.settings.temperature
        if max_tokens is None:
            max_tokens = self.settings.max_tokens
        if model == self.settings.model:
            model = None
        key = self._key(system, user, temperature, max_tokens, model)

        if self._cache is not None and not self.refresh:
            hit = await self._cache.get(key, ttl=_CACHE_TTL)
            if isinstance(hit, dict) and isinstance(hit.get("text"), str) and hit["text"] \
                    and (validate is None or validate(hit["text"])):
                self.cache_hits += 1
                return hit["text"]

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        reply = await self._ask(messages, tag, temperature, max_tokens, model)
        if validate is not None and not validate(reply):
            logger.info("llm %s: invalid reply, asking again", tag or "-")
            retry = messages + [{"role": "assistant", "content": reply},
                                {"role": "user", "content": _RETRY_NUDGE}]
            reply = await self._ask(retry, tag, temperature, max_tokens, model)
            if not validate(reply):
                raise LLMError(f"{tag or 'llm'}: reply not in the requested format")

        if self._cache is not None:
            await self._cache.put(key, {"text": reply})
        logger.debug("llm %s: %d chars", tag or "-", len(reply))
        return reply

    async def _ask(self, messages: list[dict], tag: str, temperature: float,
                   max_tokens: int, model: str | None = None) -> str:
        self.calls += 1
        client = self._client_for(model)
        if isinstance(client, _Client):
            client.last_finish_reason = None
        reply = await client.complete(messages, temperature=temperature, max_tokens=max_tokens)
        if getattr(client, "last_finish_reason", None) == "length":
            raise LLMError(f"{tag or 'llm'}: reply truncated at max_tokens={max_tokens} "
                           "(finish_reason=length)")
        if reply.startswith("[LLM Error"):
            raise LLMError(f"{tag or 'llm'}: {reply}")
        if not reply.strip():
            raise LLMError(f"{tag or 'llm'}: empty reply")
        return reply
