"""test oracles: weak (unconditional) PASS lines, unit stdout /
return-code checks, ordered stdout matching, JCL PARM wrappers, stubs that replace repo
callees, layout / CICS / flow grounding in prompts, and spec kinds in the summary."""

from __future__ import annotations

import shutil

import pytest

from codocs.cobol.bundle import mode_label, parse_response, spec_markdown
from codocs.cobol.classify import SPEC_MAINFRAME, SPEC_TERMINAL, classify, spec_kind
from codocs.cobol.harness import (
    CobolTestRunner,
    _decode_field,
    driver_echo,
    match_stdout,
    plan_stubs,
)
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.layout import (
    cics_facts,
    record_fields,
    record_layout,
    value_image,
    working_storage_layouts,
)
from codocs.cobol.models import CobolProgram, DataItem
from codocs.cobol.oracle import weak_cases
from codocs.cobol.reflow import reflow_fixed
from codocs.cobol.testmodels import (
    FixtureFile,
    GeneratedTest,
    Testability,
    TestCaseSpec,
)
from codocs.cobol.testprompts import build_test_prompt

needs_cobc = pytest.mark.skipif(shutil.which("cobc") is None, reason="GnuCOBOL not installed")


def fx(*lines: str) -> str:
    return "\n".join(("      " + ln) if ln.startswith("*") else ("       " + ln)
                     for ln in lines) + "\n"


def drv(body: str) -> str:
    return reflow_fixed("IDENTIFICATION DIVISION.\nPROGRAM-ID. TDRV.\nDATA DIVISION.\n"
                        "WORKING-STORAGE SECTION.\n01 TD-X PIC 9.\n01 TD-ID PIC X(3).\n"
                        "PROCEDURE DIVISION.\n" + body)


# -- weak PASS lines ----------------------------------------------------------------


def test_unconditional_pass_is_weak():
    # cbi LOGGER: CALL then DISPLAY PASS with nothing compared
    src = drv("    CALL 'X' USING TD-X\n    DISPLAY 'TEST C01 PASS'\n"
              "    IF TD-X = 1\n        DISPLAY 'TEST C02 PASS'\n"
              "    ELSE\n        DISPLAY 'TEST C02 FAIL ' TD-X\n    END-IF\n"
              "    IF 1 = 1 DISPLAY 'TEST C03 PASS' ELSE DISPLAY 'TEST C03 FAIL' END-IF\n"
              "    EVALUATE TD-X\n      WHEN 2 DISPLAY 'TEST C04 PASS'\n"
              "      WHEN OTHER DISPLAY 'TEST C04 FAIL'\n    END-EVALUATE\n"
              "    IF TD-X = 3 DISPLAY 'TEST C05 PASS' END-IF\n"
              "    STOP RUN.\n")
    weak = weak_cases(src, ["C01", "C02", "C03", "C04", "C05", "C06"])
    assert set(weak) == {"C01", "C03", "C05"}
    assert "unconditionally" in weak["C01"] and "unconditionally" in weak["C03"]
    assert "no FAIL path" in weak["C05"]


def test_pass_through_conditional_perform_and_variable_ids():
    src = drv("MAIN-PARA.\n    MOVE 'C01' TO TD-ID\n    IF TD-X = 1\n"
              "        PERFORM SHOW-PASS\n    ELSE\n        PERFORM SHOW-FAIL\n    END-IF\n"
              "    STOP RUN.\nSHOW-PASS.\n    DISPLAY 'TEST ' TD-ID ' PASS'.\n"
              "SHOW-FAIL.\n    DISPLAY 'TEST ' TD-ID ' FAIL'.\n")
    assert weak_cases(src, ["C01", "C02"]) == {}
    # the same paragraph PERFORMed unconditionally vouches for nothing
    bad = drv("MAIN-PARA.\n    MOVE 'C01' TO TD-ID\n    PERFORM SHOW-PASS\n    STOP RUN.\n"
              "SHOW-PASS.\n    DISPLAY 'TEST ' TD-ID ' PASS'.\n")
    assert set(weak_cases(bad, ["C01"])) == {"C01"}
    # a READ ... AT END phrase is a condition on observed data
    phrase = drv("    READ F AT END DISPLAY 'TEST C01 PASS'\n"
                 "    NOT AT END DISPLAY 'TEST C01 FAIL' END-READ\n    STOP RUN.\n")
    assert weak_cases(phrase, ["C01"]) == {}


