"""assemble generated pages into a Wiki: cross-links, sidebar, reference appendix, leaf docs.

assemble() builds a COBOL-only wiki on its own. extend() adds the COBOL appendix and
program pages to a wiki that deepdoc's assembler built (the multi-language wiki);
protect_links() keeps the COBOL page placeholders away from deepdoc's resolver.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from codocs.cobol.docs import LeafDoc
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.wiki import mermaid
from codocs.cobol.wiki.models import PageDoc, ProgramDigest, WikiPlan
from codocs.cobol.wiki.planner import role_label
from codocs.repowiki.core.wiki_builder import SidebarItem, Wiki, WikiPage

# also consume backticks the LLM may wrap around the placeholder, since the
# replacement already renders the name in code font
_LINK_RE = re.compile(r"`?\[\[(prog|copy|job|data|page|cpage):\s*([^\]]+?)\s*\]\]`?")
# [[page:ID]] of a page deepdoc's assembler does not know, protected until extend()
_PAGE_REF_RE = re.compile(r"\[\[page:\s*([^\]\s]+?)\s*\]\]")
_SRC_EXT_RE = re.compile(r"\.(cbl|cob|cobol|cpy|copy|jcl|prc|proc|bms)$", re.IGNORECASE)

APPENDIX = (
    ("program-index", "Program Index"),
    ("copybook-index", "Copybook Index"),
    ("batch-jobs", "Batch Jobs"),
    ("data-stores", "Data Stores"),
)


def leaf_page_id(program_id: str) -> str:
    """file-name safe page id of a program's leaf doc (prog-coactvwc)."""
    return "prog-" + (re.sub(r"[^a-z0-9_]+", "-", program_id.lower()).strip("-") or "x")


class _Linker:
    """resolves [[kind:NAME]] placeholders to markdown links between exported pages."""

    def __init__(
        self,
        index: CobolRepoIndex,
        prog_page: dict[str, str],
        leaf_pages: dict[str, str],
        titles: dict[str, str],
        appendix: set[str],
    ):
        self.index = index
        self.prog_page = prog_page
        self.leaf_pages = leaf_pages
        self.titles = titles
        self.appendix = appendix
        self.existing: set[str] = set()
        self.store_of: dict[str, str] = {}
        for name, ds in index.datastores.items():
            self.store_of[name.upper()] = name
            for alias in ds.aliases:
                self.store_of.setdefault(alias.upper(), name)

    def target(self, kind: str, name: str) -> tuple[str, str | None]:
        """(display label, page id or None) for one placeholder."""
        if kind in ("page", "cpage"):
            pid = name.strip("`").lower()
            return self.titles.get(pid, name), pid if pid in self.existing else None
        key = _SRC_EXT_RE.sub("", name.strip("`'\" ")).upper()
        if kind == "prog":
            pid = self.index.program_of(key) or key
            page = self.leaf_pages.get(pid) or self.prog_page.get(pid)
            return f"`{pid}`", page
        if kind == "copy":
            page = "copybook-index" if key in self.index.copybooks else None
            return f"`{key}`", page if page in self.appendix else None
        if kind == "job":
            page = "batch-jobs" if key in self.index.jobs else None
            return f"`{key}`", page if page in self.appendix else None
        store = self.store_of.get(key)
        page = "data-stores" if store else None
        return f"`{name.strip('`')}`", page if page in self.appendix else None

    def resolve(self, markdown: str, self_id: str) -> str:
        def link(m: re.Match) -> str:
            label, page = self.target(m.group(1), m.group(2))
            if page and page != self_id:
                return f"[{label}]({page}.md)"
            return label

        # placeholders never belong inside mermaid code: strip them to the bare name there
        out: list[str] = []
        in_fence = False
        for line in markdown.split("\n"):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                out.append(line)
                continue
            if in_fence:
                out.append(_LINK_RE.sub(lambda m: m.group(2), line))
            else:
                out.append(_LINK_RE.sub(link, line))
        return "\n".join(out)


