"""orchestration of the six steps for one or more granularities K."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from codocs.bizlogic.cluster import build_hierarchy
from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.domain import use_profile
from codocs.bizlogic.export import (
    add_section,
    build_pages,
    is_section_page,
    load_wiki,
    node_info_of,
    page_ids,
    write_json,
)
from codocs.bizlogic.graph import DEFAULT_EXCLUDE, DEFAULT_PLUMBING, build_graph
from codocs.bizlogic.infer import (
    BusinessLogic,
    ClusterInfo,
    describe_clusters,
    infer_trajectories,
    label_clusters,
)
from codocs.bizlogic.profile import DomainProfile, from_dict
from codocs.bizlogic.walk import (
    Trajectory,
    coverage,
    deduplicate,
    root_candidates,
    sample_walks,
    select_roots,
)
from codocs.config import Settings
from codocs.repowiki.core.wiki_builder import Wiki

log = logging.getLogger(__name__)

PROFILE_FILE = "domain_profile.json"     # OUT/bizlogic/: the profile of the run, for re-rendering


@dataclass
class BizParams:
    clusters: tuple[int, ...] = (20,)   # K values (granularities) to produce
    resolution: float = 1.0             # louvain resolution of the base communities
    alpha: float = 0.6                  # structure vs text weight in the dendrogram
    pkg_weight: float = 0.5             # weight of the package tree in the dendrogram
    utility_fanin: int = 40
    infra_callers: int = 40             # packages called from this many packages: utilities
    fold_accessors: bool = True
    max_roots: int | None = 300
    min_reach: int = 3
    group_depth: int = 1                # subsystem = first N path segments (root stratification)
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    walks_per_root: int = 20
    walk_length: int = 12
    prefix_subsume: bool = True
    min_share: float = 0.02             # a sequence needs min_share * walks / K walks
    max_trajectories: int | None = None
    seed: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["clusters"] = list(self.clusters)
        d["exclude"] = list(self.exclude)
        return d


def _exclude_paths(settings: Settings) -> tuple[str, ...]:
    """the COBOL-scope --exclude-path globs of the settings (() when there are none)."""
    fn = getattr(settings, "exclude_list", None)
    return tuple(fn()) if callable(fn) else ()


def plumbing_rx(profile: DomainProfile | None) -> str:
    """DEFAULT_PLUMBING plus the profile's own plumbing names."""
    extra = profile.plumbing if profile else ""
    return f"{DEFAULT_PLUMBING}|{extra}" if extra else DEFAULT_PLUMBING


def stored_profile(bizlogic_dir: Path) -> DomainProfile | None:
    """the profile a run stored in OUT/bizlogic/domain_profile.json, else None."""
    path = Path(bizlogic_dir) / PROFILE_FILE
    if not path.is_file():
        return None
    return from_dict(json.loads(path.read_text(encoding="utf-8")), str(path))


def parent_k(k: int) -> int:
    """the coarser level shown above K in the hierarchy (system -> subsystems -> components)."""
    return max(2, round(math.sqrt(k)))


async def run_bizlogic(module_root: str | Path, out_dir: str | Path, settings: Settings,
                       params: BizParams, *, subtree: str | None = None,
                       docs_roots: list[Path] | None = None, wiki_dir: Path | None = None,
                       plan_only: bool = False, on_progress=None, languages=None,
                       profile: DomainProfile | None = None) -> dict:
    """languages: None = settings.source_lang (default auto: a Go module repo stays Go-only),
    a `--source-lang` string or a list; any set beyond ["go"] gives one cross-language graph.
    profile: the domain profile (None = the empty, domain-neutral default); it is active
    for the run and stored as OUT/bizlogic/domain_profile.json for bizlogic-render."""
    prev = use_profile(profile)
    try:
        return await _run(module_root, out_dir, settings, params, subtree=subtree,
                          docs_roots=docs_roots, wiki_dir=wiki_dir, plan_only=plan_only,
                          on_progress=on_progress, languages=languages, profile=profile)
    finally:
        use_profile(prev)


