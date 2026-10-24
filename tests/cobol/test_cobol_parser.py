"""tests for codocs.cobol.parser on small inline fixed / free format programs."""

from __future__ import annotations

from codocs.cobol.parser import (
    copy_members,
    is_copybook_text,
    parse_program_file,
    top_level_items,
)


def fx(*lines: str) -> str:
    """fixed format: code at col 8 ('*' first char -> comment indicator in col 7)."""
    out = []
    for ln in lines:
        if ln.startswith(("*", "-", "D ")):
            out.append("      " + ln)
        else:
            out.append("       " + ln)
    return "\n".join(out) + "\n"


BATCH = fx(
    "*****************************************************************",
    "* Program : CUSTRPT",
    "* Function: Print the customer report",
    "* Copyright Example Corp. All rights reserved.",
    "*****************************************************************",
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. CUSTRPT.",
    "AUTHOR. JANE O'BRIEN.",
    "REMARKS. READS CUSTOMERS AND",
    "    WRITES A REPORT.",
    "ENVIRONMENT DIVISION.",
    "INPUT-OUTPUT SECTION.",
    "FILE-CONTROL.",
    "    SELECT CUST-FILE ASSIGN TO CUSTIN",
    "        ORGANIZATION IS INDEXED",
    "        ACCESS MODE IS DYNAMIC",
    "        RECORD KEY IS CUST-ID",
    "        FILE STATUS IS WS-FS.",
    "    SELECT RPT-FILE ASSIGN TO UT-S-RPTOUT",
    "        ORGANIZATION IS LINE SEQUENTIAL.",
    "DATA DIVISION.",
    "FILE SECTION.",
    "FD  CUST-FILE.",
    "01  CUST-REC.",
    "    05 CUST-ID        PIC 9(6).",
    "    05 CUST-NAME      PIC X(30).",
    "FD  RPT-FILE.",
    "01  RPT-LINE         PIC X(132).",
    "WORKING-STORAGE SECTION.",
    "01  WS-FS            PIC XX VALUE '00'.",
    "01  WS-PGM           PIC X(8) VALUE 'DATEUTIL'.",
    "01  WS-OTHER         PIC X(8).",
    "77  WS-COUNT         PIC S9(7) COMP-3 VALUE ZERO.",
    "COPY CUSTCOPY REPLACING ==:PFX:== BY ==WS==.",
    "PROCEDURE DIVISION.",
    "0000-MAIN.",
    "    PERFORM 1000-INIT THRU 1000-EXIT",
    "    PERFORM 2000-PROCESS UNTIL WS-FS = '10'",
    "    PERFORM WS-COUNT TIMES",
    "        CONTINUE",
    "    END-PERFORM",
    "    CALL 'PRINTHDR' USING BY REFERENCE RPT-LINE",
    "                          BY CONTENT LENGTH OF RPT-LINE",
    "    CALL WS-PGM USING WS-COUNT",
    "    MOVE 'AUDITLOG' TO WS-OTHER",
    "    CALL WS-OTHER",
    "    STOP RUN.",
    "1000-INIT.",
    "    OPEN INPUT CUST-FILE",
    "         OUTPUT RPT-FILE.",
    "1000-EXIT.",
    "    EXIT.",
    "2000-PROCESS.",
    "    READ CUST-FILE NEXT",
    "        AT END GO TO 9000-END",
    "    END-READ.",
    "9000-END SECTION.",
    "9000-CLOSE.",
    "    CLOSE CUST-FILE RPT-FILE.",
)

CUSTCOPY = fx(
    "01  :PFX:-CUST-AREA.",
    "    05 :PFX:-LIMIT    PIC 9(5)V99 VALUE 100.",
    "    05 :PFX:-STATUS   PIC X.",
    "       88 :PFX:-ACTIVE VALUE 'A'.",
)


def parse_one(text: str, lookup=None):
    progs = parse_program_file("src/prog.cbl", text, lookup)
    assert len(progs) == 1
    return progs[0]


def test_batch_program_basics():
    p = parse_one(BATCH, {"CUSTCOPY": CUSTCOPY}.get)
    assert p.program_id == "CUSTRPT"
    assert p.format == "fixed"
    assert p.author == "JANE O'BRIEN"
    assert "Print the customer report" in p.header_comment
    assert "WRITES A REPORT" in p.header_comment
    assert "Copyright" not in p.header_comment
    assert "*" not in p.header_comment
    assert p.is_batch_main and not p.has_cics and not p.has_sql
    assert p.copybooks == ["CUSTCOPY"]
    assert p.loc == BATCH.count("\n")
    assert 0 < p.sloc < p.loc


