"""tests for codocs.cobol.docs: markdown helpers, COBOL program docs (RepoAgent heading format)
and their loading from OUT/docs, mermaid quality checks."""

from __future__ import annotations

from pathlib import Path

from wiki_fixtures import build, flat_repo

from codocs.cobol.docs import (
    LeafDoc,
    doc_programs,
    drop_broken_mermaid,
    first_sentence,
    load_program_docs,
    mermaid_quality,
    parse_program_doc,
    split_sections,
    tables,
)

# an older doc schema: the generic helpers still read it
LEGACY_DOC = """\
# Program: CBACT01C

**Purpose**: Read the account file and write three extracts.

## Overview
`CBACT01C` is a batch program. It is run by job `READACCT`.

## Interface
| How invoked | Details |
|---|---|
| JCL job step | Job `READACCT`, step `STEP05` |

| Name | Picture/Type | Direction | Meaning |
|---|---|---|---|
| `LS-DATE` | `PIC X(10)` | Input | Date to check \\| raw |

## Processing Logic
The main loop reads records.

```mermaid
flowchart TD
    P1["A"] --> P2["B"]
## not a heading
```

## Business Rules
1. If `ACCT-CURR-CYC-DEBIT` is zero the extract gets 2525.00.
"""

# a file doc in the current format with two programs
DOC = """\
# cbl/CBACT01C.cbl

**Programs**: `CBACT01C`, `CBACT01H` (COBOL; batch program (JCL); unit `cbl`)

`CBACT01C` reads the account file and writes three extracts. It is run by job `READACCT`.

## ClassDef CBACT01C
**CBACT01C**: Read the account file and write three extracts.

```cobol
 IDENTIFICATION DIVISION.
 PROGRAM-ID. CBACT01C.
## not a heading
 PROCEDURE DIVISION.
```

### Invocation
| How invoked | Details |
|---|---|
| JCL job step | Job `READACCT`, step `STEP05` |

### Inputs and outputs
| Resource | Type | Access | Meaning |
|---|---|---|---|
| `ACCTFILE-FILE` / DD `ACCTFILE` | VSAM | read | Account master. |

### Processing logic
The main loop reads records.

### Business rules
1. If `ACCT-CURR-CYC-DEBIT` is zero the extract gets 2525.00.
   This only applies to the fixed extract. (`1300-POPUL-ACCT-RECORD`)
2) End of file is status '10'. (`1000-ACCTFILE-GET-NEXT`)

### Error handling
- abends via `CEE3ABD`

## VariableDef WS-EOF, WS-STATUS
**WS-EOF**: end-of-file switch (WORKING-STORAGE SECTION).

```cobol
 01  WS-EOF  PIC X VALUE 'N'.
```

## MethodDef CBACT01C.1000-ACCTFILE-GET-NEXT
**CBACT01C.1000-ACCTFILE-GET-NEXT**: Reads the next record.

**Signature**: `1000-ACCTFILE-GET-NEXT.`

Reads the next record. Then it dispatches on status.

## MethodDef cbact01c.1350-write-acct-record
**Signature**: `1350-WRITE-ACCT-RECORD.`

Writes `OUT-ACCT-REC` e.g. the extract. More text.

## ClassDef CBACT01H
**CBACT01H**: Helper program.

### Business rules
None.

## VariableDef H-FLAG
**H-FLAG**: a flag (WORKING-STORAGE SECTION).

## MethodDef CBACT01H.0000-MAIN
**CBACT01H.0000-MAIN**: Does the helper work.
"""


def test_split_sections_ignores_fenced_headings():
    sections = split_sections(LEGACY_DOC)
    assert list(sections)[:2] == ["overview", "interface"]
    assert "not a heading" in sections["processing logic"]
    assert "## not a heading" not in sections
    merged = split_sections(DOC)
    assert "classdef cbact01c" in merged and "## not a heading" not in merged


