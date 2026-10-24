"""wiki page planning: deterministic skeleton (directories or call-graph communities) + LLM refine.

Pages are groups of COBOL programs. When the repo's directory tree yields at least three
meaningful groups, each group becomes a section; otherwise programs are clustered with
Louvain communities on a weighted, undirected program graph (calls 3, same JCL job 2,
shared non-ubiquitous copybook 1, shared data store 1). Hub programs called from a large
share of the repo (a logger, a variable repository) carry no grouping signal: their call
edges are left out of the graph and they get their own "Shared services" page, so they
neither glue unrelated programs together nor name a cluster. Oversized groups are first split
into CICS-online vs batch programs, then into communities; stray singletons are merged by
connection weight or program-name prefix, so no page is a one-program fragment or a
40-program dump.

merge_into() puts these COBOL sections into a deepdoc skeleton of a multi-language wiki,
where every COBOL program is one wiki package.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from itertools import combinations

import networkx as nx

from codocs.cobol.docs import LeafDoc
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.wiki.digest import digest_from_leafdoc, digest_from_static
from codocs.cobol.wiki.models import PagePlan, ProgramDigest, SectionPlan, WikiPlan

logger = logging.getLogger(__name__)

# character budget for the digest context that backs a single page
PAGE_BUDGET_CHARS = 120_000
# readers lose the thread beyond about a dozen programs per page
MAX_PAGE_PROGRAMS = 12
MIN_PAGE_PROGRAMS = 2
# copybooks / data stores used by more than this share of programs carry no grouping signal
UBIQUITY = 0.3
# shortest shared name prefix that counts as a naming family (CBACT*, COCRD*, LGAC*)
MIN_PREFIX = 4
# a program called by more than UBIQUITY of all programs (and at least this many) is a hub
HUB_MIN_CALLERS = 5
SHARED_SERVICES = "Shared services"

W_CALL, W_JOB, W_COPY, W_DATA = 3.0, 2.0, 1.0, 1.0

# page ids the assembler reserves for system and appendix pages
RESERVED_IDS = {
    "index", "architecture", "program-index", "copybook-index", "batch-jobs", "data-stores",
    "readme", "_sidebar",
}

ROLE_SECTIONS = {
    "online": "Online (CICS) Subsystems",
    "batch": "Batch Processing",
    "subprogram": "Shared Subprograms & Utilities",
}


# -- digests ------------------------------------------------------------------


def digest_cost(digest: ProgramDigest | None) -> int:
    """rough context cost (chars) a program contributes to its page."""
    if digest is None:
        return 400
    return 400 + len(digest.purpose) + sum(len(e.name) + len(e.summary) + 8 for e in digest.entries)


def build_digests(
    index: CobolRepoIndex, leafdocs: dict[str, LeafDoc] | None = None
) -> dict[str, ProgramDigest]:
    """one digest per program: from its leaf doc when available, else from static facts."""
    digests: dict[str, ProgramDigest] = {}
    for pid, prog in index.programs.items():
        doc = (leafdocs or {}).get(pid)
        digests[pid] = digest_from_leafdoc(doc, prog) if doc else digest_from_static(prog)
    return digests


# -- program roles and the clustering graph ---------------------------------


def program_role(index: CobolRepoIndex, pid: str) -> str:
    """online (CICS) | batch (JCL-run or main program) | subprogram (called / utility)."""
    prog = index.programs[pid]
    if prog.has_cics:
        return "online"
    if index.jobs_running(pid) or prog.is_batch_main:
        return "batch"
    return "subprogram"


def role_label(index: CobolRepoIndex, pid: str) -> str:
    """human-readable program type for prompts and tables."""
    prog = index.programs[pid]
    role = program_role(index, pid)
    tags = [t for t, on in (("DB2", prog.has_sql), ("IMS", prog.has_dli)) if on]
    base = {
        "online": "CICS online",
        "batch": "batch" + (" (JCL)" if index.jobs_running(pid) else " main"),
        "subprogram": "subprogram",
    }[role]
    return base + (" + " + "/".join(tags) if tags else "")


def hub_programs(index: CobolRepoIndex) -> set[str]:
    """shared-service programs: called by more than UBIQUITY of the repo's programs."""
    pg = index.program_graph()
    limit = max(HUB_MIN_CALLERS - 1, UBIQUITY * len(index.programs))
    return {
        p for p in pg
        if len({u for u in pg.predecessors(p) if u != p}) > limit
    }


