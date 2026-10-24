"""shared synthetic COBOL repo + fake LLM for the wiki tests (no network)."""

from __future__ import annotations

import re
from pathlib import Path

from codocs.cobol.index import CobolRepoIndex


def fx(*lines: str) -> str:
    return "\n".join(("      " + ln) if ln.startswith("*") else ("       " + ln)
                     for ln in lines) + "\n"


def batch(pid: str, purpose: str, calls: tuple[str, ...] = (), dd: str = "") -> str:
    lines = [f"* FUNCTION : {purpose}", "IDENTIFICATION DIVISION.", f"PROGRAM-ID. {pid}."]
    if dd:
        lines += ["ENVIRONMENT DIVISION.", "INPUT-OUTPUT SECTION.", "FILE-CONTROL.",
                  f"    SELECT IN-FILE ASSIGN TO {dd}."]
    lines += ["DATA DIVISION."]
    if dd:
        lines += ["FILE SECTION.", "FD IN-FILE.", "01 IN-REC PIC X(80)."]
    lines += ["WORKING-STORAGE SECTION.", "COPY ACCTREC.", "PROCEDURE DIVISION.", "MAIN-PARA."]
    if dd:
        lines += ["    OPEN INPUT IN-FILE"]
    lines += [f"    CALL '{c}'" for c in calls]
    lines += ["    STOP RUN."]
    return fx(*lines)


def online(pid: str, purpose: str, xctl: tuple[str, ...] = (), transid: str = "") -> str:
    lines = [f"* FUNCTION : {purpose}", "IDENTIFICATION DIVISION.", f"PROGRAM-ID. {pid}.",
             "DATA DIVISION.", "WORKING-STORAGE SECTION.", "COPY COMMAREA.",
             "PROCEDURE DIVISION.", "MAIN-PARA.",
             "    EXEC CICS READ FILE('ACCTDAT') INTO(X) RIDFLD(K) END-EXEC"]
    lines += [f"    EXEC CICS XCTL PROGRAM('{x}') END-EXEC" for x in xctl]
    if transid:
        lines += [f"    EXEC CICS RETURN TRANSID('{transid}') END-EXEC"]
    lines += ["    GOBACK."]
    return fx(*lines)


SUBPGM = fx(
    "* FUNCTION : convert dates",
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. DATEUTIL.",
    "DATA DIVISION.",
    "LINKAGE SECTION.",
    "01 LK-DATE PIC X(10).",
    "PROCEDURE DIVISION USING LK-DATE.",
    "    GOBACK.",
)

JOB = """//ACCTJOB  JOB (ACCT),'NIGHTLY'
//* NIGHTLY ACCOUNT POSTING
//STEP01   EXEC PGM=ACCT01
//ACCTFILE DD DSN=PROD.ACCT.MASTER,DISP=SHR
//STEP02   EXEC PGM=ACCT02
//ACCTFILE DD DSN=PROD.ACCT.MASTER,DISP=SHR
"""

COPY_ACCT = fx("01 ACCT-RECORD.", "   05 ACCT-ID PIC 9(9).")
COPY_COMM = fx("01 CARD-COMMAREA.", "   05 CA-USER PIC X(8).")


def flat_repo(root: Path) -> Path:
    """flat repo: 2 batch + 1 subprogram + 3 online programs, one JCL job."""
    files = {
        "ACCT01.cbl": batch("ACCT01", "post account balances", ("DATEUTIL",), "ACCTFILE"),
        "ACCT02.cbl": batch("ACCT02", "print account report", ("DATEUTIL",), "ACCTFILE"),
        "DATEUTIL.cbl": SUBPGM,
        "MENU01.cbl": online("MENU01", "main menu", ("VIEW01", "UPDT01"), "MN01"),
        "VIEW01.cbl": online("VIEW01", "view an account", ("MENU01",), "VW01"),
        "UPDT01.cbl": online("UPDT01", "update an account", ("MENU01",), "UP01"),
        "ACCTREC.cpy": COPY_ACCT,
        "COMMAREA.cpy": COPY_COMM,
        "ACCTJOB.jcl": JOB,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def dir_repo(root: Path) -> Path:
    """three sub-application folders with two programs each (directory mode)."""
    files = {
        "billing/BILL01.cbl": batch("BILL01", "create bills", ("BILL02",)),
        "billing/BILL02.cbl": batch("BILL02", "price bills"),
        "cards/CARD01.cbl": online("CARD01", "list cards", ("CARD02",)),
        "cards/CARD02.cbl": online("CARD02", "show a card"),
        "users/USER01.cbl": online("USER01", "list users", ("USER02",)),
        "users/USER02.cbl": online("USER02", "add a user"),
        "lonely/SOLO01.cbl": batch("SOLO01", "one-off utility", ("BILL01",)),
        "cpy/ACCTREC.cpy": COPY_ACCT,
        "cpy/COMMAREA.cpy": COPY_COMM,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def build(root: Path) -> CobolRepoIndex:
    return CobolRepoIndex.build(root)


class FakeLLM:
    """records prompts and answers each prompt kind with canned markdown / JSON."""

    model = "fake-model"

    def __init__(self, refine: dict | None = None, bad_mermaid_once: bool = False):
        self.calls: list[list[dict]] = []
        self.refine = refine
        self.bad_mermaid_once = bad_mermaid_once

    async def complete(self, messages: list[dict], max_tokens: int = 4096) -> str:
        self.calls.append(messages)
        user = messages[-1]["content"]
        system = messages[0]["content"]
        if "information architect" in system:
            import json

            return json.dumps(self.refine or {})
        if system.startswith("You fix invalid mermaid"):
            return "# Fixed\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
        m = re.search(r'Write the wiki page titled "(.+?)"', user)
        if m:
            progs = re.findall(r"### program (\S+)", user)
            if self.bad_mermaid_once:
                self.bad_mermaid_once = False
                return f"# {m.group(1)}\n\n```mermaid\nnot a diagram [\n```\n"
            return (
                f"# {m.group(1)}\n\nThis subsystem is run by [[job:ACCTJOB]] and starts at "
                f"[[prog:{progs[0]}]], reading `[[data:ACCTFILE]]` with [[copy:ACCTREC]].\n\n"
                "## Architecture\n\n```mermaid\nflowchart TD\n  A[\"[[prog:X]]\"] --> B\n```\n"
            )
        if "Architecture" in user[:80]:
            return "# Architecture\n\nSee [[page:index]] and [[page:nope]]."
        return "# Demo App\n\nStart at [[page:architecture]] then [[page:program-index]]."