def assemble(
    plan: WikiPlan,
    docs: list[PageDoc],
    index: CobolRepoIndex,
    digests: dict[str, ProgramDigest],
    leafdocs: dict[str, LeafDoc] | None = None,
) -> Wiki:
    """overview, architecture, subsystem pages, reference appendix and per-program leaf docs."""
    by_id = {d.id: d for d in docs}
    prog_page = {
        pid: page.id for page in plan.all_pages() if page.id in by_id for pid in page.programs
    }
    leafdocs = {k: v for k, v in (leafdocs or {}).items() if k in index.programs and v.markdown}
    leaf_pages = {pid: leaf_page_id(pid) for pid in leafdocs}

    appendix = {"program-index"}
    if index.copybooks:
        appendix.add("copybook-index")
    if index.jobs:
        appendix.add("batch-jobs")
    if index.datastores:
        appendix.add("data-stores")

    # planned-but-ungenerated pages keep their title as plain text, never a dead link
    titles = {p.id: p.title for p in plan.all_pages()}
    titles.update({d.id: d.title for d in docs})
    titles.update({pid: t for pid, t in APPENDIX if pid in appendix})
    titles.update({leaf_pages[p]: p for p in leafdocs})
    linker = _Linker(index, prog_page, leaf_pages, titles, appendix)
    linker.existing = set(by_id) | appendix | set(leaf_pages.values())

    pages: list[WikiPage] = []
    sidebar: list[SidebarItem] = []

    def add(pid: str, title: str, content: str, parent: str = "") -> None:
        # leaf docs and cached pages may hold diagrams that were never sanitized
        content, _problems = mermaid.sanitize_page(linker.resolve(content, pid))
        pages.append(WikiPage(id=pid, title=title, content=content,
                              parent_id=parent, order=len(pages)))

    # overview first, then architecture, then subsystem pages in section order
    for pid, title in (("index", "Overview"), ("architecture", "Architecture")):
        doc = by_id.get(pid)
        if doc:
            add(pid, title, doc.markdown)
            sidebar.append(SidebarItem(title=title, page_id=pid))

    for section in plan.sections:
        item = SidebarItem(title=section.title, page_id="", children=[])
        for p in section.pages:
            doc = by_id.get(p.id)
            if not doc:
                continue
            add(p.id, doc.title, doc.markdown + _programs_footer(p.programs), section.id)
            item.children.append(SidebarItem(title=doc.title, page_id=p.id))
        if item.children:
            sidebar.append(item)

    # deterministic reference appendix
    ref = SidebarItem(title="Reference", page_id="", children=[])
    builders = {
        "program-index": lambda: _program_index(index, digests, leafdocs, prog_page, titles),
        "copybook-index": lambda: _copybook_index(index),
        "batch-jobs": lambda: _batch_jobs(index),
        "data-stores": lambda: _data_stores(index),
    }
    for pid, title in APPENDIX:
        if pid in appendix:
            add(pid, title, builders[pid](), "reference")
            ref.children.append(SidebarItem(title=title, page_id=pid))
    sidebar.append(ref)

    # per-program leaf docs, one page each
    if leafdocs:
        leaf_item = SidebarItem(title="Program Reference", page_id="", children=[])
        for pid in sorted(leafdocs):
            doc = leafdocs[pid]
            content = _with_breadcrumb(doc.markdown, pid, index, prog_page, titles)
            add(leaf_pages[pid], pid, content, "program-reference")
            leaf_item.children.append(SidebarItem(title=pid, page_id=leaf_pages[pid]))
        sidebar.append(leaf_item)

    return Wiki(pages=pages, sidebar=sidebar, project_name=plan.project_title or index.name)


def _programs_footer(programs: list[str]) -> str:
    """deterministic list of the page's programs, so every program is reachable."""
    if not programs:
        return ""
    links = ", ".join(f"[[prog:{p}]]" for p in programs)
    return f"\n\n---\n\n**Programs on this page:** {links}\n"


