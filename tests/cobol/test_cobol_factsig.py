"""the facts signal: fact ids per class, the case rules of linking
names, phi = scored x structural Jaccard, facts_by_class, invented facts, the compile gate."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_fixtures import bound, fx

from codocs.cobol.factsig import CLASSES, evaluate, fact_class, facts, readable, split
from codocs.cobol.index import CobolRepoIndex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COBOLMOD = FIXTURES / "cobolmod"


@pytest.fixture(scope="module")
def index() -> CobolRepoIndex:
    return CobolRepoIndex.build(COBOLMOD)


def _facts(index, rel: str, text: str | None = None) -> set[str]:
    return facts(rel, text if text is not None else (COBOLMOD / rel).read_text(), index)


def test_classes_of_cobolmod(index):
    util = _facts(index, "cbl/DATEUTIL.cbl")
    assert {"id:DATEUTIL", "kind:DATEUTIL:program", "copy:DATEUTIL:DATEPARM",
            "using:DATEUTIL:1:10", "data:DATEUTIL:WORKING-STORAGE:WS-LEAP-FLAG:1",
            "para:DATEUTIL:3000-LEAP", "perform:DATEUTIL:0000-MAIN->1000-CHECK-MONTH",
            "call:DATEUTIL:function:DAYS-IN-MONTH", "cond:DATEUTIL:1000-CHECK-MONTH:LK-MONTH<1",
            "cond:DATEUTIL:2000-CHECK-DAY:LK-DAY>WS-MAX-DAY", "cond:DATEUTIL:0000-MAIN:LK-RC-OK",
            "move:DATEUTIL:1000-CHECK-MONTH:'10'->LK-RC"} <= util
    assert "para:DATEUTIL:9999-EXIT" not in util          # trivial paragraphs are no facts
    inq = _facts(index, "cbl/ACCTINQ.cbl")
    assert {"xfer:ACCTINQ:LINK:DATEUTIL:LK-DATE-PARMS", "cics:ACCTINQ:READ:FILE=ACCTDAT",
            "cics:ACCTINQ:RETURN:TRANSID=AINQ", "resp:ACCTINQ:2000-READ-ACCT:NOTFND",
            "when:ACCTINQ:2000-READ-ACCT:WS-RESP:OTHER", "text:ACCTINQ:ACCOUNT NOT FOUND",
            "cond:ACCTINQ:1000-RECEIVE:WS-RESP<>DFHRESP(NORMAL)",
            "data:ACCTINQ:LINKAGE:DFHCOMMAREA:49"} <= inq
    rpt = _facts(index, "cbl/ACCTRPT.cbl")
    assert {"file:ACCTRPT:ACCTIN:LINE SEQUENTIAL:SEQUENTIAL:INPUT", "fd:ACCTRPT:RPTOUT:80",
            "call:ACCTRPT:call:DATEUTIL", "move:ACCTRPT:1000-OPEN:12->RETURN-CODE"} <= rpt
    days = _facts(index, "cbl/DAYSINM.cbl")
    assert {"kind:DAYS-IN-MONTH:function", "returning:DAYS-IN-MONTH:2",
            "when:DAYS-IN-MONTH:0000-MAIN:LK-MON:2", "using:DAYS-IN-MONTH:2:1"} <= days
    assert {fact_class(f) for f in util | inq | rpt | days} <= set(CLASSES)
    struct, scored = split(util)
    assert all(fact_class(f) in ("id", "using", "copy", "data", "para") for f in struct)
    assert all(fact_class(f) in ("perform", "call", "cond", "move") for f in scored)
    assert facts("x.cbl", "not cobol", index) == set()


def test_linking_names_keep_their_case(index):
    src = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. get-var.", "PROCEDURE DIVISION.",
             "    ENTRY 'set-var'", "    CALL 'Logger' USING X",
             "    EXEC CICS XCTL PROGRAM('menu01') END-EXEC", "    GOBACK.")
    f = facts("g.cbl", src, None)
    assert {"id:get-var", "entry:GET-VAR:set-var", "call:GET-VAR:call:Logger",
            "xfer:GET-VAR:XCTL:menu01:-"} <= f
    upper = facts("g.cbl", src.replace("get-var", "GET-VAR"), None)
    assert "id:GET-VAR" in upper and "id:get-var" not in upper
    assert evaluate(f, upper)["phi"] < 1.0


def test_phi_formula_and_messages(index):
    o = _facts(index, "cbl/DATEUTIL.cbl")
    assert evaluate(o, o)["phi"] == 1.0
    worse = (COBOLMOD / "cbl" / "DATEUTIL.cbl").read_text().replace(
        "WHEN LK-MONTH > 12", "WHEN LK-MONTH > 11")
    r = _facts(index, "cbl/DATEUTIL.cbl", worse)
    ev = evaluate(o, r)
    os_, ok = split(o)
    rs, rk = split(r)
    assert ev["phi_structural"] == 1.0
    assert ev["phi_scored"] == round(len(ok & rk) / len(ok | rk), 6)
    assert ev["phi"] == round(ev["phi_scored"] * ev["phi_structural"], 6) and ev["phi"] < 1
    gone = "cond:DATEUTIL:1000-CHECK-MONTH:LK-MONTH>12"
    new = "absent:cond:DATEUTIL:1000-CHECK-MONTH:LK-MONTH>11"
    assert ev["failing"][gone].startswith("the regenerated program lacks the condition "
                                          "`LK-MONTH>12`")
    assert ev["failing"][new].endswith("which the original does not")
    assert new in ev["relevant"] and gone not in ev["passed"]
    assert ev["facts_by_class"]["cond"] == {"original": 6, "matched": 5, "invented": 1}
    # a structural mismatch multiplies phi down
    renamed = _facts(index, "cbl/DATEUTIL.cbl", worse.replace("WS-QUOT", "WS-QUOTX"))
    assert evaluate(o, renamed)["phi_structural"] < 1.0
    assert "PROGRAM-ID" in readable("id:X") and evaluate(set(), set())["phi"] == 1.0


async def test_signal_eval_and_compile_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "cobolmod"
    shutil.copytree(COBOLMOD, repo)
    ad, runner = bound(repo, tmp_path / "out")
    units = await ad.discover(runner)
    target = SimpleNamespace(file="cbl/ACCTINQ.cbl")
    src = ad.read("cbl/ACCTINQ.cbl")
    if runner.cobc_ok():
        ev = await ad.signal_eval("facts", runner, units["cbl"], target, src)
        assert ev.signal == "facts" and ev.build_ok and ev.phi == 1.0 and not ev.build_errors
        d = ev.to_dict()
        assert d["facts_by_class"]["xfer"] == {"original": 1, "matched": 1, "invented": 0}
        assert d["phi_scored"] == 1.0 and d["phi_structural"] == 1.0
        bad = await ad.signal_eval("facts", runner, units["cbl"], target,
                                   src.replace("MOVE MSGO TO CA-MSG", "MOVE MSGO TO TO CA-MSG"))
        assert not bad.build_ok and bad.phi == 0.0
        assert bad.build_errors[0].startswith("cbl/ACCTINQ.cbl:")
    monkeypatch.setattr(type(runner), "cobc_ok", lambda self: False)
    ad._gate.clear()
    off = await ad.signal_eval("facts", runner, units["cbl"], target, src)
    assert off.build_ok and off.phi == 1.0
    assert off.build_errors == ["compile gate off: cobc not available"]
