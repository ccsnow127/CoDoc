"""the COBOL test bundle: JSON reply normalisation, the bundle format and its round trip,
test names and lines, test removal and the expanded delivery layout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_fixtures import DATEUTIL_PLAN, bound, fx

from codocs.cobol import bundle
from codocs.cobol.bundle import parse_response, rename_program
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.testmodels import FixtureFile, GeneratedTest, Testability, TestCaseSpec

FILES = {
    "ADDONE.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. ADDONE.", "DATA DIVISION.",
        "LINKAGE SECTION.", "01 LK-N PIC 9(3).", "PROCEDURE DIVISION USING LK-N.",
        "    ADD 1 TO LK-N", "    GOBACK.",
    ),
    "TADDONE.cbl": fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. TADDONE.", "PROCEDURE DIVISION.",
        "    STOP RUN.",
    ),
}

UNIT_RESPONSE = {
    "driver_name": "TADDONE",
    "driver_source": "IDENTIFICATION DIVISION.\nPROGRAM-ID. TADDONE.\nDATA DIVISION.\n"
                     "WORKING-STORAGE SECTION.\n01 TD-N PIC 9(3).\nPROCEDURE DIVISION.\n"
                     "MAIN-PARA.\n    PERFORM TC-C-01\n    PERFORM TC-C-01x\n    STOP RUN.\n"
                     "TC-C-01.\n    MOVE 1 TO TD-N\n    CALL 'ADDONE' USING TD-N\n"
                     "    IF TD-N = 2 DISPLAY 'TEST C-01 PASS' ELSE DISPLAY 'TEST C-01 FAIL' "
                     "END-IF.\nTC-C-01x.\n    DISPLAY 'TEST C-01x PASS'.\n"
                     "END PROGRAM TADDONE.\n",
    "stubs_source": "",
    "inputs": [{"dd": "INFILE", "path": "../../etc/in.txt",
                "content": [{"ACCT-ID": 1, "ACCT-NAME": "BOB"}, "raw line"]}],
    "expected_stdout": ["DONE", {"line": "TOTAL 2"}, "TEST C-01 PASS"],
    "expected_return_code": "0",
    "cases": [
        {"id": "C 01", "title": "adds one", "inputs": {"LK-N": 1}, "expected": {"LK-N": "2"}},
        {"id": "C 01", "title": "duplicate id"},
    ],
}


@pytest.fixture()
def index(tmp_path: Path) -> CobolRepoIndex:
    for rel, text in FILES.items():
        (tmp_path / rel).write_text(text)
    return CobolRepoIndex.build(tmp_path)


def test_parse_response_errors():
    t = Testability(program_id="X", mode="unit")
    assert parse_response("no json here", t).error == "response is not a JSON object"
    assert parse_response('{"cases": []}', t).error == "no driver_source in response"
    b = Testability(program_id="X", mode="batch")
    assert "no inputs" in parse_response('{"cases": []}', b).error
    ok = parse_response('```json\n{"inputs": [{"dd": "IN", "content": "a\\nb"}]}\n```', b)
    assert not ok.error and ok.inputs[0].content == "a\nb" and ok.inputs[0].path == "IN"


def test_rename_program():
    src = "       PROGRAM-ID. TOLD.\n       END PROGRAM TOLD.\n       PROGRAM-ID. TOLDX.\n"
    out = rename_program(src, "TOLD", "TNEW")
    assert "PROGRAM-ID. TNEW." in out and "END PROGRAM TNEW." in out
    assert "PROGRAM-ID. TOLDX." in out


def test_normalize_a_reply(index: CobolRepoIndex):
    t = Testability(program_id="ADDONE", mode="unit")
    text = bundle.normalize(json.dumps(UNIT_RESPONSE), t, index, "ADDONE.cbl")
    assert text.startswith(bundle.MAGIC + "\n*> target: ADDONE.cbl\n*> program: ADDONE\n"
                           "*> mode: unit\n*> === PLAN ===\n")
    b = bundle.parse_bundle(text)
    test = bundle.to_generated(b, t)
    # TADDONE is a repo program: the harness renames the driver
    assert test.driver_name != "TADDONE" and b.plan["driver_name"] == test.driver_name
    assert f"PROGRAM-ID. {test.driver_name}." in test.driver_source
    assert all(len(ln) <= 72 for ln in test.driver_source.split("\n"))
    assert [c.id for c in test.cases] == ["C-01", "C-01x"]
    assert test.cases[0].inputs == {"LK-N": "1"}
    assert test.inputs[0].path == "etc/in.txt"            # never escapes the run dir
    assert test.inputs[0].records == [{"ACCT-ID": "1", "ACCT-NAME": "BOB"}, "raw line"]
    # the driver's own TEST line is not a stdout check; the others get stable ids
    assert [x["id"] for x in b.plan["expected_stdout"]] == ["stdout-01", "stdout-02"]
    assert test.expected_stdout == ["DONE", {"line": "TOTAL 2"}]
    assert test.expected_return_code == 0
    # RUN markers start every TC paragraph
    assert "DISPLAY 'TEST C-01 RUN'" in test.driver_source
    assert "DISPLAY 'TEST C-01x RUN'" in test.driver_source
    names = bundle.test_names(text, "s.cbt")
    assert names == ["s.cbt::C-01", "s.cbt::C-01x", "s.cbt::return-code", "s.cbt::stdout-01",
                     "s.cbt::stdout-02"]
    # normalising the bundle again changes nothing (refine rounds may echo it)
    assert bundle.normalize(text, t, index, "ADDONE.cbl") == text
    long_id = {"driver_source": "IDENTIFICATION DIVISION.\nPROGRAM-ID. TX.\n"
                                "PROCEDURE DIVISION.\n    STOP RUN.\n",
               "cases": [{"id": "A" * 40}, {"id": "A" * 40}]}
    capped = bundle.to_generated(bundle.normalize(json.dumps(long_id), t, index, "x"), t)
    assert [len(c.id) for c in capped.cases] == [30, 30] and \
        capped.cases[0].id != capped.cases[1].id


def test_malformed_and_spec_bundles():
    t = Testability(program_id="X", mode="unit")
    bad = bundle.normalize("no json", t, None, "x.cbl")
    b = bundle.parse_bundle(bad)
    assert b.error == "response is not a JSON object" and bundle.test_names(bad, "s") == []
    assert bundle.to_generated(bad, t).error
    spec_t = Testability(program_id="S", mode="spec", reasons=["uses EXEC CICS"])
    spec = bundle.normalize('{"cases": [{"id": "S1", "title": "t", "given": "g"}]}', spec_t,
                            None, "s.cbl")
    assert bundle.DRIVER_MARK not in spec and bundle.test_names(spec, "s") == []
    assert bundle.to_generated(spec, spec_t).cases[0].given == "g"
    assert not bundle.is_bundle("{}") and bundle.parse_bundle("{}") is None


def test_round_trip_and_batch_names():
    test = GeneratedTest(
        program_id="BATCHP", mode="batch",
        cases=[TestCaseSpec(id="B1", title="one run")],
        inputs=[FixtureFile(dd="IN", path="in/data.txt", content="a\nb")],
        expected_files=[FixtureFile(dd="OUT", path="out.txt", records=[{"F": "1"}, "raw"],
                                    content='{"F": "1"}\nraw')],
        expected_stdout=["x", {"line": "y"}], expected_return_code=4, stdin=["s"],
        env={"E": "1"}, parm=None)
    text = bundle.from_generated(test, "b.cbl")
    back = bundle.to_generated(text)
    assert back.model_dump(exclude={"testability"}) == test.model_dump(exclude={"testability"})
    assert bundle.from_generated(back, "b.cbl") == text
    names = bundle.test_names(text, "u/B_codocs_test.cbt")
    assert names == ["u/B_codocs_test.cbt::return-code", "u/B_codocs_test.cbt::stdout-01",
                     "u/B_codocs_test.cbt::stdout-02", "u/B_codocs_test.cbt::file-OUT"]
    lines = bundle.test_lines(text, "u/B_codocs_test.cbt")
    rows = text.split("\n")
    assert '"expected_return_code"' in rows[lines["u/B_codocs_test.cbt::return-code"] - 1]
    assert '"stdout-02"' in rows[lines["u/B_codocs_test.cbt::stdout-02"] - 1]
    assert '"dd": "OUT"' in rows[lines["u/B_codocs_test.cbt::file-OUT"] - 1]
    # checks are removed from the plan; stdout ids are never renumbered
    fewer = bundle.remove_tests(text, {"u/B_codocs_test.cbt::stdout-01",
                                       "u/B_codocs_test.cbt::file-OUT"})
    assert bundle.test_names(fewer, "r") == ["r::return-code", "r::stdout-02"]


def test_case_lines_and_removal(index: CobolRepoIndex):
    t = Testability(program_id="ADDONE", mode="unit")
    text = bundle.normalize(DATEUTIL_PLAN, t, index, "cbl/DATEUTIL.cbl")
    rows = text.split("\n")
    lines = bundle.test_lines(text, "s")
    assert set(lines) == {"s::C01", "s::C02", "s::C03", "s::C04"}
    assert "DISPLAY 'TEST C02 PASS'" in rows[lines["s::C02"] - 1]
    b = bundle.parse_bundle(text)
    assert rows[b.driver_line - 1] == bundle.DRIVER_MARK
    # a TC paragraph is deleted with its PERFORM line
    out = bundle.remove_tests(text, {"s::C02"})
    drv = bundle.parse_bundle(out).driver
    assert "TC-C02" not in drv and "TEST C02" not in drv and "TC-C03." in drv
    assert bundle.test_names(out, "s") == ["s::C01", "s::C03", "s::C04"]
    assert [c["id"] for c in bundle.parse_bundle(out).plan["cases"]] == ["C01", "C03", "C04"]
    # without a TC paragraph the case's literals are muted (same length)
    flat = bundle.from_generated(GeneratedTest(
        program_id="ADDONE", mode="unit", driver_name="TX",
        driver_source=fx("IDENTIFICATION DIVISION.", "PROGRAM-ID. TX.", "PROCEDURE DIVISION.",
                         "    DISPLAY 'TEST K1 PASS'", "    DISPLAY 'TEST K2 PASS'",
                         "    STOP RUN."),
        cases=[TestCaseSpec(id="K1"), TestCaseSpec(id="K2")]))
    muted = bundle.remove_tests(flat, {"x::K1"})
    assert "'TSKP K1 PASS'" in muted and bundle.test_names(muted, "x") == ["x::K2"]
    assert [c["id"] for c in bundle.parse_bundle(muted).plan["cases"]] == ["K2"]


async def test_expanded_delivery(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1] / "fixtures" / "cobolmod"
    ad, runner = bound(repo, tmp_path / "out")
    units = await ad.discover(runner)
    text = ad.normalize_suite(ad.extract_code(DATEUTIL_PLAN), units["cbl"], "cbl/DATEUTIL.cbl")
    path = tmp_path / "out" / "tests" / "cbl" / ad.suite_basename("DATEUTIL.cbl")
    path.parent.mkdir(parents=True)
    path.write_text(text)
    ad.deliver_extra(path, text)
    d = path.with_suffix("")
    assert d.name == "DATEUTIL_codocs_test"
    assert json.loads((d / "plan.json").read_text())["cases"][0]["id"] == "C01"
    assert (d / "TDATEU.cbl").read_text().startswith("       IDENTIFICATION DIVISION.")
    readme = (d / "README.md").read_text()
    assert "cobc -x" in readme and "-std=ibm" in readme and "cbl/DATEUTIL.cbl" in readme
