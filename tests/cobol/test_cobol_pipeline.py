"""the multi-mode pipeline with COBOL: program selection, the analysis scope that testrun
and judge restore, `pipeline.run` on the synthetic flat repo with a scripted LLM, skips,
failed stages, the judge conditions, resume, stale wiki pages and a COBOL-only run without
tree-sitter."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from pipeline_fixtures import PipelineLLM, isolate, load
from wiki_fixtures import build, flat_repo
from wiki_index_fixtures import install_fake_llm

from codocs import pipeline as pl
from codocs.cobol.select import parse_program_list, select_programs
from codocs.config import Settings
from codocs.llm import LLM


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return flat_repo(tmp_path / "demo")


@pytest.fixture()
def fake(monkeypatch, tmp_path: Path, repo: Path) -> PipelineLLM:
    isolate(monkeypatch, tmp_path)
    llm = PipelineLLM(repo)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                       validate=None, model=None):
        return llm.reply(system, user, tag)

    monkeypatch.setattr(LLM, "complete", complete)
    return llm


def _settings(tmp_path: Path, **kw) -> Settings:
    base = dict(source_lang="cobol", api_key="test-key", concurrency=3, doc_rounds=1,
                test_rounds=1, mutants=0, cache_path=str(tmp_path / "cache" / "cache.db"))
    base.update(kw)
    return Settings(**base)


def _run(repo, out, settings, **kw) -> dict:
    return asyncio.run(pl.run(repo, out, settings, kw.pop("selection", None), **kw))


# -- program selection (cobol.select) -------------------------------------------------------


def test_parse_program_list():
    assert parse_program_list(None) == []
    assert parse_program_list("A,B  C") == ["A", "B", "C"]
    assert parse_program_list(("A,B", "B", "c")) == ["A", "B", "c"]


def test_select_programs_case_insensitive(repo: Path, caplog):
    index = build(repo)
    assert select_programs(index, []) is None
    assert select_programs(index, ["acct01", "MENU01", "nope"]) == ["ACCT01", "MENU01"]
    assert "nope" in caplog.text
    with pytest.raises(ValueError, match="none of the requested programs"):
        select_programs(index, ["nope"])


def test_selection_to_dict_omits_programs():
    assert "programs" not in pl.Selection().to_dict()
    assert pl.Selection(programs=["A"]).to_dict()["programs"] == ["A"]


# -- analysis: --programs and the restored scope --------------------------------------------


def test_programs_select_their_files_and_scope_is_recorded(repo: Path, tmp_path: Path):
    (repo / "old").mkdir()
    (repo / "old" / "OLD01.cbl").write_text((repo / "ACCT02.cbl").read_text()
                                            .replace("ACCT02", "OLD01"))
    out = tmp_path / "out"
    st = _settings(tmp_path, exclude_paths="old")
    runners = pl.make_runners(repo, st, out)
    a = asyncio.run(pl.analyze_all(runners, st, out,
                                   pl.Selection(programs=["acct01", "dateutil"])))
    assert [t.file for t in a.targets] == ["ACCT01.cbl", "DATEUTIL.cbl"]
    data = load(out / "analysis.json")
    assert data["selection"]["programs"] == ["acct01", "dateutil"]
    assert data["cobol"]["scope"] == {"subtree": None, "exclude_paths": ["old"]}
    assert data["cobol"]["modes"] == {"ACCT01.cbl": "batch", "DATEUTIL.cbl": "unit"}
    assert data["cobol"]["summary"]["programs"] == 6         # OLD01 is excluded
    index = load(out / "cobol" / "index.json")
    assert "OLD01" not in index["programs"] and "ACCT01" in index["programs"]

    # testrun / judge rebuild the index from analysis.json, not from the current settings
    again = pl.cobol_index_from_out(out, Settings(exclude_paths=""))
    assert sorted(again.programs) == sorted(index["programs"])
    with pytest.raises(ValueError, match="none of the requested programs"):
        asyncio.run(pl.analyze_all(runners, st, out, pl.Selection(programs=["NOPE"])))


def test_index_from_out_needs_an_analysis(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="analysis.json"):
        pl.cobol_index_from_out(tmp_path, Settings())


# -- pipeline.run ---------------------------------------------------------------------------


@pytest.mark.needs_cobc
def test_run_end_to_end(repo: Path, tmp_path: Path, fake: PipelineLLM, monkeypatch):
    wiki_llm = install_fake_llm(monkeypatch)
    out = tmp_path / "out"
    meta = _run(repo, out, _settings(tmp_path))
    assert list(meta["stages"]) == ["analyze", "baseline", "testgen", "docgen", "judge", "wiki"]
    assert all("error" not in st for st in meta["stages"].values()), meta["stages"]
    kinds = [k for k, _ in fake.calls]
    assert kinds.count("doc") == 6 and kinds.count("code") == 6 and kinds.count("judge") == 6
    assert kinds.count("suite") == 3 and kinds.count("spec") == 3   # 2 batch, 1 unit, 3 CICS
    assert meta["stages"]["judge"]["programs"] == 6
    for rel in ("analysis.json", "cobol/index.json", "report.md", "summary.json",
                "docs/markdown_docs/ACCT01.cbl.md", "judge/summary.json", "judge/ACCT01.json",
                "testgen/_root/ACCT01.cbl/round_1/suite.cbt",
                "tests/_root/MENU01_codocs_spec.md", "wiki/index.md",
                "wiki/program-index.md", "wiki/prog-acct01.md", "wiki/grounding.json"):
        assert (out / rel).exists(), rel
    assert wiki_llm.calls
    agg = meta["stages"]["docgen"]["by_language"]["cobol"]
    assert agg["files"] == 6 and agg["cobol_mean_best_phi_by_signal"]["facts"] == 1.0
    tg = meta["stages"]["testgen"]["by_language"]["cobol"]
    assert tg["files"] == 6 and tg["spec"] == 3 and tg["ran"] == 3
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Doc judge" in report and "## COBOL test modes" in report


def test_run_end_to_end_without_cobc(repo: Path, tmp_path: Path, fake: PipelineLLM,
                                     monkeypatch):
    """without cobc the unit and batch programs get no test suite, the CICS programs still
    get their spec tests, and no stage fails."""
    install_fake_llm(monkeypatch)
    out = tmp_path / "out"
    meta = _run(repo, out, _settings(tmp_path, cobc=str(tmp_path / "no-such-cobc")))
    assert list(meta["stages"]) == ["analyze", "baseline", "testgen", "docgen", "judge", "wiki"]
    assert all("error" not in st for st in meta["stages"].values()), meta["stages"]
    kinds = [k for k, _ in fake.calls]
    assert kinds.count("suite") == 0 and kinds.count("spec") == 3   # 3 CICS
    assert kinds.count("doc") == 6 and kinds.count("code") == 6


def test_run_skips_and_failed_stage(repo: Path, tmp_path: Path, fake: PipelineLLM,
                                    monkeypatch):
    out = tmp_path / "out"
    meta = _run(repo, out, _settings(tmp_path), skip=("baseline", "testgen", "docgen", "judge",
                                                     "wiki"))
    assert list(meta["stages"]) == ["analyze"]
    assert fake.calls == [] and not (out / "wiki").exists()
    with pytest.raises(ValueError, match="bogus"):
        _run(repo, out, _settings(tmp_path), skip=("bogus",))
    with pytest.raises(ValueError, match="--skip leafdoc no longer exists"):
        _run(repo, out, _settings(tmp_path), skip=("leafdoc",))

    async def boom(*a, **k):
        raise RuntimeError("wiki exploded")

    monkeypatch.setattr(pl, "wiki", boom)
    meta = _run(repo, out, _settings(tmp_path), skip=("baseline", "testgen", "judge"),
                selection=pl.Selection(programs=["DATEUTIL"]))
    assert meta["stages"]["wiki"]["error"].startswith("RuntimeError: wiki exploded")
    assert meta["stages"]["docgen"]["files"] == 1
    assert "(error: RuntimeError" in (out / "report.md").read_text(encoding="utf-8")


def test_judge_runs_only_under_its_conditions(repo: Path, tmp_path: Path, fake: PipelineLLM):
    out = tmp_path / "out"
    skip = ("baseline", "testgen", "wiki")
    # COBOL found by auto, docgen skipped: no judge (its own model and cost)
    meta = _run(repo, out, _settings(tmp_path, source_lang="auto"),
                skip=skip + ("docgen",))
    assert meta["stages"]["judge"] == {**meta["stages"]["judge"],
                                       "skipped": "no COBOL docs produced in this run"}
    assert fake.calls == []
    # COBOL requested explicitly, no docs: the stage records the error
    meta = _run(repo, out, _settings(tmp_path), skip=skip + ("docgen",))
    assert "no COBOL docs to judge" in meta["stages"]["judge"]["error"]
    # auto + docgen: the judge runs on the docs of this run
    meta = _run(repo, out, _settings(tmp_path, source_lang="auto"), skip=skip,
                selection=pl.Selection(programs=["MENU01"]))
    assert meta["stages"]["judge"]["programs"] == 1
    assert [k for k, _ in fake.calls].count("judge") == 1
    # explicitly requested, docgen skipped: earlier docs are judged (served by the judge cache)
    n = len(fake.calls)
    meta = _run(repo, out, _settings(tmp_path), skip=skip + ("docgen",))
    assert meta["stages"]["judge"]["programs"] == 1 and len(fake.calls) == n


def test_resume_keeps_finished_results(repo: Path, tmp_path: Path, fake: PipelineLLM):
    out = tmp_path / "out"
    st = _settings(tmp_path)
    sel = pl.Selection(programs=["ACCT01", "MENU01"])
    _run(repo, out, st, skip=("wiki", "judge"), selection=sel)
    first = len(fake.calls)
    assert load(out / "testgen" / "_root" / "MENU01.cbl" / "result.json")["status"] == "spec"
    _run(repo, out, st, skip=("wiki", "judge"), selection=sel)
    assert len(fake.calls) == first                     # ok and spec results are final


def test_stale_wiki_pages_are_removed(repo: Path, tmp_path: Path, fake: PipelineLLM,
                                      monkeypatch):
    install_fake_llm(monkeypatch)
    out = tmp_path / "out"
    (out / "wiki").mkdir(parents=True)
    (out / "wiki" / "old-page.md").write_text("# old", encoding="utf-8")
    meta = _run(repo, out, _settings(tmp_path), skip=("baseline", "testgen", "docgen",
                                                     "judge"))
    assert "error" not in meta["stages"]["wiki"], meta["stages"]["wiki"]
    assert not (out / "wiki" / "old-page.md").exists()
    assert (out / "wiki" / "index.md").exists()


def test_wiki_plan_only_needs_no_llm(repo: Path, tmp_path: Path, monkeypatch):
    isolate(monkeypatch, tmp_path)
    res = asyncio.run(pl.wiki(repo, tmp_path / "out", Settings(source_lang="cobol"),
                              plan_only=True))
    assert res["pages"] >= 1 and res["tokens"] == {"input": 0, "output": 0}
    assert res["cobol"]["programs"] == 6
    assert not list((tmp_path / "out").glob("wiki/*.md"))


def test_cobol_only_run_without_tree_sitter(repo: Path, tmp_path: Path, fake: PipelineLLM,
                                            monkeypatch):
    """COBOL needs no tree-sitter."""
    real = importlib.util.find_spec

    def find_spec(name, *a, **k):
        if name.split(".")[0] in ("tree_sitter", "tree_sitter_language_pack"):
            return None
        return real(name, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert not pl._has_multilang_deps()
    out = tmp_path / "out"
    meta = _run(repo, out, _settings(tmp_path), skip=("baseline", "testgen", "judge", "wiki"),
                selection=pl.Selection(programs=["DATEUTIL"]))
    assert meta["stages"]["docgen"]["files"] == 1
    data = json.loads((out / "analysis.json").read_text())
    assert data["languages"] == ["cobol"] and not data["unavailable"]
