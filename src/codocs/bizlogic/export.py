"""json artefacts and wiki pages for one granularity; optional merge into an existing wiki.

pages: an overview, one page per component group, per business logic and per cluster. every
diagram comes from `diagrams` (deterministic, no llm); code-level evidence is compressed into
one "Where in the code" section (the full evidence stays in business_logic.json).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from codocs.bizlogic import diagrams as G
from codocs.bizlogic.domain import PROCEDURES
from codocs.bizlogic.graph import FuncGraph
from codocs.bizlogic.infer import BusinessLogic, ClusterInfo
from codocs.bizlogic.walk import Trajectory
from codocs.repowiki.core.wiki_builder import SidebarItem, Wiki, WikiPage

_SIDEBAR_LINK = re.compile(r"^(\s*)-\s+\[(.+?)\]\((.+?)\.md\)\s*$")
_SIDEBAR_GROUP = re.compile(r"^(\s*)-\s+\*\*(.+?)\*\*\s*$")
DOC_CHARS = 120             # documentation shown per central function
ALSO_THROUGH = 20           # "also passes through this group" links
DATA_OBJECTS, DATA_LOGICS = 8, 20

INTRO = ("Business logic inferred from behaviour trajectories: random walks over the function "
         "dependency graph from entry roots, deduplicated by their sequence of clusters at this "
         "granularity. Fewer clusters give fewer, coarser business logics. Each business logic "
         "was inferred by an LLM from code evidence; diagrams are generated from that inference "
         "and the code structure, and mark what the code confirms. Read them as a map, not a "
         "specification.")


def cell(s) -> str:
    """table-cell text: html.py splits cells on every '|' and has no escape."""
    return " ".join(str(s or "").replace("|", "/").split())


def link(label: str, target: str) -> str:
    return f"[{cell(label).replace('[', '(').replace(']', ')')}]({target})"


def page_ids(k: int) -> dict[str, str]:
    return {"overview": f"bizlogic-k{k}", "cluster": f"bizlogic-k{k}-c",
            "logic": f"bizlogic-k{k}-", "group": f"bizlogic-k{k}-p"}


def node_info_of(fg: FuncGraph, clusters: dict[int, ClusterInfo]) -> dict[str, tuple]:
    """(file, line, summary) of every central function of the clusters."""
    out: dict[str, tuple] = {}
    for info in clusters.values():
        for n in info.central:
            node = fg.nodes.get(n)
            if node is not None:
                out[n] = (node.file, node.line, node.summary)
    return out


def _n(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


_BADGES = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}


def _badge(bl: BusinessLogic) -> str:
    """confidence as a coloured badge; the llm's own rating in brackets when it differs."""
    conf = _BADGES.get(bl.confidence, bl.confidence or "-")
    if bl.llm_confidence and bl.llm_confidence != bl.confidence:
        conf += f" (llm: {bl.llm_confidence})"
    return conf


def _title_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else ""


def _conf(bl: BusinessLogic) -> str:
    conf = bl.confidence or "-"
    if bl.llm_confidence and bl.llm_confidence != bl.confidence:
        conf += f" (llm: {bl.llm_confidence})"
    return conf


def _ref_point(via: str) -> str:
    """the reference-point part of an interface label: "API (REST)" -> "API"."""
    return via.split(" (", 1)[0].strip()


def build_pages(*args, **kw) -> list[WikiPage]:
    """overview, group, business-logic and cluster pages of one granularity:
    build_pages(k, clusters, parents, parent_infos, trajs, logic, stats, node_info,
    system_name, plan_only=False, coverage=None). the form
    build_pages(fg, k, clusters, parents, parent_infos, trajs, logic, assign, stats, ...)
    is also accepted (node_info is then taken from fg)."""
    if args and isinstance(args[0], FuncGraph):
        fg, k, clusters, parents, parent_infos, trajs, logic, _assign, stats, *rest = args
        kw.setdefault("node_info", node_info_of(fg, clusters))
        args = (k, clusters, parents, parent_infos, trajs, logic, stats, *rest)
    return _build_pages(*args, **kw)