def test_parse_response_records_weak_cases_ends_run_and_stdout_items():
    t = Testability(program_id="P", mode="unit")
    raw = ('{"driver_source": "IDENTIFICATION DIVISION.\\nPROGRAM-ID. TP.\\n'
           'PROCEDURE DIVISION.\\n    CALL \'P\'\\n    DISPLAY \'TEST C01 PASS\'\\n'
           '    STOP RUN.\\n", "expected_stdout": ["A", {"line": "B  "}, {"text": "C"}, ""],'
           ' "cases": [{"id": "C01"}, {"id": "C02", "ends_run": true}]}')
    test = parse_response(raw, t)
    assert set(test.weak_cases) == {"C01"}
    assert test.expected_stdout == ["A", {"line": "B  "}, "C"]
    assert [c.ends_run for c in test.cases] == [False, True]
    b = parse_response('{"parm": "2022071800", "expected_stdout": ["X"]}',
                       Testability(program_id="P", mode="batch", parm_param="PARMS"))
    assert b.parm == "2022071800" and not b.error


# -- ordered stdout matching ----------------------------------------------------------


def test_match_stdout_is_an_ordered_consuming_subsequence():
    out = "HEADER\nREC 1 Total paid is: 185.00\nMISSING GROUP\nREC 2\nEND\n"
    ok = match_stdout(["HEADER", "Total paid is:", "185.00", "MISSING GROUP", "REC 2",
                       {"line": "END   "}], out)
    assert [c.status for c in ok] == ["pass"] * 6
    # a line printed once cannot satisfy two expectations; order is enforced
    dup = match_stdout(["MISSING GROUP", "MISSING GROUP"], out)
    assert [c.status for c in dup] == ["pass", "fail"]
    assert "only found before" in dup[1].detail
    order = match_stdout(["REC 2", "HEADER"], out)
    assert order[1].status == "fail" and "out of order" in order[1].detail
    # a whole-line expectation does not match a substring, nor a consumed line
    assert match_stdout([{"line": "REC"}], out)[0].status == "fail"
    assert match_stdout(["REC 1", {"line": "REC 1 Total paid is: 185.00"}],
                        out)[1].status == "fail"
    assert "missing text" in match_stdout(["NOPE"], out)[0].detail


def test_driver_echo_detection():
    driver = drv("    DISPLAY 'CASE ONE DONE'\n    DISPLAY 'TEST C01 PASS'\n    STOP RUN.\n")
    assert driver_echo("TEST C01 PASS", driver, "")
    assert driver_echo("CASE ONE", driver, "DISPLAY 'OTHER'")
    assert not driver_echo("CASE ONE", driver, "DISPLAY 'CASE ONE DONE'")  # SUT prints it too
    assert not driver_echo("RESULT 42", driver, "")


def test_text_fields_ignore_trailing_nuls():
    item = DataItem(name="R", children=["05 R-DESC PIC X(10)"])
    f = record_fields(item)[1][0]
    assert _decode_field(f, b"Int. \x00\x00\x00\x00\x00") == "Int."


# -- JCL PARM programs ----------------------------------------------------------------

