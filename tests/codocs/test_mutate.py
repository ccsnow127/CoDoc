from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codocs.golang.mutate import Mutant, apply, mutation_sites, sample

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
GOFMT = shutil.which("gofmt")


def _wrap(body: str) -> str:
    return "package p\n\nfunc f() {\n" + body + "\n}\n"


def _ops(src: str) -> list[str]:
    return [m.operator for m in mutation_sites(src)]


def _gofmt_errors(sources: dict[str, str], tmp_path: Path) -> str:
    for name, text in sources.items():
        (tmp_path / name).write_text(text)
    res = subprocess.run([GOFMT, "-e", "-l", str(tmp_path)], capture_output=True, text=True)
    return res.stderr


def test_operator_set():
    src = _wrap(
        "\tif a == b || a != c && a < b && a <= b && a > b && a >= b {\n"
        "\t\tx := a + b - c*d/e%g\n\t\tx += 1\n\t\tx -= 1\n\t\tx++\n\t\tx--\n"
        "\t\t_ = true\n\t\t_ = false\n\t}"
    )
    assert _ops(src) == [
        "cmp: == -> !=",
        "logic: || -> &&",
        "cmp: != -> ==",
        "logic: && -> ||",
        "cmp: < -> <=",
        "logic: && -> ||",
        "cmp: <= -> <",
        "logic: && -> ||",
        "cmp: > -> >=",
        "logic: && -> ||",
        "cmp: >= -> >",
        "arith: + -> -",
        "arith: - -> +",
        "arith: * -> /",
        "arith: / -> *",
        "arith: % -> *",
        "assign: += -> -=",
        "assign: -= -> +=",
        "incdec: ++ -> --",
        "incdec: -- -> ++",
        "bool: true -> false",
        "bool: false -> true",
    ]


def test_mutant_fields_and_apply():
    src = _wrap("\treturn a < b")
    (m,) = mutation_sites(src)
    assert m.offset == src.index("<")
    assert m.id == f"m{m.offset}"
    assert m.line == 4
    assert (m.original, m.replacement) == ("<", "<=")
    assert apply(src, m) == _wrap("\treturn a <= b")
    assert Mutant.from_dict(m.to_dict()) == m
    with pytest.raises(ValueError):
        apply(src.replace("<", "> "), m)


def test_ids_are_stable():
    src = (FIXTURE / "calc" / "stats.go").read_text()
    assert [m.id for m in mutation_sites(src)] == [m.id for m in mutation_sites(src)]
    assert len({m.id for m in mutation_sites(src)}) == len(mutation_sites(src))


def test_strings_comments_and_runes_never_mutated():
    src = _wrap(
        '\ts := "a < b && c == d + 1" // x > y || true\n'
        "\tr := `x-- != y`\n\t/* a * b */\n\tc := '+'\n\t_, _, _ = s, r, c"
    )
    assert mutation_sites(src) == []


@pytest.mark.parametrize(
    "body",
    [
        "\tvar p *T\n\t_ = p",
        "\tx := *p\n\t_ = x",
        "\tvar xs []*T\n\t_ = xs",
        "\tvar m map[string]*T\n\t_ = m",
        "\tvar arr [4]*T\n\t_ = arr",
        "\tg := func(p *T) *T { return p }\n\t_ = g",
        "\t*p = 3",
        "\treturn *p",
        "\tch := make(chan *T)\n\t_ = ch",
        "\ty := (*T)(nil)\n\t_ = y",
        "\tz := v.(*T)\n\t_ = z",
        "\tvar q **T\n\t_ = q",
        "\tw := &T{}\n\t_ = w",
        "\tn := -1\n\t_ = n",
        "\treturn -x",
        "\tk := -a\n\t_ = k",
        "\tswitch {\n\tcase -1 < 0:\n\t}",
        "\tif !ok {\n\t}",
        "\tf(+1, -2)",
        "\tvar s = []int{-1, +2}\n\t_ = s",
    ],
)
def test_unary_and_pointer_stars_not_arith(body):
    src = (
        "package p\n\ntype T int\n\nfunc f(p *T, v any, a, b, x int, ok bool) *T {\n"
        + body
        + "\n}\n"
    )
    assert [m for m in _ops(src) if m.startswith("arith")] == []


