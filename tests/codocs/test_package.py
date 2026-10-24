from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codocs.golang.gotool import GoListPackage, GoRunner
from codocs.golang.package import (
    PackageInfo,
    Target,
    discover,
    fix_imports,
    package_context,
    package_identifiers,
    relevant_tests,
    select_targets,
    test_index,
)
from codocs.golang.source import Import, imports

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
CANDIDATES = {"TestClamp", "TestSum", "TestNormalize", "TestStatsAdd", "TestStatsMean"}


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    dst = tmp_path / "gomod"
    shutil.copytree(FIXTURE, dst)
    return dst


def _check_fixture_pkgs(pkgs: dict[str, PackageInfo]) -> None:
    assert sorted(pkgs) == ["calc", "shapes"]
    calc, shapes = pkgs["calc"], pkgs["shapes"]
    assert calc.name == "calc" and calc.import_path == "example.com/fixture/calc"
    assert sorted(calc.go_files) == ["calc/calc.go", "calc/stats.go"]
    assert sorted(calc.test_files) == ["calc/calc_test.go", "calc/stats_test.go"]
    assert calc.xtest_files == []
    assert shapes.go_files == ["shapes/shapes.go"]
    assert shapes.all_test_files == []


def test_package_info_props_and_roundtrip():
    assert CALC.all_test_files == ["calc/calc_test.go", "calc/stats_test.go"]
    assert CALC.dir_name == "calc"
    root = PackageInfo(rel="", import_path="m", name="m", go_files=["a.go"], test_files=[],
                       xtest_files=["a_x_test.go"])
    assert root.dir_name == "_root"
    assert root.all_test_files == ["a_x_test.go"]
    assert PackageInfo.from_dict(root.to_dict()) == root
    t = Target(pkg="calc", file="calc/calc.go", stem="calc", lines=42)
    assert t.status == "ok"
    assert Target.from_dict(t.to_dict()) == t


# -- discover -----------------------------------------------------------------


@needs_go
async def test_discover_go_list():
    pkgs = await discover(GoRunner(FIXTURE))
    _check_fixture_pkgs(pkgs)


@needs_go
async def test_discover_filters():
    runner = GoRunner(FIXTURE)
    assert list(await discover(runner, subtree="calc")) == ["calc"]
    assert list(await discover(runner, packages=["shapes"])) == ["shapes"]
    assert await discover(runner, packages=["cal"]) == {}


async def test_discover_static_fallback(mod: Path, monkeypatch):
    runner = GoRunner(mod)

    async def broken(pattern="./..."):
        raise OSError("no go")

    monkeypatch.setattr(runner, "list_packages", broken)
    (mod / "calc" / "x_test.go").write_text("package calc_test\n\nimport \"testing\"\n\n"
                                            "func TestX(t *testing.T) {}\n")
    pkgs = await discover(runner)
    assert sorted(pkgs) == ["calc", "shapes"]
    calc = pkgs["calc"]
    assert calc.name == "calc" and calc.import_path == "example.com/fixture/calc"
    assert calc.go_files == ["calc/calc.go", "calc/stats.go"]
    assert calc.test_files == ["calc/calc_test.go", "calc/stats_test.go"]
    assert calc.xtest_files == ["calc/x_test.go"]
    assert pkgs["shapes"].all_test_files == []
    assert list(await discover(runner, subtree="shapes")) == ["shapes"]


async def test_discover_empty_listing_falls_back():
    runner = GoRunner(FIXTURE)

    async def empty(pattern="./..."):
        return {}

    runner.list_packages = empty
    _check_fixture_pkgs(await discover(runner))


async def test_discover_keeps_errored_packages_with_files():
    runner = GoRunner(FIXTURE)
    seen = []

    async def listing(pattern="./..."):
        seen.append(pattern)
        return {
            "a": GoListPackage(rel="a", import_path="example.com/fixture/a", name="a",
                               go_files=["a/a.go"], error="a/a.go:3:1: syntax error"),
            "a/b": GoListPackage(rel="a/b", import_path="example.com/fixture/a/b", name="b",
                                 go_files=["a/b/b.go"]),
            "ab": GoListPackage(rel="ab", import_path="example.com/fixture/ab", name="ab",
                                go_files=["ab/ab.go"]),
            "c": GoListPackage(rel="c", import_path="example.com/fixture/c", name="",
                               error="no Go files"),
        }

    runner.list_packages = listing
    pkgs = await discover(runner, subtree="./x/", packages=["a"])
    assert seen == ["./x/..."]
    assert pkgs == {}
    pkgs = await discover(runner, packages=["a"])
    assert sorted(pkgs) == ["a", "a/b"]
    assert pkgs["a"].error.startswith("a/a.go")