PARM_REPO = {
    "cbl/PARMPGM.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. PARMPGM.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "01 WS-N PIC 9 VALUE 0.", "LINKAGE SECTION.",
        "01 EXTERNAL-PARMS.", "    05 PARM-LENGTH PIC S9(04) COMP.",
        "    05 PARM-DATE PIC X(10).", "PROCEDURE DIVISION USING EXTERNAL-PARMS.",
        "MAIN-PARA.", "    DISPLAY 'START'", "    DISPLAY 'LEN ' PARM-LENGTH",
        "    DISPLAY 'DATE ' PARM-DATE(1:PARM-LENGTH)", "    DISPLAY 'DUP'",
        "    DISPLAY 'DUP'", "    IF PARM-LENGTH = 0", "        MOVE 12 TO RETURN-CODE",
        "        STOP RUN", "    END-IF", "    CALL 'HELPER' USING WS-N",
        "    DISPLAY 'HELPER SAYS ' WS-N", "    MOVE 4 TO RETURN-CODE", "    GOBACK.",
    ),
    "cbl/HELPER.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. HELPER.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-N PIC 9.", "PROCEDURE DIVISION USING LK-N.",
        "    MOVE 1 TO LK-N", "    CALL 'DEEP' USING LK-N", "    GOBACK.",
        "ENTRY 'HELPER2' USING LK-N.", "    MOVE 2 TO LK-N", "    GOBACK.",
    ),
    "cbl/DEEP.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. DEEP.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-N PIC 9.", "PROCEDURE DIVISION USING LK-N.",
        "    ADD 1 TO LK-N", "    GOBACK.",
    ),
    "cbl/USER2.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. USER2.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "01 WS-N PIC 9.", "LINKAGE SECTION.",
        "01 LK-X PIC X.", "PROCEDURE DIVISION USING LK-X.",
        "    CALL 'HELPER' USING WS-N", "    CALL 'HELPER2' USING WS-N",
        "    DISPLAY 'N=' WS-N", "    IF LK-X = 'S'", "        DISPLAY 'STOPPING'",
        "        MOVE 12 TO RETURN-CODE", "        STOP RUN", "    END-IF", "    GOBACK.",
    ),
    "jcl/RUNPARM.jcl": "//RUNPARM JOB 'X',CLASS=A\n"
                       "//STEP1 EXEC PGM=PARMPGM,PARM='2022071800'\n"
                       "//SYSOUT DD SYSOUT=*\n",
}


@pytest.fixture(scope="module")
def prepo(tmp_path_factory):
    root = tmp_path_factory.mktemp("parmrepo")
    for rel, text in PARM_REPO.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return CobolRepoIndex.build(root)


def test_jcl_parm_program_is_batch_with_parm_wrapper(prepo):
    t = classify(prepo.programs["PARMPGM"], prepo)
    assert t.mode == "batch" and t.parm_param == "EXTERNAL-PARMS"
    assert t.jcl_parms == ["2022071800"] and "PARM wrapper" in t.reasons[0]
    # a USING program that repo code calls stays a unit
    assert classify(prepo.programs["HELPER"], prepo).mode == "unit"
    user = build_test_prompt(t, prepo.programs["PARMPGM"],
                             prepo.program_source("PARMPGM"), {}, None)[1]["content"]
    assert "### JCL PARM" in user and "PARM='2022071800'" in user
    assert "EXTERNAL-PARMS (12 bytes, parameter" in user
    assert '"parm": "<the PARM text>"' in user


@needs_cobc
def test_parm_wrapper_run_and_ordered_stdout(prepo, tmp_path):
    t = classify(prepo.programs["PARMPGM"], prepo)
    test = GeneratedTest(program_id="PARMPGM", mode="batch", testability=t,
                         parm="2024-01-31",
                         expected_stdout=["START", {"line": "LEN +00010"}, "DATE 2024-01-31",
                                          "DUP", "DUP", "HELPER SAYS 2"],
                         expected_return_code=4)
    res = CobolTestRunner(prepo, tmp_path / "w").run(test)
    assert res.status == "passed", (res.cases, res.run_log, res.compile_log)
    assert "PARM wrapper" in res.compile_log
    assert res.paragraphs_hit == res.paragraphs_total == 1  # SUT traced through the wrapper
    assert res.real_callees == ["DEEP", "HELPER"]
    # a third DUP is not there: consumption makes the duplicate count
    test.expected_stdout.append("DUP")
    res = CobolTestRunner(prepo, tmp_path / "w").run(test)
    assert res.status == "failed" and res.cases[-1].status == "fail"
    # no parm text: the JCL PARM is used
    test.parm, test.expected_stdout = None, ["DATE 2022071800"]
    assert CobolTestRunner(prepo, tmp_path / "w").run(test).status == "passed"


# -- stubs that replace repo callees ----------------------------------------------------

STUB_HELPER = """IDENTIFICATION DIVISION.
PROGRAM-ID. helper.
DATA DIVISION.
LINKAGE SECTION.
01 LK-N PIC 9.
PROCEDURE DIVISION USING LK-N.
    MOVE 7 TO LK-N
    GOBACK.
"""


