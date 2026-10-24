"""tests for codocs.cobol.wiki.digest: wiki digests from program docs and from static facts."""

from __future__ import annotations

from codocs.cobol.docs import LeafDoc, parse_program_doc
from codocs.cobol.models import CallSite, CobolProgram, FileUse, Paragraph, SqlUse
from codocs.cobol.wiki.digest import (
    DIGEST_BUDGET,
    digest_from_leafdoc,
    digest_from_static,
    static_purpose,
)

PROGRAM = CobolProgram(
    program_id="CBACT01C",
    path="app/cbl/CBACT01C.cbl",
    header_comment=(
        "PROGRAM     : CBACT01C.CBL\nApplication : CardDemo\n"
        "FUNCTION    : READ THE ACCOUNT FILE AND\n             WRITE INTO FILES.\n"
        "Type        : BATCH"
    ),
    files=[FileUse(name="ACCTFILE-FILE", assign="ACCTFILE", organization="INDEXED",
                   modes=["INPUT"])],
    sql=[SqlUse(op="SELECT", tables=["CUSTOMER"])],
    calls=[CallSite(target="COBDATFT", kind="call"), CallSite(target="CEE3ABD", kind="call")],
    paragraphs=[
        Paragraph(name="0000-MAIN", performs=["1000-READ", "1000-READ-EXIT"]),
        Paragraph(name="1000-READ"),
        Paragraph(name="1000-READ-EXIT"),
    ],
    using_params=["LS-A"],
)

DOC = """\
# app/cbl/CBACT01C.cbl

**Program**: `CBACT01C` (COBOL; batch program (JCL); unit `cbl`)

Reads the account master and writes extract files.

## ClassDef CBACT01C
**CBACT01C**: Read the **account** file and write extracts.

```cobol
IDENTIFICATION DIVISION.
PROGRAM-ID. CBACT01C.
```

### Invocation
| How invoked | Details |
|---|---|
| JCL job step | Job `READACCT`, step `STEP05` |

### Interface
| Name | Picture/Type | Direction | Meaning |
|---|---|---|---|
| `LS-A` | `PIC X` | Input | flag |

### Inputs and outputs
| Resource | Type | Access | Meaning |
|---|---|---|---|
| `ACCTFILE-FILE` | VSAM | read | Account master. |

### Business rules
1. Zero debit becomes 2525.00. (`1300-POPUL`)

## MethodDef CBACT01C.0000-MAIN
**CBACT01C.0000-MAIN**: Opens the files and loops.

**Signature**: `0000-MAIN.`
"""


def test_static_purpose():
    assert static_purpose(PROGRAM) == "READ THE ACCOUNT FILE AND WRITE INTO FILES."
    plain = CobolProgram(program_id="X", path="x", header_comment="Author: me\nCALL TO CEEDAYS")
    assert static_purpose(plain) == "CALL TO CEEDAYS"
    assert static_purpose(CobolProgram(program_id="X", path="x")) == ""


def test_digest_from_static():
    d = digest_from_static(PROGRAM)
    assert d.source == "static" and d.program_id == "CBACT01C"
    by_name = {e.name: e for e in d.entries}
    assert by_name["header"].kind == "static"
    assert by_name["USING"].summary == "LS-A"
    assert by_name["ACCTFILE-FILE"].kind == "file" and "INDEXED" in by_name["ACCTFILE-FILE"].summary
    assert by_name["CUSTOMER"].summary == "DB2 SELECT"
    assert "COBDATFT (call)" in by_name["calls"].summary
    assert by_name["0000-MAIN"].summary.startswith("performs 1000-READ")
    assert "1000-READ-EXIT" not in by_name  # trivial exit paragraphs are skipped


def test_digest_from_leafdoc():
    doc = parse_program_doc(DOC, "CBACT01C")
    d = digest_from_leafdoc(doc, PROGRAM)
    assert d.source == "leafdoc"
    assert d.purpose == "Read the account file and write extracts."
    kinds = [e.kind for e in d.entries]
    assert kinds == ["interface", "interface", "file", "rule", "paragraph"]
    assert d.entries[0].name == "JCL job step" and "READACCT" in d.entries[0].summary
    assert d.entries[2].name == "ACCTFILE-FILE" and d.entries[2].summary.startswith("VSAM | read")
    assert d.entries[3].name == "R1" and "2525.00" in d.entries[3].summary


def test_digest_budget_and_fallback():
    rules = "\n".join(f"{i}. rule {i} " + "x" * 200 + " (`P`)" for i in range(1, 60))
    doc = parse_program_doc(DOC.replace("1. Zero debit becomes 2525.00. (`1300-POPUL`)", rules),
                            "CBACT01C")
    d = digest_from_leafdoc(doc, PROGRAM)
    size = sum(len(e.name) + len(e.summary) + 4 for e in d.entries)
    assert size <= DIGEST_BUDGET
    assert d.entries[0].kind == "interface"  # higher-priority entries survive

    failed = LeafDoc(program_id="CBACT01C", markdown="# stub", error="boom")
    assert digest_from_leafdoc(failed, PROGRAM).source == "static"
