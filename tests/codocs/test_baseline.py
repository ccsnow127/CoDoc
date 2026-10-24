from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codocs.baseline import Baseline, package_baseline
from codocs.golang.gotool import GoRunner, GoTestResult
from codocs.golang.package import PackageInfo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")

CALC = PackageInfo(
    rel="calc", import_path="example.com/fixture/calc", name="calc",
    go_files=["calc/calc.go", "calc/stats.go"],
    test_files=["calc/calc_test.go", "calc/stats_test.go"], xtest_files=[],
)
SHAPES = PackageInfo(
    rel="shapes", import_path="example.com/fixture/shapes", name="shapes",
    go_files=["shapes/shapes.go"], test_files=[], xtest_files=[],
)


async def _forbidden(*args, **kwargs):
    raise AssertionError("go test must not run")


def test_baseline_roundtrip():
    b = Baseline(pkg="a/b", build_ok=False, passing=["TestA"], failing=["TestB"],
                 build_errors=["a/b/x.go:1:1: oops"], seconds=1.5)
    assert Baseline.from_dict(json.loads(json.dumps(b.to_dict()))) == b


@needs_go
async def test_baseline_fixture_calc_cached(tmp_path: Path, monkeypatch):
    mod = tmp_path / "gomod"
    shutil.copytree(FIXTURE, mod)
    runner = GoRunner(mod, work_dir=tmp_path / "work")
    out = tmp_path / "out"
    base = await package_baseline(runner, CALC, out)
    assert base.pkg == "calc"
    assert base.build_ok
    assert base.passing == ["TestClamp", "TestNormalize", "TestStatsAdd", "TestStatsMean",
                            "TestSum"]
    assert base.failing == ["TestKnownBroken"]
    assert base.build_errors == []
    assert base.seconds > 0
    path = out / "baseline" / "calc.json"
    assert json.loads(path.read_text())["passing"] == base.passing

    monkeypatch.setattr(runner, "test", _forbidden)
    assert await package_baseline(runner, CALC, out) == base


@needs_go
async def test_baseline_build_failure(tmp_path: Path):
    mod = tmp_path / "gomod"
    shutil.copytree(FIXTURE, mod)
    (mod / "calc" / "stats.go").write_text("package calc\n\nfunc Broken() int { return x }\n")
    base = await package_baseline(GoRunner(mod, work_dir=tmp_path / "work"), CALC, tmp_path)
    assert not base.build_ok
    assert base.passing == []
    assert any("undefined: x" in e for e in base.build_errors)


async def test_baseline_no_tests_does_not_run_go(tmp_path: Path, monkeypatch):
    runner = GoRunner(FIXTURE)
    monkeypatch.setattr(runner, "test", _forbidden)
    base = await package_baseline(runner, SHAPES, tmp_path)
    assert base == Baseline(pkg="shapes", build_ok=True, passing=[], failing=[],
                            build_errors=[], seconds=0.0)
    assert (tmp_path / "baseline" / "shapes.json").exists()


async def test_baseline_force_and_paths(tmp_path: Path, monkeypatch):
    runner = GoRunner(FIXTURE)
    calls = []

    async def fake_test(pkg_rel, **kwargs):
        calls.append((pkg_rel, kwargs))
        return GoTestResult(build_ok=True, tests={"TestB": "pass", "TestA": "pass",
                                                  "TestC": "fail", "TestD": "skip"})

    monkeypatch.setattr(runner, "test", fake_test)
    nested = PackageInfo(rel="a/b/c", import_path="m/a/b/c", name="c", go_files=["a/b/c/c.go"],
                         test_files=[], xtest_files=["a/b/c/c_x_test.go"])
    base = await package_baseline(runner, nested, tmp_path)
    assert calls == [("a/b/c", {})]
    assert base.passing == ["TestA", "TestB"]
    assert base.failing == ["TestC"]
    assert (tmp_path / "baseline" / "a__b__c.json").exists()

    await package_baseline(runner, nested, tmp_path)
    assert len(calls) == 1
    await package_baseline(runner, nested, tmp_path, force=True)
    assert len(calls) == 2

    root = PackageInfo(rel="", import_path="m", name="m", go_files=["m.go"],
                       test_files=["m_test.go"], xtest_files=[])
    await package_baseline(runner, root, tmp_path)
    assert calls[-1][0] == ""
    assert (tmp_path / "baseline" / "_root.json").exists()


