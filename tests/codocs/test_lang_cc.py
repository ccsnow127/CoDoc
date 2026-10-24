"""C and C++ adapters (codocs.lang.cc) and the CcRunner (codocs.lang.ccrun) on real toolchains."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codocs.config import Settings
from codocs.golang.gotool import file_coverage
from codocs.lang import treesitter as ts
from codocs.lang.base import FRAGMENT_SLOTS, ImportSpec, RepoContext, Unit, apply, parse_fragments
from codocs.lang.registry import adapter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CMOD = FIXTURES / "cmod"
CPPMOD = FIXTURES / "cppmod"
PROMPTS = Path(__file__).resolve().parents[2] / "src" / "codocs" / "prompts" / "lang"

needs_ts = pytest.mark.skipif(not ts.available(), reason="tree-sitter not installed")
needs_cc = pytest.mark.skipif(not (shutil.which("cc") and shutil.which("gcov")),
                              reason="cc and gcov are required")
needs_cxx = pytest.mark.skipif(not (shutil.which("c++") and shutil.which("gcov")),
                               reason="c++ and gcov are required")
pytestmark = needs_ts


def _cc_rejects_implicit_declarations() -> bool:
    """whether cc fails on a call of an undeclared function with the runner's flags (clang
    and GCC 14+; GCC 13 and older only warn, and the runner passes -w)."""
    from codocs.lang.ccrun import BASE_FLAGS

    if not shutil.which("cc"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "probe.c"
        src.write_text("int main(void) { return undeclared_fn(0); }\n")
        p = subprocess.run(["cc", "-c", str(src), "-o", str(Path(tmp) / "probe.o"),
                            *BASE_FLAGS], capture_output=True)
    return p.returncode != 0


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _ctx(root: Path, langs=("c",), settings=None) -> RepoContext:
    return RepoContext.build(root, list(langs), settings or Settings())


def _settings(**kw):
    base = Settings()
    d = {k: getattr(base, k) for k in base.__dataclass_fields__}
    d.update(kw)
    return SimpleNamespace(**d)


def _runner(root: Path, lang: str, out: Path, settings=None, timeout: int = 20):
    from codocs.lang.ccrun import CcRunner

    s = settings or Settings()
    a = adapter(lang, _ctx(root, (lang,), s))
    return a, CcRunner(a, repo_root=root, work_dir=out / "work" / "tmp", settings=s,
                       timeout=timeout)


def run(coro):
    return asyncio.run(coro)


# -- declarations ---------------------------------------------------------------------------


def _kinds(decls):
    return [(d.kind, d.qualname) for d in decls]


def test_c_decls_fixture():
    c = adapter("c")
    src = (CMOD / "calc" / "calc.c").read_text()
    assert _kinds(c.decls(src)) == [("var", "sum_calls"), ("func", "is_space"),
                                    ("func", "clamp"), ("func", "sum"), ("func", "normalize")]
    clamp = next(d for d in c.decls(src) if d.name == "clamp")
    assert src[clamp.start:].startswith("/* clamp limits v")
    assert clamp.line == 15 and clamp.end_line == 23
    assert clamp.signature == "int clamp(int v, int lo, int hi)"
    hdr = (CMOD / "calc" / "calc.h").read_text()
    # prototypes are not decls, include guards are not macros
    assert _kinds(c.decls(hdr)) == [("const", "CALC_SCALE"), ("const", "CALC_OK"),
                                    ("const", "CALC_EMPTY")]
    assert _kinds(c.decls((CMOD / "calc" / "stats.h").read_text())) == [("type", "stats")]
    assert _kinds(c.decls((CMOD / "shapes" / "shapes.h").read_text())) == [("type", "rect_t")]


def test_c_decls_synthetic():
    src = """#include <stdio.h>
#define MAX(a, b) ((a) > (b) ? (a) : (b))
typedef int (*cb_t)(int);
enum color { RED, GREEN = 4 };
struct point { int x, y; };
extern int ext_var;
int glob = 3, glob2;
static int (*table[])(int) = { 0 };
int proto(int v);
#ifdef __cplusplus
extern "C" {
#endif
int inner(void) { return 0; }
#ifdef __cplusplus
}
#endif
char *ptr(void) { return 0; }
CODOCS_TEST(not_a_decl) { CHECK(1); }
"""
    got = _kinds(adapter("c").decls(src))
    assert got == [("const", "MAX"), ("type", "cb_t"), ("type", "color"), ("type", "point"),
                   ("var", "glob"), ("var", "glob2"), ("var", "table"), ("func", "inner"),
                   ("func", "ptr")]


def test_cpp_decls():
    cpp = adapter("cpp")
    vec = (CPPMOD / "geom" / "vec.cpp").read_text()
    assert _kinds(cpp.decls(vec)) == [("method", "Vec2.Vec2"), ("method", "Vec2.add"),
                                      ("method", "Vec2.dot"), ("method", "Vec2.norm"),
                                      ("method", "Vec2.operator==")]
    hpp = (CPPMOD / "geom" / "vec.hpp").read_text()
    assert _kinds(cpp.decls(hpp)) == [("type", "Vec2"), ("method", "Vec2.x"),
                                      ("method", "Vec2.y")]
    src = """namespace ns {
constexpr double EPS = 1e-9;
using Points = int;
struct A { int f() { return 1; } struct B { void g(); }; };
template <typename T> T twice(T v) { return v + v; }
namespace inner { int h(int a) { return a; } }
}
void ns::A::B::g() {}
A::~A() {}
int ns::inner::k() { return 1; }
"""
    assert _kinds(cpp.decls(src)) == [
        ("var", "EPS"), ("type", "Points"), ("type", "A"), ("method", "A.f"),
        ("func", "twice"), ("func", "h"), ("method", "A.B.g"), ("method", "A.~A"),
        ("func", "k")]


def test_imports_identifiers_format():
    c = adapter("c")
    src = (CMOD / "calc" / "calc.c").read_text()
    imps = c.imports(src)
    assert [(i.module, i.kind, i.line) for i in imps] == [
        ("calc.h", "quote", 1), ("ctype.h", "angle", 3), ("string.h", "angle", 4)]
    assert [c.format_import(i) for i in imps] == [
        '#include "calc.h"', "#include <ctype.h>", "#include <string.h>"]
    ids = c.identifiers('/* hidden */ int f(void) { return g("not_me"); } // gone\n')
    assert {"f", "g"} <= ids and not ids & {"hidden", "not_me", "gone", "int", "return"}


# -- tests in source ---------------------------------------------------------------------------


SUITE = """// generated suite
#include "codocs_test.h"
#include CODOCS_TARGET

/* first */
CODOCS_TEST(clamp_mid) {
    CHECK_EQ_INT(clamp(5, 0, 10), 5);
}

CODOCS_TEST(clamp_low) { CHECK_EQ_INT(clamp(-1, 0, 10), 0); }
/* CODOCS_TEST(commented) { } */
static int cdx_twice(int v) { return 2 * v; }
CODOCS_TEST(uses_helper) { CHECK_EQ_INT(cdx_twice(2), 4); }
"""


def test_test_names_lines_remove():
    c = adapter("c")
    rel = "calc/calc_codocs_test.c"
    assert c.test_names(SUITE, rel) == [f"{rel}::clamp_mid", f"{rel}::clamp_low",
                                        f"{rel}::uses_helper"]
    assert c.test_lines(SUITE, rel) == {f"{rel}::clamp_mid": 6, f"{rel}::clamp_low": 10,
                                        f"{rel}::uses_helper": 13}
    prog = (CMOD / "calc" / "test_calc.c").read_text()
    assert c.test_names(prog, "calc/test_calc.c") == ["calc/test_calc.c"]
    assert c.test_lines(prog, "calc/test_calc.c") == {"calc/test_calc.c": 16}
    assert c.test_names((CMOD / "calc" / "calc.c").read_text(), "calc/calc.c") == []
    pruned = c.remove_tests(SUITE, {f"{rel}::clamp_mid", "uses_helper"})
    assert c.test_names(pruned, rel) == [f"{rel}::clamp_low"]
    assert "cdx_twice" in pruned and "clamp(5, 0, 10)" not in pruned
    assert c.remove_tests(SUITE, set()) == SUITE


def test_strip_and_normalize_suite():
    c = adapter("c")
    src = '#include "calc.c"\n#include <stdio.h>\nCODOCS_TEST(a) { CHECK(1); }\n' \
          "int main(void) {\n    return 0;\n}\n"
    stripped = c.strip_non_tests(src)
    assert "main" not in stripped and "CODOCS_TEST(a)" in stripped
    norm = c.normalize_suite("/* top */\n" + stripped, None, "calc/calc.c")
    lines = [ln for ln in norm.splitlines() if ln.strip() and not ln.startswith("/*")]
    assert lines[:2] == ['#include "codocs_test.h"', "#include CODOCS_TARGET"]
    assert '#include "calc.c"' not in norm and "#include <stdio.h>" in norm
    assert norm.startswith("/* top */\n")
    assert c.normalize_suite(norm, None, "calc/calc.c") == norm


def test_suite_placement_and_baseline_path(tmp_path: Path):
    c, cpp = adapter("c"), adapter("cpp")
    u = Unit(lang="c", rel="calc", name="calc")
    assert c.stem("calc/calc.c") == "calc.c"
    assert c.suite_rel(u, "calc.c") == "calc/calc_codocs_test.c"
    assert c.suite_basename("calc.c") == "calc_codocs_test.c"
    assert cpp.suite_rel("geom", "vec.cc") == "geom/vec_codocs_test.cpp"
    assert c.suite_rel(Unit(lang="c", rel="", name="r"), "m.c") == "m_codocs_test.c"
    assert c.baseline_path(tmp_path, u) == tmp_path / "baseline" / "c" / "calc.json"


# -- mutation ----------------------------------------------------------------------------------


def test_mutation_sites_c():
    c = adapter("c")
    src = """#define TWICE(x) ((x) + (x))
