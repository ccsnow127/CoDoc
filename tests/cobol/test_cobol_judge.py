"""COBOL doc judge: checklist, name check, judgement parsing / metrics, mutation calibration
(offline: fake judge LLM, judge-level cache under OUT/judge/cache)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from judge_fixtures import FakeJudgeLLM
from wiki_fixtures import build, flat_repo

from codocs.cobol.docs import LeafDoc, parse_program_doc
from codocs.cobol.judge.calibrate import (
    FABRICATED_FILE,
    Calibration,
    Mutation,
    calibrate,
    detect,
    mutate,
)
from codocs.cobol.judge.checklist import build_checklist
from codocs.cobol.judge.judge import DocJudge, parse_judgement, quote_in
from codocs.cobol.judge.models import ChecklistItem, Claim, DocJudgement
from codocs.cobol.judge.names import check_names
from codocs.cobol.judge.report import load_judgements, write_judgements
from codocs.cobol.judge.run import run_judge
from codocs.config import Settings

DOC = """\
# ACCT01.cbl

**Program**: `ACCT01` (COBOL; batch program (JCL); unit `repo`)

`ACCT01` posts account balances.

## ClassDef ACCT01
**ACCT01**: Post account balances.

```cobol
 IDENTIFICATION DIVISION.
 PROGRAM-ID. ACCT01.
```

### Invocation
| How invoked | Details |
|---|---|
| JCL | job `ACCTJOB`, step `STEP01` |

### Interface
- The program ends with return code `0`.

### Inputs and outputs
| Resource | Type | Access | Meaning |
|---|---|---|---|
| `IN-FILE` / `PROD.ACCT.MASTER` | file | read | account master |

### Processing logic
`MAIN-PARA` opens the file. It stops when `IN-REC = SPACES`.

### Business rules
1. A record is processed only when `ACCT-ID > 0`. (`MAIN-PARA`)
2. The status must be `'00'` before posting. (`MAIN-PARA`)

### Error handling
None.

### Dependencies
- **Calls**: `DATEUTIL`.

## MethodDef ACCT01.MAIN-PARA
**ACCT01.MAIN-PARA**: Opens the file and calls the date utility.

**Signature**: `MAIN-PARA.`

