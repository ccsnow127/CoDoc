"""COBOL program docs in the RepoAgent heading format.

A file doc holds, per top-level program, a `## ClassDef <PID>` section (with fixed `###`
subsections such as `### Business rules`), `## VariableDef <name>` sections for its inline
data entries and `## MethodDef <PID>.<PARAGRAPH>` sections for its paragraphs. The wiki
and the judge read one program at a time: parse_program_doc() cuts its slice out of the
file doc into a LeafDoc. The generic markdown helpers (sections, tables, first sentence)
and the mermaid checks of program docs live here too.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

# -- models ---------------------------------------------------------------------------------


class ParagraphDoc(BaseModel):
    name: str
    summary: str = ""


class LeafDoc(BaseModel):
    """the documentation of one COBOL program plus parsed highlights."""

    program_id: str
    path: str = ""  # source path relative to repo root
    markdown: str = ""  # the program's slice of its file doc
    purpose: str = ""  # the one-sentence summary of its ClassDef section
    paragraphs: list[ParagraphDoc] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    model: str = ""
    error: str = ""  # non-empty when generation failed
    sha: str = ""  # unused (docs are recomputed, never checked for staleness)
    warnings: list[str] = Field(default_factory=list)


# -- markdown structure ---------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_H2_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_NUMBERED_RE = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'(\[*])|(?<=[。！？])")
_NONE_RE = re.compile(r"^\s*(none|n/a|なし|無)\W*$", re.IGNORECASE)


def split_sections(markdown: str) -> dict[str, str]:
    """level-2 heading -> body text (headings inside code fences are ignored)."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_fence = False
    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and (m := _H2_RE.match(line)):
            current = sections.setdefault(_canon(m.group(1)), [])
            continue
        if current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def section(markdown: str, name: str) -> str:
    """body of the ## section *name* (case-insensitive), or ""."""
    return split_sections(markdown).get(_canon(name), "")


def _canon(heading: str) -> str:
    """normalize a heading for lookup: no markup or numbering, lower case."""
    h = re.sub(r"[`*_]", "", heading).strip()
    h = re.sub(r"^\d+[.)]?\s+", "", h)
    return h.lower()


def tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """markdown tables in *text* as (header cells, body rows)."""
    out: list[tuple[list[str], list[list[str]]]] = []
    rows: list[list[str]] = []
    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("|"):
            rows.append(_cells(stripped))
            continue
        if rows:
            body = [r for r in rows[1:] if not _is_rule_row(r)]
            out.append((rows[0], body))
            rows = []
    return out


def _cells(row: str) -> list[str]:
    row = row.strip().strip("|")
    # split on pipes not escaped with a backslash
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _is_rule_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c) and any(cells)


