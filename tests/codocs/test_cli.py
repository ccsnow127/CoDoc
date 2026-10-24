from __future__ import annotations

import json
import shutil
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from codocs import pipeline as pl
from codocs.cli import cli
from codocs.config import Settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")

COMMANDS = ["analyze", "baseline", "testgen", "docgen", "wiki", "run", "report", "translate"]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("CODOCS_GO", "")


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


@pytest.mark.parametrize("cmd", COMMANDS)
def test_help(cmd: str):
    res = _invoke(cmd, "--help")
    assert res.exit_code == 0, res.output
    assert "Usage:" in res.output


def test_group_help():
    res = _invoke("--help")
    assert res.exit_code == 0
    for cmd in COMMANDS:
        assert cmd in res.output


def test_options_present():
    out = _invoke("run", "--help").output
    for opt in ("--model", "--concurrency", "--workers", "--go", "--go-timeout", "--force",
                "--subtree", "--packages", "--files", "--max-files", "--doc-rounds",
                "--doc-threshold", "--test-rounds", "--test-threshold", "--mutants",
                "--mutant-seed", "--max-file-lines", "--skip", "--no-generated-tests",
                "--refresh-llm", "--no-sandbox"):
        assert opt in out
    assert "--plan-only" in _invoke("wiki", "--help").output
    assert "--no-tree-docs" in _invoke("wiki", "--help").output


@needs_go
def test_analyze_fixture(tmp_path: Path):
    out = tmp_path / "out"
    res = _invoke("analyze", str(FIXTURE), "-o", str(out))
    assert res.exit_code == 0, res.output
    data = json.loads((out / "analysis.json").read_text())
    assert data["module"] == "example.com/fixture"
    rels = {p["rel"]: p for p in data["packages"]}
    assert set(rels) == {"calc", "shapes"}
    assert rels["calc"]["go_files"] == 2 and rels["calc"]["test_files"] == 2
    assert rels["shapes"]["test_files"] == 0
    assert {t["file"] for t in data["targets"]} == {"calc/calc.go", "calc/stats.go",
                                                    "shapes/shapes.go"}
    assert data["totals"]["files"] == 3 and data["go_version"]
    assert (out / "logs" / "codocs.log").is_file()


@needs_go
def test_analyze_selection(tmp_path: Path):
    out = tmp_path / "out"
    res = _invoke("analyze", str(FIXTURE), "-o", str(out), "-p", "shapes")
    assert res.exit_code == 0, res.output
    data = json.loads((out / "analysis.json").read_text())
    assert [p["rel"] for p in data["packages"]] == ["shapes"]
    res = _invoke("analyze", str(FIXTURE), "-o", str(out), "--files", "calc/s*.go")
    data = json.loads((out / "analysis.json").read_text())
    assert [t["file"] for t in data["targets"]] == ["calc/stats.go"]
    assert data["selection"]["files"] == "calc/s*.go"


def test_analyze_bad_go(tmp_path: Path):
    res = _invoke("analyze", str(FIXTURE), "--go", str(tmp_path / "no-such-go"))
    assert res.exit_code == 1
    assert "cannot run the go binary" in res.output


def test_analyze_not_a_module(tmp_path: Path):
    res = _invoke("analyze", str(tmp_path))
    assert res.exit_code == 1
    assert "no go.mod" in res.output


def test_llm_commands_need_key(tmp_path: Path):
    for cmd in ("testgen", "docgen", "run"):
        res = _invoke(cmd, str(FIXTURE), "-o", str(tmp_path / "out"))
        assert res.exit_code == 1, cmd
        assert "OPENAI_API_KEY" in res.output
    res = _invoke("wiki", str(FIXTURE), "-o", str(tmp_path / "out"))
    assert res.exit_code == 1 and "OPENAI_API_KEY" in res.output


def test_wiki_plan_only(tmp_path: Path):
    out = tmp_path / "out"
    d = out / "docs" / "calc" / "markdown_docs"
    d.mkdir(parents=True)
    (d / "stats.md").write_text("# calc/stats.go\n\n## ClassDef Stats\n**Stats**: totals.\n")
    res = _invoke("wiki", str(FIXTURE), "-o", str(out), "--plan-only")
    assert res.exit_code == 0, res.output
    assert "generated docs 1" in res.output and "static 1" in res.output
    assert not (out / "wiki").exists()


def test_run_rejects_unknown_skip(tmp_path: Path):
    res = _invoke("run", str(FIXTURE), "-o", str(tmp_path / "out"), "--skip", "docgen,bogus")
    assert res.exit_code == 1
    assert "bogus" in res.output