def _with_breadcrumb(
    markdown: str, pid: str, index: CobolRepoIndex, prog_page: dict[str, str],
    titles: dict[str, str],
) -> str:
    """insert a 'part of' line under the leaf doc's first heading."""
    prog = index.programs[pid]
    crumbs = [f"Source: `{prog.path}`", f"Type: {role_label(index, pid)}"]
    page = prog_page.get(pid)
    if page:
        crumbs.insert(0, f"Subsystem: [[page:{page}]]")
    line = "> " + " · ".join(crumbs)
    lines = markdown.strip().split("\n")
    if lines and lines[0].startswith("# "):
        return "\n".join([lines[0], "", line, *lines[1:]])
    return "\n".join([f"# {pid}", "", line, "", *lines])


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _list_cell(items: list[str], kind: str, limit: int = 8) -> str:
    shown = [f"[[{kind}:{x}]]" for x in items[:limit]]
    more = f" +{len(items) - limit} more" if len(items) > limit else ""
    return (", ".join(shown) + more) or "—"


def _program_index(
    index: CobolRepoIndex,
    digests: dict[str, ProgramDigest],
    leafdocs: dict[str, LeafDoc],
    prog_page: dict[str, str],
    titles: dict[str, str],
) -> str:
    leaf_n = sum(1 for d in digests.values() if d.source == "leafdoc")
    lines = [
        "# Program Index",
        "",
        f"All {len(index.programs)} COBOL programs in `{index.name}` with the subsystem page "
        f"that covers each. Purposes come from the per-program documentation ({leaf_n} programs) "
        "or, where missing, from the program's header comments.",
        "",
        "| Program | Type | LOC | Subsystem | Purpose |",
        "|---|---|---|---|---|",
    ]
    for pid in sorted(index.programs):
        prog = index.programs[pid]
        d = digests.get(pid)
        purpose = (d.purpose or (d.entries[0].summary if d.entries else "")) if d else ""
        if pid in leafdocs and leafdocs[pid].purpose:
            purpose = leafdocs[pid].purpose
        page = prog_page.get(pid)
        page_ref = f"[[page:{page}]]" if page else "—"
        lines.append(
            f"| [[prog:{pid}]] | {_cell(role_label(index, pid), 40)} | {prog.loc} | {page_ref} | "
            f"{_cell(purpose or PurePosixPath(prog.path).name)} |"
        )
    entry = index.entrypoints()
    lines += [
        "",
        f"Entry points: {len(entry['jcl'])} run by JCL, {len(entry['cics'])} CICS online, "
        f"{len(entry['standalone'])} standalone main programs.",
    ]
    return "\n".join(lines)