# -- select_targets -----------------------------------------------------------


def test_select_targets_statuses(mod: Path):
    (mod / "calc" / "gen.go").write_text(
        "// Code generated by stringer; DO NOT EDIT.\n\npackage calc\n\nfunc G() {}\n")
    (mod / "calc" / "big.go").write_text(
        "package calc\n\nfunc Big() {\n" + "\tprintln()\n" * 40 + "}\n")
    (mod / "calc" / "doc.go").write_text("// Package calc does arithmetic.\npackage calc\n")
    (mod / "calc" / "consts.go").write_text("package calc\n\nconst (\n\tA = 1\n\tB = 2\n)\n")
    calc = PackageInfo(**{**CALC.to_dict(), "go_files": [
        "calc/stats.go", "calc/calc.go", "calc/gen.go", "calc/big.go", "calc/doc.go",
        "calc/consts.go"]})
    pkgs = {"shapes": SHAPES, "calc": calc}
    got = select_targets(mod, pkgs, max_file_lines=30)
    assert [(t.file, t.status) for t in got] == [
        ("calc/big.go", "too-large"),
        ("calc/calc.go", "too-large"),
        ("calc/consts.go", "no-funcs"),
        ("calc/doc.go", "no-decls"),
        ("calc/gen.go", "generated"),
        ("calc/stats.go", "ok"),
        ("shapes/shapes.go", "too-large"),
    ]
    got = select_targets(mod, pkgs)
    by_file = {t.file: t for t in got}
    assert by_file["calc/calc.go"].status == "ok"
    assert by_file["calc/calc.go"].lines == 41
    assert by_file["calc/calc.go"].stem == "calc"
    assert by_file["calc/calc.go"].pkg == "calc"
    assert by_file["calc/big.go"].status == "ok"

    capped = select_targets(mod, pkgs, max_files=2)
    assert [(t.file, t.status) for t in capped] == [
        ("calc/big.go", "ok"),
        ("calc/calc.go", "ok"),
        ("calc/doc.go", "no-decls"),
        ("calc/gen.go", "generated"),
    ]
    globbed = select_targets(mod, pkgs, files="calc/s*.go")
    assert [t.file for t in globbed] == ["calc/stats.go"]
    assert [t.file for t in select_targets(mod, pkgs, files="*/shapes.go")] == [
        "shapes/shapes.go"]


# -- context ------------------------------------------------------------------


def test_package_context_stats_references_scale():
    ctx = package_context(FIXTURE, CALC, "calc/stats.go")
    assert ctx.startswith("// calc/calc.go\n")
    assert "const Scale = 10" in ctx
    assert "Stats" not in ctx.replace("// calc/calc.go", "")
    assert "func Clamp(v, lo, hi int) int" in ctx
    assert ctx.index("const Scale = 10") < ctx.index("func Clamp")


def test_package_context_calc_excludes_itself():
    ctx = package_context(FIXTURE, CALC, "calc/calc.go")
    assert "// calc/stats.go" in ctx
    for own in ("Clamp", "Sum", "Normalize", "ErrEmpty", "Scale", "calc/calc.go"):
        assert own not in ctx
    assert "type Stats struct" in ctx
    assert "func (s *Stats) Add(v int)" in ctx


def test_package_context_referenced_first_and_truncated():
    ctx = package_context(FIXTURE, CALC, "calc/stats.go", max_chars=60)
    assert "const Scale = 10" in ctx
    assert ctx.rstrip("\n").endswith("// ... (truncated)")
    assert len(ctx) <= 60
    assert "Normalize" not in ctx
    assert package_context(FIXTURE, SHAPES, "shapes/shapes.go") == ""


def test_package_identifiers():
    assert package_identifiers(FIXTURE, CALC) == {
        "ErrEmpty", "Scale", "Clamp", "Sum", "Normalize", "Stats"}
    assert package_identifiers(FIXTURE, CALC, exclude_rel="calc/calc.go") == {"Stats"}
    assert package_identifiers(FIXTURE, SHAPES) == {"Shape", "Rect", "Classify", "Largest"}


# -- tests --------------------------------------------------------------------