# -- run with fake stages ---------------------------------------------------------


@dataclass
class FakeResult:
    data: dict = field(default_factory=dict)

    @property
    def file(self) -> str:
        return self.data["file"]

    def to_dict(self) -> dict:
        return dict(self.data)


class FakeLLM:
    instances: list = []

    def __init__(self, settings, cache_path=None):
        self.settings = settings
        self.calls = 0
        self.cache_hits = 0
        self.closed = False
        FakeLLM.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    @property
    def tokens(self) -> dict:
        return {"input": 100 * self.calls, "output": 10 * self.calls}

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        self.calls += 1
        return "ok"


def _write_result(out: Path, stage: str, t, data: dict) -> None:
    d = out / stage / (t.pkg or "_root") / t.stem
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(data))


def _install_fakes(monkeypatch, calls: list):
    async def run_testgen(targets, pkgs, runner, llm, settings, out_dir, module_root,
                          force=False, on_progress=None):
        calls.append(("testgen", settings.test_rounds, force))
        res = []
        for t in targets:
            await llm.complete("s", "u")
            m = {"build_ok": True, "green_size": 2, "cov": 0.8, "mut": 0.5}
            data = {"pkg": t.pkg, "file": t.file, "stem": t.stem, "status": t.status,
                    "rounds": [{"round": 1, "measurement": m, "seconds": 0.1}],
                    "best_round": 1, "best_score": 0.4, "best_cov": 0.8, "best_mut": 0.5,
                    "best_size": 2, "early_stop": False, "threshold": settings.test_threshold,
                    "max_rounds": settings.test_rounds}
            _write_result(out_dir, "testgen", t, data)
            r = FakeResult(data)
            if on_progress:
                on_progress(r)
            res.append(r)
        return res

    async def run_docgen(targets, pkgs, runner, llm, settings, out_dir, module_root,
                         force=False, on_progress=None, use_generated_tests=True):
        calls.append(("docgen", settings.doc_rounds, sorted(pkgs), use_generated_tests))
        assert (out_dir / "analysis.json").is_file()
        res = []
        for t in targets:
            await llm.complete("s", "u")
            data = {"pkg": t.pkg, "file": t.file, "stem": t.stem, "status": "ok",
                    "signal": "tests", "rounds": [{"round": 1, "eval": {"phi": 1.0}}],
                    "best_round": 1, "best_phi": 1.0, "early_stop": True,
                    "threshold": settings.doc_threshold, "max_rounds": settings.doc_rounds}
            _write_result(out_dir, "docgen", t, data)
            r = FakeResult(data)
            if on_progress:
                on_progress(r)
            res.append(r)
        return res

    async def run_wiki(module_root, out_dir, settings, subtree=None, use_tree_docs=True,
                       fmt="both", plan_only=False, on_progress=None, packages=None):
        calls.append(("wiki", subtree, use_tree_docs, fmt, plan_only))
        if on_progress:
            on_progress("wiki pages")
        return {"packages": 2, "leaf_out": 2, "leaf_tree": 0, "static": 0, "pages": 4,
                "html": None, "tokens": {"input": 7, "output": 3}}

    import codocs.llm
    import codocs.wiki

    tes = types.ModuleType("codocs.tesearch.loop")
    tes.run_testgen = run_testgen
    spec = types.ModuleType("codocs.specforge.loop")
    spec.run_docgen = run_docgen
    monkeypatch.setitem(sys.modules, "codocs.tesearch.loop", tes)
    monkeypatch.setitem(sys.modules, "codocs.specforge.loop", spec)
    monkeypatch.setattr(codocs.llm, "LLM", FakeLLM)
    monkeypatch.setattr(codocs.wiki, "run_wiki", run_wiki)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    FakeLLM.instances = []


