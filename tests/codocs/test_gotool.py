from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from codocs.golang import gotool
from codocs.golang.gotool import (
    CoverBlock,
    GoRunner,
    GoTestResult,
    file_coverage,
    parse_cover_profile,
    parse_test_json,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")

PKG = "example.com/m/calc"


def _ev(action: str, test: str | None = None, output: str | None = None, **extra) -> str:
    d = {"Time": "2026-10-14T10:00:00Z", "Action": action, "Package": PKG}
    if test:
        d["Test"] = test
    if output is not None:
        d["Output"] = output
    d.update(extra)
    return json.dumps(d)


def _build_ev(action: str, output: str | None = None) -> str:
    d = {"ImportPath": f"{PKG} [{PKG}.test]", "Action": action}
    if output is not None:
        d["Output"] = output
    return json.dumps(d)


def _lines(*items: str) -> str:
    return "\n".join(items) + "\n"


# -- parse_test_json: go 1.23 style -------------------------------------------

GO123_BUILD_STDOUT = _lines(
    _ev("start"),
    _ev("output", output=f"FAIL\t{PKG} [build failed]\n"),
    _ev("fail", Elapsed=0),
)
GO123_BUILD_STDERR = (
    f"# {PKG} [{PKG}.test]\n"
    "../../tmp/codocs/t1x2y3/ovAbC123/calc.go:3:8: \"fmt\" imported and not used\n"
    "/tmp/codocs/t1x2y3/ovAbC123/calc.go:9:2: undefined: foo\n"
    "calc/stats.go:24:19: undefined: Scale\n"
)


def test_go123_build_failure_maps_overlay_paths():
    res = parse_test_json(GO123_BUILD_STDOUT, GO123_BUILD_STDERR,
                          {"/tmp/codocs/t1x2y3/ovAbC123": "calc"})
    assert res.build_ok is False
    assert res.build_errors == [
        'calc/calc.go:3:8: "fmt" imported and not used',
        "calc/calc.go:9:2: undefined: foo",
        "calc/stats.go:24:19: undefined: Scale",
    ]
    assert res.tests == {}
    assert "build failed" in res.tail


def test_go123_setup_failed():
    stdout = _lines(
        _ev("start"),
        _ev("output", output=f"FAIL\t{PKG} [setup failed]\n"),
        _ev("fail", Elapsed=0),
    )
    stderr = (f"# {PKG}\ncalc/extra_test.go:3:8: no required module provides package "
              "example.com/nope/zzz; to add it:\n\tgo get example.com/nope/zzz\n")
    res = parse_test_json(stdout, stderr, {})
    assert not res.build_ok
    assert res.build_errors[0].startswith("calc/extra_test.go:3:8: no required module")


def test_stderr_only_is_build_failure():
    res = parse_test_json("", "go: cannot find main module\n", {})
    assert not res.build_ok
    assert res.build_errors == ["go: cannot find main module"]


# -- parse_test_json: go >= 1.24 style ------------------------------------------

GO127_BUILD_STDOUT = _lines(
    _build_ev("build-output", f"# {PKG} [{PKG}.test]\n"),
    _build_ev("build-output", "calc/stats.go:24:19: undefined: Scale\n"),
    _build_ev("build-output", '../ov7Qz/calc.go:3:8: "fmt" imported and not used\n'),
    _build_ev("build-fail"),
    _ev("start"),
    _ev("output", output=f"FAIL\t{PKG} [build failed]\n", OutputType="frame"),
    _ev("fail", Elapsed=0, FailedBuild=f"{PKG} [{PKG}.test]"),
)


def test_go127_build_failure():
    res = parse_test_json(GO127_BUILD_STDOUT, "", {"/private/var/x/ov7Qz": "calc"})
    assert not res.build_ok
    assert res.build_errors == [
        "calc/stats.go:24:19: undefined: Scale",
        'calc/calc.go:3:8: "fmt" imported and not used',
    ]


def test_go127_failed_build_field_alone():
    stdout = _lines(_ev("start"), _ev("fail", Elapsed=0, FailedBuild="example.com/nope/zzz"))
    assert not parse_test_json(stdout, "", {}).build_ok


def test_build_errors_capped():
    stdout = _lines(
        *[_build_ev("build-output", f"calc/a.go:{i}:1: undefined: x{i}\n") for i in range(80)],
        _build_ev("build-fail"),
    )
    res = parse_test_json(stdout, "", {})
    assert len(res.build_errors) == gotool.MAX_BUILD_ERRORS


# -- verdicts -----------------------------------------------------------------

RUN_STDOUT = _lines(
    _ev("start"),
    _ev("run", "TestA"),
    _ev("output", "TestA", "=== RUN   TestA\n"),
    _ev("run", "TestA/sub_one"),
    _ev("output", "TestA/sub_one", "    x_test.go:9: hi\n"),
    _ev("pass", "TestA/sub_one"),
    _ev("run", "TestA/bad"),
    _ev("fail", "TestA/bad"),
    _ev("output", "TestA", "--- FAIL: TestA (0.00s)\n"),
    _ev("fail", "TestA"),
    _ev("run", "TestB"),
    _ev("output", "TestB", "--- PASS: TestB (0.00s)\n"),
    _ev("pass", "TestB"),
    _ev("run", "TestC"),
    _ev("run", "TestC/x"),
    _ev("pass", "TestC/x"),
    _ev("pass", "TestC"),
    _ev("run", "TestD"),
    _ev("output", "TestD", "    d_test.go:3: nah\n"),
    _ev("skip", "TestD"),
    _ev("output", output="FAIL\n"),
    _ev("fail", Elapsed=0.2),
)


def test_verdicts_and_subtests_folded():
    res = parse_test_json(RUN_STDOUT, "", {})
    assert res.build_ok
    assert res.tests == {"TestA": "fail", "TestB": "pass", "TestC": "pass", "TestD": "skip"}
    assert res.passed == {"TestB", "TestC"}
    assert res.failed == {"TestA"}
    assert "x_test.go:9: hi" in res.output["TestA"]
    assert "--- FAIL: TestA" in res.output["TestA"]
    assert "nah" in res.output["TestD"]


def test_go123_timeout_panic_package_level_output():
    stdout = _lines(
        _ev("start"),
        _ev("run", "TestQuick"),
        _ev("pass", "TestQuick"),
        _ev("run", "TestSleep"),
        _ev("output", "TestSleep", "=== RUN   TestSleep\n"),
        _ev("output", output="panic: test timed out after 1s\n"),
        _ev("output", output="\trunning tests:\n\t\tTestSleep (1s)\n"),
        _ev("output", output=f"FAIL\t{PKG}\t1.2s\n"),
        _ev("fail", Elapsed=1.2),
    )
    res = parse_test_json(stdout, "", {})
    assert res.build_ok
    assert res.tests == {"TestQuick": "pass", "TestSleep": "fail"}
    assert "panic: test timed out" in res.output["TestSleep"]


def test_go127_panic_in_subtest():
    stdout = _lines(
        _ev("start"),
        _ev("run", "TestP"),
        _ev("run", "TestP/s"),
        _ev("output", "TestP/s", "--- FAIL: TestP/s (0.00s)\n"),
        _ev("fail", "TestP/s"),
        _ev("output", "TestP", "panic: assignment to entry in nil map [recovered]\n"),
        _ev("fail", "TestP"),
        _ev("fail", Elapsed=0.26),
    )
    res = parse_test_json(stdout, "", {})
    assert res.tests == {"TestP": "fail"}
    assert "nil map" in res.output["TestP"]


def test_started_but_unreported_is_fail():
    stdout = _lines(_ev("run", "TestX"), _ev("output", "TestX", "=== RUN   TestX\n"))
    res = parse_test_json(stdout, "", {})
    assert res.build_ok and res.tests == {"TestX": "fail"}


def test_no_test_files_package():
    stdout = _lines(
        _ev("start"),
        _ev("output", output=f"?   \t{PKG}\t[no test files]\n"),
        _ev("skip", Elapsed=0),
    )
    res = parse_test_json(stdout, "", {})
    assert res.build_ok and res.tests == {}


def test_output_capped_keeps_tail():
    big = "x" * 9000 + "END\n"
    stdout = _lines(_ev("run", "TestBig"), _ev("output", "TestBig", big), _ev("fail", "TestBig"))
    res = parse_test_json(stdout, "", {})
    assert len(res.output["TestBig"]) == gotool.OUTPUT_CAP
    assert res.output["TestBig"].endswith("END\n")


def test_result_roundtrip():
    res = GoTestResult(build_ok=True, tests={"TestA": "pass"}, output={"TestA": ""},
                       coverage=[CoverBlock("calc/calc.go", 1, 2, 3, 4, 1, 0)],
                       timed_out=True, returncode=1, tail="t")
    d = json.loads(json.dumps(res.to_dict()))
    assert GoTestResult.from_dict(d) == res
    assert GoTestResult.from_dict(GoTestResult(build_ok=False).to_dict()).coverage is None


# -- coverage -----------------------------------------------------------------

PROFILE = """mode: set
example.com/m/calc/calc.go:16.2,16.12 1 1
example.com/m/calc/calc.go:17.3,18.1 1 1
example.com/m/calc/calc.go:20.3,21.1 1 0
example.com/m/calc/calc.go:30.2,31.27 2 0
example.com/m/calc/calc.go:20.3,21.1 1 1
example.com/m/calc/calc.go:34.2,34.19 1 0
example.com/m/calc/calc.go:34.2,34.19 1 0
example.com/m/calc/calc.go:31.27,32.3 1 1
example.com/m/calc/stats.go:12.2,12.31 1 0
example.com/m/main.go:3.13,3.14 0 0
"""


def test_parse_cover_profile():
    blocks = parse_cover_profile(PROFILE, "example.com/m")
    assert len(blocks) == 10
    assert blocks[0] == CoverBlock("calc/calc.go", 16, 2, 16, 12, 1, 1)
    assert blocks[-1].file == "main.go"
    assert {b.file for b in blocks} == {"calc/calc.go", "calc/stats.go", "main.go"}


def test_file_coverage_dedupes_blocks():
    blocks = parse_cover_profile(PROFILE, "example.com/m")
    cov, uncovered = file_coverage(blocks, "calc/calc.go")
    # unique blocks: 16(1,hit) 17-18(1,hit) 20-21(1,hit via max) 30-31(2,miss) 34(1,miss) 31-32(1,hit)
    assert cov == pytest.approx(4 / 7)
    assert uncovered == [30, 34]
    assert file_coverage(blocks, "calc/stats.go") == (0.0, [12])
    assert file_coverage(blocks, "main.go") == (None, [])
    assert file_coverage(blocks, "nope.go") == (None, [])


# -- list_packages parsing (no go needed) ---------------------------------------


async def test_list_packages_stream(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    root = tmp_path.resolve()
    objs = [
        {"Dir": str(root), "ImportPath": "example.com/m", "Name": "m", "GoFiles": ["main.go"]},
        {"Dir": str(root / "calc"), "ImportPath": "example.com/m/calc", "Name": "calc",
         "GoFiles": ["calc.go"], "CgoFiles": ["c.go"], "TestGoFiles": ["calc_test.go"],
         "XTestGoFiles": ["ext_test.go"]},
        {"Dir": "/elsewhere/dep", "ImportPath": "example.org/dep", "Name": "dep"},
        {"Dir": str(root / "bad"), "ImportPath": "example.com/m/bad", "Name": "bad",
         "GoFiles": ["b.go"], "Error": {"Pos": "", "Err": "bad import"}},
    ]
    stdout = "".join(json.dumps(o, indent="\t") + "\n" for o in objs)
    runner = GoRunner(tmp_path)

    async def fake_exec(args, timeout, env=None):
        assert args[1:3] == ["list", "-e"]
        return gotool._Proc(0, stdout, "", False)

    monkeypatch.setattr(runner, "_exec", fake_exec)
    pkgs = await runner.list_packages()
    assert set(pkgs) == {"", "calc", "bad"}
    assert pkgs[""].go_files == ["main.go"]
    calc = pkgs["calc"]
    assert calc.go_files == ["calc/calc.go", "calc/c.go"]
    assert calc.test_files == ["calc/calc_test.go"]
    assert calc.xtest_files == ["calc/ext_test.go"]
    assert pkgs["bad"].error == "bad import"


def test_import_path(tmp_path):
    (tmp_path / "go.mod").write_text('module "example.com/q"\n')
    r = GoRunner(tmp_path)
    assert r.module == "example.com/q"
    assert r.import_path("") == "example.com/q"
    assert r.import_path("a/b") == "example.com/q/a/b"


# -- real go --------------------------------------------------------------------


@pytest.fixture
def mod(tmp_path) -> Path:
    dst = tmp_path / "gomod"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def runner(mod, tmp_path) -> GoRunner:
    return GoRunner(mod, workers=4, timeout=120, work_dir=tmp_path / "work")


def _work_empty(runner: GoRunner) -> bool:
    return not runner.work_dir.exists() or not any(runner.work_dir.iterdir())


@needs_go
def test_real_version(runner):
    assert runner.available()
    assert runner.version().startswith("go version go")


@needs_go
async def test_real_list_packages(runner):
    pkgs = await runner.list_packages()
    assert set(pkgs) == {"calc", "shapes"}
    calc = pkgs["calc"]
    assert calc.import_path == "example.com/fixture/calc"
    assert calc.name == "calc"
    assert calc.go_files == ["calc/calc.go", "calc/stats.go"]
    assert calc.test_files == ["calc/calc_test.go", "calc/stats_test.go"]
    assert pkgs["shapes"].test_files == []


@needs_go
async def test_real_baseline(runner, mod):
    before = {p: p.read_bytes() for p in mod.rglob("*") if p.is_file()}
    res = await runner.test("calc")
    assert res.build_ok and not res.timed_out
    assert res.tests == {"TestClamp": "pass", "TestSum": "pass", "TestNormalize": "pass",
                         "TestKnownBroken": "fail", "TestStatsAdd": "pass",
                         "TestStatsMean": "pass"}
    assert "known broken" in res.output["TestKnownBroken"]
    assert res.returncode != 0
    assert {p: p.read_bytes() for p in mod.rglob("*") if p.is_file()} == before
    assert _work_empty(runner)


@needs_go
async def test_real_broken_overlay(runner):
    bad = 'package calc\n\nimport "fmt"\n\nfunc Clamp(v, lo, hi int) int { return v }\n'
    res = await runner.test("calc", overlay={"calc/calc.go": bad})
    assert not res.build_ok
    assert res.tests == {}
    assert any(e.startswith('calc/calc.go:3:8: "fmt" imported and not used')
               for e in res.build_errors), res.build_errors
    assert not any(e.startswith("#") for e in res.build_errors)
    assert all("/ov" not in e for e in res.build_errors)
    assert _work_empty(runner)


@needs_go
async def test_real_setup_failure(runner):
    src = 'package calc\n\nimport "example.com/nope/zzz"\n\nvar _ = zzz.X\n'
    res = await runner.test("calc", overlay={"calc/extra_test.go": src})
    assert not res.build_ok
    assert res.build_errors


NEW_TEST = """package calc

import "testing"

func TestOnlyClamp(t *testing.T) {
	if Clamp(5, 0, 10) != 5 || Clamp(-1, 0, 10) != 0 {
		t.Fatal("bad")
	}
}
"""


@needs_go
async def test_real_hide_add_with_coverage(runner):
    overlay = {"calc/calc_test.go": None, "calc/stats_test.go": None,
               "calc/calc_codocs_test.go": NEW_TEST}
    res = await runner.test("calc", overlay=overlay, cover=True)
    assert res.build_ok
    assert res.tests == {"TestOnlyClamp": "pass"}
    assert res.coverage is not None
    cov, uncovered = file_coverage(res.coverage, "calc/calc.go",
                                   (FIXTURE / "calc" / "calc.go").read_text())
    assert 0 < cov < 1
    assert cov == pytest.approx(4 / 13)
    assert uncovered == [20, 21, 27, 28, 29, 30, 31, 32, 33, 34, 39, 40, 41]
    assert file_coverage(res.coverage, "calc/stats.go")[0] == 0.0


@needs_go
async def test_real_run_filter_and_failfast(runner):
    res = await runner.test("calc", run=["TestSum", "TestStatsMean"])
    assert res.tests == {"TestSum": "pass", "TestStatsMean": "pass"}
    res = await runner.test("calc", run=["TestKnownBroken", "TestClamp"], failfast=True)
    assert res.tests["TestKnownBroken"] == "fail"


@needs_go
async def test_real_compile_only(runner):
    res = await runner.test("calc", compile_only=True)
    assert res.build_ok and res.tests == {}
    res = await runner.test("shapes", compile_only=True)
    assert res.build_ok and res.tests == {}


SLEEP_TEST = """package calc

import (
	"testing"
	"time"
)

func TestSleepy(t *testing.T) { time.Sleep(60 * time.Second) }
"""


@needs_go
async def test_real_timeout_kills_process_group(runner, monkeypatch):
    overlay = {"calc/sleepy_test.go": SLEEP_TEST}
    warm = await runner.test("calc", overlay=overlay, compile_only=True)
    assert warm.build_ok
    monkeypatch.setattr(gotool, "TIMEOUT_SLACK", -1)
    t0 = time.monotonic()
    res = await runner.test("calc", overlay=overlay, run=["TestSleepy"], timeout=3)
    assert time.monotonic() - t0 < 15
    assert res.timed_out and res.build_ok
    assert res.tests == {"TestSleepy": "fail"}
    assert _work_empty(runner)


# -- crashes, timeouts, output limits, sandbox ------------------------------------


def test_parallel_tests_cut_short_by_a_panic_are_interrupted():
    stdout = _lines(
        _ev("start"),
        _ev("run", "TestA"), _ev("pause", "TestA"),
        _ev("run", "TestBoom"), _ev("pause", "TestBoom"),
        _ev("run", "TestB"), _ev("pause", "TestB"),
        _ev("run", "TestC"), _ev("pause", "TestC"),
        _ev("cont", "TestA"), _ev("cont", "TestB"), _ev("cont", "TestBoom"),
        _ev("output", "TestBoom", "panic: assignment to entry in nil map\n"),
        _ev("fail", "TestBoom"),
        _ev("output", output=f"FAIL\t{PKG}\t0.2s\n"),
        _ev("fail", Elapsed=0.2),
    )
    res = parse_test_json(stdout, "", {})
    assert res.tests == {"TestA": "interrupted", "TestBoom": "fail", "TestB": "interrupted",
                         "TestC": "interrupted"}
    assert res.failed == {"TestBoom"}
    assert res.interrupted == {"TestA", "TestB", "TestC"}
    assert "FAIL" not in res.output["TestA"]


def test_log_fatal_blames_the_running_test_only():
    stdout = _lines(
        _ev("start"),
        _ev("run", "TestA"), _ev("pass", "TestA"),
        _ev("run", "TestFatal"), _ev("output", "TestFatal", "2026/10/14 no db\n"),
        _ev("output", output=f"FAIL\t{PKG}\t0.1s\n"),
        _ev("fail", Elapsed=0.1),
    )
    res = parse_test_json(stdout, "", {})
    assert res.tests == {"TestA": "pass", "TestFatal": "fail"}
    assert "no db" in res.output["TestFatal"]


def _sleeper(tmp_path: Path) -> str:
    script = tmp_path / "slowgo"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    return str(script)


async def test_timeout_before_the_build_finishes_is_not_a_build(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path / "m")
    runner = GoRunner(tmp_path / "m", go=_sleeper(tmp_path), work_dir=tmp_path / "work")
    monkeypatch.setattr(gotool, "TIMEOUT_SLACK", 1)
    t0 = time.monotonic()
    res = await runner.test("calc", overlay={"calc/calc.go": "package calc\n\nvar x = c\n"},
                            timeout=0)
    assert time.monotonic() - t0 < 15
    assert res.timed_out and not res.build_ok
    assert res.build_errors == [gotool.NOT_STARTED]
    assert res.tests == {}


def test_timeout_keeps_a_parsed_build_failure():
    res, started = gotool._parse_test_json("", "calc/calc.go:3:9: undefined: c\n", {})
    assert not res.build_ok and not started


async def test_list_packages_failure_raises(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.99\n")
    runner = GoRunner(tmp_path, work_dir=tmp_path / "work")

    async def failing(args, timeout, env=None):
        return gotool._Proc(1, "", "go: go.mod requires go >= 1.99 (running go 1.23.12)\n", False)

    monkeypatch.setattr(runner, "_exec", failing)
    with pytest.raises(RuntimeError, match="requires go >= 1.99"):
        await runner.list_packages()

    async def slow(args, timeout, env=None):
        return gotool._Proc(-1, '{"Dir": "x"}', "", True)

    monkeypatch.setattr(runner, "_exec", slow)
    with pytest.raises(RuntimeError, match="timed out"):
        await runner.list_packages()


def test_capture_is_bounded():
    killed = []
    cap = gotool._Capture(10, lambda: killed.append(1))
    for _ in range(100):
        cap.add(b"0123456789" * 1000)
    assert cap.overflow and killed == [1]
    assert len(cap.head) == 10 and len(cap.tail) <= 2 * gotool._KEEP_TAIL
    assert cap.text().startswith("0123456789\n")


def test_sync_tree_mirrors_and_cleans(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "a" / ".git").mkdir(parents=True)
    (src / "a" / "x.go").write_text("package a\n")
    (src / "a" / ".git" / "HEAD").write_text("ref\n")
    (src / "out").mkdir()
    (src / "out" / "skip.go").write_text("package out\n")
    (src / "link").symlink_to("a")
    gotool._sync_tree(src, dst, {src / "out"})
    assert (dst / "a" / "x.go").read_text() == "package a\n"
    assert not (dst / "a" / ".git").exists() and not (dst / "out").exists()
    assert (dst / "link").is_symlink()
    (dst / "a" / "log").mkdir()
    (dst / "a" / "log" / "app.log").write_text("x")
    (dst / "a" / "x.go").write_text("package changed\n")
    gotool._sync_tree(src, dst, {src / "out"})
    assert not (dst / "a" / "log").exists()
    assert (dst / "a" / "x.go").read_text() == "package a\n"


BREAKS_CALC = "package calc\n\nfunc Clamp(v, lo, hi int) int { return undefinedThing }\n"


@needs_go
async def test_overlay_applies_under_a_symlinked_pwd(mod, tmp_path, monkeypatch):
    link = tmp_path / "link"
    link.symlink_to(mod)
    monkeypatch.setenv("PWD", str(link))
    runner = GoRunner(link, workers=2, timeout=120, work_dir=tmp_path / "work")
    res = await runner.test("calc", overlay={"calc/calc.go": BREAKS_CALC}, compile_only=True)
    assert not res.build_ok
    assert any("undefinedThing" in e for e in res.build_errors), res.build_errors


WRITES_LOG = """package calc

import (
	"os"
	"testing"
)

func TestWritesLog(t *testing.T) {
	if err := os.MkdirAll("log", 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile("log/app.log", []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
}
"""


@needs_go
async def test_sandbox_keeps_test_writes_out_of_the_module(mod, tmp_path):
    before = sorted(p.relative_to(mod) for p in mod.rglob("*"))
    sandbox = tmp_path / "out" / "work" / "module"
    runner = GoRunner(mod, workers=2, timeout=120, work_dir=tmp_path / "work",
                      sandbox=sandbox)
    res = await runner.test("calc", overlay={"calc/w_codocs_test.go": WRITES_LOG},
                            run=["TestWritesLog"])
    assert res.tests == {"TestWritesLog": "pass"}
    assert sorted(p.relative_to(mod) for p in mod.rglob("*")) == before
    assert (sandbox / "calc" / "log" / "app.log").is_file()
    pkgs = await runner.list_packages()
    assert set(pkgs) == {"calc", "shapes"}
    assert pkgs["calc"].go_files == ["calc/calc.go", "calc/stats.go"]

    again = GoRunner(mod, workers=2, timeout=120, work_dir=tmp_path / "work", sandbox=sandbox)
    res = await again.test("calc", overlay={"calc/calc.go": BREAKS_CALC}, compile_only=True)
    assert not res.build_ok and any(e.startswith("calc/calc.go:3:") for e in res.build_errors)
    assert not (sandbox / "calc" / "log").exists()


FLOOD_TEST = """package calc

import (
	"log"
	"testing"
)

func TestFlood(t *testing.T) {
	for {
		log.Printf("service registration failed, retrying: %s", "connection refused")
	}
}
"""


@needs_go
async def test_output_flood_is_killed_early(runner, monkeypatch):
    overlay = {"calc/flood_test.go": FLOOD_TEST}
    assert (await runner.test("calc", overlay=overlay, compile_only=True)).build_ok
    monkeypatch.setattr(gotool, "OUTPUT_LIMIT", 1024 * 1024)
    t0 = time.monotonic()
    res = await runner.test("calc", overlay=overlay, run=["TestFlood"], timeout=60)
    assert time.monotonic() - t0 < 30
    assert res.timed_out and res.build_ok
    assert res.tests == {"TestFlood": "fail"}
    assert "output exceeded" in res.tail
    assert _work_empty(runner)


HANG_TEST = """package calc

import (
	"testing"
	"time"
)

func TestHang(t *testing.T) { time.Sleep(60 * time.Second) }
"""


@needs_go
async def test_killed_go_leaves_no_work_dir_in_tmp(runner, tmp_path, monkeypatch):
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(sys_tmp))
    overlay = {"calc/hang_test.go": HANG_TEST}
    assert (await runner.test("calc", overlay=overlay, compile_only=True)).build_ok
    monkeypatch.setattr(gotool, "TIMEOUT_SLACK", -1)
    res = await runner.test("calc", overlay=overlay, run=["TestHang"], timeout=4)
    assert res.timed_out and res.tests == {"TestHang": "fail"}
    assert list(sys_tmp.glob("go-build*")) == []
    assert _work_empty(runner)