def test_test_index():
    idx = test_index(FIXTURE, CALC)
    assert sorted(idx) == ["TestClamp", "TestKnownBroken", "TestNormalize", "TestStatsAdd",
                           "TestStatsMean", "TestSum"]
    assert idx["TestClamp"]["file"] == "calc/calc_test.go"
    assert idx["TestStatsMean"]["file"] == "calc/stats_test.go"
    assert "Clamp" in idx["TestClamp"]["idents"]
    assert "Sum" not in idx["TestClamp"]["idents"]
    assert idx["TestClamp"]["idents"] == sorted(idx["TestClamp"]["idents"])
    assert "Stats" in idx["TestStatsMean"]["idents"]
    assert "Mean" in idx["TestStatsMean"]["selectors"]
    assert "Mean" not in idx["TestStatsMean"]["idents"]
    assert "fails" not in idx["TestKnownBroken"]["idents"]
    assert test_index(FIXTURE, SHAPES) == {}


def test_relevant_tests_fixture():
    assert relevant_tests(FIXTURE, CALC, "calc/stats.go", CANDIDATES) == [
        "TestStatsAdd", "TestStatsMean"]
    assert relevant_tests(FIXTURE, CALC, "calc/calc.go", CANDIDATES) == [
        "TestClamp", "TestNormalize", "TestSum"]
    assert relevant_tests(FIXTURE, CALC, "calc/calc.go", {"TestClamp", "TestNope"}) == [
        "TestClamp"]
    assert relevant_tests(FIXTURE, CALC, "calc/calc.go", set()) == []


def test_relevant_tests_reference_rules(mod: Path):
    (mod / "calc" / "other_test.go").write_text(
        "package calc\n\nimport \"testing\"\n\n"
        "func TestUsesScale(t *testing.T) { _ = Scale }\n"
        "func TestMeanOnly(t *testing.T) { var x interface{ Mean() int }; _ = x }\n"
        "func TestMeanStats(t *testing.T) { s := Stats{}; _ = s.Mean() }\n"
        "// Stats Clamp in a comment\n"
        "func TestComment(t *testing.T) { _ = \"Clamp\" }\n"
    )
    calc = PackageInfo(**{**CALC.to_dict(),
                          "test_files": CALC.test_files + ["calc/other_test.go"]})
    cands = {"TestUsesScale", "TestMeanOnly", "TestMeanStats", "TestComment"}
    assert relevant_tests(mod, calc, "calc/calc.go", cands) == ["TestUsesScale"]
    assert relevant_tests(mod, calc, "calc/stats.go", cands) == ["TestMeanStats"]


# -- fix_imports --------------------------------------------------------------

SRC = 'package calc\n\nimport (\n\t"fmt"\n\t"strings"\n)\n\nfunc F() string {\n' \
      '\treturn strings.Repeat(strconv.Itoa(1), 2)\n}\n'
ALLOWED = [Import("fmt"), Import("strconv"), Import("strings"),
           Import("github.com/x/y/v2"), Import("github.com/z/w", "zw")]


def test_fix_imports_remove_and_add():
    errs = [
        "# example.com/fixture/calc",
        'calc/calc.go:4:2: "fmt" imported and not used',
        "calc/calc.go:9:27: undefined: strconv",
    ]
    out = fix_imports(SRC, errs, "calc.go", ALLOWED)
    assert out is not None
    assert sorted(i.path for i in imports(out)) == ["strconv", "strings"]
    assert "strconv.Itoa" in out


def test_fix_imports_alias_and_paths():
    src = 'package calc\n\nimport (\n\tz "github.com/x/y"\n\t"os"\n)\n\nfunc F() { _ = zw.A }\n'
    errs = [
        '/tmp/codocs/t1/ov2/calc.go:4:2: "github.com/x/y" imported as z and not used',
        "../../x/calc.go:8:16: undefined: zw",
        'other.go:5:2: "os" imported and not used',
    ]
    out = fix_imports(src, errs, "calc.go", ALLOWED)
    assert out is not None
    got = {(i.path, i.alias) for i in imports(out)}
    assert got == {("os", None), ("github.com/z/w", "zw")}


def test_fix_imports_version_suffix_and_legacy_message():
    src = 'package p\n\nimport "fmt"\n\nfunc F() { y.Do() }\n'
    errs = ['p/f.go:3:8: imported and not used: "fmt"', "p/f.go:5:12: undefined: y"]
    out = fix_imports(src, errs, "f.go", ALLOWED)
    assert out is not None
    assert [i.path for i in imports(out)] == ["github.com/x/y/v2"]