@needs_go
def test_run_with_fake_stages(tmp_path: Path, monkeypatch):
    calls: list = []
    _install_fakes(monkeypatch, calls)
    out = tmp_path / "out"
    res = _invoke("run", str(FIXTURE), "-o", str(out), "--doc-rounds", "2", "--test-rounds",
                  "4", "-j", "2", "--no-tree-docs", "-f", "markdown", "--workers", "2")
    assert res.exit_code == 0, res.output
    assert [c[0] for c in calls] == ["testgen", "docgen", "wiki"]
    assert calls[0][1] == 4 and calls[1][1] == 2
    assert calls[1][2] == ["calc", "shapes"] and calls[1][3] is True
    assert calls[2] == ("wiki", None, False, "markdown", False)
    assert len(FakeLLM.instances) == 1 and FakeLLM.instances[0].closed

    meta = json.loads((out / "run_meta.json").read_text())
    assert set(meta["stages"]) == {"analyze", "baseline", "testgen", "docgen", "wiki"}
    assert meta["settings"]["doc_rounds"] == 2 and meta["settings"]["concurrency"] == 2
    assert "api_key" not in meta["settings"]
    assert meta["stages"]["testgen"]["tokens"] == {"input": 300, "output": 30}
    assert meta["stages"]["docgen"]["tokens"] == {"input": 300, "output": 30}
    assert meta["tokens"] == {"input": 607, "output": 63}
    assert meta["llm"] == {"calls": 6, "cache_hits": 0}
    assert meta["stages"]["baseline"]["passing"] == 5
    assert all(meta["stages"][s]["seconds"] >= 0 for s in meta["stages"])

    assert (out / "baseline" / "calc.json").is_file()
    summary = json.loads((out / "summary.json").read_text())
    assert summary["docgen"]["aggregate"]["files"] == 3
    assert summary["docgen"]["aggregate"]["mean_best_phi"] == 1.0
    assert summary["testgen"]["aggregate"]["mean_best_score"] == 0.4
    assert summary["tokens"] == {"input": 607, "output": 63}
    assert "calc/stats.go" in (out / "report.md").read_text()


@needs_go
def test_run_skip(tmp_path: Path, monkeypatch):
    calls: list = []
    _install_fakes(monkeypatch, calls)
    out = tmp_path / "out"
    res = _invoke("run", str(FIXTURE), "-o", str(out), "--skip", "baseline,testgen,wiki",
                  "-p", "calc", "--force", "--no-generated-tests")
    assert res.exit_code == 0, res.output
    assert [c[0] for c in calls] == ["docgen"]
    assert calls[0][2] == ["calc"] and calls[0][3] is False
    meta = json.loads((out / "run_meta.json").read_text())
    assert set(meta["stages"]) == {"analyze", "docgen"}
    assert not (out / "baseline").exists()
    assert (out / "report.md").is_file()


@needs_go
def test_run_without_llm_stages_needs_no_key(tmp_path: Path, monkeypatch):
    out = tmp_path / "out"
    res = _invoke("run", str(FIXTURE), "-o", str(out), "--skip", "testgen,docgen,wiki")
    assert res.exit_code == 0, res.output
    meta = json.loads((out / "run_meta.json").read_text())
    assert set(meta["stages"]) == {"analyze", "baseline"}


@needs_go
def test_testgen_and_docgen_commands(tmp_path: Path, monkeypatch):
    calls: list = []
    _install_fakes(monkeypatch, calls)
    out = tmp_path / "out"
    res = _invoke("testgen", str(FIXTURE), "-o", str(out), "--max-files", "1",
                  "--mutants", "0")
    assert res.exit_code == 0, res.output
    res = _invoke("docgen", str(FIXTURE), "-o", str(out), "--files", "shapes/*")
    assert res.exit_code == 0, res.output
    assert [c[0] for c in calls] == ["testgen", "docgen"]
    meta = json.loads((out / "run_meta.json").read_text())
    assert meta["settings"]["mutants"] == 20  # last command's settings
    assert meta["tokens"] == {"input": 200, "output": 20}
    assert (out / "report.md").is_file()


@needs_go
def test_baseline_command(tmp_path: Path):
    mod = tmp_path / "gomod"
    shutil.copytree(FIXTURE, mod)
    out = tmp_path / "out"
    res = _invoke("baseline", str(mod), "-o", str(out))
    assert res.exit_code == 0, res.output
    calc = json.loads((out / "baseline" / "calc.json").read_text())
    assert "TestKnownBroken" in calc["failing"] and "TestSum" in calc["passing"]
    assert json.loads((out / "baseline" / "shapes.json").read_text())["passing"] == []


def test_report_command(tmp_path: Path):
    out = tmp_path / "out"
    d = out / "docgen" / "calc" / "stats"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({
        "pkg": "calc", "file": "calc/stats.go", "stem": "stats", "status": "ok",
        "signal": "tests", "rounds": [{"round": 1, "eval": {"phi": 0.8}}],
        "best_round": 1, "best_phi": 0.8, "early_stop": False, "threshold": 0.9,
        "max_rounds": 3}))
    res = _invoke("report", str(out))
    assert res.exit_code == 0, res.output
    assert "mean_best_phi=0.800" in res.output
    assert "calc/stats.go" in (out / "report.md").read_text()
    assert (out / "summary.json").is_file()
    assert _invoke("report", str(tmp_path / "missing")).exit_code == 1


