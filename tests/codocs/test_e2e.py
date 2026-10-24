"""end-to-end: `codocs run` on a copy of the fixture with a scripted fake llm."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from codocs.cli import cli
from codocs.golang.source import top_level_decls
from codocs.llm import LLM
from codocs.specforge.docfmt import expected_sections
from codocs.specforge.prompts import CODEGEN, REFINE, SEED, load_prompt
from codocs.tesearch.prompts import refine_system, seed_system

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
GO = shutil.which("go")
pytestmark = pytest.mark.skipif(GO is None, reason="go toolchain not installed")

SUITES = {
    "calc/calc.go": """package calc

import "testing"

func TestCodocsClamp(t *testing.T) {
	cases := []struct{ v, lo, hi, want int }{
		{5, 0, 10, 5}, {-1, 0, 10, 0}, {11, 0, 10, 10}, {0, 0, 10, 0}, {10, 0, 10, 10},
	}
	for _, c := range cases {
		if got := Clamp(c.v, c.lo, c.hi); got != c.want {
			t.Errorf("Clamp(%d,%d,%d) = %d, want %d", c.v, c.lo, c.hi, got, c.want)
		}
	}
}

func TestCodocsSum(t *testing.T) {
	if got, err := Sum([]int{4, 5, 6}); err != nil || got != 15 {
		t.Fatalf("Sum = %d, %v", got, err)
	}
	if got, err := Sum(nil); err != ErrEmpty || got != 0 {
		t.Fatalf("Sum(nil) = %d, %v", got, err)
	}
}

func TestCodocsNormalize(t *testing.T) {
	if got := Normalize("  Go Is Fun "); got != "go-is-fun" {
		t.Fatalf("Normalize = %q", got)
	}
}
""",
    "calc/stats.go": """package calc

import "testing"

func TestCodocsStats(t *testing.T) {
	var s Stats
	s.Add(2)
	s.Add(9)
	s.Add(4)
	if s.Count != 3 || s.Total != 15 || s.Max != 9 {
		t.Fatalf("Stats = %+v", s)
	}
	if got := s.Mean(); got != 50 {
		t.Fatalf("Mean = %d, want 50", got)
	}
	if got := (Stats{}).Mean(); got != 0 {
		t.Fatalf("empty Mean = %d", got)
	}
	var n Stats
	n.Add(-3)
	if n.Max != -3 {
		t.Fatalf("Max = %d, want -3", n.Max)
	}
}
""",
    "shapes/shapes.go": """package shapes

import "testing"

func TestCodocsRect(t *testing.T) {
	r := Rect{W: 2, H: 3}
	if r.Area() != 6 {
		t.Fatalf("Area = %v", r.Area())
	}
	if (Rect{W: -1, H: 3}).Area() != 0 || (Rect{W: 1, H: -3}).Area() != 0 {
		t.Fatal("negative side must give 0")
	}
	if r.Name() != "rect 2.0x3.0" {
		t.Fatalf("Name = %q", r.Name())
	}
}

func TestCodocsClassify(t *testing.T) {
	for _, c := range []struct {
		r    Rect
		want string
	}{{Rect{0, 5}, "empty"}, {Rect{1, 1}, "small"}, {Rect{2, 5}, "large"}, {Rect{3, 3}, "small"}} {
		if got := Classify(c.r); got != c.want {
			t.Errorf("Classify(%v) = %q, want %q", c.r, got, c.want)
		}
	}
}

func TestCodocsLargest(t *testing.T) {
	if Largest(nil) != nil {
		t.Fatal("Largest(nil) must be nil")
	}
	a, b := Rect{1, 1}, Rect{2, 2}
	if got := Largest([]Shape{a, b, a}); got != b {
		t.Fatalf("Largest = %v", got)
	}
	if got := Largest([]Shape{b, b}); got != b {
		t.Fatalf("Largest = %v", got)
	}
}
""",
}


def _seed_doc(rel: str, src: str) -> str:
    sigs = {d.qualname: d.signature for d in top_level_decls(src)}
    parts = [f"# {rel}", "", "Fixture file.", ""]
    for kind, name in expected_sections(src):
        parts += [f"## {kind} {name}", f"**{name}**: documented.", ""]
        if sigs.get(name):
            parts += [f"**Signature**: `{sigs[name]}`", ""]
    return "<documentation>\n" + "\n".join(parts) + "</documentation>"


class ScriptedLLM:
    """canned replies keyed on the system prompt; the target file comes from the tag."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.calls: list[tuple[str, str]] = []

    def reply(self, system: str, tag: str) -> str:
        rel = tag.split()[1]
        src = (self.repo / rel).read_text(encoding="utf-8")
        if system in (load_prompt(SEED), load_prompt(REFINE)):
            kind = "doc"
            out = _seed_doc(rel, src)
        elif system == load_prompt(CODEGEN):
            kind = "code"
            out = f"```go\n{src}```\n"
        elif system in (seed_system(), refine_system()):
            kind = "suite"
            out = f"```go\n{SUITES[rel]}```\n"
        else:
            raise AssertionError(f"unexpected system prompt for {tag!r}")
        self.calls.append((kind, rel))
        return out


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    goenv = subprocess.run([GO, "env", "GOCACHE", "GOMODCACHE", "GOPATH"], capture_output=True,
                           text=True, check=True).stdout.split("\n")
    for key, val in zip(("GOCACHE", "GOMODCACHE", "GOPATH"), goenv):
        monkeypatch.setenv(key, val.strip())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("CODOCS_GO", GO)
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    fake = ScriptedLLM(repo)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        return fake.reply(system, tag)

    monkeypatch.setattr(LLM, "complete", complete)
    return repo, tmp_path / "out", fake


