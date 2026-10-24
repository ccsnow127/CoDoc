"""CobolRunner on cobolmod: baseline, overlays, run selection,
compile-only, statement and paragraph coverage, trace formats, the SIGXFSZ fallback, crash
and timeout verdicts, build errors mapped to bundle lines, copybook case aliases, screen
libraries, and the repository left untouched; plus a cbi scenario when the dataset exists."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from runner_fixtures import (
    DATEUTIL_PLAN,
    DAYSINM_PLAN,
    LTRIM_PLAN,
    RIGHT_PLAN,
    bound,
    cbi_repo,
    fx,
    plan,
)

from codocs.cobol.harness import parse_trace
from codocs.golang.gotool import file_coverage

pytestmark = pytest.mark.needs_cobc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COBOLMOD = FIXTURES / "cobolmod"
TDATE = "test/TDATEUTL.cbl"
NAMES = [f"{TDATE}::{i}" for i in ("T01", "T02", "T03", "T99")]


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def mod(tmp_path: Path) -> Path:
    repo = tmp_path / "cobolmod"
    shutil.copytree(COBOLMOD, repo)
    return repo


async def _ready(repo: Path, out: Path, **kw):
    ad, runner = bound(repo, out, **kw)
    units = await ad.discover(runner)
    return ad, runner, units


async def test_baseline_overlays_and_selection(mod: Path, tmp_path: Path):
    before = _digest(mod)
    ad, runner, units = await _ready(mod, tmp_path / "out")
    assert runner.available() and runner.cobc_ok() and runner.version().startswith("GnuCOBOL")
    res = await runner.test("cbl")
    assert res.build_ok and res.tests == {n: ("fail" if n.endswith("T99") else "pass")
                                          for n in NAMES}
    assert "TEST T99 FAIL expected 99 got 00" in res.output[NAMES[3]]
    assert res.returncode == 0 and not res.timed_out
    # overlay: a regenerated DATEUTIL that accepts every date
    src = ad.read("cbl/DATEUTIL.cbl")
    broken = src.replace("           PERFORM 1000-CHECK-MONTH\n", "")
    res = await runner.test("cbl", overlay={"cbl/DATEUTIL.cbl": broken}, run=NAMES[:2])
    assert res.tests == {NAMES[0]: "pass", NAMES[1]: "fail"}
    # overlay that does not compile: build_ok False, errors mapped to the repo rel
    res = await runner.test("cbl", overlay={"cbl/DATEUTIL.cbl": src.replace(
        "MOVE '00' TO LK-RC", "MOVE '00' TO TO LK-RC")}, run=NAMES)
    assert not res.build_ok
    assert any(e.startswith("cbl/DATEUTIL.cbl:22: error:") for e in res.build_errors)
    # hide the test program, add a bundle
    suite = ad.normalize_suite(ad.extract_code(DATEUTIL_PLAN), units["cbl"], "cbl/DATEUTIL.cbl")
    srel = ad.suite_rel(units["cbl"], "DATEUTIL.cbl")
    res = await runner.test("cbl", overlay={TDATE: None, srel: suite})
    assert res.build_ok and set(res.tests) == {f"{srel}::C0{i}" for i in range(1, 5)}
    assert set(res.tests.values()) == {"pass"}
    assert (await runner.test("cbl", run=[])).tests == {}
    assert (await runner.test("cbl", run=["nope::X"])).tests == {}
    assert _digest(mod) == before                         # nothing written into the repo
    assert not list(mod.rglob("*.o")) and (tmp_path / "out" / "work" / "cobol" / "src").is_dir()
    assert not list((tmp_path / "out" / "work" / "tmp").glob("b*"))   # call dirs removed


async def test_compile_only(mod: Path, tmp_path: Path):
    ad, runner, units = await _ready(mod, tmp_path / "out")
    src = ad.read("cbl/DATEUTIL.cbl")
    ok = await runner.test("cbl", overlay={"cbl/DATEUTIL.cbl": src}, compile_only=True)
    assert ok.build_ok and ok.tests == {}
    bad = await runner.test("cbl", overlay={"cbl/DATEUTIL.cbl": src.replace(
        "MOVE '00' TO LK-RC", "MOVE '00' TO TO LK-RC")}, compile_only=True)
    assert not bad.build_ok and bad.build_errors[0].startswith("cbl/DATEUTIL.cbl:")
    inq = ad.read("cbl/ACCTINQ.cbl")           # EXEC CICS: neutralised first
    res = await runner.test("cbl", overlay={"cbl/ACCTINQ.cbl": inq}, compile_only=True)
    assert res.build_ok, res.build_errors


async def test_statement_and_paragraph_coverage(mod: Path, tmp_path: Path):
    ad, runner, units = await _ready(mod, tmp_path / "out")
    res = await runner.test("cbl", cover=True)
    assert res.coverage is not None and not res.coverage_error
    cov, gap = file_coverage(res.coverage, "cbl/DATEUTIL.cbl")
    assert 0.6 < cov < 1.0 and 60 in gap               # 9999-EXIT is never reached
    assert 32 in gap and 34 not in gap                 # month < 1 never tested; > 12 is
    days, _ = file_coverage(res.coverage, "cbl/DAYSINM.cbl")
    assert days > 0                                    # FUNCTION-ID records are counted
    rpt, _ = file_coverage(res.coverage, "cbl/ACCTRPT.cbl")
    assert rpt == 0.0                                  # not linked: count-0 blocks
    assert {b.file for b in res.coverage} == {"cbl/ACCTINQ.cbl", "cbl/ACCTRPT.cbl",
                                              "cbl/DATEUTIL.cbl", "cbl/DAYSINM.cbl"}
    _, para_runner, _ = await _ready(mod, tmp_path / "out2", cobol_coverage="paragraph")
    res = await para_runner.test("cbl", cover=True)
    blocks = [b for b in res.coverage if b.file == "cbl/DATEUTIL.cbl"]
    assert [b.start_line for b in blocks] == [21, 29, 38, 44]      # 9999-EXIT is trivial
    assert [b.count for b in blocks] == [1, 1, 1, 1]
    # a run that writes no trace has no coverage
    suite = ad.normalize_suite(ad.extract_code(plan(fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. TNONE.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "01 TD-X PIC 9 VALUE 1.", "PROCEDURE DIVISION.",
        "MAIN-PARA.", "    PERFORM TC-N1", "    STOP RUN.", "TC-N1.",
        "    IF TD-X = 1 DISPLAY 'TEST N1 PASS' ELSE DISPLAY 'TEST N1 FAIL' END-IF."),
        cases=["N1"])), units["cbl"], "cbl/DATEUTIL.cbl")
    srel = ad.suite_rel(units["cbl"], "DATEUTIL.cbl")
    res = await runner.test("cbl", overlay={TDATE: None, srel: suite}, cover=True)
    assert set(res.tests) == {f"{srel}::N1"}
    if res.coverage is None:               # the SUT never ran: no trace written
        assert res.coverage_error == "no GnuCOBOL trace was written"
    else:
        assert file_coverage(res.coverage, "cbl/DATEUTIL.cbl")[0] == 0.0


def test_trace_formats(tmp_path: Path):
    files = {"DATEUTIL": "cbl/DATEUTIL.cbl", "DAYS-IN-MONTH": "cbl/DAYSINM.cbl"}
    paths = {"/m/cbl/DATEUTIL.cbl": "cbl/DATEUTIL.cbl", "/m/cbl/DAYSINM.cbl": "cbl/DAYSINM.cbl"}
    pinned = tmp_path / "pinned.txt"
    pinned.write_text("\n".join([
        "Source: '/m/cbl/DATEUTIL.cbl'", "Program-Id:  DATEUTIL",
        "DATEUTIL|    21|    Entry: DATEUTIL", "DATEUTIL|    21|Paragraph: 0000-MAIN",
        "DATEUTIL|    22|           MOVE", "DATEUTIL|    22|           MOVE",
        "Source: '/m/cbl/DAYSINM.cbl'", "Function-Id: DAYS-IN-MONTH",
        "DAYS-IN-MONTH|    14|           EVALUATE", "DAYS-IN-MONTH|    25|     Exit: X",
        "Source: '/m/drv/T.cbl'", "TDRV|     5|           DISPLAY"]) + "\n")
    lines, paras = parse_trace(pinned, paths.get, files)
    assert lines == {"cbl/DATEUTIL.cbl": {22: 2}, "cbl/DAYSINM.cbl": {14: 1}}
    assert paras == {"cbl/DATEUTIL.cbl": {"0000-MAIN"}}
    default = tmp_path / "default.txt"
    default.write_text("\n".join([
        "Source: '/m/cbl/DAYSINM.cbl'", "Function-Id: DAYS-IN-MONTH",
        "Function-Id: DAYS-IN-MONTH       Paragraph: 1000-FEB                     Line:     26",
        "Function-Id: DAYS-IN-MONTH                  IF                           Line:     27",
        "Program-Id:  DATEUTIL                       MOVE                         Line:      9"]))
    lines, paras = parse_trace(default, paths.get, files)
    assert lines == {"cbl/DAYSINM.cbl": {27: 1}} and paras == {"cbl/DAYSINM.cbl": {"1000-FEB"}}
    assert parse_trace(tmp_path / "missing.txt", paths.get, files) == ({}, {})


async def test_trace_size_limit_falls_back_to_paragraphs(mod: Path, tmp_path: Path):
    ad, runner, units = await _ready(mod, tmp_path / "out")
    runner.harness.trace_fsize_limit = 5000     # paragraph trace ~3.3 kB, statement ~6.7 kB
    res = await runner.test("cbl", cover=True)
    assert res.build_ok and res.tests[NAMES[0]] == "pass"
    assert res.coverage_error.startswith("statement trace exceeded")
    blocks = [b for b in res.coverage if b.file == "cbl/DATEUTIL.cbl"]
    assert [b.start_line for b in blocks] == [21, 29, 38, 44]


SEGV_DRIVER = fx(
    "IDENTIFICATION DIVISION.", "PROGRAM-ID. TSEGV.", "DATA DIVISION.",
    "WORKING-STORAGE SECTION.", "COPY DATEPARM.", "LINKAGE SECTION.", "01 TD-L PIC X(100).",
    "PROCEDURE DIVISION.", "MAIN-PARA.", "    PERFORM TC-S1", "    PERFORM TC-S2",
    "    PERFORM TC-S3", "    STOP RUN.",
    "TC-S1.", "    MOVE '20240101' TO LK-DATE", "    CALL 'DATEUTIL' USING LK-DATE-PARMS",
    "    IF LK-RC = '00' DISPLAY 'TEST S1 PASS' ELSE DISPLAY 'TEST S1 FAIL' END-IF.",
    "TC-S2.", "    MOVE ALL 'X' TO TD-L",
    "    IF TD-L = SPACES DISPLAY 'TEST S2 PASS' ELSE DISPLAY 'TEST S2 FAIL' END-IF.",
    "TC-S3.",
    "    IF LK-RC = '00' DISPLAY 'TEST S3 PASS' ELSE DISPLAY 'TEST S3 FAIL' END-IF.")

LOOP_DRIVER = fx(
    "IDENTIFICATION DIVISION.", "PROGRAM-ID. TLOOP.", "DATA DIVISION.",
    "WORKING-STORAGE SECTION.", "COPY DATEPARM.", "PROCEDURE DIVISION.", "MAIN-PARA.",
    "    PERFORM TC-W1", "    PERFORM TC-W2", "    STOP RUN.",
    "TC-W1.", "    MOVE '20240101' TO LK-DATE", "    CALL 'DATEUTIL' USING LK-DATE-PARMS",
    "    IF LK-RC = '00' DISPLAY 'TEST W1 PASS' ELSE DISPLAY 'TEST W1 FAIL' END-IF.",
    "TC-W2.", "    PERFORM UNTIL 1 = 2", "        CONTINUE", "    END-PERFORM",
    "    DISPLAY 'TEST W2 PASS'.")


async def test_crash_timeout_and_bundle_build_errors(mod: Path, tmp_path: Path):
    ad, runner, units = await _ready(mod, tmp_path / "out")
    u = units["cbl"]
    srel = ad.suite_rel(u, "DATEUTIL.cbl")
    seg = ad.normalize_suite(ad.extract_code(plan(SEGV_DRIVER, cases=["S1", "S2", "S3"])),
                             u, "cbl/DATEUTIL.cbl")
    res = await runner.test("cbl", overlay={TDATE: None, srel: seg})
    assert res.build_ok
    assert res.tests == {f"{srel}::S1": "pass", f"{srel}::S2": "fail",
                         f"{srel}::S3": "interrupted"}
    assert "attempt to reference invalid memory address" in res.output[f"{srel}::S2"]
    assert res.returncode == 139
    msg = ad.crash_message(res)
    assert msg.startswith("did not run: the driver ended early (attempt to reference invalid")
    loop = ad.normalize_suite(ad.extract_code(plan(LOOP_DRIVER, cases=["W1", "W2"])), u,
                              "cbl/DATEUTIL.cbl")
    res = await runner.test("cbl", overlay={TDATE: None, srel: loop}, timeout=2)
    assert res.timed_out and res.returncode == -1
    assert res.tests == {f"{srel}::W1": "pass", f"{srel}::W2": "fail"}
    assert "timed out" in res.output[f"{srel}::W2"]
    # a driver that does not compile: errors point at the bundle lines
    bad = ad.normalize_suite(ad.extract_code(DATEUTIL_PLAN).replace(
        "CANCEL 'DATEUTIL'.", "CANCEL CANCEL.", 1), u, "cbl/DATEUTIL.cbl")
    res = await runner.test("cbl", overlay={TDATE: None, srel: bad})
    assert not res.build_ok
    rows = bad.split("\n")
    err = res.build_errors[0]
    assert err.startswith(f"{srel}:")
    n = int(err.split(":")[1])
    assert "CANCEL CANCEL" in rows[n - 1]
    junk = await runner.test("cbl", overlay={TDATE: None, srel: ad.normalize_suite(
        "no json", u, "cbl/DATEUTIL.cbl")})
    assert junk.build_errors == [f"{srel}:1: the reply is not a JSON test plan: response is "
                                 "not a JSON object"]


async def test_function_unit_bundle_and_mutant_runs(mod: Path, tmp_path: Path):
    ad, runner, units = await _ready(mod, tmp_path / "out")
    u = units["cbl"]
    srel = ad.suite_rel(u, "DAYSINM.cbl")
    suite = ad.normalize_suite(ad.extract_code(DAYSINM_PLAN), u, "cbl/DAYSINM.cbl")
    res = await runner.test("cbl", overlay={TDATE: None, srel: suite}, cover=True)
    assert set(res.tests.values()) == {"pass"} and len(res.tests) == 3
    cov, gap = file_coverage(res.coverage, "cbl/DAYSINM.cbl")
    assert cov > 0.8
    src = ad.read("cbl/DAYSINM.cbl")
    ms = ad.mutation_sites(src)
    assert ms and runner.mutants_supported("cbl")
    from codocs.golang.mutate import apply

    leap = next(m for m in ms if m.operator == "move" and m.original.startswith("29"))
    res = await runner.test("cbl", overlay={TDATE: None, srel: suite,
                                            "cbl/DAYSINM.cbl": apply(src, leap)},
                            run=list(res.tests), failfast=True)
    assert res.tests[f"{srel}::D01"] == "fail"
    with pytest.raises(ValueError):
        await runner.test("cbl", cover=True, failfast=True)


async def test_copybook_case_aliases(tmp_path: Path):
    repo = tmp_path / "genapp"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lgcmarea.cpy").write_text(fx("01 CA-AREA.", "   05 CA-ID PIC 9(4)."))
    (repo / "src" / "lgprog.cbl").write_text(fx(
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. LGPROG.", "DATA DIVISION.",
        "WORKING-STORAGE SECTION.", "COPY LGCMAREA.", "LINKAGE SECTION.",
        "    EXEC SQL", "        INCLUDE LGCMAREA", "    END-EXEC.",
        "PROCEDURE DIVISION.", "    GOBACK."))
    ad, runner, _ = await _ready(repo, tmp_path / "out")
    runner.harness.mirror.ensure()
    src = tmp_path / "out" / "work" / "cobol" / "src"
    names = json.loads((src / ".names.json").read_text())
    assert names["src"] == ["LGCMAREA.cpy", "lgcmarea.cpy", "lgprog.cbl"]

    def lookup(member: str, d: str) -> bool:      # cobc on Linux: exact names only
        return any(f"{member}{ext}" in names[d] for ext in ("", ".cpy", ".CPY", ".cbl"))

    for member in ("LGCMAREA",):
        assert lookup(member, "src")
    ok, errors, _ = runner.harness.compile_check("src/lgprog.cbl", neutralize=True)
    assert ok, errors


async def test_screen_library_from_cobc_info(mod: Path, tmp_path: Path):
    ad, runner, _ = await _ready(mod, tmp_path / "out")
    h = runner.harness
    m = re.search(r"extended screen I/O\s*:\s*(\S+)", h.cobc_info())
    lib = m.group(1).lower() if m else ""
    if lib in ("", "disabled", "no", "none"):
        pytest.skip("cobc reports no screen library")
    assert h.screen_library() == lib
    prog = h.index.programs["DATEUTIL"]
    prog.uses_screen_io = True
    from codocs.cobol.harness import _Build

    b = _Build(tmp_path)
    assert h._link_libs(["DATEUTIL"], b) == [f"-l{lib}"]
    prog.uses_screen_io = False
    assert h._link_libs(["DATEUTIL"], b) == []


async def test_cbi_programs(datasets, tmp_path: Path):
    src = datasets("cbi")
    if src is None:
        pytest.skip("cbi dataset not present")
    repo = cbi_repo(src / "source", tmp_path / "cbi")
    before = _digest(repo)
    ad, runner, units = await _ready(repo, tmp_path / "out")
    u = units["functions"]
    assert u.extra["modes"] == {"functions/left_func.cbl": "unit",
                                "functions/ltrim.cbl": "unit",
                                "functions/right_func.cbl": "unit"}
    base = await runner.test("functions")
    assert base.build_ok and base.tests == {}
    for rel, reply, n in (("functions/ltrim.cbl", LTRIM_PLAN, 2),
                          ("functions/right_func.cbl", RIGHT_PLAN, 1)):
        srel = ad.suite_rel(u, Path(rel).name)
        suite = ad.normalize_suite(ad.extract_code(reply), u, rel)
        res = await runner.test("functions", overlay={srel: suite}, cover=True)
        assert res.build_ok and list(res.tests.values()) == ["pass"] * n, res.output
        cov, _ = file_coverage(res.coverage, rel)
        assert cov > 0.5
        regen = ad.finalize_code(rel, ad.read(rel))
        again = await runner.test("functions", overlay={srel: suite, rel: regen},
                                  run=list(res.tests))
        assert again.tests == res.tests
    assert _digest(repo) == before
