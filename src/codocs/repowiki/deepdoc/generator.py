"""orchestrates LLM generation of the high-level wiki pages."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable

from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.core.cache import Cache, content_hash
from codocs.repowiki.deepdoc import mermaid, prompts
from codocs.repowiki.deepdoc.models import PackageDigest, PageDoc, PagePlan, WikiPlan
from codocs.repowiki.deepdoc.planner import validate_refined_plan
from codocs.repowiki.llm.client import LLMClient
from codocs.repowiki.llm.prompts import extract_json

logger = logging.getLogger(__name__)

# bump to invalidate cached pages when prompts/context format change
PROMPT_VERSION = "deepdoc-v1"

_PAGE_MAX_TOKENS = 8000
_PKG_CONTEXT_CAP = 8_000  # chars of digest per package
_PAGE_CONTEXT_CAP = 110_000  # chars of digest context per page


class DeepDocGenerator:
    """generates module pages bottom-up, then architecture and overview pages."""

    def __init__(
        self,
        index: GoModuleIndex,
        digests: dict[str, PackageDigest],
        llm: LLMClient,
        cache: Cache,
        language: str = "en",
        concurrency: int = 4,
        on_progress: Callable[[str], None] | None = None,
    ):
        self.index = index
        self.digests = digests
        self.llm = llm
        self.cache = cache
        self.language = language
        self._sem = asyncio.Semaphore(concurrency)
        self._on_progress = on_progress

    def _progress(self, msg: str) -> None:
        if self._on_progress:
            self._on_progress(msg)

    # -- plan refinement --------------------------------------------------

    async def refine_plan(self, skeleton: WikiPlan) -> WikiPlan:
        self._progress("Refining wiki plan...")
        # slim the skeleton: the LLM names pages, it must not echo 500+ package paths
        slim = skeleton.model_dump()
        for section in slim["sections"]:
            for page in section["pages"]:
                pkgs = page.pop("packages")
                page["package_count"] = len(pkgs)
                page["sample_packages"] = pkgs[:6]
        skeleton_json = json.dumps(slim, indent=1)
        facts = self._plan_facts()
        cache_key = f"deepdoc:plan:{content_hash(PROMPT_VERSION + skeleton_json + facts)}"
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
            logger.warning(
                "plan refinement returned unparseable JSON; keeping skeleton. Tail: %r",
                raw[-300:],
            )
            return skeleton
        try:
            refined = WikiPlan(**data)
        except Exception as e:
            logger.warning("plan refinement schema mismatch (%s); keeping skeleton", e)
            return skeleton
        refined = validate_refined_plan(skeleton, refined)
        await self.cache.put(cache_key, refined.model_dump())
        return refined

    def _plan_facts(self) -> str:
        lines = [f"Go module `{self.index.module}`: {len(self.index.packages)} packages."]
        mains = self.index.entrypoints()
        lines.append(f"Runtime binaries (package main): {', '.join(mains[:30])}")
        stack = self.index.tech_stack()
        if stack:
            lines.append(
                "Key external dependencies: "
                + ", ".join(f"{name} ({cat}, {n} pkgs)" for name, cat, n in stack[:12])
            )
        edges = sorted(self.index.group_edges().items(), key=lambda kv: -kv[1])
        lines.append(
            "Top-level dependency edges (importer -> imported, #package-edges): "
            + ", ".join(f"{s}->{d} ({n})" for (s, d), n in edges[:25])
        )
        # a taste of what each top package does, for naming decisions
        lines.append("\nSample package summaries:")
        for path, _score in self.index.rank_packages()[:25]:
            digest = self.digests.get(path)
            if digest and digest.entries:
                lines.append(f"- {path}: {digest.entries[0].summary[:160]}")
        return "\n".join(lines)

    # -- page generation --------------------------------------------------

    async def generate(self, plan: WikiPlan) -> list[PageDoc]:
        pages = plan.all_pages()
        self._progress(f"Generating {len(pages)} module pages...")
        done = 0

        async def _one(p: PagePlan) -> PageDoc:
            nonlocal done
            async with self._sem:
                doc = await self._module_page(p, plan.project_summary)
            done += 1
            self._progress(f"Module pages: {done}/{len(pages)}")
            return doc

        module_docs = list(await asyncio.gather(*(_one(p) for p in pages)))

        self._progress("Generating architecture page...")
        arch = await self._architecture_page(module_docs)
        self._progress("Generating overview page...")
        overview = await self._overview_page(plan, module_docs)
        return [overview, arch, *module_docs]

    async def _module_page(self, page: PagePlan, project_summary: str) -> PageDoc:
        context = self._page_context(page)
        cache_key = (
            f"deepdoc:page:{page.id}:"
            + content_hash(PROMPT_VERSION + page.title + page.description + context)
        )
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return PageDoc(**cached)
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
        return doc

    def _page_context(self, page: PagePlan) -> str:
        parts: list[str] = []

        # per-package digests under a page-level budget
        pkg_cap = _PKG_CONTEXT_CAP
        blocks = [self._package_block(p, pkg_cap) for p in page.packages]
        if sum(len(b) for b in blocks) > _PAGE_CONTEXT_CAP:
            pkg_cap = max(600, _PAGE_CONTEXT_CAP // max(1, len(page.packages)))
            blocks = [self._package_block(p, pkg_cap) for p in page.packages]
        parts.extend(blocks)

        pkgs = page.packages
        internal = self.index.subgraph_edges(pkgs)
        if internal:
            shown = internal[:200]
            parts.append(
                "### Dependency edges inside this page (importer -> imported)\n"
                + "\n".join(f"- {s} -> {d}" for s, d in shown)
                + (f"\n- ... and {len(internal) - 200} more" if len(internal) > 200 else "")
            )

        outbound = self.index.outbound_edges(pkgs)
        if outbound:
            lines = [f"- {src} -> {', '.join(dsts[:12])}" for src, dsts in list(outbound.items())[:60]]
            parts.append("### Outbound dependencies (to packages on other pages)\n" + "\n".join(lines))

        inbound = self._inbound_edges(pkgs)
        if inbound:
            lines = [f"- {dst} <- {', '.join(srcs[:12])}" for dst, srcs in list(inbound.items())[:40]]
            parts.append("### Inbound dependents (packages elsewhere importing this page)\n" + "\n".join(lines))

        ext = self.index.external_deps(pkgs).most_common(15)
        if ext:
            parts.append(
                "### External libraries used\n" + "\n".join(f"- {imp} ({n} pkgs)" for imp, n in ext)
            )
        return "\n\n".join(parts)

    def _package_block(self, path: str, cap: int) -> str:
        pkg = self.index.packages.get(path)
        digest = self.digests.get(path)
        head = f"### package {path or '(module root)'}"
        meta: list[str] = []
        if pkg:
            if pkg.is_main:
                meta.append("RUNTIME BINARY (package main)")
            meta.append(f"{len(pkg.files)} files, {pkg.loc} LOC")
            if pkg.doc_comment:
                meta.append(pkg.doc_comment[:300])
        lines = [head + (" — " + "; ".join(meta) if meta else "")]
        used = 0
        dropped = 0
        for e in digest.entries if digest else []:
            line = f"- {e.name} ({e.kind}): {e.summary}"
            if used + len(line) > cap:
                dropped += 1
                continue
            used += len(line)
            lines.append(line)
        if dropped:
            lines.append(f"- ... ({dropped} more symbols omitted)")
        return "\n".join(lines)

    def _inbound_edges(self, packages: list[str]) -> dict[str, list[str]]:
        pkgs = set(packages)
        inbound: dict[str, set[str]] = {}
        for s, d in self.index.graph.edges:
            if d in pkgs and s not in pkgs:
                inbound.setdefault(d, set()).add(s)
        return {k: sorted(v) for k, v in sorted(inbound.items())}

    # -- system-level pages ------------------------------------------------

    async def _architecture_page(self, module_docs: list[PageDoc]) -> PageDoc:
        facts = self._system_facts()
        base = self.group_mermaid()
        summaries = _page_summaries(module_docs)
        cache_key = "deepdoc:arch:" + content_hash(PROMPT_VERSION + facts + base + summaries)
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return PageDoc(**cached)
            except Exception:
                pass
        messages = prompts.build_architecture_page_prompt(facts, summaries, base, self.language)
        markdown = await self._complete_page(messages)
        doc = PageDoc(
            id="architecture", title="Architecture", markdown=markdown,
            summary=_first_paragraph(markdown),
        )
        await self.cache.put(cache_key, doc.model_dump())
        return doc

    async def _overview_page(self, plan: WikiPlan, module_docs: list[PageDoc]) -> PageDoc:
        facts = self._system_facts()
        summaries = _page_summaries(module_docs)
        toc_lines = []
        for section in plan.sections:
            toc_lines.append(f"- {section.title}")
            toc_lines.extend(f"  - [[page:{p.id}]] {p.title}" for p in section.pages)
        toc = "Architecture page: [[page:architecture]]\n" + "\n".join(toc_lines)
        cache_key = "deepdoc:overview:" + content_hash(PROMPT_VERSION + facts + summaries + toc)
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return PageDoc(**cached)
            except Exception:
                pass
        messages = prompts.build_overview_page_prompt(facts, summaries, toc, self.language)
        markdown = await self._complete_page(messages)
        doc = PageDoc(
            id="index", title="Overview", markdown=markdown, summary=_first_paragraph(markdown)
        )
        await self.cache.put(cache_key, doc.model_dump())
        return doc

    def _system_facts(self) -> str:
        idx = self.index
        total_loc = sum(p.loc for p in idx.packages.values())
        lines = [
            f"Go module `{idx.module}`: {len(idx.packages)} packages, {total_loc:,} lines of Go.",
        ]
        mains = idx.entrypoints()
        main_lines = []
        for m in mains:
            digest = self.digests.get(m)
            hint = digest.entries[0].summary[:120] if digest and digest.entries else ""
            main_lines.append(f"- {m}: {hint}")
        lines.append(f"Runtime binaries (package main), {len(mains)} total:\n" + "\n".join(main_lines))
        stack = idx.tech_stack()
        if stack:
            lines.append(
                "External dependencies:\n"
                + "\n".join(f"- {name} — {cat} (used by {n} packages)" for name, cat, n in stack)
            )
        core = [p for p, _ in idx.rank_packages()[:15]]
        lines.append("Most depended-upon packages (PageRank): " + ", ".join(core))
        return "\n\n".join(lines)

    def group_mermaid(self, max_edges: int = 40) -> str:
        """deterministic mermaid skeleton of top-level group dependencies."""
        edges = sorted(self.index.group_edges().items(), key=lambda kv: -kv[1])[:max_edges]
        lines = ["graph TD"]
        for (s, d), n in edges:
            sid, did = _mermaid_id(s), _mermaid_id(d)
            lines.append(f'  {sid}["{s}"] -->|{n}| {did}["{d}"]')
        return "\n".join(lines)

    # -- LLM helpers -------------------------------------------------------

    async def _complete(self, messages: list[dict], max_tokens: int = 4096) -> str:
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


def _mermaid_id(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name) or "root"