def _build_pages(k: int, clusters: dict[int, ClusterInfo], parents: dict[int, int],
                 parent_infos: dict[int, ClusterInfo], trajs: list[Trajectory],
                 logic: dict[str, BusinessLogic], stats: dict,
                 node_info: dict[str, tuple] | None = None, system_name: str = "",
                 plan_only: bool = False, coverage: list[dict] | None = None,
                 model: G.Model | None = None) -> list[WikiPage]:
    ids = page_ids(k)
    m = model or G.build_model(k, clusters, parents, parent_infos, trajs, logic, system_name)
    ctx = _Ctx(m, ids, logic, plan_only, node_info or {})
    pages = [WikiPage(id=ids["overview"], title=f"Business Logic (K={k})",
                      content=_overview(ctx, stats, coverage))]
    for pid in m.children:
        pages.append(WikiPage(id=f"{ids['group']}{pid}", title=m.group_labels[pid],
                              content=_group_page(ctx, pid)))
    for lv in m.logics:
        pages.append(WikiPage(id=f"{ids['logic']}{lv.tid}", title=lv.title,
                              content=_logic_page(ctx, lv)))
    for c in sorted(m.clusters):
        pages.append(WikiPage(id=f"{ids['cluster']}{c}", title=G.cname(m, c),
                              content=_cluster_page(ctx, c)))
    return pages


class _Ctx:
    def __init__(self, m: G.Model, ids: dict[str, str], logic: dict[str, BusinessLogic],
                 plan_only: bool, node_info: dict[str, tuple]):
        self.m, self.ids, self.plan_only, self.node_info = m, ids, plan_only, node_info
        self.inferred = {tid for tid, bl in (logic or {}).items() if bl and bl.title}

    def missing(self, bl: BusinessLogic) -> str:
        return bl.error or ("plan only" if self.plan_only else "no reply")

    def title(self, lv: G.LogicView) -> str:
        return lv.bl.title or f"(not inferred: {self.missing(lv.bl)})"

    def logic_link(self, lv: G.LogicView, label: str | None = None) -> str:
        return link(label or self.title(lv), G.page_logic(self.m, lv.tid))

    def group_link(self, pid: int, label: str | None = None) -> str:
        return link(label or self.m.group_labels.get(pid, f"G{pid}"), G.page_group(self.m, pid))

    def cluster_link(self, c: int) -> str:
        return link(G.cname(self.m, c), G.page_cluster(self.m, c))


def _diagram(d, caption: str | list[str] = "", fallback: str = "") -> list[str]:
    """a fenced diagram and its caption, or the fallback line when there is no diagram."""
    if not d:
        return [fallback, ""] if fallback else []
    caps = [caption] if isinstance(caption, str) else list(caption)
    return G.fence(d) + [""] + ([*caps, ""] if any(caps) else [])


# ---- overview ----------------------------------------------------------------------------

def _overview(x: _Ctx, stats: dict, coverage: list[dict] | None) -> str:
    m = x.m
    o = [f"# Business Logic (K = {m.k})", "", INTRO, ""]
    d1, cap1 = G.context_diagram(m)
    o += ["## System context", ""] + _diagram(d1, cap1)
    d2, lines2 = G.procedure_usecases(m)
    if d2:
        o += ["## Procedures", ""] + _diagram(d2, lines2)
    o += ["## Who takes part in which group", ""]
    matrix = G.actor_matrix(m)
    if matrix:
        o += ["Business logics per group that involve each outside party: a bare number = "
              "the logic shows an exchange with it; in parentheses = only mentioned.", ""]
        o += matrix + [""]
    else:
        o += [f"No business logic was inferred{' (plan only)' if x.plan_only else ''}, so "
              "no outside party is known." if not m.inferred else
              "No outside party was identified in the inferred business logic.", ""]
        o += [f"- {x.group_link(p, f'G{p}')} = {cell(m.group_labels[p])}"
              for p in m.children] + [""]
    d3, legend = G.component_view(m)
    o += ["## Code components (clustered)", ""] + _diagram(d3, legend)
    o += G.component_table(m) + [""]
    d4, cap4 = G.call_map(m)
    o += ["## Component call map", ""] + _diagram(d4, cap4, fallback=cap4)
    o += ["## All business logic", ""]
    cats = Counter(lv.category for lv in m.logics if lv.category)
    line = f"{_n(len(m.logics), 'business logic')}"
    if cats:
        line += ": " + " · ".join(f"{c} {n}" for c, n in
                                  sorted(cats.items(), key=lambda e: (-e[1], e[0])))
    o += [line, "",
          "| # | business logic | category | group | initiated by | parties | "
          "walks (whole path) | confidence |", "|---|---|---|---|---|---|---|---|"]
    for lv in m.logics:
        o.append(f"| {lv.tid} | {x.logic_link(lv)} | {cell(lv.category)} | "
                 f"{x.group_link(lv.group, f'G{lv.group}') if lv.group >= 0 else '-'} | "
                 f"{cell(G.initiated_by(lv))} | {cell(G.parties(lv)) or '-'} | "
                 f"{lv.own_support} | {cell(_conf(lv.bl))} |")
    o.append("")
    if coverage:
        o += ["## Coverage by subsystem", "",
              "Subsystems whose roots were not sampled, or whose code no trajectory passes "
              "through, are absent from this page because of sampling, not because the code "
              "lacks the behaviour.", "",
              "| subsystem | business functions | candidate roots | roots walked | "
              "functions visited | trajectories |", "|---|---|---|---|---|---|"]
        for row in coverage:
            o.append(f"| {cell(row['subsystem'])} | {row['functions']} | "
                     f"{row['candidate_roots']} | {row['roots']} | {row['visited']} | "
                     f"{row['trajectories']} |")
        empty = [row["subsystem"] for row in coverage if not row["trajectories"]]
        if empty:
            o += ["", "No business logic passes through: " + ", ".join(empty) + "."]
        o.append("")
    o += ["## Run", "", "| item | value |", "|---|---|"]
    for key, val in (stats or {}).items():
        o.append(f"| {cell(key)} | {cell(val)} |")
    return "\n".join(o) + "\n"


