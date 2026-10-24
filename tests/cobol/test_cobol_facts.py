"""tests for codocs.cobol.facts: source display, context trimming, the FACTS block (hazards,
ENTRY owners, context limits, functions, CICS VSAM facts). No LLM."""

from __future__ import annotations

from pathlib import Path

import pytest

from codocs.cobol import facts as facts_mod
from codocs.cobol.facts import (
    build_facts,
    context_limits,
    fit_context,
    program_copybooks,
    program_span,
    program_text,
    render_source,
    trim_middle,
    trim_source,
)
from codocs.cobol.index import CobolRepoIndex


def fx(*lines: str) -> str:
    """fixed-format text with a sequence number in cols 1-6 and junk in cols 73+."""
    out = []
    for i, ln in enumerate(lines, 1):
        body = ln if ln.startswith("*") else " " + ln
        out.append(f"{i:06d}{body:<66}ZZ{i:06d}")
    return "\n".join(out) + "\n"


GEN_FILES = {
    "cbl/MAINPGM.cbl": fx(
        "*****************************************************",
        "* FUNCTION : Post the daily account extract",
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. MAINPGM.",
        "ENVIRONMENT DIVISION.",
        "INPUT-OUTPUT SECTION.",
        "FILE-CONTROL.",
        "    SELECT ACCT-FILE ASSIGN TO ACCTFILE",
        "        ORGANIZATION IS INDEXED ACCESS MODE IS SEQUENTIAL",
        "        RECORD KEY IS ACCT-ID.",
        "DATA DIVISION.",
        "FILE SECTION.",
        "FD ACCT-FILE.",
        "01 ACCT-REC.",
        "   05 ACCT-ID PIC 9(11).",
        "WORKING-STORAGE SECTION.",
        "COPY ACCTREC.",
        "PROCEDURE DIVISION.",
        "0000-MAIN.",
        "    OPEN INPUT ACCT-FILE",
        "    PERFORM 1000-READ THRU 1000-READ-EXIT",
        "    CALL 'SUBPGM' USING ACCT-REC",
        "    STOP RUN.",
        "1000-READ.",
        "    READ ACCT-FILE.",
        "1000-READ-EXIT.",
        "    EXIT.",
    ),
    "cbl/SUBPGM.cbl": fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. SUBPGM.",
        "DATA DIVISION.",
        "LINKAGE SECTION.",
        "01 LK-REC.",
        "   05 LK-ID PIC 9(11).",
        "PROCEDURE DIVISION USING LK-REC.",
        "    IF LK-ID = ZERO MOVE 1 TO RETURN-CODE END-IF",
        "    GOBACK.",
    ),
    "cpy/ACCTREC.cpy": fx("01 WS-ACCT.", "   05 WS-BAL PIC S9(9)V99 COMP-3."),
    "jcl/RUNMAIN.jcl": (
        "//RUNMAIN JOB CLASS=A\n"
        "//* run the main batch\n"
        "//S1 EXEC PGM=MAINPGM\n"
        "//ACCTFILE DD DSN=PROD.ACCT.DATA,DISP=SHR\n"
        "//SYSOUT DD SYSOUT=*\n"
    ),
}



@pytest.fixture()
def index(tmp_path: Path) -> CobolRepoIndex:
    for rel, text in GEN_FILES.items():
        p = tmp_path / "demo" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(tmp_path / "demo")


# -- context ------------------------------------------------------------------


def test_render_source_strips_sequence_area():
    text = GEN_FILES["cbl/MAINPGM.cbl"]
    out = render_source(text)
    lines = out.splitlines()
    assert lines[0] == "    2 * FUNCTION : Post the daily account extract"  # banner dropped
    assert "    4  PROGRAM-ID. MAINPGM." in lines
    assert "ZZ0" not in out and "000004" not in out
    assert render_source(text, numbered=False).splitlines()[1] == " IDENTIFICATION DIVISION."
    free = "*> ----\n*> hello\nPROGRAM-ID. X.\n"
    assert render_source(free, "free", start=10).splitlines() == [
        "   11 *> hello", "   12 PROGRAM-ID. X.",
    ]


def test_trim_middle_and_fit_context():
    text = "\n".join(f"line {i:04d}" for i in range(1000))
    trimmed = trim_middle(text, 2000)
    assert len(trimmed) <= 2100
    assert "lines omitted] ..." in trimmed
    assert trimmed.startswith("line 0000") and trimmed.endswith("line 0999")
    assert trim_middle("short", 100) == "short"

    # copybooks are trimmed before the source, low-priority (BMS) ones first
    src = "S" * 5_000
    cbs = {"BIG": "B" * 3_000, "MAP": "M" * 3_000, "SMALL": "x" * 100}
    s2, c2 = fit_context(src, cbs, budget=8_300, low_priority={"MAP"})
    assert s2 == src
    assert list(c2) == ["BIG", "MAP", "SMALL"]
    assert c2["BIG"] == cbs["BIG"] and c2["SMALL"] == cbs["SMALL"]
    assert c2["MAP"] == "(omitted to fit the context budget)"

    # source alone over budget: middle of the source is cut
    big = "\n".join("S" * 70 for _ in range(400))
    s3, c3 = fit_context(big, {"CB": "c" * 100}, budget=10_000)
    assert "lines omitted" in s3 and len(s3) < 10_000 and c3["CB"] == "c" * 100


