from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codocs.baseline import Baseline
from codocs.config import Settings
from codocs.golang.gotool import GoRunner
from codocs.golang.package import PackageInfo, Target
from codocs.golang.source import Import
from codocs.llm import LLMError
from codocs.repowiki.analysis.leafdocs import LeafDocIndex
from codocs.specforge.docfmt import (
    SECTION_RE,
    clean_doc,
    doc_path,
    expected_sections,
    missing_sections,
)
from codocs.specforge.loop import (
    DocEval,
    DocResult,
    DocRound,
    SpecForgeLoop,
    codegen_max_tokens,
    result_path,
    run_docgen,
    write_result,
)
from codocs.specforge.prompts import (
    CODEGEN,
    MAX_FAILURE_CHARS,
    REFINE,
    SEED,
    codegen_messages,
    load_prompt,
    refine_messages,
    render_evaluation,
    seed_messages,
)

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
PKGS = {"calc": CALC, "shapes": SHAPES}
CALC_BASE = Baseline(pkg="calc", build_ok=True,
                     passing=["TestClamp", "TestNormalize", "TestStatsAdd", "TestStatsMean",
                              "TestSum"],
                     failing=["TestKnownBroken"], build_errors=[])
SHAPES_BASE = Baseline(pkg="shapes", build_ok=True)

CALC_SRC = (FIXTURE / "calc" / "calc.go").read_text()
STATS_SRC = (FIXTURE / "calc" / "stats.go").read_text()
SHAPES_SRC = (FIXTURE / "shapes" / "shapes.go").read_text()

CALC_DOC = """# calc/calc.go

**Package**: `calc` (`example.com/fixture/calc`)

Basic integer and string helpers.

## VariableDef ErrEmpty
**ErrEmpty**: error returned for empty input.

```go
var ErrEmpty = errors.New("calc: empty input")
```

## VariableDef Scale
**Scale**: fixed-point factor used by Stats.Mean.

```go
const Scale = 10
```

## FunctionDef Clamp
**Clamp**: limits v to the closed range [lo, hi].

**Signature**: `func Clamp(v, lo, hi int) int`

## FunctionDef Sum
**Sum**: adds all values, ErrEmpty when there are none.

**Signature**: `func Sum(values []int) (int, error)`

## FunctionDef Normalize
**Normalize**: trims, lower-cases and dashes a string.

**Signature**: `func Normalize(s string) string`
"""

STATS_DOC = """# calc/stats.go

**Package**: `calc` (`example.com/fixture/calc`)

Running statistics over integers.

## ClassDef Stats
**Stats**: accumulates integer observations.

```go
type Stats struct {
\tCount int
\tTotal int
\tMax   int
}
```

## MethodDef Stats.Add
**Stats.Add**: records one observation.

**Signature**: `func (s *Stats) Add(v int)`

## MethodDef Stats.Mean
**Stats.Mean**: fixed-point mean, 0 when empty.

**Signature**: `func (s Stats) Mean() int`
"""

SHAPES_DOC = """# shapes/shapes.go

**Package**: `shapes` (`example.com/fixture/shapes`)

Geometry helpers.

## ClassDef Shape
**Shape**: anything with an area and a name.

## ClassDef Rect
**Rect**: axis-aligned rectangle.

## MethodDef Rect.Area
**Rect.Area**: area, 0 for negative sides.

## MethodDef Rect.Name
**Rect.Name**: printable name.

## FunctionDef Classify
**Classify**: buckets an area.

## FunctionDef Largest
**Largest**: biggest shape.
"""

CALC_BAD_CLAMP = CALC_SRC.replace("if v > hi {\n\t\treturn hi", "if v > hi {\n\t\treturn v")
SHAPES_BAD = SHAPES_SRC.replace('return "large"', 'return "big"')

