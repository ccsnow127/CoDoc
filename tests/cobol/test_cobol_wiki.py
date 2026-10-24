"""COBOL in the multi-language wiki: program packages, digests, plan merge, bridge, assembly."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from wiki_fixtures import flat_repo
from wiki_index_fixtures import cobol_doc, install_fake_llm, two_program_file, write_doc

from codocs.cobol.wiki import pages as cobol_pages
from codocs.cobol.wiki.planner import RESERVED_IDS, build_skeleton, merge_into
from codocs.config import Settings
from codocs.lang.wikiindex import MultiLangIndex
from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.deepdoc.planner import build_skeleton as deep_skeleton
from codocs.wiki import build_digests, multilang_generator, run_wiki


def _index(root: Path, languages=("cobol",), out: Path | None = None, **kw) -> MultiLangIndex:
    return MultiLangIndex.build(root, None, list(languages), Settings(**kw), out_dir=out)


def _settings(tmp_path: Path, lang: str = "cobol") -> Settings:
    return Settings(source_lang=lang, cache_path=str(tmp_path / "cache" / "cache.db"))


# -- packages ------------------------------------------------------------------------------


def test_cobol_program_packages(cobolmod_copy: Path):
    idx = _index(cobolmod_copy)
    assert sorted(idx.packages) == ["cbl/ACCTINQ.cbl", "cbl/ACCTRPT.cbl", "cbl/DATEUTIL.cbl",
                                    "cbl/DAYSINM.cbl"]
    assert idx.cobol_programs["cbl/DAYSINM.cbl"] == "DAYS-IN-MONTH"
    assert idx.package_of_program["DATEUTIL"] == "cbl/DATEUTIL.cbl"
    rpt = idx.packages["cbl/ACCTRPT.cbl"]
    assert rpt.name == "ACCTRPT" and rpt.is_main
    assert rpt.files == ["cbl/ACCTRPT.cbl", "cpy/ACCTREC.cpy", "cpy/DATEPARM.cpy"]
    assert rpt.imports_internal == ["cbl/DATEUTIL.cbl"] and rpt.imports_stdlib == ["JCL"]
    assert "ACCTRPT" in rpt.doc_comment
    inq = idx.packages["cbl/ACCTINQ.cbl"]
    assert inq.is_main and inq.imports_stdlib == ["BMS", "CICS", "VSAM"]
    du = idx.packages["cbl/DATEUTIL.cbl"]
    assert not du.is_main and du.test_files == 1          # test/TDATEUTL.cbl CALLs it
    assert du.imports_internal == ["cbl/DAYSINM.cbl"]      # the user-defined FUNCTION
    syms = [(s.kind, s.receiver, s.name) for s in du.symbols]
    assert syms[0] == ("type", "", "DATEUTIL")
    assert ("method", "DATEUTIL", "1000-CHECK-MONTH") in syms
    assert ("method", "DATEUTIL", "9999-EXIT") not in syms  # trivial EXIT paragraph
    assert idx.languages["cobol"]["packages"] == 4
    assert idx.language_counts() == {"cobol": 4}
    assert ("cbl/ACCTRPT.cbl", "cbl/DATEUTIL.cbl") in idx.graph.edges
    tech = {name: n for name, _cat, n in idx.tech_stack()}
    assert tech["COBOL"] == 4 and tech["CICS"] == 1 and tech["JCL"] == 1


def test_second_program_of_a_file_gets_its_own_key(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PAIR.cbl").write_text(two_program_file())
    idx = _index(repo)
    assert sorted(idx.packages) == ["PAIR.cbl", "PAIR.cbl#SUBPGM2"]
    assert idx.cobol_programs == {"PAIR.cbl": "MAINPGM", "PAIR.cbl#SUBPGM2": "SUBPGM2"}
    assert idx.packages["PAIR.cbl"].imports_internal == ["PAIR.cbl#SUBPGM2"]


def test_group_of_is_not_overridden_and_merge_fills_page_of(cobolmod_copy: Path,
                                                            tmp_path: Path):
    assert MultiLangIndex.group_of is GoModuleIndex.group_of
    idx = _index(cobolmod_copy)
    digests, counts = build_digests(idx, cobolmod_copy, tmp_path / "out")
    assert counts["static"] == 4
    skel = deep_skeleton(idx, digests)
    assert [s.title for s in skel.sections] == ["cbl"]       # deepdoc groups by directory
    plan = merge_into(skel, idx)
    assert [s.title for s in plan.sections] == ["Application"]
    page = plan.all_pages()[0]
    assert page.title == "cobolmod programs" and sorted(page.packages) == sorted(idx.packages)
    assert set(idx.cobol_page_of) == set(idx.packages)
    assert set(idx.cobol_page_of.values()) == {page.id}
    legacy = build_skeleton(idx.cobol_index, idx.cobol_digests)
    assert [p.id for p in legacy.all_pages()] == [p.id for p in plan.all_pages()]


def test_merge_into_keeps_other_languages_and_unique_ids(mixedcobol_copy: Path,
                                                         tmp_path: Path):
    idx = _index(mixedcobol_copy, ("c", "python", "cobol"))
    digests, _ = build_digests(idx, mixedcobol_copy, tmp_path / "out")
    skel = deep_skeleton(idx, digests)
    plan = merge_into(skel, idx)
    keys = set(idx.cobol_programs)
    deep_pages = [p for p in plan.all_pages() if not set(p.packages) & keys]
    cobol_pages_ = [p for p in plan.all_pages() if set(p.packages) & keys]
    assert {k for p in deep_pages for k in p.packages} == {"clib", "mixed", "tools"}
    assert all(set(p.packages) <= keys for p in cobol_pages_)
    assert {k for p in cobol_pages_ for k in p.packages} == keys
    ids = [p.id for p in plan.all_pages()]
    assert len(ids) == len(set(ids)) and not set(ids) & RESERVED_IDS
    assert plan.sections[-1].title == "Application"          # the COBOL planner's section


def test_mixed_directory_digests(mixedcobol_copy: Path, tmp_path: Path):
    out = tmp_path / "out"
    write_doc(out, "mixed/NUMPGM.cbl", cobol_doc(
        "mixed/NUMPGM.cbl", "NUMPGM", "Formats a count as edited text.",
        {"0000-MAIN": "Moves the count through an edited picture."}))
    (out / "docs" / "mixed" / "markdown_docs" / "fmtnum.c.md").write_text(
        "# mixed/fmtnum.c\n\n## FunctionDef fmtnum\n**fmtnum**: Formats a count.\n")
    idx = _index(mixedcobol_copy, ("c", "python", "cobol"), out)
    digests, counts = build_digests(idx, mixedcobol_copy, out)
    c_names = [e.name for e in digests["mixed"].entries]
    assert "fmtnum" in c_names
    assert not any("NUMPGM" in n for n in c_names)           # no COBOL entries in the C unit
    num = digests["mixed/NUMPGM.cbl"]
    assert num.source == "leafdocs" and num.entries[0].kind == "purpose"
    assert num.entries[0].summary == "Formats a count as edited text."
    assert any(e.kind == "paragraph" and e.name == "0000-MAIN" for e in num.entries)
    assert digests["cobol/ACCTRPT.cbl"].source == "static"
    assert counts["leaf_out"] == 2
    assert idx.cobol_docs["NUMPGM"].purpose == "Formats a count as edited text."


# -- plan-only -----------------------------------------------------------------------------


def test_plan_only_cobolmod_and_mixedcobol(cobolmod_copy: Path, mixedcobol_copy: Path,
                                           tmp_path: Path):
    res = asyncio.run(run_wiki(cobolmod_copy, tmp_path / "o1", _settings(tmp_path),
                               plan_only=True))
    assert res["packages"] == 4 and res["languages"] == {
        "cobol": {"packages": 4, "files": 7, "loc": res["languages"]["cobol"]["loc"]}}
    assert res["cobol"]["programs"] == 4 and res["cobol"]["jobs"] == 1
    assert res["plan"] == [{"title": "Application",
                            "pages": [{"title": "cobolmod programs", "packages": 4}]}]
    res = asyncio.run(run_wiki(mixedcobol_copy, tmp_path / "o2", _settings(tmp_path, "all"),
                               plan_only=True))
    assert set(res["languages"]) == {"c", "python", "cobol"}
    assert res["plan"][-1]["title"] == "Application"
    assert {s["title"] for s in res["plan"][:-1]} == {"clib", "mixed", "tools"}


# -- generation through the bridge -----------------------------------------------------------


def test_cobol_only_wiki_uses_the_cobol_generator(tmp_path: Path, monkeypatch):
    llm = install_fake_llm(monkeypatch)
    repo = flat_repo(tmp_path / "repo")
    out = tmp_path / "out"
    write_doc(out, "ACCT01.cbl", cobol_doc("ACCT01.cbl", "ACCT01", "Posts account balances.",
                                           {"MAIN-PARA": "Opens the file and calls DATEUTIL."}))
    (out / "wiki").mkdir(parents=True)
    (out / "wiki" / "old-page.md").write_text("# old")
    res = asyncio.run(run_wiki(repo, out, _settings(tmp_path), fmt="markdown"))
    wiki = out / "wiki"
    names = {p.name for p in wiki.glob("*.md")}
    assert "old-page.md" not in names                        # stale pages removed
    assert {"index.md", "architecture.md", "program-index.md", "copybook-index.md",
            "batch-jobs.md", "data-stores.md", "prog-acct01.md"} <= names
    assert "package-index.md" not in names                   # COBOL-only: program-index
    systems = [c[0]["content"] for c in llm.calls]
    assert all("COBOL" in s for s in systems)                # only the COBOL prompts
    refine = next(c for c in llm.calls if "information architect" in c[0]["content"])
    assert "sample_programs" in refine[-1]["content"]        # the COBOL refine facts
    page = next(p for p in wiki.glob("*.md") if "Nice " in p.read_text().split("\n", 1)[0])
    text = page.read_text()
    assert "COBOL page starting at [`" in text and "(program-index.md)" in text
    assert "**Programs on this page:**" in text
    assert "-->|CALL| VIEW01" not in text                    # grounding dropped the edge
    grounding = json.loads((wiki / "grounding.json").read_text())
    assert grounding[page.stem] == ["dropped edge ACCT01 -->|CALL| VIEW01"]
    assert res["grounding"] >= 1
    index_md = (wiki / "index.md").read_text()
    assert "(architecture.md)" in index_md and "(program-index.md)" in index_md
    prog = (wiki / "prog-acct01.md").read_text()
    assert "Posts account balances." in prog and "Source: `ACCT01.cbl`" in prog
    sidebar = (wiki / "_sidebar.md").read_text()
    assert "Programs" in sidebar and "Reference" in sidebar
    # a second run is served from the cache (deepdoc:cobol keys)
    n = len(llm.calls)
    asyncio.run(run_wiki(repo, out, _settings(tmp_path), fmt="markdown"))
    assert len(llm.calls) == n


def test_no_refine_skips_the_plan_call(tmp_path: Path, monkeypatch):
    llm = install_fake_llm(monkeypatch)
    repo = flat_repo(tmp_path / "repo")
    asyncio.run(run_wiki(repo, tmp_path / "out", _settings(tmp_path), fmt="markdown",
                         refine=False))
    assert not any("information architect" in c[0]["content"] for c in llm.calls)


def test_mixed_wiki_keeps_package_index_and_adds_cobol_pages(mixedcobol_copy: Path,
                                                             tmp_path: Path, monkeypatch):
    llm = install_fake_llm(monkeypatch)
    out = tmp_path / "out"
    res = asyncio.run(run_wiki(mixedcobol_copy, out, _settings(tmp_path, "all"),
                               fmt="markdown"))
    wiki = out / "wiki"
    names = {p.name for p in wiki.glob("*.md")}
    assert {"package-index.md", "program-index.md", "index.md", "architecture.md"} <= names
    cobol_calls = [c for c in llm.calls if "COBOL" in c[0]["content"]]
    deep_calls = [c for c in llm.calls if "COBOL" not in c[0]["content"]]
    assert cobol_calls and deep_calls
    # deepdoc's architecture/overview facts carry the COBOL block
    arch = next(c for c in deep_calls if "Architecture" in c[-1]["content"][:80])
    assert "COBOL: 4 programs" in arch[-1]["content"]
    assert res["languages"]["cobol"]["packages"] == 4


def test_refine_bridge_keeps_assignment(tmp_path: Path):
    repo = flat_repo(tmp_path / "repo")
    idx = _index(repo)
    digests, _ = build_digests(idx, repo, tmp_path / "out")
    plan = merge_into(deep_skeleton(idx, digests), idx)

    class Cache:
        async def get(self, key):
            return None

        async def put(self, key, value):
            self.key = key

    from wiki_index_fixtures import FakeWikiLLM

    from codocs.repowiki.deepdoc.generator import DeepDocGenerator

    gen = multilang_generator(DeepDocGenerator)(idx, digests, FakeWikiLLM(), Cache())
    refined = asyncio.run(gen.refine_plan(plan))
    assert refined.project_title == "Demo Bank"
    base = {p.id: p.packages for p in plan.all_pages()}
    assert all(sorted(p.packages) == sorted(base[p.id]) for p in refined.all_pages())
    assert all(p.title == f"Nice {p.id}" for p in refined.all_pages())
    assert gen.cache.key.startswith("deepdoc:cobol:plan:")
    legacy = cobol_pages.to_legacy_plan(plan, idx)
    assert {p for pg in legacy.all_pages() for p in pg.programs} == set(idx.cobol_programs.values())
    back = cobol_pages.from_legacy_plan(legacy, idx, plan)
    assert [p.packages for p in back.all_pages()] == [p.packages for p in plan.all_pages()]


def test_per_file_facts_for_the_adapter(cobolmod_copy: Path):
    from codocs.cobol import graphfacts
    from codocs.cobol.index import CobolRepoIndex

    cidx = CobolRepoIndex.build(cobolmod_copy)
    wf = graphfacts.wiki_facts(cidx, "cbl/DATEUTIL.cbl")
    assert [(d.kind, d.name) for d in wf.decls][:2] == [("type", "DATEUTIL"),
                                                        ("method", "0000-MAIN")]
    assert ("copy", "DATEPARM") in [(i.kind, i.module) for i in wf.imports]
    assert ("function", "DAYS-IN-MONTH") in [(i.kind, i.module) for i in wf.imports]
    assert not wf.is_main and wf.loc == cidx.programs["DATEUTIL"].loc
    gf = graphfacts.graph_facts(cidx, "cbl/ACCTRPT.cbl")
    assert [f.name for f in gf.funcs][:2] == ["ACCTRPT", "ACCTRPT.0000-MAIN"]
    assert [c.name for c in gf.calls["ACCTRPT"]] == ["DATEUTIL"]
    assert graphfacts.graph_facts(cidx, "test/TDATEUTL.cbl").is_test
    assert graphfacts.test_roles(cidx) == {"test/TDATEUTL.cbl": "test"}


def test_carddemo_plan_only_in_manifest_scope(datasets, tmp_path: Path):
    import pytest

    carddemo = datasets("carddemo")
    if carddemo is None:
        pytest.skip("CardDemo clone not available")
    settings = Settings(source_lang="cobol", exclude_paths="app/data\napp/scheduler\napp/catlg",
                        cache_path=str(tmp_path / "c.db"))
    res = asyncio.run(run_wiki(carddemo, tmp_path / "out", settings, subtree="app",
                               plan_only=True))
    assert res["packages"] == 44 and res["cobol"]["programs"] == 44
    assert res["cobol"]["jobs"] == 46 and res["cobol"]["copybooks"] == 65
    pages = [p for s in res["plan"] for p in s["pages"]]
    assert sum(p["packages"] for p in pages) == 44
    assert max(p["packages"] for p in pages) <= 12 and min(p["packages"] for p in pages) >= 2