# ---- group page --------------------------------------------------------------------------

def _logic_rows(x: _Ctx, lvs: list[G.LogicView]) -> list[str]:
    out = ["| # | business logic | category | initiated by | parties | walks (whole path) | "
           "confidence |", "|---|---|---|---|---|---|---|"]
    for lv in lvs:
        out.append(f"| {lv.tid} | {x.logic_link(lv)} | {cell(lv.category)} | "
                   f"{cell(G.initiated_by(lv))} | {cell(G.parties(lv)) or '-'} | "
                   f"{lv.own_support} | {cell(_conf(lv.bl))} |")
    return out


def _group_page(x: _Ctx, pid: int) -> str:
    m = x.m
    kids = m.children[pid]
    mine = sorted((lv for lv in m.logics if lv.group == pid),
                  key=lambda lv: (-lv.own_support, lv.tid))
    fns = sum(m.clusters[c].size for c in kids if c in m.clusters)
    q = [f"# {m.group_labels[pid]}", "",
         f"Component group · K = {m.k} · {_n(len(kids), 'component')} · "
         f"{_n(len(mine), 'business logic')} · {_n(fns, 'function')}", ""]
    pinfo = m.parent_infos.get(pid)
    if pinfo and pinfo.summary:
        q += [f"Summary (LLM, from the components): {' '.join(pinfo.summary.split())}", ""]
    q += ["## Use cases", ""]
    d7, cap7, internal = G.usecase_group(m, pid)
    q += _diagram(d7, cap7, fallback=cap7 if mine else "No business logic starts in this group.")
    if internal:
        q += ["### Internal mechanisms", "",
              "Business logics of this group with no outside party:", ""] + internal + [""]
    q += ["## Components and calls", ""]
    d8, cap8 = G.call_map(m, pid)
    q += _diagram(d8, cap8, fallback=cap8)
    for c in kids:
        info = m.clusters.get(c)
        pk = ", ".join(f"`{p}`" for p in G.top_packages(info, 2))
        q.append(f"- {x.cluster_link(c)}: {cell(info.kind if info else '') or 'unlabelled'}, "
                 f"{_n(info.size if info else 0, 'function')}, "
                 f"{_n(len(m.by_cluster.get(c, [])), 'business logic')}"
                 + (f", packages {pk}" if pk else ""))
    q += ["", "## Business logic", ""]
    if mine:
        q += _logic_rows(x, mine) + [""]
    else:
        q += ["No business logic has this group as its main group.", ""]
    through = [lv for lv in m.logics if lv.group != pid
               and any(m.parents.get(c, -1) == pid for c in lv.t.clusters)]
    if through:
        more = f" (+{len(through) - ALSO_THROUGH})" if len(through) > ALSO_THROUGH else ""
        q += ["Also passes through this group: " + ", ".join(
            link(lv.tid, G.page_logic(m, lv.tid)) for lv in through[:ALSO_THROUGH]) + more, ""]
    data = [lv for lv in mine if lv.bl.data_objects][:DATA_LOGICS]
    if data:
        cnt = Counter(o.name for lv in data for o in lv.bl.data_objects)
        objs = sorted(cnt, key=lambda o: (-cnt[o], o))[:DATA_OBJECTS]
        q += ["## Data used", "",
              "Business data each logic creates, reads, updates or deletes (C/R/U/D, as "
              "inferred by the LLM).", "",
              "| business logic | " + " | ".join(cell(o) for o in objs) + " |",
              "|---|" + "---|" * len(objs)]
        for lv in data:
            ops = {o.name: o.op for o in lv.bl.data_objects}
            q.append(f"| {link(f'{lv.tid} {lv.short}', G.page_logic(m, lv.tid))} | "
                     + " | ".join(cell(ops.get(o, "")) for o in objs) + " |")
        q.append("")
    return "\n".join(q) + "\n"


