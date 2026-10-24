"""COBOL in the bizlogic graph: nodes, edges, roots, utility keys, scope, COBOL-to-C linker."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from wiki_fixtures import fx
from wiki_index_fixtures import (
    cobol_doc,
    falling_program,
    flow_program,
    shared_para_repo,
    started_program,
    write_doc,
)

from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.graph import build_graph
from codocs.bizlogic.walk import root_candidates
from codocs.bizlogic.xlang import EXTRA_PLUMBING, LINKERS, TAGS, CobolCLinker, tag
from codocs.cobol.facts import PLUMBING_PARAGRAPH

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _edges(fg) -> dict[tuple[str, str], dict]:
    return {(u, v): d for u, v, d in fg.graph.edges(data=True)}


def test_cobolmod_nodes_and_edges(cobolmod_copy: Path):
    fg = build_graph(cobolmod_copy, languages=["cobol"])
    nodes = fg.nodes
    assert {"cbl::ACCTRPT", "cbl::DATEUTIL", "cbl::DAYS-IN-MONTH", "cbl::ACCTINQ",
            "jcl::JOB.ACCTJOB", "jcl::JOB.ACCTJOB.STEP010", "cbl::TXN.AINQ"} <= set(nodes)
    assert nodes["cbl::ACCTRPT"].kind == "func" and nodes["cbl::ACCTRPT"].lang == "cobol"
    para = nodes["cbl::DATEUTIL.1000-CHECK-MONTH"]
    assert para.kind == "method" and para.file == "cbl/DATEUTIL.cbl" and para.line == 29
    assert nodes["jcl::JOB.ACCTJOB.STEP010"].line == 3
    # the trivial EXIT paragraph and the test program are no nodes
    assert "cbl::DATEUTIL.9999-EXIT" not in nodes
    assert not any(n.startswith("test::") for n in nodes)
    e = _edges(fg)
    assert ("cbl::DATEUTIL", "cbl::DATEUTIL.0000-MAIN") in e          # no main-line code
    assert ("cbl::DATEUTIL.0000-MAIN", "cbl::DATEUTIL.3000-LEAP") in e
    assert e[("cbl::ACCTRPT.3000-PROCESS", "cbl::DATEUTIL")]["xfer"] == "call"
    assert e[("cbl::ACCTINQ.2000-READ-ACCT", "cbl::DATEUTIL")]["xfer"] == "link"
    assert e[("cbl::DATEUTIL.2000-CHECK-DAY", "cbl::DAYS-IN-MONTH")]["xfer"] == "function"
    assert ("jcl::JOB.ACCTJOB", "jcl::JOB.ACCTJOB.STEP010") in e
    assert ("jcl::JOB.ACCTJOB.STEP010", "cbl::ACCTRPT") in e
    assert ("cbl::TXN.AINQ", "cbl::ACCTINQ") in e
    stats = fg.stats["languages"]["cobol"]
    assert stats["jobs"] == 1 and stats["transactions"] == 1 and stats["files"] == 4
    # JCL jobs and CICS transactions are the roots; programs they start are called
    roots = [r for r, _ in root_candidates(fg)]
    assert roots == ["jcl::JOB.ACCTJOB", "cbl::TXN.AINQ"]
    assert tag(nodes["cbl::ACCTRPT"]) == " [COBOL]" and TAGS["cobol"] == "COBOL"


def test_flow_edges_thru_goto_sections_start(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "FLOWPGM.cbl").write_text(flow_program())
    (repo / "FLOW2.cbl").write_text(started_program())
    fg = build_graph(repo, languages=["cobol"])
    e = _edges(fg)
    # PERFORM A THRU C runs A, B (C is a trivial EXIT paragraph: no node)
    assert ("::FLOWPGM", "::FLOWPGM.A-PARA") in e and ("::FLOWPGM", "::FLOWPGM.B-PARA") in e
    assert "::FLOWPGM.C-PARA" not in fg.nodes
    # GO TO ... DEPENDING splits its weight
    assert e[("::FLOWPGM", "::FLOWPGM.D-PARA")]["weight"] == 0.5
    assert e[("::FLOWPGM", "::FLOWPGM.E-PARA")]["weight"] == 0.5
    # a PERFORMed section runs its paragraphs
    assert ("::FLOWPGM", "::FLOWPGM.WORK-SEC") in e
    assert ("::FLOWPGM.WORK-SEC", "::FLOWPGM.W1-PARA") in e
    assert ("::FLOWPGM.WORK-SEC", "::FLOWPGM.W2-PARA") in e
    # EXEC CICS START is a reference to the started program
    assert e[("::FLOWPGM.E-PARA", "::FLOW2")]["kind"] == "ref"
    assert e[("::TXN.FLW2", "::FLOW2")]["kind"] == "call"
    # plumbing paragraphs (abend routines) are utilities
    assert "::FLOWPGM.9999-ABEND-PROGRAM" in fg.utilities
    assert EXTRA_PLUMBING["cobol"] is PLUMBING_PARAGRAPH


def test_fall_through_and_alter(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "FALLPGM.cbl").write_text(falling_program())
    fg = build_graph(repo, languages=["cobol"])
    e = _edges(fg)
    assert ("::FALLPGM", "::FALLPGM.P1-PARA") in e           # the main line runs on
    assert ("::FALLPGM.P1-PARA", "::FALLPGM.P2-PARA") in e   # over the EXIT paragraph
    assert not any(u == "::FALLPGM.P2-PARA" for u, _v in e)  # STOP RUN ends the sequence
    assert ("::FALLPGM.P3-PARA", "::FALLPGM.P5-PARA") in e   # GO TO
    assert ("::FALLPGM.P3-PARA", "::FALLPGM.P4-PARA") in e   # ALTER P3 TO PROCEED TO P4


def test_qualified_utility_keys(tmp_path: Path):
    fg = build_graph(shared_para_repo(tmp_path / "repo", 42), languages=["cobol"])
    inits = [n for n in fg.nodes if n.endswith(".1000-INIT")]
    assert len(inits) == 42
    assert not any(n in fg.utilities for n in inits)       # same name, different programs
    assert "::HUBPGM" in fg.utilities                        # 42 callers: a real hub


def test_exclude_paths_reach_the_cobol_index(cobolmod_copy: Path):
    fg = build_graph(cobolmod_copy, languages=["cobol"], exclude_paths=("cbl/ACCTINQ.cbl",))
    assert not any("ACCTINQ" in n for n in fg.nodes)
    assert "cbl::TXN.AINQ" not in fg.nodes
    whole = build_graph(cobolmod_copy, languages=["cobol"])
    assert "cbl::ACCTINQ" in whole.nodes


def test_cobol_c_linker_without_go(mixedcobol_copy: Path):
    assert CobolCLinker in LINKERS
    fg = build_graph(mixedcobol_copy, languages=["cobol", "c", "python"])
    e = _edges(fg)
    d = e[("cobol::ACCTRPT.3000-PROCESS", "clib::fmtline")]
    assert d["xlang"] == "cobol-c" and d["kind"] == "call"
    assert fg.stats["cross_edges"] == {"cobol_c_calls": 1, "cobol_c_unresolved": 0}
    assert e[("cobol::ACCTRPT.4000-TOTALS", "mixed::NUMPGM")]["xfer"] == "call"
    roots = [r for r, _ in root_candidates(fg)]
    assert [r for r in roots if r.startswith(("cobol::", "mixed::"))] == ["cobol::ACCTRPT"]
    # the C function nobody calls is a node of the mixed directory as well
    assert "mixed::fmtnum" in fg.nodes and fg.nodes["mixed::fmtnum"].lang == "c"


def test_cobol_c_linker_needs_exact_spelling(mixedcobol_copy: Path):
    src = mixedcobol_copy / "cobol" / "ACCTRPT.cbl"
    src.write_text(src.read_text().replace("CALL 'fmtline'", "CALL 'FMTLINE'"))
    fg = build_graph(mixedcobol_copy, languages=["cobol", "c"])
    assert not any(d.get("xlang") == "cobol-c" for _u, _v, d in fg.graph.edges(data=True))
    assert fg.stats["cross_edges"]["cobol_c_unresolved"] == 1


def test_other_languages_unaffected_by_cobol(mixedcobol_copy: Path, tmp_path: Path):
    plain = tmp_path / "plain"
    shutil.copytree(mixedcobol_copy, plain)
    for p in list(plain.rglob("*.cbl")) + list(plain.rglob("*.cpy")):
        p.unlink()
    a = build_graph(mixedcobol_copy, languages=["c", "python"])
    b = build_graph(plain, languages=["c", "python"])
    assert sorted(a.nodes) == sorted(b.nodes)
    assert sorted(a.graph.edges) == sorted(b.graph.edges)
    assert a.stats == b.stats and a.stats["cross_edges"] == {}


def test_docs_attach_to_programs_and_paragraphs(cobolmod_copy: Path, tmp_path: Path):
    out = tmp_path / "out"
    write_doc(out, "cbl/DATEUTIL.cbl", cobol_doc(
        "cbl/DATEUTIL.cbl", "DATEUTIL", "Validates a YYYYMMDD date.",
        {"1000-CHECK-MONTH": "Sets LK-RC to '10' when the month is outside 1-12."}))
    fg = build_graph(cobolmod_copy, languages=["cobol"], docs=DocIndex.build([out / "docs"]))
    assert fg.nodes["cbl::DATEUTIL"].summary.startswith("Validates a YYYYMMDD date")
    assert "month is outside" in fg.nodes["cbl::DATEUTIL.1000-CHECK-MONTH"].summary


def test_run_bizlogic_plan_only_with_exclude_paths(cobolmod_copy: Path, tmp_path: Path):
    from codocs.bizlogic.run import BizParams, run_bizlogic
    from codocs.config import Settings

    out = tmp_path / "out"
    settings = Settings(source_lang="cobol", exclude_paths="cbl/ACCTINQ.cbl")
    res = asyncio.run(run_bizlogic(cobolmod_copy, out, settings, BizParams(clusters=(2,)),
                                   plan_only=True))
    assert res is not None
    graph = json.loads((out / "bizlogic" / "graph.json").read_text())
    assert graph["exclude_paths"] == ["cbl/ACCTINQ.cbl"]
    assert graph["stats"]["languages"]["cobol"]["transactions"] == 0
    assert [r["id"] for r in graph["roots"]] == ["jcl::JOB.ACCTJOB"]


def test_generated_programs_are_no_nodes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "GENPGM.cbl").write_text(fx(
        "* THIS PROGRAM WAS GENERATED BY A TOOL - DO NOT EDIT",
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. GENPGM.", "PROCEDURE DIVISION.",
        "MAIN-PARA.", "    STOP RUN."))
    (repo / "REAL.cbl").write_text(fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. REAL.", "PROCEDURE DIVISION.",
        "MAIN-PARA.", "    STOP RUN."))
    fg = build_graph(repo, languages=["cobol"])
    assert "::REAL" in fg.nodes and "::GENPGM" not in fg.nodes


def test_carddemo_graph_matches_the_index(datasets):
    import pytest

    from codocs.cobol.index import CobolRepoIndex

    carddemo = datasets("carddemo")
    if carddemo is None:
        pytest.skip("CardDemo clone not available")
    ex = ("app/data", "app/scheduler", "app/catlg")
    idx = CobolRepoIndex.build(carddemo, subtree="app", exclude=list(ex))
    fg = build_graph(carddemo, subtree="app", languages=["cobol"], exclude_paths=ex)
    entries = {n.name for n in fg.nodes.values() if "." not in n.name}
    assert entries == set(idx.programs)
    program_of = {nid: n.name.split(".", 1)[0] for nid, n in fg.nodes.items()
                  if not n.name.startswith(("JOB.", "TXN."))}
    transfers = {(program_of[u], fg.nodes[v].name) for u, v, d in fg.graph.edges(data=True)
                 if d.get("xfer") or d["kind"] == "ref"}
    assert transfers == set(idx.program_graph().edges)
    roots = [fg.nodes[r].name for r, _ in root_candidates(fg)]
    assert sum(r.startswith("TXN.") for r in roots) == 24
    assert sum(r.startswith("JOB.") for r in roots) >= 30
    assert "CBTRN01C" in roots                      # the standalone main program