def test_translate_uses_codocs_settings(tmp_path: Path, monkeypatch):
    """the RepoWiki translator (--glossary go) gets codocs' settings: .env, CODOCS_MODEL and
    the model aliases, like the COBOL glossary."""
    import codocs.cli as cli_mod

    seen: dict = {}

    async def fake_translate(src, dst, lang, settings, no_html, kind):
        seen.update(src=src, dst=dst, lang=lang, model=settings.model, key=settings.api_key,
                    no_html=no_html, kind=kind)

    monkeypatch.setattr(cli_mod, "_translate_wiki", fake_translate)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# Overview\n")
    res = _invoke("translate", str(wiki), "-o", str(tmp_path / "ja"), "-l", "ja", "-m", "gpt",
                  "--no-html")
    assert res.exit_code == 0, res.output
    assert seen == {"src": wiki, "dst": tmp_path / "ja", "lang": "ja", "model": "gpt-5.4",
                    "key": "sk-test", "no_html": True, "kind": "go"}
    monkeypatch.setenv("CODOCS_MODEL", "judge")
    assert _invoke("translate", str(wiki), "-o", str(tmp_path / "ja")).exit_code == 0
    assert seen["model"] == "gpt-5.5"


def test_rich_hooks_progress():
    import io

    from rich.console import Console

    from codocs.cli import RichHooks, _progress

    progress = _progress(Console(file=io.StringIO()))
    hooks = RichHooks(progress)

    def completed(name: str) -> float:
        return next(t for t in progress.tasks if t.id == hooks.tasks[name]).completed

    hooks.stage_start("docgen", 3)
    hooks.callback("docgen")("docgen calc/a.go: ok phi=1.00")
    hooks.callback("docgen")(FakeResult({"file": "calc/b.go"}))
    assert completed("docgen") == 2
    hooks.stage_start("wiki")
    hooks.callback("wiki")("Generating 4 module pages...")
    assert completed("wiki") == 0
    hooks.stage_end("docgen", {"files": 3})
    assert completed("docgen") == 3


@needs_go
def test_selection_matching_nothing_fails(tmp_path: Path):
    res = _invoke("analyze", str(FIXTURE), "-p", "calcc")
    assert res.exit_code == 1
    assert "matched no Go package" in res.output


@needs_go
def test_run_passes_the_selection_to_the_wiki(tmp_path: Path, monkeypatch):
    calls: list = []
    _install_fakes(monkeypatch, calls)
    seen = {}
    import codocs.wiki

    async def run_wiki(module_root, out_dir, settings, packages=None, **kw):
        seen["packages"] = packages
        return {"packages": 1, "leaf_out": 0, "leaf_tree": 0, "static": 1, "pages": 1,
                "html": None, "tokens": {"input": 0, "output": 0}}

    monkeypatch.setattr(codocs.wiki, "run_wiki", run_wiki)
    res = _invoke("run", str(FIXTURE), "-o", str(tmp_path / "out"), "-p", "calc",
                  "--skip", "testgen,docgen", "--workers", "2")
    assert res.exit_code == 0, res.output
    assert seen["packages"] == ["calc"]
    assert (tmp_path / "out" / "work" / "sandbox" / FIXTURE.name / "calc" / "calc.go").is_file()


class _Interrupted(BaseException):
    pass


class _MeteredLLM:
    def __init__(self, settings):
        self.calls = 0
        self.cache_hits = 0
        self.spent = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    @property
    def tokens(self):
        return {"input": self.spent, "output": self.spent // 10}

    async def complete(self, system, user, **kw):
        self.calls += 1
        self.spent += 1000
        return "x"


@needs_go
async def test_tokens_survive_an_interrupted_stage(tmp_path: Path, monkeypatch):
    import codocs.tesearch.loop as tes

    async def run_testgen(targets, pkgs, runner, llm, settings, out_dir, module_root,
                          force=False, on_progress=None):
        for t in targets[:2]:
            await llm.complete("s", "u")
            await llm.complete("s", "u2")
            on_progress(t)
        raise _Interrupted()

    monkeypatch.setattr(tes, "run_testgen", run_testgen)
    out = tmp_path / "out"
    with pytest.raises(_Interrupted):
        await pl.run(FIXTURE, out, Settings(workers=2), skip=("baseline", "docgen", "wiki"),
                     llm_factory=_MeteredLLM)
    meta = json.loads((out / "run_meta.json").read_text())
    assert meta["tokens"] == {"input": 4000, "output": 400}
    assert meta["llm"] == {"calls": 4, "cache_hits": 0}