def test_files_select_fd_and_open_modes():
    p = parse_one(BATCH)
    files = {f.name: f for f in p.files}
    cust = files["CUST-FILE"]
    assert cust.assign == "CUSTIN"
    assert cust.organization == "INDEXED"
    assert cust.access == "DYNAMIC"
    assert cust.record_key == "CUST-ID"
    assert cust.fd_record == "CUST-REC"
    assert cust.modes == ["INPUT"]
    rpt = files["RPT-FILE"]
    assert rpt.assign == "UT-S-RPTOUT"
    assert rpt.organization == "LINE SEQUENTIAL"
    assert rpt.modes == ["OUTPUT"]
    assert rpt.via == "native"


def test_calls_static_dynamic_value_and_move():
    p = parse_one(BATCH)
    calls = {(c.target, c.kind) for c in p.calls}
    assert ("PRINTHDR", "call") in calls
    assert ("DATEUTIL", "call-dynamic") in calls  # VALUE clause
    assert ("AUDITLOG", "call-dynamic") in calls  # MOVE literal TO
    hdr = next(c for c in p.calls if c.target == "PRINTHDR")
    assert hdr.using == ["RPT-LINE"]
    dyn = next(c for c in p.calls if c.target == "DATEUTIL")
    assert dyn.using == ["WS-COUNT"]
    assert dyn.via == "WS-PGM"
    assert not p.parse_warnings


def test_unresolved_dynamic_call_keeps_data_name():
    text = fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. DYN.",
        "DATA DIVISION.",
        "LINKAGE SECTION.",
        "01  LK-NAME PIC X(8).",
        "PROCEDURE DIVISION USING LK-NAME.",
        "    CALL LK-NAME",
        "    GOBACK.",
    )
    p = parse_one(text)
    assert p.calls[0].target == "LK-NAME" and p.calls[0].kind == "call-dynamic"
    assert p.calls[0].via == "LK-NAME"
    assert p.parse_warnings
    assert p.using_params == ["LK-NAME"] and not p.is_batch_main


def test_paragraphs_sections_perform_thru_goto():
    p = parse_one(BATCH)
    names = [(x.name, x.kind) for x in p.paragraphs]
    assert names == [
        ("0000-MAIN", "paragraph"),
        ("1000-INIT", "paragraph"),
        ("1000-EXIT", "paragraph"),
        ("2000-PROCESS", "paragraph"),
        ("9000-END", "section"),
        ("9000-CLOSE", "paragraph"),
    ]
    main = p.paragraphs[0]
    assert main.performs == ["1000-INIT", "1000-EXIT", "2000-PROCESS"]  # WS-COUNT TIMES skipped
    assert p.paragraphs[3].performs == ["9000-END"]  # GO TO


def test_data_items_with_copybook_expansion_and_replacing():
    p = parse_one(BATCH, {"CUSTCOPY": CUSTCOPY}.get)
    items = {d.name: d for d in p.data_items}
    assert items["CUST-REC"].section == "FILE"
    assert items["CUST-REC"].children == ["05 CUST-ID PIC 9(6)", "05 CUST-NAME PIC X(30)"]
    assert items["WS-COUNT"].level == 77 and items["WS-COUNT"].picture == "S9(7)"
    area = items["WS-CUST-AREA"]
    assert area.copybook == "CUSTCOPY"
    assert area.section == "WORKING-STORAGE"
    assert "05 WS-LIMIT PIC 9(5)V99 VALUE 100" in area.children
    assert "88 WS-ACTIVE VALUE 'A'" in area.children
    # without a lookup the COPY is still recorded but not expanded
    p2 = parse_one(BATCH)
    assert "WS-CUST-AREA" not in {d.name for d in p2.data_items}
    assert p2.copybooks == ["CUSTCOPY"]


