"""COBOL mutation operators: token-only sites, operator forms, the
statement verb line, the 88-level MOVE rule, plumbing paragraphs, fixed-format columns and
0 invalid mutants on a corpus compiled with GnuCOBOL."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from runner_fixtures import fx

from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.mutate import mutation_sites, statement_verb_lines
from codocs.cobol.parser import parse_program_file
from codocs.golang.mutate import apply, sample

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COBOLMOD = FIXTURES / "cobolmod"

CORPUS = fx(
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. MUTCORP.",
    "* IF A > B in a comment is never a site",
    "ENVIRONMENT DIVISION.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 WS-A PIC 9(4) VALUE 1.",
    "01 WS-B PIC 9(4) VALUE 2.",
    "01 WS-Q PIC 9(4).",
    "01 WS-R PIC 9(4).",
    "01 WS-I PIC 9(4).",
    "01 WS-FLAG PIC X VALUE 'N'.",
    "01 APPL-RESULT PIC S9(9) COMP.",
    "   88 APPL-AOK VALUE 0.",
    "   88 APPL-EOF VALUE 16.",
    "01 WS-MSG PIC X(20) VALUE 'IF A > B'.",
    "PROCEDURE DIVISION.",
    "0000-MAIN.",
    "    IF WS-A GREATER THAN OR EQUAL TO WS-B",
    "        MOVE 'Y' TO WS-FLAG",
    "    END-IF",
    "    IF NOT WS-A = 1 OR 2",
    "        DISPLAY 'IF WS-A > 1'",
    "    END-IF",
    "    IF WS-A > 0 AND",
    "       WS-B < 9",
    "        ADD 1 TO WS-A",
    "    ELSE",
    "        SUBTRACT 1 FROM WS-B",
    "    END-IF",
    "    DIVIDE WS-A BY 4 GIVING WS-Q REMAINDER WS-R",
    "    DIVIDE 2 INTO WS-B",
    "    MULTIPLY 2 BY WS-A",
    "    COMPUTE WS-Q = WS-A + WS-B * 2 - 1",
    "    COMPUTE WS-R = - WS-A + 10",
    "    PERFORM 1000-A THROUGH 1000-A-END",
    "    PERFORM 2000-LOOP WITH TEST AFTER UNTIL WS-I > 3",
    "    PERFORM VARYING WS-I FROM 1 BY 1 UNTIL WS-I >= 3",
    "        CONTINUE",
    "    END-PERFORM",
    "    PERFORM 2000-LOOP",
    "    EVALUATE TRUE",
    "        WHEN WS-A < WS-B",
    "            MOVE 8 TO APPL-RESULT",
    "        WHEN OTHER",
    "            MOVE 12 TO APPL-RESULT",
    "    END-EVALUATE",
    "    MOVE 0 TO APPL-RESULT",
    "    EXEC SQL SELECT A INTO :B FROM T WHERE C > 1 END-EXEC",
    "    PERFORM 9999-ABEND-PROGRAM",
    "    STOP RUN.",
    "1000-A.",
    "    MOVE ZERO TO WS-Q.",
    "1000-A-END.",
    "    EXIT.",
    "2000-LOOP.",
    "    ADD 1 TO WS-I.",
    "9999-ABEND-PROGRAM.",
    "    IF WS-A = 99",
    "        DISPLAY 'ABEND'",
    "    END-IF.",
)


def _sites(text: str, **kw):
    prog = parse_program_file("x.cbl", text, None)[0]
    return mutation_sites(text, program=prog, **kw)


def _line(text: str, m) -> int:
    return text[:m.offset].count("\n") + 1


def test_operators_and_exclusions():
    ms = _sites(CORPUS, skip_plumbing=False)
    by = {}
    for m in ms:
        by.setdefault(m.operator, []).append(m)
    reps = {(m.operator, m.original.split()[0] if m.original.split() else "",
             m.replacement.split()[0]) for m in ms}
    # multi-word relational operators are one site; the OR inside is never a logic site
    assert ("cmp", "GREATER", "GREATER") in reps
    ge = next(m for m in by["cmp"] if m.original.startswith("GREATER"))
    assert ge.replacement.startswith("GREATER THAN") and "OR EQUAL" not in ge.replacement
    logic_lines = {_line(CORPUS, m) for m in by["logic"]}
    assert _line(CORPUS, ge) not in logic_lines
    # IF NOT X = 1 OR 2: negation wraps the whole condition in parentheses
    neg = [m for m in by["negate"] if m.original.startswith("NOT WS-A = 1 OR 2")]
    assert neg and neg[0].replacement.startswith("NOT (NOT WS-A = 1 OR 2)")
    # arithmetic verbs (REMAINDER skipped), compute operators (unary minus skipped)
    arith = {m.replacement.split()[0] for m in by["arith"]}
    assert arith == {"SUBTRACT", "ADD", "MULTIPLY", "DIVIDE"}
    assert not any("REMAINDER" in m.original.split("\n")[0] for m in by["arith"])
    assert len(by["compute"]) == 4        # + * - and the binary + after a unary minus
    # MOVE literal rules: 'Y' <-> 'N', ZERO -> 1, 88-levels (8 -> 0, 0 -> 16, 12 -> 0)
    moves = sorted((m.original.split()[0], m.replacement.split()[0]) for m in by["move"])
    assert moves == [("'Y'", "'N'"), ("0", "16"), ("12", "0"), ("8", "0"), ("ZERO", "1")]
    # PERFORM -> CONTINUE only for out-of-line PERFORMs without THRU / UNTIL / VARYING
    perf = [_line(CORPUS, m) for m in by["perform"]]
    assert len(perf) == 2           # PERFORM 2000-LOOP and PERFORM 9999-ABEND-PROGRAM
    lines = CORPUS.split("\n")
    for m in ms:
        first = lines[_line(CORPUS, m) - 1]
        assert not first[6:7] == "*"                       # never in comments
        assert "EXEC SQL" not in first                     # never in EXEC blocks
        assert "VALUE 'IF A > B'" not in first             # never in the DATA DIVISION
        assert "END-IF" not in m.original.split("\n")[0][:6]
    assert not any("'IF WS-A > 1'" in m.original[:12] for m in ms)   # never inside literals
    # sites are unique and in source order
    assert [m.offset for m in ms] == sorted(m.offset for m in ms)
    assert len({m.id for m in ms}) == len(ms) and all(m.id == f"m{m.offset}" for m in ms)


def test_statement_line_universe_and_plumbing():
    ms = _sites(CORPUS, skip_plumbing=False)
    # a site on the continuation line of a multi-line condition carries the IF line
    lt = next(m for m in ms if m.operator == "cmp" and m.original.startswith("<"))
    if_line = next(i for i, ln in enumerate(CORPUS.split("\n"), 1) if "IF WS-A > 0 AND" in ln)
    assert _line(CORPUS, lt) == if_line + 1 and lt.line == if_line
    universe = statement_verb_lines(CORPUS)
    assert if_line in universe and if_line + 1 not in universe
    only = _sites(CORPUS, universe={if_line}, skip_plumbing=False)
    assert only and all(m.line == if_line for m in only)
    abend = next(i for i, ln in enumerate(CORPUS.split("\n"), 1)
                 if ln.strip() == "9999-ABEND-PROGRAM.")
    plumbing = [m for m in ms if m.line > abend]
    kept = _sites(CORPUS)
    assert plumbing and not any(m.id == p.id for m in kept for p in plumbing)
    assert sample(kept, 3, 1) == sample(kept, 3, 1) and len(sample(kept, 3, 1)) == 3


def test_fixed_columns_are_preserved():
    text = (COBOLMOD / "cbl" / "ACCTRPT.cbl").read_text()
    ms = _sites(text, skip_plumbing=False)
    assert ms
    lines = text.split("\n")
    for m in ms:
        mutated = apply(text, m).split("\n")
        n = _line(text, m)
        assert mutated[n - 1][:6] == lines[n - 1][:6]
        assert mutated[n - 1][72:] == lines[n - 1][72:]
        assert len(mutated) == len(lines)
    # a mutation that does not fit in column 72 moves code to an added line
    long = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. LONG1.", "DATA DIVISION.",
              "WORKING-STORAGE SECTION.", "01 A-LONG-DATA-NAME-NUMBER-ONE PIC 9.",
              "01 A-LONG-DATA-NAME-NUMBER-TWO PIC 9.", "PROCEDURE DIVISION.",
              "    IF A-LONG-DATA-NAME-NUMBER-ONE = A-LONG-DATA-NAME-NUMBER-TWO",
              "        DISPLAY 'EQ'", "    END-IF", "    GOBACK.")
    long = "\n".join(ln.ljust(72) + "00001234" if "IF A-LONG" in ln else ln
                     for ln in long.split("\n"))
    assert max(len(ln) for ln in long.split("\n")) == 80
    neg = next(m for m in _sites(long) if m.operator == "negate")
    out = apply(long, neg).split("\n")
    orig = long.split("\n")
    n = _line(long, neg)
    assert out[n - 1].endswith("00001234") and out[n - 1][:6] == orig[n - 1][:6]
    assert len(out) == len(orig) + 1
    assert out[n][:6] == "      " and len(out[n]) <= 72
    assert all(len(ln) <= 80 for ln in out)


def _compile(text: str, incs: list[str], tmp: Path) -> tuple[bool, str]:
    p = tmp / "m.cbl"
    p.write_text(text)
    args = ["cobc", "-fsyntax-only", "-std=ibm", "-frelax-syntax-checks"]
    for d in incs:
        args += ["-I", d]
    r = subprocess.run([*args, str(p)], capture_output=True, text=True)
    return r.returncode == 0, r.stderr


@pytest.mark.needs_cobc
def test_no_invalid_mutants_on_the_corpus(tmp_path: Path):
    corpus = CORPUS.replace("EXEC SQL SELECT A INTO :B FROM T WHERE C > 1 END-EXEC",
                            "CONTINUE")
    ok, err = _compile(corpus, [], tmp_path)
    assert ok, err
    index = CobolRepoIndex.build(COBOLMOD)
    incs = [str(d) for d in index.copybook_dirs()]
    files = [("corpus.cbl", corpus, parse_program_file("x", corpus, None)[0])]
    for rel in ("cbl/DATEUTIL.cbl", "cbl/DAYSINM.cbl", "cbl/ACCTRPT.cbl"):
        pid = index._file_programs[rel][0]
        files.append((rel, (COBOLMOD / rel).read_text(), index.programs[pid]))
    bad = []
    total = 0
    for rel, text, prog in files:
        for m in mutation_sites(text, program=prog, skip_plumbing=False):
            total += 1
            ok, err = _compile(apply(text, m), incs, tmp_path)
            if not ok:
                bad.append((rel, m, err[:200]))
    assert total > 50 and bad == []
