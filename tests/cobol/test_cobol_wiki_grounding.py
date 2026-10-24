"""post-generation grounding of wiki diagrams against the static index (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from grounding_fixtures import build_chain

from codocs.cobol.wiki import mermaid
from codocs.cobol.wiki.grounding import (
    Grounding,
    chain_tree,
    edge_mode,
    ground_markdown,
    parse_flow_statement,
    validate_flowchart,
    validate_sequence,
)

PAGES = {
    "customer-online": ["FRONT01", "BIZ01", "INQ01"],
    "customer-data": ["DB01", "DB02", "VS01"],
    "batch": ["POST01", "IMPORT1"],
    "shared-services": ["LOGGR"],
}
TITLES = {
    "customer-online": "Customer Menu and Inquiry",
    "customer-data": "Customer Data Access",
    "batch": "Batch Posting and Import",
    "shared-services": "Shared services",
}


@pytest.fixture
def g(tmp_path: Path) -> Grounding:
    return Grounding(build_chain(tmp_path / "repo"), PAGES, TITLES)


def test_resolve_nodes(g):
    assert g.resolve("ds_MASTER", "MASTER VSAM KSDS") == {("store", "PROD.MASTER.KSDS")}
    assert g.resolve("m", "MASTER.KSDS", "[(") == {("store", "PROD.MASTER.KSDS")}
    assert g.resolve("job_POSTJOB", "POSTJOB") == {("job", "POSTJOB")}
    assert g.resolve("t1", "FR01 to FRONT01") == {("txn", "FR01"), ("prog", "FRONT01")}
    assert g.resolve("s1", "Customer Menu, and Inquiry") == {("page", "customer-online")}
    assert g.resolve("customer_data", "Data") == {("page", "customer-data")}
    assert g.resolve("entry_cics", "CICS transactions") == {("entry", "cics")}
    assert g.resolve("sched", "JCL job schedule") == {("entry", "jcl")}
    assert g.resolve("U", "3270 terminal user") == {("entry", "user")}
    # prose words are not identifiers; an unknown box stays unknown
    assert g.resolve("mq", "MQ request messages") == set()
    # a DD alias that is also a program name: the label's kind word decides
    assert g.resolve("x", "TRANIN") == {("store", "PROD.TRANIN")}
    assert g.resolve("x", "JOB POSTJOB") == {("job", "POSTJOB")}


def test_edge_mode():
    assert edge_mode("writes") == "w" and edge_mode("INSERT row") == "w"
    assert edge_mode("reads") == "r" and edge_mode("read by") == "r"
    assert edge_mode("reads/writes") == "" and edge_mode("LINK") == ""


def test_parse_flow_statement_shapes_and_chains():
    groups, links = parse_flow_statement(
        'a["A (x)"] -->|LINK 2| b[("B")] -.->|reads| c & d -- uses --> e{"E?"}'
    )
    assert [[n.id for n in grp] for grp in groups] == [["a"], ["b"], ["c", "d"], ["e"]]
    assert [lab for _l, lab in links] == ["LINK 2", "reads", "uses"]
    assert groups[0][0].label == "A (x)" and groups[1][0].text == 'b[("B")]'
    assert parse_flow_statement("not an edge [") is None


def test_flowchart_drops_invented_store_edges(g):
    code = "\n".join([
        "flowchart TD",
        '  sched["JCL jobs"]',
        '  job_IMPJOB["IMPJOB"]',
        '  job_POSTJOB["POSTJOB"]',
        '  ds_MASTER[("MASTER VSAM KSDS")]',
        '  ds_IMPORT[("PROD.MASTER.IMPORT")]',
        "  sched -->|runs| job_IMPJOB",
        "  job_POSTJOB -->|writes| ds_MASTER",
        "  job_IMPJOB -->|writes| ds_MASTER",      # invented: IMPORT1 writes *.IMPORT
        "  job_IMPJOB -->|writes| ds_IMPORT",
        "  job_POSTJOB -->|reads| ds_MASTER",      # wrong direction: POST01 only writes it
        '  mq["MQ requests"] --> job_IMPJOB',     # unknown node: nothing to disprove
    ])
    fixed, dropped = validate_flowchart(code, g)
    assert dropped == ["job_IMPJOB -->|writes| ds_MASTER", "job_POSTJOB -->|reads| ds_MASTER"]
    assert "job_POSTJOB -->|writes| ds_MASTER" in fixed
    assert "job_IMPJOB -->|writes| ds_IMPORT" in fixed
    assert 'mq["MQ requests"] --> job_IMPJOB' in fixed
    assert mermaid.check(fixed) == []


def test_flowchart_page_edges_and_entry_points(g):
    code = "\n".join([
        "flowchart TD",
        '  entry_cics(["CICS transactions"])',
        '  subgraph online["Online"]',
        '    customer_online["Customer Menu and Inquiry"]',
        "  end",
        '  batch["Batch Posting and Import"]',
        '  data["Customer Data Access"]',
        '  cust[("CUSTOMER DB2 table")]',
        "  entry_cics -->|starts| customer_online",
        "  entry_cics -->|starts| batch",            # no transaction starts a batch program
        "  customer_online -->|LINK| data",
        "  batch -->|LINK| data",                    # no batch -> data-access edge
        "  customer_online -->|writes| cust",        # the front end only LINKs; DB01 writes
        "  data -->|writes| cust",
        "  online --> data",                         # subgraph endpoint: its members decide
        "  linkStyle 0 stroke:#f00",
    ])
    fixed, dropped = validate_flowchart(code, g)
    assert sorted(dropped) == sorted([
        "entry_cics -->|starts| batch", "batch -->|LINK| data",
        "customer_online -->|writes| cust",
    ])
    assert "online --> data" in fixed and "data -->|writes| cust" in fixed
    assert "linkStyle" not in fixed  # indexes would point at the wrong edges now
    assert mermaid.check(fixed) == []


def test_flowchart_partial_chain_keeps_node_declarations(g):
    code = 'flowchart TD\n  a["FRONT01"] --> b["DB02"] --> c["INQ01"]\n  a & c --> b'
    fixed, dropped = validate_flowchart(code, g)
    assert dropped == ["a --> b", "a --> b"]
    lines = [ln.strip() for ln in fixed.splitlines()]
    assert 'b["DB02"] --> c["INQ01"]' in lines
    assert 'a["FRONT01"]' in lines  # its only declaration was in the dropped segment
    assert "c --> b" in lines
    assert mermaid.check(fixed) == []


def test_sequence_follows_chain(g):
    code = "\n".join([
        "sequenceDiagram",
        "  participant U as Terminal user",
        "  participant T as CICS transaction FR01",
        "  participant F as FRONT01",
        "  participant D as DB01",
        "  participant C as CUSTOMER DB2 table",
        "  participant K as CUSTFILE VSAM KSDS",
        "  participant Q as INQ01",
        "  U->>T: start menu",
        "  T->>F: starts",
        "  F->>+D: LINK add request",          # skips BIZ01
        "  D->>C: INSERT customer row",
        "  F->>K: writes customer record",      # VS01 writes it, three hops down
        "  Q->>K: writes customer record",      # not on INQ01's chain at all
        "  D-->>-F: return status",
        "  activate F",
        "  deactivate F",
    ])
    fixed, notes = validate_sequence(code, g)
    assert "F->>D: LINK add request (via BIZ01)" in fixed
    assert "D->>C: INSERT customer row" in fixed
    assert "F->>K: writes customer record (via BIZ01 -> DB01 -> VS01)" in fixed
    assert "Q->>K" not in fixed
    assert "D-->>F: return status (via BIZ01)" in fixed
    # a dropped message must not leave activations unbalanced
    assert "activate" not in fixed and "+" not in fixed
    assert any(n.startswith("dropped message Q") for n in notes)
    assert mermaid.check(fixed) == []


def test_sequence_store_and_unknown_messages(g):
    code = "\n".join([
        "sequenceDiagram",
        "  participant Q as INQ01",
        "  participant D as DB02",
        "  participant C as CUSTOMER",
        "  participant X as Mystery box",
        "  Q->>D: LINK inquiry",
        "  D->>C: SELECT customer",
        "  C-->>D: row",
        "  D->>C: UPDATE customer",            # DB02 only reads CUSTOMER
        "  X->>Q: anything",
    ])
    fixed, notes = validate_sequence(code, g)
    assert "Q->>D: LINK inquiry" in fixed and "C-->>D: row" in fixed
    assert "D->>C: SELECT customer" in fixed and "X->>Q: anything" in fixed
    assert "UPDATE" not in fixed and len(notes) == 1


def test_ground_markdown_only_touches_diagrams(g):
    page = (
        "# Overview\n\nText [[prog:FRONT01]].\n\n```mermaid\nflowchart TD\n"
        '  j["JOB IMPJOB"] -->|writes| m[("MASTER.KSDS")]\n```\n\n'
        "```mermaid\npie\n  \"a\" : 1\n```\n"
    )
    out, notes = ground_markdown(page, g)
    assert notes == ['dropped edge j -->|writes| m']
    assert "Text [[prog:FRONT01]]." in out and '"a" : 1' in out
    assert 'j["JOB IMPJOB"]' in out and 'm[("MASTER.KSDS")]' in out


def test_chain_tree_skips_hubs_and_stops(g):
    paths = chain_tree(g.pg, "FRONT01", skip={"LOGGR"})
    assert paths == [["FRONT01", "BIZ01", "DB01", "VS01"]]
    assert ["FRONT01", "LOGGR"] in chain_tree(g.pg, "FRONT01")
    assert chain_tree(g.pg, "FRONT01", depth=1, skip={"LOGGR"}) == [["FRONT01", "BIZ01"]]
    assert chain_tree(g.pg, "FRONT01", skip={"LOGGR"}, stop={"BIZ01"}) == [["FRONT01", "BIZ01"]]


def test_entry_point_without_programs_is_disproved(tmp_path):
    from wiki_fixtures import build, flat_repo

    idx = build(flat_repo(tmp_path / "flat"))
    pages = {"online": ["MENU01", "VIEW01", "UPDT01"], "batch": ["ACCT01", "ACCT02"]}
    g = Grounding(idx, pages, {"online": "Online", "batch": "Batch"})
    code = ('flowchart TD\n  jobs["JCL jobs"] -->|runs| batch["Batch"]\n'
            '  cics(["CICS transactions"]) --> online["Online"]')
    assert validate_flowchart(code, g)[1] == []
    g.job_progs = {j: set() for j in g.job_progs}  # GenApp: JCL only builds / defines
    g.txn_progs = {}
    g._reach_cache.clear()
    fixed, dropped = validate_flowchart(code, g)
    assert dropped == ["jobs -->|runs| batch"]
    assert "cics" in fixed  # no transactions: CICS programs nobody calls are the entries


def test_name_that_is_both_store_and_job(g):
    # CardDemo: TCATBALF is a VSAM file and also the JCL job that defines it
    g.job_progs["TRANIN"] = set()
    g._lookup["TRANIN"].add(("job", "TRANIN"))
    assert g.resolve("T", "TRANIN") == {("store", "PROD.TRANIN"), ("job", "TRANIN")}
    code = ("sequenceDiagram\n  participant P as POST01\n  participant T as TRANIN\n"
            "  P->>T: read daily transactions\n  P->>T: rewrite records")
    fixed, notes = validate_sequence(code, g)
    assert "P->>T: read daily transactions" in fixed
    assert "rewrite" not in fixed and len(notes) == 1  # POST01 only reads TRANIN