def test_trim_source_cuts_comments_then_data_division():
    lines = ["    1 * header comment"]
    lines += [f"{n:5d}  01 WS-FIELD-{n:04d} PIC X(10)." for n in range(2, 600)]
    lines += [f"{n:5d} * a comment line in the middle" for n in range(600, 700)]
    lines += ["  700  PROCEDURE DIVISION."]
    lines += [f"{n:5d}      MOVE A TO B-{n:04d}" for n in range(701, 900)]
    src = "\n".join(lines)
    out = trim_source(src, 20_000)
    assert len(out) <= 20_000
    assert out.startswith("    1 * header comment")
    assert "a comment line in the middle" not in out
    assert "lines omitted" in out.split("PROCEDURE DIVISION")[0]
    assert "  899      MOVE A TO B-0899" in out and "lines omitted" not in out.split("PROCEDURE")[1]
    tiny = trim_source(src, 6_000)
    assert len(tiny) <= 6_200 and out.count("PROCEDURE DIVISION") == 1
    assert trim_source("short", 100) == "short"


def test_program_span_multi_program_file():
    text = (
        "*> first\n"
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. ALPHA.\nPROCEDURE DIVISION.\n"
        "    DISPLAY 'END PROGRAM BETA'.\nEND PROGRAM ALPHA.\n"
        "\n*> second one\n"
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. Beta IS RECURSIVE.\n"
        "    MOVE 1 TO WS-PROGRAM-ID.\nEND PROGRAM Beta.\n"
    )
    assert program_span(text, "ALPHA") == (0, 6)
    assert program_span(text, "BETA") == (6, 12)
    assert program_span("PROGRAM-ID. ONLY.\n", "ONLY") is None


def test_build_facts(index: CobolRepoIndex):
    facts = build_facts(index, "MAINPGM")
    assert "Program id: MAINPGM" in facts
    assert "JCL job RUNMAIN" in facts and "ACCTFILE=PROD.ACCT.DATA" in facts
    assert "call SUBPGM" in facts and "in repo (cbl/SUBPGM.cbl)" in facts
    assert "SELECT ACCT-FILE ASSIGN TO ACCTFILE" in facts and "INDEXED" in facts
    assert "ACCTREC (cpy/ACCTREC.cpy" in facts
    assert "1000-READ (line" in facts
    sub = build_facts(index, "SUBPGM")
    assert "transferred to by program MAINPGM (call)" in sub
    assert "PROCEDURE DIVISION USING: LK-REC" in sub and "05 LK-ID PIC 9(11)" in sub


# -- grounding facts ------------------------------------------------------------


def gfx(*lines: str) -> str:
    return "\n".join(("      " if ln.startswith("*") else "       ") + ln for ln in lines) + "\n"


GROUND_FILES = {
    "cbl/RPT.cbl": gfx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. RPT.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "01 WS-EOF PIC X VALUE 'N'.",
        "01 WS-DATE PIC X(10).",
        "01 WS-NAME PIC X(10).",
        "PROCEDURE DIVISION.",
        "    PERFORM UNTIL WS-EOF = 'Y'",
        "      PERFORM 1000-READ",
        "      IF WS-DATE >= '2024-01-01'",
        "         CONTINUE",
        "      ELSE",
        "         NEXT SENTENCE",
        "      END-IF",
        "      DISPLAY WS-DATE",
        "    END-PERFORM.",
        "    CALL 'get-variable' USING WS-NAME",
        "    GOBACK.",
        "1000-READ.",
        "    MOVE 'Y' TO WS-EOF.",
        *[f"{2000 + i}-FILLER-PARA.\n           DISPLAY 'PARAGRAPH NUMBER {i:04d} OF MANY'."
          for i in range(60)],
    ),
    "cbl/REPO.cbl": gfx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. VARIABLE-REPOSITORY.",
        "DATA DIVISION.", "LINKAGE SECTION.", "01 L-NAME PIC X(10).",
        "PROCEDURE DIVISION.", "    GOBACK.",
        "    ENTRY 'get-variable' USING L-NAME.", "    GOBACK.",
    ),
    "cbl/LEFTF.cbl": gfx(
        "IDENTIFICATION DIVISION.",
        "FUNCTION-ID. LEFT-FUNC.",
        "DATA DIVISION.",
        "LINKAGE SECTION.",
        "01 L-IN PIC X(10).",
        "01 L-OUT PIC X(10).",
        "PROCEDURE DIVISION USING L-IN RETURNING L-OUT.",
        "    MOVE L-IN TO L-OUT",
        "    GOBACK.",
        "END FUNCTION LEFT-FUNC.",
    ),
    "cbl/LGICVS01.cbl": gfx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. LGICVS01.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "01 WS-REC PIC X(80).", "01 WS-KEY PIC X(10).",
        "PROCEDURE DIVISION.",
        "    EXEC CICS READ FILE('KSDSCUST') INTO(WS-REC)",
        "         RIDFLD(WS-KEY) END-EXEC",
        "    EXEC CICS RETURN END-EXEC.",
    ),
    "cntl/cdef.jcl": (
        "//CDEF JOB CLASS=A\n//S1 EXEC PGM=DFHCSDUP\n//SYSIN DD *\n"
        "  Define File(KSDSCUST) Group(G) DSname(HLQ.GENAPP.KSDSCUST)\n"
        "         Recordsize(82)    Keylength(10)\n/*\n"
    ),
}


