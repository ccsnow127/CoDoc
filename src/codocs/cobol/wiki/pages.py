"""orchestrates LLM generation of the repo-level wiki pages for a COBOL application.

WikiGenerator builds a COBOL-only wiki on its own. The multi-language wiki (codocs.wiki's
deepdoc generator) reaches it only through the bridge functions at the end of this module:
module_page, architecture_page, overview_page and refine_plan build a WikiGenerator view
over the deepdoc generator's LLM client, cache and COBOL index, and convert pages between
deepdoc package keys and COBOL program ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from collections.abc import Callable

from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.wiki import mermaid, prompts
from codocs.cobol.wiki.grounding import CHAIN_DEPTH, Grounding, chain_tree, ground_markdown
from codocs.cobol.wiki.models import PageDoc, PagePlan, ProgramDigest, WikiPlan
from codocs.cobol.wiki.planner import hub_programs, role_label, validate_refined_plan
from codocs.repowiki.core.cache import Cache, content_hash
from codocs.repowiki.llm.client import LLMClient
from codocs.repowiki.llm.prompts import extract_json

logger = logging.getLogger(__name__)

# bump to invalidate cached pages when prompts/context format change
PROMPT_VERSION = "cobolwiki-v3"

_PAGE_MAX_TOKENS = 10_000
_PROG_CONTEXT_CAP = 6_000  # chars of digest per program
_PAGE_CONTEXT_CAP = 110_000  # chars of digest context per page
_KIND_LABEL = {"call": "CALL", "cics-link": "LINK", "cics-xctl": "XCTL", "cics-start": "START"}
_UTILITY_HINT = re.compile(r"^(IDCAMS|SORT|ICETOOL|DFSORT|SYNCSORT|IEBGENER|IEBCOPY|IEFBR14|"
                           r"IKJEFT01|IKJEFT1B|DFSRRC00|DFHCSDUP|SDSF|IEHPROGM|ICEGENER|DSNUTILB)$")


class WikiGenerator:
    """generates subsystem pages, then the architecture and overview pages."""

    def __init__(
        self,
        index: CobolRepoIndex,
        digests: dict[str, ProgramDigest],
        llm: LLMClient,
        cache: Cache,
        language: str = "en",
        concurrency: int = 5,
        on_progress: Callable[[str], None] | None = None,
    ):
        self.index = index
        self.digests = digests
        self.llm = llm
        self.cache = cache
        self.language = language
        self._sem = asyncio.Semaphore(concurrency)
        self._on_progress = on_progress
        self._page_of: dict[str, str] = {}
        self._page_titles: dict[str, str] = {}
        self._stores = _program_stores(index)
        self._pg = index.program_graph()
        self._hubs = hub_programs(index)
        self._grounding: Grounding | None = None

    def _progress(self, msg: str) -> None:
        if self._on_progress:
            self._on_progress(msg)

    def _key(self, kind: str, payload: str) -> str:
        model = getattr(self.llm, "model", "")
        return f"cobolwiki:{kind}:" + content_hash(PROMPT_VERSION + model + self.language + payload)

    def purpose(self, pid: str, limit: int = 160) -> str:
        d = self.digests.get(pid)
        if d is None:
            return ""
        text = d.purpose or (d.entries[0].summary if d.entries else "")
        return " ".join(text.split())[:limit]

    # -- plan refinement --------------------------------------------------

    async def refine_plan(self, skeleton: WikiPlan) -> WikiPlan:
        self._progress("Refining wiki plan...")
        # slim the skeleton: the LLM names pages, it must not echo hundreds of program ids
        slim = skeleton.model_dump()
        for section in slim["sections"]:
            for page in section["pages"]:
                pids = page.pop("programs")
                page["program_count"] = len(pids)
                page["sample_programs"] = pids[:8]
        skeleton_json = json.dumps(slim, indent=1)
        facts = self._plan_facts(skeleton)
        cache_key = self._key("plan", skeleton_json + facts)
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return validate_refined_plan(skeleton, WikiPlan(**cached))
            except Exception:
                pass

        raw = await self._complete(
            prompts.build_plan_refine_prompt(skeleton_json, facts, self.language),
            max_tokens=16_000,
        )
        data = extract_json(raw)
        if not isinstance(data, dict):
            logger.warning("plan refinement returned unparseable JSON; keeping skeleton. "
                           "Tail: %r", raw[-300:])
            return skeleton
        try:
            refined = WikiPlan(**data)
        except Exception as e:
            logger.warning("plan refinement schema mismatch (%s); keeping skeleton", e)
            return skeleton
        refined = validate_refined_plan(skeleton, refined)
        if refined is not skeleton:
            await self.cache.put(cache_key, refined.model_dump())
        return refined

    def _plan_facts(self, plan: WikiPlan) -> str:
        idx = self.index
        lines = [self._counts_line()]
        lines.append("Technology: " + "; ".join(f"{t} ({ev})" for t, _r, ev in tech_rows(idx)))
        page_of = {p: pg.id for pg in plan.all_pages() for p in pg.programs}
        lines.append("\nPrograms (page id | program [type]: purpose | started by):")
        for pid in sorted(idx.programs)[:300]:
            starts = [f"txn {t}" for t in idx.transactions_of(pid)[:3]]
            starts += [f"job {j}" for j in idx.jobs_running(pid)[:3]]
            lines.append(
                f"- {page_of.get(pid, '?')} | {pid} [{role_label(idx, pid)}]: {self.purpose(pid)}"
                + (f" | {', '.join(starts)}" if starts else "")
            )
        if len(idx.programs) > 300:
            lines.append(f"- ... and {len(idx.programs) - 300} more programs")
        jobs = [j for j in idx.jobs.values() if j.kind == "job" and j.description]
        if jobs:
            lines.append("\nJCL job descriptions:")
            lines.extend(f"- {j.name}: {j.description[:120]}" for j in jobs[:40])
        return "\n".join(lines)

    # -- page generation --------------------------------------------------

    async def generate(self, plan: WikiPlan, only_pages: list[str] | None = None) -> list[PageDoc]:
        """all subsystem pages (or just *only_pages*, for smoke runs), then system pages."""
        self._bind_plan(plan)
        pages = plan.all_pages()
        if only_pages is not None:
            wanted = set(only_pages)
            pages = [p for p in pages if p.id in wanted]
        self._progress(f"Generating {len(pages)} subsystem pages...")
        done = 0

        async def _one(p: PagePlan) -> PageDoc:
            nonlocal done
            async with self._sem:
                doc = await self._module_page(p, plan.project_summary)
            done += 1
            self._progress(f"Subsystem pages: {done}/{len(pages)}")
            return doc

        module_docs = list(await asyncio.gather(*(_one(p) for p in pages)))

        self._progress("Generating architecture page...")
        arch = await self._architecture_page(module_docs)
        self._progress("Generating overview page...")
        overview = await self._overview_page(plan, module_docs)
        return [overview, arch, *module_docs]

    def _bind_plan(self, plan: WikiPlan) -> None:
        self._page_of = {p: pg.id for pg in plan.all_pages() for p in pg.programs}
        self._page_titles = {pg.id: pg.title for pg in plan.all_pages()}
        self._grounding = Grounding(
            self.index,
            {pg.id: list(pg.programs) for pg in plan.all_pages()},
            dict(self._page_titles),
        )

    def grounding(self) -> Grounding:
        if self._grounding is None:
            pages: dict[str, list[str]] = {}
            for p, page in self._page_of.items():
                pages.setdefault(page, []).append(p)
            self._grounding = Grounding(self.index, pages, dict(self._page_titles))
        return self._grounding

    def _grounded(self, doc: PageDoc) -> PageDoc:
        """*doc* with its diagrams checked against the current index. The cache keeps the
        model's page as written, so validator improvements apply to cached pages too."""
        markdown, notes = self.ground(doc.markdown, doc.id)
        return doc.model_copy(update={"markdown": markdown, "grounding": notes})

    def ground(self, markdown: str, page_id: str = "") -> tuple[str, list[str]]:
        """drop diagram edges / messages the index does not support (logged)."""
        fixed, notes = ground_markdown(markdown, self.grounding())
        if notes:
            logger.info("page %s: %d unsupported diagram edges removed or relabeled: %s",
                        page_id or "?", len(notes), "; ".join(notes[:8]))
        return fixed, notes

    async def _module_page(self, page: PagePlan, project_summary: str) -> PageDoc:
        context = self.page_context(page)
        cache_key = self._key(
            f"page:{page.id}",
            page.title + page.description + "\n".join(page.workflows) + project_summary + context,
        )
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return self._grounded(PageDoc(**cached))
            except Exception:
                pass

        messages = prompts.build_module_page_prompt(
            page.title, page.description, page.workflows, context, project_summary, self.language
        )
        markdown = await self._complete_page(messages)
        doc = PageDoc(
            id=page.id, title=page.title, markdown=markdown, summary=_first_paragraph(markdown)
        )
        await self.cache.put(cache_key, doc.model_dump())
        return self._grounded(doc)

    def page_context(self, page: PagePlan) -> str:
        """facts and per-program digests backing one subsystem page."""
        if not self._page_of:
            self._page_of = {p: page.id for p in page.programs}
        pids = [p for p in page.programs if p in self.index.programs]
        parts: list[str] = []

        cap = _PROG_CONTEXT_CAP
        blocks = [self.program_block(p, cap) for p in pids]
        if sum(len(b) for b in blocks) > _PAGE_CONTEXT_CAP:
            cap = max(600, _PAGE_CONTEXT_CAP // max(1, len(pids)) - 800)
            blocks = [self.program_block(p, cap) for p in pids]
        parts.extend(blocks)

        members = set(pids)
        pg = self._pg
        internal = [
            (u, v, d) for u, v, d in pg.edges(data=True) if u in members and v in members
        ]
        if internal:
            parts.append(
                "### Program edges inside this page (caller -> callee [kind x count])\n"
                + "\n".join(f"- {u} -> {v} [{_kinds(d)}]" for u, v, d in sorted(internal)[:200])
            )
        outbound = [(u, v, d) for u, v, d in pg.edges(data=True) if u in members and v not in members]
        if outbound:
            parts.append(
                "### Outbound transfers (to programs documented on other pages)\n"
                + "\n".join(
                    f"- {u} -> {v} [{_kinds(d)}] (page {self._page_of.get(v, '?')})"
                    for u, v, d in sorted(outbound)[:80]
                )
            )
        inbound = [(u, v, d) for u, v, d in pg.edges(data=True) if v in members and u not in members]
        if inbound:
            parts.append(
                "### Inbound transfers (programs elsewhere that call into this page)\n"
                + "\n".join(
                    f"- {v} <- {u} [{_kinds(d)}] (page {self._page_of.get(u, '?')})"
                    for u, v, d in sorted(inbound)[:80]
                )
            )
        parts.extend(x for x in (self._jobs_block(pids), self._stores_block(pids)) if x)
        chains = self.call_chains(roots=pids, max_lines=40, max_paths=6)
        if chains:
            parts.append("### Call chains from this page's entry programs\n" + chains)
        parts.append(
            "### Verified diagram skeleton (base the Architecture diagram on it)\n"
            f"```mermaid\n{self.page_mermaid(page)}\n```"
        )
        return "\n\n".join(parts)

    def program_block(self, pid: str, cap: int) -> str:
        idx = self.index
        prog = idx.programs[pid]
        digest = self.digests.get(pid)
        head = (
            f"### program {pid} — {role_label(idx, pid)}; `{prog.path}`; {prog.loc} lines, "
            f"{len(prog.paragraphs)} paragraphs"
        )
        lines = [head]
        if digest and digest.purpose:
            lines.append(f"Purpose: {digest.purpose}")
        facts: list[tuple[str, list[str]]] = [
            ("Started by transactions", idx.transactions_of(pid)),
            ("Run by JCL jobs", idx.jobs_running(pid)),
            ("Called by", [f"{c} ({k})" for c, k in self._edge_kinds(pid, inbound=True)]),
            ("Transfers control to", [f"{c} ({k})" for c, k in self._edge_kinds(pid)]),
            ("External / unresolved calls", sorted({
                c.target for c in prog.calls if not c.resolved and c.kind != "cics-start"
            })),
            ("Data stores", [f"{s} ({m})" for s, m in self._stores.get(pid, [])]),
            ("Native files (SELECT -> ASSIGN)", [
                f"{f.name} -> {f.assign or '?'} {f.organization} {'/'.join(f.modes)}".strip()
                for f in prog.files if f.via == "native"
            ]),
            ("SQL tables", sorted({t for s in prog.sql for t in s.tables})),
            ("BMS maps", list(prog.maps)),
            ("Copybooks", list(prog.copybooks)),
            ("Linkage (PROCEDURE DIVISION USING)", list(prog.using_params)),
        ]
        for label, values in facts:
            if values:
                shown = values[:25]
                more = f", ... (+{len(values) - 25})" if len(values) > 25 else ""
                lines.append(f"- {label}: {', '.join(shown)}{more}")
        used, dropped = 0, 0
        for e in digest.entries if digest else []:
            line = f"- {e.name} ({e.kind}): {e.summary}" if e.summary else f"- {e.name} ({e.kind})"
            if used + len(line) > cap:
                dropped += 1
                continue
            used += len(line)
            lines.append(line)
        if dropped:
            lines.append(f"- ... ({dropped} more items omitted)")
        return "\n".join(lines)

    def _edge_kinds(self, pid: str, inbound: bool = False) -> list[tuple[str, str]]:
        pg = self._pg
        if pid not in pg:
            return []
        edges = pg.in_edges(pid, data=True) if inbound else pg.out_edges(pid, data=True)
        return sorted((u if inbound else v, _kinds(d)) for u, v, d in edges)

    def _jobs_block(self, pids: list[str]) -> str:
        members = set(pids)
        lines: list[str] = []
        for name, job in sorted(self.index.jobs.items()):
            if not any(s.app_program in members for s in job.steps):
                continue
            steps = "; ".join(
                f"{s.name}: {s.app_program or s.program or s.proc}" for s in job.steps[:12]
            )
            desc = f" — {job.description[:120]}" if job.description else ""
            lines.append(f"- {job.kind} {name}{desc}. Steps: {steps}")
        txns = [
            f"- {t} -> {p}" for t, p in sorted(self.index.transactions.items()) if p in members
        ]
        out = []
        if lines:
            out.append("### JCL jobs running these programs\n" + "\n".join(lines[:40]))
        if txns:
            out.append("### CICS transactions starting these programs\n" + "\n".join(txns[:60]))
        return "\n\n".join(out)

    def _stores_block(self, pids: list[str]) -> str:
        members = set(pids)
        lines: list[str] = []
        for name, ds in sorted(self.index.datastores.items()):
            readers = [p for p in ds.readers if p in members]
            writers = [p for p in ds.writers if p in members]
            if not readers and not writers:
                continue
            others = len((set(ds.readers) | set(ds.writers)) - members)
            alias = f" aka {', '.join(ds.aliases[:4])}" if ds.aliases else ""
            org = f", {ds.organization}" if ds.organization else ""
            lines.append(
                f"- {name}{alias} [{ds.kind}{org}]: read by {', '.join(readers) or '-'}; "
                f"written by {', '.join(writers) or '-'}; also used by {others} programs elsewhere"
            )
        if not lines:
            return ""
        return "### Data stores used by this page\n" + "\n".join(lines[:60])

    # -- deterministic diagrams ---------------------------------------------

    def page_mermaid(self, page: PagePlan, max_nodes: int = 25) -> str:
        """verified flowchart skeleton: programs, their real edges, starters and stores."""
        idx = self.index
        members = [p for p in page.programs if p in idx.programs]
        mset = set(members)
        pg = self._pg
        lines = ["flowchart TD", '  subgraph page["' + _label(page.title) + '"]']
        lines += [f'    {_mid(p)}["{p}"]' for p in members]
        lines.append("  end")
        budget = max(0, max_nodes - len(members))
        edges: list[str] = []
        for u, v, d in sorted(pg.edges(data=True)):
            if u in mset and v in mset:
                edges.append(f"  {_mid(u)} -->|{_kinds(d, counts=False)}| {_mid(v)}")
        external = sorted({
            (v if u in mset else u)
            for u, v in pg.edges if (u in mset) != (v in mset)
        })[: min(6, budget)]
        budget -= len(external)
        if external:
            lines.append('  subgraph other["other subsystems"]')
            lines += [f'    {_mid(p)}["{p}"]' for p in external]
            lines.append("  end")
            ext = set(external)
            for u, v, d in sorted(pg.edges(data=True)):
                if (u in mset and v in ext) or (u in ext and v in mset):
                    edges.append(f"  {_mid(u)} -->|{_kinds(d, counts=False)}| {_mid(v)}")
        starters = [(f"txn_{t}", f"TXN {t}", p, "starts")
                    for t, p in sorted(idx.transactions.items()) if p in mset]
        for name, job in sorted(idx.jobs.items()):
            for p in sorted({s.app_program for s in job.steps if s.app_program in mset}):
                starters.append((f"job_{name}", f"JOB {name}", p, "runs"))
        nodes_added: set[str] = set()
        for nid, label, p, verb in starters:
            if nid not in nodes_added:
                if budget <= 0:
                    continue
                budget -= 1
                nodes_added.add(nid)
                lines.append(f'  {_mid(nid)}(["{_label(label)}"])')
            edges.append(f"  {_mid(nid)} -->|{verb}| {_mid(p)}")
        for store in self.rank_page_stores(members)[: max(0, budget)]:
            sid = _mid("ds_" + store)
            kind = idx.datastores[store].kind if store in idx.datastores else "data"
            lines.append(f'  {sid}[("{_label(_short_store(store))} ({kind})")]')
            for p in members:
                for s, mode in self._stores.get(p, []):
                    if s != store:
                        continue
                    if "w" in mode:
                        edges.append(f"  {_mid(p)} -->|writes| {sid}")
                    if "r" in mode:
                        edges.append(f"  {sid} -->|read by| {_mid(p)}")
        return "\n".join(lines + edges)

    def rank_page_stores(self, members: list[str]) -> list[str]:
        """stores a page's programs touch, most important first: those the page writes, then
        those shared with other pages, then by how many page programs touch them, then name."""
        mset = set(members)
        written: set[str] = set()
        touched: Counter = Counter()
        for p in members:
            for store, mode in self._stores.get(p, []):
                touched[store] += 1
                if "w" in mode:
                    written.add(store)

        def shared(store: str) -> bool:
            ds = self.index.datastores.get(store)
            if ds is None:
                return False
            return any(p not in mset for p in set(ds.readers) | set(ds.writers))

        return sorted(touched, key=lambda s: (s not in written, not shared(s), -touched[s], s))

    def _hub_pages(self) -> set[str]:
        """pages that hold only shared-service hubs (the planner's 'Shared services')."""
        pages: dict[str, list[str]] = {}
        for p, page in self._page_of.items():
            pages.setdefault(page, []).append(p)
        return {pid for pid, ps in pages.items() if ps and all(p in self._hubs for p in ps)}

    def group_mermaid(self, max_edges: int = 40, max_stores: int = 6, min_pages: int = 2) -> str:
        """deterministic mermaid skeleton of subsystem-level edges, starters and shared stores.

        Edges into the shared-services page are drawn dotted and ranked last: every page
        logs through the hub, which says nothing about how the subsystems fit together."""
        idx = self.index
        page_of = self._page_of
        titles = self._page_titles
        hub_pages = self._hub_pages()
        pair: dict[tuple[str, str], Counter] = {}
        for u, v, d in self._pg.edges(data=True):
            pu, pv = page_of.get(u), page_of.get(v)
            if pu and pv and pu != pv:
                pair.setdefault((pu, pv), Counter()).update(
                    {_KIND_LABEL.get(k, k): d.get("count", 1) for k in d.get("kinds", [d["kind"]])}
                )
        nodes: set[str] = set()
        lines = ["flowchart TD"]

        def node(pid: str) -> str:
            if pid not in nodes:
                nodes.add(pid)
                return f'{_mid(pid)}["{_label(titles.get(pid, pid))}"]'
            return _mid(pid)

        ranked = sorted(
            pair.items(), key=lambda kv: (kv[0][1] in hub_pages, -sum(kv[1].values()), kv[0])
        )[:max_edges]
        for (s, d), kinds in ranked:
            label = " ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
            arrow = "-.->" if d in hub_pages else "-->"
            lines.append(f"  {node(s)} {arrow}|{label}| {node(d)}")
        txn_pages = Counter(page_of[p] for p in repo_transactions(idx).values() if p in page_of)
        job_pages = Counter()
        for job in idx.jobs.values():
            for p in {s.app_program for s in job.steps if s.app_program in page_of}:
                job_pages[page_of[p]] += 1
        if txn_pages:
            lines.append('  entry_cics(["CICS transactions"])')
            for pid, n in sorted(txn_pages.items()):
                lines.append(f"  entry_cics -->|starts {n}| {node(pid)}")
        if job_pages:
            lines.append('  entry_jcl(["JCL jobs"])')
            for pid, n in sorted(job_pages.items()):
                lines.append(f"  entry_jcl -->|runs {n}| {node(pid)}")
        # stores touched by several subsystems show how online and batch share data
        shared: list[tuple[int, int, str, set[str]]] = []
        for name, ds in idx.datastores.items():
            users = set(ds.readers) | set(ds.writers)
            pages = {page_of[p] for p in users if p in page_of}
            if len(pages) >= min_pages:
                shared.append((len(pages), len(users), name, pages))
        for _n, _u, name, pages in sorted(shared, key=lambda x: (-x[0], -x[1], x[2]))[:max_stores]:
            sid = _mid("ds_" + name)
            lines.append(f'  {sid}[("{_label(_short_store(name))}")]')
            ds = idx.datastores[name]
            for pid in sorted(pages):
                writes = any(page_of.get(p) == pid for p in ds.writers)
                lines.append(f"  {node(pid)} -.->|{'writes' if writes else 'reads'}| {sid}")
        if len(lines) == 1:
            for pid in titles:
                lines.append(f"  {node(pid)}")
        return "\n".join(lines)

    def overview_mermaid(self) -> str:
        """skeleton for the overview's 'System at a Glance': subsystem edges, entry points and
        the main data stores with the pages that read / write them."""
        return self.group_mermaid(max_edges=30, max_stores=10, min_pages=1)

    def store_access_facts(self, limit: int = 15) -> str:
        """verified page / job / transaction -> store access for the most used stores."""
        idx = self.index
        txn_of: dict[str, list[str]] = {}
        for t, p in repo_transactions(idx).items():
            txn_of.setdefault(p, []).append(t)
        stores = sorted(
            idx.datastores.values(), key=lambda d: (-(len(d.readers) + len(d.writers)), d.name)
        )[:limit]
        rows = []
        for ds in stores:
            parts = []
            for verb, progs in (("written", ds.writers), ("read", ds.readers)):
                if not progs:
                    continue
                pages = sorted({self._page_of[p] for p in progs if p in self._page_of})
                jobs = sorted({j for p in progs for j in idx.jobs_running(p)})
                txns = sorted({t for p in progs for t in txn_of.get(p, [])})
                bits = [f"programs {', '.join(sorted(progs)[:8])}"]
                if pages:
                    bits.append(f"pages {', '.join(pages[:6])}")
                if jobs:
                    bits.append(f"jobs {', '.join(jobs[:8])}")
                if txns:
                    bits.append(f"transactions {', '.join(txns[:8])}")
                parts.append(f"{verb} by " + "; ".join(bits))
            alias = f" aka {', '.join(ds.aliases[:3])}" if ds.aliases else ""
            org = f", {ds.organization}" if ds.organization else ""
            rows.append(f"- {ds.name}{alias} [{ds.kind}{org}]: " + " | ".join(parts or ["-"]))
        return "\n".join(rows)

    def _io_tag(self, pid: str, limit: int = 4) -> str:
        """compact ' [w STORE, r STORE]' of a program's direct data access."""
        stores = sorted(self._stores.get(pid, []), key=lambda sm: ("w" not in sm[1], sm[0]))
        if not stores:
            return ""
        shown = [f"{'rw' if m == 'rw' else m} {_short_store(s)}" for s, m in stores[:limit]]
        more = f", +{len(stores) - limit}" if len(stores) > limit else ""
        return f" [{', '.join(shown)}{more}]"

    def call_chains(self, roots: list[str] | None = None, max_lines: int = 140,
                    max_paths: int = 12) -> str:
        """call / LINK / XCTL chains reachable from transaction and job entry programs
        (BFS tree, depth CHAIN_DEPTH), with each program's direct I/O."""
        idx = self.index
        starters: dict[str, list[str]] = {}
        for t, p in sorted(repo_transactions(idx).items()):
            starters.setdefault(p, []).append(f"txn {t}")
        for name, job in sorted(idx.jobs.items()):
            if job.kind != "job":
                continue
            for p in sorted({s.app_program for s in job.steps if s.app_program in idx.programs}):
                starters.setdefault(p, []).append(f"job {name}")
        for p in idx.entrypoints()["standalone"]:
            starters.setdefault(p, []).append("main program")
        order = [p for p in starters if roots is None or p in roots]
        # entry programs and programs whose sub-chain was already written out are not
        # expanded again: a shared menu or a common LINK target is described once
        stop = set(starters)
        shown: set[str] = set()
        hubs = self._hubs
        lines: list[str] = []
        for root in order:
            if len(lines) >= max_lines:
                lines.append(f"- ... ({len(order) - order.index(root)} more entry programs)")
                break
            who = starters[root]
            head = ", ".join(who[:4]) + (f" (+{len(who) - 4})" if len(who) > 4 else "")
            lines.append(f"- {head} -> {root}{self._io_tag(root)}")
            # longest chains first: they carry the LINK hops sequence diagrams tend to skip
            paths = sorted(
                (p for p in chain_tree(self._pg, root, CHAIN_DEPTH, hubs, stop) if len(p) > 1),
                key=lambda p: (-len(p), p),
            )
            for path in paths[:max_paths]:
                hops = [root]
                for u, v in zip(path, path[1:], strict=False):
                    kind = _kinds(self._pg.edges[u, v], counts=False)
                    if v in shown:
                        hops.append(f"-{kind}-> {v} (continues as above)")
                    elif v in stop:
                        hops.append(f"-{kind}-> {v} (see its chain)")
                    else:
                        hops.append(f"-{kind}-> {v}{self._io_tag(v)}")
                lines.append("  - " + " ".join(hops))
            expanded = {p for path in paths[:max_paths] for p in path[1:-1]}
            shown |= expanded
            stop |= expanded
            if len(paths) > max_paths:
                lines.append(f"  - ... (+{len(paths) - max_paths} more branches)")
        if not lines:
            return ""
        out = [
            "Call / LINK / XCTL chains from entry programs (BFS, up to "
            f"{CHAIN_DEPTH} hops; [w X] / [r X] = the program itself writes / reads X). A "
            "program only transfers control to the next program on its chain:",
            *lines,
        ]
        used = [h for h in sorted(hubs) if h in self._pg]
        if used:
            out.append(
                "Shared services left out of the chains above (called from most programs): "
                + "; ".join(
                    f"{h}{self._io_tag(h)} called by {self._pg.in_degree(h)} programs"
                    for h in used
                )
            )
        return "\n".join(out)

    # -- system-level pages ------------------------------------------------

    async def _architecture_page(self, module_docs: list[PageDoc]) -> PageDoc:
        facts = self.system_facts()
        base = self.group_mermaid()
        summaries = _page_summaries(module_docs)
        cache_key = self._key("arch", facts + base + summaries)
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return self._grounded(PageDoc(**cached))
            except Exception:
                pass
        messages = prompts.build_architecture_page_prompt(facts, summaries, base, self.language)
        markdown = await self._complete_page(messages)
        doc = PageDoc(
            id="architecture", title="Architecture", markdown=markdown,
            summary=_first_paragraph(markdown),
        )
        await self.cache.put(cache_key, doc.model_dump())
        return self._grounded(doc)

    async def _overview_page(self, plan: WikiPlan, module_docs: list[PageDoc]) -> PageDoc:
        facts = self.system_facts()
        tech = tech_table(self.index)
        summaries = _page_summaries(module_docs)
        generated = {d.id for d in module_docs}
        toc_lines = []
        for section in plan.sections:
            pages = [p for p in section.pages if p.id in generated]
            if pages:
                toc_lines.append(f"- {section.title}")
                toc_lines.extend(f"  - [[page:{p.id}]] {p.title}" for p in pages)
        refs = ["[[page:program-index]] Program Index"]
        if self.index.copybooks:
            refs.append("[[page:copybook-index]] Copybook Index")
        if any(j.kind == "job" for j in self.index.jobs.values()):
            refs.append("[[page:batch-jobs]] Batch Jobs")
        if self.index.datastores:
            refs.append("[[page:data-stores]] Data Stores")
        toc = (
            "Architecture page: [[page:architecture]]\n" + "\n".join(toc_lines)
            + "\nReference pages: " + ", ".join(refs)
        )
        if any(d.source == "leafdoc" for d in self.digests.values()):
            toc += "\nPer-program reference docs: link any program with [[prog:NAME]]"
        skeleton = self.overview_mermaid()
        access = self.store_access_facts()
        cache_key = self._key("overview", facts + tech + summaries + toc + skeleton + access)
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return self._grounded(PageDoc(**cached))
            except Exception:
                pass
        messages = prompts.build_overview_page_prompt(
            facts, tech, summaries, toc, self.language, skeleton=skeleton, store_access=access
        )
        markdown = await self._complete_page(messages)
        doc = PageDoc(
            id="index", title="Overview", markdown=markdown, summary=_first_paragraph(markdown)
        )
        await self.cache.put(cache_key, doc.model_dump())
        return self._grounded(doc)

    def _counts_line(self) -> str:
        s = self.index.summary()
        procs = sum(1 for j in self.index.jobs.values() if j.kind == "proc")
        return (
            f"Repository `{self.index.name}`: {s.programs} COBOL programs, {s.copybooks} "
            f"copybooks, {s.jcl_jobs} JCL jobs ({procs} procs), {s.bms_mapsets} BMS mapsets, "
            f"{s.cobol_loc:,} lines of COBOL (programs + copybooks), {s.call_edges} "
            "program-to-program edges."
        )

    def system_facts(self) -> str:
        idx = self.index
        parts = [self._counts_line()]
        parts.append("Technology:\n" + "\n".join(
            f"- {t} — {r}: {ev}" for t, r, ev in tech_rows(idx)
        ))
        entry = idx.entrypoints()
        txns = repo_transactions(idx)
        if txns:
            rows = [
                f"- {t} -> {p} ({self._page_of.get(p, '?')}): {self.purpose(p, 90)}"
                for t, p in sorted(txns.items())
            ]
            parts.append(f"CICS transactions ({len(rows)}), transid -> program (page):\n"
                         + "\n".join(rows[:60]))
        jobs = [j for _n, j in sorted(idx.jobs.items()) if j.kind == "job"]
        if jobs:
            rows = []
            for j in jobs[:60]:
                progs = [s.app_program for s in j.steps if s.app_program]
                utils = sorted({s.program for s in j.steps if s.program and not s.app_program})
                desc = f" — {j.description[:100]}" if j.description else ""
                rows.append(
                    f"- {j.name}{desc}: programs {', '.join(progs) or '-'}"
                    + (f"; utilities {', '.join(utils)}" if utils else "")
                )
            more = f"\n- ... and {len(jobs) - 60} more jobs" if len(jobs) > 60 else ""
            parts.append(f"JCL jobs ({len(jobs)}):\n" + "\n".join(rows) + more)
        online_only = [p for p in entry["cics"] if not idx.transactions_of(p)]
        if online_only:
            parts.append("CICS programs with no known transaction or caller: "
                         + ", ".join(online_only[:40]))
        if entry["standalone"]:
            parts.append("Standalone main programs (no JCL in repo): "
                         + ", ".join(f"{p} ({self.purpose(p, 80)})" for p in entry["standalone"][:30]))
        stores = sorted(
            idx.datastores.values(), key=lambda d: (-(len(d.readers) + len(d.writers)), d.name)
        )
        if stores:
            rows = []
            for ds in stores[:30]:
                alias = f" aka {', '.join(ds.aliases[:3])}" if ds.aliases else ""
                org = f", {ds.organization}" if ds.organization else ""
                rows.append(
                    f"- {ds.name}{alias} [{ds.kind}{org}]: written by "
                    f"{', '.join(ds.writers[:8]) or '-'}; read by {', '.join(ds.readers[:10]) or '-'}"
                    + (f"; jobs {', '.join(ds.jobs[:6])}" if ds.jobs else "")
                )
            parts.append(f"Data stores ({len(stores)}, most used first):\n" + "\n".join(rows))
        core = idx.rank_programs()[:15]
        parts.append("Most depended-upon programs (PageRank):\n" + "\n".join(
            f"- {p} [{role_label(idx, p)}]: {self.purpose(p, 100)}" for p, _s in core
        ))
        chains = self.call_chains()
        if chains:
            parts.append(chains)
        return "\n\n".join(parts)

    # -- LLM helpers -------------------------------------------------------

    async def _complete(self, messages: list[dict], max_tokens: int = 4096) -> str:
        raw = ""
        for attempt in (1, 2):
            raw = await self.llm.complete(messages, max_tokens=max_tokens)
            if not raw.startswith("[LLM Error"):
                return raw
            logger.warning("LLM call failed (attempt %d): %s", attempt, raw[:200])
            await asyncio.sleep(3 * attempt)
        raise RuntimeError(f"LLM call failed twice: {raw[:300]}")

    async def _complete_page(self, messages: list[dict]) -> str:
        markdown = await self._complete(messages, max_tokens=_PAGE_MAX_TOKENS)
        markdown = _strip_outer_fence(markdown)
        markdown, problems = mermaid.sanitize_page(markdown)
        if problems:
            logger.info("mermaid problems, requesting repair: %s", problems[:5])
            repaired = await self._complete(
                prompts.build_mermaid_repair_prompt(markdown, problems, self.language),
                max_tokens=_PAGE_MAX_TOKENS,
            )
            repaired = _strip_outer_fence(repaired)
            repaired, remaining = mermaid.sanitize_page(repaired)
            if not remaining:
                return repaired
            logger.warning("mermaid still invalid after repair: %s", remaining[:5])
        return markdown


# -- deterministic facts ------------------------------------------------------


def _program_stores(index: CobolRepoIndex) -> dict[str, list[tuple[str, str]]]:
    """program id -> [(store, 'r' | 'w' | 'rw')] from the data store readers / writers."""
    modes: dict[str, dict[str, str]] = {}
    for name, ds in index.datastores.items():
        for p in ds.readers:
            modes.setdefault(p, {})[name] = modes.get(p, {}).get(name, "") + "r"
        for p in ds.writers:
            m = modes.setdefault(p, {}).get(name, "")
            if "w" not in m:
                modes[p][name] = m + "w"
    return {p: sorted(d.items()) for p, d in modes.items()}


def repo_transactions(index: CobolRepoIndex) -> dict[str, str]:
    """transid -> program, only for programs in this repo (drops DFHMIRS mirrors etc.)."""
    return {t: p for t, p in index.transactions.items() if p in index.programs}


def tech_rows(index: CobolRepoIndex) -> list[tuple[str, str, str]]:
    """(technology, role, evidence) rows detected from static analysis only."""
    progs = list(index.programs.values())
    rows: list[tuple[str, str, str]] = []
    n_free = sum(1 for p in progs if p.format == "free")
    fmt = f"{len(progs) - n_free} fixed-format, {n_free} free-format" if n_free else "fixed format"
    rows.append(("COBOL", "application language",
                 f"{len(progs)} programs ({fmt}), {len(index.copybooks)} copybooks"))
    cics = [p for p in progs if p.has_cics]
    if cics:
        rows.append(("CICS", "online transaction processing",
                     f"{len(cics)} programs use EXEC CICS; {len(repo_transactions(index))} "
                     "transactions start repo programs"))
    if index.mapsets:
        users = sum(1 for p in progs if p.maps)
        rows.append(("BMS", "3270 screen maps",
                     f"{len(index.mapsets)} mapsets, sent/received by {users} programs"))
    sql = [p for p in progs if p.has_sql]
    if sql:
        tables = {t for p in sql for s in p.sql for t in s.tables}
        rows.append(("DB2", "relational database (embedded SQL)",
                     f"{len(sql)} programs with EXEC SQL; {len(tables)} tables"))
    dli = [p for p in progs if p.has_dli]
    if dli:
        rows.append(("IMS DB (DL/I)", "hierarchical database", f"{len(dli)} programs issue DL/I calls"))
    vsam = [d for d in index.datastores.values() if _is_vsam(d, _cics_files(index))]
    if vsam:
        rows.append(("VSAM", "indexed / keyed files", f"{len(vsam)} VSAM / indexed data stores"))
    seq = [d for d in index.datastores.values() if d.kind == "dataset" and d not in vsam]
    if seq:
        rows.append(("Sequential datasets", "batch input / output files",
                     f"{len(seq)} sequential or unspecified-organization datasets"))
    queues = [d for d in index.datastores.values() if d.kind == "queue"]
    if queues:
        rows.append(("CICS TS/TD queues", "temporary storage / transient data",
                     f"{len(queues)} queues"))
    mq = sorted({c.target for p in progs for c in p.calls if c.target.startswith("MQ")}
                | {c for p in progs for c in p.copybooks if c.startswith("CMQ")})
    if mq:
        n = sum(1 for p in progs if any(c.target.startswith("MQ") for c in p.calls)
                or any(c.startswith("CMQ") for c in p.copybooks))
        rows.append(("IBM MQ", "messaging", f"{n} programs use MQ ({', '.join(mq[:5])})"))
    jobs = [j for j in index.jobs.values() if j.kind == "job"]
    procs = [j for j in index.jobs.values() if j.kind == "proc"]
    if jobs or procs:
        utils = Counter(
            s.program for j in index.jobs.values() for s in j.steps
            if s.program and not s.app_program and _UTILITY_HINT.match(s.program)
        )
        util_txt = ", ".join(f"{u} x{n}" for u, n in utils.most_common(6))
        rows.append(("JCL", "batch job control",
                     f"{len(jobs)} jobs, {len(procs)} procs" + (f"; utilities {util_txt}"
                                                                if util_txt else "")))
    # CICS programs talk to the terminal through BMS (its own row)
    screen = [
        p for p in progs
        if (p.uses_screen_io or getattr(p, "uses_terminal_io", False)) and not p.has_cics
    ]
    if screen:
        rows.append(("Terminal screen I/O", "SCREEN SECTION / extended ACCEPT-DISPLAY UI",
                     f"{len(screen)} programs ({', '.join(p.program_id for p in screen[:5])}"
                     + (", ..." if len(screen) > 5 else "") + ")"))
    return rows


_VSAM_KINDS = {"cics-file", "vsam", "ksds", "esds", "rrds", "vsam-ksds", "vsam-esds",
               "vsam-rrds"}
_VSAM_QUALIFIER = re.compile(r"^(VSAM|AIX|KSDS\w*|ESDS\w*|RRDS\w*|\w*KSDS|\w*ESDS|\w*RRDS)$")


def _cics_files(index: CobolRepoIndex) -> set[str]:
    """names programs use in EXEC CICS file commands (FILE / DATASET option)."""
    names: set[str] = set()
    for prog in index.programs.values():
        for c in prog.cics:
            for key in ("FILE", "DATASET"):
                # a literal FILE('X'), or the values a FILE(data-name) was resolved to
                values = [c.args.get(key, "")] + list((c.resolved or {}).get(key, []))
                for v in values:
                    v = (v or "").strip().strip("'\"").upper()
                    if v and re.fullmatch(r"[A-Z0-9$#@\-]+", v):
                        names.add(v)
    return names


def _is_vsam(ds, cics_files: set[str] | None = None) -> bool:
    """a VSAM cluster: CICS-file / VSAM kind, INDEXED or RELATIVE organization, a KSDS /
    ESDS / RRDS / VSAM / AIX qualifier in the DSN (GENAPP.KSDSCUST), or a name that
    programs open as a CICS FILE."""
    if (getattr(ds, "kind", "") or "").lower() in _VSAM_KINDS:
        return True
    if (getattr(ds, "organization", "") or "").upper() in ("INDEXED", "RELATIVE", "KSDS",
                                                           "ESDS", "RRDS", "VSAM"):
        return True
    if any(_VSAM_QUALIFIER.match(q) for q in ds.name.upper().replace("-", "_").split(".")):
        return True
    names = {ds.name.upper(), *(a.upper() for a in getattr(ds, "aliases", []))}
    return bool(cics_files and names & cics_files)


def tech_table(index: CobolRepoIndex) -> str:
    lines = ["| Technology | Role | Evidence |", "|---|---|---|"]
    lines += [f"| {t} | {r} | {e} |" for t, r, e in tech_rows(index)]
    return "\n".join(lines)


def _kinds(d: dict, counts: bool = True) -> str:
    kinds = d.get("kinds") or [d.get("kind", "call")]
    label = "/".join(_KIND_LABEL.get(k, k) for k in kinds)
    n = d.get("count", 1)
    return f"{label} x{n}" if counts and n > 1 else label


def _short_store(name: str, limit: int = 28) -> str:
    """drop leading qualifiers of a long dataset name (AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS ->
    ACCTDATA.VSAM.KSDS) while keeping it distinctive."""
    parts = name.split(".")
    while len(".".join(parts)) > limit and len(parts) > 3:
        parts = parts[1:]
    return ".".join(parts)


def _label(text: str) -> str:
    """text safe inside a quoted mermaid label (no quotes, brackets or html-like tags)."""
    text = text.replace('"', "'").replace("[", "(").replace("]", ")")
    return text.replace("<", "").replace(">", "")


def _mid(name: str) -> str:
    """mermaid-safe node id."""
    mid = re.sub(r"[^a-zA-Z0-9_]", "_", name) or "node"
    if mid[0].isdigit():
        mid = "n" + mid
    # bare keywords break the flowchart parser
    return mid + "_" if mid.lower() in {"end", "graph", "subgraph", "style", "class"} else mid


def _page_summaries(module_docs: list[PageDoc]) -> str:
    return "\n".join(f"- [{d.id}] {d.title}: {d.summary[:300]}" for d in module_docs)


def _first_paragraph(markdown: str) -> str:
    """first prose paragraph after the top heading, as the page summary."""
    para: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith(("#", "|", "-", ">")):
            if para:
                break
            continue
        if not stripped:
            if para:
                break
            continue
        para.append(stripped)
    return " ".join(para)[:600]


def _strip_outer_fence(text: str) -> str:
    """LLMs sometimes wrap the whole page in ```markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1 and text.endswith("```"):
            inner = text[first_nl + 1 : -3]
            if "```" not in inner or inner.count("```") % 2 == 0:
                return inner.strip()
    return text


# -- bridge to the deepdoc generator ---------------------------------------------------------


class _BridgeGenerator(WikiGenerator):
    """the COBOL generator with deepdoc-side cache keys (`deepdoc:cobol:<kind>:...`)."""

    def _key(self, kind: str, payload: str) -> str:
        model = getattr(self.llm, "model", "")
        return f"deepdoc:cobol:{kind}:" + content_hash(
            PROMPT_VERSION + model + self.language + payload)


def view(gen) -> WikiGenerator:
    """the WikiGenerator view of deepdoc generator *gen* (one per generator)."""
    v = getattr(gen, "_cobol_view", None)
    if v is None:
        from codocs.cobol.wiki.planner import build_digests

        idx = gen.index
        digests = getattr(idx, "cobol_digests", None) or build_digests(idx.cobol_index)
        v = _BridgeGenerator(idx.cobol_index, digests, gen.llm, gen.cache,
                             language=gen.language, on_progress=getattr(gen, "_on_progress",
                                                                        None))
        gen._cobol_view = v
    if getattr(gen, "cobol_grounding", None) is None:
        gen.cobol_grounding = {}
    return v


def programs_of(index, packages: list[str]) -> list[str]:
    """COBOL program ids of a page's package keys (other packages are left out)."""
    keys = getattr(index, "cobol_programs", {})
    return [keys[k] for k in packages if k in keys]


def to_legacy_plan(plan, index) -> WikiPlan:
    """a deepdoc plan as a COBOL WikiPlan: packages become COBOL program ids."""
    from codocs.cobol.wiki.models import SectionPlan

    return WikiPlan(
        project_title=plan.project_title, project_summary=plan.project_summary,
        sections=[SectionPlan(id=s.id, title=s.title, pages=[
            PagePlan(id=p.id, title=p.title, programs=programs_of(index, p.packages),
                     workflows=list(p.workflows), description=p.description)
            for p in s.pages]) for s in plan.sections])


def from_legacy_plan(plan: WikiPlan, index, base=None):
    """a COBOL WikiPlan as a deepdoc plan: programs become package keys. Pages of *base* (the
    deepdoc plan it came from) keep their non-COBOL packages."""
    from codocs.repowiki.deepdoc.models import PagePlan as DeepPage
    from codocs.repowiki.deepdoc.models import SectionPlan as DeepSection
    from codocs.repowiki.deepdoc.models import WikiPlan as DeepPlan

    of = getattr(index, "package_of_program", {})
    keys = getattr(index, "cobol_programs", {})
    other = {p.id: [k for k in p.packages if k not in keys]
             for p in (base.all_pages() if base is not None else [])}
    return DeepPlan(
        project_title=plan.project_title, project_summary=plan.project_summary,
        sections=[DeepSection(id=s.id, title=s.title, pages=[
            DeepPage(id=p.id, title=p.title,
                     packages=other.get(p.id, []) + [of[x] for x in p.programs if x in of],
                     workflows=list(p.workflows), description=p.description)
            for p in s.pages]) for s in plan.sections])


def bind(gen, plan) -> None:
    """bind the view to the whole deepdoc plan (page of each program, grounding pages)."""
    view(gen)._bind_plan(to_legacy_plan(plan, gen.index))


def _deep_doc(doc: PageDoc):
    from codocs.repowiki.deepdoc.models import PageDoc as DeepDoc

    return DeepDoc(id=doc.id, title=doc.title, markdown=doc.markdown, summary=doc.summary)


def _legacy_docs(docs) -> list[PageDoc]:
    return [PageDoc(id=d.id, title=d.title, markdown=d.markdown, summary=d.summary)
            for d in docs]


async def module_page(gen, page, project_summary: str = ""):
    """the COBOL module page of a deepdoc page whose packages are COBOL programs."""
    v = view(gen)
    lpage = PagePlan(id=page.id, title=page.title,
                     programs=programs_of(gen.index, page.packages),
                     workflows=list(page.workflows), description=page.description)
    doc = await v._module_page(lpage, project_summary)
    gen.cobol_grounding[page.id] = list(doc.grounding)
    return _deep_doc(doc)


async def architecture_page(gen, module_docs):
    """the COBOL architecture page (COBOL-only wikis), grounded."""
    doc = await view(gen)._architecture_page(_legacy_docs(module_docs))
    gen.cobol_grounding[doc.id] = list(doc.grounding)
    return _deep_doc(doc)


async def overview_page(gen, plan, module_docs):
    """the COBOL overview page (COBOL-only wikis), grounded."""
    doc = await view(gen)._overview_page(to_legacy_plan(plan, gen.index),
                                         _legacy_docs(module_docs))
    gen.cobol_grounding[doc.id] = list(doc.grounding)
    return _deep_doc(doc)


async def refine_plan(gen, skeleton):
    """the COBOL plan refine (prompt, facts, validation keeping programs fixed)."""
    cobol_plan = to_legacy_plan(skeleton, gen.index)
    refined = await view(gen).refine_plan(cobol_plan)
    if refined is cobol_plan:
        return skeleton
    return from_legacy_plan(refined, gen.index, skeleton)


def program_blocks(gen, page, cap: int = 3_000) -> str:
    """COBOL program blocks for a deepdoc page that mixes COBOL and other packages."""
    pids = [p for p in programs_of(gen.index, page.packages)
            if p in gen.index.cobol_index.programs]
    if not pids:
        return ""
    v = view(gen)
    return "\n\n".join(v.program_block(p, cap) for p in pids)


def facts_block(index) -> str:
    """the COBOL block of a mixed wiki's plan and system facts."""
    cidx = getattr(index, "cobol_index", None)
    keys = getattr(index, "cobol_programs", {})
    if cidx is None or not keys:
        return ""
    s = cidx.summary()
    pids = set(keys.values())
    txns = {t: p for t, p in repo_transactions(cidx).items() if p in pids}
    lines = [
        f"COBOL: {len(pids)} programs, {s.copybooks} copybooks, {s.jcl_jobs} JCL jobs, "
        f"{len(txns)} CICS transactions, {len(cidx.datastores)} data stores."
    ]
    entry = cidx.entrypoints()
    kinds = [f"{k} {', '.join(p for p in v if p in pids)}" for k, v in entry.items()
             if any(p in pids for p in v)]
    if kinds:
        lines.append("COBOL entry points: " + "; ".join(kinds))
    lines.append("COBOL technology:\n" + tech_table(cidx))
    pages: dict[str, list[str]] = {}
    for key, page in sorted(getattr(index, "cobol_page_of", {}).items()):
        if key in keys:
            pages.setdefault(page, []).append(keys[key])
    if pages:
        lines.append("COBOL pages (page id: programs): " + "; ".join(
            f"{pid}: {', '.join(ps[:12])}" for pid, ps in sorted(pages.items())))
    return "\n".join(lines)
