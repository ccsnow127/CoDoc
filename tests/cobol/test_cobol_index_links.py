"""tests for index linking: ENTRY owners, CICS file stores and JCL DDs of CALLed programs."""

from __future__ import annotations

from pathlib import Path

from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.jcl import vsam_definitions


def fx(*lines: str) -> str:
    return "\n".join(("      " if ln.startswith("*") else "       ") + ln for ln in lines) + "\n"


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_call_to_entry_point_records_its_owner(tmp_path: Path):
    write(tmp_path, {
        "repo.cob": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. VARIABLE-REPOSITORY.",
            "PROCEDURE DIVISION.", "    GOBACK.",
            "    ENTRY 'get-variable'.", "    GOBACK.",
        ),
        "main.cob": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. MAINP.", "PROCEDURE DIVISION.",
            "    CALL 'get-variable'", "    CALL 'VARIABLE-REPOSITORY'", "    STOP RUN.",
        ),
    })
    idx = CobolRepoIndex.build(tmp_path)
    calls = {c.target: c for c in idx.programs["MAINP"].calls}
    assert calls["GET-VARIABLE"].owner == "VARIABLE-REPOSITORY"
    assert calls["GET-VARIABLE"].resolved
    assert calls["VARIABLE-REPOSITORY"].owner == ""  # a PROGRAM-ID, not an ENTRY


def test_vsam_definitions_from_idcams():
    text = (
        "//DEF EXEC PGM=IDCAMS\n//SYSIN DD *\n"
        " DEFINE CLUSTER(NAME(HLQ.APP.KSDSCUST) -\n          INDEXED -\n"
        "        KEYS(10 0)) -\n   DATA(NAME(HLQ.APP.KSDSCUST.DATA)) -\n"
        "   INDEX(NAME(HLQ.APP.KSDSCUST.INDEX))\n"
        " DEFINE CLUSTER (NAME(HLQ.APP.ESDS) NONINDEXED)\n"
        " DEFINE CLUSTER (NAME(HLQ.APP.DEFAULT) KEYS(4 0))\n"
        " DEFINE AIX (NAME(HLQ.APP.AIX) RELATE(HLQ.APP.KSDSCUST))\n"
        " DEFINE PATH (NAME(HLQ.APP.PATH) PATHENTRY(HLQ.APP.AIX))\n/*\n"
    )
    assert vsam_definitions(text) == [
        ("HLQ.APP.KSDSCUST", "INDEXED"),
        ("HLQ.APP.ESDS", "SEQUENTIAL"),
        ("HLQ.APP.DEFAULT", "INDEXED"),
        ("HLQ.APP.PATH", "INDEXED"),
    ]


def test_cics_file_stores_are_cics_files_with_vsam_organization(tmp_path: Path):
    write(tmp_path, {
        "src/lgicvs01.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. LGICVS01.", "DATA DIVISION.",
            "WORKING-STORAGE SECTION.", "01 WS-REC PIC X(80).", "01 WS-KEY PIC X(10).",
            "PROCEDURE DIVISION.",
            "    EXEC CICS READ FILE('KSDSCUST') INTO(WS-REC)",
            "         RIDFLD(WS-KEY) END-EXEC",
            "    EXEC CICS WRITE FILE('PLAINF') FROM(WS-REC)",
            "         RIDFLD(WS-KEY) END-EXEC",
            "    EXEC CICS READ FILE('NOCSD') INTO(WS-REC)",
            "         RIDFLD(WS-KEY) END-EXEC",
            "    EXEC CICS RETURN END-EXEC.",
        ),
        "cntl/cdef.jcl": (
            "//CDEF JOB CLASS=A\n//S1 EXEC PGM=DFHCSDUP\n//SYSIN DD *\n"
            "  Define File(KSDSCUST)    Group(GENASAF)\n"
            "         DSname(<USRHLQ>.GENAPP.KSDSCUST)\n"
            "         Recordsize(82)    Keylength(10)\n"
            "  Define File(PLAINF)    Group(GENASAF)\n"
            "         DSname(<USRHLQ>.GENAPP.PLAIN)\n/*\n"
        ),
    })
    idx = CobolRepoIndex.build(tmp_path)
    cust = idx.datastores["<USRHLQ>.GENAPP.KSDSCUST"]
    assert cust.kind == "cics-file" and cust.organization == "INDEXED"
    assert cust.readers == ["LGICVS01"] and "KSDSCUST" in cust.aliases
    plain = idx.datastores["<USRHLQ>.GENAPP.PLAIN"]
    assert plain.kind == "cics-file" and plain.organization == ""  # nothing says KSDS
    assert idx.datastores["NOCSD"].kind == "cics-file"