static int table[2 + 1] = { 1 + 2 };
const char *s = "a < b";
int f(int a, int b) {
    int arr[3 + 1];
    // a == b in a comment
    const char *t = "x + y";
#if 1 + 1 == 2
    a++;
#endif
    if (a < b && true) { a += 1; }
    return a * b - -a;
}
"""
    ms = c.mutation_sites(src)
    got = [(m.line, m.operator) for m in ms]
    # nothing in the #define, the global initializers, the array size, the comment, the
    # strings, the #if condition or the conditional block (line 9)
    assert got == [(11, "cmp: < -> <="), (11, "logic: && -> ||"),
                   (11, "bool: true -> false"), (11, "assign: += -> -="),
                   (12, "arith: * -> /"), (12, "arith: - -> +")]
    for m in ms:
        mutated = apply(src, m)
        assert not ts.parse(mutated, "c").has_error, m


def test_mutation_sites_cpp_skips_strings():
    cpp = adapter("cpp")
    src = (CPPMOD / "util" / "strutil.cpp").read_text()
    ms = cpp.mutation_sites(src)
    assert all(src[m.offset:m.offset + len(m.original)] == m.original for m in ms)
    lines = {m.line for m in ms if m.original in ("+", "+=")}
    # `cur += c` (line 24) and `out += sep` / `out += parts[i]` (lines 38, 40) are strings
    assert not lines & {24, 38, 40}
    assert any(m.operator == "cmp: == -> !=" and m.line == 22 for m in ms)


def test_shift_jis_round_trip(tmp_path: Path):
    root = tmp_path / "r"
    shutil.copytree(CMOD, root)
    ctx = _ctx(root)
    c = adapter("c", ctx)
    text = ctx.read("sjis/sjis.c")
    assert ctx.codec("sjis/sjis.c") == "shift_jis" and "こんにちは" in text
    decls = c.decls(text)
    assert [d.name for d in decls] == ["sjis_greeting", "sjis_len"]
    ms = c.mutation_sites(text)
    m = next(m for m in ms if m.original == "!=")
    mutated = apply(text, m)
    data = ts.encode(mutated, ctx.codec("sjis/sjis.c"))
    orig = (root / "sjis/sjis.c").read_bytes()
    bpos = len(text[:m.offset].encode("shift_jis"))
    assert data[:bpos] == orig[:bpos] and data[bpos:bpos + 2] == b"=="
    assert data[bpos + 2:] == orig[bpos + 2:]


# -- generated code ------------------------------------------------------------------------------


def test_is_generated_file_and_dir_rule(tmp_path: Path):
    gen = "/*\n * Generated by asn1c-0.9.29 (http://lionet.info/asn1c)\n */\nint a(void) {\n" \
          "    return 1;\n}\n"
    plain = "int b(void) { return 2; }\n"
    _write(tmp_path, "asn/src/A.c", gen)
    _write(tmp_path, "asn/src/B.c", gen.replace("a(void)", "b2(void)"))
    _write(tmp_path, "asn/src/OCTET_STRING.c", plain)
    _write(tmp_path, "app/main.c", "// generated by hand? no: code generated below\n" + plain)
    _write(tmp_path, "app/x.c", plain)
    _write(tmp_path, "app/y.c", "/* do not edit */\nint y(void) { return 0; }\n")
    _write(tmp_path, "lib/z.c", plain)
    ctx = _ctx(tmp_path)
    c = adapter("c", ctx)
    assert c.is_generated(plain, "asn/src/OCTET_STRING.c")       # dir rule (2 of 3)
    assert c.is_generated(gen, "asn/src/A.c")
    assert not c.is_generated(plain, "lib/z.c")
    assert c.is_generated(ctx.read("app/main.c"), "app/main.c")
    # app: 2 of 3 generated -> the dir rule applies to x.c too
    assert c.is_generated(plain, "app/x.c")
    targets = {t.file: t.status for t in c.select_targets(tmp_path, c.units())}
    assert targets["asn/src/OCTET_STRING.c"] == "generated" and targets["lib/z.c"] == "ok"
    ctx2 = _ctx(tmp_path, settings=_settings(include_generated=True))
    c2 = adapter("c", ctx2)
    assert not c2.is_generated(gen, "asn/src/A.c")
    assert {t.status for t in c2.select_targets(tmp_path, c2.units())} == {"ok"}


# -- units and targets ----------------------------------------------------------------------------


def test_units_cmod():
    ctx = _ctx(CMOD)
    c = adapter("c", ctx)
    units = run(c.discover(None))
    assert sorted(units) == ["calc", "shapes", "sjis"]
    calc = units["calc"]
    assert calc.files == ["calc/calc.c", "calc/calc.h", "calc/stats.c", "calc/stats.h"]
    assert calc.test_files == ["calc/test_broken.c", "calc/test_calc.c", "calc/test_stats.c"]
    assert calc.name == "calc" and calc.build_root == "calc" and calc.import_path == ""
    assert calc.extra["main_files"] == [] and calc.extra["compile_db"] is False
    assert calc.extra["include_dirs"][:2] == ["calc", ""]
    assert units["shapes"].test_files == []
    assert sorted(run(c.discover(None, subtree="calc"))) == ["calc"]
    assert sorted(run(c.discover(None, packages=["shapes"]))) == ["shapes"]
    targets = {t.file: t.status for t in c.select_targets(CMOD, units)}
    assert targets == {"calc/calc.c": "ok", "calc/calc.h": "header", "calc/stats.c": "ok",
                       "calc/stats.h": "header", "shapes/shapes.c": "ok",
                       "shapes/shapes.h": "header", "sjis/sjis.c": "ok"}
    picked = c.select_targets(CMOD, units, files="calc/*.c", max_files=1)
    assert [(t.file, t.status) for t in picked] == [("calc/calc.c", "ok")]
    assert all(t.lang == "c" and t.stem == t.file.rsplit("/", 1)[1] for t in picked)
    assert c.is_test_file("calc/test_calc.c") and not c.is_test_file("calc/calc.c")
    assert c.is_test_file("calc/calc_codocs_test.c", SUITE)
    assert not c.is_test_file("calc/calc.h")


def test_units_cppmod():
    cpp = adapter("cpp", _ctx(CPPMOD, ("cpp",)))
    units = cpp.units()
    assert sorted(units) == ["geom", "util"]
    assert units["geom"].test_files == ["geom/test_broken.cpp", "geom/test_vec.cpp"]
    assert units["geom"].files == ["geom/poly.cpp", "geom/poly.hpp", "geom/vec.cpp",
                                   "geom/vec.hpp"]


def test_programs_in_test_dirs_attach_by_calls(tmp_path: Path):
    _write(tmp_path, "lib/util.h", "int twice(int v);\n")
    _write(tmp_path, "lib/util.c", '#include "util.h"\nint twice(int v) { return 2 * v; }\n')
    _write(tmp_path, "app/app.c", "int app_main(void) { return 0; }\n")
    _write(tmp_path, "app/tool.c", "int main(void) { return 0; }\n")
    _write(tmp_path, "tests/test_util.c", '#include "util.h"\nint main(void) '
                                          "{ return twice(2) == 4 ? 0 : 1; }\n")
    _write(tmp_path, "tests/helpers.c", "int helper(void) { return 1; }\n")
    c = adapter("c", _ctx(tmp_path))
    units = c.units()
    assert sorted(units) == ["app", "lib", "tests"]
    assert units["lib"].test_files == ["tests/test_util.c"]
    assert units["tests"].test_files == [] and units["tests"].files == ["tests/helpers.c"]
    assert units["app"].extra["main_files"] == ["app/tool.c"]
    st = {t.file: t.status for t in c.select_targets(tmp_path, units)}
    assert st["tests/helpers.c"] == "test-support" and st["app/tool.c"] == "ok"
    assert c.symbols()["twice"] == ["lib/util.c"]


# -- context, relevance, repair --------------------------------------------------------------------


def test_package_context_and_identifiers():
    c = adapter("c", _ctx(CMOD))
    unit = c.unit("calc")
    ctx_text = c.package_context(CMOD, unit, "calc/stats.c")
    assert "// from calc/stats.h" in ctx_text and "// from calc/calc.h" in ctx_text
    assert "struct stats {" in ctx_text and "#define CALC_SCALE 10" in ctx_text
    assert ctx_text.index("CALC_SCALE") < ctx_text.index("CALC_EMPTY")      # referenced first
    assert "int clamp(int v, int lo, int hi);" in ctx_text
    small = c.package_context(CMOD, unit, "calc/stats.c", max_chars=80)
    assert small.rstrip().endswith("// ... (truncated)")
    assert c.package_identifiers(CMOD, unit, "calc/stats.c") == {
        "clamp", "sum", "normalize"}
    assert c.package_identifiers(CMOD, unit) == {"clamp", "sum", "normalize", "stats_add",
                                                 "stats_mean"}


def test_package_context_synthesises_prototypes(tmp_path: Path):
    _write(tmp_path, "u/a.c", "int helper(int v) { return v + 1; }\n"
                              "static int hidden(void) { return 0; }\n")
    _write(tmp_path, "u/b.c", "int use(void) { return helper(1) + hidden(); }\n")
    c = adapter("c", _ctx(tmp_path))
    text = c.package_context(tmp_path, c.unit("u"), "u/b.c")
    assert "int helper(int v);" in text and "hidden" not in text


def test_cpp_context_strips_method_bodies():
    cpp = adapter("cpp", _ctx(CPPMOD, ("cpp",)))
    text = cpp.package_context(CPPMOD, cpp.unit("geom"), "geom/poly.cpp")
    assert "class Polygon {" in text and "class Vec2 {" in text
    assert "return x_;" not in text and "double x() const;" in text
    assert "std::size_t size() const;" in text


def test_relevant_tests_and_reference():
    c = adapter("c", _ctx(CMOD))
    unit = c.unit("calc")
    cands = set(unit.test_files)
    assert c.relevant_tests(CMOD, unit, "calc/calc.c", cands) == [
        "calc/test_broken.c", "calc/test_calc.c"]
    assert c.relevant_tests(CMOD, unit, "calc/stats.c", cands) == ["calc/test_stats.c"]
    assert c.relevant_tests(CMOD, unit, "calc/calc.c", {"calc/test_calc.c"}) == [
        "calc/test_calc.c"]
    ref = c.test_reference(CMOD, unit, "calc/calc.c")
    assert ref.startswith("// from calc/test_calc.c") and "test_stats" not in ref
    assert c.declared_tests(CMOD, unit) == cands
    idx = c.test_index(CMOD, unit)
    assert idx["calc/test_calc.c"]["file"] == "calc/test_calc.c"
    assert "clamp" in idx["calc/test_calc.c"]["idents"]


def test_fix_imports_and_allowed():
    c = adapter("c", _ctx(CMOD))
    unit = c.unit("calc")
    allowed = c.default_allowed(SimpleNamespace(module_root=CMOD), unit)
    mods = [(i.kind, i.module) for i in allowed]
    assert ("quote", "calc.h") in mods and ("quote", "stats.h") in mods
    assert ("angle", "stdio.h") in mods
    src = '#include "codocs_test.h"\n#include CODOCS_TARGET\n\nCODOCS_TEST(a) { ' \
          "printf(\"x\"); stats_add(0, 1); }\n"
    errors = [
        "calc/calc_codocs_test.c:4:18: error: call to undeclared library function 'printf' "
        "with type 'int (const char *, ...)'",
        "calc/calc_codocs_test.c:4:33: error: call to undeclared function 'stats_add'; ISO C99",
        "calc/other.c:1:1: error: use of undeclared identifier 'strlen'",
    ]
    fixed = c.fix_imports(src, errors, "calc_codocs_test.c", allowed)
    assert fixed is not None
    lines = fixed.splitlines()
    assert lines[:4] == ['#include "codocs_test.h"', "#include CODOCS_TARGET",
                         "#include <stdio.h>", '#include "stats.h"']
    assert "string.h" not in fixed
    assert c.fix_imports(fixed, errors, "calc_codocs_test.c", allowed) is None
    assert c.fix_imports(src, ["calc/x.c:1:1: error: something else"], "x.c", allowed) is None
    cpp = adapter("cpp")
    got = cpp.fix_imports("int f() { std::vector<int> v; return 0; }\n",
                          ["a.cpp:1:16: error: no member named 'vector' in namespace 'std'"],
                          "a.cpp", cpp.fix_allowed([]))
    assert got.startswith("#include <vector>\n")
    extra = c.fix_allowed([ImportSpec(module="calc.h", kind="quote")])
    assert extra[0].module == "calc.h" and {"stdio.h", "math.h"} <= {i.module for i in extra}


def test_preamble_and_apply():
    c = adapter("c")
    src = (CMOD / "calc" / "calc.c").read_text()
    pre = c.preamble(src)
    assert pre == '#include "calc.h"\n\n#include <ctype.h>\n#include <string.h>\n'
    guarded = "/* x */\n#ifdef HAVE_X\n#include <x.h>\n#endif\n#define A 1\nint a;\n"
    assert c.preamble(guarded) == "/* x */\n#ifdef HAVE_X\n#include <x.h>\n#endif\n"
    hdr = "#ifndef X_H\n#define X_H\nint a;\n#endif\n"
    assert c.preamble(hdr) == ""
    code = '#include "calc.h"\n#include <stdlib.h>\n\nint clamp(int v, int lo, int hi) ' \
           "{ return v; }\n"
    out = c.apply_preamble(code, pre)
    assert out.startswith(pre + "#include <stdlib.h>\n\nint clamp(")
    assert out.count('#include "calc.h"') == 1
    assert c.apply_preamble(code, "") == code


# -- include resolution and flags ----------------------------------------------------------------------


def test_resolve_include_and_flags(tmp_path: Path):
    _write(tmp_path, "proj/include/proj/api.h", "int api(void);\n")
    _write(tmp_path, "proj/src/core/core.c", '#include "proj/api.h"\n#include "local.h"\n'
                                              '#include "deep.h"\n#include <stdio.h>\n'
                                              "int api(void) { return 1; }\n")
    _write(tmp_path, "proj/src/core/local.h", "int local(void);\n")
    _write(tmp_path, "proj/src/other/sub/deep.h", "int deep(void);\n")
    _write(tmp_path, "far/away/deep.h", "int deep2(void);\n")
    ctx = _ctx(tmp_path)
    c = adapter("c", ctx)
    rel = "proj/src/core/core.c"
    assert c.resolve_include(rel, "local.h", ctx) == "proj/src/core/local.h"
    assert c.resolve_include(rel, "proj/api.h", ctx) == "proj/include/proj/api.h"
    assert c.resolve_include(rel, "deep.h", ctx) == "proj/src/other/sub/deep.h"     # nearest
    assert c.resolve_include(rel, "<stdio.h>", ctx) is None
    dirs = c.include_dirs("proj/src/core")
    assert "proj/include" in dirs and "proj/src/other/sub" in dirs
    assert c.resolve_imports(rel, c.imports(ctx.read(rel)), ctx) == [
        "proj/include/proj", "proj/src/other/sub"]
    flags, from_db = c.file_flags(rel)
    assert not from_db and f"-I{tmp_path.resolve() / 'proj/include'}" in flags


def test_cgo_include_dirs(tmp_path: Path):
    _write(tmp_path, "go.mod", "module example.com/m\n\ngo 1.21\n")
    _write(tmp_path, "ring/ring.go", 'package ring\n\n/*\n#cgo CFLAGS: -I${SRCDIR}/../clib/inc '
                                     '-DX=1\n#include "cring.h"\n*/\nimport "C"\n')
    _write(tmp_path, "ring/shim.c", '#include "cring.h"\nint shim(void) { return 0; }\n')
    _write(tmp_path, "clib/inc/cring.h", "int cring_len(void);\n")
    ctx = _ctx(tmp_path)
    c = adapter("c", ctx)
    assert c.cgo_include_dirs("ring") == ["clib/inc"]
    assert ctx.cgo_flags["ring"]["include_dirs"] == ["clib/inc"]
    assert c.resolve_include("ring/shim.c", "cring.h", ctx) == "clib/inc/cring.h"


def test_compile_commands(tmp_path: Path):
    _write(tmp_path, "src/a.c", "int a(void) { return A; }\n")
    _write(tmp_path, "build/compile_commands.json", json.dumps([{
        "directory": str(tmp_path / "build"), "file": "../src/a.c",
        "command": "cc -DA=3 -I../inc -isystem /opt/x -std=c11 -O2 -fPIC -o a.o -c ../src/a.c",
    }]))
    c = adapter("c", _ctx(tmp_path))
    flags, from_db = c.file_flags("src/a.c")
    assert from_db
    assert flags == ["-DA=3", f"-I{(tmp_path / 'inc').resolve()}", "-isystem", "/opt/x",
                     "-std=c11", "-fPIC"]
    u = c.unit("src")
    assert u.extra["compile_db"] and u.extra["defines"] == ["-DA=3"] and u.extra["std"] == "c11"


# -- facts -------------------------------------------------------------------------------------------


def test_graph_and_wiki_facts(tmp_path: Path):
    src = """#include "x.h"
