from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codocs.golang.source import (
    Decl,
    Import,
    add_imports,
    apply_cgo_header,
    cgo_header,
    extract_go_code,
    function_spans,
    identifiers,
    imports,
    is_generated,
    mask,
    package_name,
    remove_functions,
    remove_imports,
    top_level_decls,
    type_definitions,
)
from codocs.golang.source import test_functions as find_tests

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
GOFMT = shutil.which("gofmt")

TRICKY = r"""// Package p is tricky.
package p

import (
	"fmt"
	yaml "gopkg.in/yaml.v2"
	. "strings"
	_ "embed"
	"github.com/redis/go-redis/v9"
)

/* Map applies f.
   It is generic. */
func Map[T any, U any](xs []T, f func(T) U) []U {
	s := "}{ // not a comment \" func Fake() {"
	q := "\\"
	r := '\''
	b := '{'
	c := '"'
	raw := `
}
func Fake() {
`
	_ = func() {
		if true {
		}
	}
	_, _, _, _, _, _ = s, q, r, b, c, raw
	return nil
}

func (s *List[T]) Push(v T) (err interface{ Error() string }) { return }

func (List[T]) Len() int { return 0 }

func Weird() map[string]interface{} { return nil }

func Fn() func(int) (int, error) { return nil }

func St() struct{ A int } { return struct{ A int }{} }

// Asm has no body.
func Asm(x int) int

type A = B

// Grouped types.
type (
	// X is x
	X int
	Y[T any] struct {
		v T
	}
)

const (
	K0 = iota
	K1
	_
	K3
)

var a, b, _ = 1, 2, 3

// f is a closure.

var f = func() int {
	return 1
}
"""


def _decl(decls: list[Decl], qualname: str) -> Decl:
    return next(d for d in decls if d.qualname == qualname)


def _gofmt_ok(src: str, tmp_path: Path) -> bool:
    p = tmp_path / "check.go"
    p.write_text(src)
    return subprocess.run([GOFMT, "-e", str(p)], capture_output=True).returncode == 0


# -- mask -----------------------------------------------------------------


def test_mask_keeps_length_newlines_and_delimiters():
    m = mask(TRICKY)
    assert len(m) == len(TRICKY)
    assert [i for i, c in enumerate(m) if c == "\n"] == [
        i for i, c in enumerate(TRICKY) if c == "\n"
    ]
    assert "Fake" not in m
    assert "not a comment" not in m
    assert "Map applies" not in m
    assert m.count("func ") == TRICKY.count("func ") - 2


def test_mask_strings_runes_and_comments():
    src = "x := \"a\\\"b{\" + `c}` // tail {\ny := '\\'' + '{' /* } */ + \"\\\\\"\n"
    m = mask(src)
    assert m == "x := \"     \" + `  ` //       \ny := '  ' + ' ' /*   */ + \"  \"\n"
    assert "{" not in m and "}" not in m


def test_mask_unterminated_literals_stop_at_newline():
    src = 'a := "open\nb := 1 // c\n'
    m = mask(src)
    assert m.splitlines()[0] == 'a := "    '
    assert m.splitlines()[1] == "b := 1 //  "


def test_mask_block_comment_spanning_lines_and_raw_string():
    src = "/* a\n{ */ x := `\n}\n` + y\n"
    m = mask(src)
    assert m == "/*  \n  */ x := `\n \n` + y\n"


# -- declarations ---------------------------------------------------------


def test_top_level_decls_kinds_and_order():
    decls = top_level_decls(TRICKY)
    assert [(d.kind, d.qualname) for d in decls] == [
        ("func", "Map"),
        ("method", "List.Push"),
        ("method", "List.Len"),
        ("func", "Weird"),
        ("func", "Fn"),
        ("func", "St"),
        ("func", "Asm"),
        ("type", "A"),
        ("type", "X"),
        ("type", "Y"),
        ("const", "K0"),
        ("const", "K1"),
        ("const", "K3"),
        ("var", "a"),
        ("var", "b"),
        ("var", "f"),
    ]


