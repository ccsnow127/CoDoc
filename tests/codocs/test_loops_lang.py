"""docgen/testgen/baseline loops on the non-Go fixtures, with a scripted llm (no network)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

from codocs import pipeline as pl
from codocs.baseline import Baseline, package_baseline
from codocs.config import Settings
from codocs.golang.package import PackageInfo
from codocs.lang.base import LangTestResult, RepoContext
from codocs.lang.registry import adapter
from codocs.specforge.docfmt import documented_sections, expected_sections, missing_sections
from codocs.specforge.loop import DocResult, delivered_suite_path, run_docgen
from codocs.specforge.prompts import CODEGEN, REFINE, SEED, load_prompt
from codocs.tesearch.loop import SuiteResult, run_testgen
from codocs.tesearch.measure import Measurement, measure
from codocs.tesearch.prompts import refine_system, seed_system

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CC = shutil.which("cc")
CXX = shutil.which("c++")
GCOV = shutil.which("gcov")
needs_cc = pytest.mark.skipif(not (CC and GCOV), reason="cc/gcov not installed")
needs_cxx = pytest.mark.skipif(not (CXX and GCOV), reason="c++/gcov not installed")
needs_java = pytest.mark.skipif(not (shutil.which("mvn") and shutil.which("javac")),
                                reason="javac/mvn not installed")

EXT_LANG = {".py": "python", ".java": "java", ".c": "c", ".cpp": "cpp", ".cc": "cpp"}

PY_SUITES = {
    "src/pymod/calc.py": '''import pytest

from pymod.calc import clamp, normalize, total


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

C_SUITES = {
    "calc/calc.c": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(clamp_bounds) {
    CHECK_EQ_INT(clamp(5, 0, 10), 5);
    CHECK_EQ_INT(clamp(-1, 0, 10), 0);
    CHECK_EQ_INT(clamp(11, 0, 10), 10);
}

CODOCS_TEST(sum_values) {
    int v[] = {1, 2, 3};
    int err = -1;
    CHECK_EQ_INT(sum(v, 3, &err), 6);
    CHECK_EQ_INT(err, CALC_OK);
    CHECK_EQ_INT(sum(v, 0, &err), 0);
    CHECK_EQ_INT(err, CALC_EMPTY);
    CHECK_EQ_INT(sum(v, 0, NULL), 0);
    CHECK_EQ_INT(sum(v, 1, NULL), 1);
}

CODOCS_TEST(normalize_text) {
    char buf[32];
    CHECK_EQ_INT(normalize("  Hello World ", buf, sizeof buf), 11);
    CHECK_EQ_STR(buf, "hello-world");
    CHECK(is_space('\\t'));
}

CODOCS_TEST(wrong) {
    CHECK_EQ_INT(clamp(1, 2, 3), 1);
}
''',
    "shapes/shapes.c": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(area) {
    rect_t r = {2, 3};
    rect_t z = {0, 3};
    rect_t n = {3, -1};
    CHECK_EQ_INT(rect_area(r), 6);
    CHECK_EQ_INT(rect_area(z), 0);
    CHECK_EQ_INT(rect_area(n), 0);
}

CODOCS_TEST(classify_all) {
    rect_t e = {0, 1};
    rect_t s = {2, 2};
    rect_t r = {2, 3};
    CHECK_EQ_STR(classify(e), "empty");
    CHECK_EQ_STR(classify(s), "square");
    CHECK_EQ_STR(classify(r), "rect");
}
''',
}

CPP_SUITES = {
    "util/strutil.cpp": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(trim_spaces) {
    CHECK_EQ_STR(util::trim("  a b\\t").c_str(), "a b");
    CHECK_EQ_STR(util::trim("").c_str(), "");
}

CODOCS_TEST(split_parts) {
    std::vector<std::string> p = util::split("a,b,,c", ',');
    CHECK_EQ_INT((long long)p.size(), 4);
    CHECK_EQ_STR(p[2].c_str(), "");
    CHECK_EQ_INT((long long)util::split("", ',').size(), 1);
}

CODOCS_TEST(join_parts) {
    CHECK_EQ_STR(util::join({"a", "b", "c"}, "-").c_str(), "a-b-c");
    CHECK_EQ_STR(util::join({}, "-").c_str(), "");
}
''',
}

JAVA_SUITES = {
    "src/main/java/com/example/calc/Calc.java": '''package com.example.calc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class AnyName {
    @Test
    void clamp() {
        assertEquals(5, Calc.clamp(5, 0, 10));
        assertEquals(0, Calc.clamp(-1, 0, 10));
        assertEquals(10, Calc.clamp(11, 0, 10));
    }

    @Test
    void sum() {
        assertEquals(6, Calc.sum(new int[] {1, 2, 3}));
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> Calc.sum(new int[0]));
        assertEquals("calc: empty input", e.getMessage());
        assertThrows(IllegalArgumentException.class, () -> Calc.sum(null));
    }

    @Test
    void normalize() {
        assertEquals("hello-world", Calc.normalize("  Hello World "));
    }

    @Test
    void wrong() {
        assertEquals(1, Calc.clamp(1, 2, 3));
    }
}
''',
    "src/main/java/com/example/shapes/Rect.java": '''package com.example.shapes;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class RectCodocsTest {
    @Test
    void area() {
        assertEquals(6, new Rect(2, 3).area());
    }

    @Test
    void name() {
        assertEquals("square", new Rect(2, 2).name());
        assertEquals("rect", new Rect(2, 3).name());
    }
}
''',
}

_HEADER_RE = re.compile(r"```markdown\n(.*?)\n```", re.DOTALL)


def lang_of_rel(rel: str) -> str:
    return EXT_LANG[Path(rel).suffix]


def build_doc(user: str, src: str, lang: str) -> str:
    """a complete doc: the header lines the prompt asks for, then every expected section."""
    m = _HEADER_RE.search(user)
    header = m.group(1) if m else "# file"
    parts = [header, "", "What this file does."]
    for kind, name in expected_sections(src, lang):
        parts += ["", f"## {kind} {name}", f"**{name}**: does its documented job."]
    return "\n".join(parts) + "\n"


class ScriptedLLM:
    """replies keyed on the system prompt, as the Go e2e does; target = tag.split()[1].

    codegen: rel -> list of replies used in order (default: the original source).
    """

    def __init__(self, repo: Path, suites: dict[str, str], codegen: dict | None = None):
        self.repo = Path(repo)
        self.suites = suites
        self.codegen = {k: list(v) for k, v in (codegen or {}).items()}
        self.log: list[tuple[str, str, str]] = []
        self.calls = 0
        self.cache_hits = 0
        self.tokens = {"input": 0, "output": 0}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None):
        rel = tag.split()[1]
        lang = lang_of_rel(rel)
        src = (self.repo / rel).read_text(encoding="utf-8", errors="surrogateescape")
        fence = adapter(lang).prompts.fence
        self.calls += 1
        self.tokens["input"] += 10
        self.tokens["output"] += 1
        if system in (load_prompt(SEED, lang), load_prompt(REFINE, lang)):
            kind = "doc"
            out = build_doc(user, src, lang)
            if system == load_prompt(REFINE, lang):
                out = f"<documentation>\n{out}</documentation>"
        elif system == load_prompt(CODEGEN, lang):
            kind = "code"
            queue = self.codegen.get(rel)
            code = queue.pop(0) if queue else src
            out = f"```{fence}\n{code}```\n"
        elif system in (seed_system(lang), refine_system(lang)):
            kind = "suite"
            out = f"```{fence}\n{self.suites[rel]}```\n"
        else:
            raise AssertionError(f"unexpected system prompt for {tag!r}")
        self.log.append((kind, rel, user))
        return out


def copy_fixture(name: str, tmp_path: Path) -> Path:
    dst = tmp_path / "repo" / name
    shutil.copytree(FIXTURES / name, dst,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def settings(**kw) -> Settings:
    base = dict(mutants=4, test_rounds=2, doc_rounds=2, concurrency=2, workers=4,
                go_timeout=120)
    base.update(kw)
    return Settings(**base)


async def prepare(repo: Path, out: Path, s: Settings, lang: str):
    runners = pl.make_runners(repo, s, out, [lang])
    analysis = await pl.analyze_all(runners, s, out, pl.Selection())
    return runners[lang], analysis.part_for_loops(lang)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# -- docfmt ------------------------------------------------------------------------------


def test_docfmt_per_language():
    src = (FIXTURES / "cppmod/geom/vec.cpp").read_text(encoding="utf-8")
    want = expected_sections(src, "cpp")
    assert ("MethodDef", "Vec2.add") in want and ("MethodDef", "Vec2.operator==") in want
    doc = ("# geom/vec.cpp\n\n## MethodDef Vec2::Vec2(double x, double y)\n**x**: y.\n"
           "## MethodDef Vec2::add(const Vec2 &o)\n## MethodDef `Vec2.dot`\n"
           "## MethodDef Vec2.norm()\n## MethodDef Vec2::operator==\n")
    assert missing_sections(doc, src, "cpp") == []
    assert ("MethodDef", "Vec2.add") in documented_sections(doc, "cpp")
    # the Go default: `::` is not a separator there
    assert ("MethodDef", "Vec2::add") in documented_sections(doc)
    py = (FIXTURES / "pymod/src/pymod/shapes.py").read_text(encoding="utf-8")
    assert expected_sections(py, "python")[:2] == [("ClassDef", "Rect"),
                                                   ("MethodDef", "Rect.__init__")]
    assert missing_sections("## ClassDef Rect\n", py, "python")[0] == "MethodDef Rect.__init__"


# -- python ------------------------------------------------------------------------------


def test_python_testgen_then_docgen(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    before = snapshot(repo)
    out = tmp_path / "out"
    s = settings()
    runner, part = asyncio.run(prepare(repo, out, s, "python"))
    targets = {t.file: t for t in part.targets}
    assert set(targets) == {"src/pymod/calc.py", "src/pymod/shapes.py", "src/pymod/stats.py"}
    llm = ScriptedLLM(repo, PY_SUITES)

    suites = asyncio.run(run_testgen(
        [targets["src/pymod/calc.py"], targets["src/pymod/shapes.py"]], part.pkgs, runner, llm,
        s, out, repo))
    by = {r.file: r for r in suites}
    calc = by["src/pymod/calc.py"]
    assert calc.status == "ok" and calc.lang == "python"
    assert calc.best_size == 3 and calc.best_cov == pytest.approx(1.0)
    m = calc.rounds[0].measurement
    assert set(m.failing) == {"src/pymod/test_calc_codocs.py::test_wrong"}
    assert m.mutants_valid > 0 and m.mut is not None and m.killed > 0
    delivered = out / "tests" / "src/pymod" / "test_calc_codocs.py"
    assert delivered.is_file() and "test_wrong" not in delivered.read_text()
    rdir = out / "testgen" / "src/pymod" / "calc.py" / "round_1"
    assert (rdir / "suite.py").is_file() and (rdir / "green.py").is_file()
    prompt = (rdir / "prompt.txt").read_text()
    assert "from pymod.calc import ..." in prompt and seed_system("python") in prompt
    saved = _json(out / "testgen" / "src/pymod" / "calc.py" / "result.json")
    assert saved["lang"] == "python" and "cov_note" not in saved
    assert SuiteResult.from_dict(saved).lang == "python"
    assert by["src/pymod/shapes.py"].best_size == 3

    docs = asyncio.run(run_docgen(part.targets, part.pkgs, runner, llm, s, out, repo))
    by = {r.file: r for r in docs}
    assert by["src/pymod/calc.py"].signal == "tests"
    assert by["src/pymod/stats.py"].signal == "tests"
    assert by["src/pymod/shapes.py"].signal == "generated-tests"
    for r in docs:
        assert r.status == "ok" and r.best_phi == 1.0 and r.early_stop, r.to_dict()
        assert r.rounds[0].missing == [] and r.lang == "python"
    doc = (out / "docs" / "src/pymod" / "markdown_docs" / "calc.py.md").read_text()
    assert doc.startswith("# src/pymod/calc.py\n\n**Module**: `pymod.calc`")
    rd = out / "docgen" / "src/pymod" / "calc.py" / "round_1"
    assert (rd / "code.py").is_file() and not (rd / "code.go").exists()
    assert load_prompt(SEED, "python") in (rd / "prompt_doc.txt").read_text()
    assert _json(out / "docgen" / "src/pymod" / "calc.py" / "result.json")["lang"] == "python"
    base = _json(out / "baseline" / "python" / "src__pymod.json")
    assert base["failing"] == ["tests/test_calc.py::test_known_broken"]
    assert "tests/test_calc.py::test_clamp" in base["declared"]
    assert snapshot(repo) == before


def test_python_docgen_refines_after_a_failing_round(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = tmp_path / "out"
    s = settings(doc_rounds=3)
    runner, part = asyncio.run(prepare(repo, out, s, "python"))
    target = next(t for t in part.targets if t.file == "src/pymod/calc.py")
    src = (repo / target.file).read_text()
    broken = src.replace("return lo\n", "return hi\n", 1)
    llm = ScriptedLLM(repo, PY_SUITES, codegen={target.file: [broken]})
    [res] = asyncio.run(run_docgen([target], part.pkgs, runner, llm, s, out, repo))
    assert res.signal == "tests" and len(res.rounds) == 2
    assert res.rounds[0].eval.phi < 1.0 and "tests/test_calc.py::test_clamp" in \
        res.rounds[0].eval.failing
    assert res.best_round == 2 and res.best_phi == 1.0
    refine = [u for k, r, u in llm.log if k == "doc"][1]
    assert refine.startswith("Round 2. Refine the documentation of the Python module")
    assert "```python" in refine and "tests/test_calc.py::test_clamp" in refine


def test_python_baseline_files(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = tmp_path / "out"
    s = settings()
    runner, part = asyncio.run(prepare(repo, out, s, "python"))
    unit = part.pkgs["src/pymod"]
    base = asyncio.run(package_baseline(runner, unit, out))
    path = out / "baseline" / "python" / "src__pymod.json"
    d = _json(path)
    assert d["lang"] == "python" and d["build_ok"] and "notes" in d
    assert set(d["declared"]) == set(d["passing"]) | set(d["failing"])
    assert Baseline.from_dict(d).declared == base.declared

    async def forbidden(*a, **k):
        raise AssertionError("cached baseline must not rerun")

    runner.test = forbidden
    assert asyncio.run(package_baseline(runner, unit, out)).passing == base.passing
    # Go baselines have no lang, notes or declared keys
    go = Baseline(pkg="calc", build_ok=True, passing=["TestA"])
    assert set(go.to_dict()) == {"pkg", "build_ok", "passing", "failing", "build_errors",
                                 "seconds", "timed_out", "incomplete", "runs"}


# -- measure: coverage rules for non-Go ------------------------------------------------------


class _FakeRunner:
    def __init__(self, ad, root: Path, coverage, error: str = ""):
        self.adapter = ad
        self.lang = ad.name
        self.module_root = root
        self.timeout = 60
        self.coverage = coverage
        self.error = error

    async def test(self, unit_rel, *, overlay=None, run=None, cover=False, failfast=False,
                   compile_only=False, timeout=None):
        suite = next(v for k, v in (overlay or {}).items() if v and "codocs" in k)
        names = self.adapter.test_names(suite, "src/pymod/test_calc_codocs.py")
        return LangTestResult(build_ok=True, tests={n: "pass" for n in names},
                              coverage=self.coverage if cover else None,
                              coverage_error=self.error if cover else "")


@pytest.mark.parametrize("coverage,error,cov,note", [
    (None, "JaCoCo 0.8.12 cannot instrument class-file release 27", 1.0,
     "coverage unavailable: JaCoCo 0.8.12 cannot instrument class-file release 27"),
    ([], "", 0.0, ""),
])
def test_measure_coverage_rules(coverage, error, cov, note):
    ctx = RepoContext.build(FIXTURES / "pymod", ["python"])
    ad = adapter("python", ctx)
    unit = ad.unit("src/pymod")
    runner = _FakeRunner(ad, ctx.root, coverage, error)
    m = asyncio.run(measure(runner, unit, "src/pymod/calc.py", PY_SUITES["src/pymod/calc.py"],
                            []))
    assert m.build_ok and m.green_size == 4
    assert m.cov == cov and m.cov_note == note and m.uncovered == []
    d = m.to_dict()
    assert ("cov_note" in d) == bool(note)
    assert Measurement.from_dict(d).cov_note == note


def test_go_results_serialize_as_before():
    d = DocResult(pkg="calc", file="calc/calc.go", stem="calc", status="ok").to_dict()
    assert "lang" not in d and list(d)[:4] == ["pkg", "file", "stem", "status"]
    t = SuiteResult(pkg="calc", file="calc/calc.go", stem="calc", status="ok").to_dict()
    assert "lang" not in t and "cov_note" not in t
    assert "cov_note" not in Measurement(build_ok=True).to_dict()
    assert DocResult.from_dict(d).lang == "go"
    assert delivered_suite_path(Path("o"), "calc", "calc") == Path(
        "o/tests/calc/calc_codocs_test.go")
    assert delivered_suite_path(Path("o"), "", "x.py", basename="test_x_codocs.py") == Path(
        "o/tests/_root/test_x_codocs.py")
    assert PackageInfo(rel="", import_path="", name="").dir_name == "_root"


# -- c / c++ -------------------------------------------------------------------------------


@needs_cc
def test_c_testgen_then_docgen(tmp_path: Path):
    repo = copy_fixture("cmod", tmp_path)
    before = snapshot(repo)
    out = tmp_path / "out"
    s = settings(mutants=3)
    runner, part = asyncio.run(prepare(repo, out, s, "c"))
    targets = {t.file: t for t in part.targets}
    assert "calc/calc.h" not in targets          # headers are not passed to the loops
    llm = ScriptedLLM(repo, C_SUITES)
    picked = [targets["calc/calc.c"], targets["shapes/shapes.c"]]
    suites = {r.file: r for r in asyncio.run(run_testgen(picked, part.pkgs, runner, llm, s, out,
                                                         repo))}
    calc = suites["calc/calc.c"]
    assert calc.status == "ok" and calc.lang == "c" and calc.best_size == 3
    assert "calc/calc_codocs_test.c::wrong" in calc.rounds[0].measurement.failing
    assert calc.best_cov > 0.9
    assert (out / "tests" / "calc" / "calc_codocs_test.c").is_file()
    assert {p.name for p in (out / "tests" / "_support").iterdir()} >= {
        "codocs_test.h", "codocs_main.c", "codocs_main.cpp"}
    assert (out / "testgen" / "calc" / "calc.c" / "round_1" / "suite.c").is_file()
    assert suites["shapes/shapes.c"].best_size == 2

    docs = {r.file: r for r in asyncio.run(run_docgen(picked, part.pkgs, runner, llm, s, out,
                                                      repo))}
    assert docs["calc/calc.c"].signal == "tests"
    assert docs["shapes/shapes.c"].signal == "generated-tests"
    for r in docs.values():
        assert r.best_phi == 1.0 and r.rounds[0].missing == [], r.to_dict()
    doc = (out / "docs" / "calc" / "markdown_docs" / "calc.c.md").read_text()
    assert doc.startswith("# calc/calc.c\n\n**Unit**: `calc` (C)")
    code = [u for k, r, u in llm.log if k == "code" and r == "calc/calc.c"][0]
    assert "## Include preamble" in code and '#include "calc.h"' in code
    assert (out / "docgen" / "calc" / "calc.c" / "round_1" / "code.c").is_file()
    assert snapshot(repo) == before


@needs_cxx
def test_cpp_testgen_then_docgen(tmp_path: Path):
    repo = copy_fixture("cppmod", tmp_path)
    out = tmp_path / "out"
    s = settings(mutants=2)
    runner, part = asyncio.run(prepare(repo, out, s, "cpp"))
    targets = {t.file: t for t in part.targets}
    llm = ScriptedLLM(repo, CPP_SUITES)
    [suite] = asyncio.run(run_testgen([targets["util/strutil.cpp"]], part.pkgs, runner, llm, s,
                                      out, repo))
    assert suite.status == "ok" and suite.best_size == 3 and suite.lang == "cpp"
    assert (out / "tests" / "util" / "strutil_codocs_test.cpp").is_file()
    docs = asyncio.run(run_docgen([targets["util/strutil.cpp"], targets["geom/vec.cpp"]],
                                  part.pkgs, runner, llm, s, out, repo))
    by = {r.file: r for r in docs}
    assert by["util/strutil.cpp"].signal == "generated-tests"
    assert by["geom/vec.cpp"].signal == "tests"
    assert all(r.best_phi == 1.0 for r in docs), [r.to_dict() for r in docs]
    doc = (out / "docs" / "geom" / "markdown_docs" / "vec.cpp.md").read_text()
    assert "**Unit**: `geom` (C++)" in doc and "## MethodDef Vec2.add" in doc


# -- java ------------------------------------------------------------------------------------


@pytest.mark.slow
@needs_java
def test_java_testgen_then_docgen(tmp_path: Path):
    repo = copy_fixture("javamod", tmp_path)
    out = tmp_path / "out"
    s = settings(mutants=2, test_rounds=1, doc_rounds=1)
    runner, part = asyncio.run(prepare(repo, out, s, "java"))
    if not runner.available():
        pytest.skip(runner.unavailable_reason())
    targets = {t.file: t for t in part.targets}
    calc_rel = "src/main/java/com/example/calc/Calc.java"
    rect_rel = "src/main/java/com/example/shapes/Rect.java"
    llm = ScriptedLLM(repo, JAVA_SUITES)
    suites = {r.file: r for r in asyncio.run(run_testgen(
        [targets[calc_rel], targets[rect_rel]], part.pkgs, runner, llm, s, out, repo))}
    calc = suites[calc_rel]
    if calc.rounds and not calc.rounds[0].measurement.build_ok:
        errs = calc.rounds[0].measurement.build_errors
        if any("resolve" in e or "offline" in e for e in errs):
            pytest.skip(f"java tool jars unavailable: {errs[:1]}")
    assert calc.status == "ok" and calc.best_size == 3, calc.to_dict()
    assert "com.example.calc.CalcCodocsTest#wrong" in calc.rounds[0].measurement.failing
    delivered = out / "tests" / "src/main/java/com/example/calc" / "CalcCodocsTest.java"
    assert delivered.is_file() and "class CalcCodocsTest" in delivered.read_text()
    docs = {r.file: r for r in asyncio.run(run_docgen(
        [targets[calc_rel], targets[rect_rel]], part.pkgs, runner, llm, s, out, repo))}
    assert docs[calc_rel].signal == "tests" and docs[calc_rel].best_phi == 1.0
    assert docs[rect_rel].signal == "generated-tests" and docs[rect_rel].best_phi == 1.0
    doc = (out / "docs" / "src/main/java/com/example/calc" / "markdown_docs" /
           "Calc.java.md").read_text()
    assert "**Package**: `com.example.calc` (module `javamod`)" in doc
