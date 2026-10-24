"""wiki page planning: deterministic skeleton from the package tree + LLM refinement."""

from __future__ import annotations

import logging
import re

from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.deepdoc.models import PackageDigest, PagePlan, SectionPlan, WikiPlan

logger = logging.getLogger(__name__)

# character budgets for the digest context that backs a single page
PAGE_BUDGET_CHARS = 120_000
MERGE_BUDGET_CHARS = 40_000


def digest_cost(digest: PackageDigest | None) -> int:
    """rough context cost (chars) a package contributes to its page."""
    if digest is None:
        return 200
    return 80 + sum(len(e.name) + len(e.summary) + 8 for e in digest.entries)


def build_skeleton(
    index: GoModuleIndex,
    digests: dict[str, PackageDigest],
    page_budget: int = PAGE_BUDGET_CHARS,
    merge_budget: int = MERGE_BUDGET_CHARS,
) -> WikiPlan:
    """split the package tree into sections/pages under a per-page context budget."""

    def cost(pkgs: list[str]) -> int:
        return sum(digest_cost(digests.get(p)) for p in pkgs)

    groups: dict[str, list[str]] = {}
    for path in sorted(index.packages):
        groups.setdefault(index.group_of(path), []).append(path)

    sections: list[SectionPlan] = []
    for group, pkgs in groups.items():
        pages = _split_pages(group, pkgs, 1, cost, page_budget, merge_budget)
        section = SectionPlan(id=_slug(group), title=group, pages=pages)
        sections.append(section)

    # largest groups first so the sidebar leads with the substantial subsystems
    sections.sort(key=lambda s: -sum(index.packages[p].loc for pg in s.pages for p in pg.packages))
    return WikiPlan(project_title=index.module, sections=sections)


def _split_pages(
    prefix: str,
    pkgs: list[str],
    depth: int,
    cost,
    page_budget: int,
    merge_budget: int,
) -> list[PagePlan]:
    if cost(pkgs) <= page_budget or len(pkgs) == 1:
        return [_make_page(prefix, pkgs)]

    # bucket by the next path segment; packages at or above this depth stay in "core"
    buckets: dict[str, list[str]] = {}
    core: list[str] = []
    for p in pkgs:
        parts = p.split("/")
        if len(parts) <= depth:
            core.append(p)
        else:
            buckets.setdefault("/".join(parts[: depth + 1]), []).append(p)

    pages: list[PagePlan] = []
    small: list[str] = list(core)
    for sub_prefix in sorted(buckets):
        sub = buckets[sub_prefix]
        if cost(sub) <= merge_budget:
            small.extend(sub)
        else:
            pages.extend(_split_pages(sub_prefix, sub, depth + 1, cost, page_budget, merge_budget))

    # merged leftovers become one or more "<prefix> core" pages, chunked to budget
    if small:
        chunks: list[list[str]] = []
        chunk: list[str] = []
        for p in sorted(small):
            if chunk and cost(chunk + [p]) > page_budget:
                chunks.append(chunk)
                chunk = []
            chunk.append(p)
        if chunk:
            chunks.append(chunk)
        for i, ch in enumerate(chunks):
            title = f"{prefix} core" if len(chunks) == 1 else f"{prefix} core ({i + 1})"
            pages.insert(i, _make_page(title, ch, page_id=_slug(title)))

    return pages


def _make_page(prefix: str, pkgs: list[str], page_id: str | None = None) -> PagePlan:
    return PagePlan(id=page_id or _slug(prefix), title=prefix, packages=sorted(pkgs))


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "root"


def validate_refined_plan(skeleton: WikiPlan, refined: WikiPlan) -> WikiPlan:
    """accept an LLM-refined plan only if it keeps every page and its packages intact."""
    base_pages = {p.id: p for p in skeleton.all_pages()}
    seen: set[str] = set()
    for page in refined.all_pages():
        original = base_pages.get(page.id)
        if original is None or page.id in seen:
            logger.warning("refined plan altered page ids (%s); keeping skeleton", page.id)
            return skeleton
        seen.add(page.id)
        page.packages = original.packages  # package assignment is not negotiable
    if seen != set(base_pages):
        logger.warning("refined plan dropped pages; keeping skeleton")
        return skeleton
    if not refined.project_title:
        refined.project_title = skeleton.project_title
    return refined