static int helper(int v) { return v; }
int run(int v) { return helper(v) + other(v); }
static int (*table[])(int) = { helper, run };
int main(void) { int (*f)(int) = run; return f(1); }
"""
    c = adapter("c", _ctx(CMOD))
    facts = c.graph_facts(CMOD, "app/app.c", src, c.ctx)
    assert [(f.name, f.kind, f.static, f.line) for f in facts.funcs] == [
        ("helper@app.c", "func", True, 2), ("run", "func", False, 3), ("main", "func", False, 5)]
    assert [(k.kind, k.name) for k in facts.calls["run"]] == [("call", "helper"),
                                                              ("call", "other")]
    assert ("ref", "run") in [(k.kind, k.name) for k in facts.calls["main"]]
    assert facts.tables == {"table": ["helper", "run"]}
    assert facts.imports[0].module == "x.h" and not facts.is_test
    wiki = c.wiki_facts(CMOD, "app/app.c", src, c.ctx)
    assert wiki.is_main and wiki.loc == 5 and wiki.doc_comment == ""
    cpp = adapter("cpp", _ctx(CPPMOD, ("cpp",)))
    vsrc = (CPPMOD / "geom" / "poly.cpp").read_text()
    pf = cpp.graph_facts(CPPMOD, "geom/poly.cpp", vsrc, cpp.ctx)
    assert [f.name for f in pf.funcs] == ["Polygon.Polygon", "Polygon.area", "Polygon.perimeter"]
    area = {(k.qual, k.name, k.recv_type) for k in pf.calls["Polygon.area"]}
    assert ("a", "x", "Vec2") in area and ("std", "fabs", None) in area
    hpp = (CPPMOD / "geom" / "poly.hpp").read_text()
    hf = cpp.graph_facts(CPPMOD, "geom/poly.hpp", hpp, cpp.ctx)
    assert hf.types["Polygon"].fields == {"pts_": "vector"}
    assert set(hf.types["Polygon"].methods) >= {"area", "perimeter", "size"}


def test_prompt_fragments_define_every_slot():
    for lang, fence in (("c", "c"), ("cpp", "cpp")):
        frags = parse_fragments((PROMPTS / f"{lang}.txt").read_text(encoding="utf-8"))
        assert set(FRAGMENT_SLOTS) <= set(frags), lang
        assert frags["FENCE"] == fence and frags["MARK"] == "//"
        assert "CODOCS_TEST" in frags["TEST_SHAPE"]
    assert adapter("cpp").prompts.fragments["LANG"] == "C++"


# -- the support files -------------------------------------------------------------------------------


@needs_cc
def test_support_files_compile(tmp_path: Path):
    from codocs.lang.cc import write_support

    d = write_support(tmp_path / "cc")
    assert sorted(p.name for p in d.iterdir()) == ["codocs_main.c", "codocs_main.cpp",
                                                   "codocs_test.h"]
    cmds = [["cc", "-std=c99", "-pedantic", "-Wall", "-c", "codocs_main.c", "-o", "a.o"]]
    if shutil.which("c++"):
        cmds.append(["c++", "-std=c++11", "-Wall", "-c", "codocs_main.cpp", "-o", "b.o"])
    for cmd in cmds:
        cp = subprocess.run(cmd, cwd=d, capture_output=True, text=True)
        assert cp.returncode == 0, cp.stderr


# -- the runner ----------------------------------------------------------------------------------


@pytest.fixture()
def crepo(tmp_path: Path) -> Path:
    root = tmp_path / "cmod"
    shutil.copytree(CMOD, root)
    return root


CALC_SUITE = """#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(clamp_mid) { CHECK_EQ_INT(clamp(5, 0, 10), 5); }
CODOCS_TEST(clamp_edges) { CHECK_EQ_INT(clamp(-1, 0, 10), 0); CHECK_EQ_INT(clamp(11, 0, 10), 10); }
CODOCS_TEST(sum_values) {
    int v[] = {1, 2, 3};
    int err = -1;
    CHECK_EQ_INT(sum(v, 3, &err), 6);
    CHECK_EQ_INT(err, CALC_OK);
}
CODOCS_TEST(space) { CHECK(is_space(' ')); }
"""


def _suite_overlay(unit, rel: str, suite: str) -> dict:
    ov = {f: None for f in unit.test_files}
    ov[rel] = suite
    return ov


@needs_cc
def test_runner_baseline_and_selection(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out")
    assert r.available() and "gcov" in r.version() and r.lang == "c" and r.adapter is a
    assert run(r.list_tests("calc")) == ["calc/test_broken.c", "calc/test_calc.c",
                                         "calc/test_stats.c"]
    res = run(r.test("calc"))
    assert res.build_ok and res.tests == {"calc/test_broken.c": "fail",
                                          "calc/test_calc.c": "pass",
                                          "calc/test_stats.c": "pass"}
    assert "known broken" in res.output["calc/test_broken.c"] and res.returncode != 0
    res = run(r.test("calc", run=["calc/test_calc.c", "calc/nope.c"]))
    assert res.tests == {"calc/test_calc.c": "pass"} and res.returncode == 0
    res = run(r.test("calc", run=[]))
    assert res.build_ok and res.tests == {}
    res = run(r.test("shapes"))
    assert res.build_ok and res.tests == {}
    res = run(r.test("nope"))
    assert not res.build_ok
    assert not list((tmp_path / "out" / "work" / "tmp").iterdir())      # call dirs removed
    assert sorted(p.name for p in (crepo / "calc").iterdir()) == sorted(
        p.name for p in (CMOD / "calc").iterdir())                    # repo untouched


@needs_cc
def test_runner_baseline_with_package_baseline(crepo: Path, tmp_path: Path):
    from codocs.baseline import package_baseline

    a, r = _runner(crepo, "c", tmp_path / "out")
    base = run(package_baseline(r, a.unit("calc"), tmp_path / "out"))
    assert base.build_ok and base.failing == ["calc/test_broken.c"]
    assert base.passing == ["calc/test_calc.c", "calc/test_stats.c"]


@needs_cc
def test_runner_replaced_broken_file(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out")
    bad = (crepo / "calc/calc.c").read_text().replace("return v;\n}", "return v\n}")
    res = run(r.test("calc", overlay={"calc/calc.c": bad}, run=["calc/test_calc.c"]))
    assert not res.build_ok
    assert res.build_errors and res.build_errors[0].startswith("calc/calc.c:22:")
    assert "error" in res.build_errors[0]
    assert all(str(tmp_path) not in e for e in res.build_errors)
    co = run(r.test("calc", overlay={"calc/calc.c": bad}, compile_only=True))
    assert not co.build_ok and co.build_errors[0].startswith("calc/calc.c:22:")
    good = run(r.test("calc", overlay={"calc/calc.c": bad.replace("return v\n", "return v;\n")},
                      compile_only=True))
    assert good.build_ok and good.tests == {}
    # a regenerated file that keeps its behaviour passes the relevant programs
    same = run(r.test("calc", overlay={"calc/calc.c": (crepo / "calc/calc.c").read_text()},
                      run=["calc/test_calc.c"]))
    assert same.tests == {"calc/test_calc.c": "pass"}


@needs_cc
def test_runner_hidden_tests_added_suite_coverage(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out")
    unit = a.unit("calc")
    rel = a.suite_rel(unit, "calc.c")
    res = run(r.test("calc", overlay=_suite_overlay(unit, rel, CALC_SUITE), cover=True))
    assert res.build_ok, res.build_errors
    assert res.tests == {f"{rel}::clamp_mid": "pass", f"{rel}::clamp_edges": "pass",
                         f"{rel}::sum_values": "pass", f"{rel}::space": "pass"}
    src = (crepo / "calc/calc.c").read_text()
    cov, missed = file_coverage(res.coverage, "calc/calc.c", src)
    assert 0.3 < cov < 1.0
    assert 47 in missed and 16 not in missed and 31 in missed       # normalize, n == 0 branch
    scov, smissed = file_coverage(res.coverage, "calc/stats.c")
    assert scov == 0.0 and smissed                                   # never called: 0, not absent
    assert {b.file for b in res.coverage} == {"calc/calc.c", "calc/stats.c"}
    assert all(b.stmts == 1 and b.start_line == b.end_line for b in res.coverage)
    # programs with coverage: lines of every unit source
    res = run(r.test("calc", run=["calc/test_stats.c"], cover=True))
    assert file_coverage(res.coverage, "calc/stats.c")[0] > 0.8
    assert file_coverage(res.coverage, "calc/calc.c")[0] == 0.0


@needs_cc
def test_runner_suite_not_using_target_has_zero_coverage(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out")
    unit = a.unit("calc")
    rel = a.suite_rel(unit, "stats.c")
    suite = '#include "codocs_test.h"\n#include CODOCS_TARGET\n\nCODOCS_TEST(t) { CHECK(1); }\n'
    res = run(r.test("calc", overlay=_suite_overlay(unit, rel, suite), cover=True))
    assert res.tests == {f"{rel}::t": "pass"}
    assert file_coverage(res.coverage, "calc/stats.c")[0] == 0.0
    assert file_coverage(res.coverage, "calc/calc.c")[0] == 0.0


@needs_cc
def test_runner_suite_failures_crash_timeout(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out", timeout=3)
    unit = a.unit("calc")
    rel = a.suite_rel(unit, "calc.c")
    suite = """#include "codocs_test.h"
