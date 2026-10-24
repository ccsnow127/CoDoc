"""the `codocs` CLI with COBOL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pipeline_fixtures import FIXTURES, PipelineLLM, invoke, isolate, load
from wiki_fixtures import flat_repo
from wiki_index_fixtures import install_fake_llm

from codocs.llm import LLM

COMMANDS = ("analyze", "baseline", "testgen", "docgen", "judge", "testrun", "wiki", "run",
            "bizlogic", "bizlogic-render", "report", "translate")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return flat_repo(tmp_path / "demo")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path: Path):
    isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture()
def fake(monkeypatch, repo: Path) -> PipelineLLM:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = PipelineLLM(repo)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                       validate=None, model=None):
        return llm.reply(system, user, tag)

    monkeypatch.setattr(LLM, "complete", complete)
    return llm


def test_help_lists_every_command():
    res = invoke("--help")
    assert res.exit_code == 0
    for cmd in COMMANDS:
        assert cmd in res.output, cmd
    assert "leafdoc" not in res.output and " phi " not in res.output
    for opt in ("--cobc", "--cobc-flags", "--cobol-coverage", "--cobol-spec-signal",
                "--exclude-path", "--judge-model", "--codegen-model", "--programs"):
        assert opt in invoke("run", "--help").output, opt
    assert "exclude COBOL files below" in " ".join(invoke("analyze", "--help").output.split())
    assert "--no-refine" in invoke("wiki", "--help").output
    assert "--glossary" in invoke("translate", "--help").output


def test_analyze_prints_cobol_summary_and_writes_json(repo: Path, tmp_path: Path):
    out = tmp_path / "o"
    res = invoke("analyze", repo, "-o", out, "--top", "3")
    assert res.exit_code == 0, res.output
    assert "Programs" in res.output and "Most depended-upon programs" in res.output
    assert "JCL jobs" in res.output
    data = load(out / "analysis.json")
    assert data["languages"] == ["cobol"] and data["cobol"]["summary"]["jcl_jobs"] == 1
    index = load(out / "cobol" / "index.json")
    assert len(index["programs"]) == 6


def test_analyze_missing_repo_and_unknown_programs(repo: Path, tmp_path: Path):
    res = invoke("analyze", tmp_path / "nope")
    assert res.exit_code == 1
    res = invoke("analyze", repo, "--programs", "NOPE")
    assert res.exit_code == 1 and "none of the requested programs" in res.output


def test_llm_commands_need_api_key(repo: Path, tmp_path: Path):
    out = tmp_path / "o"
    for cmd in (["testgen", repo, "-o", out], ["docgen", repo, "-o", out],
                ["wiki", repo, "-o", out], ["run", repo, "-o", out], ["judge", out]):
        res = invoke(*cmd)
        assert res.exit_code == 1, cmd
        assert "OPENAI_API_KEY" in res.output, cmd


def test_run_judging_existing_docs_needs_the_key(repo: Path, tmp_path: Path):
    """the judge stage would run (COBOL requested), so the key is checked up front."""
    res = invoke("run", repo, "-o", tmp_path / "o", "--source-lang", "cobol", "--skip",
                 "testgen,docgen,wiki")
    assert res.exit_code == 1 and "OPENAI_API_KEY" in res.output
    # auto with docgen skipped: no judge, no LLM stage, no key needed
    res = invoke("run", repo, "-o", tmp_path / "o2", "--skip", "testgen,docgen,wiki")
    assert res.exit_code == 0, res.output


def test_wiki_plan_only_without_key(repo: Path, tmp_path: Path):
    res = invoke("wiki", repo, "-o", tmp_path / "o", "--plan-only", "--no-refine")
    assert res.exit_code == 0, res.output
    assert "Plan:" in res.output


def test_removed_commands_name_their_replacement(repo: Path, tmp_path: Path):
    res = invoke("leafdoc", repo, "-o", tmp_path / "o")
    assert res.exit_code == 2 and "codocs docgen" in res.output and "testgen" in res.output
    res = invoke("phi", tmp_path / "o")
    assert res.exit_code == 2
    assert "phi is now the docgen loop's score" in res.output.replace("\n", " ")
    for old in ("leafdoc", "testrun", "phi"):
        res = invoke("run", repo, "-o", tmp_path / "o", "--skip", old)
        assert res.exit_code == 1 and f"--skip {old} no longer exists" in res.output
    res = invoke("run", repo, "-o", tmp_path / "o", "--doc-eval", "judge")
    assert res.exit_code == 2                          # click: no such option


def test_docgen_testgen_and_run(repo: Path, tmp_path: Path, fake: PipelineLLM, monkeypatch):
    install_fake_llm(monkeypatch)
    out = tmp_path / "o"
    res = invoke("testgen", repo, "-o", out, "--programs", "menu01", "--test-rounds", "1")
    assert res.exit_code == 0, res.output
    assert (out / "tests" / "_root" / "MENU01_codocs_spec.md").exists()
    res = invoke("docgen", repo, "-o", out, "--programs", "acct01,MENU01", "--doc-rounds", "1")
    assert res.exit_code == 0, res.output
    assert sorted(p.name for p in (out / "docs" / "markdown_docs").glob("*.md")) == [
        "ACCT01.cbl.md", "MENU01.cbl.md"]
    res = invoke("judge", out, "--programs", "ACCT01")
    assert res.exit_code == 0, res.output
    assert "Docs judged:" in res.output and (out / "judge" / "ACCT01.json").exists()
    assert not (out / "judge" / "MENU01.json").exists()

    res = invoke("run", repo, "-o", out, "--programs", "DATEUTIL", "-j", "2", "--mutants", "0",
                 "--test-rounds", "1", "--doc-rounds", "1")
    assert res.exit_code == 0, res.output
    meta = load(out / "run_meta.json")
    assert {"analyze", "baseline", "testgen", "docgen", "judge", "wiki"} <= set(meta["stages"])
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "DATEUTIL.cbl" in md and "## Doc judge" in md
    res = invoke("run", repo, "-o", out, "--skip", "wiki,bogus")
    assert res.exit_code == 1 and "unknown stage" in res.output


def test_run_exit_code_when_a_stage_fails(repo: Path, tmp_path: Path, fake: PipelineLLM,
                                         monkeypatch):
    from codocs import pipeline

    async def boom(*a, **k):
        raise RuntimeError("wiki exploded")

    monkeypatch.setattr(pipeline, "wiki", boom)
    res = invoke("run", repo, "-o", tmp_path / "o", "--programs", "DATEUTIL", "--skip",
                 "testgen,judge", "--doc-rounds", "1")
    assert res.exit_code == 2
    assert "wiki failed: RuntimeError" in res.output.replace("\n", " ")


def test_programs_need_cobol(tmp_path: Path):
    res = invoke("analyze", FIXTURES / "gomod", "--programs", "X")
    assert res.exit_code == 1 and "--programs selects COBOL programs" in res.output


def test_testrun_and_translate_errors(tmp_path: Path):
    res = invoke("testrun", tmp_path)
    assert res.exit_code == 1 and "analysis.json" in res.output
    (tmp_path / "analysis.json").write_text(json.dumps({"module_root": str(tmp_path),
                                                        "languages": ["cobol"]}))
    res = invoke("testrun", tmp_path)
    assert res.exit_code == 1 and "no delivered test suites" in res.output
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# x\n")
    res = invoke("translate", wiki, "-o", tmp_path / "ja")
    assert res.exit_code == 1 and "API key" in res.output


def test_translate_glossary_selection(tmp_path: Path, monkeypatch):
    import codocs.cli as cli_mod

    seen: list[str] = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake(src, dst, lang, settings, no_html, kind):
        seen.append(kind)

    monkeypatch.setattr(cli_mod, "_translate_wiki", fake)
    out = tmp_path / "out"
    wiki = out / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "index.md").write_text("# Go\n")
    res = invoke("translate", wiki, "-o", tmp_path / "ja", "-j", "3")
    assert res.exit_code == 0, res.output
    assert seen == ["go"] and "-j applies only to --glossary cobol; ignored" in res.output
    (wiki / "program-index.md").write_text("# Programs\n")
    assert invoke("translate", wiki, "-o", tmp_path / "ja").exit_code == 0
    assert seen[-1] == "cobol"
    (wiki / "program-index.md").unlink()
    (out / "analysis.json").write_text(json.dumps({"languages": ["c", "cobol"]}))
    assert invoke("translate", wiki, "-o", tmp_path / "ja").exit_code == 0
    assert seen[-1] == "cobol"
    assert invoke("translate", wiki, "-o", tmp_path / "ja", "--glossary", "go").exit_code == 0
    assert seen[-1] == "go"