def test_tables_and_first_sentence():
    found = tables(split_sections(LEGACY_DOC)["interface"])
    assert len(found) == 2
    header, rows = found[1]
    assert header == ["Name", "Picture/Type", "Direction", "Meaning"]
    assert rows == [["`LS-DATE`", "`PIC X(10)`", "Input", "Date to check | raw"]]
    assert first_sentence("| a | b |\n\nHello there. Bye.") == "Hello there."
    assert first_sentence("これは文です。次の文。") == "これは文です。"


def test_parse_program_doc_fields():
    doc = parse_program_doc(DOC, "CBACT01C")
    assert doc is not None and doc.program_id == "CBACT01C"
    assert doc.purpose == "Read the account file and write three extracts."
    assert [p.name for p in doc.paragraphs] == ["1000-ACCTFILE-GET-NEXT",
                                                 "1350-WRITE-ACCT-RECORD"]
    assert doc.paragraphs[0].summary == "Reads the next record."
    # no summary line: the first sentence of the prose ("e.g. the" is not a sentence end)
    assert doc.paragraphs[1].summary.startswith("Writes `OUT-ACCT-REC` e.g. the extract.")
    assert len(doc.business_rules) == 2
    assert "only applies to the fixed extract" in doc.business_rules[0]
    assert doc.business_rules[1].endswith("(`1000-ACCTFILE-GET-NEXT`)")


def test_multi_program_slicing():
    first = parse_program_doc(DOC, "CBACT01C")
    second = parse_program_doc(DOC, "cbact01h")  # the program id is matched case-insensitively
    assert doc_programs(DOC) == ["CBACT01C", "CBACT01H"]
    assert first.markdown.startswith("## ClassDef CBACT01C")
    assert "## VariableDef WS-EOF, WS-STATUS" in first.markdown
    assert "H-FLAG" not in first.markdown and "CBACT01H" not in first.markdown
    assert "## not a heading" in first.markdown  # fenced text stays inside its section
    assert second.markdown.startswith("## ClassDef CBACT01H")
    assert "## VariableDef H-FLAG" in second.markdown and "WS-EOF" not in second.markdown
    assert [p.name for p in second.paragraphs] == ["0000-MAIN"]
    assert second.business_rules == []
    assert parse_program_doc(DOC, "NOPE") is None


def test_purpose_variants_and_fallback():
    def purpose(md: str) -> str:
        return parse_program_doc(md, "X").purpose

    assert purpose("## ClassDef X\n**X:** Does X.\n") == "Does X."
    assert purpose("# a.cbl\n\n## ClassDef X\n**X**：日本語。\n") == "日本語。"
    assert purpose("## ClassDef X\nX: Plain label.\n") == "Plain label."
    overview = ("# cbl/X.cbl\n\n**Program**: `X` (COBOL; batch main program; unit `cbl`)\n\n"
                "First one. Second one.\n\n## ClassDef X\n\n```cobol\n PROGRAM-ID. X.\n```\n")
    assert purpose(overview) == "First one."
    assert purpose("## ClassDef X\nIt does things. More.\n") == "It does things."


def test_rules_none_and_bullets():
    def rules(body: str) -> list[str]:
        return parse_program_doc(f"## ClassDef X\n**X**: p.\n\n### Business rules\n{body}",
                                 "X").business_rules

    assert rules("None.\n") == []
    assert rules("n/a\n") == []
    assert rules("- rule a (`P1`)\n- rule b (`P2`)\n") == ["rule a (`P1`)", "rule b (`P2`)"]
    assert rules("* rule a\n* rule b\n") == ["rule a", "rule b"]
    assert rules("1. one\n2) two\n\n### Error handling\n- not a rule\n") == ["one", "two"]