def test_signatures_with_pointers_have_no_mutants():
    src = (
        "package p\n\nfunc f(p *T) {}\n\nfunc (s *S) M(m map[string]*T, xs []*T) *T { return nil }\n"
        "\ntype S struct {\n\tnext *S\n\tbuf  [4]*byte\n}\n"
    )
    assert mutation_sites(src) == []


def test_binary_star_forms():
    src = _wrap(
        "\ta := b * c\n\td := b*c + e*(f+g)\n\th := xs[i]*2\n\tk := g() * 2\n\t_, _, _ = a, d, h"
    )
    stars = [m.offset for m in mutation_sites(src) if m.original == "*"]
    assert len(stars) == 5
    assert [m.original for m in mutation_sites(src) if m.original != "*"] == ["+", "+"]
    assert _ops(_wrap("\tk := a * -b\n\t_ = k")) == ["arith: * -> /"]


def test_compound_tokens_never_split():
    src = _wrap(
        "\tch <- v\n\tv = <-ch\n\tx := a << 2 >> 1 &^ m\n\tx <<= 1\n\tx >>= 1\n\tx &^= 1\n"
        "\tx *= 2\n\tx /= 2\n\tx %= 2\n\tf(xs...)\n\ty := x != 0\n\t_ = y"
    )
    assert _ops(src) == ["cmp: != -> =="]


def test_selector_true_and_bool_identifiers():
    src = _wrap("\t_ = x.true\n\t_ = istrue\n\t_ = true")
    assert _ops(src) == ["bool: true -> false"]


def test_excluded_regions():
    src = (
        'package p\n\nimport "fmt"\n\nconst Big = 1 << 10 * 2\n\nvar (\n\tok = true\n\tn  = 1 + 2\n)\n\n'
        "var g = func() bool { return 1 < 2 }\n\n"
        "type Arr [2 * 3]int\n\nfunc f() {\n\tvar local = 1 + 2\n\tconst c = 3 - 1\n\t_ = local < c\n}\n"
    )
    ops = [(m.line, m.operator) for m in mutation_sites(src)]
    assert ops == [
        (14, "arith: * -> /"),
        (17, "arith: + -> -"),
        (18, "arith: - -> +"),
        (19, "cmp: < -> <="),
    ]


def test_methods_closures_and_generics():
    src = (
        "package p\n\nfunc Map[T any, U any](xs []T, f func(T) U) []U {\n"
        "\tout := make([]U, 0, len(xs)+1)\n"
        "\tfor i := 0; i < len(xs); i++ {\n\t\tout = append(out, f(xs[i]))\n\t}\n"
        "\tgo func() { _ = len(out) > 0 }()\n\treturn out\n}\n\n"
        "func (l *List[T]) Len() int { return l.n - 0 }\n"
    )
    assert _ops(src) == [
        "arith: + -> -",
        "cmp: < -> <=",
        "incdec: ++ -> --",
        "cmp: > -> >=",
        "arith: - -> +",
    ]


def test_sample_is_deterministic_and_ordered():
    src = (FIXTURE / "shapes" / "shapes.go").read_text()
    ms = mutation_sites(src)
    assert len(ms) >= 5
    s1 = sample(ms, 4, 7)
    assert s1 == sample(ms, 4, 7)
    assert len(s1) == 4 and [m.offset for m in s1] == sorted(m.offset for m in s1)
    assert sample(ms, 0, 7) == [] and sample(ms, -1, 7) == []
    assert sample(ms, len(ms) + 5, 1) == ms
    assert {m.id for m in sample(ms, 4, 1)} | {m.id for m in sample(ms, 4, 2)} <= {m.id for m in ms}


