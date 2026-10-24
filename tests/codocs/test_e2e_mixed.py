"""end-to-end on the mixed (Go + cgo C + Python) fixture: `codocs run --source-lang all`.

also checks that the Go artifacts of a multi-mode run are those of a Go-only run,
`codocs bizlogic --plan-only` (one graph with cgo edges) and `codocs wiki --plan-only`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _e2e_lang import (
    build_and_run,
    check_doc,
    check_resume,
    check_suite,
    install,
    invoke,
    load,
    run_cli,
    snapshot,
    wiki_plan,
)

GO = shutil.which("go")
CC = shutil.which("cc")
GCOV = shutil.which("gcov")
pytestmark = pytest.mark.skipif(not (GO and CC and GCOV), reason="go/cc/gcov not installed")

SUITES = {
    "ring/ring.go": """package ring

import "testing"

func TestCodocsRing(t *testing.T) {
	r := New(2)
	r.Push(1)
	r.Push(2)
	r.Push(3)
	if got := r.Len(); got != 2 {
		t.Fatalf("Len = %d, want 2", got)
	}
	r.Close()
	r.Close()
	if got := Fill(New(0), []int{4, 5}); got != 1 {
		t.Fatalf("Fill = %d, want 1", got)
	}
	if got := Collect([]int{1, 2, 3}); got != 3 {
		t.Fatalf("Collect = %d, want 3", got)
	}
}
""",
    "ring/export.go": """package ring

import "testing"

func TestCodocsFullEvents(t *testing.T) {
	before := FullEvents()
	r := New(1)
	defer r.Close()
	r.Push(1)
	r.Push(2)
	if got := FullEvents(); got != before+1 {
		t.Fatalf("FullEvents = %d, want %d", got, before+1)
	}
}
""",
    "clib/src/cring.c": """#include "codocs_test.h"
#include CODOCS_TARGET

static int full_calls;

/* stands in for the Go callback (ring/export.go) */
void ring_on_full(int n) {
    full_calls += n;
}

CODOCS_TEST(push_wraps) {
    struct cring *r = cring_new(2);
    REQUIRE(r != NULL);
    cring_push(r, 1);
    cring_push(r, 2);
    CHECK_EQ_INT(cring_len(r), 2);
    cring_push(r, 3);
    CHECK_EQ_INT(cring_len(r), 2);
    CHECK_EQ_INT(full_calls, 1);
    CHECK_EQ_INT(r->buf[r->head], 2);
    cring_free(r);
    cring_free(NULL);
}

