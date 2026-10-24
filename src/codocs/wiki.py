"""repowiki deepdoc run with leaf docs from OUT/docs, then in-tree markdown_docs/, then static.

a Go module repo (or --source-lang go) runs on repowiki's GoModuleIndex;
any other language set runs the same deepdoc code on `codocs.lang.wikiindex.MultiLangIndex`
(one package per unit directory of every language) with `MultiLangDeepDocGenerator`.

COBOL is one package per program. Its pages come from the COBOL planner
(`codocs.cobol.wiki.planner.merge_into`) and, for pages holding only COBOL programs,
from the COBOL page generator (grounding, stricter mermaid checks); the
COBOL appendix and program pages are added after deepdoc's assembly.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from codocs.config import Settings

log = logging.getLogger(__name__)

LEAF_DIRNAME = "markdown_docs"


def _drop_out_dir(index, module_root: Path, out_dir: Path) -> None:
    """forget packages found inside OUT when OUT lives in the module tree."""
    try:
        rel = out_dir.resolve().relative_to(module_root.resolve()).as_posix()
    except ValueError:
        return
    if rel in ("", "."):
        return
    inside = [p for p in index.packages if p == rel or p.startswith(rel + "/")]
    for p in inside:
        del index.packages[p]
    index.graph.remove_nodes_from(inside)


def _keep_packages(index, packages: list[str]) -> None:
    """forget packages outside the selection (a rel equal to one of them or below it)."""
    wanted = [p.strip().strip("/") for p in packages]
    outside = [p for p in index.packages
               if not any(p == w or not w or p.startswith(w + "/") for w in wanted)]
    for p in outside:
        del index.packages[p]
    index.graph.remove_nodes_from(outside)


def _is_cobol_doc(path: Path, d: str, program_files: set[str]) -> bool:
    """a COBOL file doc: `<basename>.md` of a program file of dir d, or a COBOL basename."""
    from codocs.cobol.docs import PROGRAM_EXTS

    base = path.name[:-3] if path.name.endswith(".md") else path.name
    return (f"{d}/{base}" if d else base) in program_files or base.lower().endswith(PROGRAM_EXTS)


def _leaf_digests(root: Path, known: set[str], index, view_dir: Path) -> dict:
    """LeafDocIndex digests under root; a directory holding the docs of a COBOL program and
    of another language's unit is read through a copy without the COBOL docs."""
    from codocs.repowiki.analysis.leafdocs import LeafDocIndex

    digests = LeafDocIndex.build(root, known_packages=known, docs_dirname=LEAF_DIRNAME).digests
    keys = getattr(index, "cobol_programs", None) or {}
    cidx = getattr(index, "cobol_index", None)
    if not keys or cidx is None:
        return digests
    program_files = {p.path for p in cidx.programs.values()}
    mixed: list[str] = []
    for d in sorted(known - set(keys)):
        mdir = (root / d if d else root) / LEAF_DIRNAME
        if mdir.is_dir() and any(_is_cobol_doc(f, d, program_files) for f in mdir.rglob("*.md")):
            mixed.append(d)
    if not mixed:
        return digests
    shutil.rmtree(view_dir, ignore_errors=True)
    for d in mixed:
        src = (root / d if d else root) / LEAF_DIRNAME
        dst = (view_dir / d if d else view_dir) / LEAF_DIRNAME
        for f in sorted(src.rglob("*.md")):
            if _is_cobol_doc(f, d, program_files):
                continue
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
    view = (LeafDocIndex.build(view_dir, known_packages=known, docs_dirname=LEAF_DIRNAME).digests
            if view_dir.is_dir() else {})
    for d in mixed:
        if d in view:
            digests[d] = view[d]
        else:
            digests.pop(d, None)
    return digests