async def _run(module_root, out_dir, settings: Settings, params: BizParams, *, subtree,
               docs_roots, wiki_dir, plan_only, on_progress, languages,
               profile: DomainProfile | None) -> dict:
    root = Path(module_root).resolve()
    if languages is None:
        languages = getattr(settings, "source_lang", None)
    out = Path(out_dir) / "bizlogic"
    say = on_progress or (lambda _msg: None)
    t0 = time.monotonic()
    if profile is not None and not profile.empty:
        write_json(out / PROFILE_FILE, profile.to_dict())
        say(f"domain profile: {profile.name}")
    elif (out / PROFILE_FILE).is_file():
        (out / PROFILE_FILE).unlink()           # from an earlier run with a profile

    roots_for_docs = list(docs_roots or [])
    gen_docs = Path(out_dir) / "docs"
    if gen_docs.is_dir():
        roots_for_docs.insert(0, gen_docs)        # SpecForge-go docs win over in-tree docs
    roots_for_docs.append(root)
    docs = DocIndex.build(roots_for_docs)
    say(f"docs: {len(docs)} sections from {docs.files} files")

    exclude_paths = _exclude_paths(settings)
    fg = build_graph(root, subtree=subtree, docs=docs, utility_fanin=params.utility_fanin,
                     fold_accessors=params.fold_accessors, exclude=params.exclude,
                     infra_callers=params.infra_callers, languages=languages,
                     out_dir=Path(out_dir), exclude_paths=exclude_paths,
                     plumbing=plumbing_rx(profile))
    say(f"graph: {fg.stats['functions'] + fg.stats['methods']} functions, "
        f"{fg.stats['edges']} edges, {fg.stats['utilities']} utilities")
    h = build_hierarchy(fg, resolution=params.resolution, alpha=params.alpha, seed=params.seed,
                        pkg_weight=params.pkg_weight)
    candidates = root_candidates(fg, min_reach=params.min_reach)
    roots = candidates
    if params.max_roots and len(candidates) > params.max_roots:
        roots = select_roots(fg, candidates, params.max_roots, params.group_depth)
    walks = sample_walks(fg, [r for r, _ in roots], walks_per_root=params.walks_per_root,
                         max_len=params.walk_length, seed=params.seed, reach=dict(roots),
                         stop_at={r for r, _ in candidates})
    say(f"hierarchy: {h.max_k} base communities; {len(roots)} roots; {len(walks)} walks")
    biz = set(fg.business())
    visited = len({n for w in walks for n in w} & biz)
    graph_json = {
        "stats": fg.stats, "base_communities": h.max_k, "params": params.to_dict(),
        "candidate_roots": len(candidates), "walks": len(walks),
        "visited_business_functions": visited,
        "coverage": coverage(fg, candidates, roots, walks, depth=params.group_depth),
        "roots": [{"id": r, "reach": n} for r, n in roots],
    }
    if exclude_paths:                         # the COBOL scope, kept for re-rendering
        graph_json["exclude_paths"] = list(exclude_paths)
    write_json(out / "graph.json", graph_json)

    results: dict[int, dict] = {}
    llm_ctx = None
    if not plan_only:
        from codocs.llm import LLM
        llm_ctx = LLM(settings)
        llm = await llm_ctx.__aenter__()
    try:
        done: set[int] = set()
        for k in params.clusters:
            kk = min(k, h.max_k)
            assign = h.assign(kk)
            keff = len(set(assign.values()))
            if keff != k:
                log.warning("K=%d clamped to %d (base communities: %d)", k, keff, h.max_k)
                say(f"K={k} clamped to {keff} ({h.max_k} base communities)")
            if keff in done:
                continue
            done.add(keff)
            trajs, dstats = deduplicate(fg, walks, assign, prefix_subsume=params.prefix_subsume,
                                        max_trajectories=params.max_trajectories,
                                        min_share=params.min_share, k=keff)
            clusters = describe_clusters(fg, assign)
            pk = parent_k(len(clusters))
            parents = h.parents(kk, pk)
            kids: dict[int, list[int]] = {}
            for c, p in parents.items():
                kids.setdefault(p, []).append(c)
            parent_infos = describe_clusters(fg, h.assign(pk), children=kids)
            say(f"K={keff}: {len(clusters)} clusters, {dstats['distinct_signatures']} cluster "
                f"sequences -> {len(trajs)} trajectories")
            logic: dict = {}
            if llm_ctx is not None:
                await label_clusters(llm, fg, clusters, settings.language,
                                     settings.concurrency, say)
                await label_clusters(llm, fg, parent_infos, settings.language,
                                     settings.concurrency, say, children=clusters)
                logic = await infer_trajectories(llm, fg, trajs, clusters, len(clusters),
                                                 settings.language, assign,
                                                 settings.concurrency, say)
            stats = {
                "functions (business / utility)": f"{len(fg.business())} / {len(fg.utilities)}",
                "base communities": h.max_k, "clusters K": len(clusters),
                "parent level": pk, "entry roots (walked / candidates)":
                    f"{len(roots)} / {len(candidates)}",
                "walks": len(walks),
                "business functions visited": f"{visited} / {len(biz)}",
                "distinct cluster sequences": dstats["distinct_signatures"],
                "min walks per kept sequence": dstats["min_support"],
                "rarer sequences folded in": dstats["folded_low_support"],
                "after prefix merge": dstats["after_prefix_subsume"],
                "business logics": len(trajs),
            }
            if k != keff:
                stats["requested K"] = k
            if dstats["dropped_by_cap"]:
                stats["dropped by --max-trajectories"] = (
                    f"{dstats['dropped_by_cap']} ({dstats['dropped_own_walks']} walks)")
            cov = coverage(fg, candidates, roots, walks, trajs, depth=params.group_depth)
            kdir = out / f"k{keff}"
            write_json(kdir / "clusters.json", {
                "k": len(clusters), "parent_k": pk,
                "clusters": [c.to_dict() for c in clusters.values()],
                "parents": [c.to_dict() for c in parent_infos.values()],
                "stats": stats,
            })
            node_info = node_info_of(fg, clusters)
            write_json(kdir / "nodes.json", {n: list(v) for n, v in node_info.items()})
            write_json(kdir / "trajectories.json",
                       {"dedup": dstats, "coverage": cov,
                        "trajectories": [t.to_dict() for t in trajs]})
            write_json(kdir / "business_logic.json", [
                dict(t.to_dict(), logic=logic[t.id].to_dict() if t.id in logic else None,
                     functions=[{"id": n, "file": fg.nodes[n].file, "line": fg.nodes[n].line}
                                for n in t.path])
                for t in trajs])
            wiki = load_wiki(wiki_dir) if wiki_dir else Wiki(project_name=root.name)
            pages = build_pages(keff, clusters, parents, parent_infos, trajs, logic, stats,
                                node_info=node_info, system_name=wiki.project_name,
                                plan_only=plan_only, coverage=cov)
            add_section(wiki, keff, pages, trajs, logic)
            html = _export(wiki, kdir / "wiki", keff)
            results[keff] = {"clusters": len(clusters), "trajectories": len(trajs),
                             "requested_k": k,
                             "inferred": sum(1 for b in logic.values() if b.title),
                             "html": str(html), "dedup": dstats}
    finally:
        if llm_ctx is not None:
            tokens = llm_ctx.tokens
            await llm_ctx.__aexit__(None, None, None)
        else:
            tokens = {"input": 0, "output": 0}
    summary = {"seconds": round(time.monotonic() - t0, 1), "tokens": tokens,
               "graph": fg.stats, "base_communities": h.max_k, "roots": len(roots),
               "walks": len(walks), "per_k": results}
    if profile is not None and not profile.empty:
        summary["domain_profile"] = profile.name
    write_json(out / "summary.json", summary)
    return summary


