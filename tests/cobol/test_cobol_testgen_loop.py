"""the TeSearch loop on COBOL targets, called directly with a scripted LLM: unit / batch
programs run the bounded loop (round 1 = the one-shot seed body), spec
programs get one call and a spec artefact, no cobc gives no-toolchain without a call."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from runner_fixtures import (
    ACCTINQ_SPEC,
    ACCTRPT_PLAN,
    DATEUTIL_DRIVER,
    DATEUTIL_PLAN,
    DAYSINM_PLAN,
    ScriptedLLM,
    bound,
    plan,
)

from codocs.config import Settings
from codocs.lang.cobolrun import CobolRunner
from codocs.llm import LLMError
from codocs.tesearch.loop import run_testgen

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _settings(**kw) -> Settings:
    st = Settings()
    st.test_rounds, st.mutants, st.concurrency = 2, 6, 3
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _replies(by_target: dict):
    def answer(tag, system, user):
        rel = tag.split()[1]
        reply = by_target[rel]
        if callable(reply):
            return reply(tag, system, user)
        return reply
    return answer


PLANS = {"cbl/DATEUTIL.cbl": DATEUTIL_PLAN, "cbl/DAYSINM.cbl": DAYSINM_PLAN,
         "cbl/ACCTRPT.cbl": ACCTRPT_PLAN, "cbl/ACCTINQ.cbl": ACCTINQ_SPEC}


async def _run(repo: Path, out: Path, llm, st: Settings | None = None, files=None):
    st = st or _settings()
    ad, runner = bound(repo, out, st)
    units = await ad.discover(runner)
    targets = [t for t in ad.select_targets(repo, units) if files is None or t.file in files]
    res = await run_testgen(targets, units, runner, llm, st, out, repo)
    return ad, {r.file: r for r in res}


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    repo = tmp_path / "cobolmod"
    shutil.copytree(FIXTURES / "cobolmod", repo)
    return repo


@pytest.mark.needs_cobc
async def test_loop_on_cobolmod(mod: Path, tmp_path: Path):
    out = tmp_path / "out"
    llm = ScriptedLLM(_replies(PLANS))
    ad, res = await _run(mod, out, llm, _settings(test_rounds=1, mutants=4))
    util = res["cbl/DATEUTIL.cbl"]
    assert util.status == "ok" and util.lang == "cobol"
    assert util.best_size == 4 and util.best_cov > 0.5 and util.best_mut is not None
    assert (out / "tests" / "cbl" / "DATEUTIL_codocs_test.cbt").is_file()
    assert (out / "tests" / "cbl" / "DATEUTIL_codocs_test" / "plan.json").is_file()
    rd = out / "testgen" / "cbl" / "DATEUTIL.cbl" / "round_1"
    assert {p.name for p in rd.iterdir()} == {"prompt.txt", "reply.txt", "suite.cbt",
                                              "green.cbt", "measure.json"}
    days = res["cbl/DAYSINM.cbl"]
    assert days.status == "ok" and days.best_cov > 0 and days.best_size == 3
    rpt = res["cbl/ACCTRPT.cbl"]
    assert rpt.status == "ok" and rpt.best_size == 3       # return-code, stdout, file
    inq = res["cbl/ACCTINQ.cbl"]
    assert inq.status == "spec" and inq.best_score == 0.0
    assert inq.cov_note == "spec: not executable (CICS/DB2)" and len(inq.rounds) == 1
    assert (out / "tests" / "cbl" / "ACCTINQ_codocs_spec.json").is_file()
    assert "# Test specification: ACCTINQ" in (
        out / "tests" / "cbl" / "ACCTINQ_codocs_spec.md").read_text()
    spec_round = out / "testgen" / "cbl" / "ACCTINQ.cbl" / "round_1"
    m = json.loads((spec_round / "measure.json").read_text())
    assert m["build_ok"] and m["size"] == 2 and m["green_size"] == 0
    assert not (out / "tests" / "cbl" / "ACCTINQ_codocs_test.cbt").exists()
    # round 1 sends the one-shot seed body with a completion budget of 32000
    first = {tag: (system, user, kw) for tag, system, user, kw in llm.calls}
    system, user, kw = first["testgen cbl/DATEUTIL.cbl r1"]
    assert "## What to produce (mode: unit)" in user and "CALL 'DATEUTIL'" in user
    assert "### Case paragraphs" in user and kw == {"max_tokens": 32000}
    spec_system, spec_user, _ = first["testgen cbl/ACCTINQ.cbl spec"]
    assert "## What to produce (mode: spec)" in spec_user and "CICS facts" in spec_user
    # a resume makes no LLM call: ok and spec are finished
    n = len(llm.calls)
    _, again = await _run(mod, out, llm)
    assert len(llm.calls) == n and again["cbl/ACCTINQ.cbl"].status == "spec"


@pytest.mark.needs_cobc
async def test_refine_round_repairs_a_failing_case(mod: Path, tmp_path: Path):
    wrong = DATEUTIL_DRIVER.replace("IF LK-RC = '10'", "IF LK-RC = '99'")
    first = plan(wrong, cases=["C01", "C02", "C03", "C04"])

    def reply(tag, system, user):
        return first if tag.endswith("r1") else DATEUTIL_PLAN

    llm = ScriptedLLM(_replies({"cbl/DATEUTIL.cbl": reply}))
    _, res = await _run(mod, tmp_path / "out", llm, _settings(mutants=2),
                        files={"cbl/DATEUTIL.cbl"})
    r = res["cbl/DATEUTIL.cbl"]
    m1, m2 = r.rounds[0].measurement, r.rounds[1].measurement
    assert m1.green_size == 3 and "cbl/DATEUTIL_codocs_test.cbt::C02" in m1.failing
    assert m2.green_size == 4 and r.best_round == 2
    refine = [u for tag, _s, u, _k in llm.calls if tag.endswith("r2")][0]
    assert "[ROUND 2]" in refine and "*> <-- FAILING: TEST C02 FAIL rc=10" in refine
    assert "*> <-- NOT COVERED YET" in refine or "SURVIVING MUTANT" in refine


async def test_llm_error_and_missing_cobc(mod: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch):
    llm = ScriptedLLM(lambda tag, s, u: LLMError("boom"))
    monkeypatch.setattr(CobolRunner, "cobc_ok", lambda self: True)
    _, res = await _run(mod, tmp_path / "out", llm, _settings(test_rounds=1),
                        files={"cbl/DATEUTIL.cbl", "cbl/ACCTINQ.cbl"})
    assert res["cbl/DATEUTIL.cbl"].status == "error" and "boom" in res["cbl/DATEUTIL.cbl"].error
    assert res["cbl/ACCTINQ.cbl"].status == "error"
    assert not (tmp_path / "out" / "tests" / "cbl" / "DATEUTIL_codocs_test.cbt").exists()
    # without cobc: unit / batch targets are no-toolchain with no LLM call; spec still runs
    monkeypatch.setattr(CobolRunner, "cobc_ok", lambda self: False)
    llm = ScriptedLLM(_replies(PLANS))
    _, res = await _run(mod, tmp_path / "out2", llm)
    assert {f: r.status for f, r in res.items()} == {
        "cbl/ACCTINQ.cbl": "spec", "cbl/ACCTRPT.cbl": "no-toolchain",
        "cbl/DATEUTIL.cbl": "no-toolchain", "cbl/DAYSINM.cbl": "no-toolchain"}
    assert [c[0] for c in llm.calls] == ["testgen cbl/ACCTINQ.cbl spec"]
    _, res = await _run(mod, tmp_path / "out2", llm)
    assert len(llm.calls) == 1                         # spec resumed, no-toolchain: no call
