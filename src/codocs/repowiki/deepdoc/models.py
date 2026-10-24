"""data models for the deepdoc high-level wiki pipeline."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GoSymbol(BaseModel):
    """an exported (or package-main) top-level symbol in a Go package."""

    name: str
    kind: str = "func"  # func, method, type
    receiver: str = ""  # for methods: the receiver type name
    file: str = ""  # file path relative to module root
    exported: bool = True


class GoPackage(BaseModel):
    """one Go package (a directory of .go files) inside a module."""

    path: str  # package dir relative to module root ("" = module root)
    name: str = ""  # package clause name
    is_main: bool = False
    doc_comment: str = ""  # package-level doc comment, if any
    files: list[str] = Field(default_factory=list)  # non-test .go files
    test_files: int = 0
    loc: int = 0  # non-test lines of code
    imports_internal: list[str] = Field(default_factory=list)  # package paths
    imports_external: list[str] = Field(default_factory=list)
    imports_stdlib: list[str] = Field(default_factory=list)
    symbols: list[GoSymbol] = Field(default_factory=list)


class DigestEntry(BaseModel):
    """one-line summary of a single symbol, from leaf docs or static analysis."""

    name: str
    kind: str = ""  # function, class, type, method
    summary: str = ""


class PackageDigest(BaseModel):
    """condensed per-package knowledge used as LLM context."""

    package: str
    source: str = "static"  # "leafdocs" or "static"
    entries: list[DigestEntry] = Field(default_factory=list)


class PagePlan(BaseModel):
    """one wiki page: a coherent group of packages plus focus workflows."""

    id: str
    title: str
    packages: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    description: str = ""


class SectionPlan(BaseModel):
    """a sidebar section grouping several pages."""

    id: str
    title: str
    pages: list[PagePlan] = Field(default_factory=list)


class WikiPlan(BaseModel):
    """the full table of contents produced by the planner."""

    project_title: str = ""
    project_summary: str = ""
    sections: list[SectionPlan] = Field(default_factory=list)

    def all_pages(self) -> list[PagePlan]:
        return [p for s in self.sections for p in s.pages]


class PageDoc(BaseModel):
    """a generated wiki page plus its short summary for upstream pages."""

    id: str
    title: str
    markdown: str = ""
    summary: str = ""  # 2-3 sentence digest consumed by architecture/overview pages
