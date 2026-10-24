"""stub fallback: LLM stubs for user-defined FUNCTIONs must replace the repo function (not
be linked next to it: duplicate symbols), and a stub that replaces a repo program but does
not compile must fall back to the real program (not leave its CALLs unresolved at run
time)."""

from __future__ import annotations

import shutil

import pytest

from codocs.cobol.classify import classify
from codocs.cobol.harness import CobolTestRunner, plan_stubs
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.reflow import program_ids, reflow_fixed
from codocs.cobol.testmodels import GeneratedTest, TestCaseSpec

needs_cobc = pytest.mark.skipif(shutil.which("cobc") is None, reason="GnuCOBOL not installed")


def fx(*lines: str) -> str:
    return "\n".join("       " + ln for ln in lines) + "\n"


REPO = {
    "src/upfn.cbl": fx(
        "IDENTIFICATION DIVISION.", "FUNCTION-ID. up-fn.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 L-IN PIC X(5).", "01 L-OUT PIC X(5).",
        "PROCEDURE DIVISION USING L-IN RETURNING L-OUT.",
        "    MOVE FUNCTION UPPER-CASE(L-IN) TO L-OUT", "    GOBACK.",
        "END FUNCTION up-fn.",
    ),
    "src/usefn.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. use-fn.", "ENVIRONMENT DIVISION.",
        "CONFIGURATION SECTION.", "REPOSITORY.", "    FUNCTION up-fn", "    FUNCTION ALL INTRINSIC.",
        "DATA DIVISION.", "LINKAGE SECTION.", "01 L-IN PIC X(5).", "01 L-OUT PIC X(5).",
        "PROCEDURE DIVISION USING L-IN L-OUT.",
        "    MOVE up-fn(L-IN) TO L-OUT", "    GOBACK.",
    ),
    "src/helper.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. helper.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 L-N PIC 9(4).", "PROCEDURE DIVISION USING L-N.",
        "    ADD 1 TO L-N", "    GOBACK.",
    ),
    "src/caller.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. caller.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 L-N PIC 9(4).", "PROCEDURE DIVISION USING L-N.",
        "    CALL 'helper' USING L-N", "    GOBACK.",
    ),
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("stubrepo")
    for rel, text in REPO.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(root)


FN_STUB = """IDENTIFICATION DIVISION.
FUNCTION-ID. UP-FN.
DATA DIVISION.
LINKAGE SECTION.
01 L-IN PIC X(5).
01 L-OUT PIC X(5).
PROCEDURE DIVISION USING L-IN RETURNING L-OUT.
    MOVE 'STUB!' TO L-OUT
    GOBACK.
END FUNCTION UP-FN.
"""

BROKEN_HELPER_STUB = """IDENTIFICATION DIVISION.
PROGRAM-ID. helper.
DATA DIVISION.
LINKAGE SECTION.
01 L-N PIC 9(4).
01 FILLER.
   05 FILLER PIC X.
PROCEDURE DIVISION USING L-N FILLER.
    MOVE 99 TO L-N
    GOBACK.
"""


def _unit(index, pid: str, driver: str, stubs: str = "") -> GeneratedTest:
    return GeneratedTest(
        program_id=pid, mode="unit", driver_name="TDRV", driver_source=reflow_fixed(driver),
        stubs_source=reflow_fixed(stubs) if stubs else "",
        cases=[TestCaseSpec(id="C01")],
        testability=classify(index.programs[pid], index),
    )


def test_program_ids_include_function_ids():
    assert program_ids(reflow_fixed(FN_STUB)) == ["UP-FN"]


def test_function_stub_replaces_repo_function(repo):
    plan = plan_stubs(repo, "USE-FN", reflow_fixed(FN_STUB))
    assert plan.replaced == ["UP-FN"]
    assert "UP-FN" not in plan.linked
    assert [n for _, n in plan.chunks] == [["UP-FN"]]


def test_plan_drops_stubs_that_do_not_compile(repo):
    plan = plan_stubs(repo, "CALLER", reflow_fixed(BROKEN_HELPER_STUB),
                      drop=frozenset({"HELPER"}))
    assert plan.replaced == [] and plan.linked == ["HELPER"]
    assert any("does not compile (HELPER linked for real)" in s for s in plan.skipped)


@needs_cobc
def test_function_stub_links_without_duplicate_symbol(repo, tmp_path):
    driver = """IDENTIFICATION DIVISION.
PROGRAM-ID. TDRV.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 TD-IN PIC X(5).
01 TD-OUT PIC X(5).
PROCEDURE DIVISION.
    MOVE 'abc' TO TD-IN
    CALL 'use-fn' USING TD-IN TD-OUT
    IF TD-OUT = 'STUB!' DISPLAY 'TEST C01 PASS'
    ELSE DISPLAY 'TEST C01 FAIL [' TD-OUT ']' END-IF
    STOP RUN.
"""
    res = CobolTestRunner(repo, tmp_path / "work").run(_unit(repo, "USE-FN", driver, FN_STUB))
    assert res.status == "passed", (res.compile_log, res.run_log)
    assert res.stubbed_callees == ["UP-FN"]


@needs_cobc
def test_broken_stub_falls_back_to_real_callee(repo, tmp_path):
    driver = """IDENTIFICATION DIVISION.
PROGRAM-ID. TDRV.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 TD-N PIC 9(4).
PROCEDURE DIVISION.
    MOVE 41 TO TD-N
    CALL 'caller' USING TD-N
    IF TD-N = 42 DISPLAY 'TEST C01 PASS'
    ELSE DISPLAY 'TEST C01 FAIL ' TD-N END-IF
    STOP RUN.
"""
    res = CobolTestRunner(repo, tmp_path / "work").run(
        _unit(repo, "CALLER", driver, BROKEN_HELPER_STUB))
    assert res.status == "passed", (res.compile_log, res.run_log)
    assert res.stubbed_callees == [] and res.real_callees == ["HELPER"]
    assert any("does not compile" in s for s in res.skipped_stubs)
    assert "stub helper failed" in res.compile_log
