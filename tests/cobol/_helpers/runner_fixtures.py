"""shared helpers of the COBOL adapter / runner / loop tests: a bound adapter and
runner on a repo copy, canned JSON test plans for cobolmod, a scripted LLM."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from codocs.config import Settings
from codocs.lang.base import RepoContext
from codocs.lang.registry import adapter


def bound(repo: Path, out: Path, settings: Settings | None = None, **kw):
    """(adapter, runner) of COBOL on repo with OUT=out."""
    st = settings or Settings()
    for k, v in kw.items():
        setattr(st, k, v)
    ctx = RepoContext.build(repo, ["cobol"], st, out)
    ad = adapter("cobol", ctx)
    rc = SimpleNamespace(settings=st, repo_root=Path(repo).resolve(), out_dir=Path(out),
                         work_dir=Path(out) / "work" / "tmp", exec_root=None, workers=4,
                         ensure_synced=None)
    return ad, ad.make_runner(rc)


def fx(*lines: str) -> str:
    """fixed-format text: '*' lines become comments, others start in column 8."""
    return "\n".join(("      " + ln) if ln.startswith("*") else ("       " + ln)
                     for ln in lines) + "\n"


DATEUTIL_DRIVER = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. TDATEU.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "COPY DATEPARM.",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    PERFORM TC-C01",
    "    PERFORM TC-C02",
    "    PERFORM TC-C03",
    "    PERFORM TC-C04",
    "    STOP RUN.",
    "TC-C01.",
    "    INITIALIZE LK-DATE-PARMS",
    "    MOVE '20240229' TO LK-DATE",
    "    CALL 'DATEUTIL' USING LK-DATE-PARMS",
    "    IF LK-RC = '00'",
    "        DISPLAY 'TEST C01 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST C01 FAIL rc=' LK-RC",
    "    END-IF",
    "    CANCEL 'DATEUTIL'.",
    "TC-C02.",
    "    INITIALIZE LK-DATE-PARMS",
    "    MOVE '20240001' TO LK-DATE",
    "    CALL 'DATEUTIL' USING LK-DATE-PARMS",
    "    IF LK-RC = '10'",
    "        DISPLAY 'TEST C02 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST C02 FAIL rc=' LK-RC",
    "    END-IF",
    "    CANCEL 'DATEUTIL'.",
    "TC-C03.",
    "    INITIALIZE LK-DATE-PARMS",
    "    MOVE '19000229' TO LK-DATE",
    "    CALL 'DATEUTIL' USING LK-DATE-PARMS",
    "    IF LK-RC = '20'",
    "        DISPLAY 'TEST C03 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST C03 FAIL rc=' LK-RC",
    "    END-IF",
    "    CANCEL 'DATEUTIL'.",
    "TC-C04.",
    "    INITIALIZE LK-DATE-PARMS",
    "    MOVE '20000229' TO LK-DATE",
    "    CALL 'DATEUTIL' USING LK-DATE-PARMS",
    "    IF LK-RC = '00'",
    "        DISPLAY 'TEST C04 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST C04 FAIL rc=' LK-RC",
    "    END-IF",
    "    CANCEL 'DATEUTIL'.",
)


def plan(driver: str = "", *, cases: list[str] | None = None, **extra) -> str:
    """a JSON reply of the test-generation prompt (fenced as the system prompt asks)."""
    data = {"driver_name": "", "driver_source": driver, "stubs_source": "",
            "inputs": [], "expected_files": [], "expected_stdout": [],
            "expected_return_code": None, "stdin": [], "env": {},
            "cases": [{"id": c, "title": f"case {c}"} for c in (cases or [])]}
    data.update(extra)
    return "```json\n" + json.dumps(data, indent=2) + "\n```\n"


DATEUTIL_PLAN = plan(DATEUTIL_DRIVER, cases=["C01", "C02", "C03", "C04"])

DAYSINM_DRIVER = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. TDAYS.",
    "ENVIRONMENT DIVISION.",
    "CONFIGURATION SECTION.",
    "REPOSITORY.",
    "    FUNCTION DAYS-IN-MONTH.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 TD-MON PIC 9(2).",
    "01 TD-LEAP PIC X.",
    "01 TD-DAYS PIC 9(2).",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    PERFORM TC-D01",
    "    PERFORM TC-D02",
    "    PERFORM TC-D03",
    "    STOP RUN.",
    "TC-D01.",
    "    MOVE 2 TO TD-MON",
    "    MOVE 'Y' TO TD-LEAP",
    "    MOVE FUNCTION DAYS-IN-MONTH(TD-MON, TD-LEAP) TO TD-DAYS",
    "    IF TD-DAYS = 29",
    "        DISPLAY 'TEST D01 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST D01 FAIL days=' TD-DAYS",
    "    END-IF.",
    "TC-D02.",
    "    MOVE 4 TO TD-MON",
    "    MOVE 'N' TO TD-LEAP",
    "    MOVE FUNCTION DAYS-IN-MONTH(TD-MON, TD-LEAP) TO TD-DAYS",
    "    IF TD-DAYS = 30",
    "        DISPLAY 'TEST D02 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST D02 FAIL days=' TD-DAYS",
    "    END-IF.",
    "TC-D03.",
    "    MOVE 1 TO TD-MON",
    "    MOVE 'N' TO TD-LEAP",
    "    MOVE FUNCTION DAYS-IN-MONTH(TD-MON, TD-LEAP) TO TD-DAYS",
    "    IF TD-DAYS = 31",
    "        DISPLAY 'TEST D03 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST D03 FAIL days=' TD-DAYS",
    "    END-IF.",
)

DAYSINM_PLAN = plan(DAYSINM_DRIVER, cases=["D01", "D02", "D03"])

ACCTRPT_PLAN = plan(
    "", cases=["B01"],
    inputs=[{"dd": "ACCTIN", "path": "acct.txt", "content": [
        {"ACCT-ID": "1", "ACCT-NAME": "ALICE", "ACCT-OPEN-DATE": "20240229"},
        {"ACCT-ID": "2", "ACCT-NAME": "BOB", "ACCT-OPEN-DATE": "20231301"}]}],
    expected_files=[{"dd": "RPTOUT", "path": "rpt.txt", "content": [
        "000000001 20240229 VALID", "000000002 20231301 INVALID",
        "TOTAL VALID:      1 INVALID:      1"]}],
    expected_stdout=["TOTAL VALID: 00001 INVALID: 00001"],
    expected_return_code=0)

ACCTINQ_SPEC = plan("", cases=["S01", "S02"])


class ScriptedLLM:
    """answers by the system prompt / tag; records every call."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[str, str, str, dict]] = []

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                       **kw):
        rec = dict(kw)
        if max_tokens is not None:
            rec["max_tokens"] = max_tokens
        self.calls.append((tag, system, user, rec))
        reply = self.replies(tag, system, user)
        if isinstance(reply, BaseException):
            raise reply
        return reply


