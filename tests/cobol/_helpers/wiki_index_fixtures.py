"""COBOL wiki / bizlogic test helpers: COBOL file docs, a fake LLMClient, synthetic repos."""

from __future__ import annotations

import json
import re
from pathlib import Path

from wiki_fixtures import fx


def cobol_doc(rel: str, pid: str, purpose: str, paras: dict[str, str] | None = None,
              rules: list[str] | None = None) -> str:
    """a minimal RepoAgent-format COBOL file doc."""
    lines = [f"# {rel}", "", f"**Program**: `{pid}` (COBOL; callable subprogram; unit `x`)",
             "", f"{purpose}", "", f"## ClassDef {pid}", f"**{pid}**: {purpose}", "",
             "```cobol", "IDENTIFICATION DIVISION.", f"PROGRAM-ID. {pid}.", "```", "",
             "### Business rules"]
    lines += [f"{i}. {r}" for i, r in enumerate(rules, 1)] if rules else ["None."]
    lines.append("")
    for name, summary in (paras or {}).items():
        lines += [f"## MethodDef {pid}.{name}", f"**{pid}.{name}**: {summary}", "",
                  f"**Signature**: `{name}.`", ""]
    return "\n".join(lines) + "\n"


def write_doc(out: Path, rel: str, text: str) -> Path:
    """OUT/docs/<dir>/markdown_docs/<basename>.md"""
    parent = Path(rel).parent
    path = out / "docs" / parent / "markdown_docs" / f"{Path(rel).name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FakeWikiLLM:
    """a repowiki LLMClient stand-in answering deepdoc and COBOL wiki prompts."""

    model = "fake-model"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def complete(self, messages: list[dict], max_tokens: int = 4096, **_kw) -> str:
        self.calls.append(messages)
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "information architect" in system:
            m = re.search(r"```json\n(.*?)\n```", user, re.S)
            draft = json.loads(m.group(1)) if m else {}
            for s in draft.get("sections", []):
                for p in s.get("pages", []):
                    for k in ("package_count", "sample_packages", "program_count",
                              "sample_programs"):
                        p.pop(k, None)
                    p["title"] = f"Nice {p['id']}"
            draft["project_title"] = "Demo Bank"
            return json.dumps(draft)
        if system.startswith("You fix invalid mermaid"):
            return "# Fixed\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
        m = re.search(r'Write the wiki page titled "(.+?)"', user)
        if m:
            progs = re.findall(r"### program (\S+)", user)
            if progs:          # the COBOL module-page prompt
                a, b = progs[0], progs[-1]       # an edge the index does not know
                diagram = (f'flowchart TD\n  {a.replace("-", "_")}["{a}"] -->|CALL| '
                           f'{b.replace("-", "_")}["{b}"]')
                return (f"# {m.group(1)}\n\nCOBOL page starting at [[prog:{progs[0]}]], see "
                        "[[page:program-index]] and [[copy:ACCTREC]].\n\n"
                        f"```mermaid\n{diagram}\n```\n")
            return f"# {m.group(1)}\n\nDeepdoc page for [[page:architecture]].\n"
        if "Architecture" in user[:80]:
            return "# Architecture\n\nSee [[page:index]] and [[page:program-index]]."
        return "# Demo\n\nStart at [[page:architecture]] then [[page:program-index]]."


def install_fake_llm(monkeypatch) -> FakeWikiLLM:
    """make run_wiki's LLMClient the fake (returned)."""
    llm = FakeWikiLLM()
    monkeypatch.setattr("codocs.repowiki.llm.client.LLMClient", lambda *a, **k: llm)
    return llm


def two_program_file() -> str:
    """one file holding two top-level programs (MAINPGM, SUBPGM2)."""
    return fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. MAINPGM.",
        "PROCEDURE DIVISION.",
        "MAIN-PARA.",
        "    CALL 'SUBPGM2'",
        "    STOP RUN.",
        "END PROGRAM MAINPGM.",
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. SUBPGM2.",
        "DATA DIVISION.",
        "LINKAGE SECTION.",
        "01 LK-X PIC X.",
        "PROCEDURE DIVISION USING LK-X.",
        "SUB-PARA.",
        "    MOVE 'Y' TO LK-X",
        "    GOBACK.",
        "END PROGRAM SUBPGM2.",
    )


def flow_program() -> str:
    """PERFORM THRU, GO TO DEPENDING, a section with paragraphs, CICS START, main-line code."""
    return fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. FLOWPGM.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "01 WS-IX PIC 9.",
        "PROCEDURE DIVISION.",
        "    PERFORM A-PARA THRU C-PARA",
        "    PERFORM WORK-SEC",
        "    GO TO D-PARA E-PARA DEPENDING ON WS-IX",
        "    STOP RUN.",
        "A-PARA.",
        "    MOVE 1 TO WS-IX.",
        "B-PARA.",
        "    MOVE 2 TO WS-IX.",
        "C-PARA.",
        "    EXIT.",
        "D-PARA.",
        "    MOVE 4 TO WS-IX.",
        "E-PARA.",
        "    EXEC CICS START TRANSID('FLW2') END-EXEC",
        "    MOVE 5 TO WS-IX.",
        "WORK-SEC SECTION.",
        "W1-PARA.",
        "    MOVE 6 TO WS-IX.",
        "W2-PARA.",
        "    MOVE 7 TO WS-IX.",
        "9999-ABEND-PROGRAM.",
        "    DISPLAY 'ABEND'.",
    )


def started_program() -> str:
    """the program FLOWPGM's CICS START runs (transaction FLW2, from its RETURN TRANSID)."""
    return fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. FLOW2.",
        "PROCEDURE DIVISION.",
        "MAIN-PARA.",
        "    EXEC CICS RETURN TRANSID('FLW2') END-EXEC",
        "    GOBACK.",
    )


def shared_para_repo(root: Path, n: int = 42) -> Path:
    """n main programs that each PERFORM a paragraph 1000-INIT and CALL 'HUBPGM'."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        pid = f"PGM{i:03d}"
        (root / f"{pid}.cbl").write_text(fx(
            "IDENTIFICATION DIVISION.",
            f"PROGRAM-ID. {pid}.",
            "PROCEDURE DIVISION.",
            "0000-MAIN.",
            "    PERFORM 1000-INIT",
            "    STOP RUN.",
            "1000-INIT.",
            "    CALL 'HUBPGM'.",
        ))
    (root / "HUBPGM.cbl").write_text(fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. HUBPGM.",
        "PROCEDURE DIVISION.",
        "0000-MAIN.",
        "    DISPLAY 'HUB'",
        "    GOBACK.",
    ))
    return root


def falling_program() -> str:
    """main-line code that falls into its first paragraph, which runs on into the next one;
    an ALTER that redirects a paragraph's GO TO."""
    return fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. FALLPGM.",
        "PROCEDURE DIVISION.",
        "    DISPLAY 'START'.",
        "P1-PARA.",
        "    ALTER P3-PARA TO PROCEED TO P4-PARA",
        "    DISPLAY 'P1'.",
        "P1-EXIT.",
        "    EXIT.",
        "P2-PARA.",
        "    DISPLAY 'P2'",
        "    STOP RUN.",
        "P3-PARA.",
        "    GO TO P5-PARA.",
        "P4-PARA.",
        "    DISPLAY 'P4'.",
        "P5-PARA.",
        "    DISPLAY 'P5'.",
    )
