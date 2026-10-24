"""the EXEC precompile API and neutraliser: every rule on snippets, the
line map, a custom rewrite, and the neutralised programs compiling with GnuCOBOL."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from runner_fixtures import fx

from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.precompile import (
    STUB_DIR,
    bms_copybook,
    neutralize,
    precompile,
    scan_exec,
    stub_members,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COBOLMOD = FIXTURES / "cobolmod"

SQLPROG = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. SQLP.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 WS-ID PIC 9(4).",
    "    EXEC SQL DECLARE C1 CURSOR FOR",
    "        SELECT ID FROM T1 WHERE ID > :WS-ID",
    "    END-EXEC.",
    "    EXEC SQL",
    "        INCLUDE CUSTREC",
    "    END-EXEC.",
    "LINKAGE SECTION.",
    "01 LK-X PIC X.",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    EXEC SQL OPEN C1 END-EXEC",
    "    IF SQLCODE = 0",
    "        EXEC SQL",
    "            FETCH C1 INTO :WS-ID",
    "        END-EXEC",
    "    END-IF",
    "    EXEC CICS RETURN END-EXEC.",
)


def _compile(text: str, dirs: list[Path], tmp: Path, extra: list[str] = ()) -> tuple[bool, str]:
    p = tmp / "p.cbl"
    p.write_text(text)
    args = ["cobc", "-fsyntax-only", "-std=ibm", "-frelax-syntax-checks", *extra]
    for d in dirs:
        args += ["-I", str(d)]
    r = subprocess.run([*args, str(p)], capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def test_scan_exec_blocks():
    blocks = scan_exec(SQLPROG)
    assert [(b.kind, b.command, b.division) for b in blocks] == [
        ("SQL", "DECLARE", "DATA"), ("SQL", "INCLUDE", "DATA"), ("SQL", "OPEN", "PROCEDURE"),
        ("SQL", "FETCH", "PROCEDURE"), ("CICS", "RETURN", "PROCEDURE")]
    assert blocks[1].options == {"MEMBER": "CUSTREC"}
    assert (blocks[3].start_line, blocks[3].end_line) == (18, 20)
    cics = scan_exec(fx("PROCEDURE DIVISION.",
                        "    EXEC CICS SEND MAP('M1') MAPSET('S1') ERASE END-EXEC"))[0]
    assert cics.command == "SEND MAP" and cics.options["MAPSET"] == "'S1'" and \
        "ERASE" in cics.options


def test_rules_on_snippets(tmp_path: Path):
    (tmp_path / "cpy").mkdir()
    # named as the program spells it: cobc on Linux does not fold COPY names
    (tmp_path / "cpy" / "CUSTREC.cpy").write_text(fx("01 CUST-REC.", "   05 C-ID PIC 9(4)."))
    pre = neutralize(SQLPROG, "fixed", [tmp_path / "cpy"], gen_dir=tmp_path / "gen")
    lines = pre.text.split("\n")
    # DATA DIVISION SQL blanked; a multi-line INCLUDE -> COPY
    assert not any("DECLARE" in ln or "SELECT ID" in ln for ln in lines)
    assert any(ln.strip() == "COPY CUSTREC." for ln in lines)
    # PROCEDURE DIVISION blocks -> CONTINUE, the sentence period kept
    assert sum(ln.strip() == "CONTINUE" for ln in lines) == 2
    assert any(ln.strip() == "CONTINUE." for ln in lines)
    # implicit SQLCA (SQLCODE referenced without INCLUDE SQLCA), last in WS
    ws_end = next(i for i, ln in enumerate(lines) if "LINKAGE SECTION" in ln)
    assert lines[ws_end - 1].strip() == "COPY SQLCA."
    assert len(pre.line_map) == len(lines)
    assert pre.line_map[ws_end - 1] == pre.line_map[ws_end]     # inserted before LINKAGE
    assert pre.line_map[-1] == len(SQLPROG.split("\n"))
    assert STUB_DIR in pre.include_dirs
    assert {"DFHAID", "DFHBMSCA", "DFHEIBLK", "SQLCA", "SQLDA", "DLIDIB", "CMQV",
            "CMQGMOV", "CMQODV", "CMQMDV", "CMQPMOV", "CMQTML"} <= stub_members()
    if not _has_cobc():
        return
    ok, err = _compile(pre.text, [tmp_path / "cpy", *pre.include_dirs], tmp_path)
    assert ok, err


def test_eib_dib_resp_and_cmq(tmp_path: Path):
    src = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. ONL.", "DATA DIVISION.",
             "WORKING-STORAGE SECTION.", "01 WS-RESP PIC S9(8) COMP.", "COPY DFHAID.",
             "01 MQ-CONSTANTS.", "COPY CMQV.", "PROCEDURE DIVISION.",
             "    IF EIBCALEN = 0 AND EIBAID = DFHENTER",
             "        EXEC CICS RETURN END-EXEC", "    END-IF",
             "    IF WS-RESP = DFHRESP(NORMAL) AND MQCC-OK = 0",
             "        EXEC DLI GU USING PCB(1) SEGMENT(ROOT) END-EXEC",
             "        DISPLAY DIBSTAT", "    END-IF", "    GOBACK.")
    pre = precompile(src, "fixed", [])
    text = pre.text
    assert "DFHRESP" not in text and "WS-RESP = 0" in text
    lines = text.split("\n")
    link = next(i for i, ln in enumerate(lines) if ln.strip() == "LINKAGE SECTION.")
    assert [ln.strip() for ln in lines[link + 1:link + 3]] == ["COPY DFHEIBLK.",
                                                              "COPY DLIDIB."]
    assert "PROCEDURE DIVISION" in lines[link + 3]
    assert not any("no stub for" in n for n in pre.notes)
    missing = precompile(src.replace("COPY CMQV.", "COPY CMQXYZ."), "fixed", [])
    assert "compile gate off: no stub for CMQXYZ" in missing.notes
    if _has_cobc():
        ok, err = _compile(text, pre.include_dirs, tmp_path)
        assert ok, err


def test_custom_rewrite_and_line_map():
    def stub(block):
        return [f"CALL 'CDGCICS' USING '{block.command}'", "CONTINUE"]

    src = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. ONL.", "PROCEDURE DIVISION.",
             "    EXEC CICS SEND TEXT FROM(X) END-EXEC", "    GOBACK.")
    pre = precompile(src, "fixed", [], rewrite=stub)
    lines = pre.text.split("\n")
    at = next(i for i, ln in enumerate(lines) if "CDGCICS" in ln)
    assert "'SEND TEXT'" in lines[at] and lines[at + 1].strip() == "CONTINUE"
    assert pre.line_map[at] == 4 and pre.line_map[at + 1] == 5
    assert pre.blocks[0].command == "SEND TEXT"


def test_bms_symbolic_map_is_generated(tmp_path: Path):
    text = (COBOLMOD / "bms" / "ACCTSET.bms").read_text()
    cpy = bms_copybook(text, "ACCTSET")
    assert "01  ACCTMAPI." in cpy and "02  ACCTIDI PIC X(9)." in cpy
    assert "01  ACCTMAPO REDEFINES ACCTMAPI." in cpy and "02  MSGO PIC X(40)." in cpy
    index = CobolRepoIndex.build(COBOLMOD)
    src = (COBOLMOD / "cbl" / "ACCTINQ.cbl").read_text().replace("COPY ACCTSET.",
                                                                  "COPY ACCTSET.")
    # without the cpy/ dir the mapset copybook does not resolve: it is generated
    pre = neutralize(src, "fixed", [], index=index, gen_dir=tmp_path / "gen")
    assert (tmp_path / "gen" / "ACCTSET.cpy").is_file()
    assert any("BMS symbolic map ACCTSET generated" in n for n in pre.notes)
    if _has_cobc():
        dirs = [COBOLMOD / "cpy"]
        ok, err = _compile(pre.text, [*dirs, *pre.include_dirs], tmp_path)
        assert ok, err


def _has_cobc() -> bool:
    import shutil

    return shutil.which("cobc") is not None


@pytest.mark.needs_cobc
def test_cobolmod_cics_program_compiles_neutralised(tmp_path: Path):
    index = CobolRepoIndex.build(COBOLMOD)
    from codocs.cobol.harness import CobolTestRunner

    h = CobolTestRunner(index, tmp_path / "w")
    ok, errors, _ = h.compile_check("cbl/ACCTINQ.cbl", neutralize=True)
    assert ok, errors
    ok, errors, _ = h.compile_check("cbl/ACCTINQ.cbl")
    assert not ok and errors[0].startswith("cbl/ACCTINQ.cbl:")
    # an error in neutralised text maps back to the original line
    bad = (COBOLMOD / "cbl" / "ACCTINQ.cbl").read_text().replace(
        "MOVE ACCT-NAME TO NAMEO", "MOVE ACCT-NAME TO TO NAMEO")
    ok, errors, _ = h.compile_check("cbl/ACCTINQ.cbl", bad, neutralize=True)
    line = next(i for i, ln in enumerate(bad.split("\n"), 1) if "TO TO NAMEO" in ln)
    assert not ok and errors[0].startswith(f"cbl/ACCTINQ.cbl:{line}:")


def test_dcl_members_are_neutralised_and_searched(tmp_path: Path):
    repo = tmp_path / "db2"
    (repo / "dcl").mkdir(parents=True)
    (repo / "cbl").mkdir()
    (repo / "dcl" / "DCLT1.dcl").write_text(fx(
        "    EXEC SQL DECLARE T1 TABLE", "    ( ID INTEGER NOT NULL ) END-EXEC.",
        "01 DCLT1.", "   10 T1-ID PIC S9(9) COMP."))
    (repo / "cbl" / "DB2P.cbl").write_text(fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. DB2P.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "    EXEC SQL INCLUDE DCLT1 END-EXEC.",
        "    EXEC SQL INCLUDE SQLCA END-EXEC.", "PROCEDURE DIVISION.",
        "    EXEC SQL SELECT ID INTO :T1-ID FROM T1 END-EXEC",
        "    IF SQLCODE NOT = 0 DISPLAY 'ERR' END-IF", "    GOBACK."))
    index = CobolRepoIndex.build(repo)
    from codocs.cobol.harness import CobolTestRunner

    h = CobolTestRunner(index, tmp_path / "w")
    assert h._dcl_flags("DB2P") == ["-ext", "dcl"]
    if _has_cobc():
        ok, errors, log = h.compile_check("cbl/DB2P.cbl", neutralize=True)
        assert ok, (errors, log)
