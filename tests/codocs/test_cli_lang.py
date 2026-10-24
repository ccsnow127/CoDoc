"""cli in multi mode: language options, detection errors, analyze/baseline/testgen/docgen/run."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_loops_lang import PY_SUITES, ScriptedLLM, copy_fixture, snapshot

from codocs.cli import _settings, cli

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GO = shutil.which("go")
LANG_OPTIONS = ("--source-lang", "--test-timeout", "--py", "--pytest-args", "--java-home",
                "--mvn", "--java-runner", "--jacoco-version", "--cc", "--cxx", "--cc-flags",
                "--ld-flags", "--cc-db", "--cc-root", "--build-cmd", "--test-cmd", "--gcov",
                "--include-generated")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    for var in ("CODOCS_GO", "CODOCS_SOURCE_LANG", "CODOCS_PY", "CODOCS_CC"):
        monkeypatch.setenv(var, "")


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _patch_llm(monkeypatch, repo: Path) -> list:
    made: list = []

    class LLM(ScriptedLLM):
        def __init__(self, settings, cache_path=None):
            super().__init__(repo, PY_SUITES)
            made.append(self)

    import codocs.llm

    monkeypatch.setattr(codocs.llm, "LLM", LLM)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return made


@pytest.mark.parametrize("cmd", ["analyze", "baseline", "testgen", "docgen", "run", "wiki",
                                 "bizlogic"])
def test_language_options_present(cmd: str):
    out = _invoke(cmd, "--help").output
    for opt in LANG_OPTIONS:
        assert opt in out, (cmd, opt)
    assert "--go" in out and "--workers" in out


def test_settings_from_options():
    s = _settings(source_lang="go,c", cc_flags="-DX=1 -O1", ld_flags="-lz", py="/x/python",
                  java_runner="mvn", include_generated=True, test_timeout=7, cc_root="native",
                  test_cmd="make test", build_cmd="make", gcov="gcov-13", cxx="g++",
                  jacoco_version="0.8.13", java_home="/jdk", mvn="/bin/mvn", cc_db="db.json",
                  pytest_args="-k x", force=False)
    assert (s.source_lang, s.cc_flags, s.ld_flags, s.py, s.java_runner) == (
        "go,c", "-DX=1 -O1", "-lz", "/x/python", "mvn")
    assert s.include_generated is True and s.test_timeout == 7 and s.cc_root == "native"
    assert (s.test_cmd, s.build_cmd, s.gcov, s.cxx, s.jacoco_version) == (
        "make test", "make", "gcov-13", "g++", "0.8.13")
    assert (s.java_home, s.mvn, s.cc_db, s.pytest_args) == ("/jdk", "/bin/mvn", "db.json", "-k x")
    d = _settings().to_dict()
    assert d["source_lang"] == "auto" and d["include_generated"] is False and d["cc"] == "cc"


def test_env_settings(monkeypatch):
    monkeypatch.setenv("CODOCS_SOURCE_LANG", "python")
    monkeypatch.setenv("CODOCS_TEST_TIMEOUT", "11")
    monkeypatch.setenv("CODOCS_CC_FLAGS", "-DY")
    s = _settings()
    assert (s.source_lang, s.test_timeout, s.cc_flags) == ("python", 11, "-DY")
    monkeypatch.setenv("CODOCS_TEST_TIMEOUT", "soon")
    assert _settings().test_timeout == 0


def test_detection_errors(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    for cmd in ("analyze", "wiki"):
        args = [cmd, str(empty)] + (["-o", str(tmp_path / "o"), "--plan-only"]
                                    if cmd == "wiki" else [])
        res = _invoke(*args)
        assert res.exit_code == 1 and "no go.mod" in res.output, res.output
    res = _invoke("analyze", str(FIXTURES / "pymod"), "--source-lang", "java")
    assert res.exit_code == 1 and "no Java files" in res.output
    res = _invoke("analyze", str(FIXTURES / "pymod"), "--source-lang", "cobol")
    assert res.exit_code == 1 and "unknown language" in res.output


def test_analyze_multi(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = tmp_path / "out"
    res = _invoke("analyze", str(repo), "-o", str(out))
    assert res.exit_code == 0, res.output
    assert "python: 3 targets (1 skipped: no-decls 1)" in res.output
    assert "src/pymod" in res.output and "lang" in res.output
    d = _json(out / "analysis.json")
    assert d["languages"] == ["python"] and d["packages"][0]["lang"] == "python"
    res = _invoke("analyze", str(repo), "-p", "nowhere")
    assert res.exit_code == 1 and "the selection matched no package (python)" in res.output


def test_baseline_multi(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = tmp_path / "out"
    res = _invoke("baseline", str(repo), "-o", str(out), "--workers", "2")
    assert res.exit_code == 0, res.output
    base = _json(out / "baseline" / "python" / "src__pymod.json")
    assert base["failing"] == ["tests/test_calc.py::test_known_broken"]
    meta = _json(out / "run_meta.json")
    assert meta["stages"]["baseline"]["by_language"]["python"]["passing"] == 5


def test_testgen_and_docgen_multi(tmp_path: Path, monkeypatch):
    repo = copy_fixture("pymod", tmp_path)
    before = snapshot(repo)
    made = _patch_llm(monkeypatch, repo)
    out = tmp_path / "out"
    res = _invoke("testgen", str(repo), "-o", str(out), "--files", "src/pymod/calc.py",
                  "--mutants", "2", "--test-rounds", "1")
    assert res.exit_code == 0, res.output
    assert "python: 1 targets" in res.output
    assert (out / "tests" / "src/pymod" / "test_calc_codocs.py").is_file()
    res = _invoke("docgen", str(repo), "-o", str(out), "--files", "src/pymod/*.py",
                  "--doc-rounds", "1")
    assert res.exit_code == 0, res.output
    summary = _json(out / "summary.json")
    assert summary["docgen"]["by_language"]["python"]["files"] == 3
    assert summary["testgen"]["by_language"]["python"]["delivered_suites"] == 1
    assert [k for k, _, _ in made[-1].log].count("doc") == 3
    assert snapshot(repo) == before


def test_run_multi(tmp_path: Path, monkeypatch):
    repo = copy_fixture("pymod", tmp_path)
    made = _patch_llm(monkeypatch, repo)
    out = tmp_path / "out"
    res = _invoke("run", str(repo), "-o", str(out), "--skip", "wiki", "--mutants", "2",
                  "--test-rounds", "1", "--doc-rounds", "1", "-j", "2", "--workers", "2")
    assert res.exit_code == 0, res.output
    assert "python: 3 targets" in res.output
    meta = _json(out / "run_meta.json")
    assert set(meta["stages"]) == {"analyze", "baseline", "testgen", "docgen"}
    assert "## Languages" in (out / "report.md").read_text()
    assert len(made) == 1 and made[0].calls > 0
    res = _invoke("run", str(repo), "-o", str(out), "--skip", "wiki,docgen", "--source-lang",
                  "go")
    assert res.exit_code == 1 and "no go.mod" in " ".join(res.output.split())   # rich wraps


def test_wiki_accepts_non_go_repo(tmp_path: Path, monkeypatch):
    calls = []

    async def run_wiki(module_root, out_dir, settings, subtree=None, use_tree_docs=True,
                       fmt="both", plan_only=False, on_progress=None, packages=None):
        calls.append((Path(module_root), plan_only, settings.source_lang))
        return {"module": "pymod", "packages": 1, "leaf_out": 0, "leaf_tree": 0, "static": 1,
                "pages": 1, "sections": 1, "plan": [], "html": None, "tokens": {}}

    import codocs.wiki

    monkeypatch.setattr(codocs.wiki, "run_wiki", run_wiki)
    repo = FIXTURES / "pymod"
    res = _invoke("wiki", str(repo), "-o", str(tmp_path / "out"), "--plan-only",
                  "--source-lang", "python")
    assert res.exit_code == 0, res.output
    assert calls == [(repo.resolve(), True, "python")]


@pytest.mark.skipif(GO is None, reason="go toolchain not installed")
def test_go_module_with_other_sources_stays_legacy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODOCS_GO", GO)
    repo = tmp_path / "gomod"
    shutil.copytree(FIXTURES / "gomod", repo)
    (repo / "tools").mkdir()
    (repo / "tools" / "gen.py").write_text("def main():\n    return 0\n")
    out = tmp_path / "out"
    res = _invoke("analyze", str(repo), "-o", str(out))
    assert res.exit_code == 0, res.output
    assert "also found: python 1 source files" in res.output
    d = _json(out / "analysis.json")
    assert "languages" not in d and {p["rel"] for p in d["packages"]} == {"calc", "shapes"}
    res = _invoke("analyze", str(repo), "-o", str(out), "--source-lang", "all")
    assert res.exit_code == 0, res.output
    d = _json(out / "analysis.json")
    assert d["languages"] == ["go", "python"]
    assert {(p["lang"], p["rel"]) for p in d["packages"]} == {
        ("go", "calc"), ("go", "shapes"), ("python", "tools")}
