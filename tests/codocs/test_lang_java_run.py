"""JavaRunner against the real toolchain (javac, JUnit Platform, JaCoCo, mvn) on javamod."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from codocs.golang.gotool import file_coverage
from codocs.golang.mutate import apply
from codocs.lang import treesitter as ts
from codocs.lang.base import LangTestResult, RepoContext
from codocs.lang.javarun import NOT_STARTED, JavaRunner, select_jdk
from codocs.lang.registry import adapter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "javamod"
CALC = "src/main/java/com/example/calc"
SHAPES = "src/main/java/com/example/shapes"
TEST_DIR = "src/test/java/com/example/calc"
CALC_TESTS = [f"com.example.calc.CalcTest#{n}" for n in ("clamp", "sum", "normalize",
                                                          "knownBroken")]
STATS_TESTS = ["com.example.calc.StatsTest#add", "com.example.calc.StatsTest#mean"]

pytestmark = [
    pytest.mark.skipif(not ts.available(), reason="tree-sitter not installed"),
    pytest.mark.skipif(select_jdk("", shutil.which("mvn"))[0] is None,
                       reason="no usable JDK (javac and java)"),
    pytest.mark.skipif(shutil.which("mvn") is None, reason="mvn not installed"),
]


def _digest(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _make(root: Path, out: Path, **settings) -> JavaRunner:
    s = SimpleNamespace(test_timeout=60, go_timeout=300, **settings)
    ctx = RepoContext.build(root, ["java"], s, out)
    return JavaRunner(root, adapter=adapter("java", ctx), settings=s, out_dir=out)


async def _ready(runner: JavaRunner, unit: str = CALC) -> None:
    prep = await runner.prepare(runner.adapter.unit(unit))
    if not prep.ok and any("cannot resolve the Java tool jars" in e for e in prep.errors):
        pytest.skip("Java tool jars cannot be resolved offline or online: " + prep.errors[0])
    assert prep.ok, prep.errors


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("jrun")
    repo = base / "javamod"
    shutil.copytree(FIXTURE, repo)
    before = _digest(repo)
    runner = _make(repo, base / "out")
    yield SimpleNamespace(repo=repo, out=base / "out", runner=runner, before=before)
    assert _digest(repo) == before, "the runner wrote into the repository"


def _src(env, rel: str) -> str:
    return (env.repo / rel).read_text(encoding="utf-8")


def _suite(body: str, name: str = "CalcCodocsTest", imports: str = "") -> str:
    return ("package com.example.calc;\n\nimport static org.junit.jupiter.api.Assertions.*;\n"
            "import org.junit.jupiter.api.*;\n" + imports + f"\nclass {name} {{\n{body}\n}}\n")


# -- toolchain ---------------------------------------------------------------------------


async def test_available_and_version(env):
    r = env.runner
    assert r.available() and r.lang == "java" and r.adapter.name == "java"
    assert r.version().startswith("Java ") and "JaCoCo" in r.version()
    assert r.module_root == env.repo.resolve() and r.module == "javamod"


def test_unavailable_jdk(tmp_path):
    r = _make(FIXTURE, tmp_path / "out", java_home=str(tmp_path / "nojdk"))
    assert not r.available()
    assert "no usable JDK" in r.unavailable_reason()
    res = asyncio.run(r.test(CALC))
    assert not res.build_ok and "no usable JDK" in res.build_errors[0]


# -- baseline and discovery ----------------------------------------------------------------


async def test_baseline_run(env):
    await _ready(env.runner)
    res = await env.runner.test(CALC)
    assert isinstance(res, LangTestResult) and res.build_ok and not res.timed_out
    assert res.tests == {**{n: "pass" for n in CALC_TESTS + STATS_TESTS},
                         "com.example.calc.CalcTest#knownBroken": "fail"}
    assert "known broken" in res.output["com.example.calc.CalcTest#knownBroken"]
    assert res.coverage is None and res.returncode == 1


async def test_list_tests(env):
    await _ready(env.runner)
    assert await env.runner.list_tests(CALC) == sorted(CALC_TESTS + STATS_TESTS)
    assert await env.runner.list_tests(SHAPES) == []
    assert await env.runner.list_tests("nowhere") is None


async def test_unknown_unit_and_empty_selection(env):
    res = await env.runner.test("src/main/java/nowhere")
    assert not res.build_ok and "no Java unit" in res.build_errors[0]
    res = await env.runner.test(CALC, run=[])
    assert res.build_ok and res.tests == {}
    res = await env.runner.test(CALC, run=["com.example.calc.Gone#x",
                                           "com.example.calc.CalcTest#nope",
                                           "com.example.calc.CalcTest#clamp"])
    assert res.build_ok and res.tests == {"com.example.calc.CalcTest#clamp": "pass"}


# -- overlays -----------------------------------------------------------------------------


async def test_replaced_broken_file_reports_original_path(env):
    await _ready(env.runner)
    bad = _src(env, f"{CALC}/Calc.java").replace("return total;", "return totl;")
    res = await env.runner.test(CALC, overlay={f"{CALC}/Calc.java": bad})
    assert not res.build_ok
    assert res.build_errors == [f"{CALC}/Calc.java:34: cannot find symbol "
                                "(symbol: variable totl; location: class Calc)"]
    res = await env.runner.test(CALC, overlay={f"{CALC}/Calc.java": bad}, compile_only=True)
    assert not res.build_ok and res.build_errors[0].startswith(f"{CALC}/Calc.java:34:")


async def test_changed_signature_breaks_dependents(env):
    await _ready(env.runner)
    renamed = _src(env, f"{CALC}/Calc.java").replace("SCALE", "FACTOR")
    res = await env.runner.test(CALC, overlay={f"{CALC}/Calc.java": renamed},
                                run=STATS_TESTS)
    assert not res.build_ok
    assert any(e.startswith(f"{CALC}/Stats.java:") and "FACTOR" not in e and "SCALE" in e
               for e in res.build_errors), res.build_errors


async def test_compile_only(env):
    await _ready(env.runner)
    good = _src(env, f"{CALC}/Calc.java").replace("v > hi", "v >= hi")
    res = await env.runner.test(CALC, overlay={f"{CALC}/Calc.java": good}, compile_only=True)
    assert res.build_ok and res.tests == {}
    res = await env.runner.test(CALC, compile_only=True)
    assert res.build_ok


async def test_hidden_tests_and_added_suite_with_coverage(env):
    """measure()'s overlay: the unit's tests hidden, the suite added, run=None, cover=True."""
    await _ready(env.runner)
    suite = _suite("    @Test void clampRange() { assertEquals(3, Calc.clamp(3, 0, 5)); }\n"
                   "    @Test void clampLow() { assertEquals(0, Calc.clamp(-2, 0, 5)); }\n"
                   "    @Test void wrong() { assertEquals(1, Calc.clamp(9, 0, 5)); }")
    unit = env.runner.adapter.unit(CALC)
    overlay = {f: None for f in unit.test_files}
    overlay[f"{TEST_DIR}/CalcCodocsTest.java"] = suite
    res = await env.runner.test(CALC, overlay=overlay, cover=True)
    assert res.build_ok, res.build_errors
    assert res.tests == {"com.example.calc.CalcCodocsTest#clampRange": "pass",
                         "com.example.calc.CalcCodocsTest#clampLow": "pass",
                         "com.example.calc.CalcCodocsTest#wrong": "fail"}
    assert res.coverage_error == "" and res.coverage
    assert {b.file for b in res.coverage} == {f"{CALC}/Calc.java", f"{CALC}/Stats.java"}
    calc = _src(env, f"{CALC}/Calc.java")
    cov, missed = file_coverage(res.coverage, f"{CALC}/Calc.java", calc)
    lines = calc.splitlines()
    assert 0.0 < cov < 1.0
    assert lines[16].strip() == "return lo;" and 17 not in missed
    assert any(lines[n - 1].strip() == "int total = 0;" for n in missed)
    # Stats was never loaded: 0% rather than missing
    stats_cov, stats_missed = file_coverage(res.coverage, f"{CALC}/Stats.java")
    assert stats_cov == 0.0 and stats_missed


async def test_suite_not_touching_target_gives_zero_coverage(env):
    await _ready(env.runner)
    suite = _suite("    @Test void stats() { assertEquals(0, new Stats().mean()); }",
                   name="StatsCodocsTest")
    unit = env.runner.adapter.unit(CALC)
    overlay = {f: None for f in unit.test_files}
    overlay[f"{TEST_DIR}/StatsCodocsTest.java"] = suite
    res = await env.runner.test(CALC, overlay=overlay, cover=True)
    assert res.tests == {"com.example.calc.StatsCodocsTest#stats": "pass"}
    assert file_coverage(res.coverage, f"{CALC}/Calc.java")[0] == 0.0
    assert file_coverage(res.coverage, f"{CALC}/Stats.java")[0] > 0.0


async def test_suite_build_error_maps_to_suite_rel(env):
    await _ready(env.runner)
    suite = _suite("    @Test void a() { List<Integer> x = List.of(1); assertEquals(1, x.size()); }")
    rel = f"{TEST_DIR}/CalcCodocsTest.java"
    res = await env.runner.test(CALC, overlay={rel: suite}, cover=True)
    assert not res.build_ok
    assert res.build_errors[0].startswith(f"{rel}:7: cannot find symbol (symbol: class List")
    ja = env.runner.adapter
    fixed = ja.fix_imports(suite, res.build_errors, "CalcCodocsTest.java",
                           ja.default_allowed(env.runner, ja.unit(CALC)))
    res = await env.runner.test(CALC, overlay={rel: fixed}, cover=True)
    assert res.build_ok and res.tests["com.example.calc.CalcCodocsTest#a"] == "pass"
    assert res.tests["com.example.calc.CalcTest#clamp"] == "pass"


async def test_hidden_class_replaced_by_same_name(env):
    """an added test class with the FQCN of a hidden one must not see the old class files."""
    await _ready(env.runner)
    new = ("package com.example.calc;\nimport org.junit.jupiter.api.Test;\n"
           "class StatsTest { @Test void fresh() {} }\n")
    rel = f"{TEST_DIR}/StatsTest.java"
    res = await env.runner.test(CALC, overlay={rel: new})
    assert res.build_ok
    assert "com.example.calc.StatsTest#fresh" in res.tests
    assert "com.example.calc.StatsTest#add" not in res.tests


async def test_parameterized_nested_and_container_failure(env):
    await _ready(env.runner)
    suite = _suite(
        "    @org.junit.jupiter.params.ParameterizedTest\n"
        "    @org.junit.jupiter.params.provider.ValueSource(ints = {1, 2, 30})\n"
        "    void param(int v) { assertTrue(v < 10); }\n"
        "    @Disabled @Test void off() {}\n"
        "    @Nested class Inner { @Test void deep() { assertEquals(1, 1); } }\n"
        "    @RepeatedTest(3) void rep() {}")
    broken = ("package com.example.calc;\nimport org.junit.jupiter.api.*;\n"
              "class BrokenSetupTest {\n  @BeforeAll static void boom() { throw new "
              "IllegalStateException(\"no setup\"); }\n  @Test void one() {}\n"
              "  @Test void two() {}\n}\n")
    overlay = {f"{TEST_DIR}/CalcCodocsTest.java": suite,
               f"{TEST_DIR}/BrokenSetupTest.java": broken}
    res = await env.runner.test(CALC, overlay=overlay, run=[
        "com.example.calc.CalcCodocsTest#param", "com.example.calc.CalcCodocsTest#off",
        "com.example.calc.CalcCodocsTest$Inner#deep", "com.example.calc.CalcCodocsTest#rep",
        "com.example.calc.BrokenSetupTest#one", "com.example.calc.BrokenSetupTest#two"])
    assert res.build_ok, res.build_errors
    assert res.tests == {"com.example.calc.CalcCodocsTest#param": "fail",
                         "com.example.calc.CalcCodocsTest#off": "skip",
                         "com.example.calc.CalcCodocsTest$Inner#deep": "pass",
                         "com.example.calc.CalcCodocsTest#rep": "pass",
                         "com.example.calc.BrokenSetupTest#one": "fail",
                         "com.example.calc.BrokenSetupTest#two": "fail"}
    assert "no setup" in res.output["com.example.calc.BrokenSetupTest#one"]
    res = await env.runner.test(CALC, overlay=overlay)
    assert res.tests["com.example.calc.CalcCodocsTest$Inner#deep"] == "pass"
    assert res.tests["com.example.calc.CalcTest#clamp"] == "pass"


async def test_crash_and_failfast(env):
    await _ready(env.runner)
    suite = _suite("    @Test void a1() {}\n"
                   "    @Test void b2() { Runtime.getRuntime().halt(7); }\n"
                   "    @Test void c3() {}",
                   imports="@TestMethodOrder(MethodOrderer.MethodName.class)\n")
    names = [f"com.example.calc.CalcCodocsTest#{n}" for n in ("a1", "b2", "c3")]
    res = await env.runner.test(CALC, overlay={f"{TEST_DIR}/CalcCodocsTest.java": suite},
                                run=names)
    assert res.build_ok and res.returncode == 7
    assert res.tests == {names[0]: "pass", names[1]: "fail", names[2]: "interrupted"}
    assert "exited during this test" in res.output[names[1]]
    assert "exited early" in env.runner.adapter.crash_message(res) or res.tail is not None
    ff = _suite("    @Test void a1() { fail(\"first\"); }\n    @Test void b2() {}",
                imports="@TestMethodOrder(MethodOrderer.MethodName.class)\n")
    res = await env.runner.test(CALC, overlay={f"{TEST_DIR}/CalcCodocsTest.java": ff},
                                run=names[:2], failfast=True)
    assert res.tests == {names[0]: "fail", names[1]: "interrupted"}


async def test_inner_timeout(env):
    await _ready(env.runner)
    suite = _suite("    @Test void a1() {}\n"
                   "    @Test void b2() { while (true) { Thread.onSpinWait(); } }\n"
                   "    @Test void c3() {}",
                   imports="@TestMethodOrder(MethodOrderer.MethodName.class)\n")
    names = [f"com.example.calc.CalcCodocsTest#{n}" for n in ("a1", "b2", "c3")]
    res = await env.runner.test(CALC, overlay={f"{TEST_DIR}/CalcCodocsTest.java": suite},
                                run=names, timeout=3)
    assert res.build_ok and res.timed_out and res.returncode == 124
    assert res.tests == {names[0]: "pass", names[1]: "fail", names[2]: "interrupted"}
    assert "timed out" in res.output[names[1]]


async def test_outer_timeout_before_start(env, monkeypatch):
    await _ready(env.runner)
    import codocs.lang.javarun as jr

    monkeypatch.setattr(jr, "OUTER_SLACK", -2)
    suite = _suite("    static { cdxSpin(); }\n"
                   "    static void cdxSpin() { while (true) { Thread.onSpinWait(); } }\n"
                   "    @Test void a() {}")
    res = await env.runner.test(CALC, overlay={f"{TEST_DIR}/CalcCodocsTest.java": suite},
                                run=["com.example.calc.CalcCodocsTest#a"], timeout=4)
    # the class initializer runs during discovery: killed before any test started
    assert res.timed_out and not res.build_ok and res.build_errors == [NOT_STARTED]


# -- mutants ----------------------------------------------------------------------------


async def test_mutants_valid_killed_and_survived(env):
    await _ready(env.runner)
    ja = env.runner.adapter
    stats = _src(env, f"{CALC}/Stats.java")
    mutants = ja.mutation_sites(stats)
    assert {m.operator for m in mutants} >= {"cmp: == -> !=", "logic: || -> &&",
                                             "incdec: ++ -> --", "assign: += -> -=",
                                             "arith: * -> /"}
    results = await asyncio.gather(*(env.runner.test(
        CALC, overlay={f"{CALC}/Stats.java": apply(stats, m)}, compile_only=True)
        for m in mutants))
    assert all(r.build_ok for r in results), [r.build_errors for r in results]

    def by_op(src: str, op: str, line_text: str):
        for m in ja.mutation_sites(src):
            if m.operator == op and line_text in src.splitlines()[m.line - 1]:
                return m
        raise AssertionError(op)

    killed = by_op(stats, "cmp: == -> !=", "if (count == 0) {")
    res = await env.runner.test(CALC, overlay={f"{CALC}/Stats.java": apply(stats, killed)},
                                run=STATS_TESTS, failfast=True, timeout=30)
    assert res.build_ok and "fail" in res.tests.values()
    calc = _src(env, f"{CALC}/Calc.java")
    survivor = by_op(calc, "cmp: > -> >=", "if (v > hi)")
    res = await env.runner.test(CALC, overlay={f"{CALC}/Calc.java": apply(calc, survivor)},
                                run=CALC_TESTS[:3], failfast=True, timeout=30)
    assert res.build_ok and set(res.tests.values()) == {"pass"}


# -- paths -------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="the /tmp symlink exists on macOS")
async def test_out_under_tmp_symlink(env):
    base = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        assert base.resolve() != base
        r = _make(env.repo, base / "out")
        await _ready(r)
        bad = _src(env, f"{CALC}/Calc.java").replace("return total;", "return totl;")
        res = await r.test(CALC, overlay={f"{CALC}/Calc.java": bad})
        assert res.build_errors[0].startswith(f"{CALC}/Calc.java:34:")
        res = await r.test(CALC, run=CALC_TESTS[:1], cover=False)
        assert res.tests == {CALC_TESTS[0]: "pass"}
    finally:
        shutil.rmtree(base, ignore_errors=True)


# -- plain mode, gradle -------------------------------------------------------------------


async def test_plain_layout(tmp_path):
    repo = tmp_path / "plain"
    (repo / "src/p").mkdir(parents=True)
    (repo / "src/p/Greeter.java").write_text(
        "package p;\npublic class Greeter {\n  public static String hi(String n) {\n"
        "    if (n.isEmpty()) {\n      return \"hi\";\n    }\n    return \"hi \" + n;\n  }\n}\n")
    (repo / "src/p/GreeterTest.java").write_text(
        "package p;\nimport org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n"
        "class GreeterTest {\n  @Test void hi() { assertEquals(\"hi bo\", Greeter.hi(\"bo\")); }\n"
        "  @Test void bad() { assertEquals(\"x\", Greeter.hi(\"\")); }\n}\n")
    before = _digest(repo)
    r = _make(repo, tmp_path / "out")
    unit = r.adapter.unit("src/p")
    assert unit.extra["mode"] == "plain" and unit.test_files == ["src/p/GreeterTest.java"]
    await _ready(r, "src/p")
    res = await r.test("src/p", cover=True)
    assert res.tests == {"p.GreeterTest#hi": "pass", "p.GreeterTest#bad": "fail"}
    cov, missed = file_coverage(res.coverage, "src/p/Greeter.java")
    assert cov == 0.75 and missed == [2]      # the implicit constructor never ran
    assert await r.list_tests("src/p") == ["p.GreeterTest#bad", "p.GreeterTest#hi"]
    assert _digest(repo) == before


async def test_gradle_unit_is_unsupported(tmp_path):
    repo = tmp_path / "g"
    (repo / "src/main/java/a").mkdir(parents=True)
    (repo / "build.gradle").write_text("plugins { id 'java' }\n")
    (repo / "src/main/java/a/A.java").write_text("package a;\nclass A { void f() {} }\n")
    r = _make(repo, tmp_path / "out")
    res = await r.test("src/main/java/a")
    assert not res.build_ok and "Gradle" in res.build_errors[0]


# -- mvn path ------------------------------------------------------------------------------


@pytest.mark.slow
async def test_mvn_path_baseline_and_coverage(tmp_path):
    repo = tmp_path / "javamod"
    shutil.copytree(FIXTURE, repo)
    before = _digest(repo)
    r = _make(repo, tmp_path / "out", java_runner="mvn")
    await _ready(r)
    res = await r.test(CALC)
    assert res.build_ok, res.build_errors
    assert res.tests == {**{n: "pass" for n in CALC_TESTS + STATS_TESTS},
                         "com.example.calc.CalcTest#knownBroken": "fail"}
    unit = r.adapter.unit(CALC)
    overlay = {f: None for f in unit.test_files}
    overlay[f"{TEST_DIR}/CalcCodocsTest.java"] = _suite(
        "    @Test void sum() { assertEquals(6, Calc.sum(new int[] {1, 2, 3})); }")
    res = await r.test(CALC, overlay=overlay, cover=True)
    assert res.tests == {"com.example.calc.CalcCodocsTest#sum": "pass"}
    cov, _ = file_coverage(res.coverage, f"{CALC}/Calc.java")
    assert 0.0 < cov < 1.0
    assert file_coverage(res.coverage, f"{CALC}/Stats.java")[0] == 0.0
    # non-coverage overlay runs (mutants) use the fast path
    res = await r.test(CALC, overlay=overlay, run=["com.example.calc.CalcCodocsTest#sum"],
                       failfast=True)
    assert res.tests == {"com.example.calc.CalcCodocsTest#sum": "pass"}
    assert _digest(repo) == before


# -- multi-module reactor, jacoco limits ----------------------------------------------------


def _pom(artifact: str, body: str = "", packaging: str = "jar", parent: bool = True) -> str:
    par = ("<parent><groupId>g</groupId><artifactId>root</artifactId><version>1</version>"
           "</parent>") if parent else "<groupId>g</groupId><version>1</version>"
    return ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0'
            f"</modelVersion>{par}<artifactId>{artifact}</artifactId>"
            f"<packaging>{packaging}</packaging>{body}</project>")


async def test_reactor_module_with_sibling_dependency(tmp_path):
    repo = tmp_path / "reactor"
    files = {
        "pom.xml": _pom("root", "<properties><maven.compiler.release>17"
                        "</maven.compiler.release></properties><modules><module>core</module>"
                        "<module>app</module></modules>", packaging="pom", parent=False),
        "core/pom.xml": _pom("core"),
        "core/src/main/java/c/Core.java":
            "package c;\npublic class Core {\n  public static int two() {\n    return 2;\n  }\n}\n",
        "app/pom.xml": _pom("app", "<dependencies><dependency><groupId>g</groupId>"
                            "<artifactId>core</artifactId><version>1</version></dependency>"
                            "<dependency><groupId>org.junit.jupiter</groupId><artifactId>"
                            "junit-jupiter</artifactId><version>5.10.2</version><scope>test"
                            "</scope></dependency></dependencies>"),
        "app/src/main/java/a/App.java":
            "package a;\npublic class App {\n  public static int four() {\n"
            "    return c.Core.two() * 2;\n  }\n}\n",
        "app/src/test/java/a/AppTest.java":
            "package a;\nimport org.junit.jupiter.api.Test;\n"
            "import static org.junit.jupiter.api.Assertions.assertEquals;\n"
            "class AppTest { @Test void four() { assertEquals(4, App.four()); } }\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    before = _digest(repo)
    r = _make(repo, tmp_path / "out")
    unit = r.adapter.unit("app/src/main/java/a")
    assert unit.extra["module"] == "app" and unit.extra["reactor"] == ""
    await _ready(r, "app/src/main/java/a")
    res = await r.test("app/src/main/java/a", cover=True)
    assert res.tests == {"a.AppTest#four": "pass"}
    assert file_coverage(res.coverage, "app/src/main/java/a/App.java") == (0.5, [2])
    app = files["app/src/main/java/a/App.java"]
    res = await r.test("app/src/main/java/a", overlay={
        "app/src/main/java/a/App.java": app.replace("* 2", "* 3")}, run=["a.AppTest#four"],
        failfast=True)
    assert res.tests == {"a.AppTest#four": "fail"}
    assert _digest(repo) == before


def test_jacoco_release_limit():
    from codocs.lang.javarun import ModulePrep

    assert ModulePrep(key="", release=17, jacoco="0.8.12").coverage_error == ""
    assert ModulePrep(key="", release=27, jacoco="0.8.12").coverage_error == \
        "JaCoCo 0.8.12 cannot instrument class-file release 27"
    r = JavaRunner.__new__(JavaRunner)
    r.jacoco_pin = ""
    assert [r._jacoco_for(n) for n in (11, 22, 23, 25, 30)] == \
        ["0.8.12", "0.8.12", "0.8.13", "0.8.14", "0.8.14"]
