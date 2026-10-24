"""end to end: `codocs run` on a tmp copy of cobolmod with a scripted
LLM (codegen returns the original program, doc replies carry every expected section, test
replies are valid plans); `codocs testrun`; a resume run without LLM calls; the facts-only
variant without cobc; plan-only wiki and bizlogic on mixedcobol (Go-free: COBOL + C + Python).
"""

from __future__ import annotations

import asyncio

import pytest
from pipeline_fixtures import install, invoke, load, snapshot
from wiki_index_fixtures import cobol_doc, write_doc

from codocs import pipeline as pl
from codocs.config import Settings
from codocs.lang.cobolrun import CobolRunner
from codocs.lang.wikiindex import MultiLangIndex
from codocs.wiki import build_digests

RUN = ("--skip", "wiki", "--mutants", "4", "--test-rounds", "2", "-j", "3", "--workers", "4")


def _doc(out, base: str) -> dict:
    return load(out / "docgen" / "cbl" / base / "result.json")


def _suite(out, base: str) -> dict:
    return load(out / "testgen" / "cbl" / base / "result.json")


@pytest.fixture(scope="module")
def cobolmod_run(tmp_path_factory):
    """one full run shared by the assertions below (the run takes about a minute)."""
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("e2e")
    try:
        repo, out, fake = install(mp, tmp)
        before = snapshot(repo)
        res = invoke("run", repo, "-o", out, *RUN)
        yield {"repo": repo, "out": out, "fake": fake, "before": before, "res": res, "mp": mp}
    finally:
        mp.undo()


@pytest.mark.needs_cobc
def test_run_succeeds_and_leaves_the_repo_alone(cobolmod_run):
    res = cobolmod_run["res"]
    assert res.exit_code == 0, res.output
    assert snapshot(cobolmod_run["repo"]) == cobolmod_run["before"]
    out = cobolmod_run["out"]
    meta = load(out / "run_meta.json")
    assert set(meta["stages"]) == {"analyze", "baseline", "testgen", "docgen", "judge"}
    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["cobol"]
    assert analysis["cobol"]["modes"] == {"cbl/ACCTINQ.cbl": "spec", "cbl/ACCTRPT.cbl": "batch",
                                          "cbl/DATEUTIL.cbl": "unit", "cbl/DAYSINM.cbl": "unit"}
    assert analysis["cobol"]["scope"] == {"subtree": None, "exclude_paths": []}
    assert (out / "cobol" / "index.json").is_file()


@pytest.mark.needs_cobc
def test_baseline(cobolmod_run):
    base = load(cobolmod_run["out"] / "baseline" / "cobol" / "cbl.json")
    assert base["build_ok"] is True
    assert sorted(base["passing"]) == [f"test/TDATEUTL.cbl::T0{i}" for i in (1, 2, 3)]
    assert list(base["failing"]) == ["test/TDATEUTL.cbl::T99"]


@pytest.mark.needs_cobc
def test_docs_phi_and_signals(cobolmod_run):
    out = cobolmod_run["out"]
    want = {"DATEUTIL.cbl": "tests", "DAYSINM.cbl": "generated-tests",
            "ACCTRPT.cbl": "generated-tests", "ACCTINQ.cbl": "facts"}
    for base, signal in want.items():
        r = _doc(out, base)
        assert r["status"] == "ok" and r["lang"] == "cobol", r
        assert r["signal"] == signal, (base, r["signal"])
        assert r["best_phi"] == 1.0, (base, r["phi_trajectory"])
        assert r["early_stop"] is True and len(r["rounds"]) == 1, base
        assert r["rounds"][0]["missing"] == [], base
        assert (out / "docgen" / "cbl" / base / "round_1" / "code.cbl").is_file()
        doc = out / "docs" / "cbl" / "markdown_docs" / f"{base}.md"
        text = doc.read_text(encoding="utf-8")
        assert text.startswith(f"# cbl/{base}\n"), text[:80]
        assert "## ClassDef " in text
    facts = _doc(out, "ACCTINQ.cbl")["rounds"][0]["eval"]
    assert facts["facts_by_class"] and facts["phi_scored"] == 1.0


