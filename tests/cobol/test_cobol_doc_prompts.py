"""COBOL docgen prompts: the seed prompt (header lines, the
required sections, the FACTS block, the copybook context, the display-form source), the
codegen prompt (copybook text, dialect, allowed COPY / CALL list), finalize_doc and the
rendered system prompts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from runner_fixtures import bound

from codocs.lang.base import FRAGMENT_SLOTS, prompt_spec
from codocs.specforge.docfmt import clean_doc, expected_sections
from codocs.specforge.prompts import CODEGEN, REFINE, SEED, load_prompt

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
async def ctx(tmp_path: Path):
    repo = tmp_path / "cobolmod"
    shutil.copytree(FIXTURES / "cobolmod", repo)
    ad, runner = bound(repo, tmp_path / "out")
    units = await ad.discover(runner)
    return repo, ad, units["cbl"]


def test_fragments_and_system_prompts():
    spec = prompt_spec("cobol")
    assert set(FRAGMENT_SLOTS) <= set(spec.fragments)
    assert (spec.fence, spec.comment, spec.file_noun) == ("cobol", "*>", "COBOL source file")
    assert spec.code_helper_prefix == "CDG-" and spec.test_helper_prefix == "TD-"
    seed = load_prompt(SEED, "cobol")
    assert "ONE COBOL source file" in seed and "## ClassDef <PROGRAM-ID>" in seed
    assert "### Business rules" in seed and "flowchart TD" in seed
    assert "the full text of the copybooks the program COPYs" in seed
    code = load_prompt(CODEGEN, "cobol")
    assert "inside a single ```cobol fenced block" in code
    assert "case included" in code and "CDG-" in code and "never inline" in code
    assert "NEXT SENTENCE is not CONTINUE" in load_prompt(REFINE, "cobol")


async def test_seed_prompt_contents(ctx):
    repo, ad, u = ctx
    src = ad.read("cbl/ACCTRPT.cbl")
    system, user = ad.doc_seed_messages("cbl/ACCTRPT.cbl", u, src,
                                        ad.package_context(repo, u, "cbl/ACCTRPT.cbl"))
    assert system == load_prompt(SEED, "cobol")
    assert user.startswith("Document the COBOL source file `cbl/ACCTRPT.cbl` of directory "
                           "unit `cbl` (batch program (JCL); GnuCOBOL 3.2, ")
    assert "```markdown\n# cbl/ACCTRPT.cbl\n\n**Program**: `ACCTRPT` (COBOL; batch program " \
           "(JCL); unit `cbl`)\n```" in user
    for kind, name in expected_sections(src, "cobol", ad):
        assert f"- `## {kind} {name}`" in user
    assert "- `## MethodDef ACCTRPT.3000-PROCESS`" in user
    assert "## Static analysis facts (authoritative for names and relationships)" in user
    facts_at = user.index("## Static analysis facts")
    assert facts_at < user.index("## Context (declared in OTHER files")
    assert "*> ACCTREC" in user and "ACCT-OPEN-DATE" in user        # copybook context
    shown = user.split("## Source of `cbl/ACCTRPT.cbl`\n```cobol\n", 1)[1]
    assert shown.startswith(" IDENTIFICATION DIVISION.\n PROGRAM-ID. ACCTRPT.\n")
    assert "00000100" not in shown                                  # columns 73-80 dropped
    refine = ad.doc_refine_messages("cbl/ACCTRPT.cbl", u, src, "# doc", "code",
                                    _eval(), [], attempt=2)[1]
    assert "## Static analysis facts" in refine and "Round 2." in refine
    assert "00000100" not in refine.split("## Current documentation")[0]


def _eval():
    from codocs.specforge.loop import DocEval

    return DocEval(signal="facts", build_ok=True, phi=0.5, relevant=["a"], passed=[],
                   failing={"a": "the regenerated program lacks x"})


async def test_codegen_prompt_and_extract(ctx):
    repo, ad, u = ctx
    rel = "cbl/ACCTRPT.cbl"
    src = ad.read(rel)
    allowed = ad.imports(src)
    system, user = ad.doc_codegen_messages(rel, u, "# doc\n", ad.package_context(repo, u, rel),
                                           allowed, taken=ad.package_identifiers(repo, u, rel))
    assert system == load_prompt(CODEGEN, "cobol")
    assert "## Allowed COPY members and calls (use only these; omit unused ones)" in user
    assert "COPY ACCTREC.\nCOPY DATEPARM.\nCALL 'DATEUTIL'" in user
    assert "05  ACCT-OPEN-DATE      PIC X(8)." in user                 # copybook text itself
    assert "-std=ibm -frelax-syntax-checks, fixed reference format" in user
    assert "## Static analysis facts" not in user                     # codegen gets no FACTS
    assert "DAYS-IN-MONTH" in user                                     # names already taken
    assert ad.extract_code("blah\n```cobol\nCODE\n```\nmore") == "CODE\n"
    assert ad.extract_code("CODE") == "CODE\n"


async def test_finalize_doc_and_function_units(ctx):
    repo, ad, u = ctx
    doc = ("Here it is:\n<documentation>\n# cbl/DAYSINM.cbl\n\n**Program**: `DAYS-IN-MONTH` "
           "(COBOL; user-defined function; unit `cbl`)\n\n## ClassDef DAYS-IN-MONTH\nx.\n\n"
           "```mermaid\nflowchart TD\n  A[\"start\"] --> B{\"LK-MON = 2?\"}\n"
           "  B -->|\"yes\"| C[\"1000-FEB\"]\n```\n\n```mermaid\nflowchart TD\n  A[\"x\" --> \n"
           "```\n</documentation>")
    cleaned = ad.finalize_doc(clean_doc(doc))
    assert cleaned.startswith("# cbl/DAYSINM.cbl\n")
    assert cleaned.count("```mermaid") == 1 and "LK-MON = 2?" in cleaned
    head = ad.header_lines("cbl/DAYSINM.cbl", u)
    assert head[1] == "**Program**: `DAYS-IN-MONTH` (COBOL; user-defined function; unit `cbl`)"
    user = ad.doc_seed_messages("cbl/DAYSINM.cbl", u, ad.read("cbl/DAYSINM.cbl"), "")[1]
    assert "(user-defined function; GnuCOBOL 3.2" in user
    assert "- `## ClassDef DAYS-IN-MONTH`" in user