def test_fix_imports_nothing_applies():
    assert fix_imports(SRC, [], "calc.go", ALLOWED) is None
    assert fix_imports(SRC, ["calc/stats.go:9:27: undefined: strconv"], "calc.go",
                       ALLOWED) is None
    assert fix_imports(SRC, ["calc/calc.go:9:27: undefined: Foo"], "calc.go", ALLOWED) is None
    assert fix_imports(SRC, ["calc/calc.go:9:27: undefined: strings"], "calc.go",
                       ALLOWED) is None
    assert fix_imports(SRC, ['calc/calc.go:4:2: "os" imported and not used'], "calc.go",
                       ALLOWED) is None
    assert fix_imports(SRC, ["calc/calc.go:9:27: undefined: strconv"], "calc.go", []) is None


@needs_go
async def test_fix_imports_on_real_build_errors(mod: Path):
    runner = GoRunner(mod)
    src = (mod / "calc" / "calc.go").read_text()
    broken = src.replace('import (\n\t"errors"\n\t"strings"\n)',
                         'import (\n\t"errors"\n\t"fmt"\n)')
    res = await runner.test("calc", overlay={"calc/calc.go": broken}, compile_only=True)
    assert not res.build_ok
    fixed = fix_imports(broken, res.build_errors, "calc.go",
                        [Import("strings"), Import("fmt"), Import("errors")])
    assert fixed is not None
    assert sorted(i.path for i in imports(fixed)) == ["errors", "strings"]
    res = await runner.test("calc", overlay={"calc/calc.go": fixed}, compile_only=True)
    assert res.build_ok, res.build_errors


def test_selector_names_are_not_references(mod: Path):
    (mod / "calc" / "factory.go").write_text(
        "package calc\n\nfunc New() *Stats { return &Stats{} }\n")
    (mod / "calc" / "other_test.go").write_text(
        "package calc\n\nimport (\n\t\"errors\"\n\t\"testing\"\n)\n\n"
        "func TestMeanOnly(t *testing.T) {\n\ts := &Stats{}\n\tif s.Mean() != 0 {\n"
        "\t\tt.Fatal(errors.New(\"bad\"))\n\t}\n}\n"
        "func TestNew(t *testing.T) { _ = New() }\n")
    (mod / "calc" / "x_test.go").write_text(
        "package calc_test\n\nimport (\n\t\"testing\"\n\n\tc \"example.com/fixture/calc\"\n)\n\n"
        "func TestExternal(t *testing.T) { _ = c.New() }\n")
    calc = PackageInfo(**{**CALC.to_dict(),
                          "go_files": CALC.go_files + ["calc/factory.go"],
                          "test_files": CALC.test_files + ["calc/other_test.go"],
                          "xtest_files": ["calc/x_test.go"]})
    cands = {"TestMeanOnly", "TestNew", "TestExternal"}
    assert relevant_tests(mod, calc, "calc/factory.go", cands) == ["TestExternal", "TestNew"]
    assert relevant_tests(mod, calc, "calc/stats.go", cands) == ["TestMeanOnly"]
    idx = test_index(mod, calc)
    assert "New" in idx["TestMeanOnly"]["selectors"]
    assert "New" not in idx["TestMeanOnly"]["idents"]
    assert "New" in idx["TestExternal"]["idents"]


def test_package_context_shows_full_referenced_structs(mod: Path):
    (mod / "calc" / "model.go").write_text(
        "package calc\n\n// Ue is a UE context.\ntype Ue struct {\n\tSupi string // id\n"
        "\tRegState int\n\tCounters map[string]int\n\tGuami string\n}\n\n"
        "type Unused struct {\n\tA int\n\tB int\n\tC int\n\tD int\n}\n")
    (mod / "calc" / "handler.go").write_text(
        "package calc\n\nfunc Register(ue *Ue) { ue.RegState++ }\n")
    calc = PackageInfo(**{**CALC.to_dict(), "go_files": CALC.go_files + [
        "calc/handler.go", "calc/model.go"]})
    ctx = package_context(mod, calc, "calc/handler.go")
    assert ("type Ue struct {\n\tSupi string\n\tRegState int\n\tCounters map[string]int\n"
            "\tGuami string\n}") in ctx
    assert "type Unused struct\n" in ctx and "\tA int" not in ctx
    small = package_context(mod, calc, "calc/handler.go", max_chars=60)
    assert "type Ue struct\n" in small or small.endswith("// ... (truncated)\n")


