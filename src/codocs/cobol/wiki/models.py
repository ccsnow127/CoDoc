"""data models of the COBOL wiki (DigestEntry is deepdoc's)."""

from __future__ import annotations

from pydantic import BaseModel, Field

# one-line summary of a paragraph / rule / interface item (kinds: purpose | paragraph | rule |
# interface | file | static); the deepdoc model itself, so COBOL digests feed deepdoc pages
from codocs.repowiki.deepdoc.models import DigestEntry

__all__ = ["DigestEntry", "PageDoc", "PagePlan", "ProgramDigest", "SectionPlan", "WikiPlan"]


class ProgramDigest(BaseModel):
    """condensed per-program knowledge used as LLM context for wiki pages."""

    program_id: str
    source: str = "static"  # "leafdoc" or "static"
    purpose: str = ""
    entries: list[DigestEntry] = Field(default_factory=list)


class PagePlan(BaseModel):
    """one wiki page: a coherent group of programs plus focus workflows."""

    id: str
    title: str
    programs: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    description: str = ""


class SectionPlan(BaseModel):
    id: str
    title: str
    pages: list[PagePlan] = Field(default_factory=list)


class WikiPlan(BaseModel):
    project_title: str = ""
    project_summary: str = ""
    sections: list[SectionPlan] = Field(default_factory=list)

    def all_pages(self) -> list[PagePlan]:
        return [p for s in self.sections for p in s.pages]


class PageDoc(BaseModel):
    id: str
    title: str
    markdown: str = ""
    summary: str = ""
    # diagram edges / messages removed or relabeled because the index does not support them
    grounding: list[str] = Field(default_factory=list)