def _cobol_digests(index, module_root: Path, out_dir: Path, use_tree_docs: bool
                   ) -> tuple[dict, dict]:
    """(program docs by PID: OUT/docs first, then in-tree; ProgramDigest by PID)."""
    from codocs.cobol.docs import load_program_docs
    from codocs.cobol.wiki.digest import load_tree_docs, program_digest

    cidx = index.cobol_index
    docs = load_tree_docs(module_root, cidx) if use_tree_docs else {}
    out_docs = load_program_docs(out_dir, cidx)
    docs.update(out_docs)
    index.cobol_docs = docs
    index.cobol_out_docs = set(out_docs)
    index.cobol_digests = {pid: program_digest(prog, docs.get(pid))
                           for pid, prog in cidx.programs.items()}
    return docs, index.cobol_digests


def build_digests(index, module_root: Path, out_dir: Path, use_tree_docs: bool = True
                  ) -> tuple[dict, dict[str, int]]:
    """package -> PackageDigest merged by priority OUT/docs > in-tree markdown_docs > static.

    entries are merged per symbol: files a leaf doc does not cover (generated, too large,
    not selected) keep their static entries instead of vanishing from the package.
    COBOL packages (programs) get the COBOL digest of their program doc (OUT/docs, then
    in-tree markdown_docs/, else static facts).
    """
    from codocs.repowiki.analysis.leafdocs import digest_from_static
    from codocs.repowiki.deepdoc.models import PackageDigest

    known = set(index.packages)
    docs_root = out_dir / "docs"
    views = out_dir / "work" / "wiki"
    out_leaf = (_leaf_digests(docs_root, known, index, views / "leafview")
                if docs_root.is_dir() else {})
    tree_leaf = (_leaf_digests(module_root, known, index, views / "leaftree")
                 if use_tree_docs else {})
    keys = getattr(index, "cobol_programs", None) or {}
    cobol_docs: dict = {}
    if keys and getattr(index, "cobol_index", None) is not None:
        cobol_docs, _ = _cobol_digests(index, module_root, out_dir, use_tree_docs)
    digests = {}
    counts = {"leaf_out": 0, "leaf_tree": 0, "static": 0, "mixed": 0}
    for path, pkg in index.packages.items():
        if path in keys:
            from codocs.cobol.wiki.digest import to_package

            pid = keys[path]
            doc = cobol_docs.get(pid)
            if doc is None:
                counts["static"] += 1
            elif pid in index.cobol_out_docs:
                counts["leaf_out"] += 1
            else:
                counts["leaf_tree"] += 1
            digests[path] = to_package(index.cobol_digests[pid], path)
            continue
        out_d, tree_d = out_leaf.get(path), tree_leaf.get(path)
        has_out = out_d is not None and bool(out_d.entries)
        has_tree = tree_d is not None and bool(tree_d.entries)
        if has_out:
            counts["leaf_out"] += 1
        elif has_tree:
            counts["leaf_tree"] += 1
        else:
            counts["static"] += 1
            digests[path] = digest_from_static(pkg)
            continue
        merged = PackageDigest(package=path, source="leafdocs")
        seen: set[str] = set()
        extra = 0
        for d, leaf in ((out_d, True), (tree_d, True), (digest_from_static(pkg), False)):
            for e in (d.entries if d is not None else []):
                key = e.name.replace("::", ".")       # a C++ leaf doc's `Cls::m` = static Cls.m
                if key in seen:
                    continue
                seen.add(key)
                merged.entries.append(e)
                extra += not leaf
        counts["mixed"] += extra > 0
        digests[path] = merged
    return digests, counts


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "wiki"


def wiki_cache_path(settings: Settings) -> Path:
    """wiki llm cache next to codocs' own, one file per model and language."""
    base = Path(settings.cache_path).expanduser().parent
    return base / f"wiki-{_slug(settings.model)}-{_slug(settings.language)}.db"