# ---- business-logic page -----------------------------------------------------------------

def _exchange(lv: G.LogicView, s) -> str:
    who = lv.ref(lv.party_of(s))
    actor = who.label if who else s.actor
    if not actor:
        return ""
    if s.message and s.direction in ("in", "out"):
        msg = f"`{cell(s.message)}`" + ("" if G.verify_message(s.message, lv) else "?")
        return (f" (receives {msg} from {cell(actor)})" if s.direction == "in"
                else f" (sends {msg} to {cell(actor)})")
    return f" (with {cell(actor)})"


def _logic_page(x: _Ctx, lv: G.LogicView) -> str:
    m, t, bl = x.m, lv.t, lv.bl
    roots = len(t.roots or [])
    walks = (f"{lv.own_support:,} of {t.support:,} walks follow the whole path"
             if t.step_walks else _n(t.support, "walk"))
    p = [f"# {lv.title}", "",
         "| ID | Granularity | Category | Confidence | Evidence |", "|---|---|---|---|---|",
         f"| **{lv.tid}** | K = {m.k} clusters | {cell(_title_case(lv.category)) or '-'} | "
         f"{_badge(bl)} | {walks} · {_n(roots, 'entry root')} |", ""]
    if bl.summary:
        p += ["> " + " ".join(bl.summary.split()), ""]
    if bl.error or not bl.title:
        p += [f"> Not inferred: {x.missing(bl)}", ""]
    starters = set(lv.initiator.members) | {lv.initiator.name}
    other = [a for a in lv.actors if a.evidenced and a.name not in starters]
    listed = [a for a in lv.actors if not a.evidenced and a.name not in starters]
    procs = [f"{q} ({PROCEDURES[q][0]})" for q in bl.implements if q in PROCEDURES]
    rows = [("Trigger", bl.trigger),
            ("Initiated by", G.initiated_by(lv) if bl.title else ""),
            ("Other parties", ", ".join(
                a.label + (f" ({_ref_point(a.via)})" if a.via else "") for a in other)),
            ("Also mentioned", ", ".join(a.label for a in listed)),
            ("Preconditions", "; ".join(bl.preconditions)),
            ("Outcome", bl.outcome),
            ("Group", x.group_link(lv.group) if lv.group >= 0 else "Ungrouped"),
            ("Runs in", " + ".join(lv.deployables)),
            ("Implements", ", ".join(procs)),
            ("Used during", ", ".join(bl.supports))]
    p += ["| Aspect | Description |", "|---|---|"]
    p += [f"| **{a}** | {b if a == 'Group' else cell(b)} |" for a, b in rows if b] + [""]
    d9, cap9 = G.participants(m, lv)
    if d9:
        p += ["## Who is involved", ""] + _diagram(d9, cap9)
    d10, cap10 = G.flow_diagram(m, lv)
    if not (bl.variants or t.variants):
        cap10 = cap10.replace(" See Variants.", "")
    p += ["## Flow", ""] + _diagram(d10, cap10)
    p += ["Component path (walks reaching each step): " + " → ".join(
        f"{x.cluster_link(c)} ({lv.step_walks[i]})" for i, c in enumerate(t.clusters)), ""]
    if lv.steps:
        p += ["## Steps", ""]
        for i, s in enumerate(lv.steps, start=1):
            if s.at is not None and 1 <= s.at <= len(t.clusters):
                c = t.clusters[s.at - 1]
                p.append(f"- **Step {s.at}** · {cell(s.action)}{_exchange(lv, s)} — "
                         f"{x.cluster_link(c)}")
            elif lv.mapped:
                p.append(f"- **Also** · {cell(s.action)}{_exchange(lv, s)}")
            else:
                p.append(f"- **Step {i}** · {cell(s.action)}{_exchange(lv, s)}")
        p.append("")
    if bl.variants or t.variants:
        p += ["## Variants", ""] + G.variants_lines(m, lv) + [""]
    if bl.alternatives:
        p += ["## Alternatives", ""]
        for a in bl.alternatives:
            what = f": {cell(a.result)}" if a.result else ": the logic ends here"
            p.append(f"- At step {a.at}, if {cell(a.condition)}{what}" if a.at
                     else f"- If {cell(a.condition)}{what}")
        p.append("")
    if bl.state_changes:
        d11, mentioned = G.state_logic(lv)
        p += ["## State changes", ""] + _diagram(d11, G.STATE_CAPTION)
        if mentioned:
            p += [mentioned, ""]
    rel = G.related(m, lv)
    if rel:
        p += ["## Related business logic", ""]
        p += [f"- {link(f'{o.tid} {o.title}', G.page_logic(m, o.tid))} — {cell(why)}"
              for o, why in rel] + [""]
    p += ["## Where in the code", ""] + G.where_in_code(t, lv.tid)
    return "\n".join(p) + "\n"