def test_plan_stubs_replaces_callees_and_keeps_needed_entry_owners(prepo):
    plan = plan_stubs(prepo, "PARMPGM", STUB_HELPER)
    assert plan.replaced == ["HELPER"]
    assert plan.linked == []  # DEEP was reachable only through HELPER
    assert plan.chunks[0][1] == ["HELPER"]  # renamed to the CALL literal's spelling
    assert "PROGRAM-ID. HELPER." in plan.chunks[0][0]
    # USER2 also calls ENTRY HELPER2: stubbing only HELPER would drop that entry
    plan = plan_stubs(prepo, "USER2", STUB_HELPER)
    assert plan.replaced == [] and plan.linked == ["DEEP", "HELPER"]
    assert "still needed for HELPER2" in plan.skipped[0]
    both = STUB_HELPER + STUB_HELPER.replace("helper.", "HELPER2.")
    plan = plan_stubs(prepo, "USER2", both)
    assert plan.replaced == ["HELPER"] and len(plan.chunks) == 2
    # the program under test is never stubbed
    plan = plan_stubs(prepo, "HELPER", STUB_HELPER)
    assert plan.chunks == [] and "program under test" in plan.skipped[0]


@needs_cobc
def test_stub_is_linked_instead_of_the_repo_callee(prepo, tmp_path):
    t = classify(prepo.programs["PARMPGM"], prepo)
    test = GeneratedTest(program_id="PARMPGM", mode="batch", testability=t,
                         parm="X", stubs_source=reflow_fixed(STUB_HELPER),
                         expected_stdout=["HELPER SAYS 7"])
    res = CobolTestRunner(prepo, tmp_path / "w").run(test)
    assert res.status == "passed", (res.cases, res.run_log, res.compile_log)
    assert res.stubbed_callees == ["HELPER"] and res.real_callees == []
    assert "replaced by LLM stubs: HELPER" in res.compile_log


# -- unit runs: weak cases, stdout / return-code checks, ends_run ---------------------

UNIT_DRIVER = """IDENTIFICATION DIVISION.
PROGRAM-ID. TUSER2.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 TD-X PIC X.
PROCEDURE DIVISION.
    MOVE 'A' TO TD-X
    CALL 'USER2' USING TD-X
    DISPLAY 'TEST C01 PASS'
    MOVE 'S' TO TD-X
    CALL 'USER2' USING TD-X
    DISPLAY 'TEST C02 PASS'
    STOP RUN.
"""


@needs_cobc
def test_unit_run_grades_weak_stdout_rc_and_ending_case(prepo, tmp_path):
    t = classify(prepo.programs["USER2"], prepo)
    test = GeneratedTest(
        program_id="USER2", mode="unit", testability=t, driver_name="TUSER2",
        driver_source=reflow_fixed(UNIT_DRIVER),
        cases=[TestCaseSpec(id="C01"), TestCaseSpec(id="C02", ends_run=True)],
        expected_stdout=["N=2", "TEST C01 PASS", "STOPPING"], expected_return_code=12,
    )
    res = CobolTestRunner(prepo, tmp_path / "w").run(test)
    by = {c.id: c for c in res.cases}
    assert by["C01"].status == "weak"  # PASS without a comparison
    assert by["return-code"].status == "pass"  # the SUT's STOP RUN with 12
    # the driver's own "TEST C01 PASS" line is dropped: N=2 and STOPPING remain
    assert by["stdout-1"].status == "pass" and by["stdout-2"].status == "pass"
    assert "stdout-3" not in by
    assert by["C02"].status == "pass" and "graded by" in by["C02"].detail
    assert res.weak == 1 and res.passed == 4 and res.failed == 0
    assert res.status == "passed"
    test.expected_return_code = 0
    res = CobolTestRunner(prepo, tmp_path / "w").run(test)
    by = {c.id: c for c in res.cases}
    assert by["C02"].status == "fail" and "return-code" in by["C02"].detail
    assert res.status == "failed"
    # only weak checks: the program is 'weak', not 'passed'
    test.expected_stdout, test.expected_return_code = ["TEST C01 PASS"], None
    test.cases = [TestCaseSpec(id="C01")]
    assert CobolTestRunner(prepo, tmp_path / "w").run(test).status == "weak"


# -- layouts with VALUEs, WORKING-STORAGE groups, flow hazards -------------------------

