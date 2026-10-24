"""tests for codocs.cobol.reflow: strict fixed-format output for LLM-written COBOL."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from codocs.cobol.reflow import (
    is_area_a,
    program_ids,
    reflow_fixed,
    split_programs,
    tokens,
    wrap_free_copies,
)
from codocs.cobol.source import logical_lines


def assert_fixed(text: str) -> None:
    """every line obeys fixed reference format."""
    for ln in text.rstrip("\n").split("\n"):
        if not ln:
            continue
        assert len(ln) <= 72, ln
        assert ln[:6] == "      ", ln
        assert ln[6] in " *-", ln
        if ln[6] == " " and ln[7:].strip():
            code = ln[7:]
            # area B code never starts in cols 8-11 unless it is an area A item
            if not code.startswith(" "):
                assert is_area_a(code.strip()), ln
            else:
                assert code.startswith("    "), ln


def literal_after(text: str, marker: str) -> str:
    """value of the first literal following *marker* after joining continuation lines."""
    joined = "\n".join(ll.text for ll in logical_lines(text, "fixed"))
    i = joined.index(marker) + len(marker)
    m = re.match(r"\s*(['\"])", joined[i:])
    assert m, joined[i:i + 40]
    q = m.group(1)
    j = i + m.end()
    out = ""
    while j < len(joined):
        if joined[j] == q:
            if j + 1 < len(joined) and joined[j + 1] == q:
                out += q + q
                j += 2
                continue
            return out
        out += joined[j]
        j += 1
    raise AssertionError("unterminated literal")


FREE_PROGRAM = """IDENTIFICATION DIVISION.
PROGRAM-ID. TDEMO.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 TD-REC.
   05 TD-A PIC X(10).
   05 TD-B PIC 9(5)V99 COMP-3.
77 TD-N PIC 9(3) VALUE 0.
PROCEDURE DIVISION.
MAIN-PARA.
    MOVE 'ABC' TO TD-A *> trailing comment
    IF TD-A = 'ABC' AND TD-N = 0 AND TD-B = 0 AND TD-A(1:1) = 'A' AND TD-A(2:1) = 'B'
        DISPLAY 'TEST C01 PASS'
    ELSE
        DISPLAY 'TEST C01 FAIL ' TD-A
    END-IF
    PERFORM SUB-PARA
    GOBACK.
SUB-PARA.
    EXIT.
