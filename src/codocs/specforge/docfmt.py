"""specforge doc format: section headers, completeness check, reply cleanup, output path."""

from __future__ import annotations

import re
from pathlib import Path

from codocs.golang.source import top_level_decls

SECTION_RE = re.compile(r"^## (FunctionDef|MethodDef|ClassDef|VariableDef) (.+?)\s*$", re.MULTILINE)

_KINDS = {
    "func": "FunctionDef",
    "method": "MethodDef",
    "type": "ClassDef",
    "var": "VariableDef",
    "const": "VariableDef",
}
_DOC_TAG_RE = re.compile(r"<documentation>(.*?)(?:</documentation>|\Z)", re.DOTALL)
_OPEN_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*(markdown|md)?[ \t]*$", re.MULTILINE | re.I)
_HEAD_RE = re.compile(r"^(?:# \S|## (?:FunctionDef|MethodDef|ClassDef|VariableDef) )", re.MULTILINE)
_PARAMS_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _decls(src: str, lang: str, ad=None):
    if lang == "go":
        return top_level_decls(src)
    if ad is None:
        from codocs.lang.registry import adapter

        ad = adapter(lang)
    return ad.decls(src)


def expected_sections(src: str, lang: str = "go", adapter=None) -> list[tuple[str, str]]:
    """[(kind, name)] for the file's top-level declarations, in source order, without repeats.

    adapter: the run's bound adapter (C++ then sees the repo's namespaces); default unbound.
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for d in _decls(src, lang, adapter):
        key = (_KINDS.get(d.kind, "VariableDef"), d.qualname)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _lang_name(part: str) -> str:
    """non-Go header name: `Cls::m` -> `Cls.m`, a trailing parameter list dropped."""
    name = part.replace("`", "").strip().replace("::", ".")
    stripped = _PARAMS_RE.sub("", name)
    if stripped and not stripped.endswith("operator"):
        name = stripped
    return name.strip()


def _header_names(kind: str, raw: str, lang: str = "go") -> list[str]:
    """names a section header documents: `(*Stats).Add`, `Add()` and `A, B` are tolerated."""
    if lang != "go":
        parts = raw.split(",") if kind == "VariableDef" else [raw]
        return [n for n in (_lang_name(p) for p in parts) if n]
    raw = re.sub(r"\[[^\]]*\]", "", raw.replace("`", "")).strip()
    if raw.startswith("(") and ")" in raw:
        recv, rest = raw[1:].split(")", 1)
        words = recv.replace("*", " ").split()
        raw = (words[-1] if words else "") + rest
    parts = raw.split(",") if kind == "VariableDef" else [raw]
    names = []
    for part in parts:
        name = part.split("(")[0].strip()
        if name:
            names.append(name)
    return names


def documented_sections(doc: str, lang: str = "go") -> set[tuple[str, str]]:
    """(kind, name) pairs present as section headers in a doc."""
    found: set[tuple[str, str]] = set()
    for m in SECTION_RE.finditer(doc):
        for name in _header_names(m.group(1), m.group(2), lang):
            found.add((m.group(1), name))
    return found


def missing_sections(doc: str, src: str, lang: str = "go", adapter=None) -> list[str]:
    """the "Kind Name" of every expected section absent from the doc."""
    found = documented_sections(doc, lang)
    return [f"{k} {n}" for k, n in expected_sections(src, lang, adapter) if (k, n) not in found]


def _unwrap(text: str) -> str | None:
    """the inside of a fence (``` or ~~~, any length, optionally markdown) wrapping text."""
    m = _OPEN_RE.match(text)
    if not m:
        return None
    fence = m.group(1)
    lines = text[m.end():].split("\n")[1:]
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i].strip()
        if len(ln) >= len(fence) and set(ln) == {fence[0]}:
            return "\n".join(lines[:i])
    return "\n".join(lines)


def _from_heading(text: str) -> str:
    head = _HEAD_RE.search(text)
    return text[head.start():] if head and not text.startswith("#") else text


def clean_doc(text: str) -> str:
    """strip a <documentation> wrapper, a preface, and a wrapping markdown fence."""
    text = text.strip()
    m = _DOC_TAG_RE.search(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("#"):
        head = _HEAD_RE.search(text)
        fence = next((f for f in _OPEN_RE.finditer(text) if f.group(2)), None)
        if fence and (head is None or fence.start() < head.start()):
            text = text[fence.start():]
        elif head:
            text = text[head.start():]
    inner = _unwrap(text)
    if inner is not None:
        text = _from_heading(inner.strip())
    text = text.strip()
    return text + "\n" if text else ""


def doc_path(out_dir: Path, pkg: str, stem: str) -> Path:
    """OUT/docs/<pkg>/markdown_docs/<stem>.md (root package: OUT/docs/markdown_docs/)."""
    base = Path(out_dir) / "docs"
    pkg = pkg.strip("/")
    if pkg:
        base = base / pkg
    return base / "markdown_docs" / f"{stem}.md"
