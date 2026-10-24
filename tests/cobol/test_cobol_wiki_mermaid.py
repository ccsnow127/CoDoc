"""mermaid sanitizer / checker rules for syntax the real mermaid 11 parser rejects.

Each case is taken from a block that failed `scripts/cobol/check_mermaid.py` in the docs
generated for CardDemo, GenApp or CBI (leaf docs and wiki pages)."""

from __future__ import annotations

import pytest

from codocs.cobol.wiki import mermaid

CASES = {
    # carddemo CBEXPORT: quoted edge label padded with spaces inside the pipes
    "padded_pipe": (
        'flowchart TD\n    P1100 -->| "open error" | P9999["9999-ABEND-PROGRAM"]',
        'P1100 -->|"open error"| P9999["9999-ABEND-PROGRAM"]',
    ),
    # carddemo COTRTUPC: labelled link without an arrow head
    "headless_link": (
        'flowchart TD\n    D1 --|"yes"| PEXIT["XCTL to caller/COADM01C"]',
        'D1 -->|"yes"| PEXIT["XCTL to caller/COADM01C"]',
    ),
    # cbi GOSUB-GOTO-CMD: backslash-escaped quotes inside a quoted label
    "escaped_quote": (
        'flowchart TD\n    L2 --> D2{"command starts with \\"GOTO \\" or \\"GOSUB \\"?"}',
        "L2 --> D2{\"command starts with 'GOTO ' or 'GOSUB '?\"}",
    ),
    # cbi CLEAR-SCREEN / genapp LGICVS01: label opening with a backtick (markdown string)
    "backtick": (
        'flowchart TD\n    P1 --> D1{"`L-SCREEN-MODE = 0`?"}',
        'P1 --> D1{"L-SCREEN-MODE = 0?"}',
    ),
    "thick_headless": ("flowchart TD\n    A ==|yes| B", "A ==>|yes| B"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_check_flags_and_sanitize_fixes(name):
    code, fixed_line = CASES[name]
    assert mermaid.check(code), name
    fixed = mermaid.sanitize(code)
    assert fixed_line in fixed
    assert mermaid.check(fixed) == []


def test_valid_links_are_left_alone():
    code = (
        "flowchart TD\n  A ---|ok| B\n  A -.-|maybe| C\n  A -->|\"x y\"| D\n"
        "  A -- text --> E\n  A <-->|both| F\n  A --> G"
    )
    assert mermaid.check(code) == []
    assert mermaid.sanitize(code) == code


def test_sequence_backticks_untouched():
    code = "sequenceDiagram\n  A->>B: call `X` now"
    assert mermaid.check(code) == []
    assert "`X`" in mermaid.sanitize(code)


def test_sanitize_page_rewrites_every_block():
    page = (
        "# P\n\n```mermaid\nflowchart TD\n  A --|y| B\n```\n\ntext\n\n"
        "```mermaid\nflowchart TD\n  C -->| \"n\" | D\n```\n"
    )
    out, problems = mermaid.sanitize_page(page)
    assert problems == []
    assert "A -->|y| B" in out and 'C -->|"n"| D' in out and "text" in out


def test_mermaid_stray_end_is_dropped():
    # seen in a real CardDemo overview: a trailing 'end' after the last subgraph
    code = "flowchart TD\n  subgraph a[\"A\"]\n    X --> Y\n  end\n  Y --> Z\nend"
    assert mermaid.check(code) == ["'end' without an open subgraph"]
    fixed = mermaid.sanitize(code)
    assert mermaid.check(fixed) == [] and fixed.count("end") == 1
    assert mermaid.check("flowchart TD\n  subgraph a\n  X --> Y") == [
        "1 subgraph(s) never closed with 'end'"]
    # sequence diagrams keep their 'end' keywords
    seq = "sequenceDiagram\n  loop poll\n    A->>B: x\n  end\nend"
    assert mermaid.sanitize(seq).count("end") == 2