def first_sentence(text: str, limit: int = 300) -> str:
    """the first sentence of the first prose paragraph of *text*, markup kept light."""
    para: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", "|", "```")):
            if para:
                break
            continue
        para.append(stripped)
    body = " ".join(para)
    parts = _SENTENCE_END_RE.split(body, maxsplit=1)
    sentence = parts[0].strip() if parts else ""
    if len(sentence) > limit:
        sentence = sentence[: limit - 3].rstrip() + "..."
    return sentence


def _list_items(body: str, item_re: re.Pattern, group: int) -> list[str]:
    items: list[list[str]] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = item_re.match(line)
        if m:
            items.append([m.group(group).strip()])
        elif items and line.strip() and not line.lstrip().startswith(("#", "|")):
            items[-1].append(line.strip())  # wrapped continuation or nested detail
    return [" ".join(parts).strip() for parts in items]


# -- program docs ---------------------------------------------------------------------------

_KIND_RE = re.compile(r"^(ClassDef|VariableDef|MethodDef|FunctionDef)\s+(.+?)\s*$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*#*\s*$")
_HEADER_LINE_RE = re.compile(r"^\s*\*\*\s*Programs?\s*\*\*\s*[:：]", re.IGNORECASE)
_SIGNATURE_RE = re.compile(r"^\s*\*\*\s*Signature\s*\*\*\s*[:：]", re.IGNORECASE)
# COBOL program file extensions, for docs read without an index
PROGRAM_EXTS = (".cbl", ".cob", ".cobol", ".ccp", ".pco", ".sqb", ".cblle", ".sqlcblle",
                ".scbl", ".cicscbl", ".cbx")


def _blocks(markdown: str) -> tuple[list[str], list[tuple[str, str, int, int]]]:
    """(lines, [(kind, name, start, end)]): every `## Kind name` section with its line span
    (heading line included); headings inside code fences are ignored."""
    lines = markdown.split("\n")
    heads: list[tuple[str, str, int]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _H2_RE.match(line)
        if m:
            k = _KIND_RE.match(re.sub(r"[`*]", "", m.group(1)).strip())
            heads.append((k.group(1), k.group(2).strip(), i) if k else ("", m.group(1), i))
    out = []
    for n, (kind, name, start) in enumerate(heads):
        end = heads[n + 1][2] if n + 1 < len(heads) else len(lines)
        out.append((kind, name, start, end))
    return lines, out


def _subsection(body: str, name: str) -> str:
    """body of the `### name` subsection of *body* (case-insensitive), or ""."""
    want = _canon(name)
    out: list[str] | None = None
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and (m := _H3_RE.match(line)):
            if out is not None:
                break
            if _canon(m.group(1)) == want:
                out = []
            continue
        if out is not None:
            out.append(line)
    return "\n".join(out or []).strip()


def _label_re(name: str) -> re.Pattern:
    """`**NAME**: text`, `**NAME**：text`, `**NAME:** text` or `NAME: text`."""
    n = rf"`?{re.escape(name)}`?"
    label = rf"(?:\*\*\s*{n}\s*\*\*\s*[:：]|\*\*\s*{n}\s*[:：]\s*\*\*|{n}\s*[:：])"
    return re.compile(rf"^\s*(?:[-*]\s+)?{label}\s*(.*?)\s*$", re.IGNORECASE)


def _summary(body: str, name: str) -> str:
    """the summary line of a section (label `**name**:`), "" when there is none."""
    rx = _label_re(name)
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        m = rx.match(line)
        if m:
            return m.group(1).strip().strip("*").strip()
        if line.lstrip().startswith("#"):
            break
    return ""


def _prose(body: str) -> str:
    """section prose without the signature line (for first-sentence fallbacks)."""
    return "\n".join(ln for ln in body.split("\n") if not _SIGNATURE_RE.match(ln))


def _overview(lines: list[str], first_head: int) -> str:
    """the file doc's opening paragraph: the text before the first `##` section, without
    the title and the `**Program**:` header line."""
    keep = [ln for ln in lines[:first_head]
            if not ln.lstrip().startswith("#") and not _HEADER_LINE_RE.match(ln)]
    return "\n".join(keep)


def parse_rules(body: str) -> list[str]:
    """numbered (or, failing that, bulleted) items of a business-rules body; `None.` -> []."""
    if not body or _NONE_RE.match(body):
        return []
    rules = _list_items(body, _NUMBERED_RE, group=2)
    if not rules:
        rules = _list_items(body, _BULLET_RE, group=1)
    return [r for r in rules if r and not _NONE_RE.match(r)]


def doc_programs(markdown: str) -> list[str]:
    """the program ids of the ClassDef headings of a file doc, in order."""
    _, blocks = _blocks(markdown)
    return list(dict.fromkeys(name for kind, name, _, _ in blocks if kind == "ClassDef"))


def parse_program_doc(markdown: str, pid: str) -> LeafDoc | None:
    """the LeafDoc of program *pid* from its file doc, or None when the doc has no
    `## ClassDef <pid>` section.

    The slice is the ClassDef section, the VariableDef sections up to the next ClassDef,
    and the `MethodDef <pid>.*` sections, in document order.
    """
    lines, blocks = _blocks(markdown)
    up = pid.upper()
    owner = ""  # the ClassDef the current VariableDefs belong to
    picked: list[tuple[int, int]] = []
    cls_body = ""
    paragraphs: list[ParagraphDoc] = []
    seen: set[str] = set()
    found = False
    for kind, name, start, end in blocks:
        body = "\n".join(lines[start + 1:end])
        if kind == "ClassDef":
            owner = name.upper()
            if owner == up and not found:
                found = True
                cls_body = body
                picked.append((start, end))
        elif kind == "VariableDef":
            if owner == up:
                picked.append((start, end))
        elif kind == "MethodDef":
            prefix, dot, para = name.partition(".")
            if dot and prefix.strip().upper() == up and para.strip():
                picked.append((start, end))
                para_name = para.strip().upper()
                if para_name not in seen:
                    seen.add(para_name)
                    summary = (_summary(body, name) or _summary(body, para.strip())
                               or first_sentence(_prose(body)))
                    paragraphs.append(ParagraphDoc(name=para_name, summary=summary))
    if not found:
        return None
    purpose = _summary(cls_body, pid)
    if not purpose:
        first = blocks[0][2] if blocks else len(lines)
        purpose = first_sentence(_overview(lines, first)) or first_sentence(_prose(cls_body))
    rules = parse_rules(_subsection(cls_body, "Business rules"))
    sliced = "\n\n".join("\n".join(lines[a:b]).strip() for a, b in picked)
    return LeafDoc(program_id=pid, markdown=sliced.strip() + "\n", purpose=purpose,
                   paragraphs=paragraphs, business_rules=rules)


def _doc_file(out_dir: Path, rel: str) -> Path:
    """OUT/docs/<dir>/markdown_docs/<basename>.md (root dir: OUT/docs/markdown_docs/)."""
    base = Path(out_dir) / "docs"
    parent = Path(rel).parent.as_posix().strip("/")
    if parent and parent != ".":
        base = base / parent
    return base / "markdown_docs" / f"{Path(rel).name}.md"


def _title_rel(markdown: str) -> str:
    for line in markdown.split("\n"):
        if line.startswith("# "):
            return line[2:].strip().strip("`")
        if line.strip():
            break
    return ""


def _is_cobol_doc(path: Path, markdown: str) -> bool:
    if path.stem.lower().endswith(PROGRAM_EXTS):
        return True
    first = markdown.split("\n## ", 1)[0]
    return any(_HEADER_LINE_RE.match(ln) for ln in first.split("\n"))


def load_program_docs(out_dir: str | Path, index=None) -> dict[str, LeafDoc]:
    """program id -> LeafDoc from the docs under OUT/docs.

    with a CobolRepoIndex, OUT/docs/<dir>/markdown_docs/<basename>.md is read for every
    program file of the index; without one, every COBOL doc under OUT/docs is sliced by
    its ClassDef headings. A missing OUT/docs gives {}.
    """
    out_dir = Path(out_dir)
    docs_dir = out_dir / "docs"
    if not docs_dir.is_dir():
        return {}
    out: dict[str, LeafDoc] = {}
    if index is not None:
        by_file: dict[str, list[str]] = {}
        for pid, prog in sorted(index.programs.items()):
            by_file.setdefault(prog.path, []).append(pid)
        for rel, pids in sorted(by_file.items()):
            path = _doc_file(out_dir, rel)
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pid in pids:
                doc = parse_program_doc(text, pid)
                if doc is not None:
                    doc.path = rel
                    out[pid] = doc
        return out
    for path in sorted(docs_dir.rglob("*.md")):
        if path.parent.name != "markdown_docs":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _is_cobol_doc(path, text):
            continue
        rel = _title_rel(text) or path.stem
        for pid in doc_programs(text):
            doc = parse_program_doc(text, pid)
            if doc is not None:
                doc.path = rel
                out[pid.upper()] = doc
    return out


# -- mermaid checks of program docs ---------------------------------------------------------

_MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```\s*\n?", re.DOTALL)
# mermaid rectangle nodes: id["label"]
_MERMAID_NODE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\s*\[\s*\"([^\"]*)\"")
# a label that is just a COBOL paragraph / section name
_PARAGRAPH_LABEL_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*(?: SECTION)?$")
_EXIT_LABELS = {"GOBACK", "RETURN", "EXIT", "STOP RUN", "END"}


def mermaid_quality(text: str) -> list[str]:
    """deterministic checks of the flowchart a leaf doc carries (warnings only): a
    paragraph / section name used as the label of several nodes makes a degenerate chart."""
    out: list[str] = []
    for m in _MERMAID_FENCE_RE.finditer(text):
        labels: dict[str, set[str]] = {}
        for node, label in _MERMAID_NODE_RE.findall(m.group(1)):
            label = " ".join(label.split()).upper()
            if _PARAGRAPH_LABEL_RE.match(label) and label not in _EXIT_LABELS:
                labels.setdefault(label, set()).add(node)
        dup = sorted(lab for lab, nodes in labels.items() if len(nodes) > 1)
        if dup:
            out.append("mermaid: paragraph name on several nodes: " + "; ".join(dup[:5]))
    return out


def _mermaid_check(code: str) -> list[str]:
    """the COBOL wiki's strict mermaid check (repowiki's when the COBOL wiki is absent)."""
    try:
        from codocs.cobol.wiki import mermaid
    except ImportError:
        from codocs.repowiki.deepdoc import mermaid
    return mermaid.check(code)


def drop_broken_mermaid(text: str, warnings: list[str] | None = None) -> str:
    """the doc without mermaid blocks that fail the mermaid check (noted in *warnings*)."""
    def _check(m: re.Match) -> str:
        problems = _mermaid_check(m.group(1).strip())
        if not problems:
            return m.group(0)
        if warnings is not None:
            warnings.append("mermaid diagram dropped: " + "; ".join(problems[:3]))
        return ""

    return _MERMAID_FENCE_RE.sub(_check, text)


def _strip_outer_fence(text: str) -> str:
    """LLMs sometimes wrap the whole document in ```markdown fences."""
    if text.startswith("```") and not text.startswith("```mermaid"):
        first_nl = text.find("\n")
        if first_nl != -1 and text.endswith("```"):
            inner = text[first_nl + 1: -3]
            if inner.count("```") % 2 == 0:
                return inner.strip()
    return text