CICS = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. COMENU0C.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 WS-VARS.",
    "   05 WS-TRANID      PIC X(4) VALUE 'CM00'.",
    "   05 WS-FILE        PIC X(8) VALUE 'ACCTDAT '.",
    "   05 WS-NEXT        PIC X(8).",
    "   05 LIT-SIGNON     PIC X(8) VALUE 'COSGN00C'.",
    "01 MENU-OPTIONS-DATA.",
    "   05 FILLER         PIC X(8) VALUE 'COACTVWC'.",
    "   05 FILLER         PIC X(20) VALUE 'ACCOUNT VIEW'.",
    "   05 FILLER         PIC X(8) VALUE 'COBIL00C'.",
    "   05 FILLER         PIC X(20) VALUE 'BILL PAY'.",
    "01 MENU-OPTIONS REDEFINES MENU-OPTIONS-DATA.",
    "   05 MENU-OPT OCCURS 2 TIMES.",
    "      10 MENU-PGM    PIC X(8).",
    "      10 MENU-NAME   PIC X(20).",
    "COPY COCOM01Y.",
    "LINKAGE SECTION.",
    "01 DFHCOMMAREA PIC X(100).",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    EXEC CICS RECEIVE MAP('COMEN1A') MAPSET('COMEN01')",
    "              INTO(COMEN1AI) RESP(WS-RESP) END-EXEC",
    "    EXEC CICS XCTL PROGRAM(MENU-PGM(WS-IDX))",
    "              COMMAREA(WS-VARS) END-EXEC",
    "    MOVE LIT-SIGNON TO WS-NEXT",
    "    EXEC CICS XCTL",
    "              PROGRAM(WS-NEXT)",
    "    END-EXEC",
    "    EXEC CICS LINK PROGRAM('CSUTLDTC') COMMAREA(WS-VARS)",
    "    END-EXEC",
    "    EXEC CICS START TRANSID('CPVD') END-EXEC",
    "    EXEC CICS READ FILE(WS-FILE) INTO(ACCT-REC)",
    "              RIDFLD(ACCT-ID) UPDATE END-EXEC",
    "    EXEC CICS REWRITE DATASET('ACCTDAT') FROM(ACCT-REC) END-EXEC",
    "    EXEC CICS WRITEQ TS QUEUE('AUDITQ') FROM(WS-VARS) END-EXEC",
    "    EXEC CICS SEND MAP('COMEN1A') MAPSET('COMEN01') ERASE",
    "    END-EXEC",
    "    EXEC CICS RETURN TRANSID(WS-TRANID)",
    "              COMMAREA(WS-VARS) LENGTH(LENGTH OF WS-VARS)",
    "    END-EXEC.",
)


def test_cics_transfers_and_commands():
    p = parse_one(CICS)
    assert p.has_cics and not p.is_batch_main
    xctl = sorted(c.target for c in p.calls if c.kind == "cics-xctl")
    assert xctl == ["COACTVWC", "COBIL00C", "COSGN00C"]  # table + MOVE of a VALUE constant
    assert [c.target for c in p.calls if c.kind == "cics-link"] == ["CSUTLDTC"]
    assert [c.target for c in p.calls if c.kind == "cics-start"] == ["CPVD"]
    assert p.transactions == ["CPVD", "CM00"]
    assert p.maps == ["COMEN01"]
    assert p.uses_screen_io
    cmds = [u.command for u in p.cics]
    assert cmds == ["RECEIVE MAP", "READ", "REWRITE", "WRITEQ TS", "SEND MAP", "RETURN"]
    ret = p.cics[-1]
    assert ret.args["TRANSID"] == "WS-TRANID"
    assert ret.args["LENGTH"] == "LENGTH OF WS-VARS"
    assert ret.resolved["TRANSID"] == ["CM00"]
    assert p.cics[1].args["UPDATE"] == ""
    assert p.cics[3].resolved["QUEUE"] == ["AUDITQ"]
    files = [(f.name, f.via, f.modes) for f in p.files]
    assert files == [("ACCTDAT", "cics", ["READ", "REWRITE"])]
    assert p.copybooks == ["COCOM01Y"]


SQL = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. LGIPDB01.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "    EXEC SQL INCLUDE SQLCA END-EXEC.",
    "    EXEC SQL",
    "        INCLUDE LGPOLICY",
    "    END-EXEC.",
    "    EXEC SQL DECLARE CUST_CURSOR CURSOR FOR",
    "        SELECT C.NAME, P.NUM FROM CUSTOMER C, POLICY P",
    "        WHERE C.ID = P.CUSTID",
    "        AND P.NUM IN (SELECT NUM FROM ENDOWMENT)",
    "        AND EXTRACT(YEAR FROM P.ISSUED) > 2000",
    "    END-EXEC.",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    EXEC SQL SELECT NAME INTO :WS-NAME FROM CUSTOMER",
    "             WHERE ID = :WS-ID END-EXEC",
    "    EXEC SQL INSERT INTO DBO.AUDIT (ID) VALUES (:WS-ID) END-EXEC",
    "    EXEC SQL UPDATE POLICY SET X = 1 END-EXEC",
    "    EXEC SQL DELETE FROM MOTOR WHERE ID = :WS-ID END-EXEC",
    "    EXEC SQL OPEN CUST_CURSOR END-EXEC",
    "    EXEC SQL FETCH CUST_CURSOR INTO :WS-NAME, :WS-NUM END-EXEC",
    "    EXEC CICS RETURN END-EXEC.",
)