# the template caps the content column at 900px, which squeezes wide diagrams on a large
# screen: let the column fill the window, keep prose at a readable line length
CONTENT_CSS = ".content { flex: 1; padding: 32px 48px; overflow-y: auto; max-width: 900px; }"
CONTENT_CSS_WIDE = (".content { flex: 1; padding: 32px 48px; overflow-y: auto; max-width: none; "
                    "min-width: 0; }\n.content > p, .content > ul, .content > ol, "
                    ".content > blockquote, .content > h1, .content > h2, .content > h3 "
                    "{ max-width: 1000px; }")
MERMAID_CSS = ".mermaid { margin: 16px 0; text-align: center; }"
MERMAID_CSS_SCROLL = ".mermaid { margin: 16px 0; text-align: center; overflow-x: auto; }"
# the template's showPage() ends with event.target...: the first call (no event) throws, and
# a link inside a page marks that link, not the sidebar entry. mark the entry of the page.
SHOW_ACTIVE = "  event.target.classList.add('active');"
SHOW_ACTIVE_FIX = ("  document.querySelectorAll('.nav-item').forEach(a => {\n"
                   "    if ((a.getAttribute('onclick') || '').includes(\"showPage('\" + id + \"')\"))"
                   " a.classList.add('active');\n"
                   "  });")


