"""tests for codocs.cobol.harness: record codec, loader generation, GnuCOBOL execution."""

from __future__ import annotations

import re
import shutil
from decimal import Decimal

import pytest

from codocs.cobol.classify import classify
from codocs.cobol.harness import (
    CobolTestRunner,
    _decode_field,
    compare_records,
    conversion_program,
    encode_record,
    select_and_fd,
)
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.layout import record_fields
from codocs.cobol.models import DataItem, FileUse
from codocs.cobol.reflow import reflow_fixed
from codocs.cobol.testmodels import FixtureFile, GeneratedTest, TestCaseSpec

needs_cobc = pytest.mark.skipif(shutil.which("cobc") is None, reason="GnuCOBOL not installed")


def _cobc_indexed_handler() -> bool:
    """whether cobc was built with an indexed (ISAM) file handler."""
    import subprocess

    try:
        info = subprocess.run(["cobc", "--info"], capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    m = re.search(r"indexed file handler\s*:\s*(\S+)", info, re.I)
    return not (m and m.group(1).lower() in ("disabled", "no", "none"))


needs_isam = pytest.mark.skipif(shutil.which("cobc") is None or not _cobc_indexed_handler(),
                                reason="cobc has no indexed file handler")

REC = DataItem(name="ACCT", children=[
    "05 A-ID PIC 9(5)", "05 A-NAME PIC X(8)", "05 A-BAL PIC S9(5)V99",
    "05 A-PK PIC S9(5)V99 COMP-3", "05 A-BIN PIC S9(4) COMP", "05 A-TAB OCCURS 3",
    "10 A-T PIC 9(2)",
])


def layouts():
    return [record_fields(REC)[1]]


# -- codec -------------------------------------------------------------------------


def test_encode_record_formats_every_usage():
    data, warns = encode_record(
        {"A-ID": "42", "A-NAME": "BOB", "A-BAL": "-12.5", "A-PK": "-12.5", "A-BIN": "10",
         "A-T(2)": "7", "NOPE": "1"}, layouts())
    assert warns == ["unknown field NOPE"]
    assert data[:5] == b"00042"
    assert data[5:13] == b"BOB     "
    assert data[13:20] == b"000125" + bytes([0x70])  # trailing overpunch for negative
    assert data[20:24] == bytes([0x00, 0x01, 0x25, 0x0D])
    assert data[24:26] == b"\x00\x0a"
    assert data[26:32] == b"000700"  # unlisted occurrences default to zero
    assert len(data) == 32


def test_encode_defaults_and_raw_strings():
    data, _ = encode_record({}, layouts())
    assert data[:5] == b"00000" and data[5:13] == b" " * 8
    assert data[20:24] == b"\x00\x00\x00\x0c"
    raw, warns = encode_record("hello", layouts())
    assert raw == b"hello" and warns == []


def test_decode_and_compare_records():
    fields = {f.name: f for f in layouts()[0]}
    data, _ = encode_record({"A-ID": "7", "A-BAL": "3.25", "A-PK": "-1", "A-BIN": "-2"},
                            layouts())
    assert _decode_field(fields["A-BAL"], data[13:20]) == Decimal("3.25")
    assert _decode_field(fields["A-PK"], data[20:24]) == Decimal("-1")
    assert _decode_field(fields["A-BIN"], data[24:26]) == Decimal("-2")
    assert compare_records([{"A-ID": "7", "A-BAL": "3.250"}], [data], layouts()) == ""
    assert "A-BAL" in compare_records([{"A-BAL": "3.26"}], [data], layouts())
    assert "expected 2 records" in compare_records([{}, {}], [data], layouts())
    assert compare_records(["abc  "], [b"abc\x00\x00  "], []) == ""
    assert "record 1" in compare_records(["abd"], [b"abc"], [])


SRC = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. P.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT ACCT-FILE ASSIGN TO ACCTFILE
               ORGANIZATION IS INDEXED
               ACCESS MODE IS RANDOM
               RECORD KEY IS FD-ID
               FILE STATUS IS WS-FS.
       DATA DIVISION.
       FILE SECTION.
       FD  ACCT-FILE RECORD VARYING FROM 1 TO 30 DEPENDING ON WS-LEN.
       01  FD-REC.
           05 FD-ID PIC 9(5).
           05 FD-DATA PIC X(25).
       WORKING-STORAGE SECTION.
       01  WS-FS PIC XX.
"""


def test_select_fd_and_conversion_program():
    sel, fd = select_and_fd([SRC], "ACCT-FILE")
    assert sel.startswith("SELECT ACCT-FILE") and sel.rstrip().endswith(".")
    assert fd.startswith("FD  ACCT-FILE") and "FD-DATA" in fd and "WORKING" not in fd
    f = FileUse(name="ACCT-FILE", assign="ACCTFILE", organization="INDEXED",
                fd_record="FD-REC")
    load = conversion_program(f, [SRC], "fixed", "_in.bin", "acct.dat", load=True)
    assert "ASSIGN TO 'acct.dat'" in load and "ACCESS MODE IS DYNAMIC" in load
    assert "WS-FS" not in load and "DEPENDING" not in load
    assert "MOVE CDL-SEQ-REC TO FD-REC" in load
    unload = conversion_program(f, [SRC], "fixed", "_out.bin", "acct.dat", load=False)
    assert "ACCESS MODE IS SEQUENTIAL" in unload and "MOVE FD-REC TO CDL-SEQ-REC" in unload
    assert all(len(ln) <= 72 for ln in load.split("\n"))
    assert conversion_program(FileUse(name="NOPE"), [SRC], "fixed", "a", "b", True) is None


# -- execution with GnuCOBOL ---------------------------------------------------------


def fx(*lines: str) -> str:
    return "\n".join("       " + ln for ln in lines) + "\n"


REPO = {
    "cpy/ACCTREC.cpy": "\n".join([
        "       01  ACCT-REC.                                                    00010000",
        "           05  ACCT-ID     PIC 9(5).                                    00020000",
        "           05  ACCT-NAME   PIC X(10).                                   00030000",
        "           05  ACCT-BAL    PIC S9(5)V99 COMP-3.                         00040000",
        "           05  FILLER      PIC X(3).                                    00050000",
    ]) + "\n",
    "cpy/FREEPARM.cpy": "01 FP-PARMS.\n   05 FP-AMT PIC 9(5)V99.\n   05 FP-RC PIC XX.\n",
    "cbl/ADDBAL.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. ADDBAL.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-AMT PIC 9(5)V99.", "01 LK-BAL PIC S9(5)V99 COMP-3.",
        "01 LK-RC PIC XX.", "PROCEDURE DIVISION USING LK-AMT LK-BAL LK-RC.", "MAIN-PARA.",
        "    IF LK-AMT > 1000", "        PERFORM REJECT-PARA", "    ELSE",
        "        PERFORM ADD-PARA", "    END-IF", "    GOBACK.", "ADD-PARA.",
        "    ADD LK-AMT TO LK-BAL", "    MOVE '00' TO LK-RC.", "REJECT-PARA.",
        "    CALL 'AUDITLOG' USING LK-AMT", "    MOVE '08' TO LK-RC.",
    ),
    "cbl/ACCTUPD.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. ACCTUPD.", "ENVIRONMENT DIVISION.",
        "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
        "    SELECT ACCT-FILE ASSIGN TO ACCTFILE", "        ORGANIZATION IS INDEXED",
        "        ACCESS MODE IS SEQUENTIAL", "        RECORD KEY IS FD-ACCT-ID",
        "        FILE STATUS IS WS-FS.",
        "    SELECT OUT-FILE ASSIGN TO OUTFILE", "        ORGANIZATION IS SEQUENTIAL.",
        "    SELECT RPT-FILE ASSIGN TO RPTFILE", "        ORGANIZATION IS LINE SEQUENTIAL.",
        "DATA DIVISION.", "FILE SECTION.", "FD  ACCT-FILE.", "01  FD-ACCT.",
        "    05  FD-ACCT-ID   PIC 9(5).", "    05  FD-ACCT-DATA PIC X(17).",
        "FD  OUT-FILE.", "01  OUT-REC PIC X(22).", "FD  RPT-FILE.", "01  RPT-LINE PIC X(40).",
        "WORKING-STORAGE SECTION.", "COPY ACCTREC.", "01  WS-FS  PIC XX.",
        "01  WS-EOF PIC X VALUE 'N'.", "01  WS-AMT PIC 9(5)V99 VALUE 10.",
        "01  WS-RC  PIC XX.", "01  WS-N PIC 9(3) VALUE 0.",
        "PROCEDURE DIVISION.", "MAIN-PARA.",
        "    OPEN INPUT ACCT-FILE OUTPUT OUT-FILE RPT-FILE",
        "    PERFORM UNTIL WS-EOF = 'Y'", "        READ ACCT-FILE INTO ACCT-REC",
        "            AT END MOVE 'Y' TO WS-EOF",
        "            NOT AT END PERFORM UPDATE-PARA", "        END-READ", "    END-PERFORM",
        "    DISPLAY 'UPDATED ' WS-N", "    CLOSE ACCT-FILE OUT-FILE RPT-FILE",
        "    STOP RUN.", "UPDATE-PARA.", "    CALL 'ADDBAL' USING WS-AMT ACCT-BAL WS-RC",
        "    ADD 1 TO WS-N", "    WRITE OUT-REC FROM ACCT-REC",
        "    MOVE SPACES TO RPT-LINE",
        "    STRING ACCT-NAME DELIMITED BY SPACE ' '",
        "        WS-RC DELIMITED BY SIZE INTO RPT-LINE", "    WRITE RPT-LINE.",
    ),
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    for rel, text in REPO.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(root)


def unit_test(index, driver: str, cases: list[str], stubs: str = "") -> GeneratedTest:
    return GeneratedTest(
        program_id="ADDBAL", mode="unit", driver_name="TADDBAL",
        driver_source=reflow_fixed(driver), stubs_source=reflow_fixed(stubs) if stubs else "",
        cases=[TestCaseSpec(id=c) for c in cases],
        testability=classify(index.programs["ADDBAL"], index),
    )


DRIVER = """IDENTIFICATION DIVISION.
PROGRAM-ID. TADDBAL.
DATA DIVISION.
WORKING-STORAGE SECTION.
COPY FREEPARM.
01 TD-BAL PIC S9(5)V99 COMP-3.
PROCEDURE DIVISION.
    MOVE 10 TO FP-AMT MOVE 5.5 TO TD-BAL
    CALL 'ADDBAL' USING FP-AMT TD-BAL FP-RC
    IF TD-BAL = 15.5 AND FP-RC = '00' DISPLAY 'TEST C01 PASS'
    ELSE DISPLAY 'TEST C01 FAIL ' TD-BAL ' ' FP-RC END-IF
    CANCEL 'ADDBAL'
    MOVE 2000 TO FP-AMT
    CALL 'ADDBAL' USING FP-AMT TD-BAL FP-RC
    IF FP-RC = '08' DISPLAY 'TEST C02 PASS' ELSE DISPLAY 'TEST C02 FAIL ' FP-RC END-IF
    IF FP-RC = '99' DISPLAY 'TEST C03 PASS' ELSE DISPLAY 'TEST C03 FAIL rc=' FP-RC END-IF
    STOP RUN.
"""

STUB = """IDENTIFICATION DIVISION.
PROGRAM-ID. AUDITLOG.
DATA DIVISION.
LINKAGE SECTION.
01 LK-X PIC 9(5)V99.
PROCEDURE DIVISION USING LK-X.
    GOBACK.
"""


@needs_cobc
def test_unit_driver_runs_with_stub_free_copybook_and_coverage(repo, tmp_path):
    runner = CobolTestRunner(repo, tmp_path / "work")
    assert runner.available()
    res = runner.run(unit_test(repo, DRIVER, ["C01", "C02", "C03", "C04"], STUB))
    assert res.compiled, res.compile_log
    assert res.status == "failed"
    assert {c.id: c.status for c in res.cases} == {
        "C01": "pass", "C02": "pass", "C03": "fail", "C04": "not-run"}
    assert res.passed == 2 and res.failed == 1
    assert res.paragraphs_total == 3 and res.paragraphs_hit == 3
    # the SUT object is cached by source hash
    assert len(list((tmp_path / "work" / "_modules").glob("ADDBAL-*.o"))) == 1


@needs_cobc
def test_unit_compile_error_and_missing_stub(repo, tmp_path):
    runner = CobolTestRunner(repo, tmp_path / "work")
    bad = runner.run(unit_test(repo, "IDENTIFICATION DIVISION.\nPROGRAM-ID. TADDBAL.\n"
                                     "PROCEDURE DIVISION.\n    MOVE X TO Y.\n", ["C01"]))
    assert bad.status == "compile-error" and not bad.compiled
    assert "error" in bad.compile_log
    # no stub for AUDITLOG: the CALL fails at run time only on the path that needs it
    res = runner.run(unit_test(repo, DRIVER, ["C01", "C02", "C03"]))
    assert res.status == "runtime-error"
    assert res.cases[0].status == "pass"
    assert "AUDITLOG" in res.run_log


@needs_cobc
def test_timeout(repo, tmp_path):
    runner = CobolTestRunner(repo, tmp_path / "work", timeout=2)
    loop = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TADDBAL.\nPROCEDURE DIVISION.\n"
            "    PERFORM UNTIL 1 = 2\n        CONTINUE\n    END-PERFORM.\n")
    res = runner.run(unit_test(repo, loop, ["C01"]))
    assert res.status == "timeout"


@needs_cobc
@needs_isam
def test_batch_with_indexed_loader_and_unloaders(repo, tmp_path):
    t = GeneratedTest(
        program_id="ACCTUPD", mode="batch",
        testability=classify(repo.programs["ACCTUPD"], repo),
        inputs=[FixtureFile(dd="ACCTFILE", path="data/acct.dat", records=[
            {"ACCT-ID": "2", "ACCT-NAME": "BOB", "ACCT-BAL": "1.00"},
            {"ACCT-ID": "1", "ACCT-NAME": "ALICE", "ACCT-BAL": "-5"},
        ])],
        expected_files=[
            FixtureFile(dd="OUTFILE", path="out.dat", records=[
                {"ACCT-ID": "1", "ACCT-BAL": "5.00"},
                {"ACCT-ID": "2", "ACCT-NAME": "BOB", "ACCT-BAL": "11"},
            ]),
            FixtureFile(dd="RPTFILE", path="rpt.txt", content="ALICE 00\nBOB 00\n"),
        ],
        expected_stdout=["UPDATED 002"], expected_return_code=0,
    )
    res = CobolTestRunner(repo, tmp_path / "work").run(t)
    assert res.status == "passed", (res.cases, res.run_log, res.compile_log)
    assert [c.id for c in res.cases] == ["return-code", "stdout-1", "file-OUTFILE",
                                         "file-RPTFILE"]
    assert res.paragraphs_hit == res.paragraphs_total == 2
    assert (tmp_path / "work" / "ACCTUPD" / "data" / "acct.dat").exists()

    t.expected_files[0].records[1]["ACCT-BAL"] = "12"
    res = CobolTestRunner(repo, tmp_path / "work").run(t)
    assert res.status == "failed"
    assert "record 2 ACCT-BAL: expected '12' got 11.00" in res.cases[2].detail


def test_spec_and_generation_error_are_not_executed(repo, tmp_path):
    runner = CobolTestRunner(repo, tmp_path / "work")
    spec = runner.run(GeneratedTest(program_id="ADDBAL", mode="spec",
                                    cases=[TestCaseSpec(id="S1")]))
    assert spec.status == "not-executable" and spec.cases[0].status == "not-run"
    err = runner.run(GeneratedTest(program_id="ADDBAL", mode="unit", error="boom"))
    assert err.status == "generation-error"


def test_run_all_writes_artifacts(repo, tmp_path):
    tests = {"ADDBAL": GeneratedTest(program_id="ADDBAL", mode="spec",
                                     cases=[TestCaseSpec(id="S1")])}
    seen: list[str] = []
    results = CobolTestRunner(repo, tmp_path / "work").run_all(
        tests, tmp_path / "out", workers=2, on_progress=seen.append)
    assert results["ADDBAL"].status == "not-executable"
    assert (tmp_path / "out" / "summary.json").exists()
    assert (tmp_path / "out" / "ADDBAL" / "plan.json").exists()
    assert seen and "not-executable" in seen[0]
