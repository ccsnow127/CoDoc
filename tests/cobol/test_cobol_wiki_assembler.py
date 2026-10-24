"""tests for codocs.cobol.wiki.assembler (link resolution, appendix pages, leaf docs, exporters)."""

from __future__ import annotations

import re
from pathlib import Path

from wiki_fixtures import build, flat_repo

from codocs.cobol.docs import LeafDoc
from codocs.cobol.wiki.assembler import assemble, leaf_page_id
from codocs.cobol.wiki.models import PageDoc
from codocs.cobol.wiki.planner import build_digests, build_skeleton
from codocs.repowiki.export.html import export_html
from codocs.repowiki.export.markdown import export_markdown


def _wiki(tmp_path: Path, with_leaf: bool = True):
    idx = build(flat_repo(tmp_path / "repo"))
    digests = build_digests(idx)
    plan = build_skeleton(idx, digests, max_programs=3)
    batch = next(p for p in plan.all_pages() if "ACCT01" in p.programs)
    online = next(p for p in plan.all_pages() if "MENU01" in p.programs)
    docs = [
        PageDoc(id="index", title="Overview",
                markdown="# Demo\n\nRead [[page:architecture]], [[page:missing]], "
                         "`[[prog:MENU01]]`, [[prog:acct01.cbl]] and [[prog:NOPE]]."),
        PageDoc(id="architecture", title="Architecture",
                markdown="# Architecture\n\n[[job:ACCTJOB]] [[job:NOJOB]] [[data:ACCTFILE]] "
                         "[[data:PROD.ACCT.MASTER]] [[data:UNKNOWN]] [[copy:ACCTREC]] "
                         "[[copy:NOCOPY]]\n\n```mermaid\nflowchart TD\n  A[\"[[prog:MENU01]]\"]"
                         " --> B\n```\n"),
        PageDoc(id=batch.id, title="Batch Posting",
                markdown="# Batch Posting\n\nSelf [[prog:ACCT01]], other [[prog:MENU01]]."),
        PageDoc(id=online.id, title="Online Menu", markdown="# Online Menu\n\nx"),
    ]
    leaf = None
    if with_leaf:
        leaf = {
            "ACCT01": LeafDoc(program_id="ACCT01", markdown="# ACCT01 — Posting\n\nCalls "
                              "[[prog:DATEUTIL]].", purpose="Posts balances nightly."),
            "GHOST": LeafDoc(program_id="GHOST", markdown="# GHOST"),
        }
    return idx, plan, assemble(plan, docs, idx, digests, leaf), batch, online


def test_page_order_and_sidebar(tmp_path: Path):
    _idx, plan, wiki, batch, online = _wiki(tmp_path)
    ids = [p.id for p in wiki.pages]
    assert ids[:2] == ["index", "architecture"]
    assert {batch.id, online.id} <= set(ids)
    assert ids[-5:] == ["program-index", "copybook-index", "batch-jobs", "data-stores",
                        "prog-acct01"]
    titles = [s.title for s in wiki.sidebar]
    assert titles[-2:] == ["Reference", "Program Reference"]
    assert "GHOST" not in str(wiki.sidebar)  # leaf docs of unknown programs are dropped
    assert wiki.project_name == plan.project_title