`ACCT01` reads `IN-FILE` (DD `ACCTFILE`) and calls `DATEUTIL`.
"""


@pytest.fixture
def index(tmp_path: Path):
    return build(flat_repo(tmp_path / "repo"))


def _doc(pid: str = "ACCT01", md: str = DOC) -> LeafDoc:
    doc = parse_program_doc(md, pid)
    return doc if doc is not None else LeafDoc(program_id=pid, markdown=md)


# -- checklist ------------------------------------------------------------------------------


def test_checklist_comes_from_the_index(index):
    items = build_checklist(index, "ACCT01")
    by_cat = {}
    for i in items:
        by_cat.setdefault(i.category, []).append(i)
    assert "ACCTJOB" in by_cat["invocation"][0].question  # JCL job that runs it
    assert any("IN-FILE" in i.question and "INPUT" in i.question for i in by_cat["io"])
    assert any("DATEUTIL" in i.question for i in by_cat["transfer"])
    assert [i.id for i in items] == [f"K{n:02d}" for n in range(1, len(items) + 1)]
    sub = build_checklist(index, "DATEUTIL")
    assert any(i.category == "interface" and "LK-DATE" in i.question for i in sub)
    online = build_checklist(index, "MENU01")
    assert any("MN01" in i.question for i in online)  # RETURN TRANSID / transaction


def test_checklist_cap_keeps_every_category(index):
    items = build_checklist(index, "ACCT01", max_items=3)
    assert len(items) == 3 and len({i.category for i in items}) == 3


# -- deterministic name check ------------------------------------------------------------------


def test_name_check_flags_only_names_that_exist_nowhere(index):
    src = index.program_source("ACCT01")
    md = DOC + (
        "\nAlso writes `AUDIT-TRAIL-FILE`; masks like `YYYY-MM-DD`; CICS words like "
        "`HANDLE CONDITION`; a placeholder `<new-value>`; a prefix `ACCT-*`; the copybook "
        "field `ACCT-ID` and the DSN `PROD.ACCT.MASTER(+1)`.\n"
    )
    nc = check_names(index, "ACCT01", md, [src, index.copybook_source("ACCTREC") or ""])
    assert nc.unknown == ["AUDIT-TRAIL-FILE"]
    assert nc.total > 5 and nc.precision == pytest.approx((nc.total - 1) / nc.total)


# -- parsing and metrics -------------------------------------------------------------------


def test_parse_judgement_normalizes_and_maps_checklist():
    checklist = [ChecklistItem(id="K01", category="io", question="q1"),
                 ChecklistItem(id="K02", category="io", question="q2")]
    raw = "```json\n" + json.dumps({
        "claims": [
            {"id": "C01", "text": "reads IN-FILE", "verdict": "SUPPORTED",
             "error_type": "io", "severity": "high"},
            {"text": "writes the master", "verdict": "contradicted", "error_type": "weird"},
            {"text": "uses LE", "verdict": "maybe"},
            {"text": ""},
        ],
        "checklist": [{"id": "K01", "answer": "Partial", "evidence": "x"}],
        "actionability": {"reasoning": "r", "score": 9}, "clarity": {"score": "4"},
    }) + "\n```"
    j = parse_judgement(raw, "ACCT01", checklist)
    assert [c.verdict for c in j.claims] == ["supported", "contradicted", "unverifiable"]
    assert j.claims[0].error_type == "" and j.claims[0].severity == ""  # only for contradicted
    assert j.claims[1].error_type == "other" and j.claims[1].severity == "medium"
    assert [i.answer for i in j.checklist] == ["partial", ""]
    assert j.actionability.score == 0 and j.clarity.score == 4  # out of range -> not given
    assert parse_judgement("not json", "X", checklist) is None


def test_metrics_and_gate():
    claims = [Claim(id=f"C{i}", text="t", verdict="supported") for i in range(9)]
    claims.append(Claim(id="C9", text="t", verdict="contradicted", severity="low"))
    claims.append(Claim(id="CA", text="t", verdict="unverifiable"))
    items = [ChecklistItem(id="K1", category="io", question="q", answer="yes"),
             ChecklistItem(id="K2", category="io", question="q", answer="partial"),
             ChecklistItem(id="K3", category="io", question="q", answer="no"),
             ChecklistItem(id="K4", category="io", question="q", answer="")]
    j = DocJudgement(program_id="P", claims=claims, checklist=items)
    assert j.accuracy == 0.9 and j.high_errors == 0  # unverifiable left out
    assert j.coverage == pytest.approx(0.5)  # unanswered items left out
    assert j.gate(min_coverage=0.5) == "pass" and j.gate() == "fail"
    j.claims[-2].severity = "high"
    assert j.gate(min_coverage=0) == "fail"  # any high-severity error fails
    assert DocJudgement(program_id="P", error="x").gate() == "error"


def test_quote_check():
    hay = " ".join("OPEN INPUT IN-FILE  CALL 'DATEUTIL'  STOP RUN.".upper().split())
    assert quote_in("  12   OPEN INPUT IN-FILE", hay) is True  # line number prefix stripped
    assert quote_in("OPEN INPUT IN-FILE ... STOP RUN", hay) is True
    assert quote_in("OPEN OUTPUT IN-FILE", hay) is False
    assert quote_in("", hay) is None


# -- judge with a fake LLM ------------------------------------------------------------------


async def test_doc_judge_caches_and_adds_deterministic_layers(index, tmp_path: Path):
    llm = FakeJudgeLLM()
    cache = tmp_path / "out" / "judge" / "cache"
    judge = DocJudge(index, llm, cache)
    j = await judge.judge_one(_doc())
    assert llm.kinds == ["judge"] and len(j.claims) == 2 and j.accuracy == 1.0
    assert j.coverage == 1.0 and len(j.checklist) == len(build_checklist(index, "ACCT01"))
    assert all(c.quote_found is not None for c in j.claims)  # every quote was checked
    assert j.names.total > 0 and j.judge_model == "fake-model"
    assert llm.tags == ["judge ACCT01"] and llm.models == [None]
    assert len(list(cache.glob("*.json"))) == 1  # the parsed judgement
    again = await judge.judge_one(_doc())
    assert llm.kinds == ["judge"] and again.metrics() == j.metrics()  # served from the cache
    fresh = DocJudge(index, llm, cache)  # the cache is on disk: a new judge reuses it
    assert (await fresh.judge_one(_doc())).metrics() == j.metrics() and len(llm.kinds) == 1
    await judge.judge_one(_doc(), salt="repeat:1")
    assert llm.kinds == ["judge", "judge"]  # a salt forces a fresh sample
    assert llm.calls[-1][1].endswith("(sample repeat:1)")


async def test_judge_model_override_is_passed_to_each_call(index):
    llm = FakeJudgeLLM(model="run-model")
    j = await DocJudge(index, llm, None, model="judge/test").judge_one(_doc())
    assert llm.models == ["judge/test"] and j.judge_model == "judge/test"


async def test_unusable_judge_answer_is_an_error(index, tmp_path: Path):
    class Broken(FakeJudgeLLM):
        def answer(self, system, user):
            return "I cannot do that"

    llm = Broken()
    cache = tmp_path / "cache"
    j = await DocJudge(index, llm, cache).judge_one(_doc())
    assert j.error and j.gate() == "error" and llm.kinds == ["judge", "judge"]  # one retry
    assert not cache.exists() or not list(cache.glob("*.json"))  # never cached


# -- calibration ------------------------------------------------------------------------------


def test_mutation_operators_inject_one_error_each():
    doc = _doc()
    paras = ["MAIN-PARA", "READ-PARA", "WRITE-PARA"]
    md, m = mutate(doc, "flip-comparison", paras)
    assert m.needle in ("ACCT-ID <= 0", "IN-REC NOT = SPACES") and m.needle in md
    md, m = mutate(doc, "swap-access", paras)
    assert "| write |" in md and m.needle == "IN-FILE"
    md, m = mutate(doc, "swap-paragraph", paras)
    assert m.needle in ("READ-PARA", "WRITE-PARA") and f"(`{m.needle}`)" in md
    md, m = mutate(doc, "change-value", paras)
    assert m.needle in ("'03'", "3") and md.count(f"`{m.needle}`") == 1
    md, m = mutate(doc, "fabricate", paras)
    assert FABRICATED_FILE in md and md.index(FABRICATED_FILE) > md.index("### Business rules")
    assert md.index(FABRICATED_FILE) < md.index("### Error handling")
    assert mutate(LeafDoc(program_id="X", markdown="## ClassDef X\n\nnothing\n"), "fabricate",
                  paras) is None


def test_comparisons_are_flipped_in_method_prose():
    md = ("## ClassDef P\n**P**: p.\n\n### Business rules\nNone.\n\n"
          "## MethodDef P.MAIN-PARA\n**P.MAIN-PARA**: checks.\n\n"
          "Stops when `WS-COUNT >= 10`.\n")
    res = mutate(LeafDoc(program_id="P", markdown=md), "flip-comparison", ["MAIN-PARA"])
    assert res is not None and res[1].needle == "WS-COUNT < 10"
    assert res[1].section == "MethodDef P.MAIN-PARA"


def test_detect_uses_the_mutated_line_and_the_baseline():
    m = Mutation(program_id="P", operator="change-value",
                 original="2. The status must be `'00'` before posting.",
                 mutated="2. The status must be `'03'` before posting.", needle="'03'")
    hit = Claim(id="C1", text="The status must be '03' before posting", verdict="contradicted")
    other = Claim(id="C2", text="Reads IN-FILE", verdict="contradicted")
    assert detect(m.model_copy(), DocJudgement(program_id="P", claims=[hit])).detected is True
    assert detect(m.model_copy(), DocJudgement(program_id="P", claims=[other])).detected is False
    soft = hit.model_copy(update={"verdict": "unverifiable"})
    got = detect(m.model_copy(), DocJudgement(program_id="P", claims=[soft]))
    assert got.detected is False and got.verdict_found == "unverifiable"
    # the unmutated doc was already contradicted on that line: ambiguous, not credited
    base_hit = Claim(id="C1", text="The status must be '00' before posting",
                     verdict="contradicted", explanation="mentions 03 too")
    base = DocJudgement(program_id="P", claims=[base_hit])
    assert detect(m.model_copy(), DocJudgement(program_id="P", claims=[hit]), base).detected is None


async def test_calibration_measures_sensitivity_stability_and_names(index, tmp_path: Path):
    llm = FakeJudgeLLM()
    judge = DocJudge(index, llm, tmp_path / "cache")
    docs = {"ACCT01": _doc()}
    base = await judge.judge(docs)
    cal = await calibrate(judge, index, docs, base, ["ACCT01"], repeats=3)
    by_op = {m.operator: m for m in cal.mutations}
    # the fake judge contradicts NOT = and the fabricated file, nothing else
    assert by_op["fabricate"].detected is True
    assert by_op["swap-access"].detected is False
    assert by_op["flip-comparison"].detected is (" NOT = " in f" {by_op['flip-comparison'].needle} ")
    assert 0 < cal.sensitivity() < 1
    st = cal.stability[0]
    assert st.runs == 3 and st.checklist_agreement == 1.0 and len(set(st.gates)) == 1
    assert isinstance(cal, Calibration) and cal.name_findings == 0


def _write_doc(out: Path, rel: str, md: str) -> None:
    p = out / "docs" / "markdown_docs" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md)


async def test_run_judge_writes_artifacts(index, tmp_path: Path):
    out = tmp_path / "out"
    _write_doc(out, "ACCT01.cbl", DOC)
    _write_doc(out, "DATEUTIL.cbl", DOC.replace("ACCT01", "DATEUTIL"))
    llm = FakeJudgeLLM()
    summary = await run_judge(index, out, llm, Settings(api_key="k"), calibrate=True,
                              sample=1, repeats=2)
    assert summary["docs"] == 2 and summary["judged"] == ["ACCT01", "DATEUTIL"]
    assert summary["notes"] and summary["notes"][0].startswith("judge fake-model")
    for f in ("summary.json", "summary.md", "calibration.json", "ACCT01.json"):
        assert (out / "judge" / f).exists(), f
    assert list((out / "judge" / "cache").glob("*.json"))
    md = (out / "judge" / "summary.md").read_text()
    assert "Judge calibration" in md and "| [ACCT01](ACCT01.json) |" in md
    assert set(load_judgements(out)) == {"ACCT01", "DATEUTIL"}
    assert "doc_model" not in json.loads((out / "judge" / "ACCT01.json").read_text())
    # a later partial run merges into the summary
    write_judgements({"ACCT01": load_judgements(out)["ACCT01"]}, out)
    assert json.loads((out / "judge" / "summary.json").read_text())["docs"] == 2
    # a resumed judge run is served from the judge cache
    calls = len(llm.kinds)
    again = await run_judge(index, out, llm, Settings(api_key="k"), programs=["ACCT01"])
    assert again["judged"] == ["ACCT01"] and len(llm.kinds) == calls


async def test_run_judge_without_docs_is_an_input_error(index, tmp_path: Path):
    with pytest.raises(ValueError, match="no COBOL docs to judge"):
        await run_judge(index, tmp_path / "out", FakeJudgeLLM(), Settings())