def weighted_graph(
    index: CobolRepoIndex,
    programs: Iterable[str] | None = None,
    hubs: set[str] | None = None,
) -> nx.Graph:
    """undirected program graph weighted by calls, shared jobs, copybooks and data stores.

    Call edges into *hubs* (default: hub_programs) are skipped — every caller would
    otherwise be pulled towards the logger it shares with everyone else."""
    pids = sorted(programs if programs is not None else index.programs)
    hubs = hub_programs(index) if hubs is None else hubs
    wanted = set(pids)
    g = nx.Graph()
    g.add_nodes_from(pids)
    limit = max(2.0, UBIQUITY * len(index.programs))

    def bump(a: str, b: str, w: float, key: str, cap: float | None = None) -> None:
        if a == b or a not in wanted or b not in wanted:
            return
        if not g.has_edge(a, b):
            g.add_edge(a, b, weight=0.0)
        d = g.edges[a, b]
        if cap is not None:
            used = d.get(key, 0.0)
            if used >= cap:
                return
            d[key] = used + w
        d["weight"] += w

    pg = index.program_graph()
    for u, v in pg.edges:
        if v in hubs or u in hubs:
            continue
        bump(u, v, W_CALL, "call")
    for job in index.jobs.values():
        ran = sorted({s.app_program for s in job.steps if s.app_program in index.programs})
        for a, b in combinations(ran, 2):
            bump(a, b, W_JOB, "job", cap=W_JOB)
    for cb in index.copybooks.values():
        users = sorted(set(cb.used_by) & set(index.programs))
        if 2 <= len(users) <= limit:
            for a, b in combinations(users, 2):
                bump(a, b, W_COPY, "copy", cap=3 * W_COPY)
    for ds in index.datastores.values():
        users = sorted((set(ds.readers) | set(ds.writers)) & set(index.programs))
        if 2 <= len(users) <= limit:
            for a, b in combinations(users, 2):
                bump(a, b, W_DATA, "data", cap=3 * W_DATA)
    return g


# -- skeleton ------------------------------------------------------------------