def _copybook_index(index: CobolRepoIndex) -> str:
    lines = [
        "# Copybook Index",
        "",
        f"All {len(index.copybooks)} copybooks, the programs that COPY them and the level-01 "
        "records they define.",
        "",
        "| Copybook | Kind | Path | Used by | Top items |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(index.copybooks):
        cb = index.copybooks[name]
        users = sorted(set(cb.used_by))
        items = ", ".join(cb.top_items[:5]) + (" …" if len(cb.top_items) > 5 else "")
        lines.append(
            f"| `{name}` | {cb.kind} | `{_cell(cb.path, 80)}` | {_list_cell(users, 'prog')} | "
            f"{_cell(items, 120) or '—'} |"
        )
    missing = index.summary().missing_copybooks
    if missing:
        lines += ["", "Referenced but not found in the repository: "
                  + ", ".join(f"`{m}`" for m in missing[:40])]
    return "\n".join(lines)


def _batch_jobs(index: CobolRepoIndex) -> str:
    jobs = [j for _n, j in sorted(index.jobs.items())]
    n_jobs = sum(1 for j in jobs if j.kind == "job")
    lines = [
        "# Batch Jobs",
        "",
        f"{n_jobs} JCL jobs and {len(jobs) - n_jobs} catalogued procedures. Steps show the "
        "application program a step runs (or the utility / PGM name), datasets are the DSNs "
        "allocated by DD statements.",
        "",
        "| Job | Kind | Description | Steps | Programs | Datasets |",
        "|---|---|---|---|---|---|",
    ]
    for key, job in sorted(index.jobs.items()):
        steps = "; ".join(
            f"{s.name}: {s.app_program or s.program or s.proc}" for s in job.steps[:10]
        ) + (f"; +{len(job.steps) - 10} more" if len(job.steps) > 10 else "")
        progs = sorted({s.app_program for s in job.steps if s.app_program in index.programs})
        dsns: list[str] = []
        for s in job.steps:
            for dsn in s.dd.values():
                for one in dsn.split(","):
                    one = one.strip()
                    if one and one not in dsns and _is_dataset(one):
                        dsns.append(one)
        ds_cell = ", ".join(f"`{d}`" for d in dsns[:5]) + (
            f" +{len(dsns) - 5} more" if len(dsns) > 5 else ""
        )
        lines.append(
            f"| `{key}` | {job.kind} | {_cell(job.description, 100) or '—'} | "
            f"{_cell(steps, 220) or '—'} | {_list_cell(progs, 'prog')} | {ds_cell or '—'} |"
        )
    return "\n".join(lines)


def _is_dataset(dsn: str) -> bool:
    up = dsn.upper()
    return (
        up not in {"*", "DUMMY", "SYSOUT", "NULLFILE"}
        and not up.startswith(("SYSOUT", "&", "SYS1.", "IMS.", "CEE.", "DSN", "CICS"))
        and "LOADLIB" not in up and "PROCLIB" not in up
    )


def _data_stores(index: CobolRepoIndex) -> str:
    lines = [
        "# Data Stores",
        "",
        f"{len(index.datastores)} data stores (datasets / VSAM files, CICS files, DB2 tables, "
        "queues) with the programs that read and write them and the JCL jobs that allocate them.",
        "",
        "| Store | Kind | Organization | Also known as | Written by | Read by | Jobs |",
        "|---|---|---|---|---|---|---|",
    ]
    stores = sorted(index.datastores.values(), key=lambda d: (d.kind, d.name))
    for ds in stores:
        aliases = ", ".join(f"`{a}`" for a in ds.aliases[:5]) or "—"
        lines.append(
            f"| `{ds.name}` | {ds.kind} | {ds.organization or '—'} | {aliases} | "
            f"{_list_cell(sorted(ds.writers), 'prog')} | {_list_cell(sorted(ds.readers), 'prog')} | "
            f"{_list_cell(sorted(ds.jobs), 'job', 6)} |"
        )
    return "\n".join(lines)


# -- multi-language wiki (deepdoc assembly + COBOL pages) ---------------------------------


def protect_links(docs: list, known: set[str]) -> list:
    """copies of deepdoc PageDocs whose `[[page:ID]]` placeholders of pages deepdoc does not
    know (the COBOL appendix and program pages) become `[[cpage:ID]]`, which deepdoc's
    resolver leaves alone and extend() resolves."""
    def fix(m: re.Match) -> str:
        pid = m.group(1).strip("`")
        return m.group(0) if pid in known else f"[[cpage:{pid}]]"

    return [d.model_copy(update={"markdown": _PAGE_REF_RE.sub(fix, d.markdown)}) for d in docs]


def extend(
    wiki: Wiki,
    plan,
    docs: list,
    index: CobolRepoIndex,
    keys: dict[str, str],
    program_docs: dict[str, LeafDoc] | None = None,
    languages: list[str] | tuple[str, ...] = ("cobol",),
    *,
    digests: dict[str, ProgramDigest] | None = None,
    grounding: dict[str, list[str]] | None = None,
    wiki_dir=None,
) -> Wiki:
    """add the COBOL reference pages to a deepdoc-assembled *wiki*.

    *plan* is the deepdoc plan, *keys* maps COBOL package keys to program ids. Adds the
    appendix pages (program-index, copybook-index, batch-jobs, data-stores) and one
    `prog-<pid>` page per program with a doc; resolves the [[prog:]], [[copy:]], [[job:]],
    [[data:]] and [[page:]] placeholders in every page; a COBOL-only wiki gets program-index
    instead of deepdoc's package-index. With *wiki_dir*, writes grounding.json there.
    """
    import json
    from pathlib import Path

    from codocs.cobol.wiki.planner import build_digests

    cobol_only = list(languages) == ["cobol"]
    digests = digests if digests is not None else build_digests(index)
    pids_of: dict[str, list[str]] = {}
    for page in plan.all_pages():
        pids_of[page.id] = [keys[k] for k in page.packages if k in keys]
    page_ids = {p.id for p in wiki.pages}
    prog_page = {pid: page for page, ps in pids_of.items() if page in page_ids for pid in ps}
    wanted = set(keys.values())
    leafdocs = {k: v for k, v in (program_docs or {}).items()
                if k in index.programs and k in wanted and v.markdown}
    leaf_pages = {pid: leaf_page_id(pid) for pid in leafdocs}

    appendix = {"program-index"}
    if index.copybooks:
        appendix.add("copybook-index")
    if index.jobs:
        appendix.add("batch-jobs")
    if index.datastores:
        appendix.add("data-stores")

    pages = [p for p in wiki.pages if not (cobol_only and p.id == "package-index")]
    sidebar = [s for s in wiki.sidebar if not (cobol_only and s.page_id == "package-index")]
    titles = {p.id: p.title for p in plan.all_pages()}
    titles.update({p.id: p.title for p in pages})
    titles.update({pid: t for pid, t in APPENDIX if pid in appendix})
    titles.update({leaf_pages[p]: p for p in leafdocs})
    linker = _Linker(index, prog_page, leaf_pages, titles, appendix)
    linker.existing = {p.id for p in pages} | appendix | set(leaf_pages.values())

    cobol_pages = {pid for pid, ps in pids_of.items() if ps}
    if cobol_only:
        cobol_pages |= {"index", "architecture"}
    out: list[WikiPage] = []

    def add(pid: str, title: str, content: str, parent: str = "", cobol: bool = True) -> None:
        content = linker.resolve(content, pid)
        if cobol:
            content, _problems = mermaid.sanitize_page(content)
        out.append(WikiPage(id=pid, title=title, content=content, parent_id=parent,
                            order=len(out)))

    for p in pages:
        content = p.content
        if pids_of.get(p.id):
            content += _programs_footer(pids_of[p.id])
        add(p.id, p.title, content, p.parent_id, p.id in cobol_pages)

    ref = SidebarItem(title="Reference", page_id="", children=[])
    builders = {
        "program-index": lambda: _program_index(index, digests, leafdocs, prog_page, titles),
        "copybook-index": lambda: _copybook_index(index),
        "batch-jobs": lambda: _batch_jobs(index),
        "data-stores": lambda: _data_stores(index),
    }
    for pid, title in APPENDIX:
        if pid in appendix:
            add(pid, title, builders[pid](), "reference")
            ref.children.append(SidebarItem(title=title, page_id=pid))
    sidebar.append(ref)
    if leafdocs:
        leaf_item = SidebarItem(title="Programs", page_id="", children=[])
        for pid in sorted(leafdocs):
            content = _with_breadcrumb(leafdocs[pid].markdown, pid, index, prog_page, titles)
            add(leaf_pages[pid], pid, content, "programs")
            leaf_item.children.append(SidebarItem(title=pid, page_id=leaf_pages[pid]))
        sidebar.append(leaf_item)
    if wiki_dir is not None:
        path = Path(wiki_dir) / "grounding.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(sorted((grounding or {}).items())), indent=1,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
    return Wiki(pages=out, sidebar=sidebar, project_name=wiki.project_name)
