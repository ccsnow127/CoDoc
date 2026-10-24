"""ingest leaf-node (per-symbol) markdown docs produced by `codocs docgen` or RepoAgent."""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from codocs.repowiki.deepdoc.models import DigestEntry, GoPackage, PackageDigest

_HEADER_RE = re.compile(
    r"^#{2,4}\s+(FunctionDef|ClassDef|MethodDef|Function|Class)\s+(.+?)\s*$", re.MULTILINE
)
_BOLD_PREFIX_RE = re.compile(r"^\*\*[^*]+\*\*\s*[::]\s*")

_KIND_MAP = {
    "FunctionDef": "function",
    "Function": "function",
    "MethodDef": "method",
    "ClassDef": "class",
    "Class": "class",
}


class LeafDocIndex:
    """maps package path -> PackageDigest built from markdown_docs leaf files."""

    def __init__(self):
        self.digests: dict[str, PackageDigest] = {}
        self.files_parsed = 0

    @classmethod
    def build(
        cls,
        root: str | Path,
        known_packages: set[str] | None = None,
        docs_dirname: str = "markdown_docs",
    ) -> LeafDocIndex:
        root = Path(root).resolve()
        index = cls()

        for dirpath, dirnames, filenames in os.walk(root):
            if Path(dirpath).name != docs_dirname:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                continue
            docs_dir = Path(dirpath)
            owner = str(docs_dir.parent.relative_to(root)).replace(os.sep, "/")
            if owner == ".":
                owner = ""
            dirnames[:] = []  # we walk the docs dir ourselves below

            for sub, _dirs, files in os.walk(docs_dir):
                for fname in sorted(files):
                    if not fname.endswith(".md"):
                        continue
                    rel_sub = str(Path(sub).relative_to(docs_dir)).replace(os.sep, "/")
                    package = owner
                    if rel_sub != ".":
                        # nested docs may mirror a subpackage of the owner
                        candidate = f"{owner}/{rel_sub}" if owner else rel_sub
                        if known_packages is None or candidate in known_packages:
                            package = candidate
                    index._parse_file(Path(sub) / fname, package)

        return index

    def _parse_file(self, path: Path, package: str) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        matches = list(_HEADER_RE.finditer(text))
        if not matches:
            return
        self.files_parsed += 1
        digest = self.digests.setdefault(
            package, PackageDigest(package=package, source="leafdocs")
        )
        seen = {e.name for e in digest.entries}
        for i, m in enumerate(matches):
            kind = _KIND_MAP.get(m.group(1), "function")
            name = m.group(2).split("(")[0].strip()
            if not name or name in seen:
                continue
            seen.add(name)
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            summary = _extract_summary(text[m.end() : end])
            digest.entries.append(DigestEntry(name=name, kind=kind, summary=summary))

    def coverage(self, all_packages: list[str]) -> tuple[int, int]:
        covered = sum(1 for p in all_packages if p in self.digests and self.digests[p].entries)
        return covered, len(all_packages)


def _extract_summary(body: str, max_chars: int = 300) -> str:
    """first meaningful line after a symbol header, trimmed to a sentence or two."""
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "```", "|", "·", "-")):
            continue
        line = _BOLD_PREFIX_RE.sub("", line)
        line = line.replace("**", "").strip()
        if not line:
            continue
        # cut at the first sentence end past a reasonable minimum
        cut = _sentence_cut(line, max_chars)
        return cut
    return ""


def _sentence_cut(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    dot = text.rfind(". ", 0, max_chars)
    if dot > 40:
        return text[: dot + 1]
    return text[:max_chars].rstrip() + "..."


def digest_from_static(pkg: GoPackage, max_symbols: int = 40) -> PackageDigest:
    """fallback digest for packages without leaf docs: exported symbols + doc comment."""
    digest = PackageDigest(package=pkg.path, source="static")
    if pkg.doc_comment:
        digest.entries.append(
            DigestEntry(name="(package doc)", kind="package", summary=pkg.doc_comment)
        )
    exported = [s for s in pkg.symbols if s.exported]
    # types first, then funcs; methods grouped implicitly by receiver name
    exported.sort(key=lambda s: (s.kind != "type", s.receiver, s.name))
    type_counts: Counter[str] = Counter(s.receiver for s in exported if s.receiver)
    for sym in exported[:max_symbols]:
        label = f"{sym.receiver}.{sym.name}" if sym.receiver else sym.name
        methods = (
            f" ({type_counts[sym.name]} methods)"
            if sym.kind == "type" and type_counts.get(sym.name)
            else ""
        )
        digest.entries.append(
            DigestEntry(name=label, kind=sym.kind, summary=f"defined in {sym.file}{methods}")
        )
    return digest
