"""tests for codocs.cobol.source: decoding, format detection, logical lines."""

from __future__ import annotations

from codocs.cobol.source import (
    comment_lines,
    detect_format,
    logical_lines,
    read_text,
    strip_comments_keep_layout,
)


def fx(*lines: str) -> str:
    """fixed-format text: each line is '<indicator><code>' placed at col 7."""
    return "\n".join(f"000100{ln}" for ln in lines) + "\n"


def test_read_text_encodings(tmp_path):
    p = tmp_path / "a.cbl"
    p.write_bytes("       DISPLAY 'café'.\r\n\x1a".encode("utf-8"))
    text, enc = read_text(p)
    assert enc == "utf-8"
    assert "\r" not in text and "\x1a" not in text
    assert "café" in text

    p.write_bytes(b"       DISPLAY 'caf\xe9 \x80'.\n")
    text, enc = read_text(p)
    assert enc == "cp1252"
    assert "€" in text


def test_detect_format_fixed_and_free():
    fixed = fx(
        "*COMMENT LINE",
        " IDENTIFICATION DIVISION.",
        " PROGRAM-ID. HELLO.",
        " PROCEDURE DIVISION.",
        "     DISPLAY 'HI'.",
    )
    assert detect_format(fixed) == "fixed"
    free = (
        "*> header\n"
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. hello.\n"
        "PROCEDURE DIVISION.\n"
        "    DISPLAY \"hi\"\n"
        "    GOBACK.\n"
    )
    assert detect_format(free) == "free"
    assert detect_format(">>SOURCE FORMAT IS FREE\n       IDENTIFICATION DIVISION.\n") == "free"
    assert detect_format("      >>SOURCE FORMAT FIXED\nIDENTIFICATION DIVISION.\n") == "fixed"


def test_fixed_sequence_area_comments_and_cols_73():
    text = fx(
        "*THIS IS A COMMENT",
        "/PAGE EJECT COMMENT",
        "D    DISPLAY 'DEBUG'.",
        "     MOVE A TO B." + " " * 50 + "XXXXXXXX",
    ).replace("000100     MOVE", "SEQ001     MOVE")
    lines = logical_lines(text)
    assert [ll.text for ll in lines] == ["    MOVE A TO B."]
    assert lines[0].line == 4
    comments = comment_lines(text)
    assert [c.text for c in comments] == ["THIS IS A COMMENT", "PAGE EJECT COMMENT"]


def test_fixed_literal_continuation_pads_to_col_72():
    first = "     MOVE 'ABC"  # literal left open, line ends early
    text = fx(first, "-    'DEF' TO X.")
    lines = logical_lines(text)
    assert len(lines) == 1
    joined = lines[0].text
    # the literal keeps the blanks up to column 72 before the continued part
    lit = joined[joined.index("'") + 1:joined.rindex("'")]
    assert lit.startswith("ABC") and lit.endswith("DEF")
    code_len = len(first) - 1  # first char is the indicator column
    assert len(lit) == (72 - 7) - code_len + len("ABC") + len("DEF")


def test_fixed_word_continuation_and_inline_comment():
    text = fx(
        "     MOVE WS-AMOUNT TO",
        "-        WS-TOTAL.  *> trailing note",
        "     PERFORM X. *> another",
    )
    lines = logical_lines(text)
    assert lines[0].text.split() == ["MOVE", "WS-AMOUNT", "TO", "WS-TOTAL."]
    assert lines[1].text.strip() == "PERFORM X."


def test_directives_and_compiler_options_are_dropped():
    text = (
        "       PROCESS SQL\n"
        "       CBL CICS('SP')\n"
        "       IDENTIFICATION DIVISION.\n"
        "       EJECT\n"
        "      >>SOURCE FORMAT FREE\n"
        "PROGRAM-ID. X.\n"
        "*> free comment\n"
        "DISPLAY 'Y'. *> inline\n"
    )
    lines = [ll.text.strip() for ll in logical_lines(text)]
    assert lines == ["IDENTIFICATION DIVISION.", "PROGRAM-ID. X.", "DISPLAY 'Y'."]


def test_inline_comment_marker_inside_literal_is_kept():
    text = "DISPLAY '*> not a comment' *> real comment\n"
    assert logical_lines(text, "free")[0].text == "DISPLAY '*> not a comment'"


def test_strip_comments_keep_layout_preserves_line_count():
    text = fx("*COMMENT", "     DISPLAY 'A'. *> c")
    out = strip_comments_keep_layout(text)
    assert out.count("\n") == text.count("\n")
    assert "COMMENT" not in out and "*> c" not in out
    assert "DISPLAY 'A'." in out