def build_skeleton(
    index: CobolRepoIndex,
    digests: dict[str, ProgramDigest],
    page_budget: int = PAGE_BUDGET_CHARS,
    max_programs: int = MAX_PAGE_PROGRAMS,
) -> WikiPlan:
    """split the programs into sections/pages under a per-page context and size budget."""

    def cost(pids: list[str]) -> int:
        return sum(digest_cost(digests.get(p)) for p in pids)

    hubs = hub_programs(index)
    # hubs only take a page of their own when the rest still needs organising
    if len(index.programs) - len(hubs) < MIN_PAGE_PROGRAMS:
        hubs = set()
    graph = weighted_graph(index, hubs=hubs)
    rest = sorted(p for p in index.programs if p not in hubs)
    online = {p for p in rest if program_role(index, p) == "online"}
    groups: dict[str, list[str]] = {}
    for pid in rest:
        groups.setdefault(index.group_of(pid), []).append(pid)

    used_ids: set[str] = set()
    sections: list[SectionPlan] = []
    shared = _make_page(SHARED_SERVICES, sorted(hubs), used_ids) if hubs else None
    if shared is not None:
        shared.description = (
            "Programs called from most of the application (" + ", ".join(shared.programs)
            + "); documented once instead of on every caller's page."
        )
    if len(groups) >= 3:
        # directory mode: single-program folders are pooled so they do not become fragments
        pooled = sorted(p for ps in groups.values() if len(ps) < MIN_PAGE_PROGRAMS for p in ps)
        blocks = {g: ps for g, ps in groups.items() if len(ps) >= MIN_PAGE_PROGRAMS}
        if pooled:
            _absorb_pooled(blocks, pooled, graph, max_programs)
        for group, pids in blocks.items():
            clusters = _partition(pids, graph, cost, page_budget, max_programs, online)
            title = _group_title(group)
            pages = [
                _make_page(_cluster_title(c, title if len(clusters) == 1 else "", graph, hubs),
                           c, used_ids)
                for c in clusters
            ]
            sections.append(SectionPlan(id=_slug(title), title=title, pages=pages))
        sections.sort(key=lambda s: -sum(_loc(index, s)))
        if shared is not None:
            sections.append(SectionPlan(id="shared-services", title=SHARED_SERVICES,
                                        pages=[shared]))
    else:
        clusters = _partition(rest, graph, cost, page_budget, max_programs, online)
        if len(clusters) == 1:
            page = _make_page(f"{index.name} programs", clusters[0], used_ids)
            pages = [page] + ([shared] if shared is not None else [])
            return WikiPlan(project_title=index.name,
                            sections=[SectionPlan(id="application", title="Application",
                                                  pages=pages)])
        by_role: dict[str, list[list[str]]] = {}
        for c in clusters:
            roles = Counter(program_role(index, p) for p in c)
            role = max(ROLE_SECTIONS, key=lambda r: (roles.get(r, 0), -list(ROLE_SECTIONS).index(r)))
            by_role.setdefault(role, []).append(c)
        for role, title in ROLE_SECTIONS.items():
            cs = by_role.get(role)
            pages = [_make_page(_cluster_title(c, "", graph, hubs), c, used_ids)
                     for c in cs or []]
            if role == "subprogram" and shared is not None:
                pages.append(shared)
            if pages:
                sections.append(SectionPlan(id=role, title=title, pages=pages))
    _dedupe_section_ids(sections)
    return WikiPlan(project_title=index.name, sections=sections)


def _loc(index: CobolRepoIndex, section: SectionPlan) -> list[int]:
    return [index.programs[p].loc for pg in section.pages for p in pg.programs]


def _absorb_pooled(
    blocks: dict[str, list[str]], pooled: list[str], graph: nx.Graph, max_programs: int
) -> None:
    """attach programs of one-program folders to the most connected folder, else pool them."""
    rest: list[str] = []
    for pid in pooled:
        best, best_w = None, 0.0
        for group, pids in sorted(blocks.items()):
            w = sum(graph.edges[pid, q]["weight"] for q in pids if graph.has_edge(pid, q))
            if w > best_w:
                best, best_w = group, w
        if best is not None and len(blocks[best]) < max_programs:
            blocks[best].append(pid)
        else:
            rest.append(pid)
    if rest:
        blocks.setdefault("(other)", []).extend(rest)


