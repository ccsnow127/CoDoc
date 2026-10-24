"""pipeline in multi mode (runners, analysis, stage order, report) and in Go-only mode."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest
from test_loops_lang import PY_SUITES, ScriptedLLM, copy_fixture, snapshot

from codocs import pipeline as pl
from codocs.config import Settings
from codocs.report import build_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GO = shutil.which("go")
needs_go = pytest.mark.skipif(GO is None, reason="go toolchain not installed")


def _settings(**kw) -> Settings:
    base = dict(mutants=2, test_rounds=1, doc_rounds=1, concurrency=2, workers=4,
                go_timeout=120)
    base.update(kw)
    return Settings(**base)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _mixed(tmp_path: Path) -> Path:
    """python + c, no go.mod: a multi-mode repository that needs no toolchain to analyze."""
    root = tmp_path / "mixed"
    files = {
        "tool/app.py": "def f(x):\n    return x + 1\n",
        "tool/more.py": "def g(x):\n    return x * 2\n",
        "tool/test_app.py": "from app import f\n\n\ndef test_f():\n    assert f(1) == 2\n",
        "clib/a.c": "int add(int a, int b) { return a + b; }\n",
        "clib/b.c": "int sub(int a, int b) { return a - b; }\n",
        "clib/b.h": "int sub(int a, int b);\n",
        "other/c.c": "int mul(int a, int b) { return a * b; }\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_make_runners_and_analysis_layout(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = tmp_path / "out"
    s = _settings()
    runners = pl.make_runners(repo, s, out)
    assert runners.languages == ["python"] and not runners.detection.legacy
    assert runners["python"].adapter.ctx is runners.repo_ctx
    ctx = runners.run_ctx
    assert ctx.exec_root == ctx.sandbox and ctx.sandbox.parent == (out / "work/sandbox").resolve()
    a = asyncio.run(pl.analyze_all(runners, s, out, pl.Selection()))
    d = _json(out / "analysis.json")
    for key in ("module_root", "module", "go_version", "languages", "versions", "selection",
                "totals", "totals_by_language", "packages", "targets", "skipped", "notes"):
        assert key in d, key
    assert d["module"] == "pymod" and d["go_version"] == "" and d["languages"] == ["python"]
    assert d["versions"]["python"].startswith("Python ")
    [pkg] = d["packages"]
    assert pkg == {"lang": "python", "rel": "src/pymod", "import_path": "pymod", "name": "pymod",
                   "files": 4, "test_files": 2, "targets": {"no-decls": 1, "ok": 3},
                   "error": ""}
    assert all(t["lang"] == "python" for t in d["targets"])
    assert [t["file"] for t in d["skipped"]] == ["src/pymod/__init__.py"]
    assert d["totals"]["docgen_targets"] == 3 and d["totals"]["files"] == 4
    assert a.cost_lines() == ["python: 3 targets (1 skipped: no-decls 1)"]
    assert [t.file for t in a.work_targets("python")] == [
        "src/pymod/calc.py", "src/pymod/shapes.py", "src/pymod/stats.py"]


@pytest.mark.skipif(shutil.which("cc") is None, reason="cc not installed")
def test_selection_rules_across_languages(tmp_path: Path):
    repo = _mixed(tmp_path)
    s = _settings()
    runners = pl.make_runners(repo, s, None)
    assert runners.languages == ["c", "python"] and not runners.unavailable
    a = asyncio.run(pl.analyze_all(runners, s, None, pl.Selection(max_files=4)))
    processed = [t.file for t in a.targets if t.status in pl.PROCESSED]
    # one global cap consumed in LANG_ORDER: c first (3 sources), then python (1 of 2)
    assert processed == ["clib/a.c", "clib/b.c", "other/c.c", "tool/app.py"]
    assert ("clib/b.h", "header") in {(t.file, t.status) for t in a.targets}
    assert [t.file for t in a.work_targets("c")] == ["clib/a.c", "clib/b.c", "other/c.c"]
    a = asyncio.run(pl.analyze_all(runners, s, None, pl.Selection(packages=["clib"])))
    assert set(a.parts["c"].pkgs) == {"clib"} and not a.parts["python"].pkgs
    a = asyncio.run(pl.analyze_all(runners, s, None, pl.Selection(files="tool/*")))
    assert {t.file for t in a.targets} == {"tool/app.py", "tool/more.py"}
    with pytest.raises(ValueError, match=r"the selection matched no package \(c, python\)"):
        asyncio.run(pl.analyze_all(runners, s, None, pl.Selection(packages=["nope"])))


def test_unusable_toolchains(tmp_path: Path, caplog):
    repo = _mixed(tmp_path)
    s = _settings(py="/nonexistent/python", cc="/nonexistent/cc")
    runners = pl.make_runners(repo, s, None)
    assert set(runners.unavailable) == {"c", "python"}
    assert "WARNING" in caplog.text and "no-toolchain" in caplog.text
    assert runners.usable() == []
    a = asyncio.run(pl.analyze_all(runners, s, None, pl.Selection()))
    assert {t.status for t in a.targets if t.lang == "python"} == {"no-toolchain"}
    assert a.work_targets("python") == []
    assert "no toolchain" in a.cost_lines()[0]
    s = _settings(py="/nonexistent/python", source_lang="python")
    with pytest.raises(ValueError, match="Python: "):
        pl.make_runners(repo, s, None)
    s = _settings(py="/nonexistent/python", source_lang="all")
    assert "python" in pl.make_runners(repo, s, None).unavailable


def test_missing_optional_dependencies(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pl, "_has_multilang_deps", lambda: False)
    with pytest.raises(ValueError, match="pip install tree-sitter tree-sitter-language-pack"):
        pl.make_runners(_mixed(tmp_path), _settings(source_lang="python"), None)
    runners = pl.make_runners(_mixed(tmp_path), _settings(), None)
    assert runners.missing_deps == {"c", "python"} and runners.usable() == []
    a = asyncio.run(pl.analyze_all(runners, _settings(), None, pl.Selection()))
    assert a.parts == {} and any("tree-sitter" in n for n in a.notes)


def test_sync_tree_skips_build_dirs(tmp_path: Path):
    src = tmp_path / "src"
    for rel in ("a.py", ".venv/lib/x.py", "pkg/__pycache__/m.pyc", "mod/pom.xml",
                "mod/target/classes/A.class", "env/pyvenv.cfg", "env/lib/y.py", "keep/target/t"):
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    dst = tmp_path / "dst"
    (dst / "mod" / "target").mkdir(parents=True)
    (dst / "mod" / "target" / "built.txt").write_text("mvn output")
    (dst / "stale.txt").parent.mkdir(parents=True, exist_ok=True)
    (dst / "stale.txt").write_text("old")
    pl.sync_tree(src, dst, set())
    got = {p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()}
    assert got == {"a.py", "mod/pom.xml", "mod/target/built.txt", "keep/target/t"}


def test_run_context_syncs_once_without_go(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
    out = tmp_path / "out"
    ctx = pl.RunContext(repo, _settings(), out)

    async def go():
        await ctx.ensure_synced()
        await ctx.ensure_synced()

    asyncio.run(go())
    assert ctx.sync_generation == 1
    assert (ctx.sandbox / "src/pymod/calc.py").is_file()
    assert not (ctx.sandbox / ".venv").exists()
    nosb = pl.RunContext(repo, _settings(sandbox=False), out)
    assert nosb.exec_root == repo.resolve() and nosb.sandbox is None
    asyncio.run(nosb.ensure_synced())
    assert nosb.sync_generation == 0


class _Hooks(pl.Hooks):
    def __init__(self):
        self.notes: list[str] = []
        self.stages: list[str] = []

    def note(self, text: str) -> None:
        self.notes.append(text)

    def stage_end(self, name: str, info: dict) -> None:
        self.stages.append(name)


def test_run_multi_python_and_resume(tmp_path: Path, monkeypatch):
    repo = copy_fixture("pymod", tmp_path)
    before = snapshot(repo)
    out = tmp_path / "out"
    s = _settings()
    llms: list[ScriptedLLM] = []
    wiki_calls: list = []

    def factory(settings):
        llms.append(ScriptedLLM(repo, PY_SUITES))
        return llms[-1]

    async def run_wiki(module_root, out_dir, settings, subtree=None, use_tree_docs=True,
                       fmt="both", plan_only=False, on_progress=None, packages=None):
        wiki_calls.append((Path(module_root), subtree, packages))
        return {"packages": 1, "leaf_out": 3, "leaf_tree": 0, "static": 0, "pages": 2,
                "html": None, "tokens": {"input": 0, "output": 0}}

    import codocs.wiki

    monkeypatch.setattr(codocs.wiki, "run_wiki", run_wiki)
    hooks = _Hooks()
    meta = asyncio.run(pl.run(repo, out, s, hooks=hooks, llm_factory=factory))
    assert hooks.stages == ["analyze", "baseline", "testgen", "docgen", "wiki", "report"]
    assert "python: 3 targets (1 skipped: no-decls 1)" in hooks.notes
    assert wiki_calls == [(repo.resolve(), None, None)]
    assert set(meta["stages"]) == {"analyze", "baseline", "testgen", "docgen", "wiki"}
    st = meta["stages"]
    assert st["analyze"]["languages"] == ["python"]
    assert st["baseline"]["by_language"]["python"]["failing"] == 1
    assert st["testgen"]["by_language"]["python"]["files"] == 3
    assert st["docgen"]["by_language"]["python"]["mean_best_phi"] == 1.0
    assert st["testgen"]["tokens"]["input"] > 0
    assert meta["settings"]["source_lang"] == "auto"

    summary = _json(out / "summary.json")
    assert summary["languages"] == ["python"]
    assert summary["docgen"]["by_language"]["python"]["files"] == 3
    assert all(r["lang"] == "python" for r in summary["docgen"]["rows"])
    assert summary["analysis"]["skipped_by_language"] == {"python": {"no-decls": 1}}
    report = (out / "report.md").read_text()
    assert "## Languages" in report and "| python | 1 | 3 | no-decls 1 |" in report
    assert "| lang | file | status | signal |" in report
    for stem in ("calc", "shapes", "stats"):
        assert (out / "tests/src/pymod" / f"test_{stem}_codocs.py").is_file()
        assert (out / "docs/src/pymod/markdown_docs" / f"{stem}.py.md").is_file()
    assert snapshot(repo) == before

    meta = asyncio.run(pl.run(repo, out, s, skip=("wiki",), llm_factory=factory))
    assert llms[-1].calls == 0
    assert set(meta["stages"]) >= {"analyze", "baseline", "testgen", "docgen"}


def test_out_inside_repo_is_ignored(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = repo / "out"
    stray = out / "docgen/src/pymod/calc.py/round_1/code.py"
    stray.parent.mkdir(parents=True)
    stray.write_text("def leaked():\n    return 1\n")
    (out / "tests/_support").mkdir(parents=True)
    (out / "tests/_support/codocs_main.c").write_text("int main(void) { return 0; }\n")
    s = _settings()
    runners = pl.make_runners(repo, s, out)
    assert runners.languages == ["python"]
    a = asyncio.run(pl.analyze_all(runners, s, out, pl.Selection()))
    assert all(not t.file.startswith("out/") for t in a.targets)


def test_report_unchanged_for_go_rows(tmp_path: Path):
    out = tmp_path / "out"
    d = out / "docgen" / "calc" / "calc"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({
        "pkg": "calc", "file": "calc/calc.go", "stem": "calc", "status": "ok",
        "signal": "tests", "rounds": [{"round": 1, "eval": {"phi": 1.0}}], "best_phi": 1.0}))
    summary = build_report(out)
    assert "languages" not in summary and "by_language" not in summary["docgen"]
    assert "lang" not in summary["docgen"]["rows"][0]
    report = (out / "report.md").read_text()
    assert "## Languages" not in report and "| file | status | signal |" in report


# -- Go-only mode --------------------------------------------------------------------------


def _stub_loops(monkeypatch, calls: list):
    async def run_testgen(targets, pkgs, runner, llm, settings, out_dir, module_root,
                          force=False, on_progress=None):
        calls.append(("testgen", [t.file for t in targets]))
        return []

    async def run_docgen(targets, pkgs, runner, llm, settings, out_dir, module_root,
                         force=False, on_progress=None, use_generated_tests=True):
        calls.append(("docgen", [t.file for t in targets]))
        return []

    tes = types.ModuleType("codocs.tesearch.loop")
    tes.run_testgen = run_testgen
    spec = types.ModuleType("codocs.specforge.loop")
    spec.run_docgen = run_docgen
    monkeypatch.setitem(sys.modules, "codocs.tesearch.loop", tes)
    monkeypatch.setitem(sys.modules, "codocs.specforge.loop", spec)


class _NullLLM:
    def __init__(self, settings):
        self.calls = self.cache_hits = 0
        self.tokens = {"input": 0, "output": 0}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@needs_go
def test_legacy_run_with_stub_loops(tmp_path: Path, monkeypatch):
    """Go-only mode never reaches the loop modules through the adapter."""
    monkeypatch.setenv("CODOCS_GO", GO)
    calls: list = []
    _stub_loops(monkeypatch, calls)
    repo = tmp_path / "gomod"
    shutil.copytree(FIXTURES / "gomod", repo)
    (repo / "tools").mkdir()
    (repo / "tools" / "gen.py").write_text("def main():\n    return 0\n")
    out = tmp_path / "out"
    hooks = _Hooks()
    s = _settings(go=GO)
    asyncio.run(pl.run(repo, out, s, skip=("wiki",), hooks=hooks, llm_factory=_NullLLM))
    assert [c[0] for c in calls] == ["testgen", "docgen"]
    assert sorted(calls[0][1]) == ["calc/calc.go", "calc/stats.go", "shapes/shapes.go"]
    assert hooks.notes == []
    d = _json(out / "analysis.json")
    assert "languages" not in d and "lang" not in d["packages"][0]
    assert (out / "baseline" / "calc.json").is_file()


@needs_go
def test_go_part_of_multi_mode_matches_legacy(tmp_path: Path, monkeypatch):
    """with --source-lang all, the Go entries of analysis.json are those of a Go-only run."""
    monkeypatch.setenv("CODOCS_GO", GO)
    repo = tmp_path / "gomod"
    shutil.copytree(FIXTURES / "gomod", repo)
    (repo / "tools").mkdir()
    (repo / "tools" / "gen.py").write_text("def main():\n    return 0\n")
    legacy_out, multi_out = tmp_path / "legacy", tmp_path / "multi"
    s = _settings(go=GO)
    runner = pl.make_runner(repo, s, legacy_out)
    asyncio.run(pl.analyze(runner, s, legacy_out, pl.Selection()))
    legacy = _json(legacy_out / "analysis.json")
    s = _settings(go=GO, source_lang="all")
    runners = pl.make_runners(repo, s, multi_out)
    assert runners.languages == ["go", "python"]
    asyncio.run(pl.analyze_all(runners, s, multi_out, pl.Selection()))
    multi = _json(multi_out / "analysis.json")
    go_pkgs = [{k: v for k, v in p.items() if k != "lang"} for p in multi["packages"]
               if p["lang"] == "go"]
    assert go_pkgs == legacy["packages"]
    go_targets = [{k: v for k, v in t.items() if k != "lang"} for t in multi["targets"]
                  if t["lang"] == "go"]
    assert go_targets == legacy["targets"]
    assert multi["go_version"] == legacy["go_version"] and multi["module"] == legacy["module"]
    assert multi["totals_by_language"]["go"] == legacy["totals"]
    # the shared sandbox: one sync through the Go runner
    ctx = runners.run_ctx
    assert runners["go"].sandbox == ctx.sandbox
    asyncio.run(ctx.ensure_synced())
    assert ctx.sync_generation == 1 and (ctx.sandbox / "tools" / "gen.py").is_file()
    assert os.path.isdir(ctx.sandbox / "calc")