SHAPES_SUITE = """package shapes

import "testing"

func TestClassifyBuckets(t *testing.T) {
\tif Classify(Rect{W: 5, H: 5}) != "large" {
\t\tt.Fatal("want large")
\t}
}

func TestRectArea(t *testing.T) {
\tif (Rect{W: 2, H: 3}).Area() != 6 {
\t\tt.Fatal("want 6")
\t}
}
"""

STATS_SUITE = """package calc

import "testing"

func TestStatsAdd(t *testing.T) {
\tvar s Stats
\ts.Add(4)
\tif s.Count != 1 || s.Max != 4 {
\t\tt.Fatalf("%+v", s)
\t}
}
"""


class FakeLLM:
    """scripted llm: picks the reply list by system prompt; the last reply repeats."""

    def __init__(self, seed: list[str], code: list[str], refine: list[str] | None = None,
                 fail_on: tuple[str, int] | None = None):
        self.replies = {"seed": seed, "codegen": code, "refine": refine or seed}
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.kinds = {load_prompt(SEED): "seed", load_prompt(CODEGEN): "codegen",
                      load_prompt(REFINE): "refine"}

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c["kind"] == kind)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        kind = self.kinds[system]
        n = self.count(kind)
        self.calls.append({"kind": kind, "user": user, "temperature": temperature, "tag": tag})
        if self.fail_on == (kind, n + 1):
            raise LLMError("boom")
        replies = self.replies[kind]
        return replies[min(n, len(replies) - 1)]


def _go(code: str) -> str:
    return f"Here you go:\n```go\n{code}```\n"


def _settings(**kw) -> Settings:
    base = dict(doc_rounds=3, doc_threshold=0.9, concurrency=2, workers=4, go_timeout=120)
    base.update(kw)
    return Settings(**base)


def _target(pkg: str, rel: str, status: str = "ok") -> Target:
    stem = Path(rel).stem
    return Target(pkg=pkg, file=rel, stem=stem, lines=10, status=status)


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    dst = tmp_path / "gomod"
    shutil.copytree(FIXTURE, dst)
    return dst


def _loop(mod: Path, out: Path, llm, **kw) -> SpecForgeLoop:
    runner = GoRunner(mod, work_dir=out.parent / "work", workers=4)
    return SpecForgeLoop(runner, llm, _settings(**kw), out, mod)


# -- docfmt ---------------------------------------------------------------------


def test_expected_sections():
    assert expected_sections(CALC_SRC) == [
        ("VariableDef", "ErrEmpty"), ("VariableDef", "Scale"), ("FunctionDef", "Clamp"),
        ("FunctionDef", "Sum"), ("FunctionDef", "Normalize"),
    ]
    assert expected_sections(STATS_SRC) == [
        ("ClassDef", "Stats"), ("MethodDef", "Stats.Add"), ("MethodDef", "Stats.Mean"),
    ]
    assert expected_sections("package p\n\nfunc init() {}\n\nfunc init() {}\n") == [
        ("FunctionDef", "init")]


def test_missing_sections():
    assert missing_sections(CALC_DOC, CALC_SRC) == []
    assert missing_sections(STATS_DOC, STATS_SRC) == []
    doc = CALC_DOC.replace("## FunctionDef Sum", "## Sum")
    assert missing_sections(doc, CALC_SRC) == ["FunctionDef Sum"]
    assert missing_sections("", STATS_SRC) == [
        "ClassDef Stats", "MethodDef Stats.Add", "MethodDef Stats.Mean"]


def test_missing_sections_tolerant_headers():
    doc = ("## ClassDef `Stats`\n## MethodDef (*Stats).Add\n## MethodDef Stats.Mean()\n")
    assert missing_sections(doc, STATS_SRC) == []
    src = "package p\n\nconst (\n\tA = iota\n\tB\n)\n\nvar c, d = 1, 2\n"
    assert missing_sections("## VariableDef A, B\n## VariableDef c\n", src) == ["VariableDef d"]
    generic = "package p\n\ntype M[K comparable] struct{}\n\nfunc (m *M[K]) Get() {}\n"
    assert missing_sections("## ClassDef M\n## MethodDef M[K].Get\n", generic) == []


