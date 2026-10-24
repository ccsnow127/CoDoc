"""test-execution harness edge cases: format detection with floating comments,
tab expansion, user-defined FUNCTIONs, terminal I/O, dialect fallback, sanitized source
mirror, UNBOUNDED tables in drivers, COPY literals, stub / ENTRY clashes, CALL STATIC."""

from __future__ import annotations

import shutil

import pytest

from codocs.cobol.classify import classify
from codocs.cobol.harness import CobolTestRunner, choose_dialect, link_closure
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.reflow import (
    bound_unbounded,
    reflow_fixed,
    wrap_free_copies,
)
from codocs.cobol.source import detect_format, expand_tabs_fixed, logical_lines
from codocs.cobol.testmodels import GeneratedTest, TestCaseSpec
from codocs.cobol.testprompts import build_test_prompt

needs_cobc = pytest.mark.skipif(shutil.which("cobc") is None, reason="GnuCOBOL not installed")


def fx(*lines: str) -> str:
    """fixed format: '*' lines become comments, others start in col 8."""
    return "\n".join(("      " + ln) if ln.startswith("*") else ("       " + ln)
                     for ln in lines) + "\n"


# -- source: format detection and tabs ------------------------------------------------


def test_floating_comments_in_area_b_keep_fixed_format():
    # cbi print_text.cbl: many indented `*>` comments (past col 7) are legal fixed format
    lines = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. PT.", "PROCEDURE DIVISION."]
    for i in range(10):
        lines += [f"         *> note {i} about the next statement",
                  f"    MOVE {i} TO X"]
    lines.append("    GOBACK.")
    assert detect_format(fx(*lines)) == "fixed"
    # `*>` inside the sequence area is still a free-format signal
    free = "*> header\n*> more\nIDENTIFICATION DIVISION.\nPROGRAM-ID. F.\n"
    assert detect_format(free) == "free"


def test_tabs_expand_so_code_stays_inside_col_72():
    # CardDemo CUSTREC.cpy: two leading tabs written with 3-column stops
    line = "\t\t     05  CUST-FIRST-NAME                         PIC X(25)."
    assert len(line.expandtabs(8)) > 72
    out = expand_tabs_fixed(line)
    assert len(out.rstrip()) <= 72 and out.endswith("PIC X(25).")
    assert out.index("05") >= 7
    assert expand_tabs_fixed("\tMOVE A TO B") == "        MOVE A TO B"
    text = "       01  CUSTOMER-RECORD.\n" + line + "\n"
    assert any("PIC X(25)." in ll.text for ll in logical_lines(text, "fixed"))


# -- user-defined functions, terminal I/O, static calls --------------------------------

REPO = {
    "src/repo.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. repo.", "ENVIRONMENT DIVISION.",
        "CONFIGURATION SECTION.", "REPOSITORY.", "    FUNCTION right-func",
        "    FUNCTION ALL INTRINSIC.", "SPECIAL-NAMES.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "COPY tabbed.", "LOCAL-STORAGE SECTION.",
        "01 LS-N PIC 9(4) COMP.", "LINKAGE SECTION.", "01 L-IN PIC X(10).",
        "01 L-OUT PIC X(10).", "PROCEDURE DIVISION USING L-IN L-OUT.", "MAIN-PARA.",
        "    MOVE right-func(L-IN) TO L-OUT", "    GOBACK.",
        "ENTRY 'get-value' USING L-IN L-OUT.",
        "    MOVE FUNCTION right-func(L-IN) TO L-OUT",
        "    CALL STATIC 'c_noop' USING BY VALUE 0",
        "    GOBACK.",
    ),
    "src/right_func.cbl": fx(
        "IDENTIFICATION DIVISION.", "FUNCTION-ID. right-func.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 L-IN PIC X(10).", "01 L-OUT PIC X(10).",
        "PROCEDURE DIVISION USING L-IN RETURNING L-OUT.",
        "    MOVE FUNCTION REVERSE(L-IN) TO L-OUT", "    GOBACK.",
        "END FUNCTION right-func.",
    ),
    "src/screen.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. screen-io.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 L-X PIC X.", "PROCEDURE DIVISION USING L-X.",
        "    DISPLAY L-X AT 0101", "    ACCEPT OMITTED WITH NO-ECHO TIME-OUT AFTER 1",
        "    GOBACK.",
    ),
    "cpy/tabbed.cpy": "       01  TAB-REC.\n           05  TAB-A   PIC X(4).\n"
                      "\t\t     05  TAB-B                                   PIC X(25).\n",
    "cpy/ubtab.cpy": fx(
        "01  UB-TABLE.", "    05  UB-N   PIC 9(4) COMP.",
        "    05  UB-ROW OCCURS 0 TO UNBOUNDED TIMES DEPENDING ON UB-N.",
        "        10  UB-V  PIC 9(3).",
    ),
    "src/sumtab.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. sumtab.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "01 WS-I PIC 9(4) COMP.", "LINKAGE SECTION.",
        "COPY ubtab.", "01 L-SUM PIC 9(6).", "PROCEDURE DIVISION USING UB-TABLE L-SUM.",
        "    MOVE 0 TO L-SUM",
        "    PERFORM VARYING WS-I FROM 1 BY 1 UNTIL WS-I > UB-N",
        "        ADD UB-V(WS-I) TO L-SUM", "    END-PERFORM", "    GOBACK.",
    ),
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("fnrepo")
    for rel, text in REPO.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(root)


