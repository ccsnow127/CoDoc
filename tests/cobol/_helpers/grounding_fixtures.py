"""a small CICS + batch repo with a LINK chain, a logger hub and distinct file writers.

FRONT01 (txn FR01) -LINK-> BIZ01 -LINK-> DB01 (INSERT CUSTOMER) -LINK-> VS01 (WRITE CUSTFILE)
INQ01 (txn IQ01) -LINK-> DB02 (SELECT CUSTOMER)
POSTJOB runs POST01 (reads TRANIN, writes PROD.MASTER.KSDS)
IMPJOB runs IMPORT1 (reads PROD.EXPORT, writes PROD.MASTER.IMPORT)
every program except LOGGR LINKs / CALLs LOGGR, the hub.
"""

from __future__ import annotations

from pathlib import Path

from wiki_fixtures import fx

from codocs.cobol.index import CobolRepoIndex


def _cics(pid: str, purpose: str, body: list[str], transid: str = "") -> str:
    lines = [f"* FUNCTION : {purpose}", "IDENTIFICATION DIVISION.", f"PROGRAM-ID. {pid}.",
             "DATA DIVISION.", "WORKING-STORAGE SECTION.", "01 WS-REC PIC X(80).",
             "01 WS-KEY PIC X(8).", "LINKAGE SECTION.", "01 DFHCOMMAREA PIC X(100).",
             "PROCEDURE DIVISION.", "MAIN-PARA."]
    lines += [f"    {b}" for b in body]
    lines += ["    EXEC CICS LINK PROGRAM('LOGGR') END-EXEC"]
    if transid:
        lines += [f"    EXEC CICS RETURN TRANSID('{transid}') END-EXEC"]
    lines += ["    EXEC CICS RETURN END-EXEC", "    GOBACK."]
    return fx(*lines)


def _batch(pid: str, purpose: str, infile: str, outfile: str, org: str = "") -> str:
    org_line = [f"    ORGANIZATION IS {org}"] if org else []
    lines = [f"* FUNCTION : {purpose}", "IDENTIFICATION DIVISION.", f"PROGRAM-ID. {pid}.",
             "ENVIRONMENT DIVISION.", "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
             f"    SELECT IN-FILE ASSIGN TO {infile}.",
             f"    SELECT OUT-FILE ASSIGN TO {outfile}", *org_line, "    .",
             "DATA DIVISION.", "FILE SECTION.", "FD IN-FILE.", "01 IN-REC PIC X(80).",
             "FD OUT-FILE.", "01 OUT-REC PIC X(80).", "PROCEDURE DIVISION.", "MAIN-PARA.",
             "    OPEN INPUT IN-FILE", "    OPEN OUTPUT OUT-FILE", "    READ IN-FILE",
             "    WRITE OUT-REC", "    CALL 'LOGGR'", "    CLOSE IN-FILE OUT-FILE",
             "    STOP RUN."]
    return fx(*lines)


LOGGR = fx(
    "* FUNCTION : write a log message to a TS queue",
    "IDENTIFICATION DIVISION.", "PROGRAM-ID. LOGGR.", "DATA DIVISION.",
    "WORKING-STORAGE SECTION.", "01 WS-MSG PIC X(80).", "LINKAGE SECTION.",
    "01 DFHCOMMAREA PIC X(100).", "PROCEDURE DIVISION.", "MAIN-PARA.",
    "    EXEC CICS WRITEQ TS QUEUE('APPLOG') FROM(WS-MSG) END-EXEC",
    "    EXEC CICS RETURN END-EXEC.",
)

POSTJOB = """//POSTJOB  JOB (ACCT),'POST'
//STEP01   EXEC PGM=POST01
//TRANIN   DD DSN=PROD.TRANIN,DISP=SHR
//MASTER   DD DSN=PROD.MASTER.KSDS,DISP=SHR
"""

IMPJOB = """//IMPJOB   JOB (ACCT),'IMPORT'
//STEP01   EXEC PGM=IMPORT1
//EXPIN    DD DSN=PROD.EXPORT,DISP=SHR
//IMPOUT   DD DSN=PROD.MASTER.IMPORT,DISP=(NEW,CATLG)
"""


def chain_repo(root: Path) -> Path:
    files = {
        "FRONT01.cbl": _cics("FRONT01", "customer menu",
                             ["EXEC CICS LINK PROGRAM('BIZ01') COMMAREA(WS-REC) END-EXEC"],
                             transid="FR01"),
        "BIZ01.cbl": _cics("BIZ01", "validate a new customer",
                           ["EXEC CICS LINK PROGRAM('DB01') COMMAREA(WS-REC) END-EXEC"]),
        "DB01.cbl": _cics("DB01", "insert the customer row",
                          ["EXEC SQL INSERT INTO CUSTOMER VALUES (:WS-REC) END-EXEC",
                           "EXEC CICS LINK PROGRAM('VS01') COMMAREA(WS-REC) END-EXEC"]),
        "VS01.cbl": _cics("VS01", "write the customer KSDS record",
                          ["EXEC CICS WRITE FILE('CUSTFILE') FROM(WS-REC)",
                           "     RIDFLD(WS-KEY) END-EXEC"]),
        "INQ01.cbl": _cics("INQ01", "customer inquiry",
                           ["EXEC CICS LINK PROGRAM('DB02') COMMAREA(WS-REC) END-EXEC"],
                           transid="IQ01"),
        "DB02.cbl": _cics("DB02", "read the customer row",
                          ["EXEC SQL SELECT NAME INTO :WS-REC FROM CUSTOMER END-EXEC"]),
        "LOGGR.cbl": LOGGR,
        "POST01.cbl": _batch("POST01", "post transactions to the master", "TRANIN", "MASTER",
                             "INDEXED"),
        "IMPORT1.cbl": _batch("IMPORT1", "split the export file", "EXPIN", "IMPOUT"),
        "POSTJOB.jcl": POSTJOB,
        "IMPJOB.jcl": IMPJOB,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def build_chain(root: Path) -> CobolRepoIndex:
    return CobolRepoIndex.build(chain_repo(root))