def test_load_program_docs_over_out_docs(tmp_path: Path):
    index = build(flat_repo(tmp_path / "repo"))
    out = tmp_path / "out"
    md_dir = out / "docs" / "markdown_docs"  # flat repo: the root unit
    md_dir.mkdir(parents=True)
    (md_dir / "ACCT01.cbl.md").write_text(
        "# ACCT01.cbl\n\n**Program**: `ACCT01` (COBOL; batch program (JCL); unit `repo`)\n\n"
        "Posts balances.\n\n## ClassDef ACCT01\n**ACCT01**: Post account balances.\n\n"
        "## MethodDef ACCT01.MAIN-PARA\n**ACCT01.MAIN-PARA**: Does everything.\n")
    (md_dir / "DATEUTIL.cbl.md").write_text("# DATEUTIL.cbl\n\n## ClassDef DATEUTIL\n"
                                            "**DATEUTIL**: Converts dates.\n")
    (md_dir / "notes.py.md").write_text("# notes.py\n\n## ClassDef ACCT02\n**ACCT02**: no.\n")
    docs = load_program_docs(out, index)
    assert set(docs) == {"ACCT01", "DATEUTIL"}
    assert docs["ACCT01"].path == "ACCT01.cbl"
    assert docs["ACCT01"].purpose == "Post account balances."
    assert [p.name for p in docs["ACCT01"].paragraphs] == ["MAIN-PARA"]
    assert docs["DATEUTIL"].purpose == "Converts dates."
    assert isinstance(docs["ACCT01"], LeafDoc)


def test_load_without_index_and_missing_dir(tmp_path: Path):
    assert load_program_docs(tmp_path / "nope") == {}
    assert load_program_docs(tmp_path / "nope", index=None) == {}
    d = tmp_path / "docs" / "cbl" / "markdown_docs"
    d.mkdir(parents=True)
    (d / "PGM1.cbl.md").write_text("# cbl/PGM1.cbl\n\n## ClassDef PGM1\n**PGM1**: P.\n")
    (d / "calc.py.md").write_text("# calc.py\n\n## ClassDef Calc\n**Calc**: not COBOL.\n")
    (d / "noext.md").write_text("# cbl/PGM2\n\n**Program**: `PGM2` (COBOL; ...)\n\n"
                                "## ClassDef PGM2\n**PGM2**: Q.\n")
    loaded = load_program_docs(tmp_path)
    assert set(loaded) == {"PGM1", "PGM2"}
    assert loaded["PGM1"].purpose == "P." and loaded["PGM1"].path == "cbl/PGM1.cbl"
    assert loaded["PGM2"].path == "cbl/PGM2"


def test_mermaid_quality_flags_repeated_paragraph_labels():
    bad = (
        "```mermaid\nflowchart TD\n"
        '    P1["MAINLINE SECTION"] --> D1{"WF-REQUEST-ID = \'C\'?"}\n'
        '    D1 -->|"yes"| P2["MAINLINE SECTION"]\n'
        '    D1 -->|"no"| P3["MAINLINE SECTION"]\n```\n'
    )
    assert mermaid_quality(bad) == ["mermaid: paragraph name on several nodes: MAINLINE SECTION"]
    ok = (
        "```mermaid\nflowchart TD\n"
        '    P1["MAINLINE SECTION"] --> D1{"READ: WS-RESP = DFHRESP(NORMAL)?"}\n'
        '    D1 -->|"no"| X1["EXEC CICS RETURN"]\n'
        '    D1 -->|"yes"| D2{"REWRITE: WS-RESP = DFHRESP(NORMAL)?"}\n'
        '    D2 -->|"no"| X2["EXEC CICS RETURN"]\n'
        '    D2 -->|"yes"| X3["GOBACK"]\n    X3 --> X4["GOBACK"]\n```\n'
    )
    assert mermaid_quality(ok) == []


def test_drop_broken_mermaid():
    good = '```mermaid\nflowchart TD\n    P0000["0000-MAIN"] --> P1000["1000-READ"]\n```\n'
    broken = '```mermaid\nflowchart TD\n    A["broken (label"] --> B[x\n```\n'
    warnings: list[str] = []
    out = drop_broken_mermaid("## ClassDef X\n" + good + "\ntext\n" + broken, warnings)
    assert out.count("```mermaid") == 1 and "P0000" in out and "text" in out
    assert len(warnings) == 1 and warnings[0].startswith("mermaid diagram dropped")
    assert drop_broken_mermaid(good) == good