def test_function_units_are_indexed_and_referenced(repo):
    fn = repo.programs["RIGHT-FUNC"]
    assert fn.unit_kind == "function" and fn.returning == "L-OUT"
    prog = repo.programs["REPO"]
    assert prog.unit_kind == "program"
    calls = [(c.target, c.kind, c.resolved) for c in prog.calls if c.kind == "function"]
    assert calls == [("RIGHT-FUNC", "function", True)]  # ALL INTRINSIC is not a call
    assert repo.callees("REPO") == ["RIGHT-FUNC"]
    assert repo.callers("RIGHT-FUNC") == ["REPO"]
    assert link_closure(repo, "REPO") == ["RIGHT-FUNC"]
    static = [c for c in prog.calls if c.target == "C_NOOP"]
    assert static and static[0].static


def test_classification_of_functions_terminal_io_and_static_calls(repo):
    t = classify(repo.programs["RIGHT-FUNC"], repo)
    assert t.mode == "unit" and "FUNCTION" in t.reasons[0]
    t = classify(repo.programs["REPO"], repo)
    assert t.mode == "unit" and t.callees == ["RIGHT-FUNC"]
    assert t.external_callees == []  # CALL STATIC 'c_noop' is linked to a harness no-op
    assert repo.programs["SCREEN-IO"].uses_terminal_io
    t = classify(repo.programs["SCREEN-IO"], repo)
    assert t.mode == "spec" and "terminal" in t.reasons[0]


def test_function_prompt_explains_invocation(repo):
    prog = repo.programs["RIGHT-FUNC"]
    t = classify(prog, repo)
    msgs = build_test_prompt(t, prog, repo.program_source("RIGHT-FUNC"), {}, None,
                             dialect="default")
    text = msgs[1]["content"]
    assert "FUNCTION right-func(L-IN)" in text and "Never CALL or CANCEL it" in text
    assert "default GnuCOBOL dialect" in text
    prog = repo.programs["REPO"]
    msgs = build_test_prompt(classify(prog, repo), prog, repo.program_source("REPO"), {},
                             None)
    assert "ENTRY points (GET-VALUE)" in msgs[1]["content"]


# -- reflow: COPY literals and UNBOUNDED ------------------------------------------------


def test_copy_literal_is_never_continued():
    src = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. T.\nDATA DIVISION.\n"
           "WORKING-STORAGE SECTION.\n"
           "           COPY \"copybooks/linkage_section/l_select_boundary_table.cpy\".\n"
           "           COPY \"a/very/long/directory/path/that/cannot/fit/in/area/b/"
           "member_name.cpy\".\n")
    out = reflow_fixed(src)
    assert all(ln[6:7] != "-" for ln in out.split("\n") if len(ln) > 6)
    assert '"copybooks/linkage_section/l_select_boundary_table.cpy".' in out
    assert '"member_name.cpy".' in out  # cut to the file name, found via -I
    assert reflow_fixed(out) == out


def test_bound_unbounded_outside_linkage_only():
    members = {"UBTAB": REPO["cpy/ubtab.cpy"], "PLAIN": fx("01 P PIC X.")}
    src = reflow_fixed(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. T.\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\nCOPY\n    \"cpy/ubtab.cpy\".\nCOPY PLAIN.\n"
        "01 W-T.\n  05 W-N PIC 9(4) COMP.\n"
        "  05 W-R OCCURS 0 TO UNBOUNDED TIMES DEPENDING ON W-N PIC X.\n"
        "LINKAGE SECTION.\nCOPY UBTAB.\nPROCEDURE DIVISION.\n    GOBACK.\n")
    out, notes = bound_unbounded(src, members.get, 99)
    assert len(notes) == 2
    ws, link = out.split("LINKAGE SECTION")
    assert "REPLACING ==UNBOUNDED== BY ==99==" in ws and "TO 99 TIMES" in ws
    assert "UNBOUNDED" not in ws.replace("==UNBOUNDED==", "")
    assert "COPY UBTAB." in link  # legal in LINKAGE: untouched
    assert "COPY PLAIN." in ws
    # a split COPY statement is still recognized when wrapping free-format members
    wrapped = wrap_free_copies(out, lambda m: m == "UBTAB")
    assert ">>SOURCE FORMAT FREE" in wrapped.split("LINKAGE")[0]