async def test_baseline_corrupt_cache_recomputed(tmp_path: Path, monkeypatch):
    runner = GoRunner(FIXTURE)

    async def fake_test(pkg_rel, **kwargs):
        return GoTestResult(build_ok=True, tests={"TestClamp": "pass"})

    monkeypatch.setattr(runner, "test", fake_test)
    (tmp_path / "baseline").mkdir()
    (tmp_path / "baseline" / "calc.json").write_text("{not json")
    base = await package_baseline(runner, CALC, tmp_path)
    assert base.passing == ["TestClamp"]


CRASHY_TESTS = """package calc

import (
	"log"
	"testing"
)

func TestAdd(t *testing.T) {
	if Clamp(5, 0, 10) != 5 {
		t.Fatal("clamp")
	}
}

func TestPanic(t *testing.T) {
	var m map[string]int
	m["x"] = 1
}

func TestDiv(t *testing.T) {
	if v, _ := Sum([]int{4, 2}); v != 6 {
		t.Fatal("sum")
	}
}

func TestFatal(t *testing.T) { log.Fatal("no registry reachable") }

func TestLast(t *testing.T) {
	if Normalize(" A ") != "a" {
		t.Fatal("norm")
	}
}
"""


@needs_go
async def test_baseline_reruns_tests_lost_to_crashes(tmp_path: Path):
    mod = tmp_path / "gomod"
    shutil.copytree(FIXTURE, mod)
    (mod / "calc" / "calc_test.go").write_text(CRASHY_TESTS)
    calc = PackageInfo(**{**CALC.to_dict(), "test_files": ["calc/calc_test.go"]})
    base = await package_baseline(GoRunner(mod, work_dir=tmp_path / "work"), calc,
                                  tmp_path / "out")
    assert base.build_ok and not base.timed_out and not base.incomplete
    assert base.passing == ["TestAdd", "TestDiv", "TestLast"]
    assert base.failing == ["TestFatal", "TestPanic"]
    assert base.runs == 3


async def test_timed_out_baseline_is_not_reused(tmp_path: Path, monkeypatch):
    runner = GoRunner(FIXTURE)
    results = [
        GoTestResult(build_ok=False, build_errors=["go test timed out before any test started"],
                     timed_out=True, returncode=-1),
        GoTestResult(build_ok=True, tests={"TestClamp": "pass", "TestSum": "pass"}),
    ]
    calls = []

    async def fake_test(pkg_rel, **kwargs):
        calls.append(kwargs)
        return results[len(calls) - 1]

    monkeypatch.setattr(runner, "test", fake_test)
    first = await package_baseline(runner, CALC, tmp_path)
    assert first.timed_out and not first.build_ok
    assert json.loads((tmp_path / "baseline" / "calc.json").read_text())["timed_out"] is True
    second = await package_baseline(runner, CALC, tmp_path)
    assert len(calls) == 2
    assert second.build_ok and not second.timed_out
    assert second.passing == ["TestClamp", "TestSum"]
    assert await package_baseline(runner, CALC, tmp_path) == second
    assert len(calls) == 2


def test_baseline_reads_old_cache_files():
    old = {"pkg": "a", "build_ok": True, "passing": ["TestA"], "failing": [],
           "build_errors": [], "seconds": 1.0}
    b = Baseline.from_dict(old)
    assert not b.timed_out and not b.incomplete and b.runs == 0


def test_summarize_baselines_counts_timeouts():
    from codocs.pipeline import summarize_baselines

    bases = {"a": Baseline(pkg="a", build_ok=True, timed_out=True),
             "b": Baseline(pkg="b", build_ok=True, incomplete=True, passing=["T"]),
             "c": Baseline(pkg="c", build_ok=False)}
    assert summarize_baselines(bases) == {"packages": 3, "build_failed": 1, "timed_out": 1,
                                          "incomplete": 1, "passing": 1, "failing": 0}