def _partition(
    pids: list[str],
    graph: nx.Graph,
    cost,
    page_budget: int,
    max_programs: int,
    online: set[str] | None = None,
) -> list[list[str]]:
    """communities of *pids*, small ones merged, large ones split, all within budget.

    A large group that mixes CICS online and batch programs is split along that line first:
    mainframe readers think in online transactions vs batch jobs, and shared files would
    otherwise pull batch readers into screen-flow pages.
    """
    if len(pids) <= max_programs and cost(pids) <= page_budget:
        return [sorted(pids)]
    if online is not None:
        on = [p for p in pids if p in online]
        off = [p for p in pids if p not in online]
        if len(on) >= 3 and len(off) >= 3:
            return [
                *_partition(on, graph, cost, page_budget, max_programs),
                *_partition(off, graph, cost, page_budget, max_programs),
            ]
    sub = graph.subgraph(pids)
    comms = _communities(sub, max_programs, cost, page_budget)
    big = [c for c in comms if len(c) >= MIN_PAGE_PROGRAMS]
    small = sorted(p for c in comms if len(c) < MIN_PAGE_PROGRAMS for p in c)
    leftovers = _merge_small(small, big, sub, cost, page_budget, max_programs)
    clusters = big + _prefix_groups(leftovers, cost, page_budget, max_programs)
    clusters = _merge_families(clusters, sub, cost, page_budget, max_programs)
    # a final lone singleton joins the smallest page rather than standing alone
    singles = [c for c in clusters if len(c) == 1]
    if len(singles) == 1 and len(clusters) > 1:
        target = min((c for c in clusters if c is not singles[0]), key=len)
        if len(target) < max_programs and cost(target + singles[0]) <= page_budget:
            clusters.remove(singles[0])
            target.extend(singles[0])
    return sorted((sorted(c) for c in clusters), key=lambda c: (-len(c), c[0]))


def _communities(sub: nx.Graph, max_programs: int, cost, page_budget: int) -> list[list[str]]:
    """louvain communities; oversized ones are re-clustered at higher resolution, then chunked."""
    out: list[list[str]] = []
    todo: list[tuple[list[str], float]] = [(sorted(sub.nodes), 1.0)]
    while todo:
        nodes, res = todo.pop(0)
        g = sub.subgraph(nodes)
        if g.number_of_edges() == 0:
            out.extend([n] for n in sorted(nodes))
            continue
        found = _louvain(g, res)
        found = [sorted(c) for c in sorted(found, key=lambda c: sorted(c)[0])]
        for c in found:
            if len(c) <= max_programs and cost(c) <= page_budget:
                out.append(c)
            elif len(found) > 1 or res < 4.0:
                todo.append((c, res * 1.6 if len(found) == 1 else res * 1.3))
            else:
                out.extend(_chunk(c, cost, page_budget, max_programs))
    return out


def _louvain(g: nx.Graph, resolution: float) -> list[set[str]]:
    """louvain communities on integer labels (sorted program ids): networkx's louvain
    iterates sets of node labels, so string labels made the plan depend on the process's
    hash seed (PYTHONHASHSEED); integers make it reproducible across runs."""
    names = sorted(g.nodes)
    ix = {n: i for i, n in enumerate(names)}
    h = nx.Graph()
    h.add_nodes_from(range(len(names)))
    h.add_edges_from(sorted((min(ix[u], ix[v]), max(ix[u], ix[v]), dict(d))
                            for u, v, d in g.edges(data=True)))
    found = nx.community.louvain_communities(h, weight="weight", resolution=resolution, seed=42)
    return [{names[i] for i in c} for c in found]


def _merge_small(
    small: list[str],
    big: list[list[str]],
    sub: nx.Graph,
    cost,
    page_budget: int,
    max_programs: int,
) -> list[str]:
    """attach singletons to the best-connected or same-prefix cluster; return the rest."""
    rest: list[str] = []
    for pid in small:
        best, best_score = None, (0.0, 0)
        for c in big:
            if len(c) >= max_programs or cost(c + [pid]) > page_budget:
                continue
            w = sum(sub.edges[pid, q]["weight"] for q in c if sub.has_edge(pid, q))
            pre = max(_common_prefix_len(pid, q) for q in c)
            score = (w, pre if pre >= MIN_PREFIX else 0)
            if score > best_score:
                best, best_score = c, score
        if best is not None:
            best.append(pid)
        else:
            rest.append(pid)
    return rest