def test_fixture_mutants():
    calc = (FIXTURE / "calc" / "calc.go").read_text()
    assert [(m.line, m.operator) for m in mutation_sites(calc)] == [
        (16, "cmp: < -> <="),
        (19, "cmp: > -> >="),
        (27, "cmp: == -> !="),
        (32, "assign: += -> -="),
    ]
    stats = (FIXTURE / "calc" / "stats.go").read_text()
    assert [(m.line, m.operator) for m in mutation_sites(stats)] == [
        (12, "cmp: == -> !="),
        (12, "logic: || -> &&"),
        (12, "cmp: > -> >="),
        (15, "incdec: ++ -> --"),
        (16, "assign: += -> -="),
        (21, "cmp: == -> !="),
        (24, "arith: * -> /"),
        (24, "arith: / -> *"),
    ]
    shapes = (FIXTURE / "shapes" / "shapes.go").read_text()
    assert len(mutation_sites(shapes)) == 9


@pytest.mark.skipif(GOFMT is None, reason="gofmt not installed")
def test_all_fixture_mutants_parse(tmp_path):
    sources = {}
    for path in sorted(FIXTURE.rglob("*.go")):
        src = path.read_text()
        for m in mutation_sites(src):
            mutated = apply(src, m)
            assert mutated != src
            sources[f"{path.stem}_{m.id}.go"] = mutated
    assert len(sources) > 20
    assert _gofmt_errors(sources, tmp_path) == ""


@pytest.mark.skipif(GOFMT is None, reason="gofmt not installed")
def test_tricky_mutants_parse(tmp_path):
    src = """package p

import "fmt"

type T struct{ n int }

type List[E any] struct {
	items []*E
	n     int
}

func (l *List[E]) Push(v *E) *List[E] {
	l.items = append(l.items, v)
	l.n++
	return l
}

func Scale(p *T, m map[string]*T, xs []*T, k int) (int, error) {
	total := 0
	for i := 0; i < len(xs) && k > 0; i++ {
		total += xs[i].n*k + p.n*(k-1) - -k
		if v, ok := m[fmt.Sprint(i)]; ok || v != nil {
			total -= v.n % 3 / 2
		}
	}
	ch := make(chan *T, 1)
	ch <- p
	q := <-ch
	*q = T{n: total << 1 >> 1 &^ 4}
	done := false
	go func() { done = !done || total >= 1 }()
	return total * q.n, nil
}
"""
    ms = mutation_sites(src)
    assert ms
    sources = {f"t_{m.id}.go": apply(src, m) for m in ms}
    assert _gofmt_errors(sources, tmp_path) == ""


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_fixture_mutants_compile(tmp_path):
    work = tmp_path / "mod"
    shutil.copytree(FIXTURE, work)
    target = work / "calc" / "stats.go"
    src = target.read_text()
    for m in mutation_sites(src):
        target.write_text(apply(src, m))
        res = subprocess.run(
            ["go", "build", "./calc"],
            cwd=work,
            capture_output=True,
            text=True,
            env={**os.environ, "GOTOOLCHAIN": "local"},
        )
        assert res.returncode == 0, (m.operator, res.stderr)


NRF_URI = """package nf

import "strconv"

type Profile struct {
	Name string
	Port int
}

func uri(apiRoot string, host, id string, x int) string {
	s := apiRoot + "/nnrf-nfm/"
	s += "@" + host
	label := "UNKNOWN(" + strconv.Itoa(x) + ")"
	var suffix string
	suffix += id
	return s + label + suffix + host
}

func (p Profile) Key() string { return p.Name + ":" + strconv.Itoa(p.Port+1) }

func grid(n int, m [][2]int, i, j int) int {
	b := make([][4]*string, n)
	var c map[[2]string][2]*float64
	var d [3][4]*int
	_, _, _ = b, c, d
	return m[i][j] * 2
}
"""


def test_string_concatenation_and_nested_pointer_arrays_are_not_mutated():
    ops = [(m.line, m.operator) for m in mutation_sites(NRF_URI)]
    assert ops == [(19, "arith: + -> -"), (26, "arith: * -> /")]


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_nrf_mutants_compile(tmp_path):
    (tmp_path / "go.mod").write_text("module ex.com/nf\n\ngo 1.21\n")
    target = tmp_path / "uri.go"
    for m in mutation_sites(NRF_URI):
        target.write_text(apply(NRF_URI, m))
        res = subprocess.run(["go", "build", "."], cwd=tmp_path, capture_output=True, text=True,
                             env={**os.environ, "GOTOOLCHAIN": "local"})
        assert res.returncode == 0, (m.operator, res.stderr)
