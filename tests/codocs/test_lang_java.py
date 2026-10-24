"""Java adapter (static half): layout, parsing, tests in source, mutation, context, repair, facts."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from codocs.golang.mutate import apply
from codocs.lang import treesitter as ts
from codocs.lang.base import ImportSpec, LangTarget, RepoContext, Unit, prompt_spec
from codocs.lang.java import JavaAdapter, package_of, read_pom
from codocs.lang.javarun import (
    NOT_STARTED,
    JavaRunner,
    PathMap,
    _level_flags,
    _mirror_tree,
    _symlink_farm,
    encode_java,
    expand_argline,
    fold_events,
    jacoco_blocks,
    parse_effective_pom,
    parse_javac,
    parse_mvn_errors,
    parse_surefire,
    platform_version,
    select_jdk,
)
from codocs.lang.registry import adapter

pytestmark = pytest.mark.skipif(not ts.available(), reason="tree-sitter not installed")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "javamod"
CALC = "src/main/java/com/example/calc"
SHAPES = "src/main/java/com/example/shapes"

SAMPLE = '''/* header */
package com.example.demo;

import java.util.List;
import java.util.*;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static java.lang.Math.*;

/** The sample. */
@Deprecated
public final class Sample<T extends Number> extends Base implements Runnable {
    public static final int LIMIT = 3;
    private String label = "a" + "b";
    private boolean on = true;

    /** Builds one. */
    public Sample() {
        this(1);
    }

    Sample(int n) {
        count = n;
    }

    @Override
    public void run() {
        if (count < LIMIT && on) {
            count++;
        }
        String s = "x";
        s += "y";
        int k = count * 2 + 1;
        k -= 1;
        label = label + k;
        boolean f = false;
    }

    public int add(int a) { return a + count; }
    public int add(int a, int b) { return a - b; }

    abstract static class Inner {
        abstract void nobody();
        int twice(int x) { return x * 2; }
    }

    enum Mode { ON, OFF; boolean active() { return this == ON; } }

    record Pair(int a, int b) { int sum() { return a + b; } }

    interface Shape { double area(); }

    @SuppressWarnings(value = "x")
    private int count;
}

class Helper { static int one() { return 1; } }
'''

TEST_SRC = '''package com.example.calc;

import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class CalcExtraTest {
    private final int base = Calc.SCALE;

    @BeforeEach
    void setUp() {}

    /** Doc of a. */
    @Test
    void a() { Assertions.assertEquals(1, Calc.clamp(1, 0, 2)); }

    @ParameterizedTest
    @ValueSource(ints = {1, 2})
    void b(int v) { Assertions.assertTrue(v > 0); }

    @org.junit.Test
    public void legacy() {}

    void helper() {}

    @Nested
    class Inner {
        @Test void c() {}
        @Nested class Deeper { @RepeatedTest(2) void d() {} }
    }

    class NotNested { @Test void ignored() {} }

    public static void main(String[] args) { System.out.println("x"); }
}
'''


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("jstatic") / "javamod"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture(scope="module")
def ja(repo) -> JavaAdapter:
    ctx = RepoContext.build(repo, ["java"])
    return adapter("java", ctx)


@pytest.fixture(scope="module")
def units(ja) -> dict[str, Unit]:
    return asyncio.run(ja.discover(None))


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# -- registry, prompts ---------------------------------------------------------------


def test_registry_and_prompt_spec():
    a = adapter("java")
    assert isinstance(a, JavaAdapter) and a.ctx is None
    spec = prompt_spec("java")
    assert spec.fence == "java" and spec.comment == "//"
    assert spec.fragments["LANG"] == "Java" and spec.fragments["MARK"] == "//"
    assert "CodocsTest" not in spec.fragments["TEST_RULES"]
    assert set(spec.fragments) >= {"CODEGEN_RULES", "TEST_RULES", "SECTION_KINDS", "DOC_HEADER"}


# -- layout and discovery -------------------------------------------------------------


def test_discover_units(units):
    assert list(units) == [CALC, SHAPES]
    calc = units[CALC]
    assert calc.lang == "java" and calc.name == calc.import_path == "com.example.calc"
    assert calc.files == [f"{CALC}/Calc.java", f"{CALC}/Stats.java"]
    assert calc.test_files == ["src/test/java/com/example/calc/CalcTest.java",
                               "src/test/java/com/example/calc/StatsTest.java"]
    assert calc.build_root == ""
    assert calc.extra == {"module": "", "reactor": "", "source_root": "src/main/java",
                          "test_root": "src/test/java", "mode": "maven",
                          "artifact": "javamod", "encoding": "UTF-8"}
    assert units[SHAPES].test_files == []
    assert units[CALC].dir_name == CALC and units[CALC].go_files == units[CALC].files


def test_discover_subtree_and_packages(ja):
    assert list(asyncio.run(ja.discover(None, subtree=SHAPES))) == [SHAPES]
    assert list(asyncio.run(ja.discover(None, packages=[CALC]))) == [CALC]


def test_context_indexes(ja, units):
    assert ja.ctx.java_classes["com.example.calc.Calc"] == f"{CALC}/Calc.java"
    assert ja.ctx.java_classes["com.example.calc.CalcTest"].startswith("src/test/java")
    assert ja.ctx.java_modules[""]["mode"] == "maven"


def test_select_targets(ja, repo, units):
    targets = ja.select_targets(repo, units)
    assert all(isinstance(t, LangTarget) and t.lang == "java" for t in targets)
    assert [(t.file.rsplit("/", 1)[-1], t.stem, t.status) for t in targets] == [
        ("Calc.java", "Calc.java", "ok"), ("Stats.java", "Stats.java", "ok"),
        ("Rect.java", "Rect.java", "ok"), ("Shapes.java", "Shapes.java", "ok")]
    assert [t.stem for t in ja.select_targets(repo, units, max_files=1)] == ["Calc.java"]
    assert [t.file for t in ja.select_targets(repo, units, files="*Stats*")] == \
        [f"{CALC}/Stats.java"]


def test_layout_multimodule_plain_gradle_and_statuses(tmp_path):
    _write(tmp_path, "pom.xml", '<project xmlns="http://maven.apache.org/POM/4.0.0">'
           "<artifactId>root</artifactId><packaging>pom</packaging>"
           "<modules><module>core</module></modules></project>")
    _write(tmp_path, "core/pom.xml", "<project><artifactId>core</artifactId>"
           "<build><sourceDirectory>${project.basedir}/java</sourceDirectory></build>"
           "<dependencies><dependency><groupId>g</groupId><artifactId>a</artifactId>"
           "</dependency></dependencies></project>")
    _write(tmp_path, "core/java/c/Core.java", "package c;\npublic class Core { void f() {} }\n")
    _write(tmp_path, "core/java/c/package-info.java", "/** Core things. */\npackage c;\n")
    _write(tmp_path, "core/java/c/Gen.java",
           "// Code generated by protoc. DO NOT EDIT.\npackage c;\nclass Gen { void g() {} }\n")
    _write(tmp_path, "core/java/c/Iface.java", "package c;\ninterface Iface { void x(); }\n")
    _write(tmp_path, "core/src/test/java/c/CoreTest.java",
           "package c;\nclass CoreTest { @org.junit.jupiter.api.Test void t() {} }\n")
    _write(tmp_path, "tools/src/p/q/Tool.java", "package p.q;\nclass Tool { void m() {} }\n")
    _write(tmp_path, "tools/src/p/q/ToolTest.java",
           "package p.q;\nclass ToolTest { @org.junit.Test public void t() {} }\n")
    _write(tmp_path, "app/build.gradle", "plugins { id 'java' }\n")
    _write(tmp_path, "app/src/main/java/a/App.java", "package a;\nclass App { void r() {} }\n")
    ja = JavaAdapter(RepoContext.build(tmp_path, ["java"]))
    units = asyncio.run(ja.discover(None))
    assert sorted(units) == ["app/src/main/java/a", "core/java/c", "tools/src/p/q"]
    core = units["core/java/c"]
    assert core.extra["module"] == "core" and core.extra["reactor"] == ""
    assert core.extra["test_root"] == "core/src/test/java"
    assert core.test_files == ["core/src/test/java/c/CoreTest.java"]
    tools = units["tools/src/p/q"]
    assert tools.extra["mode"] == "plain" and tools.extra["source_root"] == "tools/src"
    assert tools.files == ["tools/src/p/q/Tool.java"]
    assert tools.test_files == ["tools/src/p/q/ToolTest.java"]
    assert units["app/src/main/java/a"].extra["mode"] == "gradle"
    status = {t.file.rsplit("/", 1)[-1]: t.status
              for t in ja.select_targets(tmp_path, units)}
    assert status == {"App.java": "unsupported-build", "Core.java": "ok",
                      "Gen.java": "generated", "Iface.java": "no-funcs",
                      "package-info.java": "no-decls", "Tool.java": "ok"}
    assert ja.allowed_third_party(tmp_path, core) == ["g:a"]
    assert ja.allowed_third_party(tmp_path, tools) == []
    assert ja.suite_rel(tools, "Tool.java") == "tools/src/p/q/ToolCodocsTest.java"
    assert ja.suite_rel(core, "Core.java") == "core/src/test/java/c/CoreCodocsTest.java"
    ctx = ja.ctx
    ctx.cache.pop(("java", "layout"))
    ctx.settings = type("S", (), {"include_generated": True})()
    assert {t.file.rsplit("/", 1)[-1]: t.status
            for t in ja.select_targets(tmp_path, units)}["Gen.java"] == "ok"
    wf = ja.wiki_facts(tmp_path, "core/java/c/package-info.java",
                       (tmp_path / "core/java/c/package-info.java").read_text(), ctx)
    assert wf.doc_comment == "Core things." and not wf.is_main


def test_no_toolchain_status(repo):
    ctx = RepoContext.build(repo, ["java"])
    ctx.mark_no_toolchain("java", "no JDK")
    ja = JavaAdapter(ctx)
    units = asyncio.run(ja.discover(None))
    assert {t.status for t in ja.select_targets(repo, units)} == {"no-toolchain"}


def test_read_pom(repo):
    info = read_pom(repo / "pom.xml")
    assert info.artifact == "javamod" and info.packaging == "jar"
    assert ("org.junit.jupiter", "junit-jupiter", "test") in info.dependencies
    assert info.properties["maven.compiler.release"] == "17"
    assert read_pom(repo / "missing.xml") is None


# -- files ------------------------------------------------------------------------------


def test_is_test_file(ja):
    assert ja.is_test_file("src/test/java/com/example/calc/CalcTest.java")
    assert not ja.is_test_file(f"{CALC}/Calc.java")
    suite = "package com.example.calc;\nclass CalcCodocsTest { @Test void x() {} }\n"
    assert ja.is_test_file("src/test/java/com/example/calc/CalcCodocsTest.java", suite)
    assert not ja.is_test_file("src/test/java/com/example/calc/CalcCodocsTest.java",
                               "package com.example.calc;\nclass CalcCodocsTest {}\n")
    assert not ja.is_test_file("src/test/java/com/example/calc/Helper.java", suite)
    assert not ja.is_test_file(f"{CALC}/CalcTest.java", suite)
    unbound = JavaAdapter()
    assert unbound.is_test_file("x/src/test/java/a/ATest.java")
    assert unbound.is_test_file("a/ATest.java", suite)
    assert not unbound.is_test_file("a/ATest.java", "class ATest {}")


def test_is_generated():
    ja = JavaAdapter()
    assert ja.is_generated("// Generated by the protocol buffer compiler.\npackage a;\n")
    assert ja.is_generated("/* @generated */ package a; class A {}")
    assert ja.is_generated("package a;\n@javax.annotation.processing.Generated(\"x\")\n"
                           "class A {}\n")
    assert ja.is_generated("package a;\n@Generated class A {}\n")
    assert not ja.is_generated("package a;\n// Generated by hand later\nclass A {}\n")
    assert ja.is_generated("package a; class A {}", "m/target/generated-sources/A.java")


# -- parsing ----------------------------------------------------------------------------


def test_decls():
    ja = JavaAdapter()
    decls = ja.decls(SAMPLE)
    got = [(d.kind, d.qualname) for d in decls]
    assert got == [
        ("type", "Sample"), ("method", "Sample.Sample"), ("method", "Sample.Sample"),
        ("method", "Sample.run"), ("method", "Sample.add"), ("method", "Sample.add"),
        ("type", "Sample.Inner"), ("method", "Sample.Inner.twice"),
        ("type", "Sample.Mode"), ("method", "Sample.Mode.active"),
        ("type", "Sample.Pair"), ("method", "Sample.Pair.sum"),
        ("type", "Sample.Shape"), ("type", "Helper"), ("method", "Helper.one")]
    top = decls[0]
    assert top.signature == \
        "public final class Sample<T extends Number> extends Base implements Runnable"
    assert SAMPLE[top.start:].startswith("/** The sample. */")
    assert top.line == 10 and top.receiver == ""
    ctor = decls[1]
    assert ctor.signature == "public Sample()" and SAMPLE[ctor.start:].startswith("/** Builds")
    assert decls[4].signature == "public int add(int a)"
    inner = decls[6]
    assert inner.name == "Sample.Inner" and inner.receiver == "Sample"
    assert inner.signature == "abstract static class Inner"
    assert decls[10].signature == "record Pair(int a, int b)"
    assert decls[7].receiver == "Sample.Inner" and decls[7].name == "twice"


def test_imports():
    ja = JavaAdapter()
    imps = ja.imports(SAMPLE)
    assert [(i.kind, i.module, i.name, i.bound, i.line) for i in imps] == [
        ("single", "java.util.List", "", "List", 4),
        ("wildcard", "java.util", "", "", 5),
        ("static", "org.junit.jupiter.api.Assertions", "assertEquals", "assertEquals", 6),
        ("static", "java.lang.Math", "*", "*", 7)]
    assert imps[1].text == "import java.util.*;"
    assert imps[2].text == "import static org.junit.jupiter.api.Assertions.assertEquals;"
    assert ja.format_import(ImportSpec(module="a.B", kind="single")) == "import a.B;"
    assert ja.format_import(ImportSpec(module="a.B", name="*", kind="static")) == \
        "import static a.B.*;"


def test_identifiers_and_package():
    ja = JavaAdapter()
    ids = ja.identifiers(SAMPLE)
    assert {"Sample", "LIMIT", "count", "Runnable"} <= ids
    assert "header" not in ids and "class" not in ids and "y" not in ids
    assert package_of(SAMPLE) == "com.example.demo"
    assert package_of("class A {}") == ""


def test_extract_code_and_preamble():
    ja = JavaAdapter()
    reply = "Here:\n```java\nclass A {}\n```\n```text\nignore me please, much longer text\n```"
    assert ja.extract_code(reply) == "class A {}\n"
    assert ja.preamble(SAMPLE) == "package com.example.demo;"
    assert ja.preamble("class A {}") == ""
    code = "package wrong.pkg;\n\nclass A {}\n"
    assert ja.apply_preamble(code, "package a.b;") == "package a.b;\n\nclass A {}\n"
    assert ja.apply_preamble("import x.Y;\nclass A {}\n", "package a.b;") == \
        "package a.b;\n\nimport x.Y;\nclass A {}\n"
    assert ja.apply_preamble(code, "") == "class A {}\n"


# -- tests in source ---------------------------------------------------------------------


def test_test_names_and_lines():
    ja = JavaAdapter()
    names = ja.test_names(TEST_SRC, "src/test/java/com/example/calc/CalcExtraTest.java")
    assert names == ["com.example.calc.CalcExtraTest#a", "com.example.calc.CalcExtraTest#b",
                     "com.example.calc.CalcExtraTest#legacy",
                     "com.example.calc.CalcExtraTest$Inner#c",
                     "com.example.calc.CalcExtraTest$Inner$Deeper#d"]
    lines = ja.test_lines(TEST_SRC, "")
    assert lines["com.example.calc.CalcExtraTest#a"] == 15
    assert lines["com.example.calc.CalcExtraTest#b"] == 19
    abstract = "package p;\nabstract class BaseTest { @Test void t() {} }\n"
    assert ja.test_names(abstract, "") == []
    assert ja.test_names("", "") == []


def test_remove_tests_and_strip():
    ja = JavaAdapter()
    out = ja.remove_tests(TEST_SRC, {"com.example.calc.CalcExtraTest#a",
                                     "com.example.calc.CalcExtraTest$Inner#c"})
    assert "void a()" not in out and "Doc of a" not in out and "void c()" not in out
    assert "void b(int v)" in out and "void d()" in out and "void helper()" in out
    assert not ts.parse(out, "java").has_error
    assert ja.test_names(out, "") == ["com.example.calc.CalcExtraTest#b",
                                      "com.example.calc.CalcExtraTest#legacy",
                                      "com.example.calc.CalcExtraTest$Inner$Deeper#d"]
    assert ja.remove_tests(TEST_SRC, set()) == TEST_SRC
    stripped = ja.strip_non_tests(TEST_SRC)
    assert "void main" not in stripped and "void a()" in stripped
    assert not ts.parse(stripped, "java").has_error
    assert ja.strip_non_tests("class A { void main(int x) {} }") == \
        "class A { void main(int x) {} }"


def test_normalize_suite(units):
    ja = JavaAdapter()
    suite = ("import org.junit.jupiter.api.Test;\n\nclass MyTests {\n    MyTests() {}\n"
             "    @Test void x() { new Calc(); }\n}\n")
    out = ja.normalize_suite(suite, units[CALC], f"{CALC}/Calc.java")
    assert out.startswith("package com.example.calc;\n\nimport org.junit")
    assert "class CalcCodocsTest {" in out and "    CalcCodocsTest() {}" in out
    assert "new Calc()" in out
    again = ja.normalize_suite(out, units[CALC], f"{CALC}/Calc.java")
    assert again == out
    other_pkg = "package x.y;\nclass CalcCodocsTest {}\n"
    assert ja.normalize_suite(other_pkg, units[CALC], f"{CALC}/Calc.java").startswith(
        "package com.example.calc;\n")


def test_suite_placement(ja, units):
    assert ja.stem(f"{CALC}/Calc.java") == "Calc.java"
    assert ja.suite_basename("Calc.java") == "CalcCodocsTest.java"
    assert ja.suite_rel(units[CALC], "Calc.java") == \
        "src/test/java/com/example/calc/CalcCodocsTest.java"
    assert ja.baseline_path(Path("/o"), units[CALC]) == \
        Path("/o/baseline/java/src__main__java__com__example__calc.json")


# -- mutation -----------------------------------------------------------------------------


def test_mutation_sites():
    ja = JavaAdapter()
    ms = ja.mutation_sites(SAMPLE)
    ops = [m.operator for m in ms]
    assert ops == [
        "cmp: < -> <=", "logic: && -> ||", "incdec: ++ -> --", "arith: * -> /",
        "arith: + -> -", "assign: -= -> +=", "bool: false -> true",
        "arith: + -> -", "arith: - -> +", "arith: * -> /", "cmp: == -> !=",
        "arith: + -> -"]
    lines = SAMPLE.splitlines()
    for m in ms:
        assert m.id == f"m{m.offset}" and SAMPLE[m.offset:].startswith(m.original)
        assert lines[m.line - 1].find(m.original) >= 0
        mutated = apply(SAMPLE, m)
        assert not ts.parse(mutated, "java").has_error
    # never in field initializers, annotations, strings or comments; string + skipped
    field_line = SAMPLE[:SAMPLE.index('"a" + "b"')].count("\n") + 1
    assert all(m.line != field_line for m in ms)
    assert all(m.line != 13 for m in ms)          # private boolean on = true;
    assert not any(m.original == "+=" for m in ms)  # s += "y"
    label_line = SAMPLE[:SAMPLE.index("label = label + k")].count("\n") + 1
    assert all(m.line != label_line for m in ms)


def test_mutation_sites_lambda_initializers_and_errors():
    ja = JavaAdapter()
    src = ("class A {\n  static { int z = 1 + 2; }\n  { int y = 3 - 1; }\n"
           "  java.util.function.IntUnaryOperator f = x -> x + 1;\n"
           "  void m() { Runnable r = () -> { int q = 2 * 3; }; String s = \"a < b\"; "
           "// c > d\n }\n}\n")
    ops = [(m.line, m.operator) for m in ja.mutation_sites(src)]
    assert ops == [(2, "arith: + -> -"), (3, "arith: - -> +"), (5, "arith: * -> /")]
    broken = "class A { void m() { int x = 1 + ; int y = 2 - 1; } }"
    assert all(m.original != "+" for m in ja.mutation_sites(broken))


# -- context -------------------------------------------------------------------------------


def test_package_context(ja, repo, units):
    ctx = ja.package_context(repo, units[CALC], f"{CALC}/Stats.java")
    assert ctx.startswith(f"// {CALC}/Calc.java\n" + "public final class Calc {")
    assert "    public static final int SCALE = 10;" in ctx
    assert "    public static int clamp(int v, int lo, int hi);" in ctx
    assert "{\n        if" not in ctx and "return" not in ctx
    small = ja.package_context(repo, units[CALC], f"{CALC}/Stats.java", max_chars=60)
    assert small.strip().endswith("// ... (truncated)")


def test_package_context_imported_types(tmp_path):
    _write(tmp_path, "pom.xml", "<project><artifactId>m</artifactId></project>")
    _write(tmp_path, "src/main/java/a/Model.java",
           "package a;\npublic class Model {\n  public int v;\n  public int get() { return v; }\n"
           "  private void hidden() {}\n}\n")
    _write(tmp_path, "src/main/java/b/Use.java",
           "package b;\nimport a.Model;\npublic class Use { int f(Model m) { return m.get(); } }\n")
    _write(tmp_path, "src/main/java/b/Other.java",
           "package b;\npublic class Other { public void o() {} void pkg() {} }\n")
    ja = JavaAdapter(RepoContext.build(tmp_path, ["java"]))
    units = asyncio.run(ja.discover(None))
    ctx = ja.package_context(tmp_path, units["src/main/java/b"], "src/main/java/b/Use.java")
    assert "// src/main/java/a/Model.java\npublic class Model {" in ctx
    assert "public int get();" in ctx and "private void hidden();" in ctx
    assert "// src/main/java/b/Other.java\npublic class Other {\n    public void o();\n}" in ctx
    unit_b = units["src/main/java/b"]
    assert ja.resolve_imports("src/main/java/b/Use.java",
                              ja.imports((tmp_path / "src/main/java/b/Use.java").read_text()),
                              ja.ctx) == ["src/main/java/a"]
    assert ja.resolve_imports("src/main/java/b/Use.java",
                              [ImportSpec(module="Other", kind="simple"),
                               ImportSpec(module="a", kind="wildcard"),
                               ImportSpec(module="a.Model", name="get", kind="static"),
                               ImportSpec(module="java.util.List", kind="single")],
                              ja.ctx) == ["src/main/java/b", "src/main/java/a"]
    assert unit_b.test_files == []


def test_package_identifiers(ja, repo, units):
    names = ja.package_identifiers(repo, units[CALC], f"{CALC}/Calc.java")
    assert names == {"Stats", "CalcTest", "StatsTest"}


def test_test_reference(ja, repo, units):
    ref = ja.test_reference(repo, units[CALC], f"{CALC}/Calc.java")
    assert ref.startswith("// from src/test/java/com/example/calc/CalcTest.java\n"
                          "// example test of the target from the existing suite\n")
    assert ref.count("@Test") == 1
    assert ja.test_reference(repo, units[SHAPES], f"{SHAPES}/Rect.java") == ""


def test_test_reference_setup_and_helpers(tmp_path):
    _write(tmp_path, "pom.xml", "<project><artifactId>m</artifactId></project>")
    _write(tmp_path, "src/main/java/a/Box.java", "package a;\npublic class Box { int n; }\n")
    _write(tmp_path, "src/test/java/a/BoxTest.java",
           "package a;\nclass BoxTest {\n  private Box box;\n  @BeforeEach void init() { box = "
           "new Box(); }\n  private Box make() { return new Box(); }\n"
           "  @Test void longer() { Box b = make(); org.junit.jupiter.api.Assertions."
           "assertEquals(0, b.n); }\n  @Test void t() { assert new Box().n == 0; }\n}\n")
    ja = JavaAdapter(RepoContext.build(tmp_path, ["java"]))
    unit = asyncio.run(ja.discover(None))["src/main/java/a"]
    ref = ja.test_reference(tmp_path, unit, "src/main/java/a/Box.java")
    assert "  @Test void t() { assert new Box().n == 0; }" in ref
    assert "private Box box;" in ref and "@BeforeEach void init()" in ref
    assert "private Box make()" in ref and "void longer()" not in ref


def test_test_index_and_relevant_tests(ja, repo, units):
    index = ja.test_index(repo, units[CALC])
    assert sorted(index) == ["com.example.calc.CalcTest#clamp", "com.example.calc.CalcTest#knownBroken",
                             "com.example.calc.CalcTest#normalize", "com.example.calc.CalcTest#sum",
                             "com.example.calc.StatsTest#add", "com.example.calc.StatsTest#mean"]
    info = index["com.example.calc.StatsTest#add"]
    assert info["file"] == "src/test/java/com/example/calc/StatsTest.java"
    assert "Stats" in info["idents"] and "getCount" in info["selectors"]
    every = set(index) | {"com.example.calc.CalcTest$Inner#x", "com.other.Gone#x"}
    assert ja.relevant_tests(repo, units[CALC], f"{CALC}/Calc.java", every) == [
        "com.example.calc.CalcTest#clamp", "com.example.calc.CalcTest#knownBroken",
        "com.example.calc.CalcTest#normalize", "com.example.calc.CalcTest#sum",
        "com.example.calc.CalcTest$Inner#x"]
    assert ja.relevant_tests(repo, units[CALC], f"{CALC}/Stats.java", every) == [
        "com.example.calc.StatsTest#add", "com.example.calc.StatsTest#mean"]
    assert ja.declared_tests(repo, units[CALC]) == set(index)


def test_relevant_tests_by_reference(tmp_path):
    _write(tmp_path, "pom.xml", "<project><artifactId>m</artifactId></project>")
    _write(tmp_path, "src/main/java/a/Box.java", "package a;\npublic class Box { int n; }\n")
    _write(tmp_path, "src/main/java/a/Lid.java", "package a;\npublic class Lid { int m; }\n")
    _write(tmp_path, "src/test/java/z/UsesTest.java",
           "package z;\nimport a.Box;\nclass UsesTest {\n  @Test void box() { new Box(); }\n"
           "  @Test void none() { int x = 1; }\n}\n")
    _write(tmp_path, "src/test/java/z/WildTest.java",
           "package z;\nimport a.*;\nclass WildTest {\n  private Lid lid;\n"
           "  @Test void lid() { lid.m = 1; }\n}\n")
    ja = JavaAdapter(RepoContext.build(tmp_path, ["java"]))
    unit = asyncio.run(ja.discover(None))["src/main/java/a"]
    assert unit.test_files == ["src/test/java/z/UsesTest.java", "src/test/java/z/WildTest.java"]
    cands = set(ja.test_index(tmp_path, unit))
    assert ja.relevant_tests(tmp_path, unit, "src/main/java/a/Box.java", cands) == \
        ["z.UsesTest#box"]
    assert ja.relevant_tests(tmp_path, unit, "src/main/java/a/Lid.java", cands) == \
        ["z.WildTest#lid"]


# -- repair -----------------------------------------------------------------------------


def test_fix_imports():
    ja = JavaAdapter()
    suite = ("package com.example.calc;\n\nimport org.junit.jupiter.api.Test;\n"
             "import org.mockito.Mockito;\nimport static org.mockito.Mockito.*;\n\n"
             "class CalcCodocsTest {\n  @Test void a() { List<Integer> x = null; "
             "assertEquals(1, 1); }\n}\n")
    allowed = ja.default_allowed(type("R", (), {"module_root": FIXTURE})(),
                                 Unit(lang="java", rel=CALC, name="", files=[]))
    base = "src/test/java/com/example/calc/CalcCodocsTest.java"
    errors = [
        f"{base}:4: package org.mockito does not exist",
        f"{base}:8: cannot find symbol (symbol: class List; location: class CalcCodocsTest)",
        f"{base}:8: cannot find symbol (symbol: method assertEquals(int,int); "
        "location: class CalcCodocsTest)",
        "src/test/java/com/example/calc/Other.java:3: cannot find symbol (symbol: class Map)",
    ]
    out = ja.fix_imports(suite, errors, "CalcCodocsTest.java", allowed)
    assert out is not None
    assert "org.mockito" not in out
    assert "import java.util.List;" in out
    assert "import static org.junit.jupiter.api.Assertions.*;" in out
    assert "java.util.Map" not in out
    assert not ts.parse(out, "java").has_error
    assert ja.fix_imports(suite, ["x.java:1: boom"], "CalcCodocsTest.java", allowed) is None
    assert ja.fix_imports(out, errors[1:2], "CalcCodocsTest.java", allowed) is None


def test_allowed_lists(ja, repo, units):
    src = (repo / CALC / "Calc.java").read_text()
    fixed = ja.fix_allowed(ja.imports(src))
    texts = [ja.format_import(i) for i in fixed]
    assert texts[0] == "import java.util.Locale;" and "import java.util.Optional;" in texts
    runner = type("R", (), {"module_root": repo})()
    dflt = [ja.format_import(i) for i in ja.default_allowed(runner, units[CALC])]
    assert dflt[0] == "import java.util.Locale;"
    assert "import org.junit.jupiter.api.Test;" in dflt
    assert "import static org.junit.jupiter.api.Assertions.*;" in dflt
    assert "import org.junit.jupiter.params.provider.CsvSource;" in dflt
    assert len(dflt) == len(set(dflt))


def test_failure_and_crash_messages():
    ja = JavaAdapter()
    out = ("captured line\norg.opentest4j.AssertionFailedError: known broken ==> expected: <2> "
           "but was: <3>\n\tat com.example.calc.CalcTest.knownBroken(CalcTest.java:33)\n")
    msg = ja.failure_message(out)
    assert msg.splitlines()[0].startswith("org.opentest4j.AssertionFailedError: known broken")
    assert "CalcTest.java:33" in msg
    assert ja.failure_message("x" * 5000).endswith("\n...")
    from codocs.lang.base import LangTestResult

    res = LangTestResult(build_ok=True, tail="ok\nException in thread main\n")
    assert ja.crash_message(res) == \
        "did not run: the test JVM exited early (Exception in thread main)"
    assert ja.crash_message(LangTestResult(build_ok=True)) == \
        "did not run: the test JVM exited early"


# -- facts -------------------------------------------------------------------------------


def test_wiki_and_graph_facts(ja, repo):
    src = ('package com.example.calc;\nimport java.util.List;\n'
           'public class App extends Base implements Runnable {\n'
           '  private Stats stats = new Stats();\n'
           '  public static void main(String[] args) { new App().run(); }\n'
           '  public void run() { Stats s = new Stats(); s.add(1); stats.mean(); this.go(); '
           'go(); Calc.clamp(1, 2, 3); List<Integer> l = null; l.forEach(Calc::abs); }\n'
           '  void go() { super.toString(); }\n'
           '  interface Cb { void call(); }\n}\n')
    wf = ja.wiki_facts(repo, "src/main/java/com/example/calc/App.java", src, ja.ctx)
    assert wf.is_main and wf.loc == 9 and [i.module for i in wf.imports] == ["java.util.List"]
    gf = ja.graph_facts(repo, "src/main/java/com/example/calc/App.java", src, ja.ctx)
    assert [f.name for f in gf.funcs] == ["App.main", "App.run", "App.go"]
    assert gf.funcs[0].static and gf.funcs[0].cls == "App" and gf.funcs[0].kind == "method"
    calls = [(c.kind, c.qual, c.name, c.recv_type) for c in gf.calls["App.run"]]
    assert calls == [("call", "Stats", "Stats", "Stats"), ("call", "s", "add", "Stats"),
                     ("call", "stats", "mean", "Stats"), ("call", "this", "go", "App"),
                     ("call", "", "go", None), ("call", "Calc", "clamp", None),
                     ("call", "l", "forEach", "List"), ("ref", "Calc", "abs", None)]
    assert ("call", "super", "toString", None) in [
        (c.kind, c.qual, c.name, c.recv_type) for c in gf.calls["App.go"]]
    t = gf.types["App"]
    assert t.bases == ["Base", "Runnable"] and t.fields == {"stats": "Stats"}
    assert t.methods == ["main", "run", "go"] and not t.is_interface
    assert gf.types["App.Cb"].is_interface
    assert not gf.is_test
    tsrc = (repo / "src/test/java/com/example/calc/CalcTest.java").read_text()
    assert ja.graph_facts(repo, "src/test/java/com/example/calc/CalcTest.java", tsrc,
                          ja.ctx).is_test


# -- runner pure helpers -------------------------------------------------------------------


def _ev(e, tid="", uid=None, outcome=None, text=""):
    d = {"e": e}
    if tid:
        d["id"] = tid
        d["uid"] = uid or tid
    if outcome:
        d["outcome"] = outcome
    if text:
        d["text"] = text
    return d


def test_fold_events_parameterized_and_skip():
    evs = [_ev("run", "C#p", "u1"), _ev("result", "C#p", "u1", "passed"),
           _ev("run", "C#p", "u2"), _ev("result", "C#p", "u2", "failed", "boom"),
           _ev("run", "C#s", "u3"), _ev("result", "C#s", "u3", "skipped"),
           _ev("run", "C#a", "u4"), _ev("result", "C#a", "u4", "aborted"),
           _ev("run", "C#ok", "u5"), _ev("result", "C#ok", "u5", "passed"), _ev("end")]
    res = fold_events(evs)
    assert res.build_ok and not res.timed_out
    assert res.tests == {"C#p": "fail", "C#s": "skip", "C#a": "skip", "C#ok": "pass"}
    assert res.output["C#p"] == "boom"


def test_fold_events_crash_and_timeout():
    crash = [_ev("run", "C#a"), _ev("result", "C#a", outcome="passed"), _ev("run", "C#b")]
    res = fold_events(crash, requested=["C#a", "C#b", "C#c"], died=True, tail="exit 3")
    assert res.tests == {"C#a": "pass", "C#b": "fail", "C#c": "interrupted"}
    assert "exited during this test" in res.output["C#b"]
    other_failed = [_ev("run", "C#a"), _ev("result", "C#a", outcome="failed"), _ev("run", "C#b")]
    assert fold_events(other_failed, died=True).tests == {"C#a": "fail", "C#b": "interrupted"}
    timeout = other_failed + [_ev("timeout")]
    res = fold_events(timeout, requested=["C#c"])
    assert res.timed_out and res.tests == {"C#a": "fail", "C#b": "fail", "C#c": "interrupted"}
    res = fold_events([], outer_timeout=True)
    assert not res.build_ok and res.build_errors == [NOT_STARTED] and res.timed_out
    res = fold_events([_ev("discovery_error", text="cannot load test class X: CNFE")])
    assert not res.build_ok and res.build_errors == ["cannot load test class X: CNFE"]
    # requested tests without a verdict are interrupted only when the process died
    assert fold_events([_ev("end")], requested=["C#x"]).tests == {}


def test_parse_javac_and_mvn_errors(tmp_path):
    call = tmp_path / "call"
    mapper = PathMap({str(call / "src"): "", str(tmp_path / "sb"): ""})
    out = (f"{call}/src/src/main/java/a/A.java:5: error: cannot find symbol\n"
           "        return totl;\n               ^\n  symbol:   variable totl\n"
           "  location: class A\n"
           f"{tmp_path}/sb/src/main/java/a/B.java:7: warning: [deprecation] x\n"
           f"{tmp_path}/sb/src/main/java/a/B.java:9: error: ';' expected\n2 errors\n")
    assert parse_javac(out, mapper) == [
        "src/main/java/a/A.java:5: cannot find symbol (symbol: variable totl; location: class A)",
        "src/main/java/a/B.java:9: ';' expected"]
    assert parse_javac("error: invalid flag: -x\n", mapper) == ["error: invalid flag: -x"]
    mvn = (f"[ERROR] {tmp_path}/sb/src/main/java/a/A.java:[5,16] cannot find symbol\n"
           "[ERROR] -> [Help 1]\n[ERROR] Failed to execute goal x\n")
    assert parse_mvn_errors(mvn, mapper) == ["src/main/java/a/A.java:5:16: cannot find symbol"]
    assert parse_mvn_errors("[ERROR] Failed to execute goal x\n[ERROR] -> [Help 1]\n",
                            mapper) == ["Failed to execute goal x"]


def test_parse_surefire_and_jacoco(tmp_path):
    (tmp_path / "TEST-a.CalcTest.xml").write_text(
        '<testsuite><testcase classname="a.CalcTest" name="clamp"/>'
        '<testcase classname="a.CalcTest" name="p(int)[1]"/>'
        '<testcase classname="a.CalcTest" name="p(int)[2]"><failure message="m">trace'
        '</failure></testcase><testcase classname="a.CalcTest" name="s"><skipped/></testcase>'
        '</testsuite>')
    (tmp_path / "TEST-a.CalcTest$Inner.xml").write_text(
        '<testsuite><testcase classname="a.CalcTest$Inner" name="n"/></testsuite>')
    (tmp_path / "TEST-b.X.xml").write_text(
        '<testsuite><testcase classname="b.X" name="X"><error message="init"/></testcase>'
        '</testsuite>')
    got = parse_surefire(tmp_path)
    assert got["a.CalcTest#clamp"] == [("pass", "")]
    assert [v for v, _ in got["a.CalcTest#p"]] == ["pass", "fail"]
    assert got["a.CalcTest#s"] == [("skip", "")] and "a.CalcTest$Inner#n" in got
    assert got["container:b.X"] == [("fail", "init")]
    assert set(parse_surefire(tmp_path, {"b.X"})) == {"container:b.X"}
    xml = ('<report><package name="a"><class name="a/A"/><sourcefile name="A.java">'
           '<line nr="3" mi="0" ci="2"/><line nr="4" mi="3" ci="0"/></sourcefile>'
           '<sourcefile name="T.java"><line nr="1" mi="0" ci="1"/></sourcefile></package>'
           '</report>')
    blocks = jacoco_blocks(xml, {"a/A.java": "src/a/A.java"})
    assert [(b.file, b.start_line, b.count, b.stmts) for b in blocks] == [
        ("src/a/A.java", 3, 1, 1), ("src/a/A.java", 4, 0, 1)]


def test_effective_pom_and_argline():
    text = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>m</artifactId>'
            '<properties><maven.compiler.source>1.8</maven.compiler.source>'
            '<project.build.sourceEncoding>ISO-8859-1</project.build.sourceEncoding>'
            '<agent>-Dx=1</agent></properties><build><outputDirectory>/o/c</outputDirectory>'
            '<plugins><plugin><artifactId>maven-compiler-plugin</artifactId><configuration>'
            '<compilerArgs><arg>-parameters</arg></compilerArgs><annotationProcessorPaths>'
            '<path><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>'
            '<version>1.18.34</version></path></annotationProcessorPaths></configuration>'
            '</plugin><plugin><artifactId>maven-surefire-plugin</artifactId><configuration>'
            '<argLine>@{argLine} -Xmx256m ${agent} ${nope}</argLine><workingDirectory>/w'
            '</workingDirectory><systemPropertyVariables><k>v</k></systemPropertyVariables>'
            '<environmentVariables><E>1</E></environmentVariables></configuration></plugin>'
            '</plugins></build></project>')
    eff = parse_effective_pom(text, "m")
    assert eff.release == 8 and eff.explicit_release and eff.encoding == "ISO-8859-1"
    assert eff.output_dir == "/o/c" and eff.compiler_args == ["-parameters"]
    assert eff.processor_paths == [("org.projectlombok", "lombok", "1.18.34")]
    assert eff.sysprops == {"k": "v"} and eff.envvars == {"E": "1"} and eff.workdir == "/w"
    tokens, notes = expand_argline(eff.argline, eff.properties)
    assert tokens == ["-Xmx256m", "-Dx=1"] and len(notes) == 1 and "${nope}" in notes[0]
    plain = parse_effective_pom("<project><artifactId>m</artifactId></project>")
    assert plain.release == 0 and not plain.explicit_release and plain.encoding == "UTF-8"


# -- classpath, JUnit 6, encodings, layouts, mvn errors -------------------------------------


needs_jdk = pytest.mark.skipif(select_jdk("", shutil.which("mvn"))[0] is None,
                               reason="no usable JDK (javac and java)")
needs_mvn = pytest.mark.skipif(shutil.which("mvn") is None, reason="mvn not installed")


def _write_all(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
    return root


def _make(root: Path, out: Path, **settings) -> JavaRunner:
    s = SimpleNamespace(test_timeout=60, go_timeout=300, **settings)
    ctx = RepoContext.build(root, ["java"], s, out)
    return JavaRunner(root, adapter=adapter("java", ctx), settings=s, out_dir=out)


def _pom(props: str, junit: str = "5.10.2") -> str:
    return ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0'
            "</modelVersion><groupId>g</groupId><artifactId>a</artifactId><version>1</version>"
            f"<properties>{props}</properties><dependencies><dependency>"
            "<groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>"
            f"<version>{junit}</version><scope>test</scope></dependency></dependencies>"
            "<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
            "<artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin>"
            "</plugins></build></project>\n")


async def _ok(r: JavaRunner, unit: str) -> None:
    prep = await r.prepare(r.adapter.unit(unit))
    if not prep.ok and any("cannot resolve the Java tool jars" in e for e in prep.errors):
        pytest.skip("Java tool jars cannot be resolved: " + prep.errors[0])
    assert prep.ok, prep.errors


TEST_HDR = ("import static org.junit.jupiter.api.Assertions.*;\n"
            "import org.junit.jupiter.api.Test;\n")


# -- pure helpers -----------------------------------------------------------------------------


def test_source_target_only_compiles_with_source_and_target():
    pom = ('<project><properties><maven.compiler.source>1.8</maven.compiler.source>'
           '<maven.compiler.target>1.8</maven.compiler.target></properties></project>')
    eff = parse_effective_pom(pom)
    assert eff.release == 8 and eff.source_target
    prep = SimpleNamespace(release=8, source_target=True)
    assert _level_flags(prep)[:4] == ["-source", "8", "-target", "8"]
    eff = parse_effective_pom('<project><properties><maven.compiler.release>17'
                              '</maven.compiler.release></properties></project>')
    assert not eff.source_target
    assert _level_flags(SimpleNamespace(release=17, source_target=False)) == ["--release", "17"]


def test_platform_version_follows_the_platform_jars():
    jup5 = ["/r/org/junit/jupiter/junit-jupiter-api/5.10.2/junit-jupiter-api-5.10.2.jar"]
    assert platform_version(jup5) == "1.10.2"
    six = ["/r/junit-jupiter-api-6.0.0.jar",
           "/r/org/junit/platform/junit-platform-engine/6.0.0/junit-platform-engine-6.0.0.jar"]
    assert platform_version(six) == "6.0.0"
    assert platform_version(["/r/junit-jupiter-api-6.0.1.jar"]) == "6.0.1"
    assert platform_version([]) == "1.10.2"


def test_encode_java_escapes_what_the_codec_cannot_hold():
    text = "// 名前 — note\nString s = \"名無しさん\U0001F600\";\n"
    data = encode_java(text, "shift_jis")
    back = data.decode("shift_jis")
    assert "名前" in back and "\\u2014" in back and "\\ud83d\\ude00" in back
    assert encode_java("plain", "utf-8") == b"plain"


def test_parse_mvn_errors_folds_symbol_lines_and_drops_noise():
    out = (
        "[ERROR] COMPILATION ERROR : \n"
        "[ERROR] /w/src/test/java/a/BoxCodocsTest.java:[5,20] cannot find symbol\n"
        "  symbol:   class List\n  location: class a.BoxCodocsTest\n[INFO] 1 error\n"
        "[ERROR] BUILD FAILURE\n"
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:"
        "testCompile (default-testCompile) on project v: Compilation failure\n"
        "[ERROR] /w/src/test/java/a/BoxCodocsTest.java:[5,20] cannot find symbol\n"
        "[ERROR]   symbol:   class List\n[ERROR]   location: class a.BoxCodocsTest\n"
        "[ERROR] -> [Help 1]\n[ERROR] After correcting the problems, you can resume the build\n")
    errs = parse_mvn_errors(out, PathMap({"/w": ""}))
    assert errs == ["src/test/java/a/BoxCodocsTest.java:5:20: cannot find symbol (symbol: "
                    "class List; location: class a.BoxCodocsTest)"]
    ja = adapter("java")
    src = "package a;\nclass BoxCodocsTest {\n  List<String> xs;\n}\n"
    fixed = ja.fix_imports(src, errs, "BoxCodocsTest.java", ja.fix_allowed([]))
    assert fixed is not None and "import java.util.List;" in fixed


def test_string_returning_project_methods_are_not_arith_mutated(tmp_path):
    repo = _write_all(tmp_path / "r", {
        "src/p/Currency.java": ("package p;\nclass Currency {\n  String symbol() { return \"$\"; }"
                                "\n  int scale() { return 2; }\n}\n"),
        "src/p/Money.java": ("package p;\nclass Money {\n  Currency c; long minor;\n"
                             "  private String tag(String s) { return s; }\n"
                             "  String show() { return c.symbol() + minor; }\n"
                             "  String label(int n) { return tag(\"x\") + n; }\n"
                             "  int digits() { return c.scale() + 1; }\n}\n"),
    })
    ja = adapter("java", RepoContext.build(repo, ["java"]))
    src = (repo / "src/p/Money.java").read_text()
    lines = {m.line for m in ja.mutation_sites(src) if m.operator == "arith: + -> -"}
    assert lines == {7}


def test_mirror_keeps_package_dirs_named_like_build_outputs(tmp_path):
    src = _write_all(tmp_path / "m", {
        "pom.xml": "<project/>", "target/classes/X.class": "x",
        "src/main/java/a/build/B.java": "package a.build;\nclass B {}\n",
        "src/main/java/a/target/T.java": "package a.target;\nclass T {}\n",
    })
    dst = tmp_path / "dst"
    _mirror_tree(src, dst, set(), {"src", "src/main", "src/main/java", "src/main/java/a",
                                   "src/main/java/a/build", "src/main/java/a/target"})
    assert (dst / "src/main/java/a/build/B.java").is_file()
    assert (dst / "src/main/java/a/target/T.java").is_file()
    assert not (dst / "target").exists()
    farm = tmp_path / "farm"
    _symlink_farm(src, farm, {}, None, "", {"src", "src/main", "src/main/java",
                                             "src/main/java/a", "src/main/java/a/build",
                                             "src/main/java/a/target"})
    assert (farm / "src/main/java/a/build/B.java").is_file()
    assert (farm / "src/main/java/a/target/T.java").is_file()
    assert not (farm / "target").exists()


@needs_jdk
def test_maven_layout_without_mvn_is_unavailable_and_not_cached(tmp_path):
    from codocs.baseline import package_baseline

    repo = _write_all(tmp_path / "j", {
        "pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>"),
        "src/main/java/a/A.java": "package a;\npublic class A { static int f() { return 1; } }\n",
        "src/test/java/a/ATest.java": ("package a;\n" + TEST_HDR + "class ATest {\n"
                                       "  @Test void f() { assertEquals(1, A.f()); }\n}\n"),
    })
    r = _make(repo, tmp_path / "out", mvn=str(tmp_path / "nope" / "mvn"))
    assert "mvn not found" in r.unavailable_reason() and not r.available()
    unit = r.adapter.unit("src/main/java/a")
    b = asyncio.run(package_baseline(r, unit, tmp_path / "out"))
    assert not b.build_ok and "mvn not found" in b.build_errors[0]
    assert not r.adapter.baseline_path(tmp_path / "out", unit).exists()


@needs_jdk
@needs_mvn
def test_overlay_cannot_borrow_the_originals_classes(tmp_path):
    repo = _write_all(tmp_path / "j", {
        "pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>"),
        "src/main/java/a/Ledger.java": (
            "package a;\npublic class Ledger {\n  public static long big() { return "
            "Helper.limit() + 1; }\n  public static class Entry { public int n = 3; }\n}\n"
            "class Helper {\n  static long limit() { return 9; }\n}\n"),
        "src/test/java/a/LedgerTest.java": (
            "package a;\n" + TEST_HDR + "class LedgerTest {\n"
            "  @Test void big() { assertEquals(10, Ledger.big()); }\n"
            "  @Test void entry() { assertEquals(3, new Ledger.Entry().n); }\n}\n"),
    })
    r = _make(repo, tmp_path / "out")
    u = "src/main/java/a"
    asyncio.run(_ok(r, u))
    f = f"{u}/Ledger.java"
    src = (repo / f).read_text()
    res = asyncio.run(r.test(u, overlay={f: src}))
    assert res.build_ok and set(res.tests.values()) == {"pass"}
    for code in ("package a;\n", "package a;\n// nothing here\n"):
        for kw in ({}, {"compile_only": True}):
            res = asyncio.run(r.test(u, overlay={f: code}, **kw))
            assert not res.build_ok and "declares no type" in res.build_errors[0]
    no_helper = src.split("class Helper")[0]
    res = asyncio.run(r.test(u, overlay={f: no_helper}, compile_only=True))
    assert not res.build_ok and "Helper" in res.build_errors[0]
    no_entry = src.replace("  public static class Entry { public int n = 3; }\n", "")
    res = asyncio.run(r.test(u, overlay={f: no_entry}))
    assert not res.build_ok and "Entry" in res.build_errors[0]     # as Maven would fail


@needs_jdk
@needs_mvn
def test_source_target_project_overlay_uses_the_running_jdk_library(tmp_path):
    repo = _write_all(tmp_path / "j", {
        "pom.xml": _pom("<maven.compiler.source>1.8</maven.compiler.source>"
                        "<maven.compiler.target>1.8</maven.compiler.target>"),
        "src/main/java/a/Text.java": ("package a;\npublic class Text {\n  public static boolean "
                                      "empty(String s) { return s.isBlank(); }\n}\n"),
        "src/test/java/a/TextTest.java": ("package a;\n" + TEST_HDR + "class TextTest {\n"
                                          "  @Test void blank() { assertTrue(Text.empty(\" \"));"
                                          " }\n}\n"),
    })
    r = _make(repo, tmp_path / "out")
    u = "src/main/java/a"
    asyncio.run(_ok(r, u))
    f = f"{u}/Text.java"
    res = asyncio.run(r.test(u, overlay={f: (repo / f).read_text()}))
    assert res.build_ok, res.build_errors
    assert res.tests == {"a.TextTest#blank": "pass"}


@needs_jdk
@needs_mvn
def test_shift_jis_module_compiles_new_suites_and_unencodable_text(tmp_path):
    greet = "package a;\n/** 名前に敬称を付ける。 */\npublic class Greet {\n" \
        "  public static String san(String n) { return n.isEmpty() ? \"名無しさん\" : n + \"さん\"; }\n}\n"
    repo = _write_all(tmp_path / "j", {
        "pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>"
                        "<project.build.sourceEncoding>Shift_JIS</project.build.sourceEncoding>"),
        "src/main/java/a/Greet.java": greet.encode("shift_jis"),
        "src/main/java/a/Plain.java": ("package a;\npublic class Plain {\n  public static "
                                       "String name() { return \"x\"; }\n}\n"),
        "src/test/java/a/PlainTest.java": ("package a;\n" + TEST_HDR + "class PlainTest {\n"
                                           "  @Test void name() { assertEquals(\"名無し\", "
                                           "Plain.name()); }\n}\n").encode("shift_jis"),
        "src/test/java/a/GreetTest.java": ("package a;\n" + TEST_HDR + "class GreetTest {\n"
                                           "  @Test void san() { assertEquals(\"太郎さん\", "
                                           "Greet.san(\"太郎\")); }\n}\n").encode("shift_jis"),
    })
    r = _make(repo, tmp_path / "out")
    u = "src/main/java/a"
    asyncio.run(_ok(r, u))
    f = f"{u}/Greet.java"
    dash = greet.replace("付ける。", "付ける — 空なら既定値。")
    res = asyncio.run(r.test(u, overlay={f: dash}, run=["a.GreetTest#san"]))
    assert res.build_ok, res.build_errors
    assert res.tests == {"a.GreetTest#san": "pass"}
    suite = ("package a;\n" + TEST_HDR + "class GreetCodocsTest {\n  // 空文字なら既定値 —\n"
             "  @Test void empty() { assertEquals(\"名無しさん\", Greet.san(\"\")); }\n}\n")
    ov = {"src/test/java/a/GreetTest.java": None, "src/test/java/a/PlainTest.java": None,
          "src/test/java/a/GreetCodocsTest.java": suite}
    res = asyncio.run(r.test(u, overlay=ov, cover=True))
    assert res.build_ok, res.build_errors
    assert res.tests == {"a.GreetCodocsTest#empty": "pass"}
    # an ASCII original reads as utf-8, but javac still reads the overlay as Shift_JIS
    f = f"{u}/Plain.java"
    res = asyncio.run(r.test(u, overlay={f: (repo / f).read_text().replace("x", "名無し")},
                             run=["a.PlainTest#name"]))
    assert res.build_ok and res.tests == {"a.PlainTest#name": "pass"}, res.output


@needs_jdk
@needs_mvn
def test_package_named_build_survives_the_private_mirror(tmp_path):
    repo = _write_all(tmp_path / "j", {
        "pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>"),
        "src/main/java/a/build/Info.java": ("package a.build;\npublic class Info {\n"
                                            "  public static int major() { return 2; }\n}\n"),
        "src/test/java/a/build/InfoTest.java": ("package a.build;\n" + TEST_HDR +
                                                "class InfoTest {\n  @Test void major() { "
                                                "assertEquals(2, Info.major()); }\n}\n"),
    })
    r = _make(repo, tmp_path / "out")        # no sandbox: the runner's own mirror
    u = "src/main/java/a/build"
    asyncio.run(_ok(r, u))
    res = asyncio.run(r.test(u))
    assert res.build_ok, res.build_errors
    assert res.tests == {"a.build.InfoTest#major": "pass"}


@needs_jdk
def test_plain_layout_with_separate_test_root(tmp_path):
    repo = _write_all(tmp_path / "plain", {
        "src/com/p/Util.java": ("package com.p;\npublic class Util {\n  public static int "
                                "twice(int x) { return 2 * x; }\n}\n"),
        "test/com/p/UtilTest.java": ("package com.p;\n" + TEST_HDR + "class UtilTest {\n"
                                     "  @Test void twice() { assertEquals(4, Util.twice(2)); }"
                                     "\n}\n"),
    })
    r = _make(repo, tmp_path / "out")
    u = "src/com/p"
    assert r.adapter.unit(u).test_files == ["test/com/p/UtilTest.java"]
    asyncio.run(_ok(r, u))
    assert asyncio.run(r.list_tests(u)) == ["com.p.UtilTest#twice"]
    res = asyncio.run(r.test(u))
    assert res.build_ok and res.tests == {"com.p.UtilTest#twice": "pass"}


@pytest.mark.slow
@needs_jdk
@needs_mvn
def test_junit6_project_runs_on_the_fast_path(tmp_path):
    repo = _write_all(tmp_path / "j6", {
        "pom.xml": _pom("<maven.compiler.release>17</maven.compiler.release>", junit="6.0.0"),
        "src/main/java/a/Calc.java": ("package a;\npublic class Calc {\n  public static int "
                                      "add(int x, int y) { return x + y; }\n}\n"),
        "src/test/java/a/CalcTest.java": ("package a;\n" + TEST_HDR + "class CalcTest {\n"
                                          "  @Test void add() { assertEquals(3, Calc.add(1, 2));"
                                          " }\n}\n"),
    })
    r = _make(repo, tmp_path / "out")
    u = "src/main/java/a"
    prep = asyncio.run(r.prepare(r.adapter.unit(u)))
    if not prep.ok and any("resolve" in e for e in prep.errors):
        pytest.skip("JUnit 6 jars unavailable: " + prep.errors[0])
    assert prep.ok and prep.platform.startswith("6."), (prep.errors, prep.platform)
    assert asyncio.run(r.list_tests(u)) == ["a.CalcTest#add"]
    assert asyncio.run(r.test(u)).tests == {"a.CalcTest#add": "pass"}
