"""end-to-end deepdoc pipeline: analyze -> plan -> generate -> assemble -> export."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.analysis.leafdocs import LeafDocIndex, digest_from_static
from codocs.repowiki.config import Config
from codocs.repowiki.core.wiki_builder import Wiki
from codocs.repowiki.deepdoc.assembler import assemble
from codocs.repowiki.deepdoc.models import PackageDigest, WikiPlan
from codocs.repowiki.deepdoc.planner import build_skeleton

logger = logging.getLogger(__name__)


def analyze(
    root: str | Path,
    leaf_dirname: str = "markdown_docs",
    subtree: str | None = None,
) -> tuple[GoModuleIndex, dict[str, PackageDigest], WikiPlan]:
    """the deterministic half of the pipeline: static analysis + leaf docs + skeleton."""
    index = GoModuleIndex.build(root, subtree=subtree)
    if not index.packages:
        raise ValueError(f"no Go packages found under {root}")
    leaf = LeafDocIndex.build(root, known_packages=set(index.packages), docs_dirname=leaf_dirname)
    digests = {
        path: leaf.digests.get(path) or digest_from_static(pkg)
        for path, pkg in index.packages.items()
    }
    skeleton = build_skeleton(index, digests)
    return index, digests, skeleton


async def run_deepdoc(
    root: str | Path,
    cfg: Config,
    leaf_dirname: str = "markdown_docs",
    subtree: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Wiki, object]:
    """full pipeline; returns the assembled Wiki and the LLM client (for usage stats)."""
    from codocs.repowiki.core.cache import Cache
    from codocs.repowiki.deepdoc.generator import DeepDocGenerator
    from codocs.repowiki.llm.client import LLMClient

    index, digests, skeleton = analyze(root, leaf_dirname=leaf_dirname, subtree=subtree)
    if on_progress:
        on_progress(
            f"Analyzed {len(index.packages)} packages, "
            f"{index.graph.number_of_edges()} dependency edges, "
            f"{sum(1 for d in digests.values() if d.source == 'leafdocs')} with leaf docs"
        )

    llm = LLMClient(model=cfg.model, api_key=cfg.api_key, api_base=cfg.api_base)
    cache = Cache()
    await cache.init()
    try:
        gen = DeepDocGenerator(
            index,
            digests,
            llm,
            cache,
            language=cfg.language,
            concurrency=cfg.concurrency,
            on_progress=on_progress,
        )
        plan = await gen.refine_plan(skeleton)
        docs = await gen.generate(plan)
    finally:
        await cache.close()

    wiki = assemble(plan, docs, index, digests)
    return wiki, llm
