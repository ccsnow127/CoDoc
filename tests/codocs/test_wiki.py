from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codocs.config import Settings
from codocs.wiki import build_digests, run_wiki

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"

OUT_DOC = """# calc/stats.go

**Package**: `calc` (`example.com/fixture/calc`)

Accumulates observations.

## ClassDef Stats
**Stats**: running count, total and maximum of observed integers.

## MethodDef Stats.Add
**Stats.Add**: records one observation from the generated docs.
"""

TREE_DOC_CALC = """# calc.go

## FunctionDef Sum
**Sum**: in-tree description that must lose against OUT/docs.
"""

TREE_DOC_SHAPES = """# shapes.go

## FunctionDef Classify
**Classify**: buckets an area into empty, small or large.
"""


@pytest.fixture
def module(tmp_path: Path) -> Path:
    mod = tmp_path / "gomod"
    shutil.copytree(FIXTURE, mod)
    for pkg, text in (("calc", TREE_DOC_CALC), ("shapes", TREE_DOC_SHAPES)):
        d = mod / pkg / "markdown_docs"
        d.mkdir()
        (d / f"{pkg}.md").write_text(text, encoding="utf-8")
    return mod


@pytest.fixture
def out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    d = out / "docs" / "calc" / "markdown_docs"
    d.mkdir(parents=True)
    (d / "stats.md").write_text(OUT_DOC, encoding="utf-8")
    return out


async def test_plan_only_out_docs_win(module: Path, out: Path):
    res = await run_wiki(module, out, Settings(), plan_only=True)
    assert res["packages"] == 2
    assert (res["leaf_out"], res["leaf_tree"], res["static"]) == (1, 1, 0)
    assert res["pages"] >= 1 and res["html"] is None
    assert res["tokens"] == {"input": 0, "output": 0}
    assert not (out / "wiki").exists()


async def test_plan_only_without_tree_docs(module: Path, out: Path):
    res = await run_wiki(module, out, Settings(), use_tree_docs=False, plan_only=True)
    assert (res["leaf_out"], res["leaf_tree"], res["static"]) == (1, 0, 1)


async def test_plan_only_static_only(tmp_path: Path):
    res = await run_wiki(FIXTURE, tmp_path / "none", Settings(), plan_only=True)
    assert (res["leaf_out"], res["leaf_tree"], res["static"]) == (0, 0, 2)


async def test_subtree(module: Path, out: Path):
    res = await run_wiki(module, out, Settings(), subtree="shapes", plan_only=True)
    assert res["packages"] == 1
    assert (res["leaf_out"], res["leaf_tree"], res["static"]) == (0, 1, 0)


def test_digest_priority(module: Path, out: Path):
    from codocs.repowiki.analysis.golang import GoModuleIndex

    index = GoModuleIndex.build(module)
    digests, counts = build_digests(index, module, out)
    assert counts == {"leaf_out": 1, "leaf_tree": 1, "static": 0, "mixed": 2}
    calc = {e.name: e.summary for e in digests["calc"].entries}
    assert "Stats.Add" in calc and "generated docs" in calc["Stats.Add"]
    assert "in-tree description" in calc["Sum"]
    assert [e.name for e in digests["calc"].entries][:3] == ["Stats", "Stats.Add", "Sum"]
    assert digests["shapes"].source == "leafdocs"
    assert any(e.name == "Classify" for e in digests["shapes"].entries)


async def test_out_dir_inside_module_ignored(module: Path):
    out = module / "out"
    d = out / "docs" / "calc" / "markdown_docs"
    d.mkdir(parents=True)
    (d / "stats.md").write_text(OUT_DOC, encoding="utf-8")
    suite = out / "tests" / "calc"
    suite.mkdir(parents=True)
    (suite / "stats_codocs_test.go").write_text("package calc\n", encoding="utf-8")
    (out / "tests" / "extra.go").write_text("package tests\n\nfunc X() {}\n", encoding="utf-8")
    res = await run_wiki(module, out, Settings(), plan_only=True)
    assert res["packages"] == 2
    assert res["leaf_out"] == 1


