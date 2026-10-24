"""COBOL settings, environment variables and model aliases; LLM.complete(model=...) (offline)."""

from __future__ import annotations

import pytest

from codocs.config import MODEL_ALIASES, Settings, resolve_model
from codocs.llm import LLM
from codocs.repowiki.llm.client import LLMClient

_VARS = ("CODOCS_COBC", "CODOCS_COBC_FLAGS", "CODOCS_COBOL_COVERAGE", "CODOCS_COBOL_SPEC_SIGNAL",
         "CODOCS_EXCLUDE_PATHS", "CODOCS_JUDGE_MODEL", "CODOCS_CODEGEN_MODEL",
         "CODOCS_PHI_MODEL", "CODOCS_MODEL")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env of the developer's checkout
    return monkeypatch


def test_defaults():
    s = Settings()
    assert (s.cobc, s.cobc_flags, s.cobol_coverage, s.cobol_spec_signal) == (
        "cobc", "", "statement", "facts")
    assert s.exclude_paths == "" and s.exclude_list() == ()
    assert s.judge_model == "gpt-5.5" and s.codegen_model == ""
    d = s.to_dict()
    assert {"cobc", "cobc_flags", "cobol_coverage", "cobol_spec_signal", "exclude_paths",
            "judge_model", "codegen_model"} <= set(d) and "api_key" not in d


def test_aliases():
    assert MODEL_ALIASES == {"gpt": "gpt-5.4", "gpt-mini": "gpt-5.4-mini", "judge": "gpt-5.5"}
    assert resolve_model("gpt") == "gpt-5.4" and resolve_model("gpt-mini").endswith("mini")
    assert resolve_model("judge") == "gpt-5.5" and resolve_model("x/y") == "x/y"


def test_environment(clean_env):
    clean_env.setenv("CODOCS_COBC", "/opt/cobc")
    clean_env.setenv("CODOCS_COBC_FLAGS", "-std=mf -frelax-syntax-checks")
    clean_env.setenv("CODOCS_COBOL_COVERAGE", "paragraph")
    clean_env.setenv("CODOCS_COBOL_SPEC_SIGNAL", "compile")
    clean_env.setenv("CODOCS_EXCLUDE_PATHS", "app/data, app/catlg\napp/scheduler,")
    clean_env.setenv("CODOCS_JUDGE_MODEL", "gpt")
    clean_env.setenv("CODOCS_CODEGEN_MODEL", "judge")
    clean_env.setenv("CODOCS_PHI_MODEL", "x/ignored")
    s = Settings.load()
    assert s.cobc == "/opt/cobc" and s.cobc_flag_list() == ["-std=mf", "-frelax-syntax-checks"]
    assert s.cobol_coverage == "paragraph" and s.cobol_spec_signal == "compile"
    assert s.exclude_list() == ("app/data", "app/catlg", "app/scheduler")
    assert s.judge_model == "gpt-5.4" and s.codegen_model == "gpt-5.5"
    assert "x/ignored" not in s.to_dict().values()  # CODOCS_PHI_MODEL is not read
    # cli overrides win, also as a sequence of globs
    o = Settings.load(exclude_paths=("a/b", "c"), judge_model="judge", codegen_model="")
    assert o.exclude_list() == ("a/b", "c") and isinstance(o.exclude_paths, str)
    assert o.judge_model == "gpt-5.5" and o.codegen_model == ""


class _Replies:
    """stands in for LLMClient.complete; records the model of each request."""

    def __init__(self, monkeypatch):
        self.models: list[str] = []
        fake = self

        async def complete(client, messages, *, temperature=0.3, max_tokens=4096, **kw):
            fake.models.append(client.model)
            client.total_input_tokens += 10
            client.total_output_tokens += 3
            return f"reply from {client.model}"

        monkeypatch.setattr(LLMClient, "complete", complete)


@pytest.fixture
def settings(tmp_path):
    return Settings(api_key="test-key", api_base="http://127.0.0.1:9/v1", model="main/m",
                    cache_path=str(tmp_path / "cache.db"))


async def test_model_override_uses_a_second_client_and_its_own_key(monkeypatch, settings):
    replies = _Replies(monkeypatch)
    async with LLM(settings) as llm:
        assert await llm.complete("s", "u") == "reply from main/m"
        assert await llm.complete("s", "u", model="judge/m") == "reply from judge/m"
        assert await llm.complete("s", "u", model="judge/m") == "reply from judge/m"  # cached
        assert await llm.complete("s", "u", model="main/m") == "reply from main/m"  # = default
        assert replies.models == ["main/m", "judge/m"]
        assert llm.calls == 2 and llm.cache_hits == 2
        assert llm.tokens == {"input": 20, "output": 6}  # both clients summed
        assert llm.client.model == "main/m"
    # the override model is part of the cache key: a new instance still hits both entries
    async with LLM(settings) as llm:
        assert await llm.complete("s", "u", model="judge/m") == "reply from judge/m"
        assert await llm.complete("s", "u") == "reply from main/m"
        assert llm.calls == 0


def test_key_shape_is_unchanged_without_override(settings):
    llm = LLM(settings)
    assert llm._key("s", "u", 0.2, 10) == llm._key("s", "u", 0.2, 10, None)
    assert llm._key("s", "u", 0.2, 10, "main/m") == llm._key("s", "u", 0.2, 10)
    assert llm._key("s", "u", 0.2, 10, "other") != llm._key("s", "u", 0.2, 10)
