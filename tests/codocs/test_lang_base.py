"""lang foundation: base dataclasses, tree-sitter helpers, registry and language detection."""

from __future__ import annotations

import re
import shutil
import sys
import types
from pathlib import Path

import pytest

from codocs.config import Settings
from codocs.golang.gotool import CoverBlock, GoTestResult
from codocs.golang.package import Target
from codocs.lang import (
    LANG_ORDER,
    LangTarget,
    Unit,
    adapter,
    adapter_for,
    base,
    detect_languages,
    registry,
)
from codocs.lang import treesitter as ts
from codocs.lang.base import (
    Decl,
    ImportSpec,
    LangAdapterBase,
    LangTestResult,
    RepoContext,
    omit_defaults,
    parse_fragments,
    prompt_spec,
)
from codocs.lang.go import GoAdapter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
needs_ts = pytest.mark.skipif(not ts.available(), reason="tree-sitter not installed")


def _write(root: Path, rel: str, text: str = "", data: bytes | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        p.write_bytes(data)
    else:
        p.write_text(text, encoding="utf-8")
    return p


# -- base dataclasses -------------------------------------------------------------------


def test_lang_order_and_display():
    assert LANG_ORDER == ("go", "c", "cpp", "java", "python")
    assert base.DISPLAY["cpp"] == "C++" and base.DISPLAY["python"] == "Python"
    assert base.TestResult is GoTestResult


def test_lang_of():
    assert base.lang_of(Target(pkg="a", file="a/a.go", stem="a", lines=1)) == "go"
    assert base.lang_of(LangTarget(pkg="a", file="a/a.py", stem="a.py", lines=1)) == "python"
    assert base.lang_of(types.SimpleNamespace(lang="java")) == "java"
    assert base.lang_of(object()) == "go"


def test_omit_defaults():
    d = {"pkg": "a", "lang": "go", "cov_note": "", "x": 1}
    assert omit_defaults(d, {"lang": "go", "cov_note": ""}) == {"pkg": "a", "x": 1}
    d2 = {"lang": "c", "cov_note": "coverage unavailable: x"}
    assert omit_defaults(d2, {"lang": "go", "cov_note": ""}) == d2


def test_lang_target_round_trip():
    t = LangTarget(pkg="src/pymod", file="src/pymod/calc.py", stem="calc.py", lines=10,
                   status="ok", lang="python")
    d = t.to_dict()
    assert d["lang"] == "python" and d["stem"] == "calc.py"
    assert LangTarget.from_dict(d) == t
    d.pop("lang")
    assert LangTarget.from_dict(d).lang == "python"
    assert Target.from_dict(t.to_dict()) == Target(pkg="src/pymod", file="src/pymod/calc.py",
                                                     stem="calc.py", lines=10)
    assert isinstance(LangTarget.from_dict({**d, "lang": "c"}), Target)


def test_unit_duck_compat():
    u = Unit(lang="c", rel="", name="repo", files=["a.c"], test_files=["test_a.c"])
    assert u.dir_name == "_root" and u.go_files == ["a.c"] and u.xtest_files == []
    assert u.all_test_files == ["test_a.c"]
    u2 = Unit(lang="python", rel="src/pymod", name="pymod", import_path="pymod",
              extra={"project_root": ""})
    assert u2.dir_name == "src/pymod"
    assert Unit.from_dict(u2.to_dict()) == u2
    assert "go_files" not in u2.to_dict()


@pytest.mark.parametrize("spec,bound", [
    (ImportSpec("a.b", kind="import"), "a"),
    (ImportSpec("a.b", alias="ab", kind="import"), "ab"),
    (ImportSpec("a.b", name="f", kind="from"), "f"),
    (ImportSpec("a.b", name="f", alias="g", kind="from"), "g"),
    (ImportSpec("com.x.Calc", kind="single"), "Calc"),
    (ImportSpec("com.x", kind="wildcard"), ""),
    (ImportSpec("com.x.Calc.clamp", kind="static"), "clamp"),
    (ImportSpec("com.x.Calc", name="clamp", kind="static"), "clamp"),
    (ImportSpec("Calc", kind="simple"), "Calc"),
    (ImportSpec("calc/calc.h", kind="quote"), ""),
    (ImportSpec("stdio.h", kind="angle"), ""),
])
def test_import_spec_bound(spec: ImportSpec, bound: str):
    assert spec.bound == bound
    assert ImportSpec.from_dict(spec.to_dict()) == spec


def test_lang_test_result():
    blocks = [CoverBlock("a.py", 1, 1, 1, 1, 1, 0)]
    r = LangTestResult(build_ok=True, tests={"t": "pass"}, coverage=blocks)
    g = GoTestResult(build_ok=True, tests={"t": "pass"}, coverage=blocks)
    assert r.to_dict() == g.to_dict()
    assert isinstance(r, GoTestResult) and r.passed == {"t"}
    r2 = LangTestResult(build_ok=True, coverage=None, coverage_error="JaCoCo cannot read it")
    d = r2.to_dict()
    assert d["coverage_error"] == "JaCoCo cannot read it" and d["coverage"] is None
    back = LangTestResult.from_dict(d)
    assert back == r2 and LangTestResult.from_dict(r.to_dict()) == r


def test_parse_fragments():
    text = "=== LANG ===\nPython\n=== RULES ===\n- a\n- b\n\n=== MARK ===\n#\n"
    assert parse_fragments(text) == {"LANG": "Python", "RULES": "- a\n- b\n", "MARK": "#"}
    assert parse_fragments("") == {}


@pytest.mark.parametrize("lang,fence,comment,noun,label,cdg,cdx", [
    ("go", "go", "//", "Go file", "Package", "cdg", "cdx"),
    ("python", "python", "#", "Python module", "Module", "_cdg", "_cdx"),
    ("java", "java", "//", "Java source file", "Package", "cdg", "cdx"),
    ("c", "c", "//", "C source file", "Unit", "cdg_", "cdx_"),
    ("cpp", "cpp", "//", "C++ source file", "Unit", "cdg_", "cdx_"),
])
def test_prompt_spec(lang, fence, comment, noun, label, cdg, cdx):
    p = prompt_spec(lang)
    assert (p.lang, p.display, p.fence, p.comment) == (lang, base.DISPLAY[lang], fence, comment)
    assert (p.file_noun, p.header_label) == (noun, label)
    assert (p.code_helper_prefix, p.test_helper_prefix) == (cdg, cdx)
    assert isinstance(p.fragments, dict)
    if lang == "go":
        assert p.fragments == {}


def test_protocol_members_are_implemented():
    members = {n for n in vars(base.LanguageAdapter) if not n.startswith("_")}
    members |= set(base.LanguageAdapter.__annotations__)
    for cls in (GoAdapter, LangAdapterBase):
        missing = sorted(m for m in members if not hasattr(cls, m) and m != "ctx")
        assert missing == [], (cls.__name__, missing)
    assert GoAdapter.ctx is None and "ctx" in base.LanguageAdapter.__annotations__


# -- tree-sitter helpers ----------------------------------------------------------------


def test_decode_codecs():
    assert ts.decode("héllo".encode()) == ("héllo", "utf-8")
    jp = "/* 日本語のコメント */\nint x;\n"
    euc = jp.encode("euc_jp")
    sjis = "// 表示\nint y;\n".encode("shift_jis")
    assert ts.decode(euc) == (jp, "euc_jp")
    text, codec = ts.decode(sjis)
    assert codec in ("shift_jis", "euc_jp") and ts.encode(text, codec) == sjis
    assert ts.decode("// 表示\n".encode("shift_jis"))[1] == "shift_jis"
    junk = b"int a; /* \xff\xfe\xfa */\n"
    text, codec = ts.decode(junk)
    assert codec == "utf-8" and ts.encode(text, codec) == junk


def test_offset_map_matches_brute_force():
    text = "a日b\U0001f600c" + "\udcff" + "dé\n"
    for codec in ("utf-8",):
        m = ts.OffsetMap(text, codec)
        b = 0
        for i, ch in enumerate(text):
            assert m.char(b) == i and m.byte(i) == b
            b += len(ts.encode(ch, codec))
        assert m.char(b) == len(text) and m.byte(len(text)) == b
    jp = "x = 'カナ';\n"
    m = ts.OffsetMap(jp, "shift_jis")
    assert m.byte(len(jp)) == len(jp.encode("shift_jis"))
    assert ts.OffsetMap("plain", "utf-8").identity


@needs_ts
def test_parse_char_offsets_non_ascii():
    src = "/* コメント */\nint 関数外 = 1;\nint f(void) { return 2; } // 終\nint g;\n"
    p = ts.parse(src, "c")
    funcs = list(ts.iter_types(p.root, {"function_definition"}))
    assert len(funcs) == 1
    a, b = p.span(funcs[0])
    assert src[a:b] == "int f(void) { return 2; }"
    assert p.node_text(funcs[0]) == src[a:b]
    assert p.line(funcs[0]) == 3 and p.end_line(funcs[0]) == 3
    assert p.line_at(a) == 3
    euc = ts.parse(src, "c", "euc_jp")
    f2 = next(ts.iter_types(euc.root, {"function_definition"}))
    assert euc.span(f2) == (a, b)
    # a char the codec cannot hold: parsed as utf-8, offsets still right
    odd = ts.parse("int x = 1; /* €\U0001f600 */ int f(void) { return 0; }\n", "c", "euc_jp")
    assert odd.codec == "utf-8"
    f3 = next(ts.iter_types(odd.root, {"function_definition"}))
    assert odd.node_text(f3) == "int f(void) { return 0; }"


@needs_ts
def test_parse_is_cached_and_errors_found():
    src = "def f(:\n    return 1\n"
    p = ts.parse(src, "python")
    assert ts.parse(src, "python") is p
    assert p.has_error
    spans = ts.error_spans(p)
    assert spans and all(0 <= a <= b <= len(src) for a, b in spans)
    assert ts.in_spans(spans[0][0], spans) if spans[0][0] < spans[0][1] else True
    ok = ts.parse("def f():\n    return 1\n", "python")
    assert not ok.has_error and ts.error_spans(ok) == []


@needs_ts
@pytest.mark.parametrize("lang,src,inside,outside", [
    ("python", 'x = "if y"  # z w\ndef f(a):\n    return a + 1\n', ["y", "z", "w"], ["x", "f"]),
    ("java", 'class A { String s = "q r"; // c1\n /* c2 */ int m() { return 1; } }\n',
     ["q", "c1", "c2"], ["A", "s", "m"]),
    ("c", '#include "inc.h"\nchar *s = "lit"; /* cm */ int f(int v) { return v; } // ln\n',
     ["lit", "cm", "ln", "inc"], ["s", "f", "v"]),
    ("cpp", 'auto s = R"(raw)"; std::string t = "st"; int g() { return 0; }\n',
     ["raw", "st"], ["s", "t", "g", "std", "string"]),
])
def test_mask_and_identifiers(lang, src, inside, outside):
    p = ts.parse(src, lang)
    masked = ts.mask(p)
    assert len(masked) == len(src)
    assert [i for i, c in enumerate(masked) if c == "\n"] == [
        i for i, c in enumerate(src) if c == "\n"]
    idents = ts.identifiers(src, lang)
    for w in inside:
        assert w not in idents
    for w in outside:
        assert w in idents
    assert not (idents & ts.KEYWORDS[lang])


@needs_ts
def test_walk_and_iter_types():
    p = ts.parse("class A:\n    def m(self):\n        def inner():\n            pass\n", "python")
    all_defs = list(ts.iter_types(p.root, {"function_definition"}))
    assert len(all_defs) == 2
    top = list(ts.iter_types(p.root, {"function_definition"}, prune={"function_definition"}))
    assert len(top) == 1
    assert sum(1 for _ in ts.walk(p.root)) == p.root.descendant_count
    cls = ts.children_of(p.root, "class_definition")[0]
    assert ts.children_of(cls) and list(ts.ancestors(top[0]))[-1] == p.root


def test_mask_c_like():
    src = 'int a; // x "y"\nchar *s = "a // b"; /* c\n d */ char c = \'"\';\n'
    m = ts.mask_c_like(src)
    assert len(m) == len(src) and m.count("\n") == src.count("\n")
    assert "x" not in m and "a // b" not in m and "d" not in m.split("\n")[2][:3]
    assert "int a;" in m and "char *s =" in m


@pytest.mark.parametrize("reply,tags,want", [
    ("```python\nx = 1\n```", ("python", "py"), "x = 1\n"),
    ("```py\nshort\n```\n```python\nlonger = 2\n```", ("python", "py"), "longer = 2\n"),
    ("```java\nclass A {}\n```\n```python\nnope\n```", ("java",), "class A {}\n"),
    ("```\nint f(void);\n```", ("c",), "int f(void);\n"),
    ("plain text\n", ("c",), "plain text\n"),
    ("```cpp\nint g();", ("cpp",), "int g();\n"),
])
def test_extract_code(reply, tags, want):
    assert ts.extract_code(reply, tags) == want


def test_line_count():
    assert ts.line_count("") == 0 and ts.line_count("a") == 1 and ts.line_count("a\nb\n") == 2


def test_missing_dependency_message(monkeypatch):
    monkeypatch.setattr(ts, "available", lambda: False)
    want = "pip install tree-sitter tree-sitter-language-pack"
    with pytest.raises(ts.MissingDependency, match=re.escape(want)):
        ts.require()


# -- registry: extensions and the header rule ---------------------------------------------


@pytest.mark.parametrize("rel,lang", [
    ("a/b.go", "go"), ("x.py", "python"), ("A.java", "java"), ("c.c", "c"), ("v.cc", "cpp"),
    ("v.cpp", "cpp"), ("v.cxx", "cpp"), ("v.c++", "cpp"), ("V.C", "cpp"), ("x.h", None),
    ("x.hpp", None), ("x.pyi", None), ("Makefile", None), (".c", None),
])
def test_source_language(rel, lang):
    assert registry.source_language(rel) == lang


def test_is_header():
    for h in ("a.h", "a.hh", "a.hpp", "a.hxx", "a.h++"):
        assert registry.is_header(h)
    assert not registry.is_header("a.c") and not registry.is_header("a.hpp.txt")


C_HEADER = """#ifndef A_H
#define A_H
#ifdef __cplusplus
extern "C" {
#endif
int clamp(int v); /* class Foo { in a comment */
const char *s = "std::string";
#ifdef __cplusplus
}
#endif
#endif
"""
CPP_HEADER = "#pragma once\nnamespace geom {\nstruct V { int x; };\n}\n"


@pytest.mark.parametrize("rel,siblings,text,want", [
    ("a.hpp", ["a.c"], None, "cpp"),
    ("a.hh", [], None, "cpp"),
    ("a.h", ["a.h", "a.cpp"], None, "cpp"),
    ("a.h", ["a.h", "a.cpp", "b.c"], None, "c"),
    ("a.h", ["a.h", "a.c"], CPP_HEADER, "c"),
    ("a.h", ["a.h"], C_HEADER, "c"),
    ("a.h", ["a.h"], CPP_HEADER, "cpp"),
    ("a.h", ["a.h"], "template <typename T> T id(T x);\n", "cpp"),
    ("a.h", ["a.h"], "class Foo : public Bar {};\n", "cpp"),
    ("a.h", ["a.h"], "struct s {\npublic:\n int x; };\n", "cpp"),
    ("a.h", ["a.h"], "int x = ns::y;\n", "cpp"),
    ("a.h", ["a.h"], "struct point { int x, y; };\n", "c"),
    ("a.h", None, None, "c"),
])
def test_header_language(rel, siblings, text, want):
    assert registry.header_language(rel, siblings, text) == want


def test_file_language_reads_lonely_headers(tmp_path: Path):
    _write(tmp_path, "inc/a.h", CPP_HEADER)
    _write(tmp_path, "inc/b.h", C_HEADER)
    _write(tmp_path, "src/c.h", CPP_HEADER)
    _write(tmp_path, "src/c.c", "int f(void) { return 0; }\n")
    assert registry.file_language(tmp_path, "inc/a.h") == "cpp"
    assert registry.file_language(tmp_path, "inc/b.h") == "c"
    assert registry.file_language(tmp_path, "src/c.h") == "c"
    assert registry.file_language(tmp_path, "src/c.c") == "c"
    assert registry.file_language(tmp_path, "README.md") is None


# -- registry: walking and detection --------------------------------------------------------


def _synthetic(root: Path) -> Path:
    _write(root, "go.mod", "module example.com/syn\n\ngo 1.21\n")
    _write(root, "svc/svc.go", "package svc\n\nfunc F() {}\n")
    _write(root, "svc/svc_test.go", "package svc\n")
    _write(root, "clib/ring.c", "int ring(void) { return 0; }\n")
    _write(root, "clib/ring.h", "int ring(void);\n")
    _write(root, "geom/vec.cpp", "int v() { return 0; }\n")
    _write(root, "geom/vec.h", "class V {};\n")
    _write(root, "include/api.h", CPP_HEADER)
    _write(root, "tools/report.py", "def main():\n    pass\n")
    _write(root, "tools/test_report.py", "def test_x():\n    pass\n")
    _write(root, "tools/conftest.py", "")
    _write(root, "jsvc/pom.xml", "<project/>")
    _write(root, "jsvc/src/main/java/com/x/A.java", "package com.x; class A {}\n")
    _write(root, "jsvc/src/test/java/com/x/ATest.java", "package com.x; class ATest {}\n")
    # skipped trees
    _write(root, "jsvc/target/classes/Gen.java", "class Gen {}\n")
    _write(root, ".venv/pyvenv.cfg", "home = /usr\n")
    _write(root, ".venv/lib/x.py", "x = 1\n")
    _write(root, "env2/pyvenv.cfg", "home = /usr\n")
    _write(root, "env2/lib/site.py", "x = 1\n")
    _write(root, "node_modules/m/i.c", "int i;\n")
    _write(root, "vendor/v/v.go", "package v\n")
    _write(root, "svc/testdata/t.go", "package t\n")
    _write(root, ".git/x.py", "")
    _write(root, "lib/site-packages/p.py", "")
    _write(root, "docs/markdown_docs/x.py", "")
    _write(root, "target/keep.c", "int keep;\n")  # no pom next to it: scanned
    return root


def test_walk_skip_rules(tmp_path: Path):
    root = _synthetic(tmp_path / "repo")
    _write(root, "out/docgen/calc/x/round_1/code.c", "int x;\n")
    files, dirs = registry.scan_files(root, out_dir=root / "out")
    every = {f for v in files.values() for f in v}
    assert "jsvc/target/classes/Gen.java" not in every
    assert not any(f.startswith((".venv/", "env2/", "node_modules/", "vendor/", ".git/",
                                 "lib/site-packages/", "docs/markdown_docs/", "out/"))
                   for f in every)
    assert "svc/testdata/t.go" not in every
    assert "target/keep.c" in files["c"]
    assert files["go"] == ["svc/svc.go", "svc/svc_test.go"]
    assert files["python"] == ["tools/conftest.py", "tools/report.py", "tools/test_report.py"]
    assert files["java"] == ["jsvc/src/main/java/com/x/A.java",
                             "jsvc/src/test/java/com/x/ATest.java"]
    assert "clib/ring.h" in files["c"] and "geom/vec.h" in files["cpp"]
    assert "include/api.h" in files["cpp"]
    assert dirs["clib"] == ["ring.c", "ring.h"] and "" in dirs
    no_headers, _ = registry.scan_files(root, headers=False)
    assert "clib/ring.h" not in no_headers["c"]


def test_detect_gomod_fixture():
    d = detect_languages(FIXTURE, out_dir=None)
    assert d.languages == ["go"] and d.legacy and d.counts == {"go": 3} and d.notes == []


def test_detect_go_module_with_others_is_legacy(tmp_path: Path):
    root = _synthetic(tmp_path / "repo")
    d = detect_languages(root, out_dir=None)
    assert d.languages == ["go"] and d.legacy
    assert d.found == {"go": 1, "c": 2, "cpp": 1, "java": 1, "python": 1}
    assert d.notes == ["also found: c 2, cpp 1, java 1, python 1 source files (not processed; "
                       "pass --source-lang all or e.g. go,c to include them)"]
    a = detect_languages(root, requested="all", out_dir=None)
    assert a.languages == ["go", "c", "cpp", "java", "python"] and not a.legacy
    assert a.notes == []
    e = detect_languages(root, requested="python, go,c", out_dir=None)
    assert e.languages == ["go", "c", "python"] and e.counts == {"go": 1, "c": 2, "python": 1}
    assert detect_languages(root, requested="go", out_dir=None).legacy
    assert detect_languages(root, requested="c++", out_dir=None).languages == ["cpp"]


def test_detect_subtree(tmp_path: Path):
    root = _synthetic(tmp_path / "repo")
    assert detect_languages(root, "svc", out_dir=None).languages == ["go"]
    # auto is decided by the whole repo: a Go module repo stays Go-only for a C-only subtree
    d = detect_languages(root, "clib", out_dir=None)
    assert d.languages == ["go"] and d.legacy
    assert d.notes and d.notes[0].startswith("also found: c ")
    assert detect_languages(root, "clib", requested="all", out_dir=None).languages == ["c"]
    # a subtree with nothing: Go-only, so the pipeline reports the empty selection itself
    _write(root, "empty/README.md", "x")
    assert detect_languages(root, "empty", out_dir=None).languages == ["go"]
    assert detect_languages(root, "no/such", requested="go", out_dir=None).legacy


def test_detect_non_go_repo(tmp_path: Path):
    root = tmp_path / "py"
    _write(root, "pkg/a.py", "x = 1\n")
    _write(root, "pkg/b.c", "int b;\n")
    _write(root, "x.go", "package main\n")
    d = detect_languages(root, out_dir=None)
    assert d.languages == ["c", "python"] and not d.legacy
    assert d.notes == ["Go files ignored: no go.mod at the repository root"]
    assert "go" not in d.found
    with pytest.raises(ValueError, match=r"is not a Go module \(no go.mod\)"):
        detect_languages(root, requested="go", out_dir=None)
    with pytest.raises(ValueError, match=r"--source-lang java: no Java files under"):
        detect_languages(root, requested="java,python", out_dir=None)
    with pytest.raises(ValueError, match="unknown language 'rust'"):
        detect_languages(root, requested="rust", out_dir=None)


def test_detect_errors(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no go.mod"):
        detect_languages(empty, out_dir=None)
    with pytest.raises(ValueError, match="has no supported source files"):
        detect_languages(empty, requested="all", out_dir=None)
    mod = tmp_path / "mod"
    _write(mod, "go.mod", "module m\n")
    _write(mod, "a/a_test.go", "package a\n")
    with pytest.raises(ValueError, match=f"no Go packages found under {re.escape(str(mod))}"):
        detect_languages(mod, out_dir=None)
    with pytest.raises(ValueError, match="no Go packages found under"):
        detect_languages(mod, requested="go", out_dir=None)
    _write(mod, "tools/t.py", "x = 1\n")
    d = detect_languages(mod, out_dir=None)
    assert d.languages == ["python"] and d.notes == ["go.mod found but no Go packages"]


def test_detect_requires_out_dir_keyword():
    with pytest.raises(TypeError):
        detect_languages(FIXTURE)  # type: ignore[call-arg]


def test_out_inside_repo_is_ignored(tmp_path: Path):
    repo = tmp_path / "gomod"
    shutil.copytree(FIXTURE, repo)
    out = repo / "out"
    _write(out, "docgen/calc/calc.c/round_1/code.c", "int x(void) { return 1; }\n")
    _write(out, "docgen/calc/calc.py/round_1/code.py", "x = 1\n")
    _write(out, "tests/_support/codocs_main.c", "int main(void) { return 0; }\n")
    d = detect_languages(repo, out_dir=out, requested="auto")
    assert d.languages == ["go"] and d.notes == [] and d.found == {"go": 3}
    assert detect_languages(repo, out_dir=out, requested="all").languages == ["go"]
    leaky = detect_languages(repo, out_dir=None, requested="all")
    assert leaky.languages == ["go", "c", "python"]


def test_resolve_languages_uses_settings(tmp_path: Path):
    root = _synthetic(tmp_path / "repo")
    assert registry.resolve_languages(root, Settings()).languages == ["go"]
    s = Settings()
    s.source_lang = "go,python"  # type: ignore[attr-defined]
    assert registry.resolve_languages(root, s).languages == ["go", "python"]


# -- RepoContext ------------------------------------------------------------------------


def test_repo_context_build(tmp_path: Path):
    root = _synthetic(tmp_path / "repo")
    _write(root, "out/x/code.py", "x = 1\n")
    _write(root, "clib/sjis.c", data="// 表示\nint s;\n".encode("shift_jis"))
    ctx = RepoContext.build(root, ["python", "c", "go"], Settings(), out_dir=root / "out")
    assert ctx.root == root.resolve() and ctx.languages == ["go", "c", "python"]
    assert set(ctx.files) == {"go", "c", "python"}
    assert "out/x/code.py" not in ctx.files["python"]
    assert ctx.files["c"] == ["clib/ring.c", "clib/ring.h", "clib/sjis.c", "target/keep.c"]
    assert ctx.header_index == {"ring.h": ["clib/ring.h"]}
    assert ctx.dirs["clib"] == ["ring.c", "ring.h", "sjis.c"]
    assert ctx.read("clib/sjis.c").startswith("// 表示")
    assert ctx.codec("clib/sjis.c") == "shift_jis" and ctx.codec("clib/ring.c") == "utf-8"
    assert ctx.read("missing.c") == ""
    assert base.StaticContext is RepoContext
    assert ctx.include_generated is False and ctx.no_toolchain("c") == ""
    ctx.mark_no_toolchain("c", "no cc")
    assert ctx.no_toolchain("c") == "no cc"
    hash(ctx)  # usable as a key (identity)


# -- adapter lookup and the non-Go base ------------------------------------------------------


class ToyAdapter(LangAdapterBase):
    """a minimal python-ish adapter over regexes, to exercise the base defaults."""

    name = "python"
    test_file_patterns = ("test_*.py",)
    _DEF = re.compile(r"^def (\w+)\(.*$", re.MULTILINE)
    _VAR = re.compile(r"^(\w+) = .*$", re.MULTILINE)

    def decls(self, src: str) -> list[Decl]:
        out = [Decl(kind="func", name=m.group(1), signature=m.group(0),
                    line=src.count("\n", 0, m.start()) + 1) for m in self._DEF.finditer(src)]
        out += [Decl(kind="var", name=m.group(1), signature=m.group(0))
                for m in self._VAR.finditer(src)]
        return out

    def imports(self, src: str) -> list:
        return [ImportSpec(m.group(1), kind="import") for m in
                re.finditer(r"^import (\w+)$", src, re.MULTILINE)]

    def is_generated(self, src: str, rel: str = "") -> bool:
        return "@generated" in src

    def test_names(self, src: str, rel: str) -> list[str]:
        return [f"{rel}::{n}" for n in re.findall(r"^def (test\w*)\(", src, re.MULTILINE)]

    def file_status(self, rel: str, unit) -> str | None:
        return "test-support" if rel.endswith("conftest.py") else None

    def suite_rel(self, unit, stem: str) -> str:
        return f"{unit.rel}/test_{stem[:-3]}_codocs.py"

    def suite_basename(self, stem: str) -> str:
        return f"test_{stem[:-3]}_codocs.py"


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    root = tmp_path / "toy"
    _write(root, "pkg/calc.py", "SCALE = 10\n\ndef clamp(v):\n    return v\n")
    _write(root, "pkg/consts.py", "A = 1\nB = 2\n")
    _write(root, "pkg/gen.py", "# @generated\ndef g():\n    pass\n")
    _write(root, "pkg/big.py", "def f():\n" + "    x = 1\n" * 40)
    _write(root, "pkg/empty.py", "# nothing\n")
    _write(root, "pkg/conftest.py", "def fixture():\n    pass\n")
    _write(root, "pkg/test_calc.py", "import calc\n\ndef test_clamp():\n    calc.clamp(1)\n\n"
                                     "def test_other():\n    pass\n")
    return root


def _toy_unit() -> Unit:
    return Unit(lang="python", rel="pkg", name="pkg",
                files=["pkg/big.py", "pkg/calc.py", "pkg/conftest.py", "pkg/consts.py",
                       "pkg/empty.py", "pkg/gen.py"],
                test_files=["pkg/test_calc.py"])


def test_base_select_targets(toy_repo: Path):
    ctx = RepoContext.build(toy_repo, ["python"], Settings())
    a = ToyAdapter(ctx)
    units = {"pkg": _toy_unit()}
    got = {t.file: t.status for t in a.select_targets(toy_repo, units, max_file_lines=20)}
    assert got == {"pkg/big.py": "too-large", "pkg/calc.py": "ok",
                   "pkg/conftest.py": "test-support", "pkg/consts.py": "no-funcs",
                   "pkg/empty.py": "no-decls", "pkg/gen.py": "generated"}
    ts_ = a.select_targets(toy_repo, units, max_file_lines=20)
    assert all(isinstance(t, LangTarget) and t.lang == "python" for t in ts_)
    assert [t.stem for t in ts_][:2] == ["big.py", "calc.py"]
    one = a.select_targets(toy_repo, units, max_files=1, max_file_lines=20)
    assert [t.file for t in one if t.status in base.PROCESSED] == ["pkg/calc.py"]
    assert "pkg/consts.py" not in {t.file for t in one}
    only = a.select_targets(toy_repo, units, files="pkg/c*.py")
    assert [t.file for t in only] == ["pkg/calc.py", "pkg/conftest.py", "pkg/consts.py"]
    s = Settings()
    s.include_generated = True  # type: ignore[attr-defined]
    gen = ToyAdapter(RepoContext.build(toy_repo, ["python"], s))
    assert {t.file: t.status for t in gen.select_targets(toy_repo, units)}["pkg/gen.py"] == "ok"
    ctx.mark_no_toolchain("python", "no pytest")
    blocked = {t.file: t.status for t in a.select_targets(toy_repo, units, max_file_lines=20)}
    assert blocked["pkg/calc.py"] == "no-toolchain" and blocked["pkg/gen.py"] == "generated"


def test_base_defaults(toy_repo: Path, tmp_path: Path):
    ctx = RepoContext.build(toy_repo, ["python"], Settings())
    a = ToyAdapter(ctx)
    unit = _toy_unit()
    assert a.display == "Python" and a.prompts.fence == "python"
    assert a.stem("pkg/calc.py") == "calc.py"
    assert a.owns("pkg/calc.py") and not a.owns("pkg/calc.c")
    assert a.is_test_file("pkg/test_calc.py") and not a.is_test_file("pkg/calc.py")
    assert a.baseline_path(tmp_path, unit) == tmp_path / "baseline" / "python" / "pkg.json"
    root_unit = Unit(lang="c", rel="", name="r")
    assert a.baseline_path(tmp_path, root_unit).name == "_root.json"
    nested = Unit(lang="c", rel="a/b", name="b")
    assert a.baseline_path(tmp_path, nested).name == "a__b.json"
    assert a.declared_tests(toy_repo, unit) == {"pkg/test_calc.py::test_clamp",
                                                "pkg/test_calc.py::test_other"}
    idx = a.test_index(toy_repo, unit)
    assert idx["pkg/test_calc.py::test_clamp"]["file"] == "pkg/test_calc.py"
    assert a.relevant_tests(toy_repo, unit, "pkg/calc.py", set(idx)) == sorted(idx)
    assert a.relevant_tests(toy_repo, unit, "pkg/consts.py", set(idx)) == []
    assert "clamp" in a.package_identifiers(toy_repo, unit, "pkg/consts.py")
    ctxt = a.package_context(toy_repo, unit, "pkg/consts.py")
    assert ctxt.startswith("# pkg/big.py\ndef f():") and "# pkg/calc.py" in ctxt
    assert a.package_context(toy_repo, unit, "pkg/consts.py", max_chars=10).endswith(
        "# ... (truncated)\n")
    assert a.extract_code("```py\nx = 1\n```") == "x = 1\n"
    assert a.extract_code("```java\nclass A {}\n```") == "```java\nclass A {}\n```\n"
    assert a.normalize_suite("s", unit, "pkg/calc.py") == "s"
    assert a.strip_non_tests("s") == "s" and a.preamble("s") == ""
    assert a.apply_preamble("code", "") == "code"
    assert a.fix_allowed([1, 2]) == [1, 2] and a.default_allowed(None, unit) == []
    assert a.fix_imports("s", [], "x.py", []) is None
    assert a.allowed_third_party(toy_repo, unit) == []
    assert a.failure_message("x" * 3000).endswith("\n...")
    assert a.crash_message(GoTestResult(build_ok=True, tail="a\nSegfault\n\n")) == (
        "did not run: the test process exited early (Segfault)")
    facts = a.wiki_facts(toy_repo, "pkg/calc.py", "import os\ndef f():\n    pass\n", ctx)
    assert facts.loc == 3 and [d.name for d in facts.decls] == ["f"]
    assert facts.imports == [ImportSpec("os", kind="import")]
    unbound = ToyAdapter()
    assert unbound.ctx is None and unbound.read("pkg/calc.py", toy_repo).startswith("SCALE")


def test_base_prompt_builders_delegate(monkeypatch):
    seen: list = []

    def rec(name):
        def f(*args, **kw):
            seen.append((name, args, kw))
            return ("sys", "user")
        return f

    sf = types.ModuleType("codocs.specforge.prompts_lang")
    sf.seed_messages, sf.codegen_messages, sf.refine_messages = (
        rec("sf.seed"), rec("sf.codegen"), rec("sf.refine"))
    te = types.ModuleType("codocs.tesearch.prompts_lang")
    te.seed_messages, te.refine_messages = rec("ts.seed"), rec("ts.refine")
    import codocs.specforge
    import codocs.tesearch

    monkeypatch.setitem(sys.modules, "codocs.specforge.prompts_lang", sf)
    monkeypatch.setitem(sys.modules, "codocs.tesearch.prompts_lang", te)
    monkeypatch.setattr(codocs.specforge, "prompts_lang", sf, raising=False)
    monkeypatch.setattr(codocs.tesearch, "prompts_lang", te, raising=False)
    a = ToyAdapter()
    u = _toy_unit()
    assert a.doc_seed_messages("pkg/calc.py", u, "src", "ctx") == ("sys", "user")
    a.doc_codegen_messages("pkg/calc.py", u, "doc", "ctx", ["imp"], taken={"t"},
                           has_preamble=True)
    a.doc_refine_messages("pkg/calc.py", u, "src", "doc", "code", "ev", ["m"], last="l",
                          last_round=1, attempt=2)
    a.test_seed_messages("pkg/calc.py", u, "src", "ctx", {"t"}, ["dep"], "ref")
    a.test_refine_messages("pkg/calc.py", u, "src", "ctx", {"t"}, ["dep"], "best", "last",
                           attempt=3, last_round=2, reference="r")
    assert [s[0] for s in seen] == ["sf.seed", "sf.codegen", "sf.refine", "ts.seed", "ts.refine"]
    assert all(s[1][0] is a for s in seen)
    assert seen[1][2] == {"taken": {"t"}, "has_preamble": True}
    assert seen[2][2] == {"last": "l", "last_round": 1, "attempt": 2}
    assert seen[3][1][1:] == ("pkg/calc.py", u, "src", "ctx", {"t"}, ["dep"], "ref")
    assert seen[4][2] == {"attempt": 3, "last_round": 2, "reference": "r"}


def test_adapter_lookup(monkeypatch, tmp_path: Path):
    mod = types.ModuleType("codocs_toy_lang")
    mod.ToyAdapter = ToyAdapter
    monkeypatch.setitem(sys.modules, "codocs_toy_lang", mod)
    monkeypatch.setitem(registry.ADAPTERS, "python", ("codocs_toy_lang", "ToyAdapter"))
    monkeypatch.setattr(registry, "_UNBOUND", {})
    ctx = RepoContext.build(FIXTURE, ["python"], Settings())
    other = RepoContext.build(FIXTURE, ["python"], Settings())
    a = adapter("python", ctx)
    assert isinstance(a, ToyAdapter) and a.ctx is ctx
    assert adapter("python", ctx) is a and adapter("py", ctx) is a
    assert adapter("python", other) is not a and adapter("python", other).ctx is other
    free = adapter("python")
    assert free.ctx is None and adapter("python") is free
    runner = types.SimpleNamespace(lang="python", adapter=a)
    assert adapter_for(runner) is a
    assert adapter_for(types.SimpleNamespace(lang="python")) is free
    with pytest.raises(ValueError, match="unknown language"):
        adapter("rust")


def test_adapter_modules_are_lazy():
    import subprocess

    code = ("import sys, codocs.lang; from codocs.lang import detect_languages; "
            f"detect_languages({str(FIXTURE)!r}, out_dir=None); "
            "print(sorted(m for m in sys.modules if m.startswith(('codocs.lang.', 'tree_sitter',"
            " 'codocs.specforge', 'codocs.tesearch', 'codocs.pipeline'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "['codocs.lang.base', 'codocs.lang.registry']"