def test_sql_tables_and_include():
    p = parse_one(SQL)
    assert p.has_sql and p.has_cics
    assert p.copybooks == ["LGPOLICY"]  # SQLCA is not a copybook
    ops = [(s.op, s.tables) for s in p.sql]
    assert ops[0] == ("INCLUDE", [])
    assert p.sql[0].member == "LGPOLICY"
    assert ops[1] == ("DECLARE-CURSOR", ["CUSTOMER", "POLICY", "ENDOWMENT"])
    assert ops[2] == ("SELECT", ["CUSTOMER"])
    assert ops[3] == ("INSERT", ["DBO.AUDIT"])
    assert ops[4] == ("UPDATE", ["POLICY"])
    assert ops[5] == ("DELETE", ["MOTOR"])
    assert ops[6] == ("OPEN", ["CUSTOMER", "POLICY", "ENDOWMENT"])
    assert ops[7][0] == "FETCH"


FREE = """\
*> --- Outer ---
*> Parses things.
IDENTIFICATION DIVISION.
PROGRAM-ID. Outer.
DATA DIVISION.
WORKING-STORAGE SECTION.
    COPY "dd-shared.cpy".
    01 WS-X PIC 9.
PROCEDURE DIVISION.
    PERFORM Do-Work
    CALL "Helper" USING WS-X
    GOBACK.
Do-Work.
    DISPLAY "work"
    .

    *> nested program
    IDENTIFICATION DIVISION.
    PROGRAM-ID. Outer-Inner.
    PROCEDURE DIVISION.
        CALL "Deep"
        GOBACK.
    END PROGRAM Outer-Inner.

END PROGRAM Outer.

*> --- Helper ---
IDENTIFICATION DIVISION.
PROGRAM-ID. Helper.
DATA DIVISION.
LINKAGE SECTION.
    01 LK-X PIC 9.
PROCEDURE DIVISION USING LK-X RETURNING LK-X.
    ENTRY "HelperAlt" USING LK-X
    CALL "Outer-Inner"
    GOBACK.
END PROGRAM Helper.
"""


def test_free_format_multiple_and_nested_programs():
    progs = parse_program_file("src/outer.cob", FREE)
    assert [p.program_id for p in progs] == ["OUTER", "HELPER"]
    outer, helper = progs
    assert outer.format == "free"
    assert outer.nested_programs == ["OUTER-INNER"]
    assert outer.header_comment.startswith("--- Outer ---")
    assert outer.copybooks == ["DD-SHARED"]
    assert [p.name for p in outer.paragraphs] == ["(entry)", "DO-WORK"]
    assert outer.paragraphs[0].performs == ["DO-WORK"]
    targets = {c.target: c for c in outer.calls}
    assert targets["HELPER"].using == ["WS-X"]
    assert "DEEP" in targets  # calls inside nested programs belong to the parent leaf
    assert helper.header_comment == "--- Helper ---"
    assert helper.using_params == ["LK-X"] and helper.returning == "LK-X"
    assert helper.entry_points == ["HELPERALT"]
    assert helper.loc < 15


def test_dli_and_ims_entry():
    text = fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. PDA101.",
        "DATA DIVISION.",
        "LINKAGE SECTION.",
        "01 IO-PCB PIC X(32).",
        "PROCEDURE DIVISION.",
        "    ENTRY 'DLITCBL' USING IO-PCB.",
        "    CALL 'CBLTDLI' USING GU-FUNC IO-PCB MSG-AREA.",
        "    GOBACK.",
    )
    p = parse_one(text)
    assert p.has_dli
    assert p.entry_points == ["DLITCBL"]
    assert not p.is_batch_main


def test_is_copybook_text_and_helpers():
    assert is_copybook_text(CUSTCOPY)
    assert not is_copybook_text(BATCH)
    assert not is_copybook_text("just some prose\nwith words\n")
    assert top_level_items(CUSTCOPY) == [":PFX:-CUST-AREA"]
    frag = fx("05 A PIC X.", "05 B PIC 9.")
    assert top_level_items(frag) == ["A", "B"]
    nested = fx("01 X.", "   COPY INNER.", "   EXEC SQL INCLUDE DCLTAB END-EXEC.")
    assert copy_members(nested) == ["INNER", "DCLTAB"]