def _merge_families(
    clusters: list[list[str]], sub: nx.Graph, cost, page_budget: int, max_programs: int
) -> list[list[str]]:
    """fold tiny clusters into a cluster of the same naming family (COTRN00C -> COTRN02C)."""
    clusters = [list(c) for c in clusters]
    for small in sorted(clusters, key=lambda c: (len(c), c[0])):
        if len(small) > 3 or small not in clusters:
            continue
        best, best_score = None, (0, 0.0)
        for c in clusters:
            if c is small or len(c) + len(small) > max_programs or cost(c + small) > page_budget:
                continue
            pre = max(_common_prefix_len(a, b) for a in small for b in c)
            w = sum(sub.edges[a, b]["weight"] for a in small for b in c if sub.has_edge(a, b))
            if pre >= MIN_PREFIX + 1 and (pre, w) > best_score:
                best, best_score = c, (pre, w)
        if best is not None:
            best.extend(small)
            clusters.remove(small)
    return clusters


def _prefix_groups(pids: list[str], cost, page_budget: int, max_programs: int) -> list[list[str]]:
    """group leftover programs by shared name prefix; unrelated ones share an 'other' page."""
    families: dict[str, list[str]] = {}
    for pid in sorted(pids):
        families.setdefault(pid[:MIN_PREFIX], []).append(pid)
    out: list[list[str]] = []
    other: list[str] = []
    for fam in families.values():
        if len(fam) >= MIN_PAGE_PROGRAMS:
            out.extend(_chunk(fam, cost, page_budget, max_programs))
        else:
            other.extend(fam)
    if other:
        out.extend(_chunk(other, cost, page_budget, max_programs))
    return out