@pytest.fixture()
def gindex(tmp_path: Path) -> CobolRepoIndex:
    for rel, text in GROUND_FILES.items():
        p = tmp_path / "ground" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(tmp_path / "ground")


def _limits(index: CobolRepoIndex, pid: str, budget: int) -> tuple[str, list[str]]:
    """the context note and warnings of a prompt for *pid* within *budget* chars."""
    prog = index.programs[pid]
    text, first = program_text(index, pid)
    full = render_source(text, prog.format, start=first)
    cbs = program_copybooks(index, pid)
    source, kept = fit_context(full, cbs, budget)
    return context_limits(prog, full, source, cbs, kept)


def test_flow_hazards_are_facts(gindex):
    facts = build_facts(gindex, "RPT")
    assert "Control-flow hazards" in facts
    assert "line 14: NEXT SENTENCE inside IF (line 11) ... END-IF (line 15) within inline " \
        "PERFORM (lines 9-17)" in facts


def test_entry_targets_name_their_owner(gindex):
    facts = build_facts(gindex, "RPT")
    assert "call GET-VARIABLE (ENTRY of VARIABLE-REPOSITORY; line 18)" in facts
    assert "not a program of its own" in facts
    repo = build_facts(gindex, "VARIABLE-REPOSITORY")
    assert "transferred to by program RPT (call) through ENTRY GET-VARIABLE" in repo


def test_default_budget_holds_large_programs():
    assert facts_mod._CONTEXT_BUDGET >= 600_000


def test_context_limits_listed_in_facts_and_warnings(gindex):
    note, warnings = _limits(gindex, "RPT", 2_000)
    facts = build_facts(gindex, "RPT", note)
    assert "Context limits: the program is larger than the prompt budget" in facts
    assert "Paragraphs whose code is NOT shown" in facts
    limits = facts[facts.index("Context limits"):]
    assert "2030-FILLER-PARA" in limits  # a middle paragraph was cut
    assert "1000-READ" not in limits.split("NOT shown")[1].split("\n")[0]  # head stays
    assert any(w.startswith("context trimmed:") for w in warnings)
    assert any("paragraphs not shown to the model" in w for w in warnings)
    # the default budget leaves the program whole: no note, no warning
    assert _limits(gindex, "RPT", facts_mod._CONTEXT_BUDGET) == ("", [])
    assert "Context limits: the program" not in build_facts(gindex, "RPT")


def test_function_units_are_described_as_functions(gindex):
    assert gindex.programs["LEFT-FUNC"].unit_kind == "function"
    facts = build_facts(gindex, "LEFT-FUNC")
    assert "user-defined FUNCTION (FUNCTION-ID LEFT-FUNC), not a program" in facts
    assert "RETURNING L-OUT" in facts
    assert "main program" not in facts


def test_cics_file_facts_show_vsam_organization(gindex):
    facts = build_facts(gindex, "LGICVS01")
    assert "- CICS file KSDSCUST: READ -> dataset HLQ.GENAPP.KSDSCUST (CSD); VSAM KSDS " \
        "(INDEXED)" in facts


def test_plumbing_paragraph_pattern():
    rx = facts_mod.PLUMBING_PARAGRAPH
    for name in ("9999-ABEND-PROGRAM", "9910-DISPLAY-IO-STATUS", "1000-READ-EXIT", "EXIT",
                 "ABEND-ROUTINE", "Z-LOGGING", "9000-LOG"):
        assert rx.search(name), name
    for name in ("1000-READ", "0000-MAIN", "2000-CATALOG", "EXITS-CHECK"):
        assert not rx.search(name), name
