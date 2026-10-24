"""the SpecForge loop on COBOL targets, called directly with a scripted
LLM: the signal per classification (tests, generated-tests, facts), phi 1.0 for the original
program, phi < 1 for broken or renamed programs, display-form codegen, no cobc."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from runner_fixtures import ACCTRPT_PLAN, DAYSINM_PLAN, ScriptedLLM, bound
from wiki_fixtures import SUBPGM

from codocs.config import Settings
from codocs.lang.cobolrun import CobolRunner
from codocs.specforge.docfmt import expected_sections
from codocs.specforge.loop import run_docgen
from codocs.tesearch.loop import run_testgen

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BROKEN_DATEUTIL = SUBPGM.replace("           GOBACK.",
                                 "           MOVE SPACES TO LK-DATE\n           GOBACK.")


def test_broken_fixture_really_differs():
    assert BROKEN_DATEUTIL != SUBPGM and "MOVE SPACES TO LK-DATE" in BROKEN_DATEUTIL


def _settings(**kw) -> Settings:
    st = Settings()
    st.doc_rounds, st.test_rounds, st.mutants, st.concurrency = 2, 1, 0, 3
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _doc(ad, unit, rel: str) -> str:
    src = ad.read(rel)
    head = "\n\n".join(ad.header_lines(rel, unit))
    secs = "\n\n".join(f"## {k} {n}\n**{n}**: does its work."
                       for k, n in expected_sections(src, "cobol", ad))
    return f"<documentation>\n{head}\n\nWhat the file does.\n\n{secs}\n</documentation>"


class Codegen:
    """scripted docgen: docs from the expected sections, code from a function of the rel."""

    def __init__(self, ad, unit, repo: Path, code=None):
        self.ad, self.unit, self.repo = ad, unit, repo
        self.code = code or (lambda rel, text: text)

    def __call__(self, tag, system, user):
        kind, rel = tag.split()[:2]
        if kind == "doc":
            return _doc(self.ad, self.unit, rel)
        if kind == "code":
            return "```cobol\n" + self.code(rel, (self.repo / rel).read_text()) + "```\n"
        if kind == "testgen":
            return {"cbl/DAYSINM.cbl": DAYSINM_PLAN, "cbl/ACCTRPT.cbl": ACCTRPT_PLAN}[rel]
        raise AssertionError(tag)


async def _docgen(repo: Path, out: Path, st: Settings, code=None, files=None, testgen=()):
    ad, runner = bound(repo, out, st)
    units = await ad.discover(runner)
    gen = Codegen(ad, units["cbl"], repo, code)
    llm = ScriptedLLM(gen)
    targets = ad.select_targets(repo, units)
    if testgen:
        await run_testgen([t for t in targets if t.file in testgen], units, runner, llm, st,
                          out, repo)
    sel = [t for t in targets if files is None or t.file in files]
    res = await run_docgen(sel, units, runner, llm, st, out, repo)
    return ad, llm, {r.file: r for r in res}


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    repo = tmp_path / "cobolmod"
    shutil.copytree(FIXTURES / "cobolmod", repo)
    return repo


@pytest.mark.needs_cobc
async def test_signals_per_classification(mod: Path, tmp_path: Path):
    st = _settings(codegen_model="test/codegen-model")
    ad, llm, res = await _docgen(mod, tmp_path / "out", st,
                                 testgen=("cbl/DAYSINM.cbl", "cbl/ACCTRPT.cbl"))
    util = res["cbl/DATEUTIL.cbl"]
    assert (util.signal, util.best_phi, util.early_stop, len(util.rounds)) == \
        ("tests", 1.0, True, 1)
    assert util.rounds[0].eval.relevant == [f"test/TDATEUTL.cbl::T0{i}" for i in (1, 2, 3)]
    for rel in ("cbl/DAYSINM.cbl", "cbl/ACCTRPT.cbl"):
        assert (res[rel].signal, res[rel].best_phi) == ("generated-tests", 1.0), rel
    inq = res["cbl/ACCTINQ.cbl"]
    assert (inq.signal, inq.best_phi) == ("facts", 1.0)
    import json

    ev = json.loads((tmp_path / "out" / "docgen" / "cbl" / "ACCTINQ.cbl" / "round_1" /
                     "eval.json").read_text())
    assert ev["facts_by_class"]["xfer"]["matched"] == 1 and ev["phi_structural"] == 1.0
    assert (tmp_path / "out" / "docgen" / "cbl" / "ACCTRPT.cbl" / "round_1" /
            "code.cbl").is_file()
    doc = (tmp_path / "out" / "docs" / "cbl" / "markdown_docs" / "ACCTINQ.cbl.md").read_text()
    assert doc.startswith("# cbl/ACCTINQ.cbl\n\n**Program**: `ACCTINQ`")
    code_calls = [kw for tag, _s, _u, kw in llm.calls if tag.startswith("code")]
    assert code_calls and all(kw.get("model") == "test/codegen-model" for kw in code_calls)
    assert all("model" not in kw for tag, _s, _u, kw in llm.calls if not tag.startswith("code"))


@pytest.mark.needs_cobc
async def test_broken_and_display_form_regenerations(mod: Path, tmp_path: Path):
    def broken(rel, text):
        return text.replace("           PERFORM 1000-CHECK-MONTH\n", "")

    ad, llm, res = await _docgen(mod, tmp_path / "out", _settings(), broken,
                                 files={"cbl/DATEUTIL.cbl"})
    r = res["cbl/DATEUTIL.cbl"]
    assert r.best_phi < 1.0 and len(r.rounds) == 2
    assert "test/TDATEUTL.cbl::T02" in r.rounds[0].eval.failing
    refine = [u for tag, _s, u, _k in llm.calls if tag == "doc cbl/DATEUTIL.cbl r2"][0]
    assert "test/TDATEUTL.cbl::T02" in refine and "TEST T02 FAIL expected 10" in refine

    def display(rel, text):           # the doc's display form, no area A/B placement
        return "\n".join(ln[6:72].rstrip() for ln in text.split("\n"))

    _, _, res = await _docgen(mod, tmp_path / "out2", _settings(), display,
                              files={"cbl/DATEUTIL.cbl"})
    assert res["cbl/DATEUTIL.cbl"].best_phi == 1.0


@pytest.mark.needs_cobc
async def test_case_of_linking_names(tmp_path: Path):
    repo = tmp_path / "lower"
    shutil.copytree(FIXTURES / "cobolmod", repo)
    for rel, old, new in (("cbl/DATEUTIL.cbl", "PROGRAM-ID. DATEUTIL.", "PROGRAM-ID. dateutil."),
                          ("cbl/ACCTINQ.cbl", "PROGRAM('DATEUTIL')", "PROGRAM('dateutil')"),
                          ("test/TDATEUTL.cbl", "CALL 'DATEUTIL'", "CALL 'dateutil'")):
        p = repo / rel
        p.write_text(p.read_text().replace(old, new))

    def upper(rel, text):
        return text.replace("PROGRAM-ID. dateutil.", "PROGRAM-ID. DATEUTIL.").replace(
            "PROGRAM('dateutil')", "PROGRAM('DATEUTIL')")

    st = _settings(doc_rounds=1)
    _, _, res = await _docgen(repo, tmp_path / "out", st, upper,
                              files={"cbl/DATEUTIL.cbl", "cbl/ACCTINQ.cbl"})
    inq = res["cbl/ACCTINQ.cbl"]
    assert inq.signal == "facts" and inq.best_phi < 1.0
    assert "xfer:ACCTINQ:LINK:dateutil:LK-DATE-PARMS" in inq.rounds[0].eval.failing
    util = res["cbl/DATEUTIL.cbl"]
    assert util.signal == "tests" and util.best_phi < 1.0
    assert "module 'dateutil' not found" in "".join(util.rounds[0].eval.failing.values())


async def test_without_cobc_every_target_gets_facts(mod: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(CobolRunner, "cobc_ok", lambda self: False)
    ad, llm, res = await _docgen(mod, tmp_path / "out", _settings(doc_rounds=1))
    assert {f: (r.status, r.signal) for f, r in res.items()} == {
        f: ("ok", "facts") for f in ("cbl/ACCTINQ.cbl", "cbl/ACCTRPT.cbl", "cbl/DATEUTIL.cbl",
                                      "cbl/DAYSINM.cbl")}
    assert all(r.best_phi == 1.0 for r in res.values())
    assert all(r.rounds[0].eval.build_errors == ["compile gate off: cobc not available"]
               for r in res.values())
    assert sum(tag.startswith("doc") for tag, *_ in llm.calls) == 4
    # an untested unit program without a bundle falls back to facts (with cobc too)
    monkeypatch.setattr(CobolRunner, "cobc_ok", lambda self: True)
    ad, runner = bound(mod, tmp_path / "out3", _settings())
    units = await ad.discover(runner)
    from codocs.baseline import Baseline
    from codocs.specforge.loop import SpecForgeLoop

    loop = SpecForgeLoop(runner, None, _settings(), tmp_path / "out3", mod)
    t = next(t for t in ad.select_targets(mod, units) if t.file == "cbl/DAYSINM.cbl")
    sig = loop._signal(t, units["cbl"], Baseline(pkg="cbl", build_ok=True), None)
    assert sig.name == "facts" and "id:DAYS-IN-MONTH" in sig.relevant
