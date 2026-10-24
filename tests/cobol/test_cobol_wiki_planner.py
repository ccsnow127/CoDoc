"""tests for codocs.cobol.wiki.planner (skeleton grouping, digests, refined-plan validation)."""

from __future__ import annotations

from pathlib import Path

import pytest
from wiki_fixtures import build, dir_repo, flat_repo

from codocs.cobol.docs import LeafDoc
from codocs.cobol.wiki.models import PagePlan, SectionPlan, WikiPlan
from codocs.cobol.wiki.planner import (
    RESERVED_IDS,
    _chunk,
    _prefix_groups,
    build_digests,
    build_skeleton,
    program_role,
    role_label,
    validate_refined_plan,
    weighted_graph,
)


def _assigned(plan: WikiPlan) -> list[str]:
    return sorted(p for page in plan.all_pages() for p in page.programs)


def test_roles_and_labels(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    assert program_role(idx, "MENU01") == "online"
    assert program_role(idx, "ACCT01") == "batch"
    assert program_role(idx, "DATEUTIL") == "subprogram"
    assert role_label(idx, "ACCT01") == "batch (JCL)"
    assert role_label(idx, "VIEW01") == "CICS online"


def test_weighted_graph_weights(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    g = weighted_graph(idx)
    # call (3) + same job (2) + shared dataset (1); copybook ACCTREC is used by 2 of 6 -> ubiquitous
    assert g.edges["ACCT01", "DATEUTIL"]["weight"] == 3
    assert g.edges["ACCT01", "ACCT02"]["weight"] >= 2 + 1
    assert g.edges["MENU01", "VIEW01"]["weight"] >= 6  # XCTL in both directions


def test_small_repo_is_one_page(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    plan = build_skeleton(idx, build_digests(idx))
    assert len(plan.all_pages()) == 1
    assert plan.sections[0].id == "application"
    assert _assigned(plan) == sorted(idx.programs)


def test_graph_mode_splits_online_and_batch(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    plan = build_skeleton(idx, build_digests(idx), max_programs=3)
    assert _assigned(plan) == sorted(idx.programs)  # every program exactly once
    by_section = {s.id: [sorted(p.programs) for p in s.pages] for s in plan.sections}
    assert by_section["online"] == [["MENU01", "UPDT01", "VIEW01"]]
    assert by_section["batch"] == [["ACCT01", "ACCT02", "DATEUTIL"]]
    assert all(len(p.programs) <= 3 for p in plan.all_pages())


def test_directory_mode_absorbs_singleton_folder(tmp_path: Path):
    idx = build(dir_repo(tmp_path))
    plan = build_skeleton(idx, build_digests(idx))
    sections = {s.id: s for s in plan.sections}
    assert set(sections) == {"billing", "cards", "users"}
    # SOLO01 lives alone in lonely/ but calls BILL01, so it joins billing
    assert sections["billing"].pages[0].programs == ["BILL01", "BILL02", "SOLO01"]
    assert _assigned(plan) == sorted(idx.programs)


def test_skeleton_is_deterministic_and_ids_safe(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    digests = build_digests(idx)
    a = build_skeleton(idx, digests, max_programs=2)
    b = build_skeleton(idx, digests, max_programs=2)
    assert a.model_dump() == b.model_dump()
    ids = [p.id for p in a.all_pages()]
    assert len(ids) == len(set(ids))
    for pid in ids:
        assert pid not in RESERVED_IDS and not pid.startswith("prog-")
        assert all(c.isalnum() or c == "-" for c in pid)
    assert all(len(p.programs) <= 2 for p in a.all_pages())
    assert _assigned(a) == sorted(idx.programs)


def test_prefix_groups_and_chunk():
    groups = _prefix_groups(
        ["COCRDLIC", "COCRDSLC", "COCRDUPC", "ZZTOP", "CBTRN01C", "CBTRN02C", "ABC"],
        cost=lambda ps: 0, page_budget=10, max_programs=12,
    )
    assert ["COCRDLIC", "COCRDSLC", "COCRDUPC"] in groups
    assert ["CBTRN01C", "CBTRN02C"] in groups
    assert ["ABC", "ZZTOP"] in groups  # unrelated leftovers share an "other" page
    chunks = _chunk([f"P{i:02d}" for i in range(25)], cost=lambda ps: 0, page_budget=1,
                    max_programs=12)
    assert [len(c) for c in chunks] == [9, 9, 7]


def test_build_digests_prefers_leafdocs(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    leaf = {"ACCT01": LeafDoc(program_id="ACCT01", markdown="# ACCT01\n\nx", purpose="Posts.")}
    digests = build_digests(idx, leaf)
    assert digests["ACCT01"].source == "leafdoc"
    assert digests["ACCT02"].source == "static"
    assert digests["ACCT02"].purpose == "print account report"


def _plan() -> WikiPlan:
    return WikiPlan(project_title="x", sections=[
        SectionPlan(id="a", title="A", pages=[
            PagePlan(id="p1", title="P1", programs=["A1", "A2"]),
            PagePlan(id="p2", title="P2", programs=["B1"]),
        ]),
    ])


def test_validate_refined_plan_keeps_programs():
    refined = WikiPlan(project_title="", project_summary="sum", sections=[
        SectionPlan(id="Online Stuff", title="Online", pages=[
            PagePlan(id="p2", title="Second", programs=["HACK"], workflows=["w"]),
        ]),
        SectionPlan(id="b", title="Batch", pages=[PagePlan(id="p1", title="First")]),
        SectionPlan(id="empty", title="Empty"),
    ])
    out = validate_refined_plan(_plan(), refined)
    assert out.project_title == "x"
    assert [s.id for s in out.sections] == ["online-stuff", "b"]
    pages = {p.id: p for p in out.all_pages()}
    assert pages["p2"].programs == ["B1"] and pages["p2"].title == "Second"
    assert pages["p1"].programs == ["A1", "A2"]


@pytest.mark.parametrize("pages", [
    [PagePlan(id="p1", title="x")],  # dropped p2
    [PagePlan(id="p1", title="x"), PagePlan(id="p1", title="y"), PagePlan(id="p2", title="z")],
    [PagePlan(id="p1", title="x"), PagePlan(id="new", title="y")],
])
def test_validate_refined_plan_rejects_structure_changes(pages):
    skeleton = _plan()
    refined = WikiPlan(sections=[SectionPlan(id="s", title="S", pages=pages)])
    assert validate_refined_plan(skeleton, refined) is skeleton


def test_carddemo_plan_is_sensible(datasets):
    carddemo = datasets("carddemo")
    if carddemo is None:
        pytest.skip("CardDemo clone not available")
    idx = build(carddemo)
    plan = build_skeleton(idx, build_digests(idx))
    sizes = [len(p.programs) for p in plan.all_pages()]
    assert _assigned(plan) == sorted(idx.programs)
    assert max(sizes) <= 12 and min(sizes) >= 2
    assert 6 <= len(sizes) <= 14
    # online menu flows and batch posting land on different pages
    page_of = {p: pg.id for pg in plan.all_pages() for p in pg.programs}
    assert page_of["COSGN00C"] != page_of["CBTRN02C"]
    assert page_of["COCRDLIC"] == page_of["COCRDUPC"]


# -- shared-service hubs --------------------------------------------------------


def test_hub_programs_get_a_shared_services_page(tmp_path: Path):
    from grounding_fixtures import build_chain

    from codocs.cobol.wiki.planner import SHARED_SERVICES, hub_programs

    idx = build_chain(tmp_path)
    assert hub_programs(idx) == {"LOGGR"}  # 8 of 9 programs LINK / CALL it
    g = weighted_graph(idx)
    assert not any(g.has_edge(p, "LOGGR") for p in idx.programs)  # no gluing via the logger
    assert g.has_edge("FRONT01", "BIZ01")
    plan = build_skeleton(idx, build_digests(idx), max_programs=4)
    assert _assigned(plan) == sorted(idx.programs)
    shared = [p for p in plan.all_pages() if "LOGGR" in p.programs]
    assert len(shared) == 1 and shared[0].programs == ["LOGGR"]
    assert shared[0].id == "shared-services" and shared[0].title == SHARED_SERVICES
    others = [p for p in plan.all_pages() if p is not shared[0]]
    assert others and not any("LOGGR" in p.title for p in others)


def test_no_hubs_in_small_flat_repo(tmp_path: Path):
    from codocs.cobol.wiki.planner import hub_programs

    idx = build(flat_repo(tmp_path))
    assert hub_programs(idx) == set()  # DATEUTIL has 2 callers: below HUB_MIN_CALLERS
    plan = build_skeleton(idx, build_digests(idx), max_programs=3)
    assert all(p.id != "shared-services" for p in plan.all_pages())


def test_cluster_title_ignores_hubs():
    import networkx as nx

    from codocs.cobol.wiki.planner import _cluster_title

    g = nx.Graph()
    g.add_edge("LOGGR", "ABC01", weight=9)
    g.add_edge("XYZ02", "ABC01", weight=1)
    assert _cluster_title(["ABC01", "LOGGR", "XYZ02"], "", g, {"LOGGR"}).startswith("ABC01")
    assert _cluster_title(["LGAC01", "LGAC02", "LOGGR"], "", g, {"LOGGR"}) == "LGAC0* programs"


def test_plan_does_not_depend_on_the_hash_seed(datasets):
    # networkx's louvain iterates sets of node labels: with string labels the CardDemo plan
    # changed between processes (9 or 10 pages); integer labels make it reproducible
    import os
    import subprocess
    import sys

    carddemo = datasets("carddemo")
    if carddemo is None:
        pytest.skip("CardDemo clone not available")
    code = (
        "import sys, json\n"
        "from codocs.cobol.index import CobolRepoIndex\n"
        "from codocs.cobol.wiki.planner import build_digests, build_skeleton\n"
        "idx = CobolRepoIndex.build(sys.argv[1], subtree='app',"
        " exclude=['app/data', 'app/scheduler', 'app/catlg'])\n"
        "plan = build_skeleton(idx, build_digests(idx))\n"
        "print(json.dumps([[p.id, p.programs] for p in plan.all_pages()]))\n"
    )
    outs = set()
    for seed in ("1", "2", "3"):
        res = subprocess.run([sys.executable, "-c", code, str(carddemo)], capture_output=True,
                             text=True, env={**os.environ, "PYTHONHASHSEED": seed},
                             check=True)
        outs.add(res.stdout)
    assert len(outs) == 1