def wiki_languages(module_root: Path, subtree: str | None, settings: Settings,
                   out_dir: Path | None) -> list[str]:
    """languages of the wiki: detection with settings.source_lang (auto: Go repos stay Go)."""
    from codocs.lang.registry import detect_languages

    req = (getattr(settings, "source_lang", "auto") or "auto").strip() or "auto"
    try:
        if any(p.strip().lower() in ("cobol", "cbl", "cob") for p in req.split(",")):
            from codocs.cobol.graphfacts import detect

            return detect(module_root, subtree or None, req, out_dir=out_dir)
        return list(detect_languages(module_root, subtree or None, req,
                                     out_dir=out_dir).languages)
    except ValueError:
        if req.lower() == "auto":         # the Go-only wiki reports what it cannot find
            return ["go"]
        raise


_DISPLAY = {"go": "Go", "c": "C", "cpp": "C++", "java": "Java", "python": "Python",
            "cobol": "COBOL"}
_MAIN_WORDS = {"go": "Go `package main`", "c": "C/C++ `main()`", "cpp": "C/C++ `main()`",
               "python": "Python `__main__`", "java": "Java `main`",
               "cobol": "COBOL main programs (JCL steps, CICS transactions, standalone)"}


def _all_langs() -> tuple[str, ...]:
    try:
        from codocs.lang.base import ALL_LANGS
    except ImportError:  # a lang.base without ALL_LANGS
        from codocs.lang.base import LANG_ORDER

        return LANG_ORDER + ("cobol",)
    return ALL_LANGS


def multilang_generator(base: type) -> type:
    """a subclass of deepdoc's generator `base` whose plan/system facts name every language."""

    class MultiLangDeepDocGenerator(base):
        def _repo_line(self) -> str:
            idx = self.index
            counts = idx.language_counts() if hasattr(idx, "language_counts") else {}
            per = ", ".join(f"{_DISPLAY.get(k, k)} {n}" for k, n in counts.items())
            return f"Repository `{idx.module}`: {len(idx.packages)} packages ({per})."

        def _entry_line(self) -> tuple[str, list[str]]:
            idx = self.index
            kinds: list[str] = []
            for lang in [x for x in _all_langs() if x != "cpp"]:
                if any(lang in v or (lang == "c" and "cpp" in v)
                       for v in getattr(idx, "main_kinds", {}).values()):
                    kinds.append(_MAIN_WORDS[lang])
            label = f"Entry points ({', '.join(kinds)})" if kinds else "Entry points"
            return label, idx.entrypoints()

        def _plan_facts(self) -> str:
            label, mains = self._entry_line()
            lines = [self._repo_line(), f"{label}: {', '.join(mains[:30])}"]
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
            lines.append("\nSample package summaries:")
            for path, _score in self.index.rank_packages()[:25]:
                digest = self.digests.get(path)
                if digest and digest.entries:
                    lines.append(f"- {path}: {digest.entries[0].summary[:160]}")
            if self._has_cobol():
                from codocs.cobol.wiki import pages

                lines.append("\n" + pages.facts_block(self.index))
            return "\n".join(lines)

        def _system_facts(self) -> str:
            idx = self.index
            per = getattr(idx, "languages", {})
            loc = ", ".join(f"{_DISPLAY.get(k, k)} {v.get('loc', 0):,}" for k, v in per.items())
            lines = [f"{self._repo_line()} Lines of code: {loc}."]
            label, mains = self._entry_line()
            main_lines = []
            for m in mains:
                digest = self.digests.get(m)
                hint = digest.entries[0].summary[:120] if digest and digest.entries else ""
                main_lines.append(f"- {m}: {hint}")
            lines.append(f"{label}, {len(mains)} total:\n" + "\n".join(main_lines))
            stack = idx.tech_stack()
            if stack:
                lines.append(
                    "External dependencies:\n"
                    + "\n".join(f"- {name} — {cat} (used by {n} packages)"
                                for name, cat, n in stack)
                )
            core = [p for p, _ in idx.rank_packages()[:15]]
            lines.append("Most depended-upon packages (PageRank): " + ", ".join(core))
            if self._has_cobol():
                from codocs.cobol.wiki import pages

                lines.append(pages.facts_block(idx))
            return "\n\n".join(lines)

        # -- COBOL programs ---------------------------------------------------------------

        def _has_cobol(self) -> bool:
            return bool(getattr(self.index, "cobol_programs", None))

        def _cobol_only(self) -> bool:
            return self._has_cobol() and list(getattr(self.index, "languages", {})) == ["cobol"]

        def _cobol_page(self, page) -> bool:
            keys = getattr(self.index, "cobol_programs", {})
            return bool(page.packages) and all(k in keys for k in page.packages)

        async def refine_plan(self, skeleton):
            if self._cobol_only():
                from codocs.cobol.wiki import pages

                return await pages.refine_plan(self, skeleton)
            return await super().refine_plan(skeleton)

        async def generate(self, plan):
            if self._has_cobol():
                from codocs.cobol.wiki import pages

                pages.bind(self, plan)
            return await super().generate(plan)

        async def _module_page(self, page, project_summary):
            if self._has_cobol() and self._cobol_page(page):
                from codocs.cobol.wiki import pages

                return await pages.module_page(self, page, project_summary)
            return await super()._module_page(page, project_summary)

        def _page_context(self, page) -> str:
            text = super()._page_context(page)
            if self._has_cobol():
                from codocs.cobol.wiki import pages

                extra = pages.program_blocks(self, page)
                if extra:
                    text += "\n\n" + extra
            return text

        async def _architecture_page(self, module_docs):
            if self._cobol_only():
                from codocs.cobol.wiki import pages

                return await pages.architecture_page(self, module_docs)
            return await super()._architecture_page(module_docs)

        async def _overview_page(self, plan, module_docs):
            if self._cobol_only():
                from codocs.cobol.wiki import pages

                return await pages.overview_page(self, plan, module_docs)
            return await super()._overview_page(plan, module_docs)

    return MultiLangDeepDocGenerator


