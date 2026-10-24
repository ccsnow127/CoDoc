"""COBOL testgen prompts: the one-shot prompt, the loop's seed / refine / spec messages and
the system prompt overrides."""

from __future__ import annotations

import shutil
from pathlib import Path

from runner_fixtures import bound

from codocs.cobol.models import CobolProgram, DataItem, FileUse
from codocs.cobol.testmodels import Testability
from codocs.cobol.testprompts import (
    SYSTEM,
    TC_SHAPE_RULE,
    build_test_prompt,
    refine_user,
    seed_user,
    spec_user,
)
from codocs.specforge.prompts import render_template
from codocs.tesearch.measure import Measurement
from codocs.tesearch.prompts import refine_system, seed_system

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _program() -> CobolProgram:
    return CobolProgram(
        program_id="ADDBAL", path="cbl/ADDBAL.cbl", sloc=20,
        using_params=["LK-PARMS"],
        files=[FileUse(name="ACCT-FILE", assign="ACCTFILE", organization="INDEXED",
                       record_key="FD-ID", modes=["INPUT"], fd_record="FD-REC")],
        data_items=[
            DataItem(name="LK-PARMS", section="LINKAGE", copybook="PARMS",
                     children=["05 LK-AMT PIC 9(5)V99", "05 LK-RC PIC XX"]),
            DataItem(name="FD-REC", section="FILE",
                     children=["05 FD-ID PIC 9(5)", "05 FD-DATA PIC X(25)"]),
            DataItem(name="WS-ACCT", section="WORKING-STORAGE",
                     children=["05 WS-ID PIC 9(5)", "05 WS-NAME PIC X(20)",
                               "05 WS-BAL PIC 9(3)V99"]),
        ],
    )


def test_unit_prompt_carries_contract_layouts_and_stubs():
    prog = _program()
    t = Testability(program_id="ADDBAL", mode="unit", reasons=["PROCEDURE DIVISION USING"],
                    callees=["HELPER"], external_callees=["CEE3ABD"])
    src = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. AddBal.\n"
    msgs = build_test_prompt(t, prog, src, {"PARMS": "       05 LK-AMT PIC 9(5)V99.\n"},
                             "# ADDBAL\n**Purpose:** adds", "en")
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert "mode: unit" in user
    assert "CALL 'AddBal' USING LK-PARMS" in user
    assert "TEST <id> PASS" in user
    assert "CEE3ABD" in user and "HELPER" in user
    assert "LK-PARMS (9 bytes, parameter, from COPY PARMS)" in user
    # the WS record of the same size as the FD record is offered for field maps
    assert "FD-REC (30 bytes, FD record of ACCT-FILE, dd ACCTFILE)" in user
    assert "WS-ACCT (30 bytes, same size as the FD record" in user
    assert "### PARMS" in user and "**Purpose:** adds" in user
    assert "-std=ibm" in user and "0001250-" in user


def test_spec_and_batch_prompts():
    prog = _program()
    spec = build_test_prompt(Testability(program_id="ADDBAL", mode="spec",
                                         reasons=["uses EXEC CICS"]), prog, "", {}, None, "ja")
    user = spec[1]["content"]
    assert "mode: spec" in user and "uses EXEC CICS" in user
    assert "Japanese" in user
    assert "Driver rules" not in user
    batch = build_test_prompt(Testability(program_id="ADDBAL", mode="batch"), prog, "", {},
                              None, "en")
    user = batch[1]["content"]
    assert "runs ONCE as a main program" in user
    assert '"expected_return_code"' in user
    assert "{{" not in user  # format escapes resolved