def test_func_spans_doc_comments_and_signatures():
    decls = top_level_decls(TRICKY)
    m = _decl(decls, "Map")
    assert TRICKY[m.start :].startswith("/* Map applies f.")
    assert TRICKY[: m.end].endswith("return nil\n}")
    assert m.signature == "func Map[T any, U any](xs []T, f func(T) U) []U"
    assert TRICKY.splitlines()[m.line - 1].startswith("func Map")
    assert TRICKY.splitlines()[m.end_line - 1] == "}"
    push = _decl(decls, "List.Push")
    assert push.receiver == "List" and push.name == "Push"
    assert push.signature == "func (s *List[T]) Push(v T) (err interface{ Error() string })"
    assert TRICKY[push.start : push.end].endswith("{ return }")
    assert _decl(decls, "List.Len").receiver == "List"
    assert _decl(decls, "Weird").signature == "func Weird() map[string]interface{}"
    assert _decl(decls, "Fn").signature == "func Fn() func(int) (int, error)"
    st = _decl(decls, "St")
    assert st.signature == "func St() struct{ A int }"
    assert TRICKY[st.start : st.end] == "func St() struct{ A int } { return struct{ A int }{} }"
    asm = _decl(decls, "Asm")
    assert TRICKY[asm.start : asm.end] == "// Asm has no body.\nfunc Asm(x int) int"
    assert asm.signature == "func Asm(x int) int"


def test_grouped_decls_share_span():
    decls = top_level_decls(TRICKY)
    x, y = _decl(decls, "X"), _decl(decls, "Y")
    assert (x.start, x.end) == (y.start, y.end)
    assert TRICKY[x.start : x.end].startswith("// Grouped types.\ntype (")
    assert TRICKY[x.start : x.end].endswith("\t}\n)")
    assert x.signature == "type X int"
    assert y.signature == "type Y[T any] struct { v T }"
    assert y.line == x.line + 1
    assert _decl(decls, "A").signature == "type A = B"
    k0, k3 = _decl(decls, "K0"), _decl(decls, "K3")
    assert k0.signature == "const K0 = iota"
    assert k3.signature == "const K3"
    assert (k0.start, k0.end) == (k3.start, k3.end)
    assert k3.line == k0.line + 3
    a, b = _decl(decls, "a"), _decl(decls, "b")
    assert a.signature == b.signature == "var a, b, _ = 1, 2, 3"


def test_doc_comment_needs_adjacency():
    f = _decl(top_level_decls(TRICKY), "f")
    assert TRICKY[f.start :].startswith("var f = func() int {")
    assert TRICKY[: f.end].endswith("return 1\n}")
    assert f.signature == "var f = func() int {"


def test_long_type_signature_is_header_only():
    src = "package p\n\ntype Big struct {\n\tA int\n\tB int\n\tC int // c\n}\n\ntype I interface {\n\tM()\n}\n"
    decls = top_level_decls(src)
    assert decls[0].signature == "type Big struct"
    assert decls[1].signature == "type I interface { M() }"


def test_array_type_and_generic_type():
    src = "package p\n\ntype Arr [4]int\n\ntype Pair[K comparable, V any] struct{ k K; v V }\n"
    decls = top_level_decls(src)
    assert [d.name for d in decls] == ["Arr", "Pair"]
    assert decls[1].signature == "type Pair[K comparable, V any] struct{ k K; v V }"


def test_nested_closures_do_not_leak_decls():
    src = (
        "package p\n\nfunc Outer() {\n\tgo func() {\n\t\tdefer func() {}()\n\t}()\n"
        "\tvar inner = 1\n\ttype local int\n\t_ = inner\n}\n\nfunc After() {}\n"
    )
    assert [d.name for d in top_level_decls(src)] == ["Outer", "After"]


def test_package_name():
    assert package_name(TRICKY) == "p"
    assert package_name("// package fake\n/* package x */\npackage real_name\n") == "real_name"
    assert package_name("func main() {}\n") is None


