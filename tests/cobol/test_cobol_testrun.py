"""`codocs testrun`: the COBOL summary metrics and re-running delivered bundles without an
LLM."""

from __future__ import annotations

import asyncio

import pytest
from pipeline_fixtures import install, invoke, load

from codocs.cobol.classify import SPEC_TERMINAL
from codocs.cobol.testmodels import (
    CaseResult,
    FixtureFile,
    GeneratedTest,
    Testability,
    TestCaseSpec,
    TestResult,
)
from codocs.cobol.testrun import restore_settings, run_testrun, summarize, summary_md
from codocs.config import Settings


def _tests() -> dict[str, GeneratedTest]:
    return {
        "UNITP": GeneratedTest(
            program_id="UNITP", mode="unit", driver_name="TUNITP",
            driver_source="       PROGRAM-ID. TUNITP.\n", stubs_source="       PROGRAM-ID. S.\n",
            cases=[TestCaseSpec(id="C01"), TestCaseSpec(id="C02")],
            testability=Testability(program_id="UNITP", mode="unit", reasons=["USING A"]),
        ),
        "BATCHP": GeneratedTest(
            program_id="BATCHP", mode="batch",
            inputs=[FixtureFile(dd="IN", path="in/data.txt", content="a\nb\n")],
            expected_files=[FixtureFile(dd="OUT", path="out.txt",
                                        records=[{"F": "1"}, "raw"])],
        ),
        "ONLINE": GeneratedTest(
            program_id="ONLINE", mode="spec",
            cases=[TestCaseSpec(id="S1", title="happy path", given="COMMAREA X=1",
                                when="ENTER", then="screen shows OK",
                                inputs={"X": "1"}, expected={"MSG": "OK"})],
            testability=Testability(program_id="ONLINE", mode="spec",
                                    reasons=["uses EXEC CICS"]),
        ),
        "BROKEN": GeneratedTest(program_id="BROKEN", mode="unit", error="no driver_source"),
    }


def _results() -> dict[str, TestResult]:
    return {
        "UNITP": TestResult(program_id="UNITP", mode="unit", status="failed", compiled=True,
                            cases=[CaseResult(id="C01", status="pass"),
                                   CaseResult(id="C02", status="fail", detail="x")],
                            passed=1, failed=1, paragraphs_total=4, paragraphs_hit=3,
                            compile_log="ok", run_log="TEST C01 PASS"),
        "BATCHP": TestResult(program_id="BATCHP", mode="batch", status="compile-error"),
        "ONLINE": TestResult(program_id="ONLINE", mode="spec", status="not-executable"),
        "BROKEN": TestResult(program_id="BROKEN", mode="unit", status="generation-error"),
    }


def test_summary_metrics():
    tests, results = _tests(), _results()
    summary = summarize(tests, results)
    assert summary["programs"] == 4
    assert summary["modes"] == {"unit": 2, "batch": 1, "spec": 1}
    assert summary["executable"] == 2  # generation errors are not counted as executable
    assert summary["compiled"] == 1 and summary["compile_rate"] == 0.5
    assert summary["case_pass_rate"] == 0.5
    assert summary["mean_paragraph_coverage"] == 0.75
    assert summary["spec_cases"] == 1 and summary["generation_errors"] == 1
    md = summary_md(tests, results, summary)
    assert "| UNITP | unit | failed | 1/1/2 | 3/4 |" in md


def test_without_results():
    summary = summarize(_tests(), None)
    assert summary["executable"] == 0 and summary["compile_rate"] is None
    assert "| BROKEN | unit | generation-error |" in summary_md(_tests(), None, summary)


def test_terminal_spec_kind_summary():
    term = Testability(program_id="T", mode="spec", spec_kind=SPEC_TERMINAL,
                       reasons=["uses terminal I/O (...): needs a curses terminal"])
    old = Testability(program_id="O", mode="spec", reasons=["uses EXEC CICS"])
    tests = {
        "T": GeneratedTest(program_id="T", mode="spec", testability=term,
                           cases=[TestCaseSpec(id="S1")]),
        "O": GeneratedTest(program_id="O", mode="spec", testability=old,
                           cases=[TestCaseSpec(id="S1")]),
        "U": GeneratedTest(program_id="U", mode="unit", cases=[TestCaseSpec(id="C01")]),
    }
    results = {
        "U": TestResult(program_id="U", mode="unit", status="weak", compiled=True,
                        cases=[CaseResult(id="C01", status="weak")], weak=1,
                        stubbed_callees=["Q"]),
    }
    s = summarize(tests, results)
    assert s["spec_kinds"] == {"terminal": 1, "CICS/DB2": 1}
    assert s["cases_weak"] == 1 and s["programs_only_weak"] == 1
    assert s["cases_passed"] == 0 and s["case_pass_rate"] is None
    md = summary_md(tests, results, s)
    assert "| T | spec (terminal) |" in md and "| O | spec (CICS/DB2) |" in md
    assert "weak cases (PASS printed without a check" in md
    assert "1 weak; stubbed: Q" in md


def test_restore_settings_from_out(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "analysis.json").write_text('{"languages": ["c", "cobol"], "cobol": {"scope": '
                                       '{"subtree": "app", "exclude_paths": ["app/old"]}}}')
    (out / "run_meta.json").write_text('{"settings": {"cobc": "/opt/cobc", "model": "m", '
                                       '"cobol_coverage": "paragraph", "test_timeout": 9}}')
    st = restore_settings(Settings(), out)
    assert st.cobc == "/opt/cobc" and st.cobol_coverage == "paragraph" and st.test_timeout == 9
    assert st.source_lang == "c,cobol" and st.exclude_list() == ("app/old",)
    assert st.model == Settings().model                       # not a toolchain option
    st = restore_settings(Settings(cobc="mine"), out, keep=("cobc",))
    assert st.cobc == "mine"


def test_testrun_without_an_analysis(tmp_path):
    with pytest.raises(FileNotFoundError, match="analysis.json"):
        asyncio.run(run_testrun(tmp_path, Settings()))


@pytest.mark.needs_cobc
def test_testrun_reruns_delivered_bundles(monkeypatch, tmp_path):
    repo, out, fake = install(monkeypatch, tmp_path)
    res = invoke("testgen", repo, "-o", out, "--programs", "DATEUTIL,ACCTINQ", "--mutants",
                 "0", "--test-rounds", "1")
    assert res.exit_code == 0, res.output
    calls = len(fake.calls)
    res = invoke("testrun", out, "--programs", "dateutil")
    assert res.exit_code == 0, res.output
    assert len(fake.calls) == calls                           # no LLM
    summary = load(out / "testrun" / "summary.json")
    cob = summary["cobol"]
    assert cob["programs"] == 1 and cob["compiled"] == 1 and cob["cases_failed"] == 0
    assert cob["mean_paragraph_coverage"] > 0 and cob["mean_statement_coverage"] > 0.5
    one = load(out / "testrun" / "cobol" / "cbl" / "DATEUTIL.cbl.json")
    assert one["status"] == "passed" and one["passed"] == 4
    res = invoke("testrun", out)                              # every suite, spec included
    assert res.exit_code == 0, res.output
    cob = load(out / "testrun" / "summary.json")["cobol"]
    assert cob["programs"] == 2 and cob["modes"] == {"unit": 1, "spec": 1}
    assert cob["spec_cases"] == 2
    md = (out / "testrun" / "summary.md").read_text()
    assert "| ACCTINQ | spec (CICS/DB2) | not executable |" in md
