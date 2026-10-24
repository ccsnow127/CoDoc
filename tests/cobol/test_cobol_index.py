"""tests for codocs.cobol.index.CobolRepoIndex on a small synthetic repo (+ CardDemo if present)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codocs.cobol.index import CobolRepoIndex


def fx(*lines: str) -> str:
    return "\n".join(("      " + ln) if ln.startswith("*") else ("       " + ln)
                     for ln in lines) + "\n"


FILES = {
    "app/cbl/MAINPGM.cbl": fx(
        "* Main batch driver",
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. MAINPGM.",
        "ENVIRONMENT DIVISION.",
        "INPUT-OUTPUT SECTION.",
        "FILE-CONTROL.",
        "    SELECT ACCT-FILE ASSIGN TO ACCTFILE.",
        "DATA DIVISION.",
        "FILE SECTION.",
        "FD ACCT-FILE.",
        "01 ACCT-REC PIC X(80).",
        "WORKING-STORAGE SECTION.",
        "COPY SHARED.",
        "COPY NOTHERE.",
        "COPY DFHAID.",
        "PROCEDURE DIVISION.",
        "MAIN-PARA.",
        "    OPEN INPUT ACCT-FILE",
        "    CALL 'SUBPGM' USING ACCT-REC",
        "    CALL 'MISSING1'",
        "    CALL 'CEE3ABD'",
        "    STOP RUN.",
    ),
    "app/cbl/SUBPGM.cbl": fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. SUBPGM.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "COPY SHARED.",
        "LINKAGE SECTION.",
        "01 LK-REC PIC X(80).",
        "PROCEDURE DIVISION USING LK-REC.",
        "    GOBACK.",
    ),
    "app/cbl/ONLINE1.cbl": fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. ONLINE1.",
        "PROCEDURE DIVISION.",
        "    EXEC CICS READ FILE('ACCTDAT') INTO(X) RIDFLD(K) END-EXEC",
        "    EXEC SQL SELECT A INTO :B FROM CUSTOMER END-EXEC",
        "    EXEC CICS SEND MAP('MENUA') MAPSET('MENU01') END-EXEC",
        "    EXEC CICS XCTL PROGRAM('ONLINE2') END-EXEC",
        "    EXEC CICS RETURN TRANSID('ON01') END-EXEC.",
    ),
    "app/cbl/ONLINE2.cbl": fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. ONLINE2.",
        "PROCEDURE DIVISION.",
        "    EXEC CICS START TRANSID('ON01') END-EXEC",
        "    EXEC CICS WRITEQ TD QUEUE('CSMT') FROM(X) END-EXEC",
        "    EXEC CICS RETURN TRANSID('ON02') END-EXEC.",
    ),
    "app/cpy/SHARED.cpy": fx("01 SHARED-AREA.", "   05 SHARED-FLAG PIC X."),
    "other/SHARED.cpy": fx("01 OTHER-SHARED.", "   05 OTHER-FLAG PIC X."),
    # no extension: found by content sniffing
    "other/OTHERPGM": fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. OTHERPGM.",
        "DATA DIVISION.",
        "WORKING-STORAGE SECTION.",
        "COPY SHARED.",
        "PROCEDURE DIVISION.",
        "    DISPLAY 'HI'",
        "    STOP RUN.",
    ),
    "app/jcl/RUNMAIN.jcl": (
        "//RUNMAIN JOB CLASS=A\n"
        "//* run the main batch\n"
        "//S1 EXEC PGM=MAINPGM\n"
        "//ACCTFILE DD DSN=PROD.ACCT.DATA,DISP=SHR\n"
        "//SYSOUT DD SYSOUT=*\n"
    ),
    "app/csd/APP.csd": (
        " DEFINE TRANSACTION(ON01) GROUP(APP)\n"
        "        PROGRAM(ONLINE1)\n"
        " DEFINE FILE(ACCTDAT) GROUP(APP)\n"
        "        DSNAME(PROD.ACCT.DATA)\n"
    ),
    "app/bms/MENU01.bms": (
        "MENU01  DFHMSD TYPE=&&SYSPARM\n"
        "MENUA   DFHMDI SIZE=(24,80)\n"
        "        DFHMDF LENGTH=8\n"
        "        DFHMSD TYPE=FINAL\n"
    ),
    "docs/README.md": "# readme\n       PROGRAM-ID. NOTAPROGRAM.\n",
    ".git/HEAD": "ref: refs/heads/main\n",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        p = tmp_path / "demo" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path / "demo"


def test_discovery_and_parsing(repo: Path):
    idx = CobolRepoIndex.build(repo)
    assert idx.name == "demo"
    assert set(idx.programs) == {"MAINPGM", "SUBPGM", "ONLINE1", "ONLINE2", "OTHERPGM"}
    kinds = {f.path: f.kind for f in idx.files}
    assert kinds["other/OTHERPGM"] == "program"
    assert kinds["app/jcl/RUNMAIN.jcl"] == "jcl"
    assert kinds["app/bms/MENU01.bms"] == "bms"
    assert kinds["app/csd/APP.csd"] == "other"
    assert "docs/README.md" not in kinds and ".git/HEAD" not in kinds
    assert set(idx.copybooks) == {"SHARED"}
    assert sorted(idx.copybooks["SHARED"].used_by) == ["MAINPGM", "OTHERPGM", "SUBPGM"]
    assert set(idx.jobs) == {"RUNMAIN"}
    assert idx.mapsets["MENU01"].maps == ["MENUA"]


def test_copybook_resolution_prefers_nearest_dir(repo: Path):
    idx = CobolRepoIndex.build(repo)
    assert "OTHER-SHARED" in idx.copybook_source("SHARED", "OTHERPGM")
    assert "SHARED-AREA" in idx.copybook_source("SHARED", "MAINPGM")
    main_items = {d.name: d.copybook for d in idx.programs["MAINPGM"].data_items}
    assert main_items["SHARED-AREA"] == "SHARED"
    other_items = {d.name for d in idx.programs["OTHERPGM"].data_items}
    assert "OTHER-SHARED" in other_items
    dirs = idx.copybook_dirs()
    assert all(d.is_absolute() for d in dirs)
    assert {d.name for d in dirs} == {"cpy", "other"}


def test_graph_nodes_and_edges(repo: Path):
    idx = CobolRepoIndex.build(repo)
    g = idx.graph
    kinds = {n: d["kind"] for n, d in g.nodes(data=True)}
    assert kinds["prog:MAINPGM"] == "program"
    assert kinds["copy:SHARED"] == "copybook"
    assert kinds["job:RUNMAIN"] == "job"
    assert kinds["data:PROD.ACCT.DATA"] == "datastore"
    assert kinds["data:CUSTOMER"] == "datastore"
    assert kinds["txn:ON01"] == "transaction"
    assert kinds["map:MENU01"] == "map"
    e = {(u, v): d["kind"] for u, v, d in g.edges(data=True)}
    assert e[("prog:MAINPGM", "prog:SUBPGM")] == "call"
    assert e[("prog:ONLINE1", "prog:ONLINE2")] == "cics-xctl"
    assert e[("prog:ONLINE2", "prog:ONLINE1")] == "cics-start"
    assert e[("prog:MAINPGM", "copy:SHARED")] == "copy"
    assert e[("job:RUNMAIN", "prog:MAINPGM")] == "runs"
    assert e[("prog:MAINPGM", "data:PROD.ACCT.DATA")] == "reads"
    assert e[("prog:ONLINE1", "data:PROD.ACCT.DATA")] == "reads"  # CICS file -> CSD DSNAME
    assert e[("prog:ONLINE1", "data:CUSTOMER")] == "reads"
    assert e[("prog:ONLINE2", "data:CSMT")] == "writes"
    assert e[("prog:ONLINE1", "map:MENU01")] == "sends-map"
    assert e[("txn:ON01", "prog:ONLINE1")] == "starts"
    assert "prog:MISSING1" not in g


def test_queries(repo: Path):
    idx = CobolRepoIndex.build(repo)
    assert idx.callees("MAINPGM") == ["SUBPGM"]
    assert idx.callees("ONLINE1", kinds=["call"]) == []
    assert idx.callers("SUBPGM") == ["MAINPGM"]
    assert idx.transitive_callees("ONLINE2") == ["ONLINE1"]
    assert idx.jobs_running("MAINPGM") == ["RUNMAIN"]
    assert idx.transactions_of("ONLINE1") == ["ON01"]  # CSD
    assert idx.transactions_of("ONLINE2") == ["ON02"]  # RETURN TRANSID heuristic
    eps = idx.entrypoints()
    assert eps == {"jcl": ["MAINPGM"], "cics": ["ONLINE1", "ONLINE2"], "standalone": ["OTHERPGM"]}
    ranked = [p for p, _ in idx.rank_programs()]
    assert ranked.index("SUBPGM") < ranked.index("MAINPGM")
    pg = idx.program_graph()
    assert set(pg.nodes) == set(idx.programs)
    assert pg.edges["MAINPGM", "SUBPGM"]["kind"] == "call"
    assert idx.group_of("MAINPGM") == "(root)"
    assert idx.group_of("OTHERPGM") == "other"
    assert "PROGRAM-ID. SUBPGM." in idx.program_source("SUBPGM")
    ds = idx.datastores["PROD.ACCT.DATA"]
    assert ds.jobs == ["RUNMAIN"]
    assert sorted(ds.readers) == ["MAINPGM", "ONLINE1"]
    assert set(ds.aliases) >= {"ACCTFILE", "ACCTDAT"}
    call = next(c for c in idx.programs["MAINPGM"].calls if c.target == "SUBPGM")
    assert call.resolved
    start = next(c for c in idx.programs["ONLINE2"].calls if c.kind == "cics-start")
    assert start.resolved


def test_summary_and_dump(repo: Path):
    idx = CobolRepoIndex.build(repo)
    s = idx.summary()
    assert s.programs == 5 and s.copybooks == 1 and s.jcl_jobs == 1 and s.bms_mapsets == 1
    assert s.call_edges == 3
    assert s.unresolved_calls == ["MISSING1"]  # CEE3ABD is a system routine
    assert s.missing_copybooks == ["NOTHERE"]  # DFHAID is IBM-supplied
    assert s.cobol_loc > 0
    d = idx.to_dict()
    json.dumps(d)
    assert d["summary"]["programs"] == 5
    assert any(e["source"] == "prog:MAINPGM" and e["target"] == "prog:SUBPGM" and
               e["kind"] == "call" for e in d["edges"])


def test_subtree_and_exclude(repo: Path):
    idx = CobolRepoIndex.build(repo, subtree="other")
    assert set(idx.programs) == {"OTHERPGM"}
    assert idx.programs["OTHERPGM"].path == "other/OTHERPGM"
    idx = CobolRepoIndex.build(repo, exclude=["other/*", "app/jcl"])
    assert "OTHERPGM" not in idx.programs and not idx.jobs


def test_duplicate_program_ids_prefer_original(repo: Path):
    dup = repo / "app" / "EXPANDED_SOURCE" / "EXPANDED_SUBPGM.cbl"
    dup.parent.mkdir(parents=True)
    dup.write_text(FILES["app/cbl/SUBPGM.cbl"])
    idx = CobolRepoIndex.build(repo)
    assert idx.programs["SUBPGM"].path == "app/cbl/SUBPGM.cbl"
    assert any("duplicate PROGRAM-ID SUBPGM" in w for w in idx.summary().warnings)


def test_carddemo_integration(datasets):
    carddemo = datasets("carddemo")
    if carddemo is None or not (carddemo / "app" / "cbl").is_dir():
        pytest.skip("CardDemo clone not present")
    import time

    t0 = time.time()
    idx = CobolRepoIndex.build(carddemo)
    assert time.time() - t0 < 15
    s = idx.summary()
    assert s.programs >= 25
    assert s.copybooks >= 20
    assert s.jcl_jobs >= 20
    assert s.bms_mapsets >= 15
    kinds = [d["kind"] for _, _, d in idx.graph.edges(data=True)]
    assert kinds.count("cics-xctl") >= 20
    assert "COMEN01C" in idx.callees("COSGN00C")
    assert "COACTVWC" in idx.callees("COMEN01C")  # menu table via REDEFINES / OCCURS
    assert idx.jobs_running("CBTRN02C") == ["POSTTRAN"]
    assert "CBTRN03C" in idx.entrypoints()["jcl"]
    assert idx.transactions_of("COSGN00C") == ["CC00"]
    assert len(idx.entrypoints()["cics"]) >= 15
    acct = idx.datastores.get("AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS")
    assert acct is not None and acct.readers and acct.writers
    json.dumps(idx.to_dict())


def test_calls_to_entry_points_resolve_to_owner(tmp_path: Path):
    (tmp_path / "logger.cob").write_text(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. logger.\nPROCEDURE DIVISION.\n"
        "    GOBACK.\n    ENTRY \"enable-logger\".\n    GOBACK.\n"
    )
    (tmp_path / "main.cob").write_text(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. main.\nPROCEDURE DIVISION.\n"
        "    CALL \"enable-logger\"\n    STOP RUN.\n"
    )
    idx = CobolRepoIndex.build(tmp_path)
    assert idx.program_of("ENABLE-LOGGER") == "LOGGER"
    assert idx.callees("MAIN") == ["LOGGER"]
    assert idx.programs["MAIN"].calls[0].resolved
    assert idx.summary().unresolved_calls == []