@pytest.mark.needs_cobc
def test_suites(cobolmod_run):
    out = cobolmod_run["out"]
    du = _suite(out, "DATEUTIL.cbl")
    assert du["status"] == "ok" and du["best_size"] > 0, du
    assert du["best_cov"] > 0.5 and du["best_mut"] is not None, du
    assert (out / "tests" / "cbl" / "DATEUTIL_codocs_test.cbt").is_file()
    assert (out / "tests" / "cbl" / "DATEUTIL_codocs_test" / "plan.json").is_file()
    dm = _suite(out, "DAYSINM.cbl")
    assert dm["status"] == "ok" and dm["best_size"] > 0 and dm["best_cov"] > 0, dm
    ar = _suite(out, "ACCTRPT.cbl")
    assert ar["status"] == "ok" and ar["best_size"] > 0, ar
    green = (out / "tests" / "cbl" / "ACCTRPT_codocs_test.cbt").read_text()
    assert '"expected_files"' in green and "RPTOUT" in green
    ai = _suite(out, "ACCTINQ.cbl")
    assert ai["status"] == "spec" and ai["cov_note"].startswith("spec: not executable"), ai
    assert (out / "tests" / "cbl" / "ACCTINQ_codocs_spec.json").is_file()
    assert (out / "tests" / "cbl" / "ACCTINQ_codocs_spec.md").is_file()
    assert not (out / "tests" / "cbl" / "ACCTINQ_codocs_test.cbt").exists()


@pytest.mark.needs_cobc
def test_judge_and_report(cobolmod_run):
    out = cobolmod_run["out"]
    judge = load(out / "judge" / "summary.json")
    assert judge["docs"] == 4
    stage = load(out / "run_meta.json")["stages"]["judge"]
    assert stage["programs"] == 4 and stage["gate_pass"] == 4
    summary = load(out / "summary.json")
    assert summary["languages"] == ["cobol"]
    tg = summary["testgen"]["aggregate"]
    assert tg["files"] == 4 and tg["ran"] == 3 and tg["spec"] == 1 and tg["no_toolchain"] == 0
    dg = summary["docgen"]["aggregate"]
    assert dg["cobol_mean_best_phi_by_signal"] == {"tests": 1.0, "generated-tests": 1.0,
                                                   "facts": 1.0}
    assert dg["mean_best_phi"] == 1.0 and dg["cobol_facts_by_class"]
    rows = {r["file"]: r for r in summary["testgen"]["rows"]}
    assert rows["cbl/ACCTINQ.cbl"]["mode"] == "spec"
    assert summary["cobol"]["modes"] == {"unit": 2, "batch": 1, "spec": 1}
    assert summary["cobol"]["judge"]["docs"] == 4
    report = (out / "report.md").read_text(encoding="utf-8")
    for heading in ("## COBOL test modes", "## COBOL docs per signal", "## Doc judge",
                    "### Facts signal by class"):
        assert heading in report
    meta = load(out / "run_meta.json")
    assert meta["stages"]["testgen"]["by_language"]["cobol"]["files"] == 4
    assert meta["stages"]["testgen"]["by_language"]["cobol"]["spec"] == 1
    assert meta["stages"]["docgen"]["by_language"]["cobol"]["files"] == 4


@pytest.mark.needs_cobc
def test_testrun_passes_every_green_bundle(cobolmod_run):
    out = cobolmod_run["out"]
    res = invoke("testrun", out)
    assert res.exit_code == 0, res.output
    summary = load(out / "testrun" / "summary.json")
    cob = summary["cobol"]
    assert cob["programs"] == 4 and cob["executable"] == 3 and cob["compiled"] == 3
    assert cob["cases_failed"] == 0 and cob["cases_passed"] > 0
    assert cob["spec_cases"] == 2 and cob["mean_statement_coverage"] > 0
    for stem in ("DATEUTIL.cbl", "DAYSINM.cbl", "ACCTRPT.cbl"):
        one = load(out / "testrun" / "cobol" / "cbl" / f"{stem}.json")
        assert one["status"] == "passed", one
    assert "| ACCTINQ | spec (CICS/DB2) |" in (out / "testrun" / "summary.md").read_text()


