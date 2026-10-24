"""per-language prompts: templates + fragments, and the non-Go user-message builders."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codocs.golang.package import PackageInfo
from codocs.lang.base import FRAGMENT_SLOTS, PROMPTS_LANG_DIR, RepoContext, parse_fragments
from codocs.lang.registry import adapter
from codocs.specforge import prompts_lang as sp
from codocs.specforge.loop import DocEval
from codocs.specforge.prompts import (
    CODEGEN,
    PROMPTS_DIR,
    REFINE,
    SEED,
    TEMPLATES_DIR,
    load_prompt,
    render_template,
)
from codocs.tesearch import prompts_lang as tp
from codocs.tesearch.measure import Measurement
from codocs.tesearch.prompts import mark_failing, mark_source, refine_system, seed_system

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LANGS = ("python", "java", "c", "cpp")
TEMPLATES = (SEED, REFINE, CODEGEN, "tesearch_seed_system", "tesearch_refine_system")
TARGETS = {
    "python": ("pymod", "src/pymod", "src/pymod/calc.py"),
    "java": ("javamod", "src/main/java/com/example/calc",
             "src/main/java/com/example/calc/Calc.java"),
    "c": ("cmod", "calc", "calc/calc.c"),
    "cpp": ("cppmod", "geom", "geom/vec.cpp"),
}


def _bound(lang: str):
    fixture, unit_rel, target = TARGETS[lang]
    ctx = RepoContext.build(FIXTURES / fixture, [lang])
    ad = adapter(lang, ctx)
    return ad, ad.unit(unit_rel), target, (FIXTURES / fixture / target).read_text()


@pytest.mark.parametrize("lang", LANGS)
def test_fragment_files_define_every_slot(lang: str):
    frags = parse_fragments((PROMPTS_LANG_DIR / f"{lang}.txt").read_text(encoding="utf-8"))
    assert set(FRAGMENT_SLOTS) <= set(frags), set(FRAGMENT_SLOTS) - set(frags)
    assert frags["FENCE"] == adapter(lang).prompts.fence


def test_templates_use_only_known_slots():
    for name in TEMPLATES:
        text = (TEMPLATES_DIR / f"{name}.txt").read_text(encoding="utf-8")
        used = set(re.findall(r"\$\{(\w+)\}", text))
        assert used and used <= set(FRAGMENT_SLOTS), (name, used - set(FRAGMENT_SLOTS))
        assert not re.search(r"\$(?![{$])", text), name


@pytest.mark.parametrize("lang", LANGS)
def test_rendered_system_prompts(lang: str):
    spec = adapter(lang).prompts
    for name in (SEED, REFINE, CODEGEN):
        text = load_prompt(name, lang)
        assert "${" not in text and spec.display in text
        assert "Go " not in text and "go test" not in text
        assert text == render_template(name, lang)
    assert f"```{spec.fence}" in load_prompt(CODEGEN, lang)
    assert spec.fragments["DOC_HEADER"] in load_prompt(SEED, lang)
    assert spec.fragments["TEST_RULES"] in seed_system(lang)
    assert f"`{spec.comment} <-- NOT COVERED YET`" in refine_system(lang)
    assert not seed_system(lang).endswith("\n")


def test_go_prompts_are_the_frozen_files():
    for name in (SEED, REFINE, CODEGEN):
        assert load_prompt(name) == (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
        assert load_prompt(name, "go") == load_prompt(name)
    raw = (PROMPTS_DIR / "tesearch_seed_system.txt").read_text(encoding="utf-8")
    assert seed_system() == seed_system("go") == raw.rstrip("\n")
    raw = (PROMPTS_DIR / "tesearch_refine_system.txt").read_text(encoding="utf-8")
    assert refine_system() == raw.rstrip("\n")


def test_missing_slot_is_an_error(monkeypatch):
    from codocs.lang import base

    spec = base.prompt_spec("python")
    broken = dict(spec.fragments)
    broken.pop("TYPE_RULE")
    monkeypatch.setattr(base, "prompt_spec",
                        lambda lang: base.PromptSpec(**{**spec.__dict__, "fragments": broken}))
    render_template.cache_clear()
    try:
        with pytest.raises(KeyError):
            render_template(SEED, "python")
    finally:
        monkeypatch.undo()
        render_template.cache_clear()


# -- docgen builders -----------------------------------------------------------------------


HEADERS = {
    "python": "# src/pymod/calc.py\n\n**Module**: `pymod.calc`",
    "java": ("# src/main/java/com/example/calc/Calc.java\n\n"
             "**Package**: `com.example.calc` (module `javamod`)"),
    "c": "# calc/calc.c\n\n**Unit**: `calc` (C)",
    "cpp": "# geom/vec.cpp\n\n**Unit**: `geom` (C++)",
}


@pytest.mark.parametrize("lang", LANGS)
def test_doc_seed_and_refine_messages(lang: str):
    ad, unit, target, src = _bound(lang)
    fence = ad.prompts.fence
    system, user = ad.doc_seed_messages(target, unit, src, "int other(void);")
    assert system == load_prompt(SEED, lang)
    assert user.startswith(f"Document the {ad.prompts.file_noun} `{target}`")
    assert f"```markdown\n{HEADERS[lang]}\n```" in user
    assert f"```{fence}\n{src.rstrip()}\n```" in user
    assert "int other(void);" in user
    from codocs.specforge.docfmt import expected_sections

    for kind, name in expected_sections(src, lang):
        assert f"- `## {kind} {name}`" in user
    ev = DocEval(signal="tests", build_ok=True, phi=0.5, relevant=["a", "b"], passed=["a"],
                 failing={"b": "assert 1 == 2"})
    system, user = ad.doc_refine_messages(target, unit, src, "# doc", "code here", ev,
                                          ["FunctionDef x"], last=ev, last_round=1, attempt=2)
    assert system == load_prompt(REFINE, lang)
    assert user.startswith("Round 2. Refine the documentation of")
    assert f"```{fence}\ncode here\n```" in user and "- `## FunctionDef x`" in user
    assert "phi = 0.50 (1/2 tests passed)" in user and "## Last attempt (round 1)" in user


@pytest.mark.parametrize("lang", LANGS)
def test_doc_codegen_messages(lang: str):
    ad, unit, target, src = _bound(lang)
    allowed = ad.fix_allowed(ad.imports(src))
    header = ad.preamble(src)
    system, user = ad.doc_codegen_messages(target, unit, "# the doc", "ctx", allowed,
                                           taken={"cdg_x", "zzz_taken"},
                                           has_preamble=bool(header))
    assert system == load_prompt(CODEGEN, lang)
    name = Path(target).name
    assert user.startswith(f"Write the {ad.prompts.file_noun} `{name}`")
    assert user.rstrip().endswith(f"Output the complete file `{name}` in one ```{ad.prompts.fence}"
                                  " block.")
    for imp in allowed:
        assert ad.format_import(imp) in user
    assert ("zzz_taken" in user) == (lang != "python")
    if header.strip():
        assert header.strip() in user


def test_codegen_preamble_notes():
    ad, unit, target, src = _bound("java")
    _, user = ad.doc_codegen_messages(target, unit, "doc", "", [], has_preamble=True)
    assert "## Package declaration" in user and "package com.example.calc;" in user
    _, user = ad.doc_codegen_messages(target, unit, "doc", "", [], has_preamble=False)
    assert "## Package declaration" not in user
    assert "(none: the original file imports nothing)" in user


# -- testgen builders ----------------------------------------------------------------------


PACKAGE = {
    "python": ("[PACKAGE] module `pymod.calc`; import it as `from pymod.calc import ...`; the "
               "suite runs as src/pymod/test_calc_codocs.py with no other test file of the "
               "project"),
    "java": ("[PACKAGE] `package com.example.calc;` class `CalcCodocsTest`, compiled as "
             "src/test/java/com/example/calc/CalcCodocsTest.java with no other test class of "
             "the module"),
    "c": ("[PACKAGE] directory unit `calc`; the suite is one program compiled with the unit's "
          "other sources as calc/calc_codocs_test.c; the target is included through "
          "`#include CODOCS_TARGET`, so its static functions are reachable"),
    "cpp": ("[PACKAGE] directory unit `geom`; the suite is one program compiled with the "
            "unit's other sources as geom/vec_codocs_test.cpp"),
}
FIRST = {
    "python": "Start with the imports (the target as `from pymod.calc import ...`).",
    "java": "Start with `package com.example.calc;`.",
    "c": 'Start with `#include "codocs_test.h"` and `#include CODOCS_TARGET`.',
    "cpp": 'Start with `#include "codocs_test.h"` and `#include CODOCS_TARGET`.',
}


@pytest.mark.parametrize("lang", LANGS)
def test_test_seed_messages(lang: str):
    ad, unit, target, src = _bound(lang)
    system, user = ad.test_seed_messages(target, unit, src, "CONTEXT", {"cdx_x", "_cdx_y",
                                                                        "CdxZ", "plain"},
                                         ["requests"], "REFERENCE")
    assert system == seed_system(lang)
    assert user.startswith(f"[TARGET FILE] {target}\n\n")
    assert PACKAGE[lang] in user
    assert "plain" not in user.split("[TAKEN NAMES")[1].split("\n\n")[0]
    assert "requests" in user and "CONTEXT" in user and "REFERENCE" in user
    assert f"[TARGET SOURCE]\n{src.rstrip()}" in user
    assert FIRST[lang] in user and f"```{ad.prompts.fence} block" in user


def test_test_refine_marks_python():
    ad, unit, target, src = _bound("python")
    suite = "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n"
    best = Measurement(build_ok=True, size=2, green_size=1, cov=0.5, uncovered=[12],
                       survivors=[{"id": "m1", "line": 13, "operator": "< -> <="}],
                       failing={"src/pymod/test_calc_codocs.py::test_bad": "AssertionError: x"},
                       suite_src=suite)
    last = Measurement(build_ok=False, build_errors=["src/pymod/test_calc_codocs.py:1: Err"])
    system, user = ad.test_refine_messages(target, unit, src, "", set(), [], best, last,
                                           attempt=2, last_round=1)
    assert system == refine_system("python") and user.startswith("[ROUND 2]")
    lines = src.splitlines()
    assert f"{lines[11]}  # <-- NOT COVERED YET" in user
    assert f"{lines[12]}  # <-- SURVIVING MUTANT: < -> <=" in user
    assert "def test_bad():  # <-- FAILING: AssertionError: x" in user
    assert "[LAST ATTEMPT - did NOT BUILD; avoid these errors]" in user
    best.cov_note = "coverage unavailable: gcov missing"
    _, user = ad.test_refine_messages(target, unit, src, "", set(), [], best, None)
    assert "coverage=n/a (coverage unavailable: gcov missing)" in user


def test_test_refine_without_best():
    ad, unit, target, src = _bound("c")
    last = Measurement(build_ok=False, build_errors=["calc/calc_codocs_test.c:3: error: x"],
                       suite_src="CODOCS_TEST(a) {}\n")
    _, user = ad.test_refine_messages(target, unit, src, "", set(), [], None, last)
    assert "[LAST ATTEMPT SOURCE]\nCODOCS_TEST(a) {}" in user
    assert "fix every build error" in user


def test_mark_helpers_keep_go_defaults():
    src = "a\nb\nc\n"
    assert mark_source(src, [2], []) == "a\nb  // <-- NOT COVERED YET\nc\n"
    assert mark_source(src, [2], [], "#") == "a\nb  # <-- NOT COVERED YET\nc\n"
    go = "package x\n\nfunc TestA(t *testing.T) {}\n"
    assert mark_failing(go, {"TestA": "boom"}) == \
        "package x\n\nfunc TestA(t *testing.T) {}  // <-- FAILING: boom\n"
    assert mark_failing(src, {"x::t": "bad"}, "#", lines={"x::t": 3}) == \
        "a\nb\nc  # <-- FAILING: bad\n"


def test_go_adapter_prompts_unchanged():
    from codocs.specforge.prompts import seed_messages

    go = adapter("go")
    pkg = PackageInfo(rel="calc", import_path="example.com/fixture/calc", name="calc")
    src = (FIXTURES / "gomod/calc/calc.go").read_text()
    assert go.doc_seed_messages("calc/calc.go", pkg, src, "") == seed_messages(
        "calc/calc.go", pkg, src, "")


def test_header_helpers_fall_back_without_context():
    from codocs.lang.base import Unit

    py = adapter("python")
    unit = Unit(lang="python", rel="pkg", name="pkg", import_path="pkg")
    assert sp.module_name(py, "pkg/mod.py", unit) == "pkg.mod"
    assert sp.module_name(py, "pkg/__init__.py", unit) == "pkg"
    java = Unit(lang="java", rel="src/a", name="a", import_path="a", extra={"module": "svc"})
    assert sp.java_module(java) == "svc"
    assert tp.first_lines(adapter("java"), "src/a/A.java", java) == "`package a;`"