# -- cbi (dataset) programs copied into a tmp repo -------------------------------------------

CBI_FILES = ("copybooks", "functions/ltrim.cbl", "functions/left_func.cbl",
             "functions/right_func.cbl", "logger.cbl")


def cbi_repo(source: Path, dest: Path) -> Path:
    """a small repo of cbi programs (ltrim, left-func, right-func, logger + copybooks)."""
    import shutil

    for rel in CBI_FILES:
        src = Path(source) / rel
        dst = Path(dest) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return Path(dest)


GET_VARIABLE_STUB = fx(
    "IDENTIFICATION DIVISION.",
    'PROGRAM-ID. "get-variable".',
    "DATA DIVISION.",
    "LINKAGE SECTION.",
    "01 LK-VAR PIC X(2048).",
    "01 LK-RC PIC 9.",
    "PROCEDURE DIVISION USING LK-VAR LK-RC.",
    "    MOVE 0 TO LK-RC",
    "    GOBACK.",
)

LTRIM_DRIVER = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. TLTRIM.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 TD-IN PIC X(1024).",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    PERFORM TC-L01",
    "    PERFORM TC-L02",
    "    STOP RUN.",
    "TC-L01.",
    "    MOVE 'LTRIM$(\"   abc\")' TO TD-IN",
    "    CALL 'ltrim' USING TD-IN",
    "    IF TD-IN = '\"abc\"'",
    "        DISPLAY 'TEST L01 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST L01 FAIL got ' TD-IN(1:20)",
    "    END-IF.",
    "TC-L02.",
    "    MOVE 'LTRIM$(\"x  y\")' TO TD-IN",
    "    CALL 'ltrim' USING TD-IN",
    "    IF TD-IN = '\"x  y\"'",
    "        DISPLAY 'TEST L02 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST L02 FAIL got ' TD-IN(1:20)",
    "    END-IF.",
)

LTRIM_PLAN = plan(LTRIM_DRIVER, cases=["L01", "L02"], stubs_source=GET_VARIABLE_STUB)

RIGHT_DRIVER = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. TRIGHT.",
    "ENVIRONMENT DIVISION.",
    "CONFIGURATION SECTION.",
    "REPOSITORY.",
    "    FUNCTION right-func.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 TD-IN PIC X(1024).",
    "01 TD-OUT PIC X(1024).",
    "PROCEDURE DIVISION.",
    "MAIN-PARA.",
    "    PERFORM TC-R01",
    "    STOP RUN.",
    "TC-R01.",
    "    MOVE 'RIGHT$(\"HELLO\",3)' TO TD-IN",
    "    MOVE FUNCTION right-func(TD-IN) TO TD-OUT",
    "    IF TD-OUT = '\"LLO\"'",
    "        DISPLAY 'TEST R01 PASS'",
    "    ELSE",
    "        DISPLAY 'TEST R01 FAIL got ' TD-OUT(1:20)",
    "    END-IF.",
)

RIGHT_PLAN = plan(RIGHT_DRIVER, cases=["R01"], stubs_source=GET_VARIABLE_STUB)
