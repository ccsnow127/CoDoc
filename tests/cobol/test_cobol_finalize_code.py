"""finalize_code keeps every line of reference- and
display-form regenerations in its area (continuation operands, comment-entries, `10031-P.`
paragraphs, sequence-only lines, `*` operators), flush-left reflow in context, paragraph
coverage by header line, cmp mutants that do not glue onto identifiers, mutants that never
move a continued literal, and the prose language of COBOL docs and test prompts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from runner_fixtures import bound, fx, plan

from codocs.cobol.factsig import evaluate, facts
from codocs.cobol.harness import parse_trace
from codocs.cobol.mutate import mutation_sites
from codocs.cobol.reflow import reflow_fixed
from codocs.config import Settings
from codocs.golang.gotool import file_coverage
from codocs.lang.cobol import place_fixed


def _ref(*rows: tuple[str, str]) -> str:
    """reference-format text from (sequence area, cols 7-80) rows."""
    return "\n".join(seq.ljust(6) + rest for seq, rest in rows) + "\n"


# a reference-format program with patterns that reflow must preserve: comment-entries on
# their own lines, a sequence-only line, identification-area text, a MOVE whose last operand
# `WS-REISSUE-DATE.` sits alone deep in area B, a CALL ... USING continued the same way, a
# paragraph name starting with five digits, and a `*` operator continuing a COMPUTE
RFIX = _ref(
    ("000100", " IDENTIFICATION DIVISION."),
    ("000200", " PROGRAM-ID. RFIX."),
    ("000300", " AUTHOR."),
    ("000400", "     ACME."),
    ("000500", " DATE-WRITTEN."),
    ("000600", "     Today."),
    ("000700", " DATA DIVISION."),
    ("000800", " WORKING-STORAGE SECTION."),
    ("000900", " 01 WS-A PIC 9(4) VALUE 7."),
    ("001000", " 01 WS-REISSUE-DATE PIC 9(4) VALUE 0."),
    ("001100", " 01 WS-R PIC 9(4) VALUE 0."),
    ("001200", " PROCEDURE DIVISION."),
    ("001250", ""),
    ("001300", " 0000-MAIN.".ljust(66) + "RFIX0130"),
    ("001400", "     PERFORM 10031-INSERT-DB"),
    ("001500", "     MOVE WS-A TO"),
    ("001600", "                                          WS-REISSUE-DATE."),
    ("001700", "     COMPUTE WS-R = WS-REISSUE-DATE + 5"),
    ("001800", "         * 2."),
    ("001900", "     CALL 'RFIXSUB' USING WS-R"),
    ("002000", "                              WS-A."),
    ("002300", "     DISPLAY WS-R"),
    ("002400", "     GOBACK."),
    ("002500", " 10031-INSERT-DB."),
    ("002600", "     ADD 1 TO WS-A."),
)

RFIXSUB = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. RFIXSUB.", "DATA DIVISION.",
             "LINKAGE SECTION.", "01 L-R PIC 9(4).", "01 L-A PIC 9(4).",
             "PROCEDURE DIVISION USING L-R L-A.", "    GOBACK.")


@pytest.fixture
def rfix(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "cbl").mkdir(parents=True)
    (repo / "cbl" / "RFIX.cbl").write_text(RFIX)
    (repo / "cbl" / "RFIXSUB.cbl").write_text(RFIXSUB)
    ad, _runner = bound(repo, tmp_path / "out")
    return ad


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if len(ln) > 7 and ln[6] == " " and ln[7:].strip()]


def _run(tmp: Path, name: str, text: str, *extra: str) -> tuple[int, str]:
    src = tmp / f"{name}.cbl"
    src.write_text(text)
    exe = tmp / name.lower()
    files = [str(src)]
    for i, other in enumerate(extra):
        p = tmp / f"X{i}.cbl"
        p.write_text(other)
        files.append(str(p))
    r = subprocess.run(["cobc", "-x", "-std=ibm", "-frelax-syntax-checks", "-fformat=fixed",
                        "-o", str(exe), *files], capture_output=True, text=True)
    if r.returncode:
        return r.returncode, r.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    return run.returncode, run.stdout.strip()


@pytest.mark.parametrize("form", ["reference", "display"])
def test_identity_regeneration_keeps_every_fact(rfix, form: str):
    src = rfix.read("cbl/RFIX.cbl")
    reply = src if form == "reference" else rfix.prompt_source(src)
    out = rfix.finalize_code("cbl/RFIX.cbl", rfix.extract_code(f"```cobol\n{reply}\n```\n"))
    index = rfix.index()
    want, got = facts("cbl/RFIX.cbl", src, index), facts("cbl/RFIX.cbl", out, index)
    assert got == want and evaluate(want, got)["phi"] == 1.0
    lines = out.split("\n")
    # every line stays where it was written: sequence and identification areas dropped
    assert lines[3] == "           ACME." and lines[5] == "           Today."
    assert lines[12] == ""                                     # sequence-only line
    assert lines[16] == " " * 48 + "WS-REISSUE-DATE."
    assert lines[18] == "               * 2."
    assert lines[20] == " " * 36 + "WS-A."
    assert "       10031-INSERT-DB." in lines and "RFIX0130" not in out
    assert all(len(ln) <= 72 and not ln[:6].strip() for ln in lines)


@pytest.mark.needs_cobc
@pytest.mark.parametrize("form", ["reference", "display"])
def test_identity_regeneration_compiles_and_computes(rfix, tmp_path: Path, form: str):
    src = rfix.read("cbl/RFIX.cbl")
    reply = src if form == "reference" else rfix.prompt_source(src)
    out = rfix.finalize_code("cbl/RFIX.cbl", reply)
    assert _run(tmp_path, "RFIX", out, RFIXSUB) == (0, "0018")


DISPLAY_RFT2 = (" IDENTIFICATION DIVISION.\n PROGRAM-ID. RFT2.\n PROCEDURE DIVISION.\n"
                " 0000-MAIN.\n     PERFORM 10031-INSERT-DB\n     DISPLAY 'DONE'\n     GOBACK.\n"
                " 10031-INSERT-DB.\n     DISPLAY 'IN 10031' WITH NO ADVANCING\n"
                "     DISPLAY ' AND A LONG TAIL' ' THAT RUNS WELL PAST' ' COLUMN SEVENTY-TWO'"
                " ' OK'.\n")


def test_display_form_paragraph_with_digits_is_not_a_continuation():
    out = place_fixed(DISPLAY_RFT2)
    lines = out.split("\n")
    assert "       10031-INSERT-DB." in lines
    assert not any(ln[6:7] == "-" for ln in lines)
    assert all(len(ln) <= 72 for ln in lines)
    # only the over-long line is re-wrapped, in area B
    assert sum(ln.startswith("           DISPLAY ' AND A LONG") for ln in lines) == 1
    # the reflow path (flush-left documents) does not read ` 10031-` as a '-' line
    assert "GOBACK. INSERT-DB." not in reflow_fixed(DISPLAY_RFT2)


@pytest.mark.needs_cobc
def test_display_form_regeneration_compiles(tmp_path: Path):
    assert _run(tmp_path, "RFT2", place_fixed(DISPLAY_RFT2)) == (
        0, "IN 10031 AND A LONG TAIL THAT RUNS WELL PAST COLUMN SEVENTY-TWO OK\nDONE")


FLUSH = """IDENTIFICATION DIVISION.
PROGRAM-ID. RFLUSH.
AUTHOR.
ACME.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 WS-A PIC 9(4) VALUE 3.
01 WS-B PIC 9(4) VALUE 4.
01 WS-X PIC 9(4) VALUE 0.
01 WS-Y PIC 9(4) VALUE 0.
PROCEDURE DIVISION.
MAIN-PARA.
    IF WS-A = 3
    * a comment inside the IF
        MOVE WS-A TO
            WS-Y
    END-IF
    COMPUTE WS-X = WS-A
        * WS-B.
