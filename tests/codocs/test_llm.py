from __future__ import annotations

from pathlib import Path

import pytest

from codocs.config import Settings
from codocs.llm import LLM, LLMError
from codocs.repowiki.llm.client import LLMClient


class FakeReplies:
    """stands in for LLMClient.complete; records requests and counts tokens."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def install(self, monkeypatch) -> FakeReplies:
        fake = self

        async def complete(client, messages, *, temperature=0.3, max_tokens=4096, **kw):
            fake.requests.append(
                {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            )
            reply = fake.replies.pop(0)
            if not reply.startswith("[LLM Error"):
                client.total_input_tokens += 10
                client.total_output_tokens += 3
            return reply

        monkeypatch.setattr(LLMClient, "complete", complete)
        return self


@pytest.fixture
def settings(tmp_path):
    return Settings(api_key="test-key", api_base="http://127.0.0.1:9/v1",
                    cache_path=str(tmp_path / "unused" / "cache.db"))


@pytest.fixture
def cache_file(tmp_path):
    return str(tmp_path / "nested" / "dir" / "cache.db")


async def test_complete_and_cache_hit(monkeypatch, settings, cache_file):
    fake = FakeReplies(["hello"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        assert await llm.complete("sys", "usr", tag="t") == "hello"
        assert await llm.complete("sys", "usr", tag="t") == "hello"
        assert llm.calls == 1
        assert llm.cache_hits == 1
        assert llm.tokens == {"input": 10, "output": 3}
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert req["temperature"] == settings.temperature
    assert req["max_tokens"] == settings.max_tokens


async def test_cache_persists_across_instances(monkeypatch, settings, cache_file):
    fake = FakeReplies(["first"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        await llm.complete("s", "u")
    async with LLM(settings, cache_path=cache_file) as llm:
        assert await llm.complete("s", "u") == "first"
        assert llm.calls == 0 and llm.cache_hits == 1
        assert llm.tokens == {"input": 0, "output": 0}
    assert len(fake.requests) == 1


async def test_cache_path_defaults_to_settings(monkeypatch, tmp_path):
    FakeReplies(["x"]).install(monkeypatch)
    s = Settings(api_key="k", cache_path=str(tmp_path / "a" / "b.db"))
    async with LLM(s) as llm:
        await llm.complete("s", "u")
    assert (tmp_path / "a" / "b.db").exists()


async def test_key_depends_on_params(monkeypatch, settings, cache_file):
    fake = FakeReplies(["a", "b", "c", "d"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        assert await llm.complete("s", "u") == "a"
        assert await llm.complete("s", "u", temperature=0.0) == "b"
        assert await llm.complete("s", "u", max_tokens=100) == "c"
        assert await llm.complete("s2", "u") == "d"
        assert llm.calls == 4 and llm.cache_hits == 0
    assert fake.requests[1]["temperature"] == 0.0
    assert fake.requests[2]["max_tokens"] == 100


async def test_model_is_part_of_key(monkeypatch, settings, cache_file):
    FakeReplies(["a", "b"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        await llm.complete("s", "u")
    other = Settings(api_key="k", model="other/model", cache_path=settings.cache_path)
    async with LLM(other, cache_path=cache_file) as llm:
        assert await llm.complete("s", "u") == "b"
        assert llm.calls == 1


async def test_error_reply_raises_and_is_not_cached(monkeypatch, settings, cache_file):
    fake = FakeReplies(["[LLM Error: boom]", "ok"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        with pytest.raises(LLMError, match="boom"):
            await llm.complete("s", "u", tag="seed")
        assert await llm.complete("s", "u") == "ok"
        assert llm.calls == 2 and llm.cache_hits == 0
    assert len(fake.requests) == 2


async def test_empty_reply_raises_and_is_not_cached(monkeypatch, settings, cache_file):
    FakeReplies(["", "   \n", "ok"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        with pytest.raises(LLMError):
            await llm.complete("s", "u")
        with pytest.raises(LLMError):
            await llm.complete("s", "u")
        assert await llm.complete("s", "u") == "ok"
        assert llm.calls == 3


async def test_tokens_accumulate(monkeypatch, settings, cache_file):
    FakeReplies(["a", "b", "c"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        assert llm.tokens == {"input": 0, "output": 0}
        for u in ("1", "2", "3"):
            await llm.complete("s", u)
        await llm.complete("s", "1")
        assert llm.tokens == {"input": 30, "output": 9}
        assert llm.calls == 3 and llm.cache_hits == 1


async def test_works_without_cache_context(monkeypatch, settings):
    fake = FakeReplies(["a", "b"]).install(monkeypatch)
    llm = LLM(settings)
    assert await llm.complete("s", "u") == "a"
    assert await llm.complete("s", "u") == "b"
    assert len(fake.requests) == 2
    assert not Path(settings.cache_path).exists()


class _Resp:
    def __init__(self, text: str, finish: str):
        msg = type("Msg", (), {"content": text})()
        self.choices = [type("Choice", (), {"message": msg, "finish_reason": finish})()]
        self.usage = type("Usage", (), {"prompt_tokens": 5, "completion_tokens": 7})()


def _install_create(monkeypatch, replies: list[tuple[str, str]]) -> list[dict]:
    seen: list[dict] = []

    async def create(client, messages, max_tokens, temperature, **extra):
        seen.append({"max_tokens": max_tokens})
        return _Resp(*replies.pop(0))

    monkeypatch.setattr(LLMClient, "_create", create)
    return seen


async def test_truncated_reply_raises_and_is_not_cached(monkeypatch, settings, cache_file):
    seen = _install_create(monkeypatch, [("package calc\n\nfunc Clamp(", "length"),
                                         ("package calc\n", "stop")])
    async with LLM(settings, cache_path=cache_file) as llm:
        with pytest.raises(LLMError, match="truncated"):
            await llm.complete("s", "u", max_tokens=50)
        assert await llm.complete("s", "u", max_tokens=50) == "package calc\n"
        assert await llm.complete("s", "u", max_tokens=50) == "package calc\n"
        assert llm.calls == 2 and llm.cache_hits == 1
    assert [s["max_tokens"] for s in seen] == [50, 50]


async def test_refresh_bypasses_cached_replies(monkeypatch, settings, cache_file):
    FakeReplies(["old", "new"]).install(monkeypatch)
    async with LLM(settings, cache_path=cache_file) as llm:
        assert await llm.complete("s", "u") == "old"
    async with LLM(settings, cache_path=cache_file, refresh=True) as llm:
        assert await llm.complete("s", "u") == "new"
        assert llm.calls == 1 and llm.cache_hits == 0
    async with LLM(settings, cache_path=cache_file) as llm:
        assert await llm.complete("s", "u") == "new"
        assert llm.cache_hits == 1
    refreshing = Settings(api_key="k", refresh_llm=True, cache_path=settings.cache_path)
    assert LLM(refreshing).refresh