# -- imports --------------------------------------------------------------


def test_imports_kinds_and_names():
    imps = imports(TRICKY)
    assert [(i.path, i.alias, i.name) for i in imps] == [
        ("fmt", None, "fmt"),
        ("gopkg.in/yaml.v2", "yaml", "yaml"),
        ("strings", ".", "."),
        ("embed", "_", "_"),
        ("github.com/redis/go-redis/v9", None, "redis"),
    ]
    assert imps[0].line == 5 and imps[4].line == 9


def test_import_name_heuristics():
    assert Import("gopkg.in/yaml.v2", None, 1).name == "yaml"
    assert Import("github.com/mattn/go-sqlite3", None, 1).name == "sqlite3"
    assert Import("k8s.io/api/core/v1", None, 1).name == "core"
    assert Import("v1", None, 1).name == "v1"
    assert Import("github.com/x/foo.bar", None, 1).name == "foo"
    assert Import("net/http", "h", 1).name == "h"


def test_single_imports():
    src = 'package p\n\nimport "fmt"\nimport str "strings"\n\nfunc f() { import_ := 1; _ = import_ }\n'
    assert [(i.path, i.alias, i.line) for i in imports(src)] == [
        ("fmt", None, 3),
        ("strings", "str", 4),
    ]


def test_remove_imports_from_group():
    src = 'package p\n\nimport (\n\t"fmt"\n\t"os" // os\n\tstr "strings"\n)\n\nfunc f() {}\n'
    out = remove_imports(src, {"os", "strings"})
    assert out == 'package p\n\nimport (\n\t"fmt"\n)\n\nfunc f() {}\n'
    assert remove_imports(src, {"nope"}) == src


def test_remove_imports_trims_blank_run_edges():
    src = 'package p\n\nimport (\n\t"fmt"\n\n\t"example.com/a"\n\n\t"example.com/b"\n)\n'
    assert remove_imports(src, {"example.com/b"}) == (
        'package p\n\nimport (\n\t"fmt"\n\n\t"example.com/a"\n)\n'
    )
    assert remove_imports(src, {"example.com/a"}) == (
        'package p\n\nimport (\n\t"fmt"\n\n\t"example.com/b"\n)\n'
    )
    assert remove_imports(src, {"fmt"}) == (
        'package p\n\nimport (\n\t"example.com/a"\n\n\t"example.com/b"\n)\n'
    )


def test_remove_imports_empties_block():
    src = 'package p\n\nimport (\n\t"fmt"\n)\n\nfunc f() {}\n'
    assert remove_imports(src, {"fmt"}) == "package p\n\nfunc f() {}\n"
    single = 'package p\n\nimport "fmt"\nimport "os"\n\nfunc f() {}\n'
    assert remove_imports(single, {"fmt"}) == 'package p\n\nimport "os"\n\nfunc f() {}\n'
    assert remove_imports(single, {"fmt", "os"}) == "package p\n\nfunc f() {}\n"


def test_remove_imports_same_line_specs():
    src = 'package p\n\nimport ("fmt"; "os")\n'
    assert remove_imports(src, {"fmt"}) == 'package p\n\nimport ("os")\n'


def test_add_imports_into_group():
    src = 'package p\n\nimport (\n\t"fmt"\n)\n\nfunc f() {}\n'
    out = add_imports(src, [Import("os", None, 0), Import("strings", "str", 0)])
    assert out == 'package p\n\nimport (\n\t"fmt"\n\t"os"\n\tstr "strings"\n)\n\nfunc f() {}\n'
    assert add_imports(out, [Import("fmt", None, 0)]) == out
    runs = 'package p\n\nimport (\n\t"fmt"\n\t"os"\n\n\t"example.com/z"\n)\n'
    assert add_imports(runs, [Import("example.com/a", None, 0), Import("io", None, 0)]) == (
        'package p\n\nimport (\n\t"fmt"\n\t"io"\n\t"os"\n\n\t"example.com/a"\n'
        '\t"example.com/z"\n)\n'
    )
    tail = add_imports(runs, [Import("example.com/zz", None, 0), Import("zz", None, 0)])
    assert tail == (
        'package p\n\nimport (\n\t"fmt"\n\t"os"\n\t"zz"\n\n\t"example.com/z"\n'
        '\t"example.com/zz"\n)\n'
    )


