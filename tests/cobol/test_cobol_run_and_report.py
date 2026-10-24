"""`codocs run`, run_dataset.sh and the report: `codocs run` judges only its own stages (no stale
error from an earlier run_meta.json), run_dataset.sh keeps codocs' exit status, the expanded
bundle README builds and runs as written, the report keeps the modes of programs analysed by
earlier runs, a missing cobc is reported once, and `--programs` is repeatable."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from pipeline_fixtures import FIXTURES, PipelineLLM, install, invoke, isolate, load
from runner_fixtures import ACCTRPT_PLAN, DATEUTIL_PLAN, bound
from wiki_fixtures import flat_repo

from codocs import pipeline as pl
from codocs.config import Settings
from codocs.lang import cobolrun
from codocs.llm import LLM
from codocs.report import build_report

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def demo(tmp_path: Path) -> Path:
    return flat_repo(tmp_path / "demo")


@pytest.fixture()
def fake(monkeypatch, tmp_path: Path, demo: Path) -> PipelineLLM:
    isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = PipelineLLM(demo)

    async def complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                       validate=None, model=None):
        return llm.reply(system, user, tag)

    monkeypatch.setattr(LLM, "complete", complete)
    return llm


@pytest.fixture()
def fresh_warning(monkeypatch):
    monkeypatch.setattr(cobolrun, "_WARNED", set())


# -- a stale stage error of an earlier run ---------------------------------------------------


def test_run_ignores_a_judge_error_of_an_earlier_run(demo: Path, tmp_path: Path, fake):
    out = tmp_path / "o"
    res = invoke("run", demo, "-o", out, "--source-lang", "cobol", "--skip",
                 "testgen,docgen,wiki")
    assert res.exit_code == 2 and "judge failed" in res.output     # no docs to judge
    res = invoke("run", demo, "-o", out, "--source-lang", "cobol", "--skip",
                 "testgen,docgen,wiki,judge")
    assert res.exit_code == 0, res.output
    assert "judge failed" not in res.output
    assert "error" in load(out / "run_meta.json")["stages"]["judge"]  # kept on disk


def test_run_ignores_a_wiki_error_of_an_earlier_run(demo: Path, tmp_path: Path, fake,
                                                   monkeypatch):
    out = tmp_path / "o"

    async def boom(*a, **k):
        raise RuntimeError("wiki exploded")

    with monkeypatch.context() as m:
        m.setattr(pl, "wiki", boom)
        res = invoke("run", demo, "-o", out, "--programs", "DATEUTIL", "--skip",
                     "testgen,judge", "--doc-rounds", "1")
    assert res.exit_code == 2 and "wiki failed" in res.output
    res = invoke("run", demo, "-o", out, "--programs", "DATEUTIL", "--skip",
                 "testgen,docgen,judge,wiki")
    assert res.exit_code == 0, res.output
    assert "wiki failed" not in res.output


def test_pipeline_run_reports_the_stages_it_recorded(demo: Path, tmp_path: Path, fake):
    st = Settings(source_lang="cobol", api_key="k", cache_path=str(tmp_path / "c.db"))
    meta = asyncio.run(pl.run(demo, tmp_path / "o", st, skip=("testgen", "docgen", "judge",
                                                               "wiki")))
    assert meta[pl.RAN_KEY] == ["analyze", "baseline"]
    assert pl.RAN_KEY not in load(tmp_path / "o" / "run_meta.json")


# -- run_dataset.sh keeps the exit status ----------------------------------------------------


def _script_env(tmp_path: Path, rc_of: dict[str, int]) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    (home / ".repowiki").mkdir(parents=True)
    (home / ".repowiki" / "mermaid.min.js").write_text("// offline\n")
    stub = tmp_path / "codocs_stub.sh"
    cases = "\n".join(f'  {k}) exit {v};;' for k, v in rc_of.items())
    stub.write_text(f'#!/usr/bin/env bash\necho "stub $1"\ncase "$1" in\n{cases}\n'
                    '  *) exit 0;;\nesac\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "HOME": str(home), "CODOCS": str(stub), "OPENAI_API_KEY": "x",
           "OUT_ROOT": str(tmp_path / "out")}
    return env, repo


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_run_dataset_reports_a_failed_codocs_run(tmp_path: Path):
    env, repo = _script_env(tmp_path, {"run": 2})
    p = subprocess.run(["bash", str(ROOT / "scripts" / "cobol" / "run_dataset.sh"), str(repo)],
                       env={**env, "STEPS": "run"}, capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "[run] WARN: codocs run exited with 2" in p.stdout
    assert "[run] done." in p.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_run_dataset_stops_on_a_failed_static_step(tmp_path: Path):
    env, repo = _script_env(tmp_path, {"analyze": 3})
    p = subprocess.run(["bash", str(ROOT / "scripts" / "cobol" / "run_dataset.sh"), str(repo)],
                       env={**env, "STEPS": "analyze plan"}, capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 3
    assert "stub analyze" in p.stdout and "stub wiki" not in p.stdout
    assert "[run] done." not in p.stdout


# -- the expanded bundle README builds and runs as written ---------------------------------


def _readme_commands(readme: str, repo: Path) -> tuple[str, dict[str, str]]:
    block = re.search(r"```sh\n(.*?)```", readme, re.S).group(1)
    assert "REPO=/path/to/repository" in block
    env = {m.group(1): m.group(2)
           for m in re.finditer(r"- `(DD_[A-Z0-9_-]+)=([^`]+)`", readme)}
    return block.replace("REPO=/path/to/repository", f"REPO={repo}"), env


@pytest.mark.needs_cobc
async def test_expanded_bundle_readme_builds_and_runs(tmp_path: Path):
    repo = tmp_path / "cobolmod"
    shutil.copytree(FIXTURES / "cobolmod", repo, ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path / "out"
    ad, runner = bound(repo, out)
    units = await ad.discover(runner)
    outputs = {}
    for rel, plan_text in (("cbl/DATEUTIL.cbl", DATEUTIL_PLAN),
                           ("cbl/ACCTRPT.cbl", ACCTRPT_PLAN)):
        text = ad.normalize_suite(ad.extract_code(plan_text), units["cbl"], rel)
        path = out / "tests" / "cbl" / ad.suite_basename(Path(rel).name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        ad.deliver_extra(path, text)
        d = path.with_suffix("")
        readme = (d / "README.md").read_text()
        assert "from the repository root" not in readme
        script, dd = _readme_commands(readme, repo)
        p = subprocess.run(["bash", "-c", "set -e\n" + script], cwd=d, capture_output=True,
                           text=True, timeout=120, env={**os.environ, **dd})
        assert p.returncode == 0, readme + p.stdout + p.stderr
        outputs[rel] = p.stdout
    assert "$REPO/cbl/DAYSINM.cbl" in (out / "tests" / "cbl" / "DATEUTIL_codocs_test"
                                       / "README.md").read_text()   # the FUNCTION it calls
    assert outputs["cbl/DATEUTIL.cbl"].count(" PASS") == 4
    assert "FAIL" not in outputs["cbl/DATEUTIL.cbl"]
    assert "TOTAL VALID: 00001 INVALID: 00001" in outputs["cbl/ACCTRPT.cbl"]


# -- report modes after a --programs run -----------------------------------------------------


def _row(out: Path, stage: str, rel: str) -> None:
    d = out / stage / "cbl" / Path(rel).name
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(
        f'{{"pkg": "cbl", "file": "{rel}", "stem": "{Path(rel).name}", "status": "ok", '
        f'"lang": "cobol"}}')


def test_report_keeps_modes_of_earlier_runs(monkeypatch, tmp_path: Path):
    isolate(monkeypatch, tmp_path)
    repo = tmp_path / "cobolmod"
    shutil.copytree(FIXTURES / "cobolmod", repo, ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path / "out"
    st = Settings(source_lang="cobol", cache_path=str(tmp_path / "c.db"))
    runners = pl.make_runners(repo, st, out)
    asyncio.run(pl.analyze_all(runners, st, out, pl.Selection()))
    rels = ("cbl/ACCTINQ.cbl", "cbl/ACCTRPT.cbl", "cbl/DATEUTIL.cbl", "cbl/DAYSINM.cbl")
    for rel in rels:
        _row(out, "testgen", rel)
        _row(out, "docgen", rel)
    full = build_report(out)["cobol"]
    assert full["modes"] == {"unit": 2, "batch": 1, "spec": 1}
    runners = pl.make_runners(repo, st, out)
    asyncio.run(pl.analyze_all(runners, st, out, pl.Selection(programs=["DATEUTIL"])))
    assert load(out / "analysis.json")["cobol"]["modes"] == {"cbl/DATEUTIL.cbl": "unit"}
    summary = build_report(out)
    assert summary["cobol"]["modes"] == full["modes"]
    assert summary["cobol"]["spec_kinds"] == full["spec_kinds"] == {"CICS/DB2": 1}
    assert all(r.get("mode") for r in summary["testgen"]["rows"] + summary["docgen"]["rows"])
    assert "| spec | 1 |" in (out / "report.md").read_text()


# -- a missing cobc is reported ----------------------------------------------------------------


def test_missing_cobc_warns_once(tmp_path: Path, caplog, fresh_warning):
    st = Settings(cobc=str(tmp_path / "nonexistent" / "cobc"))
    repo = FIXTURES / "cobolmod"
    with caplog.at_level(logging.WARNING):
        for n in (1, 2):
            ad, runner = bound(repo, tmp_path / f"out{n}", st)
            assert not runner.cobc_ok() and not ad.cobc_available()
            assert ad.doc_signal("cbl/DATEUTIL.cbl", None) == "facts"
    hits = [r for r in caplog.records if "cobc not found or not runnable" in r.getMessage()]
    assert len(hits) == 1 and r"nonexistent" in hits[0].getMessage()
    assert hits[0].levelno == logging.WARNING


def test_run_without_cobc_logs_the_warning(monkeypatch, tmp_path: Path, fresh_warning):
    repo, out, _fake = install(monkeypatch, tmp_path)
    res = invoke("run", repo, "-o", out, "--programs", "DATEUTIL", "--cobc",
                 tmp_path / "nonexistent" / "cobc", "--skip", "wiki,judge", "--mutants", "0",
                 "--test-rounds", "1", "--doc-rounds", "1")
    assert res.exit_code == 0, res.output
    log = (out / "logs" / "codocs.log").read_text()
    assert "WARNING" in log and "cobc not found or not runnable" in log
    assert log.count("cobc not found or not runnable") == 1


@pytest.mark.needs_cobc
def test_testrun_without_cobc_counts_no_toolchain(monkeypatch, tmp_path: Path, fresh_warning):
    repo, out, _fake = install(monkeypatch, tmp_path)
    res = invoke("testgen", repo, "-o", out, "--programs", "DATEUTIL", "--programs", "ACCTINQ",
                 "--mutants", "0", "--test-rounds", "1")
    assert res.exit_code == 0, res.output
    missing = tmp_path / "nonexistent" / "cobc"
    res = invoke("testrun", out, "--cobc", missing)
    assert res.exit_code == 0, res.output
    text = " ".join(res.output.split())
    assert "1 unit/batch programs not run: cobc not found or not runnable" in text
    cob = load(out / "testrun" / "summary.json")["cobol"]
    assert cob["no_toolchain"] == 1 and cob["cobc"] == str(missing)
    md = (out / "testrun" / "summary.md").read_text()
    assert "| DATEUTIL | unit | no-toolchain |" in md
    assert "not run, cobc not available" in md


# -- --programs is repeatable --------------------------------------------------------------------


def test_programs_option_is_repeatable(monkeypatch, tmp_path: Path):
    isolate(monkeypatch, tmp_path)
    out = tmp_path / "o"
    res = invoke("analyze", FIXTURES / "cobolmod", "--programs", "DATEUTIL", "--programs",
                 "ACCTRPT", "-o", out)
    assert res.exit_code == 0, res.output
    data = load(out / "analysis.json")
    assert data["selection"]["programs"] == ["DATEUTIL", "ACCTRPT"]
    assert sorted(t["file"] for t in data["targets"]) == ["cbl/ACCTRPT.cbl", "cbl/DATEUTIL.cbl"]
    for cmd in ("analyze", "docgen", "testgen", "run", "judge", "testrun"):
        assert "repeatable" in invoke(cmd, "--help").output, cmd


def test_docgen_and_testgen_take_the_prose_language():
    for cmd in ("docgen", "testgen"):
        text = " ".join(invoke(cmd, "--help").output.split())
        assert "-l, --lang" in text and "COBOL docs and test plans" in text, cmd