* a column-1 comment
    MOVE WS-X TO
        WS-Y.
    DISPLAY WS-Y
    GOBACK.
"""


def test_flush_left_star_operator_and_continued_operands():
    out = place_fixed(FLUSH)
    lines = out.split("\n")
    assert "               * WS-B." in lines          # code in area B, not a comment
    assert "      * a comment inside the IF" in lines
    assert "      * a column-1 comment" in lines
    assert "           ACME." in lines               # comment-entry stays in area B
    assert "               WS-Y." in lines           # operand, not a paragraph header
    assert "       MAIN-PARA." in lines


@pytest.mark.needs_cobc
def test_flush_left_regeneration_computes(tmp_path: Path):
    assert _run(tmp_path, "RFLUSH", place_fixed(FLUSH)) == (0, "0012")


def test_reflow_keeps_unfinished_sentence_operands_in_area_b():
    # LLM-written drivers go through reflow_fixed too
    src = fx("PROCEDURE DIVISION.", "MAIN-PARA.", "    MOVE WS-A TO",
             "                                 WS-DATE.", "NEXT-PARA.", "    GOBACK.")
    lines = reflow_fixed(src).split("\n")
    assert lines[3].strip() == "WS-DATE." and lines[3].index("W") >= 11
    assert "       NEXT-PARA." in lines


def test_reference_form_sequence_only_lines_become_blank():
    src = _ref(("000100", " IDENTIFICATION DIVISION."), ("000200", " PROGRAM-ID. HELLO."),
               ("000300", ""), ("000400", " PROCEDURE DIVISION."), ("000500", "     GOBACK."))
    out = place_fixed(src)
    assert "000300" not in out and "       PROCEDURE DIVISION." in out.split("\n")
    from codocs.lang.cobol import _for_reflow

    assert "000300" not in _for_reflow(src)


# -- datasets: perfect regenerations score facts phi 1.0 -----------------------------------

DATASET_PROGRAMS = {
    "carddemo": ("app", ["data", "scheduler", "catlg"], [
        "cbl/CBACT01C.cbl", "app-authorization-ims-db2-mq/cbl/DBUNLDGS.CBL",
        "app-authorization-ims-db2-mq/cbl/PAUDBUNL.CBL",
        "app-authorization-ims-db2-mq/cbl/PAUDBLOD.CBL",
        "app-transaction-type-db2/cbl/COBTUPDT.cbl", "app-transaction-type-db2/cbl/COTRTLIC.cbl"]),
    "cbi": ("source", [], ["control_flow/loop/for_loop_handler.cbl"]),
}


@pytest.mark.parametrize("name", sorted(DATASET_PROGRAMS))
def test_dataset_identity_regenerations(name: str, datasets, tmp_path: Path):
    root = datasets(name)
    if root is None:
        pytest.skip(f"dataset {name} not present")
    sub, excludes, rels = DATASET_PROGRAMS[name]
    ad, _runner = bound(root / sub, tmp_path / "out", exclude_paths=excludes)
    index = ad.index()
    for rel in rels:
        src = ad.read(rel)
        want = facts(rel, src, index)
        for reply in (src, ad.prompt_source(src)):
            got = facts(rel, ad.finalize_code(rel, reply), index)
            assert evaluate(want, got)["phi"] == 1.0, (rel, sorted(want ^ got)[:4])


# -- paragraph coverage by header line ---------------------------------------------------

SECPROG = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. SECPROG.", "DATA DIVISION.",
             "LINKAGE SECTION.", "01 LK-N PIC 9(4).", "01 LK-R PIC 9(4).",
             "PROCEDURE DIVISION USING LK-N LK-R.", "MAIN SECTION.", "M-START.",
             "    MOVE 0 TO LK-R", "    IF LK-N > 5", "        PERFORM S1", "    ELSE",
             "        PERFORM S2", "    END-IF", "    GOBACK.",
             "S1 SECTION.", "P1.", "    ADD 10 TO LK-R.", "P2.", "    ADD 1 TO LK-R.",
             "S2 SECTION.", "P1.", "    ADD 20 TO LK-R.", "P2.", "    ADD 2 TO LK-R.")
SECDRV = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. TSEC.", "DATA DIVISION.",
            "WORKING-STORAGE SECTION.", "01 N PIC 9(4).", "01 R PIC 9(4).",
            "PROCEDURE DIVISION.", "MAIN-PARA.", "    PERFORM TC-T01", "    STOP RUN.",
            "TC-T01.", "    MOVE 9 TO N", "    CALL 'SECPROG' USING N R", "    IF R = 11",
            "        DISPLAY 'TEST T01 PASS'", "    ELSE", "        DISPLAY 'TEST T01 FAIL ' R",
            "    END-IF.")


def test_parse_trace_collects_paragraph_header_lines(tmp_path: Path):
    trace = tmp_path / "trace.txt"
    trace.write_text("\n".join([
        "Source: '/m/cbl/SECPROG.cbl'", "Program-Id:  SECPROG",
        "SECPROG|    10|  Section: MAIN", "SECPROG|    11|Paragraph: M-START",
        "SECPROG|    19|  Section: S1", "SECPROG|    20|Paragraph: P1",
        "SECPROG|    22|Paragraph: P2", "Source: '/m/drv/T.cbl'", "TSEC|   9|Paragraph: TC-T01"]))
    heads: dict[str, set[int]] = {}
    _lines, paras = parse_trace(trace, {"/m/cbl/SECPROG.cbl": "cbl/SECPROG.cbl"}.get,
                                {"SECPROG": "cbl/SECPROG.cbl"}, heads)
    assert heads == {"cbl/SECPROG.cbl": {10, 11, 19, 20, 22}}
    assert paras["cbl/SECPROG.cbl"] == {"MAIN", "M-START", "S1", "P1", "P2"}


@pytest.mark.needs_cobc
async def test_paragraph_coverage_does_not_match_by_bare_name(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "cbl").mkdir(parents=True)
    (repo / "cbl" / "SECPROG.cbl").write_text(SECPROG)
    ad, runner = bound(repo, tmp_path / "out", cobol_coverage="paragraph")
    units = await ad.discover(runner)
    u = units["cbl"]
    rel = "cbl/SECPROG.cbl"
    suite = ad.normalize_suite(ad.extract_code(plan(SECDRV, cases=["T01"])), u, rel)
    res = await runner.test("cbl", overlay={ad.suite_rel(u, "SECPROG.cbl"): suite}, cover=True)
    assert res.build_ok and list(res.tests.values()) == ["pass"], res.output
    counts = {b.start_line: b.count for b in res.coverage if b.file == rel}
    # S1.P1 (18) and S1.P2 (20) ran; S2.P1 (23) and S2.P2 (25) did not
    assert counts.get(18) == counts.get(20) == 1 and counts.get(23) == counts.get(25) == 0
    assert file_coverage(res.coverage, rel)[0] < 1.0


# -- mutation operators ---------------------------------------------------------------------

GLUE = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. TGLUE.", "DATA DIVISION.",
          "WORKING-STORAGE SECTION.", "01 WS-FLAG PIC X VALUE 'Y'.", "PROCEDURE DIVISION.",
          "    IF WS-FLAG='Y'", "        DISPLAY 'YES'", "    END-IF", "    GOBACK.")


def _apply(text: str, m) -> str:
    return text[:m.offset] + m.replacement + text[m.offset + len(m.original):]


def test_cmp_mutant_is_not_glued_onto_the_identifier():
    cmp = [m for m in mutation_sites(GLUE, fmt="fixed") if m.operator == "cmp"]
    assert len(cmp) == 1
    line = _apply(GLUE, cmp[0]).split("\n")[6]
    assert "WS-FLAG NOT ='Y'" in line and "WS-FLAGNOT" not in line


@pytest.mark.needs_cobc
def test_glued_cmp_mutant_compiles(tmp_path: Path):
    cmp = next(m for m in mutation_sites(GLUE, fmt="fixed") if m.operator == "cmp")
    assert _run(tmp_path, "TGLUE", _apply(GLUE, cmp)) == (0, "")


def _fit_program(op: str) -> str:
    """WS-S (51 chars) compared with a literal that is open to col 72 on the IF line and
    continued on a '-' line."""
    lit = "ABCDEFGHIJKLMNOPQRSTUVWXYZ ABCDEFGHIJKLMNOPQRSTUVW"
    head = f"    IF WS-S {op} '"
    first = (lit + "Z")[:65 - len(head)]
    rest = (lit + "Z")[len(first):]
    return _ref(
        ("000100", " IDENTIFICATION DIVISION."), ("000200", " PROGRAM-ID. TFIT."),
        ("000300", " DATA DIVISION."), ("000400", " WORKING-STORAGE SECTION."),
        ("000500", " 01  WS-S."), ("000510", "     05 WS-A PIC X(50) VALUE"),
        ("000520", f"        '{lit}'."), ("000530", "     05 WS-B PIC X VALUE 'Z'."),
        ("000600", " PROCEDURE DIVISION."),
        ("001500", " " + head + first), ("001600", f"-        '{rest}'"),
        ("001700", "         DISPLAY 'EQ'"), ("001800", "     ELSE"),
        ("001900", "         DISPLAY 'NE'"), ("002000", "     END-IF"),
        ("002100", "     STOP RUN."))


def test_mutants_never_move_a_continued_literal():
    for op in ("=", "NOT ="):
        text = _fit_program(op)
        lit_line = text.split("\n")[9]
        start = lit_line.index("'")
        for m in mutation_sites(text, fmt="fixed"):
            out = _apply(text, m).split("\n")
            assert len(out) == len(text.split("\n")), (op, m.operator, m.replacement)
            if m.line == 10:
                # the open literal keeps its columns: it still runs to col 72
                assert out[9].index("'", out[9].index("WS-S")) == start, (op, m.replacement)


@pytest.mark.needs_cobc
def test_shrinking_mutant_on_a_continued_literal_flips_the_outcome(tmp_path: Path):
    text = _fit_program("NOT =")
    assert _run(tmp_path, "TFIT", text) == (0, "NE")
    cmp = [m for m in mutation_sites(text, fmt="fixed") if m.operator == "cmp"]
    assert cmp, "the NOT = -> = mutant fits without moving the literal"
    assert _run(tmp_path, "TFIT", _apply(text, cmp[0])) == (0, "EQ")


# -- output language ---------------------------------------------------------------------

async def test_language_reaches_cobol_doc_and_test_prompts(tmp_path: Path):
    from codocs.specforge.prompts_lang import codegen_messages, seed_messages

    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "cobolmod",
                    tmp_path / "mod")
    for lang, needle in (("ja", "日本語"), ("en", None)):
        st = Settings()
        st.language = lang
        ad, runner = bound(tmp_path / "mod", tmp_path / f"out-{lang}", st)
        unit = (await ad.discover(runner))["cbl"]
        rel = "cbl/DATEUTIL.cbl"
        src = ad.read(rel)
        _sys, doc_user = seed_messages(ad, rel, unit, src, "")
        _sys, code_user = codegen_messages(ad, rel, unit, "# doc", "", [])
        _sys, test_user = ad.test_seed_messages(rel, unit, src, "", set(), [])
        _sys, spec_user = ad.spec_messages("cbl/ACCTINQ.cbl", unit, ad.read("cbl/ACCTINQ.cbl"))
        if needle is None:
            assert "Documentation language" not in doc_user
            assert "given/when/then texts in" not in test_user + spec_user
        else:
            assert needle in doc_user and "headings" in doc_user
            assert needle not in code_user                      # codegen stays neutral
            assert "given/when/then texts in Japanese" in test_user
            assert "given/when/then texts in Japanese" in spec_user
