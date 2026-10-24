"""condense a program into a wiki ProgramDigest, from its doc or from static facts.

Digests are the per-program context of the wiki generator, so each one is budgeted
(~4k chars): purpose first, then interface items, files, business rules and paragraphs.
Docs are the program slices of the RepoAgent-format file docs, whose `### Interface` /
`### Inputs and outputs` subsections take the place of older top-level `##` sections.
package_digest() turns a program digest into the deepdoc PackageDigest of its wiki package.
"""

from __future__ import annotations

import re

from codocs.cobol.docs import LeafDoc, section, tables
from codocs.cobol.models import CobolProgram
from codocs.cobol.wiki.models import DigestEntry, ProgramDigest
from codocs.repowiki.deepdoc.models import PackageDigest

DIGEST_BUDGET = 4_000  # chars of entry names + summaries per program

_PURPOSE_KEY_RE = re.compile(
    r"^\s*(?:function|purpose|description|desc|remarks?|abstract|summary|objet|but)\s*[:.-]\s*(.+)$",
    re.IGNORECASE,
)
_BANNER_RE = re.compile(
    r"^\s*(?:program(?:-id)?|application|type|layer|author|date|written|installation|"
    r"version|ver|copyright|license|date-written|date-compiled|security)\b\s*[:.]",
    re.IGNORECASE,
)
_MD_RE = re.compile(r"[`*_]")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEAD_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", _MD_RE.sub("", text)).strip()


def _budgeted(entries: list[DigestEntry], budget: int) -> list[DigestEntry]:
    """keep entries in order while the running size stays within *budget*."""
    out: list[DigestEntry] = []
    used = 0
    for e in entries:
        size = len(e.name) + len(e.summary) + 4
        if used + size > budget:
            continue  # a later, shorter entry may still fit
        out.append(e)
        used += size
    return out


# -- from a generated leaf doc ----------------------------------------------------


def digest_from_leafdoc(
    doc: LeafDoc, program: CobolProgram, budget: int = DIGEST_BUDGET
) -> ProgramDigest:
    """digest of a leaf doc; failed or empty docs fall back to the static digest."""
    if doc.error or not doc.markdown.strip():
        return digest_from_static(program, budget)
    md = doc.markdown
    # RepoAgent-format docs split the Interface section into Invocation + Interface
    interface = _table_entries(_part(md, "Invocation"), "interface") \
        if not section(md, "Interface") else []
    interface += _table_entries(_part(md, "Interface"), "interface")
    files = _table_entries(_part(md, "Inputs and Outputs"), "file")
    rules = [
        DigestEntry(name=f"R{i}", kind="rule", summary=_plain(r)[:400])
        for i, r in enumerate(doc.business_rules, 1)
    ]
    paragraphs = [
        DigestEntry(name=p.name, kind="paragraph", summary=_plain(p.summary)[:300])
        for p in doc.paragraphs
    ]
    # priority: interface, files, rules, then paragraphs; output keeps that order
    picked = _budgeted([*interface, *files, *rules, *paragraphs], budget)
    if not paragraphs and not rules:
        picked += _budgeted(_static_entries(program), budget - _size(picked))
    return ProgramDigest(
        program_id=program.program_id,
        source="leafdoc",
        purpose=_plain(doc.purpose) or static_purpose(program),
        entries=picked,
    )


def _part(md: str, name: str) -> str:
    """the `## name` section of an older doc, else the `### name` subsection."""
    body = section(md, name)
    if body:
        return body
    want = _canon(name)
    out: list[str] | None = None
    in_fence = False
    for line in md.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and (m := _HEAD_RE.match(line)):
            if out is not None:
                break
            if len(m.group(1)) == 3 and _canon(m.group(2)) == want:
                out = []
            continue
        if out is not None:
            out.append(line)
    return "\n".join(out or []).strip()


def _canon(heading: str) -> str:
    return re.sub(r"[`*_]", "", heading).strip().lower()


def _size(entries: list[DigestEntry]) -> int:
    return sum(len(e.name) + len(e.summary) + 4 for e in entries)


def _table_entries(text: str, kind: str) -> list[DigestEntry]:
    """one entry per table row: first cell is the name, the rest the summary."""
    out: list[DigestEntry] = []
    for _header, rows in tables(text):
        for row in rows:
            cells = [_plain(c) for c in row if c.strip()]
            if not cells:
                continue
            out.append(DigestEntry(name=cells[0][:80], kind=kind,
                                   summary=" | ".join(cells[1:])[:300]))
    return out


# -- from static facts only ---------------------------------------------------------


