"""tests for the deepdoc page planner and wiki assembly."""

import networkx as nx

from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.deepdoc.assembler import assemble
from codocs.repowiki.deepdoc.models import (
    DigestEntry,
    GoPackage,
    PackageDigest,
    PageDoc,
    PagePlan,
    SectionPlan,
    WikiPlan,
)
from codocs.repowiki.deepdoc.planner import build_skeleton, validate_refined_plan


def _index(packages: dict[str, int]) -> GoModuleIndex:
    """a synthetic GoModuleIndex with the given {path: loc} packages."""
    idx = GoModuleIndex.__new__(GoModuleIndex)
    idx.root = None
    idx.module = "demo"
    idx.packages = {p: GoPackage(path=p, name=p.split("/")[-1], loc=loc) for p, loc in packages.items()}
    idx.graph = nx.DiGraph()
    idx.graph.add_nodes_from(packages)
    return idx


def _digests(packages: dict[str, int], chars_per_pkg: int = 100) -> dict[str, PackageDigest]:
    return {
        p: PackageDigest(
            package=p,
            entries=[DigestEntry(name="X", kind="func", summary="s" * chars_per_pkg)],
        )
        for p in packages
    }


def test_small_groups_get_one_page_each():
    pkgs = {"alpha": 10, "alpha/sub": 5, "beta": 20}
    plan = build_skeleton(_index(pkgs), _digests(pkgs))
    assert {s.title for s in plan.sections} == {"alpha", "beta"}
    alpha = next(s for s in plan.sections if s.title == "alpha")
    assert len(alpha.pages) == 1
    assert alpha.pages[0].packages == ["alpha", "alpha/sub"]


def test_large_group_splits_and_preserves_coverage():
    pkgs = {f"big/sub{i}/p{j}": 100 for i in range(4) for j in range(10)}
    pkgs["big"] = 100
    digests = _digests(pkgs, chars_per_pkg=5_000)
    plan = build_skeleton(_index(pkgs), digests, page_budget=60_000, merge_budget=15_000)
    pages = plan.all_pages()
    assert len(pages) > 1
    covered = sorted(p for pg in pages for p in pg.packages)
    assert covered == sorted(pkgs)
    # every page respects the budget when it has more than one package
    for pg in pages:
        cost = sum(5_000 + 90 for _ in pg.packages)
        assert cost <= 60_000 + 5_100 or len(pg.packages) == 1


def test_sections_ordered_by_size():
    pkgs = {"tiny": 1, "huge": 100_000, "mid": 500}
    plan = build_skeleton(_index(pkgs), _digests(pkgs))
    assert [s.title for s in plan.sections] == ["huge", "mid", "tiny"]


def _skeleton() -> WikiPlan:
    return WikiPlan(
        project_title="demo",
        sections=[
            SectionPlan(
                id="a",
                title="a",
                pages=[
                    PagePlan(id="a-core", title="a core", packages=["a", "a/x"]),
                    PagePlan(id="a-y", title="a/y", packages=["a/y"]),
                ],
            )
        ],
    )


def test_validate_refined_plan_accepts_retitles_but_keeps_packages():
    skeleton = _skeleton()
    refined = WikiPlan(
        project_title="Demo System",
        sections=[
            SectionPlan(
                id="core",
                title="Core Services",
                pages=[
                    PagePlan(id="a-y", title="The Y Subsystem (a/y)", packages=["hacked"]),
                    PagePlan(id="a-core", title="A Core (a)", packages=["a", "a/x"]),
                ],
            )
        ],
    )
    result = validate_refined_plan(skeleton, refined)
    assert result is refined
    pages = {p.id: p for p in result.all_pages()}
    assert pages["a-y"].packages == ["a/y"]  # package tampering reverted
    assert pages["a-y"].title == "The Y Subsystem (a/y)"


def test_validate_refined_plan_rejects_dropped_or_unknown_pages():
    skeleton = _skeleton()
    dropped = WikiPlan(
        sections=[
            SectionPlan(id="a", title="a", pages=[PagePlan(id="a-core", title="t", packages=[])])
        ]
    )
    assert validate_refined_plan(skeleton, dropped) is skeleton

    unknown = WikiPlan(
        sections=[
            SectionPlan(
                id="a",
                title="a",
                pages=[
                    PagePlan(id="a-core", title="t", packages=[]),
                    PagePlan(id="mystery", title="m", packages=[]),
                ],
            )
        ]
    )
    assert validate_refined_plan(skeleton, unknown) is skeleton


def test_assemble_resolves_cross_links_and_builds_index():
    pkgs = {"a": 10, "a/x": 5, "a/y": 7}
    idx = _index(pkgs)
    digests = _digests(pkgs)
    plan = _skeleton()
    docs = [
        PageDoc(id="index", title="Overview", markdown="# Demo\n\nSee [[page:a-y]] and [[pkg:a/x]]."),
        PageDoc(id="architecture", title="Architecture", markdown="# Architecture\n\n[[pkg:unknown/pkg]]"),
        PageDoc(id="a-core", title="a core", markdown="# a core\n\nUses [[pkg:a/y]]. Self ref [[pkg:a/x]]."),
        PageDoc(id="a-y", title="a/y", markdown="# a/y\n\nBack to [[page:index]]."),
    ]
    wiki = assemble(plan, docs, idx, digests)

    overview = wiki.get_page("index")
    assert "[a/y](a-y.md)" in overview.content
    assert "[`a/x`](a-core.md)" in overview.content

    arch = wiki.get_page("architecture")
    assert "`unknown/pkg`" in arch.content and "](.md)" not in arch.content

    a_core = wiki.get_page("a-core")
    assert "[`a/y`](a-y.md)" in a_core.content
    assert "Self ref `a/x`" in a_core.content  # same-page links degrade to code

    pkg_index = wiki.get_page("package-index")
    assert pkg_index is not None
    assert "| `a/y` |" in pkg_index.content
    # sidebar: Overview, Architecture, one section, Package Index
    assert [s.title for s in wiki.sidebar] == ["Overview", "Architecture", "a", "Package Index"]


def test_assemble_strips_backticks_around_placeholders():
    pkgs = {"a": 10, "a/x": 5, "a/y": 7}
    plan = _skeleton()
    docs = [
        PageDoc(id="index", title="Overview", markdown="# D\n\n| `[[pkg:a/y]]` | x |"),
        PageDoc(id="a-core", title="a core", markdown="# a core"),
        PageDoc(id="a-y", title="a/y", markdown="# a/y"),
    ]
    wiki = assemble(plan, docs, _index(pkgs), _digests(pkgs))
    assert "| [`a/y`](a-y.md) | x |" in wiki.get_page("index").content