#include CODOCS_TARGET
#include <unistd.h>

CODOCS_TEST(ok) { CHECK_EQ_INT(clamp(5, 0, 10), 5); }
CODOCS_TEST(wrong) { CHECK_EQ_INT(clamp(1, 2, 3), 1); }
CODOCS_TEST(crash) { volatile int *p = 0; *p = 1; }
CODOCS_TEST(skipped) { SKIP("later"); }
CODOCS_TEST(hang) { for (;;) { sleep(1); } }
CODOCS_TEST(after) { CHECK(1); }
"""
    ov = _suite_overlay(unit, rel, suite)
    t0 = time.monotonic()
    res = run(r.test("calc", overlay=ov))
    took = time.monotonic() - t0
    assert res.build_ok and res.timed_out
    assert res.tests == {f"{rel}::ok": "pass", f"{rel}::wrong": "fail",
                         f"{rel}::crash": "fail", f"{rel}::skipped": "skip",
                         f"{rel}::hang": "fail", f"{rel}::after": "interrupted"}
    assert "2 != 1" in res.output[f"{rel}::wrong"]
    assert res.output[f"{rel}::wrong"].startswith(f"{rel}:6:")
    assert "signal" in res.output[f"{rel}::crash"]
    assert "timed out" in res.output[f"{rel}::hang"]
    assert took < 3 + 5 + 30
    assert "CHECK_EQ_INT" in a.failure_message(res.output[f"{rel}::wrong"])
    # run= selects cases; failfast stops at the first failure
    res = run(r.test("calc", overlay=ov, run=[f"{rel}::ok", f"{rel}::wrong", f"{rel}::after",
                                              f"{rel}::missing"], failfast=True))
    assert res.tests == {f"{rel}::ok": "pass", f"{rel}::wrong": "fail"}
    with pytest.raises(ValueError):
        run(r.test("calc", overlay=ov, failfast=True, cover=True))


@needs_cc
@pytest.mark.skipif(not _cc_rejects_implicit_declarations(),
                    reason="cc accepts implicit function declarations (GCC 13 or older)")
def test_runner_suite_build_error_and_fix_imports(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out")
    unit = a.unit("calc")
    rel = a.suite_rel(unit, "stats.c")
    suite = ('#include "codocs_test.h"\n#include CODOCS_TARGET\n\n'
             "CODOCS_TEST(t) { CHECK_EQ_INT(stats_mean(0), 0); }\n")
    rel = a.suite_rel(unit, "calc.c")
    res = run(r.test("calc", overlay=_suite_overlay(unit, rel, suite)))
    assert not res.build_ok
    assert any(e.startswith(f"{rel}:4:") and "stats_mean" in e for e in res.build_errors)
    fixed = a.fix_imports(suite, res.build_errors, a.suite_basename("calc.c"),
                          a.default_allowed(r, unit))
    assert '#include "stats.h"' in fixed
    fixed = fixed.replace("stats_mean(0)", "stats_mean(&(struct stats){0, 0, 0})")
    res = run(r.test("calc", overlay=_suite_overlay(unit, rel, fixed)))
    assert res.build_ok and res.tests == {f"{rel}::t": "pass"}


@needs_cc
def test_runner_mutants_killed_and_survived(crepo: Path, tmp_path: Path):
    a, r = _runner(crepo, "c", tmp_path / "out")
    unit = a.unit("calc")
    rel = a.suite_rel(unit, "calc.c")
    ov = _suite_overlay(unit, rel, CALC_SUITE)
    names = sorted(run(r.test("calc", overlay=ov)).tests)
    src = (crepo / "calc/calc.c").read_text()
    sites = a.mutation_sites(src)
    killed = next(m for m in sites if m.operator == "assign: += -> -=" and m.line == 37)
    survives = next(m for m in sites if m.operator == "cmp: < -> <=" and m.line == 16)

    async def outcome(m):
        r2 = await r.test("calc", overlay={**ov, "calc/calc.c": apply(src, m)}, run=names,
                          failfast=True, timeout=10)
        assert r2.build_ok, r2.build_errors
        return all(r2.tests.get(n) == "pass" for n in names)

    async def both():
        return await asyncio.gather(outcome(killed), outcome(survives))

    assert run(both()) == [False, True]
    # every C mutant of the fixture is syntactically valid C
    for m in sites:
        assert not ts.parse(apply(src, m), "c").has_error


@needs_cc
def test_runner_program_timeout_and_crash(tmp_path: Path):
    root = tmp_path / "repo"
    _write(root, "u/u.h", "int value(void);\n")
    _write(root, "u/u.c", '#include "u.h"\nint value(void) { return 7; }\n')
    _write(root, "u/test_hang.c", '#include <unistd.h>\n#include "u.h"\n'
                                   "int main(void) { for (;;) sleep(1); return value(); }\n")
    _write(root, "u/test_crash.c", '#include "u.h"\nint main(void) { volatile int *p = 0; '
                                    "*p = value(); return 0; }\n")
    _write(root, "u/test_ok.c", '#include "u.h"\nint main(void) { return value() == 7 ? 0 : 1; }\n')
    a, r = _runner(root, "c", tmp_path / "out", timeout=2)
    t0 = time.monotonic()
    res = run(r.test("u"))
    assert time.monotonic() - t0 < 25
    assert res.build_ok and res.timed_out
    assert res.tests == {"u/test_crash.c": "fail", "u/test_hang.c": "fail", "u/test_ok.c": "pass"}
    assert "timed out" in res.output["u/test_hang.c"]
    assert "signal" in res.output["u/test_crash.c"]


@needs_cc
def test_runner_symbol_closure_and_unresolved(tmp_path: Path):
    root = tmp_path / "repo"
    _write(root, "lib/util.c", "int twice(int v) { return 2 * v; }\n")
    _write(root, "app/app.c", "int twice(int v);\nint quad(int v) { return twice(twice(v)); }\n")
    _write(root, "app/test_app.c", "int quad(int v);\nint missing_fn(void);\n"
                                   "int main(int argc, char **argv) {\n"
                                   "    (void)argv;\n"
                                   "    if (argc > 5) return missing_fn();\n"
                                   "    return quad(2) == 8 ? 0 : 1;\n}\n")
    a, r = _runner(root, "c", tmp_path / "out")
    res = run(r.test("app"))
    assert res.build_ok and res.tests == {"app/test_app.c": "pass"}
    assert "unresolved: missing_fn" in res.tail


@needs_cc
def test_runner_test_cmd(crepo: Path, tmp_path: Path):
    if not shutil.which("make"):
        pytest.skip("make is required")
    s = _settings(test_cmd="make -s test", build_cmd="make -s build")
    a, r = _runner(crepo, "c", tmp_path / "out", settings=s)
    unit = a.unit("calc")
    assert run(r.list_tests("calc")) == ["cmd:test"]
    assert a.declared_tests(crepo, unit) == {"cmd:test"}
    assert a.relevant_tests(crepo, unit, "calc/calc.c", {"cmd:test"}) == ["cmd:test"]
    res = run(r.test("calc"))
    assert res.tests == {"cmd:test": "fail"} and "known broken" in res.output["cmd:test"]
    fixed = (crepo / "calc/test_broken.c").read_text().replace("!= 1", "!= 2")
    res = run(r.test("calc", overlay={"calc/test_broken.c": fixed}, run=["cmd:test"]))
    assert res.tests == {"cmd:test": "pass"}
    bad = (crepo / "calc/calc.c").read_text().replace("return v;\n}", "return v\n}")
    co = run(r.test("calc", overlay={"calc/calc.c": bad}, compile_only=True))
    assert not co.build_ok
    assert not (crepo / "build").exists()


@needs_cc
@pytest.mark.skipif(sys.platform != "darwin", reason="the /tmp symlink exists on macOS")
def test_runner_out_under_tmp(crepo: Path):
    out = Path(tempfile.mkdtemp(dir="/tmp", prefix="codocs-cc-"))
    try:
        a, r = _runner(crepo, "c", out)
        unit = a.unit("calc")
        rel = a.suite_rel(unit, "calc.c")
        res = run(r.test("calc", overlay=_suite_overlay(unit, rel, CALC_SUITE), cover=True))
        assert res.build_ok and set(res.tests) == {f"{rel}::{n}" for n in (
            "clamp_mid", "clamp_edges", "sum_values", "space")}
        assert file_coverage(res.coverage, "calc/calc.c")[0] > 0.3
    finally:
        shutil.rmtree(out, ignore_errors=True)


@needs_cc
def test_make_runner_from_context(crepo: Path, tmp_path: Path):
    out = tmp_path / "out"
    a = adapter("c", _ctx(crepo))
    sync = []

    async def ensure_synced():
        sync.append(1)

    rc = SimpleNamespace(repo_root=crepo, exec_root=crepo, work_dir=out / "work" / "tmp",
                         tools_dir=out / "work" / "tools", settings=Settings(), workers=2,
                         timeout=30, ensure_synced=ensure_synced)
    r = a.make_runner(rc)
    assert r.adapter is a and r.workers == 2 and r.timeout == 30 and r.lang == "c"
    res = run(r.test("calc", run=["calc/test_stats.c"]))
    assert res.tests == {"calc/test_stats.c": "pass"} and sync


@needs_cxx
def test_cpp_runner(tmp_path: Path):
    root = tmp_path / "cppmod"
    shutil.copytree(CPPMOD, root)
    a, r = _runner(root, "cpp", tmp_path / "out")
    res = run(r.test("geom"))
    assert res.build_ok and res.tests == {"geom/test_broken.cpp": "fail",
                                          "geom/test_vec.cpp": "pass"}
    unit = a.unit("geom")
    rel = a.suite_rel(unit, "vec.cpp")
    suite = """#include "codocs_test.h"