@pytest.mark.needs_cobc
def test_resume_makes_no_llm_call(cobolmod_run):
    repo, out, fake = cobolmod_run["repo"], cobolmod_run["out"], cobolmod_run["fake"]
    first = len(fake.calls)
    assert first > 0
    res = invoke("run", repo, "-o", out, *RUN)
    assert res.exit_code == 0, res.output
    assert len(fake.calls) == first, fake.calls[first:]
    assert snapshot(repo) == cobolmod_run["before"]


def test_facts_only_without_cobc(monkeypatch, tmp_path):
    """no cobc: unit/batch testgen targets are no-toolchain without an LLM call, the spec
    program still gets its spec, and every doc is scored by facts."""
    repo, out, fake = install(monkeypatch, tmp_path)
    monkeypatch.setattr(CobolRunner, "cobc_ok", lambda self: False)
    res = invoke("run", repo, "-o", out, *RUN)
    assert res.exit_code == 0, res.output
    for base in ("DATEUTIL.cbl", "DAYSINM.cbl", "ACCTRPT.cbl", "ACCTINQ.cbl"):
        r = _doc(out, base)
        assert r["signal"] == "facts" and r["best_phi"] == 1.0, (base, r)
    for base in ("DATEUTIL.cbl", "DAYSINM.cbl", "ACCTRPT.cbl"):
        assert _suite(out, base)["status"] == "no-toolchain"
    assert _suite(out, "ACCTINQ.cbl")["status"] == "spec"
    kinds = [k for k, _ in fake.calls]
    assert kinds.count("suite") == 0 and kinds.count("spec") == 1
    tg = load(out / "summary.json")["testgen"]["aggregate"]
    assert tg["ran"] == 0 and tg["no_toolchain"] == 3 and tg["spec"] == 1


def test_mixed_repo_plan_only(monkeypatch, tmp_path):
    """mixedcobol: one wiki plan over COBOL, C and Python (no go.mod: linkers run without
    Go); the graph links COBOL to C; the mixed directory's C digest has no COBOL entries."""
    repo, out, fake = install(monkeypatch, tmp_path, "mixedcobol")
    monkeypatch.delenv("OPENAI_API_KEY")                  # plan-only needs no key
    write_doc(out, "mixed/NUMPGM.cbl", cobol_doc("mixed/NUMPGM.cbl", "NUMPGM",
                                                 "Formats a count as edited text."))
    write_doc(out, "mixed/fmtnum.c", "# mixed/fmtnum.c\n\n## FunctionDef fmtnum\n"
                                     "**fmtnum**: Formats a count.\n")
    res = invoke("wiki", repo, "-o", out, "--plan-only")
    assert res.exit_code == 0, res.output
    for lang in ("cobol", "c", "python"):
        assert f"{lang}: " in res.output, res.output
    assert "Application" in res.output                     # the COBOL planner's section
    plan = asyncio.run(pl.wiki(repo, out, Settings.load(), plan_only=True))
    assert set(plan["languages"]) == {"c", "python", "cobol"}
    assert plan["plan"][-1]["title"] == "Application"
    assert plan["cobol"]["programs"] == 4
    idx = MultiLangIndex.build(repo, None, ["c", "python", "cobol"], Settings(), out_dir=out)
    digests, _ = build_digests(idx, repo, out)
    assert "fmtnum" in [e.name for e in digests["mixed"].entries]
    assert not any("NUMPGM" in e.name for e in digests["mixed"].entries)
    assert digests["mixed/NUMPGM.cbl"].source == "leafdocs"

    res = invoke("bizlogic", repo, "-o", out, "--plan-only", "-k", "2")
    assert res.exit_code == 0, res.output
    graph = load(out / "bizlogic" / "graph.json")
    assert graph["stats"]["cross_edges"] == {"cobol_c_calls": 1, "cobol_c_unresolved": 0}
    assert [r["id"] for r in graph["roots"]] == ["cobol::ACCTRPT"]
    assert set(graph["stats"]["languages"]) == {"c", "python", "cobol"}
    assert fake.calls == []