def _chunk(pids: list[str], cost, page_budget: int, max_programs: int) -> list[list[str]]:
    """split a sorted list into roughly equal chunks under both caps."""
    pids = sorted(pids)
    n = max(1, -(-len(pids) // max_programs))
    while True:
        size = -(-len(pids) // n)
        chunks = [pids[i : i + size] for i in range(0, len(pids), size)]
        if all(cost(c) <= page_budget for c in chunks) or size == 1:
            return chunks
        n += 1


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _group_title(group: str) -> str:
    if group == "(root)":
        return "Core application"
    if group == "(other)":
        return "Other programs"
    return group


def _cluster_title(
    pids: list[str], group_title: str, graph: nx.Graph, hubs: set[str] | None = None
) -> str:
    """deterministic placeholder title; the refine step replaces it with a reader-facing one.

    Shared-service hubs never name a cluster: "LGSTSQ and related programs" says nothing
    about a page whose programs merely all log through LGSTSQ."""
    if group_title:
        return group_title
    named = [p for p in pids if p not in (hubs or set())] or pids
    prefix = named[0]
    for p in named[1:]:
        prefix = prefix[: _common_prefix_len(prefix, p)]
    if len(named) > 1 and len(prefix) >= 3:
        label = f"{prefix}* programs"
    else:
        hub = max(named, key=lambda p: (graph.degree(p, weight="weight") if p in graph else 0, p))
        label = f"{hub} and related programs" if len(pids) > 1 else hub
    return label


def _make_page(title: str, pids: list[str], used: set[str]) -> PagePlan:
    base = _slug(title)
    if base in RESERVED_IDS or base.startswith("prog-"):
        base = f"sub-{base}"
    pid, n = base, 2
    while pid in used:
        pid, n = f"{base}-{n}", n + 1
    used.add(pid)
    return PagePlan(id=pid, title=title, programs=sorted(pids))


def _dedupe_section_ids(sections: list[SectionPlan]) -> None:
    seen: set[str] = set()
    for s in sections:
        sid, n = s.id, 2
        while sid in seen:
            sid, n = f"{s.id}-{n}", n + 1
        s.id = sid
        seen.add(sid)


def _slug(name: str) -> str:
    """file-name safe page id."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:60].strip("-") or "root"


def validate_refined_plan(skeleton: WikiPlan, refined: WikiPlan) -> WikiPlan:
    """accept an LLM-refined plan only if it keeps every page and its programs intact."""
    base_pages = {p.id: p for p in skeleton.all_pages()}
    seen: set[str] = set()
    for page in refined.all_pages():
        original = base_pages.get(page.id)
        if original is None or page.id in seen:
            logger.warning("refined plan altered page ids (%s); keeping skeleton", page.id)
            return skeleton
        seen.add(page.id)
        page.programs = list(original.programs)  # program assignment is not negotiable
        if not page.title.strip():
            page.title = original.title
    if seen != set(base_pages):
        logger.warning("refined plan dropped pages; keeping skeleton")
        return skeleton
    # empty sections are noise; section ids must stay unique and file-name safe
    refined.sections = [s for s in refined.sections if s.pages]
    for s in refined.sections:
        s.id = _slug(s.id or s.title)
    _dedupe_section_ids(refined.sections)
    if not refined.project_title:
        refined.project_title = skeleton.project_title
    return refined


# -- deepdoc plans (multi-language wiki) -------------------------------------------


def _unique(base: str, used: set[str]) -> str:
    uid, n = base, 2
    while uid in used:
        uid, n = f"{base}-{n}", n + 1
    used.add(uid)
    return uid


def merge_into(skeleton, index, digests: dict[str, ProgramDigest] | None = None):
    """the deepdoc *skeleton* with its COBOL packages moved onto the COBOL planner's pages.

    *index* is a MultiLangIndex holding COBOL packages (`cobol_index`, `cobol_programs`,
    `package_of_program`). COBOL keys leave the deepdoc pages (emptied pages and sections
    are dropped), then the sections of build_skeleton() follow, with programs expressed as
    package keys. Page ids avoid the reserved ids and every deepdoc page id;
    `index.cobol_page_of` maps each COBOL key to its page.
    """
    from codocs.repowiki.deepdoc.models import PagePlan as DeepPage
    from codocs.repowiki.deepdoc.models import SectionPlan as DeepSection
    from codocs.repowiki.deepdoc.models import WikiPlan as DeepPlan

    keys = set(getattr(index, "cobol_programs", {}))
    sections = []
    for s in skeleton.sections:
        pages = []
        for p in s.pages:
            pkgs = [k for k in p.packages if k not in keys]
            if pkgs:
                pages.append(p.model_copy(update={"packages": pkgs}))
        if pages:
            sections.append(s.model_copy(update={"pages": pages}))
    page_of: dict[str, str] = {}
    index.cobol_page_of = page_of
    cobol = getattr(index, "cobol_index", None)
    other = bool(sections)
    if cobol is None or not keys:
        return DeepPlan(project_title=skeleton.project_title,
                        project_summary=skeleton.project_summary, sections=sections)
    if digests is None:
        digests = getattr(index, "cobol_digests", None) or build_digests(cobol)
    cobol_plan = build_skeleton(cobol, digests)
    used_pages = {p.id for s in sections for p in s.pages} | RESERVED_IDS | {"package-index"}
    used_sections = {s.id for s in sections}
    of_program = getattr(index, "package_of_program", {})
    for ls in cobol_plan.sections:
        pages = []
        for lp in ls.pages:
            pkgs = [of_program[p] for p in lp.programs
                    if of_program.get(p) in index.packages]
            if not pkgs:
                continue
            page_id = _unique(lp.id, used_pages)
            pages.append(DeepPage(id=page_id, title=lp.title, packages=pkgs,
                                  workflows=list(lp.workflows), description=lp.description))
            for k in pkgs:
                page_of[k] = page_id
        if pages:
            sections.append(DeepSection(id=_unique(ls.id, used_sections), title=ls.title,
                                        pages=pages))
    title = skeleton.project_title if other else (cobol_plan.project_title
                                                  or skeleton.project_title)
    return DeepPlan(project_title=title, project_summary=skeleton.project_summary,
                    sections=sections)