def patch_html(html: str) -> str:
    """the exported page with a full-width content column, scrolling wide diagrams and a
    working sidebar highlight (post-processed here, so the shared repowiki template stays
    as it is)."""
    return (html.replace(MERMAID_CSS, MERMAID_CSS_SCROLL, 1)
            .replace(CONTENT_CSS, CONTENT_CSS_WIDE, 1)
            .replace(SHOW_ACTIVE, SHOW_ACTIVE_FIX, 1))


def section_files(wiki_dir: Path, k: int) -> list[Path]:
    """the markdown files of the business-logic section for K in an exported wiki."""
    rx = re.compile(rf"^bizlogic-k{k}(-.*)?\.md$")
    return sorted(p for p in wiki_dir.glob("bizlogic-k*.md") if rx.match(p.name))


def _export(wiki: Wiki, wiki_out: Path, k: int) -> Path:
    from codocs.repowiki.export.html import export_html, load_local_mermaid
    from codocs.repowiki.export.markdown import export_markdown

    if wiki_out.is_dir():                        # pages of an earlier run of this K
        for f in section_files(wiki_out, k):
            f.unlink()
    export_markdown(wiki, wiki_out)
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", wiki.project_name).strip("-") or "wiki"
    path = wiki_out / f"{slug}-business-logic-k{k}.html"
    # the html opens on its first page: this K's overview, not the merged wiki's index
    ov = page_ids(k)["overview"]
    wiki.pages.sort(key=lambda p: p.id != ov)
    export_html(wiki, path, branding=False, mermaid_js=load_local_mermaid())
    # wide diagrams (useMaxWidth: false) scroll inside the content column instead of
    # shrinking (see patch_html)
    path.write_text(patch_html(path.read_text(encoding="utf-8")), encoding="utf-8")
    return path


# ---- re-render from stored json (no llm) ---------------------------------------------------

def _cluster_info(d: dict) -> ClusterInfo:
    names = {f.name for f in fields(ClusterInfo)}
    kw = {x: v for x, v in d.items() if x in names}
    kw["packages"] = [tuple(x) for x in kw.get("packages") or []]
    kw["subsystems"] = [tuple(x) for x in kw.get("subsystems") or []]
    return ClusterInfo(**kw)


def _trajectory(e: dict) -> Trajectory:
    """a stored trajectory. entries from older runs have own_support 0 and no step_walks:
    the diagram model falls back to `support` and the evidence bullets say "walks", not
    "walks following the whole path"."""
    names = {f.name for f in fields(Trajectory)}
    kw = {x: v for x, v in e.items() if x in names}
    kw["root_counts"] = [tuple(x) for x in kw.get("root_counts") or []]
    kw["step_functions"] = [[tuple(x) for x in s] for s in kw.get("step_functions") or []]
    kw["variants"] = [(list(s), n) for s, n in kw.get("variants") or []]
    kw["root_groups"] = [tuple(x) for x in kw.get("root_groups") or []]
    return Trajectory(**kw)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _node_info(kdir: Path, clusters: dict[int, ClusterInfo], repo: Path | None,
               profile: DomainProfile | None = None) -> dict:
    stored = _read_json(kdir / "nodes.json")
    if isinstance(stored, dict):
        return {n: tuple(v) for n, v in stored.items()}
    if repo is None:
        return {}
    graph_json = _read_json(kdir.parent / "graph.json") or {}
    gp = graph_json.get("params") or {}
    kw = {x: gp[x] for x in ("utility_fanin", "fold_accessors", "infra_callers") if x in gp}
    if "exclude" in gp:
        kw["exclude"] = tuple(gp["exclude"])
    if graph_json.get("exclude_paths"):
        kw["exclude_paths"] = tuple(graph_json["exclude_paths"])
    langs = (graph_json.get("stats") or {}).get("languages")
    if isinstance(langs, dict) and langs:        # a multi-language run: the same languages
        kw["languages"] = list(langs)
    roots = [d for d in (kdir.parent.parent / "docs",) if d.is_dir()] + [Path(repo)]
    fg = build_graph(Path(repo).resolve(), docs=DocIndex.build(roots),
                     plumbing=plumbing_rx(profile), **kw)
    info = node_info_of(fg, clusters)
    write_json(kdir / "nodes.json", {n: list(v) for n, v in info.items()})
    return info