# ---- cluster page ------------------------------------------------------------------------

def _cluster_page(x: _Ctx, c: int) -> str:
    m = x.m
    info = m.clusters[c]
    pid = m.parents.get(c, -1)
    head = f"Component C{c} · K = {m.k} · {_n(info.size, 'function')}"
    if info.kind:
        head += f" · {info.kind}"
    if m.cluster_dep.get(c):
        head += f" · runs in {m.cluster_dep[c]}"
    head += f" · part of {x.group_link(pid)}" if pid >= 0 else ""
    q = [f"# {G.cname(m, c)}", "", head, ""]
    if info.summary:
        q += [" ".join(info.summary.split()), ""]
    if info.also_contains:
        q += ["**Also contains:** " + "; ".join(info.also_contains), ""]
    if info.subsystems:
        q += ["**Subsystems:** " + ", ".join(f"`{sub}` {round(100 * v)}%"
                                             for sub, v in info.subsystems[:8]), ""]
    if info.packages:
        q += ["**Packages:** " + ", ".join(f"`{pk}` ({n})" for pk, n in info.packages), ""]
    tids = m.by_cluster.get(c, [])
    if tids:
        q += ["## Business logic through this component", ""]
        for tid in tids:
            lv = m.logic(tid)
            q.append(f"- {link(f'{tid} {lv.title if lv else tid}', G.page_logic(m, tid))}")
        q.append("")
    d13, cap13 = G.ego_map(m, c)
    q += ["## Neighbouring components", ""] + _diagram(d13, cap13, fallback=cap13)
    q += ["## Central functions", "", "| function | location | documentation |", "|---|---|---|"]
    for n in info.central:
        file, line, summary = (list(x.node_info.get(n) or ()) + ["", 0, ""])[:3]
        loc = f"`{file}:{line}`" if file else "-"
        q.append(f"| `{cell(n.replace('::', '.'))}` | {loc} | "
                 f"{cell(G.clip(summary or '', DOC_CHARS)) or '-'} |")
    rest = info.size - len(info.central)
    if rest > 0:
        q += ["", f"… and {rest} more functions."]
    return "\n".join(q) + "\n"


# ---- wiki merge --------------------------------------------------------------------------