#include CODOCS_TARGET
#include <stdexcept>

CODOCS_TEST(norm) { CHECK_EQ_DBL(geom::Vec2(3, 4).norm(), 5.0, 1e-9); }
CODOCS_TEST(add) { geom::Vec2 v = geom::Vec2(1, 2).add(geom::Vec2(3, 4)); CHECK(v == geom::Vec2(4, 6)); }
CODOCS_TEST(throws) { throw std::runtime_error("boom"); }
"""
    res = run(r.test("geom", overlay=_suite_overlay(unit, rel, suite), cover=True))
    assert res.build_ok, res.build_errors
    assert res.tests == {f"{rel}::norm": "pass", f"{rel}::add": "pass",
                         f"{rel}::throws": "fail"}
    assert "uncaught exception: boom" in res.output[f"{rel}::throws"]
    src = (root / "geom/vec.cpp").read_text()
    assert file_coverage(res.coverage, "geom/vec.cpp", src)[0] == 1.0
    assert file_coverage(res.coverage, "geom/poly.cpp")[0] == 0.0
    bad = src.replace("return x_ * o.x_ + y_ * o.y_;", "return x_ * o.x_ + y_ * o.y_")
    res = run(r.test("geom", overlay={"geom/vec.cpp": bad}, run=["geom/test_vec.cpp"]))
    assert not res.build_ok and res.build_errors[0].startswith("geom/vec.cpp:16:")
    res = run(r.test("util"))
    assert res.build_ok and res.tests == {}


# -- C/C++ edge cases ------------------------------------------------------------------------------


GTEST_FILE = """#include "gtest/gtest.h"
#include "strings.h"

