"""per-function doc sections from markdown_docs/ trees (RepoAgent and SpecForge-go formats)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from codocs.repowiki.analysis.leafdocs import _extract_summary

_HEADER_RE = re.compile(
    r"^#{2,4}\s+(FunctionDef|MethodDef|ClassDef|Function|Class)\s+(.+?)\s*$", re.MULTILINE
)
BODY_CHARS = 1500


@dataclass
class DocSection:
    pkg: str          # package dir relative to the module root ("" = root)
    name: str         # "Func" or "Recv.Method" (C++ `Cls::m` is stored as "Cls.m")
    summary: str
    body: str         # section text, capped at BODY_CHARS
    kind: str = ""    # FunctionDef, MethodDef, ClassDef, ...


class DocIndex:
    """(package, name) -> doc section; earlier roots win (pass generated docs first)."""

    def __init__(self) -> None:
        self.sections: dict[tuple[str, str], DocSection] = {}
        self.files = 0

    @classmethod
    def build(cls, roots: list[Path], docs_dirname: str = "markdown_docs") -> DocIndex:
        index = cls()
        for root in roots:
            root = Path(root)
            if root.is_dir():
                index._add_root(root.resolve(), docs_dirname)
        return index

    def _add_root(self, root: Path, docs_dirname: str) -> None:
        for dirpath, dirnames, _files in os.walk(root):
            if Path(dirpath).name != docs_dirname:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                continue
            dirnames[:] = []
            docs_dir = Path(dirpath)
            owner = docs_dir.parent.relative_to(root)
            # RepoAgent mirrors subpackages inside one tree: markdown_docs/<sub>/x.md
            # documents package <owner>/<sub>
            for sub, dirs, files in os.walk(docs_dir):
                dirs.sort()
                pkg = (owner / Path(sub).relative_to(docs_dir)).as_posix()
                pkg = "" if pkg == "." else pkg
                for fname in sorted(files):
                    if fname.endswith(".md"):
                        self._parse(Path(sub) / fname, pkg)

    def _parse(self, path: Path, pkg: str) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        matches = list(_HEADER_RE.finditer(text))
        if matches:
            self.files += 1
        for i, m in enumerate(matches):
            name = m.group(2).split("(")[0].strip().strip("`")
            if "::" in name:              # C++ headers (docfmt accepts them): graph form Cls.m
                name = re.sub(r"\s*::\s*", ".", name).strip(".")
            key = (pkg, name)
            if not name or key in self.sections:
                continue
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end():end].strip()
            self.sections[key] = DocSection(
                pkg=pkg, name=name, summary=_extract_summary(body),
                body=body if len(body) <= BODY_CHARS else body[:BODY_CHARS] + " ...",
                kind=m.group(1))

    def get(self, pkg: str, name: str) -> DocSection | None:
        sec = self.sections.get((pkg, name))
        if sec is None and "." in name:      # RepoAgent sometimes documents methods bare
            owner, bare = name.split(".", 1)
            sec = self.sections.get((pkg, bare))
            if sec is not None and bare == owner and sec.kind in ("ClassDef", "Class"):
                sec = None                   # a constructor `Cls.Cls` is not the class
        return sec

    def __len__(self) -> int:
        return len(self.sections)