CSUTLDTC_LIKE = fx(
    "IDENTIFICATION DIVISION.", "PROGRAM-ID. DTC.", "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 WS-DATE-TO-TEST.", "   02 VSTRING-LENGTH PIC S9(4) BINARY.",
    "   02 VSTRING-TEXT PIC X(10).",
    "01 WS-MESSAGE.", "   02 WS-SEVERITY PIC X(04).",
    "   02 WS-SEVERITY-N REDEFINES WS-SEVERITY PIC 9(4).",
    "   02 FILLER PIC X(11) VALUE 'Mesg Code:'.", "   02 WS-MSG-NO PIC X(04).",
    "   02 FILLER PIC X(01) VALUE SPACE.", "   02 WS-DATE PIC X(10) VALUE SPACES.",
    "01 WS-ST.", "   05 WS-ST1 PIC X.", "   05 WS-ST2 PIC X.",
    "LINKAGE SECTION.", "01 LS-RESULT PIC X(80).",
    "PROCEDURE DIVISION USING LS-RESULT.", "    MOVE WS-DATE-TO-TEST TO WS-DATE",
    "    MOVE WS-MESSAGE TO LS-RESULT", "    DISPLAY WS-ST", "    GOBACK.",
)


def test_layouts_show_values_and_the_groups_that_reach_the_output(tmp_path):
    (tmp_path / "dtc.cbl").write_text(CSUTLDTC_LIKE)
    idx = CobolRepoIndex.build(tmp_path)
    prog = idx.programs["DTC"]
    msg = next(d for d in prog.data_items if d.name == "WS-MESSAGE")
    _, rows, _ = record_layout(msg)
    assert any("VALUE 'Mesg Code:' (holds 'Mesg Code: ': 11 bytes)" in r for r in rows)
    assert value_image(msg) == "~~~~Mesg Code: ~~~~           "
    blocks = working_storage_layouts(prog, idx.program_source("DTC"), "ibm")
    assert blocks[0].startswith("WS-DATE-TO-TEST (12 bytes, WORKING-STORAGE, MOVEd")
    assert "initial bytes: [##~~~~~~~~~~]" in blocks[0]
    assert blocks[1].startswith("WS-MESSAGE (30 bytes, WORKING-STORAGE, MOVEd as a whole, "
                                "has REDEFINES)")
    assert len(blocks) == 2  # the 2-byte status group is left out
    user = build_test_prompt(classify(prog, idx), prog, idx.program_source("DTC"), {},
                             None)[1]["content"]
    assert "WORKING-STORAGE groups whose bytes reach the output" in user
    assert "X'09CB' -> 2507" in user and "leaves FILLER unchanged" in user
    assert "NEXT SENTENCE" not in user


def test_flow_hazards_and_next_sentence_rule():
    src = fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. NS.", "PROCEDURE DIVISION.",
             "    PERFORM UNTIL 1 = 2", "      IF 1 = 1 NEXT SENTENCE END-IF",
             "    END-PERFORM.", "    STOP RUN.")
    prog = CobolProgram(program_id="NS", path="ns.cbl")
    t = Testability(program_id="NS", mode="batch")
    user = build_test_prompt(t, prog, src, {}, None)[1]["content"]
    assert "### Control-flow hazards" in user and "leaves the loop" in user
    hazard = "line 5: NEXT SENTENCE inside IF ... END-IF within inline PERFORM"
    prog_h = prog.model_copy()
    object.__setattr__(prog_h, "flow_hazards", [hazard])  # the analysis-side field
    assert f"- {hazard}" in build_test_prompt(t, prog_h, src, {}, None)[1]["content"]


def test_prompt_rules_for_oracles_stubs_and_ends_run():
    prog = CobolProgram(program_id="P", path="p.cbl", using_params=["L"])
    t = Testability(program_id="P", mode="unit", callees=["Q"])
    user = build_test_prompt(t, prog, "", {}, None)[1]["content"]
    assert "Never DISPLAY PASS unconditionally" in user
    assert '"ends_run": true' in user and '"ends_run": false' in user
    assert "links your stub INSTEAD of the real program" in user
    assert "ordered subsequence" in user and '{"line": "..."}' in user


# -- CICS spec grounding; spec kinds -----------------------------------------------------