def test_add_imports_converts_single_and_creates():
    src = 'package p\n\nimport "fmt"\n\nfunc f() {}\n'
    out = add_imports(src, [Import("os", None, 0)])
    assert out == 'package p\n\nimport (\n\t"fmt"\n\t"os"\n)\n\nfunc f() {}\n'
    bare = "package p\n\nfunc f() {}\n"
    assert add_imports(bare, [Import("os", None, 0)]) == (
        'package p\n\nimport "os"\n\nfunc f() {}\n'
    )
    two = add_imports("package p\nfunc f() {}\n", [Import("os", None, 0), Import("io", None, 0)])
    assert two == 'package p\n\nimport (\n\t"io"\n\t"os"\n)\n\nfunc f() {}\n'


@pytest.mark.skipif(GOFMT is None, reason="gofmt not installed")
def test_import_edits_stay_gofmt_clean(tmp_path):
    src = 'package p\n\nimport (\n\t"fmt"\n\t"os"\n)\n\nfunc f() { fmt.Println(os.Args) }\n'
    for out in (
        remove_imports(src, {"os"}),
        remove_imports(src, {"os", "fmt"}),
        add_imports(src, [Import("io", None, 0)]),
        add_imports(src, [Import("bytes", None, 0), Import("zz/x", None, 0)]),
        add_imports('package p\n\nimport "os"\n', [Import("io", None, 0)]),
        add_imports("package p\n\nfunc f() {}\n", [Import("io", "_", 0)]),
    ):
        p = tmp_path / "x.go"
        p.write_text(out)
        res = subprocess.run([GOFMT, "-l", str(p)], capture_output=True, text=True)
        assert res.returncode == 0 and res.stdout == "", out


# -- tests and function removal --------------------------------------------


def test_test_functions_rules():
    src = """package p

import (
	tt "testing"
)

func TestA(t *tt.T) {}

func Test(t *tt.T) {}

func Testlower(t *tt.T) {}

func TestÄrger(t *tt.T) {}

func TestMulti(
	t *tt.T,
) {
}

func TestUnnamed(*tt.T) {}

func BenchmarkB(b *tt.B) {}

func ExampleC() {}

func FuzzD(f *tt.F) {}

func TestWrongType(t *testing.T) {}

func TestTwo(t *tt.T, x int) {}

func TestResult(t *tt.T) int { return 0 }

func TestGeneric[T any](t *tt.T) {}

func (s S) TestMethod(t *tt.T) {}

var s = "func TestInString(t *tt.T) {}"
"""
    assert find_tests(src) == ["TestA", "Test", "TestÄrger", "TestMulti", "TestUnnamed"]


def test_test_functions_dot_import_and_missing_import():
    src = 'package p\n\nimport . "testing"\n\nfunc TestX(t *T) {}\n'
    assert find_tests(src) == ["TestX"]
    assert find_tests("package p\n\nfunc TestX(t *testing.T) {}\n") == []


def test_function_spans_and_remove_functions():
    src = """package p

import "testing"

// TestA doc
// more doc
func TestA(t *testing.T) {
	s := "}"
	_ = s
}

func helper() int { return 1 }

/* TestB doc */
func TestB(t *testing.T) {
	_ = func() {}
}

func TestC(t *testing.T) {}

func (s S) TestA() {}
"""
    spans = function_spans(src)
    assert set(spans) == {"TestA", "TestB", "TestC", "helper"}
    a0, a1 = spans["TestA"]
    assert src[a0:a1].startswith("// TestA doc\n// more doc\nfunc TestA")
    assert src[a0:a1].endswith("_ = s\n}")
    out = remove_functions(src, {"TestA", "TestB"})
    assert out == (
        'package p\n\nimport "testing"\n\nfunc helper() int { return 1 }\n\n'
        "func TestC(t *testing.T) {}\n\nfunc (s S) TestA() {}\n"
    )
    assert find_tests(out) == ["TestC"]
    tail = remove_functions(src, {"TestC"})
    assert "TestC" not in tail and "\n\n\n" not in tail
    assert remove_functions(src, {"Nope"}) == src