def _cobol_summary(index) -> dict:
    """COBOL counts of a wiki run (programs, copybooks, jobs, transactions, data stores)."""
    cidx = index.cobol_index
    s = cidx.summary()
    pids = set(index.cobol_programs.values())
    return {"programs": len(pids), "copybooks": s.copybooks, "jobs": s.jcl_jobs,
            "transactions": sum(1 for p in cidx.transactions.values() if p in pids),
            "datastores": len(cidx.datastores), "pages": len(set(index.cobol_page_of.values()))}


def _clear_stale(wiki_dir: Path, fmt: str) -> None:
    """pages of an older plan must not linger (COBOL wikis)."""
    if not wiki_dir.is_dir():
        return
    if fmt in ("markdown", "both"):
        for old in wiki_dir.glob("*.md"):
            old.unlink()
    if fmt in ("html", "both"):
        for old in wiki_dir.glob("*.html"):
            old.unlink()


async def run_wiki(module_root: Path, out_dir: Path, settings: Settings,
                   subtree: str | None = None, use_tree_docs: bool = True, fmt: str = "both",
                   plan_only: bool = False,
                   on_progress: Callable[[str], None] | None = None,
                   packages: list[str] | None = None, refine: bool = True) -> dict:
    """architecture wiki of the module under OUT/wiki; plan_only stops before any llm call.

    packages narrows the wiki like `-p` narrows the other stages (a rel and everything below).
    languages come from settings.source_lang (auto: a Go module repo gets the Go-only wiki).
    refine=False keeps the deterministic plan (no plan-refinement call).
    """
    from codocs.repowiki.analysis.golang import GoModuleIndex
    from codocs.repowiki.deepdoc.planner import build_skeleton

    module_root = Path(module_root).resolve()
    out_dir = Path(out_dir)
    languages = wiki_languages(module_root, subtree, settings, out_dir)
    if languages != ["go"]:
        from codocs.lang.treesitter import with_deps

        languages = with_deps(languages, "wiki")
    multi = languages != ["go"]
    if multi:
        from codocs.lang.wikiindex import MultiLangIndex

        index = MultiLangIndex.build(module_root, subtree or None, languages, settings,
                                     out_dir=out_dir)
    else:
        index = GoModuleIndex.build(module_root, subtree=subtree or None)
    _drop_out_dir(index, module_root, out_dir)
    if packages:
        _keep_packages(index, packages)
    if not index.packages:
        where = module_root / (subtree or "")
        sel = f" matching {', '.join(packages)}" if packages else ""
        kind = "" if multi else "Go "
        raise ValueError(f"no {kind}packages found under {where}{sel}")

    digests, counts = build_digests(index, module_root, out_dir, use_tree_docs)
    skeleton = build_skeleton(index, digests)
    cobol = multi and bool(getattr(index, "cobol_programs", None))
    if cobol:
        from codocs.cobol.wiki.planner import merge_into

        skeleton = merge_into(skeleton, index)
    result = {
        "module": index.module,
        "packages": len(index.packages),
        **counts,
        "sections": len(skeleton.sections),
        "pages": len(skeleton.all_pages()),
        "html": None,
        "tokens": {"input": 0, "output": 0},
    }
    if multi:
        result["languages"] = {lang: dict(v) for lang, v in index.languages.items()}
    if cobol:
        result["cobol"] = _cobol_summary(index)
    if on_progress:
        on_progress(
            f"Analyzed {len(index.packages)} packages, "
            f"{index.graph.number_of_edges()} dependency edges, "
            f"{counts['leaf_out']} with generated docs, {counts['leaf_tree']} with in-tree docs"
        )
    if plan_only:
        result["plan"] = [
            {"title": s.title, "pages": [{"title": p.title, "packages": len(p.packages)}
                                         for p in s.pages]}
            for s in skeleton.sections
        ]
        return result

    from codocs.repowiki.core.cache import Cache
    from codocs.repowiki.deepdoc.assembler import assemble
    from codocs.repowiki.deepdoc.generator import DeepDocGenerator
    from codocs.repowiki.llm.client import LLMClient

    llm = LLMClient(model=settings.model, api_key=settings.api_key, api_base=settings.api_base)
    cache_path = wiki_cache_path(settings)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = Cache(cache_path)
    await cache.init()
    gen_cls = multilang_generator(DeepDocGenerator) if multi else DeepDocGenerator
    try:
        gen = gen_cls(
            index, digests, llm, cache,
            language=settings.language,
            concurrency=settings.concurrency,
            on_progress=on_progress,
        )
        plan = await gen.refine_plan(skeleton) if refine else skeleton
        docs = await gen.generate(plan)
    finally:
        await cache.close()

    wiki_dir = out_dir / "wiki"
    if cobol:
        from codocs.cobol.wiki.assembler import extend, protect_links

        grounding = getattr(gen, "cobol_grounding", None) or {}
        wiki = assemble(plan, protect_links(docs, {d.id for d in docs}), index, digests)
        _clear_stale(wiki_dir, fmt)
        wiki = extend(wiki, plan, docs, index.cobol_index, index.cobol_programs,
                      index.cobol_docs, list(index.languages), digests=index.cobol_digests,
                      grounding=grounding, wiki_dir=wiki_dir)
        result["grounding"] = sum(len(v) for v in grounding.values())
    else:
        wiki = assemble(plan, docs, index, digests)
    if fmt in ("markdown", "both"):
        from codocs.repowiki.export.markdown import export_markdown

        export_markdown(wiki, wiki_dir)
    if fmt in ("html", "both"):
        from codocs.repowiki.export.html import export_html, load_local_mermaid

        html_path = wiki_dir / f"{_slug(wiki.project_name)}.html"
        export_html(wiki, html_path, branding=False, mermaid_js=load_local_mermaid())
        result["html"] = os.fspath(html_path)
    result["sections"] = len(plan.sections)
    result["pages"] = len(wiki.pages)
    result["tokens"] = {"input": llm.total_input_tokens, "output": llm.total_output_tokens}
    log.info("wiki: %d pages, %d packages (%d generated docs, %d in-tree, %d static)",
             result["pages"], result["packages"], counts["leaf_out"], counts["leaf_tree"],
             counts["static"])
    return result