def test_section_re():
    m = SECTION_RE.search("x\n## MethodDef Stats.Add  \nbody")
    assert m and m.group(1) == "MethodDef" and m.group(2) == "Stats.Add"
    assert SECTION_RE.search("### FunctionDef X\n") is None


def test_clean_doc():
    assert clean_doc("intro\n<documentation>\n# a.go\n\nbody\n</documentation>\ntrailer") == (
        "# a.go\n\nbody\n")
    assert clean_doc("```markdown\n# a.go\n\n```go\nconst A = 1\n```\n```") == (
        "# a.go\n\n```go\nconst A = 1\n```\n")
    assert clean_doc("<documentation>```md\n# a.go\n```</documentation>") == "# a.go\n"
    assert clean_doc("<documentation>\n# a.go\nunterminated") == "# a.go\nunterminated\n"
    assert clean_doc("# a.go\n\n```go\nx\n```") == "# a.go\n\n```go\nx\n```\n"
    assert clean_doc("  ") == ""


def test_doc_path(tmp_path: Path):
    assert doc_path(tmp_path, "calc", "stats") == (
        tmp_path / "docs" / "calc" / "markdown_docs" / "stats.md")
    assert doc_path(tmp_path, "a/b", "x") == (
        tmp_path / "docs" / "a" / "b" / "markdown_docs" / "x.md")
    assert doc_path(tmp_path, "", "main") == tmp_path / "docs" / "markdown_docs" / "main.md"


# -- prompts --------------------------------------------------------------------


def test_system_prompts_load():
    for name in (SEED, CODEGEN, REFINE):
        assert len(load_prompt(name)) > 500
    assert "<documentation>" in load_prompt(REFINE)
    assert "MethodDef" in load_prompt(SEED) and "MethodDef" in load_prompt(REFINE)


def test_seed_messages():
    context = "// calc/calc.go\nconst Scale = 10\n"
    system, user = seed_messages("calc/stats.go", CALC, STATS_SRC, context)
    assert system == load_prompt(SEED)
    assert "# calc/stats.go" in user
    assert "**Package**: `calc` (`example.com/fixture/calc`)" in user
    assert "- `## MethodDef Stats.Add`" in user
    assert user.index("ClassDef Stats") < user.index("MethodDef Stats.Mean")
    assert "s.Total * Scale / s.Count" in user
    assert "const Scale = 10" in user


def test_codegen_messages_never_show_source():
    allowed = [Import(path="errors"), Import(path="strings", alias="str")]
    system, user = codegen_messages("calc/calc.go", CALC, CALC_DOC, "", allowed)
    assert system == load_prompt(CODEGEN)
    assert "package calc" in user and "`calc.go`" in user
    assert '\t"errors"\n\tstr "strings"\n' in user
    assert "## FunctionDef Clamp" in user
    assert "strings.ReplaceAll" not in user and "return lo" not in user
    _, user = codegen_messages("calc/stats.go", CALC, STATS_DOC, "", [])
    assert "imports nothing" in user


def test_refine_messages_failing_tests():
    ev = DocEval(signal="tests", build_ok=True, phi=0.5, relevant=["TestA", "TestB"],
                 passed=["TestA"],
                 failing={"TestB": "=== RUN   TestB\n    calc_test.go:9: got 1 want 2\n--- FAIL"})
    system, user = refine_messages("calc/calc.go", CALC, CALC_SRC, CALC_DOC, "package calc\n",
                                   ev, ["FunctionDef Sum"])
    assert system == load_prompt(REFINE)
    assert "phi = 0.50 (1/2 tests passed)" in user
    assert "#### TestB" in user and "calc_test.go:9: got 1 want 2" in user
    assert "=== RUN" not in user
    assert "- `## FunctionDef Sum`" in user
    assert "strings.ReplaceAll" in user
    assert "<documentation>" in user
    assert "````markdown" in user


