"""tests for codocs.cobol.classify on a small synthetic repo."""

from __future__ import annotations

import pytest

from codocs.cobol.classify import classify, is_runtime_routine
from codocs.cobol.index import CobolRepoIndex


def fx(*lines: str) -> str:
    return "\n".join(("      " + ln) if ln.startswith("*") else ("       " + ln)
                     for ln in lines) + "\n"


FILES = {
    "cbl/CALCSUB.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. CALCSUB.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-A PIC 9(3).", "01 LK-B PIC 9(3).",
        "PROCEDURE DIVISION USING LK-A LK-B.", "MAIN-PARA.",
        "    CALL 'CBL_TOUPPER' USING LK-A", "    CALL 'CEEDAYS' USING LK-A",
        "    COMPUTE LK-B = LK-A * 2", "    GOBACK.",
    ),
    "cbl/BATCHPGM.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. BATCHPGM.", "ENVIRONMENT DIVISION.",
        "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
        "    SELECT IN-FILE ASSIGN TO INFILE ORGANIZATION IS INDEXED",
        "        ACCESS MODE IS SEQUENTIAL RECORD KEY IS IN-KEY.",
        "DATA DIVISION.", "FILE SECTION.", "FD IN-FILE.", "01 IN-REC.",
        "    05 IN-KEY PIC X(5).", "    05 IN-DATA PIC X(20).",
        "WORKING-STORAGE SECTION.", "01 WS-A PIC 9(3).", "01 WS-B PIC 9(3).",
        "PROCEDURE DIVISION.", "MAIN-PARA.", "    OPEN INPUT IN-FILE",
        "    CALL 'CALCSUB' USING WS-A WS-B", "    CALL 'EXTRTN'",
        "    CLOSE IN-FILE", "    STOP RUN.",
    ),
    "cbl/ONLINE.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. ONLINE.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "01 WS-X PIC X.", "PROCEDURE DIVISION.",
        "    EXEC CICS RETURN END-EXEC.",
    ),
    "cbl/USEONL.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. USEONL.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-A PIC X.", "PROCEDURE DIVISION USING LK-A.",
        "    CALL 'ONLINE'", "    GOBACK.",
    ),
    "cbl/DBPGM.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. DBPGM.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-A PIC X.", "PROCEDURE DIVISION USING LK-A.",
        "    EXEC SQL SELECT A INTO :LK-A FROM T END-EXEC", "    GOBACK.",
    ),
    "cbl/SCRPGM.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. SCRPGM.", "DATA DIVISION.",
        "SCREEN SECTION.", "01 S-MAIN.", "    05 LINE 1 COL 1 VALUE 'HELLO'.",
        "PROCEDURE DIVISION.", "    DISPLAY S-MAIN", "    STOP RUN.",
    ),
}


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(root)


def test_unit_program_with_external_callee(index):
    t = classify(index.programs["CALCSUB"], index)
    assert t.mode == "unit"
    assert t.reasons[0].startswith("PROCEDURE DIVISION USING")
    assert t.callees == []
    # GnuCOBOL runtime routines never need stubs; LE routines do
    assert t.external_callees == ["CEEDAYS"]


def test_batch_program_builds_callees_and_stubs_externals(index):
    t = classify(index.programs["BATCHPGM"], index)
    assert t.mode == "batch"
    assert t.callees == ["CALCSUB"]
    assert set(t.external_callees) == {"EXTRTN", "CEEDAYS"}
    assert any("INDEXED" in r for r in t.reasons)


def test_spec_for_cics_sql_screen_and_cics_callees(index):
    assert classify(index.programs["ONLINE"], index).mode == "spec"
    db = classify(index.programs["DBPGM"], index)
    assert db.mode == "spec" and "SQL" in db.reasons[0]
    scr = classify(index.programs["SCRPGM"], index)
    assert scr.mode == "spec" and "screen" in scr.reasons[0]
    use = classify(index.programs["USEONL"], index)
    assert use.mode == "spec"
    assert any("calls ONLINE" in r for r in use.reasons)


def test_runtime_routines():
    assert is_runtime_routine("CBL_TOUPPER")
    assert is_runtime_routine("C$SLEEP")
    assert is_runtime_routine("system")
    assert not is_runtime_routine("CEE3ABD")


def test_positioned_display_callee_does_not_make_callers_spec(tmp_path):
    # a positioned DISPLAY sets uses_screen_io as well as uses_terminal_io; only the
    # program itself needs a terminal, its callers stay testable (cbi: INKEY-FUNC callers)
    files = {
        "cbl/POSDSP.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. POSDSP.", "DATA DIVISION.",
            "LINKAGE SECTION.", "01 LK-A PIC X.", "PROCEDURE DIVISION USING LK-A.",
            "    DISPLAY LK-A AT LINE 1 COL 1 WITH FOREGROUND-COLOR 2", "    GOBACK.",
        ),
        "cbl/CALLPOS.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. CALLPOS.", "DATA DIVISION.",
            "LINKAGE SECTION.", "01 LK-A PIC X.", "PROCEDURE DIVISION USING LK-A.",
            "    CALL 'POSDSP' USING LK-A", "    GOBACK.",
        ),
        "cbl/SCRPGM.cbl": FILES["cbl/SCRPGM.cbl"].replace(
            "PROCEDURE DIVISION.", "LINKAGE SECTION.\n       01 LK-A PIC X.\n"
            "       PROCEDURE DIVISION USING LK-A."),
        "cbl/CALLSCR.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. CALLSCR.", "DATA DIVISION.",
            "LINKAGE SECTION.", "01 LK-A PIC X.", "PROCEDURE DIVISION USING LK-A.",
            "    CALL 'SCRPGM' USING LK-A", "    GOBACK.",
        ),
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    idx = CobolRepoIndex.build(tmp_path)
    pos = idx.programs["POSDSP"]
    assert pos.uses_screen_io and pos.uses_terminal_io
    assert classify(pos, idx).mode == "spec"
    caller = classify(idx.programs["CALLPOS"], idx)
    assert caller.mode == "unit", caller.reasons
    assert not any("POSDSP" in r for r in caller.reasons)
    # a SCREEN SECTION callee still blocks its callers
    scr_caller = classify(idx.programs["CALLSCR"], idx)
    assert scr_caller.mode == "spec"
    assert any("calls SCRPGM, which uses screen I/O" in r for r in scr_caller.reasons)