def test_remove_last_function_keeps_single_trailing_newline():
    src = "package p\n\nfunc A() {}\n\n// B doc\nfunc B() {}\n"
    assert remove_functions(src, {"B"}) == "package p\n\nfunc A() {}\n"


def test_remove_functions_with_multiple_inits():
    src = "package p\n\nfunc init() {}\n\nfunc keep() {}\n\nfunc init() { x() }\n"
    assert remove_functions(src, {"init"}) == "package p\n\nfunc keep() {}\n"


# -- misc -----------------------------------------------------------------


def test_identifiers_skip_literals_comments_keywords_and_numbers():
    src = 'package p\n\n// Ghost\nfunc F(x int) int { return x + 0x1F + 1e5 + len("Hidden") }\n'
    ids = identifiers(src)
    assert {"p", "F", "x", "int", "len"} <= ids
    assert not ids & {"Ghost", "Hidden", "func", "return", "package", "x1F", "e5"}


def test_is_generated():
    assert is_generated("// Code generated by protoc-gen-go. DO NOT EDIT.\n\npackage p\n")
    assert is_generated("// Copyright x\n\n// Code generated by mockgen. DO NOT EDIT.\npackage p\n")
    assert not is_generated("package p\n\n// Code generated by x. DO NOT EDIT.\n")
    assert not is_generated("// Code generated by x.\npackage p\n")


def test_extract_go_code():
    reply = "Here:\n```go\npackage a\n```\nand\n```golang\npackage b\n\nfunc F() {}\n```\n"
    assert extract_go_code(reply) == "package b\n\nfunc F() {}\n"
    assert extract_go_code("```\npackage c\n```") == "package c\n"
    assert extract_go_code("  package d  ") == "package d\n"
    assert extract_go_code("```go\npackage e\nfunc F() {}") == "package e\nfunc F() {}\n"
    assert extract_go_code("```bash\nls\n```\npackage f") == "```bash\nls\n```\npackage f\n"


# -- fixture --------------------------------------------------------------


def test_fixture_calc_and_stats():
    calc = (FIXTURE / "calc" / "calc.go").read_text()
    assert [(d.kind, d.qualname) for d in top_level_decls(calc)] == [
        ("var", "ErrEmpty"),
        ("const", "Scale"),
        ("func", "Clamp"),
        ("func", "Sum"),
        ("func", "Normalize"),
    ]
    decls = {d.name: d for d in top_level_decls(calc)}
    assert decls["Clamp"].signature == "func Clamp(v, lo, hi int) int"
    assert decls["Sum"].signature == "func Sum(values []int) (int, error)"
    assert decls["ErrEmpty"].signature == 'var ErrEmpty = errors.New("calc: empty input")'
    assert calc[decls["Clamp"].start :].startswith("// Clamp limits v")
    assert [i.path for i in imports(calc)] == ["errors", "strings"]
    stats = (FIXTURE / "calc" / "stats.go").read_text()
    sd = top_level_decls(stats)
    assert [(d.kind, d.qualname) for d in sd] == [
        ("type", "Stats"),
        ("method", "Stats.Add"),
        ("method", "Stats.Mean"),
    ]
    assert sd[0].signature == "type Stats struct"
    assert sd[1].signature == "func (s *Stats) Add(v int)"
    assert sd[0].line == 4 and sd[0].end_line == 8
    assert not is_generated(calc)