def static_purpose(program: CobolProgram) -> str:
    """best one-line purpose from the header comment (FUNCTION:/PURPOSE: lines first)."""
    lines = [ln.strip(" *-=") for ln in program.header_comment.splitlines()]
    lines = [ln for ln in lines if ln]
    collected: list[str] = []
    for i, ln in enumerate(lines):
        m = _PURPOSE_KEY_RE.match(ln)
        if m:
            collected.append(m.group(1).strip())
            # a purpose often wraps onto unlabeled lines that follow it
            for nxt in lines[i + 1: i + 3]:
                if ":" in nxt[:20] or _BANNER_RE.match(nxt):
                    break
                collected.append(nxt)
            break
    if not collected:
        collected = [ln for ln in lines if not _BANNER_RE.match(ln)][:1]
    return _plain(" ".join(collected))[:300]


def _static_entries(program: CobolProgram) -> list[DigestEntry]:
    entries: list[DigestEntry] = []
    if program.using_params:
        entries.append(DigestEntry(name="USING", kind="interface",
                                   summary=", ".join(program.using_params)))
    if program.transactions:
        entries.append(DigestEntry(name="transactions", kind="interface",
                                   summary=", ".join(program.transactions)))
    for f in program.files:
        bits = [b for b in (f.via, f.assign, f.organization, " ".join(f.modes)) if b]
        entries.append(DigestEntry(name=f.name, kind="file", summary=", ".join(bits)))
    tables_used: dict[str, set[str]] = {}
    for s in program.sql:
        for t in s.tables:
            tables_used.setdefault(t, set()).add(s.op)
    for t, ops in tables_used.items():
        entries.append(DigestEntry(name=t, kind="file", summary="DB2 " + ", ".join(sorted(ops))))
    if program.maps:
        entries.append(DigestEntry(name="maps", kind="file", summary=", ".join(program.maps)))
    calls: dict[str, str] = {}
    for c in program.calls:
        calls.setdefault(c.target, c.kind)
    if calls:
        entries.append(DigestEntry(
            name="calls", kind="static",
            summary=", ".join(f"{t} ({k})" for t, k in calls.items()),
        ))
    for p in program.paragraphs:
        if p.name.upper().endswith("-EXIT") and not p.performs:
            continue
        perf = f"performs {', '.join(p.performs[:8])}" if p.performs else ""
        entries.append(DigestEntry(name=p.name, kind="paragraph", summary=perf))
    return entries


def digest_from_static(program: CobolProgram, budget: int = DIGEST_BUDGET) -> ProgramDigest:
    """digest from parsed facts only: header comment, interface, files, calls, paragraphs."""
    entries: list[DigestEntry] = []
    header = _plain(program.header_comment)
    if header:
        entries.append(DigestEntry(name="header", kind="static", summary=header[:800]))
    entries += _static_entries(program)
    return ProgramDigest(
        program_id=program.program_id,
        source="static",
        purpose=static_purpose(program),
        entries=_budgeted(entries, budget),
    )


# -- wiki packages ------------------------------------------------------------------


def program_digest(program: CobolProgram, doc: LeafDoc | None = None,
                   budget: int = DIGEST_BUDGET) -> ProgramDigest:
    """the digest of a program: from its doc when there is one, else from static facts."""
    if doc is not None:
        return digest_from_leafdoc(doc, program, budget)
    return digest_from_static(program, budget)


def to_package(digest: ProgramDigest, key: str) -> PackageDigest:
    """the deepdoc digest of wiki package *key*: a `purpose` entry first, then the entries."""
    entries: list[DigestEntry] = []
    if digest.purpose:
        entries.append(DigestEntry(name=digest.program_id, kind="purpose",
                                   summary=digest.purpose))
    entries += [e.model_copy() for e in digest.entries]
    source = "leafdocs" if digest.source == "leafdoc" else "static"
    return PackageDigest(package=key, source=source, entries=entries)


def package_digest(index, pid: str, doc: LeafDoc | None = None, key: str | None = None
                   ) -> PackageDigest:
    """deepdoc PackageDigest of program *pid* of a CobolRepoIndex."""
    program = index.programs[pid]
    return to_package(program_digest(program, doc), key if key is not None else program.path)


def load_tree_docs(root, index) -> dict[str, LeafDoc]:
    """program id -> LeafDoc from in-tree docs: <dir>/markdown_docs/<basename>.md next to
    each program file of the index (the RepoAgent layout of OUT/docs, inside the repo)."""
    from pathlib import Path

    from codocs.cobol.docs import parse_program_doc

    out: dict[str, LeafDoc] = {}
    by_file: dict[str, list[str]] = {}
    for pid, prog in sorted(index.programs.items()):
        by_file.setdefault(prog.path, []).append(pid)
    for rel, pids in sorted(by_file.items()):
        parent = Path(rel).parent
        path = Path(root) / parent / "markdown_docs" / f"{Path(rel).name}.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pid in pids:
            doc = parse_program_doc(text, pid)
            if doc is not None:
                doc.path = rel
                out[pid] = doc
    return out