"""


def test_free_input_becomes_fixed_with_areas():
    out = reflow_fixed(FREE_PROGRAM)
    assert_fixed(out)
    lines = out.split("\n")
    assert "       IDENTIFICATION DIVISION." in lines
    assert "       01 TD-REC." in lines
    assert "       77 TD-N PIC 9(3) VALUE 0." in lines
    assert "       MAIN-PARA." in lines
    assert "       SUB-PARA." in lines
    assert "           05 TD-A PIC X(10)." in lines
    # statements stay in area B; EXIT. / GOBACK. are not paragraph headers
    assert any(ln.startswith("           GOBACK.") for ln in lines)
    assert any(ln.startswith("           EXIT.") for ln in lines)
    # inline comment dropped, long condition wrapped onto an area B continuation
    assert "trailing comment" not in out
    assert sum("IF TD-A = 'ABC'" in ln for ln in lines) == 1


def test_fixed_input_keeps_relative_indentation_and_comments():
    src = (
        "       PROCEDURE DIVISION.\n"
        "      * a comment line\n"
        "           IF X = 1\n"
        "               DISPLAY 'ONE'\n"
        "           END-IF.\n"
    )
    out = reflow_fixed(src)
    assert out == src


def test_idempotent():
    once = reflow_fixed(FREE_PROGRAM)
    assert reflow_fixed(once) == once


@pytest.mark.parametrize("n", list(range(50, 140, 7)) + [57, 58, 59, 60, 61, 120, 121, 122])
def test_long_literal_split_preserves_value(n):
    value = "".join("ABCDEFGHIJ"[i % 10] if i % 7 else " " for i in range(n))
    src = f"       DISPLAY 'X' '{value}' 'END'.\n"
    out = reflow_fixed(src)
    assert_fixed(out)
    assert literal_after(out, "'X'") == value


@pytest.mark.parametrize("lead", range(0, 12))
def test_literal_split_never_breaks_doubled_quotes(lead):
    value = "x" * (40 + lead) + "''" * 20 + "tail"
    src = f"       MOVE '{value}' TO TD-A.\n"
    out = reflow_fixed(src)
    assert_fixed(out)
    assert literal_after(out, "MOVE") == value


def test_literal_split_at_area_a_level_value_clause():
    value = "Q" * 150
    out = reflow_fixed(f"01 TD-L PIC X(150) VALUE '{value}'.\n")
    assert_fixed(out)
    assert out.startswith("       01 TD-L PIC X(150) VALUE")
    assert literal_after(out, "VALUE") == value
    assert out.count("\n      -") >= 2


def test_long_comment_is_wrapped():
    out = reflow_fixed("      * " + "word " * 40 + "\n")
    assert_fixed(out)
    assert all(ln[6] == "*" for ln in out.strip("\n").split("\n"))
    assert out.count("word") == 40


def test_llm_continuation_of_open_literal_is_joined():
    src = (
        "           MOVE 'ABCDEFGHIJKLMNOPQRSTUVWXYZ ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghij\n"
        "      -    'klmnop' TO TD-A.\n"
    )
    out = reflow_fixed(src)
    assert_fixed(out)
    assert literal_after(out, "MOVE") == (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnop"
    )


def test_llm_continuation_of_closed_literals_concatenates():
    # seen in practice: '-' lines each holding a complete literal, meaning one value
    src = (
        "           IF LS-RESULT(1:40) =\n"
        "      -       '0000Mesg Code:0000 Date is valid   TstD'\n"
        "      -       'ate:2024'\n"
        "               DISPLAY 'TEST C01 PASS'\n"
        "           END-IF.\n"
    )
    out = reflow_fixed(src)
    assert_fixed(out)
    assert literal_after(out, "=") == "0000Mesg Code:0000 Date is valid   TstDate:2024"


def test_tokens_keep_literals_whole():
    assert tokens("DISPLAY 'A B  C' X(1:2) \"D 'E'\".") == [
        "DISPLAY", "'A B  C'", "X(1:2)", "\"D 'E'\".",
    ]


def test_area_a_detection():
    for code in ("PROCEDURE DIVISION USING A B.", "WORKING-STORAGE SECTION.", "0100-INIT.",
                 "MAIN SECTION.", "FD IN-FILE.", "01 X.", "1 X PIC 9.", "77 N PIC 9.",
                 "PROGRAM-ID. TX.", "FILE-CONTROL.", "END PROGRAM TX.", ">>SOURCE FREE"):
        assert is_area_a(code), code
    for code in ("MOVE 1 TO X", "GOBACK.", "EXIT.", "END-IF.", "CONTINUE.", "05 X PIC 9.",
                 "88 FLAG VALUE 'Y'.", "STOP RUN.", "ELSE"):
        assert not is_area_a(code), code


def test_program_ids_and_split():
    stubs = reflow_fixed(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. CEEDAYS.\nPROCEDURE DIVISION.\n    GOBACK.\n"
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. 'MQOPEN'.\nPROCEDURE DIVISION.\n    GOBACK.\n"
    )
    assert program_ids(stubs) == ["CEEDAYS", "MQOPEN"]
    parts = split_programs(stubs)
    assert len(parts) == 2
    assert program_ids(parts[1]) == ["MQOPEN"]
    assert program_ids("      * PROGRAM-ID. NOTME.\n       PROGRAM-ID. ME.\n") == ["ME"]


def test_wrap_free_copies_around_copy_statements():
    src = reflow_fixed(
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\nCOPY FREEBOOK.\nCOPY FIXBOOK.\n"
        "COPY FREE2 REPLACING Attendee BY\n    ==Rec is global.\n    88 Eof value 'Y'==.\n"
        "01 X PIC 9.\n"
    )
    out = wrap_free_copies(src, lambda m: m in ("FREEBOOK", "FREE2"))
    lines = [ln.strip() for ln in out.split("\n")]
    i = lines.index("COPY FREEBOOK.")
    assert lines[i - 1] == ">>SOURCE FORMAT FREE" and lines[i + 1] == ">>SOURCE FORMAT FIXED"
    j = lines.index("COPY FIXBOOK.")
    assert lines[j - 1] != ">>SOURCE FORMAT FREE"
    # the period inside pseudo-text does not end the COPY statement
    k = next(n for n, ln in enumerate(lines) if ln.startswith("COPY FREE2"))
    end = lines.index(">>SOURCE FORMAT FIXED", k)
    assert "88 Eof value 'Y'==." in lines[end - 1]


@pytest.mark.skipif(shutil.which("cobc") is None, reason="GnuCOBOL not installed")
def test_reflowed_program_compiles_and_keeps_literal_values(tmp_path):
    long_value = "The quick brown fox jumps over the lazy dog, " * 3
    src = (
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. TREFLOW.\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\n"
        f"01 TD-A PIC X(200) VALUE '{long_value}'.\n"
        "PROCEDURE DIVISION.\n"
        f"    IF TD-A = '{long_value}' AND FUNCTION LENGTH(FUNCTION TRIM(TD-A)) = "
        f"{len(long_value.rstrip())}\n"
        "        DISPLAY 'TEST C01 PASS'\n    ELSE\n        DISPLAY 'TEST C01 FAIL'\n"
        "    END-IF\n    STOP RUN.\n"
    )
    path = tmp_path / "TREFLOW.cbl"
    path.write_text(reflow_fixed(src))
    exe = tmp_path / "treflow"
    subprocess.run(["cobc", "-x", "-std=ibm", "-fformat=fixed", "-o", str(exe), str(path)],
                   check=True, capture_output=True)
    run = subprocess.run([str(exe)], capture_output=True, text=True, check=True)
    assert "TEST C01 PASS" in run.stdout
