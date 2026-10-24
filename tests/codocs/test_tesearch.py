from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codocs.config import Settings
from codocs.golang import gotool
from codocs.golang.gotool import GoRunner
from codocs.golang.mutate import mutation_sites
from codocs.golang.package import PackageInfo, Target
from codocs.golang.source import function_spans, imports
from codocs.llm import LLMError
from codocs.tesearch import measure as measure_mod
from codocs.tesearch.loop import (
    SuiteResult,
    SuiteRound,
    TeSearchLoop,
    go_requires,
    pick_best,
    run_testgen,
)
from codocs.tesearch.measure import (
    Measurement,
    failure_message,
    measure,
    prune,
    strip_non_tests,
)
from codocs.tesearch.prompts import (
    FAIL_MSG_CHARS,
    mark_failing,
    mark_source,
    refine_messages,
    seed_messages,
    suite_path,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")

STRONG = """package calc

import (
	"errors"
	"testing"
)

func TestClampTable(t *testing.T) {
	cases := []struct{ v, lo, hi, want int }{
		{5, 0, 10, 5}, {-1, 0, 10, 0}, {11, 0, 10, 10}, {0, 0, 10, 0}, {10, 0, 10, 10},
	}
	for _, c := range cases {
		if got := Clamp(c.v, c.lo, c.hi); got != c.want {
			t.Errorf("Clamp(%d,%d,%d) = %d, want %d", c.v, c.lo, c.hi, got, c.want)
		}
	}
}

func TestSumValues(t *testing.T) {
	if got, err := Sum([]int{1, 2, 3}); err != nil || got != 6 {
		t.Fatalf("Sum = %d, %v", got, err)
	}
	if _, err := Sum(nil); !errors.Is(err, ErrEmpty) {
		t.Fatalf("Sum(nil) err = %v", err)
	}
}

func TestNormalizeText(t *testing.T) {
	if got := Normalize("  Hello World "); got != "hello-world" {
		t.Fatalf("Normalize = %q", got)
	}
}
"""

WITH_FAILING = """package calc

import (
	"strings"
	"testing"
)

func TestClampOK(t *testing.T) {
	if Clamp(5, 0, 10) != 5 || Clamp(-1, 0, 10) != 0 {
		t.Fatal("clamp")
	}
}

func TestNormalizeWrong(t *testing.T) {
	if got := strings.ToUpper(Normalize("A B")); got != "nope" {
		t.Fatalf("got %q, want nope", got)
	}
}

func TestSumOK(t *testing.T) {
	if got, _ := Sum([]int{2, 3}); got != 5 {
		t.Fatal("sum")
	}
}
"""

BROKEN = """package calc

import "testing"

func TestBroken(t *testing.T) {
	if NoSuchFunc(1) != 2 {
		t.Fatal("x")
	}
}
"""

CLAMP_ONLY = """package calc

import "testing"

func TestClampOnly(t *testing.T) {
	if Clamp(5, 0, 10) != 5 || Clamp(-1, 0, 10) != 0 || Clamp(11, 0, 10) != 10 {
		t.Fatal("clamp")
	}
}
"""

THREE_TESTS = """package calc

import "testing"

func TestA(t *testing.T) {
	if Clamp(5, 0, 10) != 5 || Clamp(-1, 0, 10) != 0 || Clamp(11, 0, 10) != 10 {
		t.Fatal("clamp")
	}
}

func TestB(t *testing.T) {
	if v, err := Sum([]int{1}); v != 1 || err != nil {
		t.Fatal("sum")
	}
	if _, err := Sum(nil); err != ErrEmpty {
		t.Fatal("empty")
	}
}

func TestC(t *testing.T) {
	if Normalize(" A b ") != "a-b" {
		t.Fatal("norm")
	}
}
"""

ONE_TEST = """package calc

import "testing"

func TestAll(t *testing.T) {
	if Clamp(5, 0, 10) != 5 || Clamp(-1, 0, 10) != 0 || Clamp(11, 0, 10) != 10 {
		t.Fatal("clamp")
	}
	if v, err := Sum([]int{1}); v != 1 || err != nil {
		t.Fatal("sum")
	}
	if _, err := Sum(nil); err != ErrEmpty {
		t.Fatal("empty")
	}
	if Normalize(" A b ") != "a-b" {
		t.Fatal("norm")
	}
}
"""


class FakeLLM:
    def __init__(self, replies: list[str | Exception]):
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        self.prompts.append((system, user))
        if not self.replies:
            raise LLMError("no more replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class SpyRunner(GoRunner):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[dict] = []

    async def test(self, pkg_rel, **kw):
        self.calls.append(kw)
        return await super().test(pkg_rel, **kw)


@pytest.fixture
def module(tmp_path: Path) -> Path:
    root = tmp_path / "mod"
    shutil.copytree(FIXTURE, root)
    return root


def _runner(module: Path, tmp_path: Path, cls=GoRunner) -> GoRunner:
    return cls(module, workers=4, timeout=120, work_dir=tmp_path / "work")


def _calc() -> PackageInfo:
    return PackageInfo(
        rel="calc", import_path="example.com/fixture/calc", name="calc",
        go_files=["calc/calc.go", "calc/stats.go"],
        test_files=["calc/calc_test.go", "calc/stats_test.go"], xtest_files=[],
    )


def _target() -> Target:
    return Target(pkg="calc", file="calc/calc.go", stem="calc", lines=41)


def _settings(**kw) -> Settings:
    base = dict(test_rounds=3, test_threshold=0.85, mutants=6, mutant_seed=0, concurrency=2,
                workers=4)
    base.update(kw)
    return Settings(**base)


def _fence(src: str) -> str:
    return f"Here you go:\n```go\n{src}```\n"


# -- pure ---------------------------------------------------------------------


def test_suite_path():
    assert suite_path(_calc(), "calc") == "calc/calc_codocs_test.go"
    root = PackageInfo(rel="", import_path="m", name="m")
    assert suite_path(root, "main") == "main_codocs_test.go"


def test_mark_source_formatting():
    src = "a\nb\nc\n"
    survivors = [
        {"id": "m1", "line": 2, "operator": "cmp: < -> <="},
        {"id": "m2", "line": 2, "operator": "logic: && -> ||"},
        {"id": "m3", "line": 3, "operator": "cmp: < -> <="},
        {"id": "m4", "line": 3, "operator": "cmp: < -> <="},
    ]
    out = mark_source(src, [1, 3], survivors)
    assert out == (
        "a  // <-- NOT COVERED YET\n"
        "b  // <-- SURVIVING MUTANT: cmp: < -> <=; SURVIVING MUTANT: logic: && -> ||\n"
        "c  // <-- NOT COVERED YET; SURVIVING MUTANT: cmp: < -> <=\n"
    )
    assert mark_source("x\ny", [], []) == "x\ny"
    assert mark_source("if a {\n\treturn\n\t}\n", [1, 2, 3], []) == (
        "if a {  // <-- NOT COVERED YET\n\treturn  // <-- NOT COVERED YET\n\t}\n"
    )


def test_mark_failing_formatting():
    long = "calc_codocs_test.go:12: " + "x" * 400
    out = mark_failing(WITH_FAILING, {"TestNormalizeWrong": "\n  got \"A-B\", want nope\nmore",
                                      "TestSumOK": long})
    lines = out.splitlines()
    assert 'func TestNormalizeWrong(t *testing.T) {  // <-- FAILING: got "A-B", want nope' in lines
    sum_line = next(ln for ln in lines if ln.startswith("func TestSumOK"))
    mark = sum_line.split("// <-- FAILING: ", 1)[1]
    assert len(mark) == FAIL_MSG_CHARS and mark.endswith("...")
    assert "func TestClampOK(t *testing.T) {" in lines
    assert out.endswith("}\n") and out.count("FAILING") == 2
    assert mark_failing(WITH_FAILING, {}) == WITH_FAILING


def test_failure_message_drops_status_lines():
    out = "=== RUN   TestX\n    x_test.go:9: want 3, got 4\n--- FAIL: TestX (0.00s)\n"
    assert failure_message(out) == "x_test.go:9: want 3, got 4"
    assert failure_message("") == "test failed"


def test_prune_removes_imports_only_the_removed_tests_used():
    out = prune(WITH_FAILING, {"TestNormalizeWrong"})
    assert "TestNormalizeWrong" not in out
    assert [i.path for i in imports(out)] == ["testing"]
    assert "func TestSumOK" in out and "func TestClampOK" in out


def test_go_requires(tmp_path: Path):
    (tmp_path / "go.mod").write_text(
        "module m\n\ngo 1.23\n\nrequire github.com/a/b v1.0.0\n"
        "require github.com/x/y v0.1.0 // indirect\n"
        "require (\n\tgithub.com/c/d v1.2.0\n\tgolang.org/x/e v0.3.0 // indirect\n"
        "\tgithub.com/f/g/v2 v2.0.0\n)\n"
    )
    assert go_requires(tmp_path) == ["github.com/a/b", "github.com/c/d", "github.com/f/g/v2"]
    assert go_requires(tmp_path / "missing") == []


def _m(score_cov: float, green: int, build_ok: bool = True) -> Measurement:
    return Measurement(build_ok=build_ok, size=green, green_size=green, cov=score_cov)


def test_pick_best_tie_breaks():
    rounds = [SuiteRound(1, "", _m(0.8, 5)), SuiteRound(2, "", _m(0.8, 2)),
              SuiteRound(3, "", _m(0.8, 2)), SuiteRound(4, "", _m(0.5, 1))]
    assert pick_best(rounds).round == 2
    assert pick_best([SuiteRound(1, "", _m(0.0, 0, build_ok=False))],
                     measured_only=True) is None
    assert pick_best([]) is None


def test_prompts_seed_and_refine():
    pkg = _calc()
    src = (FIXTURE / "calc" / "calc.go").read_text()
    sys_, user = seed_messages("calc/calc.go", pkg, src, "// calc/stats.go\ntype Stats struct",
                               {"Clamp", "Sum", "Stats", "cdxHelper", "TestOld"},
                               ["github.com/a/b"])
    assert "package <name>" in sys_ and "cdx" in sys_
    assert "this module's own packages" in sys_
    assert "[TARGET FILE] calc/calc.go" in user
    assert "TestOld, cdxHelper" in user and "Clamp" not in user.split("[TAKEN")[1].split("\n")[1]
    assert "the packages of this module (`example.com/fixture/...`)" in user
    assert "github.com/a/b" in user
    assert "calc/calc_codocs_test.go" in user and "type Stats struct" in user

    best = Measurement(build_ok=True, size=3, passing=["TestClampOK", "TestSumOK"],
                       failing={"TestNormalizeWrong": "got \"A-B\", want nope"},
                       green_size=2, cov=0.5, uncovered=[39, 40], mut=0.5, mutants_valid=2,
                       killed=1, survivors=[{"id": "m303", "line": 16,
                                             "operator": "cmp: < -> <="}],
                       suite_src=WITH_FAILING)
    last = Measurement(build_ok=False, build_errors=["calc/calc_codocs_test.go:6:5: undefined: X"],
                       suite_src=BROKEN)
    sys_, user = refine_messages("calc/calc.go", pkg, src, "", set(), [], best, last)
    assert "IMPROVE" in sys_
    assert "if v < lo {  // <-- SURVIVING MUTANT: cmp: < -> <=" in user
    assert "s = strings.TrimSpace(strings.ToLower(s))  // <-- NOT COVERED YET" in user
    assert "// <-- FAILING: got \"A-B\", want nope" in user
    assert "size=3 green=2 coverage=0.500 mutation=0.500" in user
    assert "undefined: X" in user and "NoSuchFunc" not in user

    _, user = refine_messages("calc/calc.go", pkg, src, "", set(), [], None, last)
    assert "undefined: X" in user and "NoSuchFunc" in user and "[TARGET SOURCE]" in user


def test_result_roundtrip():
    m = Measurement(build_ok=True, size=2, passing=["TestA"], failing={"TestB": "x"},
                    green_src="g", green_size=1, cov=0.5, uncovered=[3], mut=None,
                    survivors=[{"id": "m1", "line": 3, "operator": "op"}], suite_src="s")
    res = SuiteResult(pkg="calc", file="calc/calc.go", stem="calc", status="ok",
                      rounds=[SuiteRound(1, "s", m, 1.5)], best_round=1, best_score=0.5,
                      best_cov=0.5, best_mut=None, best_size=1, threshold=0.85, max_rounds=3)
    d = json.loads(json.dumps(res.to_dict()))
    assert d["score_trajectory"] == [0.5]
    back = SuiteResult.from_dict(d)
    assert back == res and back.rounds[0].measurement.score == 0.5


# -- go -----------------------------------------------------------------------


@needs_go
async def test_strong_suite_early_stop(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    out = tmp_path / "out"
    llm = FakeLLM([_fence(STRONG), _fence(STRONG)])
    loop = TeSearchLoop(runner, llm, _settings(test_threshold=0.4), out, module)
    res = await loop.run_file(_target(), _calc())

    assert res.status == "ok" and len(res.rounds) == 1 and len(llm.prompts) == 1
    assert res.early_stop and res.best_round == 1
    m = res.rounds[0].measurement
    assert m.build_ok and m.size == 3 and m.green_size == 3 and not m.failing
    assert m.cov == pytest.approx(1.0) and m.uncovered == []
    assert m.mutants_total == 4 and m.mutants_valid == 4
    assert m.killed == 2 and m.mut == pytest.approx(0.5)
    assert {s["line"] for s in m.survivors} == {16, 19}
    assert res.best_score == pytest.approx(0.5)

    delivered = out / "tests" / "calc" / "calc_codocs_test.go"
    assert delivered.read_text() == m.green_src
    rd = out / "testgen" / "calc" / "calc" / "round_1"
    for name in ("suite.go", "green.go", "measure.json", "prompt.txt", "reply.txt"):
        assert (rd / name).exists(), name
    saved = json.loads((out / "testgen" / "calc" / "calc" / "result.json").read_text())
    assert SuiteResult.from_dict(saved).best_round == 1
    assert not (module / "calc" / "calc_codocs_test.go").exists()


@needs_go
async def test_failing_test_pruned_with_its_import(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    m = await measure(runner, _calc(), "calc/calc.go", WITH_FAILING, [])
    assert m.build_ok and m.size == 3 and m.green_size == 2
    assert list(m.failing) == ["TestNormalizeWrong"]
    assert "want nope" in m.failing["TestNormalizeWrong"]
    assert m.passing == ["TestClampOK", "TestSumOK"]
    assert "TestNormalizeWrong" not in m.green_src
    assert [i.path for i in imports(m.green_src)] == ["testing"]
    assert m.mut is None and 0 < m.cov < 1
    assert m.score == pytest.approx(m.cov)
    assert 40 in m.uncovered


@needs_go
async def test_build_failure_then_refine(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    llm = FakeLLM([_fence(BROKEN), _fence(STRONG)])
    loop = TeSearchLoop(runner, llm, _settings(test_threshold=1.01, test_rounds=2, mutants=0),
                        tmp_path / "out", module)
    res = await loop.run_file(_target(), _calc())
    assert [r.measurement.build_ok for r in res.rounds] == [False, True]
    assert any("undefined: NoSuchFunc" in e for e in res.rounds[0].measurement.build_errors)
    assert "undefined: NoSuchFunc" in llm.prompts[1][1]
    assert "did NOT COMPILE" in llm.prompts[1][1]
    assert res.best_round == 2 and res.best_cov == pytest.approx(1.0) and res.best_mut is None
    assert res.score_trajectory == [0.0, pytest.approx(1.0)]


@needs_go
async def test_unreachable_threshold_runs_all_rounds(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    llm = FakeLLM([_fence(CLAMP_ONLY)] * 3)
    loop = TeSearchLoop(runner, llm, _settings(test_threshold=1.01, mutants=0),
                        tmp_path / "out", module)
    res = await loop.run_file(_target(), _calc())
    assert len(res.rounds) == 3 and len(llm.prompts) == 3
    assert not res.early_stop and res.best_round == 1
    assert "NOT COVERED YET" in llm.prompts[1][1]


@needs_go
async def test_delivery_prefers_smaller_suite(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    out = tmp_path / "out"
    llm = FakeLLM([_fence(THREE_TESTS), _fence(ONE_TEST)])
    loop = TeSearchLoop(runner, llm, _settings(test_threshold=1.01, test_rounds=2, mutants=0),
                        out, module)
    res = await loop.run_file(_target(), _calc())
    scores = res.score_trajectory
    assert scores[0] == pytest.approx(1.0) and scores[1] == pytest.approx(1.0)
    assert res.best_round == 2 and res.best_size == 1
    assert (out / "tests" / "calc" / "calc_codocs_test.go").read_text() == ONE_TEST


@needs_go
async def test_uncovered_mutants_survive_without_running(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path, SpyRunner)
    src = (module / "calc" / "calc.go").read_text()
    mutants = mutation_sites(src)
    m = await measure(runner, _calc(), "calc/calc.go", CLAMP_ONLY, mutants)
    mutant_calls = [c for c in runner.calls if "calc/calc.go" in (c.get("overlay") or {})]
    run_calls = [c for c in mutant_calls if not c.get("compile_only")]
    compile_calls = [c for c in mutant_calls if c.get("compile_only")]
    assert len(run_calls) == 2 and len(compile_calls) == 2
    assert all(c["failfast"] and c["run"] == ["TestClampOnly"] for c in run_calls)
    assert all("calc/calc_codocs_test.go" not in c["overlay"] for c in compile_calls)
    assert m.mutants_total == 4 and m.mutants_valid == 4
    by_line = {s["line"] for s in m.survivors}
    assert {27, 32} <= by_line
    assert {27, 32} <= set(m.uncovered)
    assert m.killed == 0 and m.mut == 0.0 and m.score == 0.0


@needs_go
async def test_llm_error_keeps_completed_rounds(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    llm = FakeLLM([_fence(CLAMP_ONLY), LLMError("gateway down")])
    loop = TeSearchLoop(runner, llm, _settings(test_threshold=1.01, mutants=0),
                        tmp_path / "out", module)
    res = await loop.run_file(_target(), _calc())
    assert res.status == "error" and "gateway down" in res.error
    assert len(res.rounds) == 1 and res.best_round == 1 and res.best_size == 1


@needs_go
async def test_run_testgen_skips_and_resumes(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    out = tmp_path / "out"
    settings = _settings(test_threshold=0.4, mutants=0)
    pkgs = {"calc": _calc()}
    targets = [_target(), Target(pkg="calc", file="calc/stats.go", stem="stats", lines=25,
                                 status="no-funcs")]
    seen = []
    llm = FakeLLM([_fence(STRONG)])
    results = await run_testgen(targets, pkgs, runner, llm, settings, out, module,
                                on_progress=seen.append)
    assert [r.status for r in results] == ["ok", "no-funcs"]
    assert results[1].rounds == [] and len(llm.prompts) == 1 and len(seen) == 2

    llm2 = FakeLLM([])
    again = await run_testgen(targets[:1], pkgs, runner, llm2, settings, out, module)
    assert llm2.prompts == [] and again[0].best_round == 1
    assert again[0].to_dict() == results[0].to_dict()


# -- edge cases --------------------------------------------------------------------

EXAMPLES = """package calc

import (
\t"fmt"
\t"os"
\t"testing"
)

func TestClampOK(t *testing.T) {
\tif Clamp(5, 0, 10) != 5 {
\t\tt.Fatal("clamp")
\t}
}

func ExampleClamp() {
\tfmt.Println(Clamp(20, 0, 10))
\t// Output: 15
}

func FuzzClamp(f *testing.F) {
\tf.Add(1)
\tf.Fuzz(func(t *testing.T, v int) { _ = Clamp(v, 0, 10) })
}

func BenchmarkClamp(b *testing.B) {
\tfor i := 0; i < b.N; i++ {
\t\tClamp(i, 0, 10)
\t}
}

func TestMain(m *testing.M) { os.Exit(m.Run()) }

func Examples() int { return 1 }
"""


def test_strip_non_tests():
    out = strip_non_tests(EXAMPLES)
    names = list(function_spans(out))
    assert names == ["TestClampOK", "Examples"]
    assert [i.path for i in imports(out)] == ["testing"]
    assert strip_non_tests(STRONG) == STRONG


@needs_go
async def test_examples_and_fuzz_never_reach_the_green_suite(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    m = await measure(runner, _calc(), "calc/calc.go", EXAMPLES, [])
    assert m.build_ok and m.size == 1 and m.green_size == 1
    assert m.passing == ["TestClampOK"]
    assert "ExampleClamp" not in m.green_src and "FuzzClamp" not in m.green_src
    assert "TestMain" not in m.green_src
    res = await runner.test("calc", overlay={"calc/calc_test.go": None,
                                             "calc/stats_test.go": None,
                                             "calc/calc_codocs_test.go": m.green_src})
    assert res.build_ok and res.returncode == 0 and res.tests == {"TestClampOK": "pass"}


PANICS = "\n".join(
    f"func TestPanics{i}(t *testing.T) {{\n\tvar s *Stats\n\ts.Add({i})\n}}\n"
    for i in range(5)
)
GOOD = "\n".join(
    f"func TestClamp{i}(t *testing.T) {{\n\tif Clamp({i}, 0, 10) != {i} {{\n\t\tt.Fatal(\"x\")\n"
    f"\t}}\n}}\n" for i in range(5)
)
CRASHERS = f"package calc\n\nimport \"testing\"\n\n{PANICS}\n{GOOD}"


@needs_go
async def test_many_panicking_tests_keep_the_passing_ones(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    m = await measure(runner, _calc(), "calc/calc.go", CRASHERS, [])
    assert m.build_ok and m.size == 10
    assert m.green_size == 5 and m.passing == [f"TestClamp{i}" for i in range(5)]
    assert sorted(m.failing) == [f"TestPanics{i}" for i in range(5)]
    assert all("nil pointer" in msg for msg in m.failing.values())
    assert m.cov > 0 and m.score > 0


PARALLEL = """package calc

import (
\t"testing"
\t"time"
)

func TestClampA(t *testing.T) {
\tt.Parallel()
\ttime.Sleep(300 * time.Millisecond)
\tif Clamp(5, 0, 10) != 5 {
\t\tt.Fatal("a")
\t}
}

func TestBoom(t *testing.T) {
\tt.Parallel()
\ttime.Sleep(20 * time.Millisecond)
\tvar m map[string]int
\tm["x"] = 1
}

func TestClampB(t *testing.T) {
\tt.Parallel()
\ttime.Sleep(300 * time.Millisecond)
\tif Clamp(-1, 0, 10) != 0 {
\t\tt.Fatal("b")
\t}
}
"""


@needs_go
async def test_parallel_tests_cut_short_by_a_panic_are_kept(module: Path, tmp_path: Path):
    runner = _runner(module, tmp_path)
    m = await measure(runner, _calc(), "calc/calc.go", PARALLEL, [])
    assert list(m.failing) == ["TestBoom"]
    assert "nil map" in m.failing["TestBoom"]
    assert m.green_size == 2 and m.passing == ["TestClampA", "TestClampB"]


SLOW = """package calc

import (
\t"testing"
\t"time"
)

func TestSlowClamp(t *testing.T) {
\ttime.Sleep(2 * time.Second)
\tif Clamp(5, 0, 10) != 5 || Clamp(-1, 0, 10) != 0 || Clamp(11, 0, 10) != 10 {
\t\tt.Fatal("clamp")
\t}
}
"""


@needs_go
async def test_mutant_timeout_follows_the_green_run(module: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(measure_mod, "MUTANT_MIN_TIMEOUT", 0)
    runner = _runner(module, tmp_path, SpyRunner)
    src = (module / "calc" / "calc.go").read_text()
    mutants = [m for m in mutation_sites(src) if m.line == 16][:1]
    m = await measure(runner, _calc(), "calc/calc.go", SLOW, mutants)
    assert m.green_size == 1 and m.mutants_valid == 1
    timeouts = [c["timeout"] for c in runner.calls if c.get("failfast")]
    assert timeouts and all(t >= 16 for t in timeouts)


GREET_MOD = """package calc

import "strings"

func Add(a, b int) int {
\treturn a + b
}

func Greet(n string) string {
\tm := strings.ToUpper(n)
\treturn m + suffix()
}

func suffix() string { return "!" }
"""

ADD_ONLY = """package calc

import "testing"

func TestAdd(t *testing.T) {
\tif Add(2, 3) != 5 {
\t\tt.Fatal("add")
\t}
}
"""


@needs_go
async def test_uncompilable_uncovered_mutant_is_invalid(module: Path, tmp_path: Path):
    (module / "calc" / "greet.go").write_text(GREET_MOD)
    pkg = _calc()
    pkg.go_files = pkg.go_files + ["calc/greet.go"]
    runner = _runner(module, tmp_path)
    mutants = mutation_sites(GREET_MOD)
    assert [x.line for x in mutants] == [6, 11]
    m = await measure(runner, pkg, "calc/greet.go", ADD_ONLY, mutants)
    assert 11 in m.uncovered
    assert m.mutants_total == 2 and m.mutants_valid == 1
    assert m.killed == 1 and m.survivors == [] and m.mut == 1.0


async def test_timeout_before_build_is_not_blamed_on_tests(module: Path, tmp_path: Path,
                                                         monkeypatch):
    script = tmp_path / "slowgo"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(gotool, "TIMEOUT_SLACK", 1)
    runner = GoRunner(module, go=str(script), timeout=0, work_dir=tmp_path / "work")
    m = await measure(runner, _calc(), "calc/calc.go", STRONG, [])
    assert not m.build_ok and m.failing == {}
    assert m.build_errors == [gotool.NOT_STARTED]


def test_pick_best_prefers_a_real_suite_on_score_ties():
    broken = SuiteRound(1, "", Measurement(build_ok=False))
    empty = SuiteRound(1, "", Measurement(build_ok=True, size=3, green_size=0))
    weak = SuiteRound(2, "", Measurement(build_ok=True, size=6, green_size=6, cov=0.55, mut=0.0,
                                         uncovered=[3, 4]))
    assert weak.measurement.score == 0.0
    assert pick_best([broken, weak]).round == 2
    assert pick_best([empty, weak], measured_only=True) is weak
    lo = SuiteRound(3, "", Measurement(build_ok=True, size=2, green_size=2, cov=0.4, mut=0.0))
    assert pick_best([lo, weak]) is weak


def test_refine_prompt_shows_a_non_improving_last_round():
    pkg = _calc()
    src = (FIXTURE / "calc" / "calc.go").read_text()
    best = Measurement(build_ok=True, size=3, green_size=3, cov=0.5, suite_src=STRONG)
    worse = Measurement(build_ok=True, size=1, green_size=1, cov=0.3, suite_src=CLAMP_ONLY,
                        failing={"TestX": "want 1"})
    _, r2 = refine_messages("calc/calc.go", pkg, src, "", set(), [], best, best, attempt=2,
                            last_round=1)
    _, r3 = refine_messages("calc/calc.go", pkg, src, "", set(), [], best, worse, attempt=3,
                            last_round=2)
    assert r2 != r3
    assert "[LAST ATTEMPT (round 2) - it built but did NOT beat" in r3
    assert "coverage=0.300" in r3 and "TestX FAILED: want 1" in r3
    assert "LAST ATTEMPT" not in r2 and r2.startswith("[ROUND 2]")


@needs_go
async def test_non_improving_round_is_not_replayed(module: Path, tmp_path: Path, monkeypatch):
    from codocs.llm import LLM
    from codocs.repowiki.llm.client import LLMClient

    replies = iter([_fence(STRONG), _fence(CLAMP_ONLY), _fence(CLAMP_ONLY), _fence(ONE_TEST)])

    async def fake_complete(client, messages, **kw):
        return next(replies)

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    settings = _settings(test_threshold=1.01, mutants=0, api_key="k",
                         cache_path=str(tmp_path / "cache.db"))
    async with LLM(settings) as llm:
        loop = TeSearchLoop(_runner(module, tmp_path), llm, settings, tmp_path / "out", module)
        res = await loop.run_file(_target(), _calc())
        assert llm.calls == 3 and llm.cache_hits == 0
    assert len(res.rounds) == 3
    rounds = tmp_path / "out" / "testgen" / "calc" / "calc"
    prompts = [(rounds / f"round_{r}" / "prompt.txt").read_text() for r in (2, 3)]
    assert prompts[0] != prompts[1]
    assert "LAST ATTEMPT (round 2)" in prompts[1]