def test_fixture_tests_and_shapes():
    tests = (FIXTURE / "calc" / "calc_test.go").read_text()
    assert find_tests(tests) == ["TestClamp", "TestSum", "TestNormalize", "TestKnownBroken"]
    out = remove_functions(tests, {"TestKnownBroken", "TestSum"})
    assert find_tests(out) == ["TestClamp", "TestNormalize"]
    assert "known broken" not in out and "baselines must exclude" not in out
    shapes = (FIXTURE / "shapes" / "shapes.go").read_text()
    assert package_name(shapes) == "shapes"
    assert [d.qualname for d in top_level_decls(shapes)] == [
        "Shape",
        "Rect",
        "Rect.Area",
        "Rect.Name",
        "Classify",
        "Largest",
    ]
    assert top_level_decls(shapes)[1].signature == "type Rect struct{ W, H float64 }"
    assert top_level_decls(shapes)[0].signature == "type Shape interface"


@pytest.mark.skipif(GOFMT is None, reason="gofmt not installed")
def test_fixture_edits_parse(tmp_path):
    tests = (FIXTURE / "calc" / "calc_test.go").read_text()
    out = remove_functions(tests, set(find_tests(tests)))
    out = remove_imports(out, {"testing"})
    assert out.strip() == "package calc"
    assert _gofmt_ok(out, tmp_path)
    assert _gofmt_ok(TRICKY, tmp_path)


CGO_LONE = """package a

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

CGO_FIRST = """package a

// static int twice(int x) { return 2 * x; }
import "C"

import (
\t"os"
)

func Twice(x int) int { _ = os.Args; return fmt.Sprint(int(C.twice(C.int(x)))) }
"""


def test_add_imports_never_joins_cgo_import():
    out = add_imports(CGO_LONE, [Import("unsafe")])
    assert '*/\nimport "C"\n\nimport "unsafe"\n\nfunc Len' in out
    out = add_imports(CGO_FIRST, [Import("fmt")])
    assert '// static int twice(int x) { return 2 * x; }\nimport "C"\n' in out
    assert 'import (\n\t"fmt"\n\t"os"\n)' in out
    two = add_imports(CGO_LONE, [Import("unsafe"), Import("fmt")])
    assert 'import "C"\n\nimport (\n\t"fmt"\n\t"unsafe"\n)' in two
    assert [i.path for i in imports(two)] == ["C", "fmt", "unsafe"]


def test_cgo_header_roundtrip():
    src = "//go:build linux\n\n" + CGO_LONE.replace("func Len", "import \"unsafe\"\n\nfunc Len")
    header = cgo_header(src)
    assert header.startswith("//go:build linux\n\n/*\n#include <stdlib.h>")
    assert header.endswith('*/\nimport "C"')
    assert cgo_header("package a\n\nimport \"fmt\"\n") == ""
    regen = ('package a\n\nimport (\n\t"C"\n\t"unsafe"\n)\n\n'
             'func Len(s string) int { return int(C.slen(C.CString(s))) + int(unsafe.Sizeof(s)) }\n')
    out = apply_cgo_header(regen, header)
    assert out.startswith("//go:build linux\n\npackage a\n\n/*\n#include <stdlib.h>")
    assert [i.path for i in imports(out)] == ["C", "unsafe"]
    assert out.count('"C"') == 1 and "static size_t slen" in out
    bogus = "package a\n\n// #include <nothing.h>\nimport \"C\"\n\nfunc F() {}\n"
    out = apply_cgo_header(bogus, header)
    assert "nothing.h" not in out and "static size_t slen" in out
    assert apply_cgo_header(regen, "") == regen


def test_type_definitions_full_text_without_comments():
    src = ("package a\n\n// T is long.\ntype T struct {\n\t// id\n\tID int `json:\"id\"`\n\n"
           "\tName string // the name\n\tTags []string\n}\n\ntype (\n\tA int\n\tI interface {\n"
           "\t\tM() error\n\t}\n)\n")
    defs = type_definitions(src)
    assert defs["T"] == ('type T struct {\n\tID int `json:"id"`\n\tName string\n'
                         '\tTags []string\n}')
    assert defs["A"] == "type A int"
    assert defs["I"] == "type I interface {\n\t\tM() error\n\t}"