CODOCS_TEST(min_capacity) {
    struct cring *r = cring_new(0);
    REQUIRE(r != NULL);
    CHECK_EQ_INT(r->cap, 1);
    CHECK_EQ_INT(wrap(r, 5), 0);
    cring_free(r);
}
""",
    "tools/report.py": '''from report import main, parse, render, summarize


def test_parse():
    assert parse("a, 3") == ("a", 3)


def test_summarize_and_render():
    totals = summarize(["a,1", "", "b,2", "a,4"])
    assert totals == {"a": 5, "b": 2}
    assert render(totals) == "a: 5\\nb: 2"


def test_main(tmp_path, capsys):
    p = tmp_path / "s.csv"
    p.write_text("a,1\\nb,2\\n\\na,3\\n", encoding="utf-8")
    assert main(["report", str(p)]) == 0
    assert capsys.readouterr().out == "a: 4\\nb: 2\\n"
''',
}
GO_TARGETS = ["ring/export.go", "ring/ring.go"]
GO_SUITES = {"ring/ring.go": "ring_codocs_test.go", "ring/export.go": "export_codocs_test.go"}
DROP = {"seconds", "finished", "created", "updated"}


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in DROP}
    if isinstance(o, list):
        return [_strip(x) for x in o]
    return o


def _go_artifacts(out: Path) -> dict[str, object]:
    """every Go artifact of a run, timings removed."""
    got: dict[str, object] = {}
    for top in ("docgen/ring", "testgen/ring", "tests/ring", "docs/ring"):
        for p in sorted((out / top).rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(out).as_posix()
            if p.suffix == ".json":
                got[rel] = _strip(json.loads(p.read_text(encoding="utf-8")))
            else:
                got[rel] = p.read_text(encoding="utf-8").replace(str(out), "<OUT>")
    got["baseline/ring.json"] = _strip(load(out / "baseline" / "ring.json"))
    return got


def test_mixed_run_end_to_end(tmp_path: Path, monkeypatch):
    repo, out, fake = install(monkeypatch, tmp_path, "mixedmod", SUITES)
    before = snapshot(repo)
    res = run_cli(repo, out, "--source-lang", "all")
    assert res.exit_code == 0, res.output

    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["go", "c", "python"]
    ok = {(t["lang"], t["file"]) for t in analysis["targets"] if t["status"] == "ok"}
    assert ok == {("go", "ring/export.go"), ("go", "ring/ring.go"), ("c", "clib/src/cring.c"),
                  ("python", "tools/report.py")}
    status = {t["file"]: t["status"] for t in analysis["targets"]}
    assert {status[f"gen/asn/src/{n}.c"] for n in ("msg1", "msg2", "msg3", "skeleton")} == {
        "generated"}
    assert status["ring/cring_build.c"] == "no-decls"

    go_base = load(out / "baseline" / "ring.json")             # Go baselines stay at the top level
    assert go_base["build_ok"] and set(go_base["passing"]) == {"TestPushLen", "TestFill"}
    assert "lang" not in go_base
    c_base = load(out / "baseline" / "c" / "clib__src.json")
    assert c_base["passing"] == ["clib/src/test_cring.c"] and c_base["failing"] == []
    py_base = load(out / "baseline" / "python" / "tools.json")
    # no config marker: the project root is tools/, nodeids are relative to it
    assert set(py_base["passing"]) == {"test_report.py::test_parse",
                                       "test_report.py::test_summarize_and_render"}

    for rel, name in GO_SUITES.items():
        tg = load(out / "testgen" / "ring" / Path(rel).stem / "result.json")
        assert tg["status"] == "ok" and tg["best_size"] == 1 and "lang" not in tg, tg
        assert tg["best_cov"] == pytest.approx(1.0), (rel, tg["best_cov"])
        assert (out / "tests" / "ring" / name).is_file()
    check_suite(out, "clib/src/cring.c", "cring_codocs_test.c", min_cov=0.8)
    check_suite(out, "tools/report.py", "test_report_codocs.py", min_cov=0.8)

    # delivered suites: go test with the Go suites, the C suite on its own, pytest for tools/
    check = tmp_path / "check"
    shutil.copytree(repo, check)
    for name in GO_SUITES.values():
        shutil.copy(out / "tests" / "ring" / name, check / "ring")
    run = subprocess.run([GO, "test", "-count=1", "-run", "^TestCodocs", "./ring/"], cwd=check,
                         capture_output=True, text=True, env={**os.environ, "GOTOOLCHAIN": "local"})
    assert run.returncode == 0, run.stdout + run.stderr
    work = tmp_path / "cwork"
    work.mkdir()
    build_and_run(CC, out, check, "clib/src/cring.c", [], work, ("clib/include",))
    shutil.copy(out / "tests" / "tools" / "test_report_codocs.py", check / "tools")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "test_report_codocs.py"], cwd=check / "tools", capture_output=True,
                         text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert run.returncode == 0 and "3 passed" in run.stdout, run.stdout + run.stderr

    for rel in GO_TARGETS:
        stem = Path(rel).stem
        dg = load(out / "docgen" / "ring" / stem / "result.json")
        assert dg["status"] == "ok" and dg["best_phi"] == 1.0 and dg["early_stop"], dg
        assert dg["signal"] == "tests" and "lang" not in dg
        assert (out / "docs" / "ring" / "markdown_docs" / f"{stem}.md").is_file()
    check_doc(out, "clib/src/cring.c", signal="tests")
    check_doc(out, "tools/report.py", signal="tests")

    summary = load(out / "summary.json")
    assert summary["languages"] == ["go", "c", "python"]
    assert {k: v["files"] for k, v in summary["docgen"]["by_language"].items()} == {
        "go": 2, "c": 1, "python": 1}
    assert summary["docgen"]["aggregate"]["mean_best_phi"] == 1.0
    assert summary["testgen"]["aggregate"]["delivered_suites"] == 4
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Languages" in report and "| go |" in report and "| python |" in report

    check_resume(repo, out, fake, "--source-lang", "all")
    assert snapshot(repo) == before

    # the Go artifacts equal those of a Go-only run
    legacy = tmp_path / "legacy"
    res = run_cli(repo, legacy, "--source-lang", "go")
    assert res.exit_code == 0, res.output
    assert "languages" not in load(legacy / "analysis.json")
    assert _go_artifacts(out) == _go_artifacts(legacy)
    calls = len(fake.calls)

    # bizlogic: one graph across languages with the cgo edges (no llm with --plan-only)
    res = invoke("bizlogic", repo, "-o", out, "--plan-only", "-k", "2", "--source-lang", "all",
                 "--utility-fanin", "40", "--min-reach", "1")
    assert res.exit_code == 0, res.output
    graph = load(out / "bizlogic" / "graph.json")
    stats = graph["stats"]
    assert set(stats["languages"]) == {"go", "c", "python"}
    assert stats["cross_edges"]["cgo_calls"] == 5 and stats["cross_edges"]["cgo_exports"] == 1
    assert stats["documented"] >= 10, stats       # the docs of this run's OUT/docs are read
    kdir = out / "bizlogic" / "k2"
    nodes = load(kdir / "nodes.json")
    assert nodes["clib/src::cring_push"][0] == "clib/src/cring.c"
    assert nodes["ring::ring_on_full"][0] == "ring/export.go"
    assert "tools::report.main" in nodes and not any(v[0].startswith("gen/")
                                                     for v in nodes.values())
    # the Go entry root reaches into C through cgo: one cluster spans both languages
    reach = {r["id"]: r["reach"] for r in graph["roots"]}
    assert reach["ring::Collect"] > 8, reach
    clusters = load(kdir / "clusters.json")["clusters"]
    assert any({p for p, _ in c["packages"]} >= {"ring", "clib/src"} for c in clusters)
    # the default (auto) bizlogic on this Go module stays Go-only
    res = invoke("bizlogic", repo, "-o", legacy, "--plan-only", "-k", "2", "--min-reach", "1")
    assert res.exit_code == 0, res.output
    assert "languages" not in load(legacy / "bizlogic" / "graph.json")["stats"]

    # wiki: packages from every language, the generated docs of all three
    text, plan = wiki_plan(repo, out, source_lang="all")
    assert set(plan["languages"]) == {"go", "c", "python"}, plan["languages"]
    assert plan["leaf_out"] >= 3, plan
    for lang in ("go", "c", "python"):
        assert f"{lang}: " in text, text
    assert len(fake.calls) == calls