TEST(Strings, Upper) { EXPECT_EQ(upper('a'), 'A'); }
TEST(Strings, Lower) { EXPECT_EQ(upper('B'), 'B'); }
"""


def _gtest_repo(root: Path) -> Path:
    _write(root, "util/strings.h", "char upper(char c);\n")
    _write(root, "util/strings.cc", '#include "strings.h"\n'
                                    "char upper(char c) { return c >= 'a' && c <= 'z' ? "
                                    "(char)(c - 32) : c; }\n")
    _write(root, "util/strings_test.cc", GTEST_FILE)
    return root


def test_framework_test_without_main_is_no_source_or_target(tmp_path: Path):
    """a gtest foo_test.cc (main from gtest_main) is a test file: no library source, no target."""
    _gtest_repo(tmp_path)
    cpp = adapter("cpp", _ctx(tmp_path, ("cpp",)))
    unit = cpp.unit("util")
    assert unit.files == ["util/strings.cc", "util/strings.h"] and unit.test_files == []
    assert cpp.is_framework_test("util/strings_test.cc") and cpp.is_test_file(
        "util/strings_test.cc")
    assert unit.extra["framework_tests"] == ["util/strings_test.cc"]
    st = {t.file: t.status for t in cpp.select_targets(tmp_path, cpp.units())}
    assert st == {"util/strings.cc": "ok", "util/strings.h": "header"}
    assert "upper" in cpp.symbols() and not cpp.is_framework_test("util/strings.cc")


@needs_cxx
def test_framework_test_never_breaks_a_suite_build(tmp_path: Path):
    root = _gtest_repo(tmp_path / "repo")
    a, r = _runner(root, "cpp", tmp_path / "out")
    unit = a.unit("util")
    rel = a.suite_rel(unit, "strings.cc")
    suite = '#include "codocs_test.h"\n#include CODOCS_TARGET\n\n' \
            "CODOCS_TEST(up) { CHECK(upper('a') == 'A'); }\n"
    res = run(r.test("util", overlay={rel: suite}))
    assert res.build_ok, res.build_errors
    assert res.tests == {f"{rel}::up": "pass"}


@needs_cc
def test_runner_verdict_after_partial_output_line(tmp_path: Path):
    """a case printing without a newline (printf("%d ", x)) still gets its PASS verdict."""
    root = tmp_path / "repo"
    _write(root, "src/show.h", "void print_list(const int *v, int n);\nint total(const int *v, "
                               "int n);\n")
    _write(root, "src/show.c", '#include <stdio.h>\n#include "show.h"\n'
                               'void print_list(const int *v, int n) { for (int i = 0; i < n; '
                               'i++) printf("%d ", v[i]); }\n'
                               "int total(const int *v, int n) { int s = 0; for (int i = 0; "
                               "i < n; i++) s += v[i]; return s; }\n")
    a, r = _runner(root, "c", tmp_path / "out")
    unit = a.unit("src")
    rel = a.suite_rel(unit, "show.c")
    suite = """#include "codocs_test.h"
#include CODOCS_TARGET
#include <stdio.h>