def test_seed_refine_and_spec_messages():
    prog = _program()
    t = Testability(program_id="ADDBAL", mode="unit", callees=[])
    src = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. AddBal.\n"
    legacy = build_test_prompt(t, prog, src, {"PARMS": "x"}, None)[1]["content"]
    seed = seed_user("cbl/ADDBAL.cbl", "directory unit `cbl`", t, prog, src, {"PARMS": "x"},
                     [], None, reference="       CALL 'AddBal' USING X")
    assert seed.startswith("[TARGET FILE] cbl/ADDBAL.cbl\n\n[PACKAGE] directory unit `cbl`")
    for part in legacy.split("\n\n"):
        assert part in seed
    assert seed.endswith(TC_SHAPE_RULE) and "[EXISTING TEST CODE OF THE PROJECT" in seed
    assert "## Documentation of the program" not in seed          # tests come before docs
    refine = refine_user("cbl/ADDBAL.cbl", "unit", t, prog, src, {"PARMS": "x"}, [], None,
                         marked_source="[TARGET SOURCE]\n MOVE 1 TO X  *> <-- NOT COVERED YET",
                         best_block="[CURRENT BEST PLAN - size=1]", last_block="[LAST]",
                         attempt=2)
    assert refine.startswith("[ROUND 2]\n\n[TARGET FILE] cbl/ADDBAL.cbl")
    assert "NOT COVERED YET" in refine and "[CURRENT BEST PLAN" in refine
    assert "## What to produce (mode: unit)" in refine and "## Source of ADDBAL" not in refine
    assert refine.endswith("add cases for the marked gaps.")
    fixing = refine_user("x", "u", t, prog, src, {}, [], None, marked_source="s", fixing=True)
    assert fixing.endswith("while keeping the plan's testing intent.")
    spec_t = Testability(program_id="ADDBAL", mode="spec", reasons=["uses EXEC CICS"])
    assert "## What to produce (mode: spec)" in spec_user(spec_t, prog, src, {})


def test_system_prompt_overrides():
    seed = seed_system("cobol")
    refine = refine_system("cobol")
    assert seed.startswith(SYSTEM) and refine.startswith(SYSTEM)
    assert "inside a single ```json fenced block" in seed
    assert "*> <-- FAILING" in refine and "*> <-- NOT COVERED YET" in refine
    assert "TC-<id>" in seed and "TC-<id>" in refine and "${" not in seed + refine
    # other languages keep the shared template
    assert seed_system("python") == render_template("tesearch_seed_system", "python").rstrip()
    assert render_template("tesearch_seed_system", "python").startswith("You write ONE")


async def test_adapter_test_messages(tmp_path: Path):
    repo = tmp_path / "cobolmod"
    shutil.copytree(FIXTURES / "cobolmod", repo)
    ad, runner = bound(repo, tmp_path / "out")
    units = await ad.discover(runner)
    u = units["cbl"]
    src = ad.read("cbl/DATEUTIL.cbl")
    system, user = ad.test_seed_messages("cbl/DATEUTIL.cbl", u, src, "", set(), [],
                                         ad.test_reference(repo, u, "cbl/DATEUTIL.cbl"))
    assert system == seed_system("cobol")
    assert "# Program under test: DATEUTIL" in user and "CALL 'DATEUTIL' USING LK-DATE-PARMS"\
        in user and "TDATEUTL" in user and "DAYS-IN-MONTH" in user
    best = Measurement(build_ok=True, size=2, green_size=1, cov=0.5, uncovered=[32],
                       survivors=[{"line": 30, "operator": "cmp"}],
                       failing={"cbl/DATEUTIL_codocs_test.cbt::C02": "TEST C02 FAIL rc=00"},
                       suite_src=ad.normalize_suite(
                           '{"driver_source": "IDENTIFICATION DIVISION.\\nPROGRAM-ID. TX.\\n'
                           'PROCEDURE DIVISION.\\nMAIN-PARA.\\n    PERFORM TC-C02\\n'
                           '    STOP RUN.\\nTC-C02.\\n    DISPLAY \'TEST C02 FAIL\'.\\n",'
                           ' "cases": [{"id": "C02"}]}', u, "cbl/DATEUTIL.cbl"))
    last = Measurement(build_ok=False, build_errors=["s.cbt:40: error: x"])
    system, user = ad.test_refine_messages("cbl/DATEUTIL.cbl", u, src, "", set(), [], best,
                                           last, attempt=2, last_round=1)
    assert system == refine_system("cobol")
    assert "[ROUND 2]" in user and "*> <-- NOT COVERED YET" in user
    assert "*> <-- SURVIVING MUTANT: cmp" in user
    assert "[CURRENT BEST PLAN - size=2 green=1 coverage=0.500" in user
    assert "*> <-- FAILING: TEST C02 FAIL rc=00" in user
    assert "[LAST ATTEMPT - did NOT BUILD; avoid these errors]\ns.cbt:40: error: x" in user
    marked = [ln for ln in user.split("\n") if ln.endswith("*> <-- NOT COVERED YET")]
    assert [ln.strip() for ln in marked] == ["MOVE '10' TO LK-RC  *> <-- NOT COVERED YET"]
    assert marked[0].startswith("             MOVE")          # display form: columns 7-72