def test_link_resolution(tmp_path: Path):
    _idx, _plan, wiki, batch, online = _wiki(tmp_path)
    overview = wiki.get_page("index").content
    assert "[Architecture](architecture.md)" in overview
    assert "missing" in overview and "[[" not in overview
    assert f"[`MENU01`]({online.id}.md)" in overview  # no leaf doc -> subsystem page
    assert "[`ACCT01`](prog-acct01.md)" in overview  # leaf doc wins, extension stripped
    assert "`NOPE`" in overview and "NOPE`](" not in overview

    arch = wiki.get_page("architecture").content
    assert "[`ACCTJOB`](batch-jobs.md)" in arch and "`NOJOB`" in arch
    assert "[`ACCTFILE`](data-stores.md)" in arch  # DD alias resolves to its store
    assert "[`PROD.ACCT.MASTER`](data-stores.md)" in arch
    assert "`UNKNOWN`" in arch and "UNKNOWN`](" not in arch
    assert "[`ACCTREC`](copybook-index.md)" in arch and "NOCOPY`](" not in arch
    assert 'A["MENU01"]' in arch  # placeholders are stripped inside mermaid

    page = wiki.get_page(batch.id).content
    assert "Self [`ACCT01`](prog-acct01.md)" in page
    # footer: leaf-doc links where they exist, plain names for programs on this very page
    assert "**Programs on this page:** [`ACCT01`](prog-acct01.md), `ACCT02`, `DATEUTIL`" in page


def test_leaf_doc_page(tmp_path: Path):
    _idx, _plan, wiki, batch, _online = _wiki(tmp_path)
    leaf = wiki.get_page(leaf_page_id("ACCT01")).content
    lines = leaf.split("\n")
    assert lines[0] == "# ACCT01 — Posting"
    assert lines[2].startswith(f"> Subsystem: [Batch Posting]({batch.id}.md)")
    assert "Type: batch (JCL)" in lines[2]
    assert "[`DATEUTIL`](" in leaf


def test_appendix_pages(tmp_path: Path):
    idx, _plan, wiki, batch, _online = _wiki(tmp_path)
    pindex = wiki.get_page("program-index").content
    assert pindex.count("\n| [`") == len(idx.programs)
    assert "Posts balances nightly." in pindex
    assert f"[Batch Posting]({batch.id}.md)" in pindex
    jobs = wiki.get_page("batch-jobs").content
    assert "`ACCTJOB`" in jobs and "STEP01: ACCT01; STEP02: ACCT02" in jobs
    assert "`PROD.ACCT.MASTER`" in jobs
    stores = wiki.get_page("data-stores").content
    assert "`PROD.ACCT.MASTER`" in stores and "`ACCTFILE`" in stores
    copies = wiki.get_page("copybook-index").content
    assert "`ACCTREC`" in copies and "ACCT-RECORD" in copies


def test_no_leafdocs_links_to_subsystems(tmp_path: Path):
    _idx, _plan, wiki, batch, _online = _wiki(tmp_path, with_leaf=False)
    assert not any(p.id.startswith("prog-") for p in wiki.pages)
    assert f"[`ACCT01`]({batch.id}.md)" in wiki.get_page("index").content


def test_exporters_resolve_every_link(tmp_path: Path):
    _idx, _plan, wiki, _b, _o = _wiki(tmp_path)
    out = tmp_path / "md"
    export_markdown(wiki, out)
    files = {p.name for p in out.glob("*.md")}
    for page in wiki.pages:
        assert f"{page.id}.md" in files
        for target in re.findall(r"\]\(([^)]+\.md)\)", page.content):
            assert target in files, (page.id, target)
    html_path = tmp_path / "wiki.html"
    export_html(wiki, html_path, mermaid_js="/* mermaid */")
    html = html_path.read_text()
    ids = {p.id for p in wiki.pages}
    for target in set(re.findall(r"showPage\('([^']+)'\)", html)):
        assert target in ids
    assert html.count('class="wiki-page"') == len(wiki.pages)


def test_ungenerated_planned_page_is_plain_title(tmp_path: Path):
    idx = build(flat_repo(tmp_path / "repo"))
    digests = build_digests(idx)
    plan = build_skeleton(idx, digests, max_programs=3)
    online = next(p for p in plan.all_pages() if "MENU01" in p.programs)
    online.title = "Online Menu"
    docs = [PageDoc(id="index", title="Overview",
                    markdown=f"# Demo\n\nSee [[page:{online.id}]] and [[prog:MENU01]].")]
    wiki = assemble(plan, docs, idx, digests)
    text = wiki.get_page("index").content
    assert "See Online Menu and `MENU01`." in text  # no dead links to unwritten pages
    assert wiki.get_page(online.id) is None


