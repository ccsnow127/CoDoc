"""tests for mermaid validation and sanitization."""

from codocs.repowiki.deepdoc.mermaid import check, extract_blocks, sanitize, sanitize_page

_VALID = """graph TD
  a["common/logger"] --> b["common/store"]
  subgraph Core
    c[core_mm] -->|calls| d[core_api]
  end"""

_SEQUENCE = """sequenceDiagram
  participant SVC as registrar
  participant REG
  SVC->>REG: PUT /instances (register)
  REG-->>SVC: 201 Created"""


def test_valid_blocks_pass():
    assert check(_VALID) == []
    assert check(_SEQUENCE) == []


def test_unknown_type_flagged():
    assert check("notadiagram\n a --> b")


def test_unbalanced_brackets_flagged():
    problems = check("graph TD\n  a[foo --> b[bar]")
    assert any("unbalanced" in p for p in problems)


def test_empty_and_nested_fence_flagged():
    assert check("") == ["empty diagram"]
    assert any("fence" in p for p in check("graph TD\n```\na-->b"))


def test_sanitize_quotes_special_labels():
    fixed = sanitize("graph TD\n  a[foo (bar)] --> b[plain];")
    assert 'a["foo (bar)"]' in fixed
    assert "b[plain]" in fixed
    assert not fixed.endswith(";")
    assert check(fixed) == []


def test_extract_blocks_and_sanitize_page():
    md = f"# Page\n\n```mermaid\n{_VALID}\n```\n\ntext\n\n```mermaid\ngraph TD\n  x[has (parens)] --> y\n```\n"
    assert len(extract_blocks(md)) == 2
    out, problems = sanitize_page(md)
    assert problems == []
    assert 'x["has (parens)"]' in out


def test_sanitize_page_reports_remaining_problems():
    md = "```mermaid\nbogus diagram\n```\n"
    _out, problems = sanitize_page(md)
    assert problems


def test_sequence_stray_text_line_flagged():
    code = (
        "sequenceDiagram\n"
        "  A->>B: do the thing\n"
        "  Note over A: examples: x,\n"
        "stray continuation line\n"
        "  B-->>A: done"
    )
    problems = check(code)
    assert any("not a sequenceDiagram statement" in p for p in problems)


def test_sequence_keywords_and_arrows_not_flagged():
    assert check(_SEQUENCE) == []
    code = (
        "sequenceDiagram\n"
        "  autonumber\n"
        "  box Group\n"
        "  participant A\n"
        "  end\n"
        "  activate A\n"
        "  loop every second\n"
        "  A-)B: async\n"
        "  A--xB: fail\n"
        "  end\n"
        "  deactivate A"
    )
    assert check(code) == []