def test_render_evaluation_limits():
    errs = [f"calc/calc.go:{i}:1: undefined: x{i}" for i in range(100)]
    text = render_evaluation(DocEval(signal="tests", build_ok=False, phi=0.0,
                                     relevant=["TestA"], build_errors=errs))
    assert "calc/calc.go:39:1" in text and "calc/calc.go:40:1" not in text
    assert "60 more lines omitted" in text

    failing = {f"TestN{i:02d}": "x" * 5000 for i in range(10)}
    failing["TestInformative"] = "    calc_test.go:3: got 1 want 2"
    failing["TestSilent"] = ""
    text = render_evaluation(DocEval(signal="tests", build_ok=True, phi=0.0,
                                     relevant=sorted(failing), failing=failing))
    assert text.index("TestInformative") < text.index("TestN00")
    assert text.count("x") <= MAX_FAILURE_CHARS
    assert "Also failing (output omitted):" in text
    assert "phi = 1.00 (builds)" in render_evaluation(
        DocEval(signal="compile", build_ok=True, phi=1.0))


# -- dataclasses ----------------------------------------------------------------


def test_result_roundtrip():
    ev = DocEval(signal="tests", build_ok=True, phi=0.5, relevant=["A", "B"], passed=["A"],
                 failing={"B": "boom"}, build_errors=[], import_fixes=1)
    res = DocResult(pkg="calc", file="calc/calc.go", stem="calc", status="ok", signal="tests",
                    rounds=[DocRound(round=1, doc="d", code="c", eval=ev, missing=["X y"],
                                     seconds=1.5)],
                    best_round=1, best_phi=0.5, early_stop=False, threshold=0.9, max_rounds=3)
    d = json.loads(json.dumps(res.to_dict()))
    assert d["phi_trajectory"] == [0.5]
    assert DocResult.from_dict(d) == res
    assert res.phi_trajectory == [0.5]


# -- the loop on the fixture module -----------------------------------------------