async def test_no_packages(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/empty\n\ngo 1.21\n")
    with pytest.raises(ValueError):
        await run_wiki(tmp_path, tmp_path / "out", Settings(), plan_only=True)


async def test_full_flow_with_fake_generator(module: Path, out: Path, monkeypatch):
    import codocs.repowiki.core.cache as cache_mod
    import codocs.repowiki.deepdoc.generator as gen_mod
    import codocs.repowiki.llm.client as client_mod
    from codocs.repowiki.deepdoc.models import PageDoc

    seen: dict = {}

    class FakeCache:
        def __init__(self, db_path=None):
            seen["cache_path"] = db_path

        async def init(self):
            seen["cache"] = "open"

        async def close(self):
            seen["cache"] = "closed"

    class FakeClient:
        def __init__(self, model, api_key="", api_base=""):
            seen["model"] = model
            self.total_input_tokens = 123
            self.total_output_tokens = 45

    class FakeGenerator:
        def __init__(self, index, digests, llm, cache, language="en", concurrency=4,
                     on_progress=None):
            seen.update(language=language, concurrency=concurrency,
                        sources={k: d.source for k, d in digests.items()})

        async def refine_plan(self, skeleton):
            return skeleton

        async def generate(self, plan):
            docs = [PageDoc(id="index", title="Overview", markdown="# Overview\n"),
                    PageDoc(id="architecture", title="Architecture", markdown="# Arch\n")]
            docs += [PageDoc(id=p.id, title=p.title, markdown=f"# {p.title}\n")
                     for p in plan.all_pages()]
            return docs

    monkeypatch.setattr(cache_mod, "Cache", FakeCache)
    monkeypatch.setattr(client_mod, "LLMClient", FakeClient)
    monkeypatch.setattr(gen_mod, "DeepDocGenerator", FakeGenerator)

    settings = Settings(model="m-test", language="ja", concurrency=2)
    msgs: list[str] = []
    res = await run_wiki(module, out, settings, on_progress=msgs.append)
    assert seen["model"] == "m-test" and seen["cache"] == "closed"
    assert Path(seen["cache_path"]).name == "wiki-m-test-ja.db"
    assert seen["language"] == "ja" and seen["concurrency"] == 2
    assert seen["sources"] == {"calc": "leafdocs", "shapes": "leafdocs"}
    assert res["tokens"] == {"input": 123, "output": 45}
    wiki = out / "wiki"
    assert (wiki / "index.md").is_file() and (wiki / "package-index.md").is_file()
    assert res["html"] and Path(res["html"]).is_file()
    assert Path(res["html"]).parent == wiki
    assert msgs

    res = await run_wiki(module, tmp := out.parent / "out2", settings, fmt="markdown")
    assert res["html"] is None and not list(tmp.joinpath("wiki").glob("*.html"))


async def test_packages_narrow_the_wiki(module: Path, out: Path):
    res = await run_wiki(module, out, Settings(), packages=["calc"], plan_only=True)
    assert res["packages"] == 1
    with pytest.raises(ValueError, match="calcc"):
        await run_wiki(module, out, Settings(), packages=["calcc"], plan_only=True)


def test_partial_out_docs_keep_static_symbols(module: Path, out: Path):
    from codocs.repowiki.analysis.golang import GoModuleIndex

    index = GoModuleIndex.build(module)
    digests, counts = build_digests(index, module, out, use_tree_docs=False)
    names = [e.name for e in digests["calc"].entries]
    assert names[:2] == ["Stats", "Stats.Add"]
    assert {"Clamp", "Normalize", "Sum", "Stats.Mean"} <= set(names)
    assert len(names) == len(set(names))
    assert digests["calc"].source == "leafdocs"
    assert counts == {"leaf_out": 1, "leaf_tree": 0, "static": 1, "mixed": 1}


def test_wiki_cache_is_per_model_and_language(tmp_path: Path):
    from codocs.wiki import wiki_cache_path

    base = Settings(cache_path=str(tmp_path / "c" / "cache.db"))
    en = wiki_cache_path(base)
    ja = wiki_cache_path(Settings(cache_path=base.cache_path, language="ja"))
    mini = wiki_cache_path(Settings(cache_path=base.cache_path, model="gpt-5.4-mini"))
    assert len({en, ja, mini}) == 3
    assert en.parent == tmp_path / "c"
