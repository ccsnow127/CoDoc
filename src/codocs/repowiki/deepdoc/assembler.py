"""assemble generated pages into a Wiki: cross-links, sidebar, package index."""

from __future__ import annotations

import re

from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.core.wiki_builder import SidebarItem, Wiki, WikiPage
from codocs.repowiki.deepdoc.models import PackageDigest, PageDoc, WikiPlan

# also consume backticks the LLM may wrap around the placeholder, since the
# replacement already renders the package name in code font
_PKG_LINK_RE = re.compile(r"`?\[\[pkg:([^\]\s]+)\]\]`?")
_PAGE_LINK_RE = re.compile(r"`?\[\[page:([^\]\s]+)\]\]`?")


def assemble(
    plan: WikiPlan,
    docs: list[PageDoc],
    index: GoModuleIndex,
    digests: dict[str, PackageDigest],
) -> Wiki:
    pkg_to_page: dict[str, str] = {
        pkg: page.id for page in plan.all_pages() for pkg in page.packages
    }
    titles = {d.id: d.title for d in docs}

    pages: list[WikiPage] = []
    by_id = {d.id: d for d in docs}

    def _resolve(markdown: str, self_id: str) -> str:
        def pkg_link(m: re.Match) -> str:
            pkg = m.group(1).strip("`")
            target = pkg_to_page.get(pkg)
            label = f"`{pkg}`"
            if target and target != self_id:
                return f"[{label}]({target}.md)"
            return label

        def page_link(m: re.Match) -> str:
            pid = m.group(1)
            if pid in titles and pid != self_id:
                return f"[{titles[pid]}]({pid}.md)"
            return titles.get(pid, pid)

        markdown = _PKG_LINK_RE.sub(pkg_link, markdown)
        return _PAGE_LINK_RE.sub(page_link, markdown)

    # overview first, then architecture, then module pages in section order
    order = 0
    sidebar: list[SidebarItem] = []
    for pid, title in (("index", "Overview"), ("architecture", "Architecture")):
        doc = by_id.get(pid)
        if doc:
            pages.append(WikiPage(id=pid, title=title, content=_resolve(doc.markdown, pid), order=order))
            sidebar.append(SidebarItem(title=title, page_id=pid))
            order += 1

    for section in plan.sections:
        item = SidebarItem(title=section.title, page_id="", children=[])
        for p in section.pages:
            doc = by_id.get(p.id)
            if not doc:
                continue
            pages.append(
                WikiPage(
                    id=p.id, title=doc.title, content=_resolve(doc.markdown, p.id), order=order
                )
            )
            item.children.append(SidebarItem(title=doc.title, page_id=p.id))
            order += 1
        if item.children:
            sidebar.append(item)

    # deterministic appendix: every package -> its page, with a one-liner
    index_page = _package_index_page(plan, index, digests, pkg_to_page, titles)
    pages.append(WikiPage(id="package-index", title="Package Index", content=index_page, order=order))
    sidebar.append(SidebarItem(title="Package Index", page_id="package-index"))

    return Wiki(pages=pages, sidebar=sidebar, project_name=plan.project_title or index.module)


def _package_index_page(
    plan: WikiPlan,
    index: GoModuleIndex,
    digests: dict[str, PackageDigest],
    pkg_to_page: dict[str, str],
    titles: dict[str, str],
) -> str:
    leaf_covered = sum(
        1 for d in digests.values() if d.source == "leafdocs" and d.entries
    )
    lines = [
        "# Package Index",
        "",
        f"All {len(index.packages)} Go packages in `{index.module}`, with the wiki page that "
        "documents each. Summaries come from the per-symbol leaf documentation "
        f"({leaf_covered} packages covered) or, where missing, from static analysis.",
        "",
        "| Package | LOC | Documented in | Summary |",
        "|---|---|---|---|",
    ]
    for path in sorted(index.packages):
        pkg = index.packages[path]
        digest = digests.get(path)
        summary = ""
        if digest and digest.entries:
            summary = digest.entries[0].summary
            if digest.source == "static":
                summary = pkg.doc_comment or f"{len(pkg.symbols)} symbols (no leaf docs)"
        summary = summary.replace("|", "\\|")[:140]
        page_id = pkg_to_page.get(path, "")
        page_ref = f"[{titles.get(page_id, page_id)}]({page_id}.md)" if page_id else "—"
        main_mark = " ⚙" if pkg.is_main else ""
        lines.append(f"| `{path or '(root)'}`{main_mark} | {pkg.loc} | {page_ref} | {summary} |")
    lines.append("")
    lines.append("⚙ = runtime binary (`package main`)")
    return "\n".join(lines)