def _run(repo: Path, out: Path):
    args = ["run", str(repo), "-o", str(out), "--skip", "wiki", "--mutants", "4",
            "--test-rounds", "2", "-j", "3", "--workers", "4"]
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_end_to_end_and_resume(env):
    repo, out, fake = env
    res = _run(repo, out)
    assert res.exit_code == 0, res.output

    analysis = _json(out / "analysis.json")
    assert {t["file"] for t in analysis["targets"]} == {
        "calc/calc.go", "calc/stats.go", "shapes/shapes.go"}

    base = _json(out / "baseline" / "calc.json")
    assert base["build_ok"] is True
    assert "TestKnownBroken" in base["failing"]
    assert set(base["passing"]) >= {"TestClamp", "TestSum", "TestNormalize", "TestStatsAdd"}
    assert (out / "baseline" / "shapes.json").is_file()

    for rel in SUITES:
        pkg, stem = rel.split("/")[0], Path(rel).stem
        suite = out / "tests" / pkg / f"{stem}_codocs_test.go"
        assert suite.is_file(), rel
        tg = _json(out / "testgen" / pkg / stem / "result.json")
        assert tg["status"] == "ok" and tg["best_size"] > 0, tg
        assert tg["best_cov"] == pytest.approx(1.0)

    check = out.parent / "check"
    shutil.copytree(repo, check)
    shutil.copy(out / "tests" / "calc" / "calc_codocs_test.go", check / "calc")
    shutil.copy(out / "tests" / "calc" / "stats_codocs_test.go", check / "calc")
    shutil.copy(out / "tests" / "shapes" / "shapes_codocs_test.go", check / "shapes")
    run = subprocess.run([GO, "test", "-count=1", "-run", "^TestCodocs", "./..."], cwd=check,
                         capture_output=True, text=True, env={**os.environ, "GOTOOLCHAIN": "local"})
    assert run.returncode == 0, run.stdout + run.stderr

    for rel in SUITES:
        pkg, stem = rel.split("/")[0], Path(rel).stem
        doc = out / "docs" / pkg / "markdown_docs" / f"{stem}.md"
        assert doc.is_file(), rel
        dg = _json(out / "docgen" / pkg / stem / "result.json")
        assert dg["status"] == "ok"
        assert dg["best_phi"] == 1.0
        assert dg["early_stop"] is True and len(dg["rounds"]) == 1, dg["phi_trajectory"]
        assert dg["rounds"][0]["missing"] == []
        assert (out / "docgen" / pkg / stem / "round_1" / "code.go").is_file()
    assert _json(out / "docgen/calc/calc/result.json")["signal"] == "tests"
    assert _json(out / "docgen/shapes/shapes/result.json")["signal"] == "generated-tests"
    assert "## FunctionDef Clamp" in (out / "docs/calc/markdown_docs/calc.md").read_text()

    summary = _json(out / "summary.json")
    assert summary["docgen"]["aggregate"]["files"] == 3
    assert summary["docgen"]["aggregate"]["mean_best_phi"] == 1.0
    assert summary["docgen"]["aggregate"]["early_stop_rate"] == 1.0
    assert summary["testgen"]["aggregate"]["delivered_suites"] == 3
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "calc/stats.go" in report and "SpecForge-go" in report
    meta = _json(out / "run_meta.json")
    assert set(meta["stages"]) >= {"analyze", "baseline", "testgen", "docgen"}

    kinds = {k for k, _ in fake.calls}
    assert kinds == {"doc", "code", "suite"}
    assert sum(1 for k, _ in fake.calls if k == "doc") == 3

    first = len(fake.calls)
    res = _run(repo, out)
    assert res.exit_code == 0, res.output
    assert len(fake.calls) == first, fake.calls[first:]
    assert _json(out / "summary.json")["docgen"]["aggregate"]["mean_best_phi"] == 1.0
    assert sorted(p.name for p in repo.rglob("*") if p.is_file()) == sorted(
        p.name for p in FIXTURE.rglob("*") if p.is_file())


def test_analyze_ignores_out_inside_module(env):
    repo, _, _ = env
    out = repo / "out"
    stale = out / "docgen" / "calc" / "calc" / "round_1"
    stale.mkdir(parents=True)
    (stale / "code.go").write_text((repo / "calc" / "calc.go").read_text())
    res = CliRunner().invoke(cli, ["analyze", str(repo), "-o", str(out)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    files = {t["file"] for t in _json(out / "analysis.json")["targets"]}
    assert files == {"calc/calc.go", "calc/stats.go", "shapes/shapes.go"}