_FENCE = re.compile(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
_SEQ_GUARD = re.compile(r"^(\s*(?:alt|else|opt|loop|break|critical|option|par|and)\s)(.*)$")
_SEMI = re.compile(r"([#&]\w+;)|;")          # an entity stays, a bare ';' is encoded


def _semi(text: str) -> str:
    return _SEMI.sub(lambda m: m.group(1) or "#59;", text)


def _seq_semicolons(src: str) -> str:
    """a sequence diagram with ';' in message, note or guard text encoded as '#59;'
    (mermaid reads a bare ';' there as the end of the statement)."""
    lines = src.split("\n")
    if not any(ln.strip() == "sequenceDiagram" for ln in lines[:3]):
        return src
    out = []
    for ln in lines:
        g = _SEQ_GUARD.match(ln)
        if ln.lstrip().startswith("%%") or ";" not in ln:
            out.append(ln)
        elif g:
            out.append(g.group(1) + _semi(g.group(2)))
        elif ":" in ln:
            head, _, text = ln.partition(":")
            out.append(f"{head}:{_semi(text)}")
        else:
            out.append(ln)
    return "\n".join(out)


def repair_mermaid(md: str) -> str:
    """mechanical repairs of mermaid blocks in pages of a merged wiki (written by other
    tools) that mermaid 11 cannot parse; everything else is left as is."""
    return _FENCE.sub(lambda m: m.group(0).replace(m.group(1), _seq_semicolons(m.group(1))),
                      md)


def load_wiki(wiki_dir: Path) -> Wiki:
    """read a RepoWiki markdown wiki (pages + _sidebar.md) back into a Wiki; mermaid
    blocks get `repair_mermaid` (the files on disk are not changed)."""
    wiki = Wiki(project_name=wiki_dir.name)
    for md in sorted(wiki_dir.glob("*.md")):
        if md.name in ("_sidebar.md", "README.md"):
            continue
        text = repair_mermaid(md.read_text(encoding="utf-8"))
        m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        wiki.pages.append(WikiPage(id=md.stem, title=m.group(1).strip() if m else md.stem,
                                   content=text))
    sidebar = wiki_dir / "_sidebar.md"
    if sidebar.is_file():
        group: SidebarItem | None = None
        for line in sidebar.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                wiki.project_name = line[2:].strip()
                continue
            ml, mg = _SIDEBAR_LINK.match(line), _SIDEBAR_GROUP.match(line)
            if ml:
                item = SidebarItem(title=ml.group(2), page_id=ml.group(3))
                if ml.group(1) and group is not None:
                    group.children.append(item)
                else:
                    wiki.sidebar.append(item)
                    group = None
            elif mg:
                group = SidebarItem(title=mg.group(2), page_id="")
                wiki.sidebar.append(group)
    else:
        wiki.sidebar = [SidebarItem(title=p.title, page_id=p.id) for p in wiki.pages]
    # the html export opens on the first page: the wiki's index, else its first sidebar page
    first = "index" if any(p.id == "index" for p in wiki.pages) else next(
        (i.page_id for i in _sidebar_items(wiki.sidebar) if i.page_id), "")
    wiki.pages.sort(key=lambda p: p.id != first)
    return wiki


def _sidebar_items(items: list[SidebarItem]):
    for item in items:
        yield item
        yield from _sidebar_items(item.children)


def is_section_page(page_id: str, k: int) -> bool:
    """a page of the business-logic section for K (overview, group, logic or cluster)."""
    ov = page_ids(k)["overview"]
    return page_id == ov or page_id.startswith(ov + "-")


def add_section(wiki: Wiki, k: int, pages: list[WikiPage], trajs: list[Trajectory],
                logic: dict[str, BusinessLogic]) -> Wiki:
    """add the business-logic pages and a sidebar group (overview, one entry per component
    group, one per logic), replacing a section for the same K merged earlier; the overview
    goes right after the wiki's index (first when there is none). cluster pages stay out of
    the sidebar and are reached through links."""
    ids = page_ids(k)
    title = f"Business Logic (K={k})"
    wiki.pages = [p for p in wiki.pages if not is_section_page(p.id, k)]
    wiki.sidebar = [s for s in wiki.sidebar if s.title != title]
    existing = {p.id for p in wiki.pages}
    new = [p for p in pages if p.id not in existing]
    overview = [p for p in new if p.id == ids["overview"]]
    rest = [p for p in new if p.id != ids["overview"]]
    at = 1 if wiki.pages and wiki.pages[0].id == "index" else 0
    wiki.pages[at:at] = overview
    wiki.pages.extend(rest)
    group = SidebarItem(title=title, page_id="")
    group.children.append(SidebarItem(title="Overview", page_id=ids["overview"]))
    grp = re.compile(re.escape(ids["group"]) + r"-?\d+$")
    for p in pages:
        if grp.match(p.id):
            group.children.append(SidebarItem(title=p.title, page_id=p.id))
    for t in trajs:
        bl = logic.get(t.id)
        group.children.append(SidebarItem(
            title=f"{t.id} {bl.title if bl and bl.title else 'trajectory'}",
            page_id=f"{ids['logic']}{t.id}"))
    wiki.sidebar.append(group)
    return wiki


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