LGTESTP1_LIKE = fx(
    "IDENTIFICATION DIVISION.", "PROGRAM-ID. ONLINE1.", "DATA DIVISION.",
    "WORKING-STORAGE SECTION.", "01 COMM-AREA.", "   03 CA-REQUEST-ID PIC X(6).",
    "   03 CA-RETURN-CODE PIC 9(2).", "   03 CA-CUSTOMER-NUM PIC 9(10).",
    "PROCEDURE DIVISION.", "    IF EIBCALEN > 0 GO TO A-GAIN.",
    "    EXEC CICS FORMATTIME ABSTIME(X) MMDDYYYY(Y) END-EXEC.",
    "A-GAIN.", "    EXEC CICS RETURN TRANSID('SSP1') COMMAREA(COMM-AREA) END-EXEC.",
)
LGACDB01_LIKE = fx(
    "IDENTIFICATION DIVISION.", "PROGRAM-ID. ONLINE2.", "DATA DIVISION.",
    "LINKAGE SECTION.", "01 DFHCOMMAREA.", "   03 CA-REQUEST-ID PIC X(6).",
    "   03 CA-RETURN-CODE PIC 9(2).", "PROCEDURE DIVISION.",
    "    IF EIBCALEN = 0 EXEC CICS ABEND ABCODE('LGCA') END-EXEC END-IF",
    "    EXEC CICS RETURN END-EXEC.",
)


def test_cics_spec_prompt_is_grounded(tmp_path):
    (tmp_path / "o1.cbl").write_text(LGTESTP1_LIKE)
    (tmp_path / "o2.cbl").write_text(LGACDB01_LIKE)
    idx = CobolRepoIndex.build(tmp_path)
    p1, p2 = idx.programs["ONLINE1"], idx.programs["ONLINE2"]
    t1 = classify(p1, idx)
    assert t1.mode == "spec" and t1.spec_kind == SPEC_MAINFRAME
    facts = cics_facts(p1, idx.program_source("ONLINE1"), "ibm")
    assert "NO DFHCOMMAREA in LINKAGE" in facts and "tests EIBCALEN" in facts
    assert "COMM-AREA (WORKING-STORAGE)" in facts
    assert "COMM-AREA (18 bytes, WORKING-STORAGE, passed as COMMAREA)" in facts
    assert "    7     2  03 CA-RETURN-CODE PIC 9(2)" in facts
    user = build_test_prompt(t1, p1, idx.program_source("ONLINE1"), {}, None)[1]["content"]
    assert "WORKING-STORAGE is fresh in every task" in user
    assert "returns digits only" in user and "addressing exception" in user
    facts2 = cics_facts(p2, idx.program_source("ONLINE2"), "ibm")
    assert "DFHCOMMAREA is declared in LINKAGE (8 bytes)" in facts2
    assert "DFHCOMMAREA (8 bytes, LINKAGE, the COMMAREA received)" in facts2


def test_terminal_spec_kind_prompt_and_summary():
    term = Testability(program_id="T", mode="spec", spec_kind=SPEC_TERMINAL,
                       reasons=["uses terminal I/O (...): needs a curses terminal"])
    user = build_test_prompt(term, CobolProgram(program_id="T", path="t.cbl"), "", {},
                             None)[1]["content"]
    assert "needs an interactive (curses) terminal" in user and "CICS / DB2" not in user
    # derived from the reasons for older plans without spec_kind
    old = Testability(program_id="O", mode="spec", reasons=["uses EXEC CICS"])
    assert spec_kind(old) == SPEC_MAINFRAME
    assert spec_kind(Testability(program_id="X", mode="spec",
                                 reasons=["uses screen I/O (SCREEN SECTION / BMS maps)"])) \
        == SPEC_TERMINAL
    tests = {
        "T": GeneratedTest(program_id="T", mode="spec", testability=term,
                           cases=[TestCaseSpec(id="S1")]),
        "O": GeneratedTest(program_id="O", mode="spec", testability=old,
                           cases=[TestCaseSpec(id="S1")]),
        "U": GeneratedTest(program_id="U", mode="unit", cases=[TestCaseSpec(id="C01")]),
    }
    # the summarize half lives in test_cobol_testrun.py (cobol.testrun.summarize)
    assert mode_label(tests["T"]) == "spec (terminal)"
    assert mode_label(tests["O"]) == "spec (CICS/DB2)"
    assert mode_label(tests["U"]) == "unit"
    assert "needs an interactive terminal" in spec_markdown(tests["T"])
    assert "off-mainframe" in spec_markdown(tests["O"])


def test_fixture_file_model_unchanged():
    # plan.json in the older format (plain-string stdout entries) still loads
    t = GeneratedTest.model_validate({"program_id": "P", "mode": "batch",
                                      "expected_stdout": ["a"],
                                      "inputs": [FixtureFile(dd="I", path="i").model_dump()]})
    assert t.expected_stdout == ["a"] and t.parm is None and t.weak_cases == {}