async def test_discover_warns_on_go_list_failure(mod: Path, monkeypatch, caplog):
    runner = GoRunner(mod)

    async def failing(pattern="./..."):
        raise RuntimeError("go list ./...: go: go.mod requires go >= 1.99")

    monkeypatch.setattr(runner, "list_packages", failing)
    with caplog.at_level("WARNING"):
        pkgs = await discover(runner)
    assert sorted(pkgs) == ["calc", "shapes"]
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "requires go >= 1.99" in text and "static scan" in text


CGO_MISSING_UNSAFE = """package cg

/*
#include <stdlib.h>
static size_t slen(const char *s) { size_t n = 0; while (s[n]) n++; return n; }
*/
import "C"

func Len(s string) int {
\tcs := C.CString(s)
\tdefer C.free(unsafe.Pointer(cs))
\treturn int(C.slen(cs))
}
"""


@needs_go
@pytest.mark.skipif(shutil.which("cc") is None, reason="cgo needs a C compiler")
async def test_fix_imports_keeps_cgo_preamble_working(mod: Path):
    (mod / "cg").mkdir()
    (mod / "cg" / "c.go").write_text(CGO_MISSING_UNSAFE.replace(
        'import "C"\n', 'import "C"\n\nimport "unsafe"\n'))
    runner = GoRunner(mod, workers=2, timeout=120, work_dir=mod.parent / "work")
    res = await runner.test("cg", overlay={"cg/c.go": CGO_MISSING_UNSAFE}, compile_only=True)
    assert not res.build_ok and any("undefined: unsafe" in e for e in res.build_errors)
    fixed = fix_imports(CGO_MISSING_UNSAFE, res.build_errors, "c.go",
                        [Import("C"), Import("unsafe"), Import("fmt")])
    assert fixed is not None
    res = await runner.test("cg", overlay={"cg/c.go": fixed}, compile_only=True)
    assert res.build_ok, res.build_errors


def test_package_context_includes_imported_module_api(tmp_path):
    from codocs.golang.package import PackageInfo, package_context

    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    (tmp_path / "kv").mkdir()
    (tmp_path / "kv" / "kv.go").write_text(
        "package kv\n\nimport \"errors\"\n\n"
        "var Nil = errors.New(\"nil\")\n\n"
        "type Store struct {\n\tAddr string\n}\n\n"
        "func (s *Store) Ping() error { return nil }\n\n"
        "func Get(key string) (string, error) { return \"\", Nil }\n\n"
        "func Set(key, val string, ttl int) error { return nil }\n\n"
        "func helper() {}\n")
    (tmp_path / "kv" / "kv_test.go").write_text("package kv\n\nfunc FromTest() {}\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "app.go").write_text(
        "package app\n\nimport (\n\tstore \"example.com/m/kv\"\n\t\"fmt\"\n)\n\n"
        "func Load(k string) string {\n\tv, err := store.Get(k)\n"
        "\tif err == store.Nil {\n\t\treturn fmt.Sprint(\"none\")\n\t}\n"
        "\tvar s store.Store\n\t_ = s\n\treturn v\n}\n")
    pkg = PackageInfo(rel="app", import_path="example.com/m/app", name="app",
                      go_files=["app/app.go"], test_files=[], xtest_files=[])
    ctx = package_context(tmp_path, pkg, "app/app.go")
    assert "// package store (example.com/m/kv)" in ctx
    used_part = ctx.split("// package store")[1]
    assert used_part.index("func Get(key string) (string, error)") < used_part.index("func Set(")
    assert "Addr string" in ctx                      # selected type shown in full
    assert "func (s *Store) Ping() error" in ctx     # methods of a selected type
    assert "helper" not in ctx and "FromTest" not in ctx


def test_imported_api_ignores_stdlib_and_unused(tmp_path):
    from codocs.golang.package import PackageInfo, imported_api

    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "a.go").write_text('package a\n\nimport "strings"\n\nfunc F() string { return strings.ToUpper("x") }\n')
    pkg = PackageInfo(rel="a", import_path="example.com/m/a", name="a",
                      go_files=["a/a.go"], test_files=[], xtest_files=[])
    assert imported_api(tmp_path, pkg, "a/a.go") == ""