CODOCS_TEST(prints) { int v[] = {1, 2}; print_list(v, 2); CHECK_EQ_INT(total(v, 2), 3); }
CODOCS_TEST(warns) { fprintf(stderr, "warn"); CHECK(1); }
CODOCS_TEST(fails) { printf("x"); CHECK_EQ_INT(total(0, 0), 1); }
CODOCS_TEST(sums) { int v[] = {4}; CHECK_EQ_INT(total(v, 1), 4); }
"""
    res = run(r.test("src", overlay={rel: suite}))
    assert res.build_ok, res.build_errors
    assert res.tests == {f"{rel}::prints": "pass", f"{rel}::warns": "pass",
                         f"{rel}::fails": "fail", f"{rel}::sums": "pass"}
    assert "1 2" in res.output[f"{rel}::prints"]
    assert "@@CODOCS" not in res.output[f"{rel}::prints"]


@needs_cc
def test_runner_programs_with_stubs_or_included_sources_link(tmp_path: Path):
    """the unit library is an archive: a program's own definitions win over the siblings'."""
    root = tmp_path / "repo"
    _write(root, "src/parse.h", "int count_fields(const char *s);\nint parse_n(const char *s);\n")
    _write(root, "src/parse.c", '#include "parse.h"\n'
                                "static int is_sep(char c) { return c == ','; }\n"
                                "int count_fields(const char *s) { int n = 1; for (; *s; s++) "
                                "n += is_sep(*s); return n; }\n")
    _write(root, "src/util.c", '#include "parse.h"\n'
                               "int parse_n(const char *s) { return count_fields(s) * 10; }\n")
    _write(root, "tests/test_parse.c", '#include "../src/parse.c"\n'
                                       "int main(void) { return is_sep(',') && "
                                       'count_fields("a,b") == 2 ? 0 : 1; }\n')
    _write(root, "tests/test_util.c", '#include "../src/parse.h"\n'
                                      "int count_fields(const char *s) { (void)s; return 7; }\n"
                                      'int main(void) { return parse_n("x") == 70 ? 0 : 1; }\n')
    a, r = _runner(root, "c", tmp_path / "out")
    assert a.unit("src").test_files == ["tests/test_parse.c", "tests/test_util.c"]
    res = run(r.test("src"))
    assert res.build_ok, res.build_errors
    assert res.tests == {"tests/test_parse.c": "pass", "tests/test_util.c": "pass"}, res.output


@needs_cc
def test_runner_sibling_that_does_not_compile_is_left_out(tmp_path: Path):
    root = tmp_path / "repo"
    _write(root, "src/ring.h", "int ring_cap(int n);\n")
    _write(root, "src/ring.c", '#include "ring.h"\nint ring_cap(int n) { return n * 2; }\n')
    _write(root, "src/poll_linux.c", "#include <codocs_no_such_header.h>\n"
                                     "int poll_once(void) { return 0; }\n")
    a, r = _runner(root, "c", tmp_path / "out")
    unit = a.unit("src")
    rel = a.suite_rel(unit, "ring.c")
    suite = '#include "codocs_test.h"\n#include CODOCS_TARGET\n\n' \
            "CODOCS_TEST(p) { CHECK_EQ_INT(ring_cap(3), 6); }\n"
    res = run(r.test("src", overlay={rel: suite}, cover=True))
    assert res.build_ok, res.build_errors
    assert res.tests == {f"{rel}::p": "pass"}
    assert "src/poll_linux.c: left out of the link" in res.tail
    assert file_coverage(res.coverage, "src/ring.c")[0] == 1.0
    # the overlaid target itself not compiling is still a build failure
    res = run(r.test("src", overlay={rel: suite, "src/ring.c": "int ring_cap(int n) {\n"}))
    assert not res.build_ok


@needs_cc
def test_runner_library_file_with_conditional_self_test_main(tmp_path: Path):
    """`#ifdef SELFTEST int main()` in a library file keeps its other functions linkable."""
    root = tmp_path / "repo"
    _write(root, "src/base64.c", "int b64_len(int n) { return (n + 2) / 3 * 4; }\n"
                                 "#ifdef BASE64_SELFTEST\n#include <stdio.h>\n"
                                 'int main(void) { printf("%d\\n", b64_len(3)); return 0; }\n'
                                 "#endif\n")
    _write(root, "src/frame.c", "int b64_len(int n);\nint frame_len(int n) { return 4 + "
                                "b64_len(n); }\n")
    _write(root, "src/test_frame.c", "int frame_len(int n);\n"
                                     "int main(void) { return frame_len(3) == 8 ? 0 : 1; }\n")
    a, r = _runner(root, "c", tmp_path / "out")
    unit = a.unit("src")
    assert unit.extra["main_files"] == ["src/base64.c"]
    rel = a.suite_rel(unit, "frame.c")
    suite = '#include "codocs_test.h"\n#include CODOCS_TARGET\n\n' \
            "CODOCS_TEST(f) { CHECK_EQ_INT(frame_len(3), 8); }\n"
    res = run(r.test("src", overlay=_suite_overlay(unit, rel, suite), cover=True))
    assert res.build_ok, res.build_errors
    assert res.tests == {f"{rel}::f": "pass"}, res.output
    assert "unresolved" not in res.tail
    assert file_coverage(res.coverage, "src/base64.c")[0] == 1.0
    res = run(r.test("src"))
    assert res.tests == {"src/test_frame.c": "pass"}, res.output


def test_program_ownership_by_methods_operators_and_other_language(tmp_path: Path):
    _write(tmp_path, "src/vec.hpp", "namespace geo {\nclass Vec {\npublic:\n"
                                    "    Vec(int x, int y);\n    int dot(const Vec &o) const;\n"
                                    "    Vec operator+(const Vec &o) const;\n"
                                    "    int x() const;\nprivate:\n    int x_, y_;\n};\n}\n")
    _write(tmp_path, "src/vec.cpp", '#include "vec.hpp"\nnamespace geo {\n'
                                    "Vec::Vec(int x, int y) : x_(x), y_(y) {}\n"
                                    "int Vec::dot(const Vec &o) const { return x_ * o.x_ + y_ * "
                                    "o.y_; }\nVec Vec::operator+(const Vec &o) const { return "
                                    "Vec(x_ + o.x_, y_ + o.y_); }\n"
                                    "int Vec::x() const { return x_; }\n}\n")
    _write(tmp_path, "test/vec_test.cpp", '#include "../src/vec.hpp"\n'
                                          "int main() { geo::Vec a(1, 2), b(3, 4);\n"
                                          "    return a.dot(b) == 11 && (a + b).x() == 4 ? 0 : 1; }\n")
    _write(tmp_path, "lib/crc.h", '#ifdef __cplusplus\nextern "C" {\n#endif\n'
                                  "int crc8(const char *p, int n);\n"
                                  "#ifdef __cplusplus\n}\n#endif\n")
    _write(tmp_path, "lib/crc.c", '#include "crc.h"\nint crc8(const char *p, int n) '
                                  "{ int s = 0; while (n--) s ^= *p++; return s; }\n")
    _write(tmp_path, "tests/test_crc.cpp", '#include "../lib/crc.h"\n'
                                           'int main() { return crc8("a", 1) == 97 ? 0 : 1; }\n')
    ctx = _ctx(tmp_path, ("c", "cpp"))
    cpp, c = adapter("cpp", ctx), adapter("c", ctx)
    assert cpp.unit("src").test_files == ["test/vec_test.cpp"]
    assert c.unit("lib").test_files == ["tests/test_crc.cpp"]
    assert "lib" not in cpp.units()


GEN_HEAD = "/*\n * Generated by asn1c-1.0.18 (http://lionet.info/asn1c)\n */\n"


def test_generated_rules_asn1c_skeletons_and_handwritten_bridge(tmp_path: Path):
    """asn1c runtime skeletons are generated with no marker of their own; a hand-written file
    (Doxygen header) next to generated ones is not swallowed by the directory rule."""
    walkin = "/*-\n * Copyright (c) 2003-2017 Lev Walkin <vlm@lionet.info>. All rights reserved.\n" \
             " * Redistribution and modifications are permitted subject to BSD license.\n */\n"
    _write(tmp_path, "asn/lib_src/OCTET_STRING.c", walkin + '#include <asn_internal.h>\n'
                                                   "int OCTET_STRING_free(void *p) { return 0; }\n")
    _write(tmp_path, "asn/lib_src/per_decoder.c", '#include <asn_application.h>\n'
                                                  "int per_decode(int v) { return v; }\n")
    _write(tmp_path, "asn/lib_src/BIT_STRING.c", '#include "asn_internal.h"\n'
                                                 "int bit_string_len(int v) { return v; }\n")
    _write(tmp_path, "asn/lib_src/asn_internal.h", "int asn_debug(int v);\n")
    _write(tmp_path, "asn/lib_src/asn_application.h", "int asn_app(int v);\n")
    for i in range(4):
        _write(tmp_path, f"codec/src/gen{i}.c", GEN_HEAD + f"int gen{i}(void) "
                                                         "{ return 0; }\n")
    bridge = "// Document\n/**\n * @file codec/src/codec_bridge.c\n" \
             " * @brief encode/decode interface\n */\n#include <string.h>\n" \
             "int codec_bridge_msgenc(int v) { return v; }\n"
    _write(tmp_path, "codec/src/codec_bridge.c", bridge)
    _write(tmp_path, "codec/src/codec_plain.c", "int plain_fn(void) { return 1; }\n")
    ctx = _ctx(tmp_path)
    c = adapter("c", ctx)
    for rel in ("asn/lib_src/OCTET_STRING.c", "asn/lib_src/per_decoder.c",
                "asn/lib_src/BIT_STRING.c"):
        assert c.is_generated(ctx.read(rel), rel), rel
    assert not c.is_generated(ctx.read("codec/src/codec_bridge.c"),
                              "codec/src/codec_bridge.c")
    assert c.is_generated(ctx.read("codec/src/codec_plain.c"),
                          "codec/src/codec_plain.c")         # the dir rule still applies
    st = {t.file: t.status for t in c.select_targets(tmp_path, c.units())}
    assert st["codec/src/codec_bridge.c"] == "ok"
    assert {st[f"asn/lib_src/{n}"] for n in ("OCTET_STRING.c", "per_decoder.c",
                                              "BIT_STRING.c")} == {"generated"}
    # a skeleton name alone (no asn1c runtime include) is no skeleton
    from codocs.lang.cc import asn1c_skeleton

    assert not asn1c_skeleton("x/INTEGER.c", "int f(void) { return 0; }\n")


def _deep_include_repo(root: Path) -> Path:
    _write(root, "lib/asn/src/any.c", '#include "asn_system.h"\nint any_len(void) '
                                      "{ return DBG_LEVEL; }\n")
    _write(root, "lib/asn/include/asn_system.h", '#include "debug.h"\nint any_len(void);\n')
    _write(root, "include/dbg/debug.h", "#define DBG_LEVEL 3\n")
    return root


def test_include_dirs_follow_included_headers(tmp_path: Path):
    """a repo header's own #include "x.h" puts x.h's directory on the include path."""
    c = adapter("c", _ctx(_deep_include_repo(tmp_path)))
    dirs = c.include_dirs("lib/asn/src")
    assert "lib/asn/include" in dirs and "include/dbg" in dirs


@needs_cc
def test_runner_compiles_with_transitively_included_headers(tmp_path: Path):
    root = _deep_include_repo(tmp_path / "repo")
    a, r = _runner(root, "c", tmp_path / "out")
    res = run(r.test("lib/asn/src", overlay={"lib/asn/src/any.c": a.read("lib/asn/src/any.c")},
                     compile_only=True))
    assert res.build_ok, res.build_errors


def test_fix_imports_accepts_gcc_curly_quotes():
    """GCC with NLS under a UTF-8 locale quotes names as ‘x’; both forms are repaired."""
    from codocs.lang.ccrun import CcRunner

    c, cpp = adapter("c"), adapter("cpp")
    src = '#include "codocs_test.h"\nint f(void) { return (int)strlen("x"); }\n'
    for err in ("src/x_codocs_test.c:2:31: error: implicit declaration of function ‘strlen’",
                "src/x_codocs_test.c:2:31: error: implicit declaration of function 'strlen'"):
        got = c.fix_imports(src, [err], "x_codocs_test.c", c.fix_allowed([]))
        assert got is not None and "#include <string.h>" in got, err
    got = c.fix_imports("int32_t v;\n", ["a.c:1:1: error: unknown type name ‘int32_t’"],
                        "a.c", c.fix_allowed([]))
    assert got is not None and "#include <stdint.h>" in got
    got = cpp.fix_imports("std::string s;\n", ["a.cpp:1:6: error: ‘string’ is not a member of "
                                               "‘std’"], "a.cpp", cpp.fix_allowed([]))
    assert got is not None and "#include <string>" in got
    env = CcRunner._tool_env()
    assert env["LC_ALL"] == "C" and env["LANG"] == "C"


def test_cpp_namespaces_are_not_part_of_receivers(tmp_path: Path):
    """`int geo::Vec::dot()` in a .cpp without a `namespace geo {}` block is MethodDef Vec.dot,
    `geo::detail::sq` is FunctionDef sq; the context keeps header namespaces."""
    from codocs.specforge.docfmt import expected_sections, missing_sections

    _write(tmp_path, "include/geo/vec.hpp", "#pragma once\nnamespace geo {\n"
                                            "namespace detail { int sq(int v); }\n"
                                            "class Vec {\npublic:\n    Vec(int x, int y) : x_(x), "
                                            "y_(y) {}\n    int dot(const Vec &o) const;\n"
                                            "    Vec scale(int k) const;\n    int len2() const;\n"
                                            "private:\n    int x_, y_;\n};\n}\n")
    src = ('#include "geo/vec.hpp"\n\nint geo::detail::sq(int v) { return v * v; }\n\n'
           "int geo::Vec::dot(const geo::Vec &o) const { return x_ * o.x_ + y_ * o.y_; }\n\n"
           "geo::Vec geo::Vec::scale(int k) const { return geo::Vec(x_ * k, y_ * k); }\n\n"
           "using namespace geo;\nint Vec::len2() const { return detail::sq(x_) + "
           "detail::sq(y_); }\n")
    _write(tmp_path, "src/vec.cpp", src)
    cpp = adapter("cpp", _ctx(tmp_path, ("cpp",)))
    want = [("func", "sq"), ("method", "Vec.dot"), ("method", "Vec.scale"),
            ("method", "Vec.len2")]
    assert _kinds(cpp.decls(src)) == want
    assert expected_sections(src, "cpp", cpp) == [("FunctionDef", "sq"),
                                                  ("MethodDef", "Vec.dot"),
                                                  ("MethodDef", "Vec.scale"),
                                                  ("MethodDef", "Vec.len2")]
    doc = "## FunctionDef sq\n## MethodDef Vec.dot\n## MethodDef Vec.scale\n## MethodDef Vec.len2\n"
    assert missing_sections(doc, src, "cpp", cpp) == []
    # the unbound adapter keeps no state of the repo analysed last (it only knows `geo` from
    # the bound one, which docgen passes)
    assert adapter("cpp").repo_namespaces() == frozenset()
    # `using namespace` in the file is enough on its own
    assert _kinds(adapter("cpp").decls("using namespace zz;\nint zz::Q::m() const "
                                       "{ return 0; }\n"))[-1] == ("method", "Q.m")
    ctx_text = cpp.package_context(tmp_path, cpp.unit("src"), "src/vec.cpp")
    assert "namespace geo::detail { int sq(int v); }" in ctx_text
    assert "namespace geo {\nclass Vec {" in ctx_text
    facts = cpp.graph_facts(tmp_path, "src/vec.cpp", src, cpp.ctx)
    assert {f.name for f in facts.funcs} == {"sq", "Vec.dot", "Vec.scale", "Vec.len2"}


@needs_cc
def test_runner_coverage_under_a_deep_path(tmp_path: Path):
    """gcov output names must not encode the whole source path (NAME_MAX)."""
    root = tmp_path.joinpath(*(["d" * 60] * 4), "repo")
    _write(root, "src/m.h", "int m(int v);\n")
    _write(root, "src/m.c", '#include "m.h"\nint m(int v) {\n    return v + 1;\n}\n')
    _write(root, "src/test_m.c", '#include "m.h"\nint main(void) { return m(1) == 2 ? 0 : 1; }\n')
    assert len(str(root / "src/m.c")) > 260
    a, r = _runner(root, "c", tmp_path / "out")
    res = run(r.test("src", cover=True))
    assert res.tests == {"src/test_m.c": "pass"}
    assert res.coverage_error == "" and res.coverage
    assert file_coverage(res.coverage, "src/m.c")[0] == 1.0


def test_mutation_sites_skip_compiled_out_code(tmp_path: Path):
    src = ("int live(int a) { return a + 1; }\n"
           "#if 0\nint dead(int a) { return a * 2; }\n#endif\n"
           "#ifdef UNIT_TEST\nint ut_only(int a) { return a - 3; }\n#else\n"
           "int prod_only(int a) { return a - 4; }\n#endif\n"
           "#ifdef FEATURE_X\nint feat(int a) { return a + 5; }\n#endif\n")
    unbound = adapter("c")
    assert {m.line for m in unbound.mutation_sites(src)} == {1, 6, 8, 11}   # only #if 0 known
    _write(tmp_path, "a/a.c", src)
    _write(tmp_path, "a/cfg.h", "#define FEATURE_X 1\n")
    c = adapter("c", _ctx(tmp_path))
    lines = {m.line for m in c.mutation_sites(src)}
    assert lines == {1, 8, 11}          # UNIT_TEST is defined nowhere; FEATURE_X in cfg.h


def test_overloads_keep_every_body_call(tmp_path: Path):
    src = ("int clampk(int k);\nint widen(int k);\n"
           "struct Shape {\n    int scale(int k);\n    int scale(int kx, int ky);\n};\n"
           "int Shape::scale(int k) { return clampk(k); }\n"
           "int Shape::scale(int kx, int ky) { return widen(kx) + ky; }\n")
    _write(tmp_path, "src/shape.cpp", src)
    cpp = adapter("cpp", _ctx(tmp_path, ("cpp",)))
    facts = cpp.graph_facts(tmp_path, "src/shape.cpp", src, cpp.ctx)
    assert [f.name for f in facts.funcs] == ["Shape.scale"]
    assert sorted(c.name for c in facts.calls["Shape.scale"]) == ["clampk", "widen"]