@needs_go
async def test_perfect_round_one_early_stop(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    llm = FakeLLM(seed=[f"<documentation>\n{STATS_DOC}</documentation>"], code=[_go(STATS_SRC)])
    res = await _loop(mod, out, llm).run_file(_target("calc", "calc/stats.go"), CALC, CALC_BASE)
    assert res.status == "ok" and res.signal == "tests"
    assert res.phi_trajectory == [1.0]
    assert res.best_round == 1 and res.best_phi == 1.0 and res.early_stop
    assert res.rounds[0].eval.relevant == ["TestStatsAdd", "TestStatsMean"]
    assert res.rounds[0].missing == []
    assert [c["kind"] for c in llm.calls] == ["seed", "codegen"]
    assert llm.calls[1]["temperature"] == 0.0
    assert "s.Total * Scale" not in llm.calls[1]["user"]

    rd = out / "docgen" / "calc" / "stats" / "round_1"
    for name in ("doc.md", "code.go", "eval.json", "prompt_doc.txt", "reply_doc.txt",
                 "prompt_code.txt", "reply_code.txt"):
        assert (rd / name).is_file(), name
    assert not (out / "docgen" / "calc" / "stats" / "round_2").exists()
    assert json.loads((rd / "eval.json").read_text())["phi"] == 1.0
    saved = DocResult.from_dict(json.loads(result_path(out, "calc", "stats").read_text()))
    assert saved == res
    assert doc_path(out, "calc", "stats").read_text() == STATS_DOC
    assert not (mod / "calc" / "stats_codocs_test.go").exists()
    assert (mod / "calc" / "stats.go").read_text() == STATS_SRC


@needs_go
async def test_refine_from_failing_round(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    refined = CALC_DOC.replace("limits v", "limits v (values above hi become hi)")
    llm = FakeLLM(seed=[CALC_DOC], refine=[f"<documentation>{refined}</documentation>"],
                  code=[_go(CALC_BAD_CLAMP), _go(CALC_SRC)])
    res = await _loop(mod, out, llm).run_file(_target("calc", "calc/calc.go"), CALC, CALC_BASE)
    assert res.signal == "tests"
    assert len(res.rounds) == 2
    assert res.phi_trajectory[0] < 1.0 and res.phi_trajectory[1] == 1.0
    assert res.phi_trajectory[0] == pytest.approx(2 / 3)
    assert res.best_round == 2 and res.early_stop
    r1 = res.rounds[0].eval
    assert r1.relevant == ["TestClamp", "TestNormalize", "TestSum"]
    assert list(r1.failing) == ["TestClamp"] and "Clamp(11,0,10)" in r1.failing["TestClamp"]
    assert [c["kind"] for c in llm.calls] == ["seed", "codegen", "refine", "codegen"]
    refine_user = llm.calls[2]["user"]
    assert "#### TestClamp" in refine_user and "return v" in refine_user
    assert "Basic integer and string helpers." in refine_user
    assert doc_path(out, "calc", "calc").read_text() == refined


@needs_go
async def test_unused_import_fixed_without_llm(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    code = STATS_SRC.replace("package calc\n", 'package calc\n\nimport "fmt"\n', 1)
    llm = FakeLLM(seed=[STATS_DOC], code=[_go(code)])
    res = await _loop(mod, out, llm).run_file(_target("calc", "calc/stats.go"), CALC, CALC_BASE)
    ev = res.rounds[0].eval
    assert ev.build_ok and ev.phi == 1.0 and ev.import_fixes == 1
    assert '"fmt"' not in res.rounds[0].code
    assert len(llm.calls) == 2


@needs_go
async def test_missing_import_added_without_llm(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    code = CALC_SRC.replace('\t"strings"\n', "")
    llm = FakeLLM(seed=[CALC_DOC], code=[_go(code)])
    res = await _loop(mod, out, llm).run_file(_target("calc", "calc/calc.go"), CALC, CALC_BASE)
    ev = res.rounds[0].eval
    assert ev.phi == 1.0 and ev.import_fixes == 1
    assert '"strings"' in res.rounds[0].code
    assert len(llm.calls) == 2


@needs_go
async def test_no_tests_uses_compile_signal(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    broken = SHAPES_SRC.replace("func Largest", "func Largest(")
    llm = FakeLLM(seed=[SHAPES_DOC], code=[_go(broken), _go(SHAPES_SRC)])
    res = await _loop(mod, out, llm).run_file(_target("shapes", "shapes/shapes.go"), SHAPES,
                                               SHAPES_BASE)
    assert res.signal == "compile"
    assert res.phi_trajectory == [0.0, 1.0]
    assert not res.rounds[0].eval.build_ok and res.rounds[0].eval.build_errors
    assert res.rounds[0].eval.relevant == []
    assert res.best_round == 2
    assert "Build failed" in llm.calls[2]["user"]
    assert doc_path(out, "shapes", "shapes").read_text() == SHAPES_DOC


@needs_go
async def test_generated_tests_signal(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    suite = out / "tests" / "shapes" / "shapes_codocs_test.go"
    suite.parent.mkdir(parents=True)
    suite.write_text(SHAPES_SUITE)
    llm = FakeLLM(seed=[SHAPES_DOC], code=[_go(SHAPES_BAD)])
    res = await _loop(mod, out, llm, doc_rounds=1).run_file(
        _target("shapes", "shapes/shapes.go"), SHAPES, SHAPES_BASE)
    assert res.signal == "generated-tests"
    ev = res.rounds[0].eval
    assert ev.relevant == ["TestClassifyBuckets", "TestRectArea"]
    assert ev.passed == ["TestRectArea"] and list(ev.failing) == ["TestClassifyBuckets"]
    assert res.best_phi == 0.5 and not res.early_stop and res.max_rounds == 1
    assert not (mod / "shapes" / "shapes_codocs_test.go").exists()


@needs_go
async def test_generated_tests_hide_package_tests(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    no_tests = Baseline(pkg="calc", build_ok=True)
    llm = FakeLLM(seed=[STATS_DOC], code=[_go(STATS_SRC)])
    loop = _loop(mod, out, llm)
    res = await loop.run_file(_target("calc", "calc/stats.go"), CALC, no_tests,
                              generated_suite=STATS_SUITE)
    assert res.signal == "generated-tests"
    assert res.rounds[0].eval.relevant == ["TestStatsAdd"]
    assert res.best_phi == 1.0

    loop.use_generated_tests = False
    out_suite = out / "tests" / "calc" / "stats_codocs_test.go"
    out_suite.parent.mkdir(parents=True)
    out_suite.write_text(STATS_SUITE)
    res = await loop.run_file(_target("calc", "calc/stats.go"), CALC, no_tests)
    assert res.signal == "compile" and res.best_phi == 1.0


@needs_go
async def test_threshold_never_reached_and_resume(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    llm = FakeLLM(seed=[CALC_DOC], code=[_go(CALC_BAD_CLAMP)])
    runner = GoRunner(mod, work_dir=tmp_path / "work", workers=4)
    settings = _settings(doc_rounds=2)
    targets = [_target("calc", "calc/calc.go"), _target("calc", "calc/stats.go"),
               _target("shapes", "shapes/gen.go", status="generated")]
    llm.replies["codegen"] = [_go(CALC_BAD_CLAMP), _go(CALC_BAD_CLAMP), _go(STATS_SRC)]
    llm.replies["seed"] = [CALC_DOC, STATS_DOC]
    settings.concurrency = 1
    progress: list[str] = []
    res = await run_docgen(targets, PKGS, runner, llm, settings, out, mod,
                           on_progress=progress.append)
    calc, stats, gen = res
    assert len(calc.rounds) == 2 and calc.max_rounds == 2
    assert not calc.early_stop
    assert calc.phi_trajectory == pytest.approx([2 / 3, 2 / 3])
    assert calc.best_round == 1
    assert stats.best_phi == 1.0 and stats.early_stop
    assert gen.status == "generated" and gen.rounds == [] and gen.best_round == 0
    assert (out / "baseline" / "calc.json").is_file()
    assert len(progress) == 3
    calls = len(llm.calls)
    assert calls == 6

    again = await run_docgen(targets, PKGS, runner, llm, settings, out, mod)
    assert len(llm.calls) == calls
    assert again[0] == calc and again[1] == stats

    forced = await run_docgen(targets[1:2], PKGS, runner, llm, settings, out, mod, force=True)
    assert len(llm.calls) == calls + 2 and forced[0].status == "ok"


@needs_go
async def test_llm_error_keeps_completed_rounds(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    llm = FakeLLM(seed=[CALC_DOC], code=[_go(CALC_BAD_CLAMP)], fail_on=("refine", 1))
    res = await _loop(mod, out, llm).run_file(_target("calc", "calc/calc.go"), CALC, CALC_BASE)
    assert res.status == "error" and "boom" in res.error
    assert len(res.rounds) == 1 and res.best_round == 1
    assert doc_path(out, "calc", "calc").read_text() == CALC_DOC
    assert json.loads(result_path(out, "calc", "calc").read_text())["status"] == "error"

    llm.fail_on = None
    res = await run_docgen([_target("calc", "calc/calc.go")], PKGS,
                           GoRunner(mod, work_dir=tmp_path / "work"), llm,
                           _settings(doc_rounds=1), out, mod)
    assert res[0].status == "ok" and len(res[0].rounds) == 1


@needs_go
async def test_leafdocs_contract(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    llm = FakeLLM(seed=[CALC_DOC, STATS_DOC, SHAPES_DOC],
                  code=[_go(CALC_SRC), _go(STATS_SRC), _go(SHAPES_SRC)])
    settings = _settings(concurrency=1)
    runner = GoRunner(mod, work_dir=tmp_path / "work", workers=4)
    targets = [_target("calc", "calc/calc.go"), _target("calc", "calc/stats.go"),
               _target("shapes", "shapes/shapes.go")]
    res = await run_docgen(targets, PKGS, runner, llm, settings, out, mod)
    assert [r.best_phi for r in res] == [1.0, 1.0, 1.0]

    index = LeafDocIndex.build(out / "docs", known_packages={"calc", "shapes"})
    entries = {e.name: e for e in index.digests["calc"].entries}
    for name in ("Clamp", "Sum", "Normalize", "Stats", "Stats.Add", "Stats.Mean"):
        assert name in entries, name
        assert entries[name].summary.strip(), name
    assert entries["Stats.Add"].kind == "method" and entries["Stats"].kind == "class"
    assert entries["Clamp"].summary == "limits v to the closed range [lo, hi]."
    assert "Rect.Area" in {e.name for e in index.digests["shapes"].entries}


# -- edge cases --------------------------------------------------------------------


def test_clean_doc_wrapper_variants():
    want = "# a.go\n\n```go\nconst A = 1\n```\n"
    assert clean_doc("````markdown\n# a.go\n\n```go\nconst A = 1\n```\n````") == want
    assert clean_doc("~~~markdown\n# a.go\n\n```go\nconst A = 1\n```\n~~~") == want
    assert clean_doc("```markdown\n# a.go\nbody\n```\n\nNote: done.") == "# a.go\nbody\n"
    assert clean_doc("Here is the documentation:\n\n# a.go\nbody") == "# a.go\nbody\n"
    assert clean_doc("Sure:\n```md\n# a.go\nbody\n```\nbye") == "# a.go\nbody\n"
    assert clean_doc("<documentation>````markdown\n# a.go\n```go\nx\n```\n````"
                     "</documentation>") == "# a.go\n```go\nx\n```\n"


def test_write_result_removes_a_stale_doc(tmp_path: Path):
    ok = DocResult(pkg="core", file="core/x.go", stem="x", status="ok", best_round=1,
                   rounds=[DocRound(1, "# core/x.go\n", "", DocEval("compile", True, 1.0))])
    write_result(tmp_path, ok)
    assert doc_path(tmp_path, "core", "x").read_text() == "# core/x.go\n"
    write_result(tmp_path, DocResult(pkg="core", file="core/x.go", stem="x", status="error",
                                     error="round 1: gateway down"))
    assert not doc_path(tmp_path, "core", "x").exists()
    write_result(tmp_path, DocResult(pkg="core", file="core/x.go", stem="x", status="too-large"))
    assert not doc_path(tmp_path, "core", "x").exists()


def test_codegen_lists_taken_names_and_helper_rule():
    system, user = codegen_messages("calc/calc.go", CALC, CALC_DOC, "func Exported() int\n",
                                    [Import("strings")],
                                    taken={"contains", "isValid", "Exported", "Stats"})
    assert "cdg" in system
    assert "never declare these)\ncontains, isValid\n" in user
    _, plain = codegen_messages("calc/calc.go", CALC, CALC_DOC, "", [Import("strings")])
    assert "already declared in this package (never" not in plain


def test_codegen_max_tokens_grows_with_the_file():
    assert codegen_max_tokens("x" * 1000, 16000) == 16000
    assert codegen_max_tokens("x" * 79529, 16000) == int(79529 / 2.5) + 8000


def test_refine_messages_differ_per_round():
    ev = DocEval(signal="tests", build_ok=True, phi=2 / 3, relevant=["TestA", "TestB", "TestC"],
                 passed=["TestA", "TestB"], failing={"TestC": "want 1"})
    worse = DocEval(signal="tests", build_ok=True, phi=1 / 3, relevant=ev.relevant,
                    passed=["TestA"], failing={"TestB": "boom", "TestC": "want 1"})
    args = ("calc/calc.go", CALC, CALC_SRC, CALC_DOC, CALC_SRC, ev, [])
    _, r2 = refine_messages(*args, attempt=2)
    _, r3 = refine_messages(*args, last=worse, last_round=2, attempt=3)
    assert r2 != r3 and r2.startswith("Round 2.") and r3.startswith("Round 3.")
    assert "## Last attempt (round 2)" in r3 and "#### TestB" in r3
    assert "Last attempt" not in r2
    _, legacy = refine_messages(*args)
    assert legacy.startswith("Refine the documentation")


@needs_go
async def test_non_improving_doc_round_is_not_replayed(mod: Path, tmp_path: Path, monkeypatch):
    from codocs.llm import LLM
    from codocs.repowiki.llm.client import LLMClient

    real: list[str] = []
    kinds = {load_prompt(SEED): "seed", load_prompt(CODEGEN): "codegen",
             load_prompt(REFINE): "refine"}

    async def fake_complete(client, messages, **kw):
        kind = kinds[messages[0]["content"]]
        real.append(kind)
        return _go(CALC_BAD_CLAMP) if kind == "codegen" else CALC_DOC

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    settings = _settings(api_key="k", cache_path=str(tmp_path / "cache.db"))
    out = tmp_path / "out"
    async with LLM(settings) as llm:
        loop = SpecForgeLoop(GoRunner(mod, work_dir=tmp_path / "work", workers=4), llm,
                             settings, out, mod)
        res = await loop.run_file(_target("calc", "calc/calc.go"), CALC, CALC_BASE)
    assert res.phi_trajectory == [pytest.approx(2 / 3)] * 3
    assert real.count("refine") == 2
    rd = out / "docgen" / "calc" / "calc"
    p2, p3 = ((rd / f"round_{r}" / "prompt_doc.txt").read_text() for r in (2, 3))
    assert p2 != p3 and "## Last attempt (round 2)" in p3


CGO_SRC = """package cg

/*
static int twice(int x) { return 2 * x; }
*/
import "C"

// Twice doubles x in C.
func Twice(x int) int {
\treturn int(C.twice(C.int(x)))
}
"""

CGO_DOC = """# cg/c.go

**Package**: `cg` (`example.com/fixture/cg`)

Doubles numbers through cgo.

## FunctionDef Twice
**Twice**: doubles x by calling the C function `twice`.

**Signature**: `func Twice(x int) int`
"""

CGO_REGEN = """package cg

import "C"

func Twice(x int) int {
\treturn int(C.twice(C.int(x)))
}
"""


@needs_go
@pytest.mark.skipif(shutil.which("cc") is None, reason="cgo needs a C compiler")
async def test_cgo_preamble_reaches_the_regenerated_file(mod: Path, tmp_path: Path):
    (mod / "cg").mkdir()
    (mod / "cg" / "c.go").write_text(CGO_SRC)
    cg = PackageInfo(rel="cg", import_path="example.com/fixture/cg", name="cg",
                     go_files=["cg/c.go"])
    out = tmp_path / "out"
    llm = FakeLLM(seed=[CGO_DOC], code=[_go(CGO_REGEN)])
    res = await _loop(mod, out, llm).run_file(_target("cg", "cg/c.go"), cg,
                                              Baseline(pkg="cg", build_ok=True))
    assert res.signal == "compile" and res.phi_trajectory == [1.0]
    assert "static int twice" in res.rounds[0].code
    assert "cgo" in llm.calls[0]["user"]
    code_prompt = llm.calls[1]["user"]
    assert "## cgo" in code_prompt and "static int twice" not in code_prompt
    assert '"C"' not in code_prompt.split("## Allowed imports")[1].split("## Package context")[0]
