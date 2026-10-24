from __future__ import annotations

import os

import pytest

from codocs.config import MODEL_ALIASES, Settings, resolve_model

_VARS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODOCS_MODEL", "CODOCS_LANG", "CODOCS_GO")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_resolve_model():
    assert resolve_model("gpt") == "gpt-5.4"
    assert resolve_model("gpt-mini") == MODEL_ALIASES["gpt-mini"]
    assert resolve_model("other/model") == "other/model"


def test_defaults(clean_env):
    s = Settings.load()
    assert s.model == "gpt-5.4"
    assert s.api_key == ""
    assert s.go == "go"
    assert s.doc_rounds == 3 and s.test_rounds == 3
    assert s.cache_path == "~/.codocs/cache.db"


def test_env_vars(clean_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw/v1")
    monkeypatch.setenv("CODOCS_MODEL", "gpt-mini")
    monkeypatch.setenv("CODOCS_LANG", "ja")
    monkeypatch.setenv("CODOCS_GO", "/opt/go/bin/go")
    s = Settings.load()
    assert s.api_key == "sk-env"
    assert s.api_base == "http://gw/v1"
    assert s.model == "gpt-5.4-mini"
    assert s.language == "ja"
    assert s.go == "/opt/go/bin/go"


def test_dotenv_in_cwd(clean_env, monkeypatch):
    (clean_env / ".env").write_text("CODOCS_GO=/from/dotenv/go\nCODOCS_LANG=ja\n")
    monkeypatch.setenv("CODOCS_LANG", "ko")
    try:
        s = Settings.load()
    finally:
        os.environ.pop("CODOCS_GO", None)
    assert s.go == "/from/dotenv/go"
    assert s.language == "ko"


def test_overrides_beat_env(clean_env, monkeypatch):
    monkeypatch.setenv("CODOCS_MODEL", "env/model")
    monkeypatch.setenv("CODOCS_GO", "/env/go")
    s = Settings.load(model="gpt", go=None, doc_rounds=5, concurrency=None)
    assert s.model == "gpt-5.4"
    assert s.go == "/env/go"
    assert s.doc_rounds == 5
    assert s.concurrency == 4


def test_unknown_override_rejected(clean_env):
    with pytest.raises(TypeError):
        Settings.load(no_such_field=1)


def test_to_dict_omits_api_key(clean_env):
    s = Settings.load(api_key="sk-secret", model="x/y")
    d = s.to_dict()
    assert "api_key" not in d
    assert "sk-secret" not in str(d)
    assert d["model"] == "x/y"
    assert d["test_threshold"] == 0.85