def test_jcl_dds_reach_statically_called_subprograms(tmp_path: Path):
    # carddemo CREASTMT: STEP040 runs CBSTM03A, which CALLs CBSTM03B; CBSTM03B owns the
    # SELECTs, so the step's DDs must map its files to the same DSN stores
    write(tmp_path, {
        "cbl/CBSTM03A.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. CBSTM03A.",
            "ENVIRONMENT DIVISION.", "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
            "    SELECT STMT-FILE ASSIGN TO STMTFILE.",
            "DATA DIVISION.", "FILE SECTION.", "FD STMT-FILE.", "01 STMT-REC PIC X(80).",
            "PROCEDURE DIVISION.",
            "    OPEN OUTPUT STMT-FILE",
            "    CALL 'CBSTM03B'",
            "    STOP RUN.",
        ),
        "cbl/CBSTM03B.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. CBSTM03B.",
            "ENVIRONMENT DIVISION.", "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
            "    SELECT ACCT-FILE ASSIGN TO ACCTFILE",
            "        ORGANIZATION IS INDEXED ACCESS MODE IS RANDOM",
            "        RECORD KEY IS ACCT-ID.",
            "DATA DIVISION.", "FILE SECTION.", "FD ACCT-FILE.", "01 ACCT-REC.",
            "   05 ACCT-ID PIC 9(11).",
            "PROCEDURE DIVISION.",
            "    OPEN INPUT ACCT-FILE",
            "    GOBACK.",
        ),
        "jcl/CREASTMT.jcl": (
            "//CREASTMT JOB CLASS=A\n"
            "//STEP040 EXEC PGM=CBSTM03A\n"
            "//ACCTFILE DD DISP=SHR,DSN=AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS\n"
            "//STMTFILE DD DISP=(NEW,CATLG),DSN=AWS.M2.CARDDEMO.STATEMNT.PS\n"
        ),
    })
    idx = CobolRepoIndex.build(tmp_path)
    acct = idx.datastores["AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS"]
    assert acct.readers == ["CBSTM03B"]
    assert acct.jobs == ["CREASTMT"] and acct.organization == "INDEXED"
    assert "ACCTFILE" not in idx.datastores  # no separate store keyed by the DD name
    stmt = idx.datastores["AWS.M2.CARDDEMO.STATEMNT.PS"]
    assert stmt.writers == ["CBSTM03A"]


def test_ksds_qualifier_sets_indexed(tmp_path: Path):
    write(tmp_path, {
        "cbl/P.cbl": fx(
            "IDENTIFICATION DIVISION.", "PROGRAM-ID. P.",
            "ENVIRONMENT DIVISION.", "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
            "    SELECT IN-FILE ASSIGN TO TRNX.",
            "DATA DIVISION.", "FILE SECTION.", "FD IN-FILE.", "01 IN-REC PIC X(80).",
            "PROCEDURE DIVISION.", "    OPEN INPUT IN-FILE", "    STOP RUN.",
        ),
        "jcl/J.jcl": "//J JOB CLASS=A\n//S1 EXEC PGM=P\n//TRNX DD DSN=A.B.TRXFL.VSAM.KSDS,DISP=SHR\n",
    })
    idx = CobolRepoIndex.build(tmp_path)
    assert idx.datastores["A.B.TRXFL.VSAM.KSDS"].organization == "INDEXED"
