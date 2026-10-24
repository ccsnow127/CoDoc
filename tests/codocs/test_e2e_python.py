"""end-to-end: `codocs run` on a copy of the pymod fixture with a scripted fake llm."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from _e2e_lang import (
    check_doc,
    check_resume,
    check_suite,
    check_summary,
    install,
    invoke,
    load,
    run_cli,
    snapshot,
    wiki_plan,
)

SUITES = {
    "src/pymod/calc.py": '''import pytest

from pymod.calc import SCALE, clamp, normalize, total


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


def test_total():
    assert total([1, 2, 3]) == 6
    with pytest.raises(ValueError, match="calc: empty input"):
        total([])


def test_normalize():
    assert normalize("  Hello World ") == "hello-world"
    assert SCALE == 10


def test_wrong():
    assert clamp(1, 2, 3) == 1
''',
    "src/pymod/shapes.py": '''from pymod.shapes import Rect, classify, largest


def test_rect():
    r = Rect(2, 3)
    assert r.area() == 6
    assert Rect(-1, 2).area() == 0.0
    assert Rect(2, -1).area() == 0.0
    assert r.name() == "rect 2.0x3.0"


def test_classify():
    assert classify(Rect(0, 3)) == "empty"
    assert classify(Rect(2, 2)) == "square"
    assert classify(Rect(2, 3)) == "rect"


def test_largest():
    a, b, c = Rect(1, 1), Rect(2, 2), Rect(2, 2)
    assert largest([a, b, c]) is b
    assert largest([]) is None
''',
    "src/pymod/stats.py": '''from pymod.stats import Stats


def test_stats():
    s = Stats()
    assert s.mean() == 0
    s.add(4)
    s.add(2)
    assert (s.count, s.total, s.max) == (2, 6, 4)
    assert s.mean() == 30
''',
}
TARGETS = sorted(SUITES)


def _suite_name(rel: str) -> str:
    return f"test_{Path(rel).stem}_codocs.py"


def test_python_run_end_to_end_and_resume(tmp_path: Path, monkeypatch):
    repo, out, fake = install(monkeypatch, tmp_path, "pymod", SUITES)
    before = snapshot(repo)
    res = run_cli(repo, out)
    assert res.exit_code == 0, res.output
    assert "python: 3 targets" in res.output

    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["python"]
    assert all(t["lang"] == "python" for t in analysis["targets"])
    assert [t["file"] for t in analysis["targets"] if t["status"] == "ok"] == TARGETS
    assert [t["file"] for t in analysis["skipped"]] == ["src/pymod/__init__.py"]

    base = load(out / "baseline" / "python" / "src__pymod.json")
    assert base["build_ok"] is True and base["lang"] == "python"
    assert base["failing"] == ["tests/test_calc.py::test_known_broken"]
    assert set(base["passing"]) >= {"tests/test_calc.py::test_clamp",
                                    "tests/test_calc.py::test_total",
                                    "tests/test_stats.py::test_mean"}

    for rel in TARGETS:
        check_suite(out, rel, _suite_name(rel))
        assert (out / "testgen" / "src/pymod" / Path(rel).name / "round_1" / "suite.py").is_file()
    calc = load(out / "testgen/src/pymod/calc.py/result.json")
    assert calc["best_size"] == 3 and "test_wrong" not in (
        out / "tests/src/pymod/test_calc_codocs.py").read_text()

    # the delivered suites pass with pytest in a clean copy of the fixture
    check = tmp_path / "check"
    shutil.copytree(repo, check)
    for rel in TARGETS:
        shutil.copy(out / "tests" / "src/pymod" / _suite_name(rel), check / "src/pymod")
    env = {**os.environ, "PYTHONPATH": str(check / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          *[f"src/pymod/{_suite_name(r)}" for r in TARGETS]],
                         cwd=check, capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "7 passed" in run.stdout, run.stdout

    for rel in TARGETS:
        check_doc(out, rel)
    assert load(out / "docgen/src/pymod/calc.py/result.json")["signal"] == "tests"
    assert load(out / "docgen/src/pymod/stats.py/result.json")["signal"] == "tests"
    assert load(out / "docgen/src/pymod/shapes.py/result.json")["signal"] == "generated-tests"
    doc = (out / "docs/src/pymod/markdown_docs/calc.py.md").read_text()
    assert "**Module**: `pymod.calc`" in doc and "## FunctionDef clamp" in doc
    assert "## ClassDef Rect" in (out / "docs/src/pymod/markdown_docs/shapes.py.md").read_text()

    summary = check_summary(out, "python", docs=3, suites=3)
    assert summary["languages"] == ["python"]
    assert {k for k, _ in fake.calls} == {"doc", "code", "suite"}
    assert sum(1 for k, _ in fake.calls if k == "doc") == 3
    calls = len(fake.calls)

    check_resume(repo, out, fake)
    assert snapshot(repo) == before

    # the wiki plan picks up the generated docs of the run (no llm call)
    text, plan = wiki_plan(repo, out)
    assert "generated docs 1" in text and plan["leaf_out"] == 1, text
    assert len(fake.calls) == calls


def test_python_wiki_plan_only(tmp_path: Path, monkeypatch):
    repo, out, fake = install(monkeypatch, tmp_path, "pymod", SUITES)
    text, plan = wiki_plan(repo, out)
    assert "python: 4 source files" in text, text
    assert set(plan["languages"]) == {"python"}
    assert plan["languages"]["python"]["packages"] == 1
    assert plan["languages"]["python"]["files"] == 4
    assert plan["packages"] == 1 and plan["static"] == 1 and plan["leaf_out"] == 0
    assert fake.calls == []


def test_bizlogic_cli_on_a_python_repo(tmp_path: Path, monkeypatch):
    """the bizlogic command detects languages instead of requiring go.mod."""
    repo, out, fake = install(monkeypatch, tmp_path, "pymod", SUITES)
    res = invoke("bizlogic", repo, "-o", out, "--plan-only", "-k", "2", "--min-reach", "1")
    assert res.exit_code == 0, res.output
    stats = load(out / "bizlogic" / "graph.json")["stats"]
    assert set(stats["languages"]) == {"python"} and stats["functions"] + stats["methods"] >= 6, stats
    assert fake.calls == []
    empty = tmp_path / "empty"
    empty.mkdir()
    res = invoke("bizlogic", empty, "-o", out, "--plan-only")
    assert res.exit_code == 1 and "no go.mod" in " ".join(res.output.split()), res.output