@dataclass
class StoredResults:
    """one granularity read back from its json files."""
    k: int
    clusters: dict[int, ClusterInfo]
    parents: dict[int, int]
    parent_infos: dict[int, ClusterInfo]
    trajs: list[Trajectory]
    logic: dict[str, BusinessLogic]
    stats: dict
    coverage: list[dict] | None


def load_results(kdir: Path) -> StoredResults:
    """clusters.json, business_logic.json (either shape) and trajectories.json of
    one granularity; stats come from clusters.json, else ../summary.json."""
    kdir = Path(kdir)
    cj = _read_json(kdir / "clusters.json")
    if cj is None:
        raise FileNotFoundError(kdir / "clusters.json")
    clusters = {d["id"]: _cluster_info(d) for d in cj["clusters"]}
    parent_infos = {d["id"]: _cluster_info(d) for d in cj.get("parents") or []}
    parents = {c: p for p, info in parent_infos.items() for c in info.children}
    trajs: list[Trajectory] = []
    logic: dict[str, BusinessLogic] = {}
    for e in _read_json(kdir / "business_logic.json") or []:
        t = _trajectory(e)
        trajs.append(t)
        if e.get("logic"):
            logic[t.id] = BusinessLogic.from_dict(e["logic"], t.id, len(t.clusters))
    stats = cj.get("stats")
    if not stats:
        summary = _read_json(kdir.parent / "summary.json") or {}
        stats = {"clusters K": cj.get("k", len(clusters)),
                 "parent level": cj.get("parent_k", len(parent_infos)),
                 "business logics": len(trajs)}
        if "roots" in summary:
            stats["entry roots"] = summary["roots"]
        if "walks" in summary:
            stats["walks"] = summary["walks"]
    cov = (_read_json(kdir / "trajectories.json") or {}).get("coverage")
    return StoredResults(k=cj.get("k", len(clusters)), clusters=clusters, parents=parents,
                         parent_infos=parent_infos, trajs=trajs, logic=logic, stats=stats,
                         coverage=cov)


def render_from_json(kdir: Path, k: int, repo: Path | None = None,
                     system_name: str | None = None,
                     profile: DomainProfile | None = None) -> Path:
    """rebuild the wiki pages and html of one granularity from its stored json (no llm):
    `load_results`, function locations from nodes.json (else the graph of `repo`, else
    none) and the wiki in kdir/wiki, whose section for K is replaced. profile: the domain
    profile (None = the one the run stored, else the empty default)."""
    kdir = Path(kdir)
    if profile is None:
        profile = stored_profile(kdir.parent)
    prev = use_profile(profile)
    try:
        return _render(kdir, k, repo, system_name, profile)
    finally:
        use_profile(prev)


def _render(kdir: Path, k: int, repo: Path | None, system_name: str | None,
            profile: DomainProfile | None) -> Path:
    r = load_results(kdir)
    node_info = _node_info(kdir, r.clusters, repo, profile)
    wiki_dir = kdir / "wiki"
    if any(wiki_dir.glob("*.md")):
        for f in section_files(wiki_dir, k):
            f.unlink()
        wiki = load_wiki(wiki_dir)
    else:
        wiki = Wiki(project_name=system_name or (Path(repo).name if repo else kdir.name))
    title = f"Business Logic (K={k})"
    wiki.pages = [p for p in wiki.pages if not is_section_page(p.id, k)]
    wiki.sidebar = [s for s in wiki.sidebar if s.title != title]
    pages = build_pages(k, r.clusters, r.parents, r.parent_infos, r.trajs, r.logic, r.stats,
                        node_info=node_info, system_name=system_name or wiki.project_name,
                        plan_only=not r.logic, coverage=r.coverage)
    add_section(wiki, k, pages, r.trajs, r.logic)
    return _export(wiki, wiki_dir, k)