# -- extend(): the COBOL pages of a deepdoc-assembled wiki ----------------------------------


def _deep(tmp_path: Path, languages=("cobol",)):
    from codocs.cobol.wiki.assembler import protect_links
    from codocs.cobol.wiki.planner import merge_into
    from codocs.config import Settings
    from codocs.lang.wikiindex import MultiLangIndex
    from codocs.repowiki.deepdoc.assembler import assemble as deep_assemble
    from codocs.repowiki.deepdoc.models import PageDoc as DeepDoc
    from codocs.repowiki.deepdoc.planner import build_skeleton as deep_skeleton
    from codocs.wiki import build_digests as deep_digests

    repo = flat_repo(tmp_path / "repo")
    idx = MultiLangIndex.build(repo, None, ["cobol"], Settings())
    digests, _ = deep_digests(idx, repo, tmp_path / "out")
    plan = merge_into(deep_skeleton(idx, digests), idx)
    page = plan.all_pages()[0]
    docs = [DeepDoc(id="index", title="Overview",
                    markdown="# Demo\n\nSee [[page:program-index]] and [[page:architecture]]."),
            DeepDoc(id="architecture", title="Architecture", markdown="# Architecture\n"),
            DeepDoc(id=page.id, title=page.title,
                    markdown="# Page\n\nRuns [[prog:ACCT01]] via [[job:ACCTJOB]].")]
    wiki = deep_assemble(plan, protect_links(docs, {d.id for d in docs}), idx, digests)
    return idx, plan, docs, wiki, page


def test_extend_adds_program_pages_and_appendix(tmp_path: Path):
    import json

    from codocs.cobol.wiki.assembler import extend

    idx, plan, docs, wiki, page = _deep(tmp_path)
    assert wiki.get_page("package-index") is not None          # deepdoc's appendix
    assert "[[cpage:program-index]]" in wiki.get_page("index").content
    leaf = {"ACCT01": LeafDoc(program_id="ACCT01",
                              markdown="## ClassDef ACCT01\n**ACCT01**: Posts balances.\n",
                              purpose="Posts balances.")}
    out = extend(wiki, plan, docs, idx.cobol_index, idx.cobol_programs, leaf, ["cobol"],
                 digests=idx.cobol_digests, grounding={page.id: ["dropped edge X --> Y"]},
                 wiki_dir=tmp_path / "wiki")
    ids = [p.id for p in out.pages]
    assert "package-index" not in ids                           # COBOL-only: program-index
    assert ids[-5:] == ["program-index", "copybook-index", "batch-jobs", "data-stores",
                        "prog-acct01"]
    assert [s.title for s in out.sidebar][-2:] == ["Reference", "Programs"]
    assert "[Program Index](program-index.md)" in out.get_page("index").content
    body = out.get_page(page.id).content
    assert "[`ACCT01`](prog-acct01.md)" in body and "(batch-jobs.md)" in body
    assert "**Programs on this page:**" in body
    prog = out.get_page("prog-acct01").content
    assert prog.startswith("# ACCT01") and "Subsystem: [" in prog and "Posts balances." in prog
    grounding = json.loads((tmp_path / "wiki" / "grounding.json").read_text())
    assert grounding == {page.id: ["dropped edge X --> Y"]}


def test_extend_keeps_package_index_in_mixed_wikis(tmp_path: Path):
    from codocs.cobol.wiki.assembler import extend

    idx, plan, docs, wiki, _page = _deep(tmp_path)
    out = extend(wiki, plan, docs, idx.cobol_index, idx.cobol_programs, {}, ["c", "cobol"])
    ids = [p.id for p in out.pages]
    assert "package-index" in ids and "program-index" in ids
    assert not any(i.startswith("prog-") for i in ids)          # no docs, no program pages