# -- runner (GnuCOBOL) -------------------------------------------------------------------


@needs_cobc
def test_choose_dialect_prefers_a_dialect_without_warnings(tmp_path):
    native = tmp_path / "native.cbl"
    native.write_text(fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. NATIVE.", "ENVIRONMENT DIVISION.",
        "CONFIGURATION SECTION.", "REPOSITORY.", "    FUNCTION ALL INTRINSIC.",
        "SPECIAL-NAMES.", "PROCEDURE DIVISION.", "    DISPLAY TRIM(' X ')", "    GOBACK."))
    name, note = choose_dialect("cobc", native, "fixed", [])
    assert name == "default" and "ibm: 1 dialect warning" in note
    ibm = tmp_path / "ibm.cbl"
    ibm.write_text(fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. IBMPGM.",
                      "PROCEDURE DIVISION.", "    DISPLAY 'X'", "    GOBACK."))
    assert choose_dialect("cobc", ibm, "fixed", [])[0] == "ibm"
    bad = tmp_path / "bad.cbl"
    bad.write_text(fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. BAD.",
                      "PROCEDURE DIVISION.", "    MOVE X TO Y."))
    name, note = choose_dialect("cobc", bad, "fixed", [])
    assert name == "ibm" and note.startswith("no dialect compiles")


def _unit(index, pid: str, driver: str, cases: list[str], stubs: str = "") -> GeneratedTest:
    return GeneratedTest(
        program_id=pid, mode="unit", driver_name="TDRV", driver_source=reflow_fixed(driver),
        stubs_source=reflow_fixed(stubs) if stubs else "",
        cases=[TestCaseSpec(id=c) for c in cases],
        testability=classify(index.programs[pid], index),
    )


@needs_cobc
def test_function_linking_tab_mirror_entry_clash_and_static_noop(repo, tmp_path):
    # plan callees without function edges (older plans): the runner adds RIGHT-FUNC itself
    driver = """IDENTIFICATION DIVISION.
PROGRAM-ID. TDRV.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 TD-IN PIC X(10).
01 TD-OUT PIC X(10).
PROCEDURE DIVISION.
    MOVE 'abc' TO TD-IN
    CALL 'get-value' USING TD-IN TD-OUT
    IF TD-OUT = '       cba' DISPLAY 'TEST C01 PASS'
    ELSE DISPLAY 'TEST C01 FAIL [' TD-OUT ']' END-IF
    STOP RUN.
"""
    # an LLM stub for an ENTRY of a linked repo program must not be linked (duplicate
    # symbol); an upper-case stub of the static C routine does not provide 'c_noop'
    stubs = """IDENTIFICATION DIVISION.
PROGRAM-ID. get-value.
PROCEDURE DIVISION.
    GOBACK.
IDENTIFICATION DIVISION.
PROGRAM-ID. C_NOOP.
PROCEDURE DIVISION.
    GOBACK.
"""
    test = _unit(repo, "REPO", driver, ["C01"], stubs)
    test.testability.callees = []
    runner = CobolTestRunner(repo, tmp_path / "work")
    res = runner.run(test)
    assert res.status == "passed", (res.compile_log, res.run_log)
    assert res.dialect == "default"  # REPOSITORY before SPECIAL-NAMES: ibm warns
    assert "stub get-value skipped" in res.compile_log
    assert "CALL STATIC 'c_noop'" in res.compile_log
    # the tabbed copybook is compiled from the sanitized mirror
    mirrored = (tmp_path / "work" / "_src" / "cpy" / "tabbed.cpy").read_text()
    assert "\t" not in mirrored and "PIC X(25)." in mirrored


@needs_cobc
def test_driver_copying_unbounded_linkage_copybook(repo, tmp_path):
    driver = """IDENTIFICATION DIVISION.
PROGRAM-ID. TDRV.
DATA DIVISION.
WORKING-STORAGE SECTION.
COPY ubtab.
01 TD-SUM PIC 9(6).
PROCEDURE DIVISION.
    INITIALIZE UB-TABLE
    MOVE 2 TO UB-N
    MOVE 5 TO UB-V(1)
    MOVE 7 TO UB-V(2)
    CALL 'sumtab' USING UB-TABLE TD-SUM
    IF TD-SUM = 12 DISPLAY 'TEST C01 PASS' ELSE DISPLAY 'TEST C01 FAIL ' TD-SUM END-IF
    STOP RUN.
"""
    res = CobolTestRunner(repo, tmp_path / "work").run(_unit(repo, "SUMTAB", driver, ["C01"]))
    assert res.status == "passed", (res.compile_log, res.run_log)
    assert "UNBOUNDED tables bounded" in res.compile_log
