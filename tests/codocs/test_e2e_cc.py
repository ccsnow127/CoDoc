"""end-to-end: `codocs run` on copies of the cmod and cppmod fixtures with a scripted fake llm."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from _e2e_lang import (
    build_and_run,
    c_suite_name,
    check_doc,
    check_resume,
    check_suite,
    check_summary,
    install,
    load,
    run_cli,
    snapshot,
    wiki_plan,
)

from codocs.lang.treesitter import decode

CC = shutil.which("cc")
CXX = shutil.which("c++")
GCOV = shutil.which("gcov")
needs_cc = pytest.mark.skipif(not (CC and GCOV), reason="cc/gcov not installed")
needs_cxx = pytest.mark.skipif(not (CXX and GCOV), reason="c++/gcov not installed")

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
    CHECK_EQ_INT(normalize("\\tAb\\n", buf, sizeof buf), 2);
    CHECK_EQ_STR(buf, "ab");
    CHECK(is_space('\\t'));
}

CODOCS_TEST(wrong) {
    CHECK_EQ_INT(clamp(1, 2, 3), 1);
}
''',
    "calc/stats.c": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(add_and_mean) {
    struct stats s = {0, 0, 0};
    CHECK_EQ_INT(stats_mean(&s), 0);
    stats_add(&s, 3);
    stats_add(&s, 7);
    stats_add(&s, 5);
    CHECK_EQ_INT(s.count, 3);
    CHECK_EQ_INT(s.total, 15);
    CHECK_EQ_INT(s.max, 7);
    CHECK_EQ_INT(stats_mean(&s), 50);
}

CODOCS_TEST(first_value_is_max) {
    struct stats s = {0, 0, 0};
    stats_add(&s, -4);
    CHECK_EQ_INT(s.max, -4);
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
    "sjis/sjis.c": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(greeting) {
    CHECK_NOT_NULL(sjis_greeting());
    CHECK_EQ_INT(sjis_len(sjis_greeting()), (long long)strlen(sjis_greeting()));
}

CODOCS_TEST(length) {
    CHECK_EQ_INT(sjis_len(""), 0);
    CHECK_EQ_INT(sjis_len("abc"), 3);
}
''',
}

CPP_SUITES = {
    "geom/vec.cpp": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(vec_ops) {
    geom::Vec2 a(1, 2), b(3, 4);
    CHECK(a.add(b) == geom::Vec2(4, 6));
    CHECK_EQ_DBL(a.dot(b), 11.0, 1e-9);
    CHECK_EQ_DBL(geom::Vec2(3, 4).norm(), 5.0, 1e-9);
}

CODOCS_TEST(vec_equality) {
    geom::Vec2 a(1, 2);
    CHECK(a == geom::Vec2(1, 2));
    CHECK(!(a == geom::Vec2(1, 3)));
    CHECK(!(a == geom::Vec2(0, 2)));
}
''',
    "geom/poly.cpp": '''#include "codocs_test.h"
#include CODOCS_TARGET

CODOCS_TEST(square) {
    geom::Polygon sq({geom::Vec2(0, 0), geom::Vec2(2, 0), geom::Vec2(2, 2), geom::Vec2(0, 2)});
    CHECK_EQ_DBL(sq.area(), 4.0, 1e-9);
    CHECK_EQ_DBL(sq.perimeter(), 8.0, 1e-9);
    CHECK_EQ_INT((long long)sq.size(), 4);
}

CODOCS_TEST(degenerate) {
    geom::Polygon two({geom::Vec2(0, 0), geom::Vec2(3, 4)});
    CHECK_EQ_DBL(two.area(), 0.0, 1e-9);
    CHECK_EQ_DBL(two.perimeter(), 10.0, 1e-9);
    geom::Polygon one({geom::Vec2(1, 1)});
    CHECK_EQ_DBL(one.perimeter(), 0.0, 1e-9);
}
''',
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


@needs_cc
def test_c_run_end_to_end_and_resume(tmp_path: Path, monkeypatch):
    repo, out, fake = install(monkeypatch, tmp_path, "cmod", C_SUITES)
    before = snapshot(repo)
    res = run_cli(repo, out)
    assert res.exit_code == 0, res.output
    assert "c: 4 targets" in res.output, res.output

    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["c"]
    ok = sorted(t["file"] for t in analysis["targets"] if t["status"] == "ok")
    assert ok == sorted(C_SUITES)
    statuses = {t["file"]: t["status"] for t in analysis["targets"]}
    assert statuses["calc/calc.h"] == "header" and statuses.get("calc/test_calc.c") != "ok"

    base = load(out / "baseline" / "c" / "calc.json")
    assert base["build_ok"] is True and base["lang"] == "c"
    assert base["failing"] == ["calc/test_broken.c"]
    assert sorted(base["passing"]) == ["calc/test_calc.c", "calc/test_stats.c"]

    for rel in C_SUITES:
        check_suite(out, rel, c_suite_name(rel), min_cov=0.9)
    calc = load(out / "testgen/calc/calc.c/result.json")
    assert calc["best_size"] == 3
    assert "CODOCS_TEST(wrong)" not in (out / "tests/calc/calc_codocs_test.c").read_text()
    assert {p.name for p in (out / "tests" / "_support").iterdir()} >= {
        "codocs_test.h", "codocs_main.c", "codocs_main.cpp"}

    # the delivered suites build against _support/ and pass on the original sources
    work = tmp_path / "check"
    work.mkdir()
    for rel in C_SUITES:
        build_and_run(CC, out, repo, rel, [], work)

    for rel in C_SUITES:
        check_doc(out, rel)
    sig = {rel: load(out / "docgen" / str(Path(rel).parent) / Path(rel).name /
                     "result.json")["signal"] for rel in C_SUITES}
    assert sig == {"calc/calc.c": "tests", "calc/stats.c": "tests",
                   "shapes/shapes.c": "generated-tests", "sjis/sjis.c": "generated-tests"}
    doc = (out / "docs/calc/markdown_docs/calc.c.md").read_text()
    assert "**Unit**: `calc` (C)" in doc and "## FunctionDef clamp" in doc
    # the Shift-JIS target: the reply was decoded with the file's codec and phi is 1.0, so the
    # overlay compiled; the round artifact keeps the same text
    text, codec = decode((repo / "sjis/sjis.c").read_bytes())
    assert codec == "shift_jis"
    assert (out / "docgen/sjis/sjis.c/round_1/code.c").read_text(encoding="utf-8") == text

    check_summary(out, "c", docs=4, suites=4)
    calls = len(fake.calls)
    check_resume(repo, out, fake)
    assert snapshot(repo) == before

    text, plan = wiki_plan(repo, out)
    assert "c: " in text and plan["leaf_out"] >= 3, (text, plan)
    assert set(plan["languages"]) == {"c"}
    assert len(fake.calls) == calls


@needs_cxx
def test_cpp_run_end_to_end_and_resume(tmp_path: Path, monkeypatch):
    repo, out, fake = install(monkeypatch, tmp_path, "cppmod", CPP_SUITES)
    before = snapshot(repo)
    res = run_cli(repo, out)
    assert res.exit_code == 0, res.output

    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["cpp"]
    ok = sorted(t["file"] for t in analysis["targets"] if t["status"] == "ok")
    assert ok == sorted(CPP_SUITES)

    base = load(out / "baseline" / "cpp" / "geom.json")
    assert base["build_ok"] is True and base["lang"] == "cpp"
    assert base["failing"] == ["geom/test_broken.cpp"]
    assert base["passing"] == ["geom/test_vec.cpp"]

    for rel in CPP_SUITES:
        check_suite(out, rel, c_suite_name(rel), min_cov=0.9)

    work = tmp_path / "check"
    work.mkdir()
    build_and_run(CXX, out, repo, "geom/vec.cpp", [], work)
    build_and_run(CXX, out, repo, "geom/poly.cpp", ["geom/vec.cpp"], work)
    build_and_run(CXX, out, repo, "util/strutil.cpp", [], work)

    for rel in CPP_SUITES:
        check_doc(out, rel)
    assert load(out / "docgen/geom/vec.cpp/result.json")["signal"] == "tests"
    assert load(out / "docgen/util/strutil.cpp/result.json")["signal"] == "generated-tests"
    doc = (out / "docs/geom/markdown_docs/vec.cpp.md").read_text()
    assert "**Unit**: `geom` (C++)" in doc and "## MethodDef Vec2.add" in doc

    check_summary(out, "cpp", docs=3, suites=3)
    calls = len(fake.calls)
    check_resume(repo, out, fake)
    assert snapshot(repo) == before

    text, plan = wiki_plan(repo, out)
    assert set(plan["languages"]) == {"cpp"} and plan["leaf_out"] == 2, plan
    assert len(fake.calls) == calls
