import json
from pathlib import Path

import pytest

from codocs.bizlogic.cluster import build_hierarchy
from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.export import load_wiki
from codocs.bizlogic.graph import build_graph
from codocs.bizlogic.profile import load_profile
from codocs.bizlogic.run import BizParams, run_bizlogic
from codocs.bizlogic.walk import deduplicate, find_roots, sample_walks, signature
from codocs.config import Settings

LOGGER = "package logger\n\nfunc Start(n string) {}\n\nfunc End(n string) {}\n"


def _module(root: Path) -> Path:
    """app/main -> registration of handlers (refs) -> reg/* and dereg/* flows, plus a logger."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    files = {
        "logger/logger.go": LOGGER,
        "app/main.go": (
            'package main\n\nimport (\n\t"example.com/m/dereg"\n\t"example.com/m/reg"\n)\n\n'
            "var handlers = map[string]func(){\n\t\"reg\": reg.Handle,\n\t\"dereg\": dereg.Handle,\n}\n\n"
            "func main() {\n\tfor _, h := range handlers {\n\t\th()\n\t}\n}\n"),
        "reg/reg.go": (
            'package reg\n\nimport (\n\t"example.com/m/logger"\n\t"example.com/m/store"\n)\n\n'
            'func Handle() {\n\tlogger.Start("x")\n\tauth()\n\tsave()\n\tlogger.End("x")\n}\n\n'
            'func auth() {\n\tlogger.Start("a")\n\tcheckKey()\n}\n\n'
            'func checkKey() { logger.Start("k") }\n\n'
            'func save() {\n\tvar s store.Store\n\ts.Put("k")\n\tstore.Commit()\n}\n'),
        "dereg/dereg.go": (
            'package dereg\n\nimport (\n\t"example.com/m/logger"\n\t"example.com/m/store"\n)\n\n'
            'func Handle() {\n\tlogger.Start("d")\n\trelease()\n}\n\n'
            'func release() {\n\tstore.Remove("k")\n\tnotify()\n}\n\n'
            'func notify() { logger.End("n") }\n'),
        "store/store.go": (
            "package store\n\ntype Store struct{ keys []string }\n\n"
            "func (s *Store) Put(k string) { s.keys = append(s.keys, k) }\n\n"
            "var Commit = commitImpl\n\nfunc commitImpl() {}\n\n"
            "func Remove(k string) { commitImpl() }\n"),
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    docs = root / "reg" / "markdown_docs"
    docs.mkdir()
    (docs / "reg.md").write_text(
        "## FunctionDef Handle\n**Handle**: Handles a user registration request.\n\n"
        "## FunctionDef auth\n**auth**: Authenticates the user.\n")
    sdocs = root / "store" / "markdown_docs"
    sdocs.mkdir()
    (sdocs / "store.md").write_text(
        "## ClassDef Store\n**Store**: Key store.\n\n"
        "## FunctionDef Store.Put\n**Store.Put**: Stores a key.\n")
    return root


def test_graph_edges_aliases_methods_and_utilities(tmp_path):
    root = _module(tmp_path)
    docs = DocIndex.build([root])
    fg = build_graph(root, docs=docs, utility_fanin=4)
    g = fg.graph
    assert g["app::main"]["reg::Handle"]["kind"] == "table"      # handler table entry
    assert g["app::main"]["reg::Handle"]["weight"] == 0.5         # 1 / table size
    assert g["reg::Handle"]["reg::auth"]["kind"] == "call"
    assert g.has_edge("reg::save", "store::Store.Put")            # method call x.Put(
    assert g.has_edge("reg::save", "store::commitImpl")           # var Commit = commitImpl
    assert "logger::Start" in fg.utilities                         # fan-in 4
    assert fg.nodes["reg::Handle"].summary == "Handles a user registration request."
    assert fg.nodes["store::Store.Put"].summary == "Stores a key."
    assert fg.stats["functions"] + fg.stats["methods"] == len(fg.nodes)


def test_roots_walks_and_dedup(tmp_path):
    root = _module(tmp_path)
    fg = build_graph(root, utility_fanin=4)
    roots = dict(find_roots(fg, min_reach=1))
    assert "app::main" not in roots              # it only registers the handlers (table)
    assert "reg::Handle" in roots and "dereg::Handle" in roots     # only referenced, not called
    assert "reg::auth" not in roots
    walks = sample_walks(fg, list(roots), walks_per_root=10, max_len=6, seed=1)
    assert all(n not in fg.utilities for w in walks for n in w)
    assert all(len(set(w)) == len(w) for w in walks)
    h = build_hierarchy(fg, seed=0)
    counts = []
    for k in range(1, h.max_k + 1):
        trajs, stats = deduplicate(fg, walks, h.assign(k))
        counts.append(len(trajs))
        assert {t.id for t in trajs} == {f"T{i:03d}" for i in range(1, len(trajs) + 1)}
        assert sum(t.support for t in trajs) == stats["walks"] - stats["dropped_by_cap"] * 0
    assert counts[0] == 1                       # one cluster: every walk has the same signature
    assert counts == sorted(counts)             # more clusters never fewer trajectories here


def test_signature_collapses_repeats_and_prefix_subsume(tmp_path):
    root = _module(tmp_path)
    fg = build_graph(root, utility_fanin=4)
    assign = {n: (0 if n.startswith(("app", "reg")) else 1) for n in fg.nodes}
    assert signature(["app::main", "reg::Handle", "store::commitImpl"], assign) == (0, 1)
    assign["store::Store.Put"] = 2
    walks = [["app::main", "reg::Handle", "store::commitImpl"],
             ["app::main", "reg::Handle", "store::commitImpl", "store::Store.Put"]]
    kept, stats = deduplicate(fg, walks, assign, prefix_subsume=True)
    assert [t.clusters for t in kept] == [[0, 1, 2]] and kept[0].subsumed == 1
    t = kept[0]
    assert (t.support, t.own_support, t.step_walks) == (2, 1, [2, 2, 1])
    assert t.path == walks[1]                     # the representative follows the sequence
    kept, _ = deduplicate(fg, walks, assign, prefix_subsume=False)
    assert len(kept) == 2


def test_hierarchy_is_nested_and_balanced_by_size(tmp_path):
    root = _module(tmp_path)
    fg = build_graph(root, utility_fanin=4)
    h = build_hierarchy(fg, seed=0)
    assert h.max_k >= 2
    fine, coarse = h.assign(h.max_k), h.assign(2)
    parent: dict[int, int] = {}
    for n, c in fine.items():
        assert parent.setdefault(c, coarse[n]) == coarse[n]
    assert set(h.parents(h.max_k, 2).values()) <= set(coarse.values())


def test_load_wiki_reads_groups(tmp_path):
    w = tmp_path / "wiki"
    w.mkdir()
    (w / "index.md").write_text("# Overview\nhello\n")
    (w / "a.md").write_text("# Page A\n")
    (w / "_sidebar.md").write_text("# Proj\n\n- [Overview](index.md)\n- **Group**\n  - [Page A](a.md)\n")
    wiki = load_wiki(w)
    assert wiki.project_name == "Proj"
    assert [s.title for s in wiki.sidebar] == ["Overview", "Group"]
    assert wiki.sidebar[1].children[0].page_id == "a"
    assert {p.id for p in wiki.pages} == {"index", "a"}


async def test_run_plan_only_and_with_fake_llm(tmp_path, monkeypatch):
    root = _module(tmp_path / "m")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("# Overview\n")
    (wiki / "_sidebar.md").write_text("# Proj\n\n- [Overview](index.md)\n")
    out = tmp_path / "out"
    params = BizParams(clusters=(1, 3), utility_fanin=4, min_reach=1, walks_per_root=8,
                       walk_length=6)
    settings = Settings(cache_path=str(tmp_path / "cache.db"))
    res = await run_bizlogic(root, out, settings, params, wiki_dir=wiki, plan_only=True)
    assert res["per_k"][1]["trajectories"] == 1
    assert res["per_k"][3]["trajectories"] >= 1
    k1 = out / "bizlogic" / "k1"
    assert (k1 / "trajectories.json").is_file() and (k1 / "clusters.json").is_file()
    html = Path(res["per_k"][1]["html"])
    assert html.is_file() and "Business Logic (K=1)" in html.read_text()
    assert (k1 / "wiki" / "index.md").is_file()         # existing wiki pages kept

    calls = []

    async def fake_complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                            validate=None):
        calls.append(tag)
        if "cluster" in tag or "subsystem" in tag:
            return json.dumps({"name": "Registration", "summary": "Handles user registration.",
                               "kind": "business"})
        return json.dumps({"title": "User registration", "summary": "Registers a user.",
                           "trigger": "Registration Request", "actors": ["customer"],
                           "steps": ["authenticate", "store context"], "outcome": "registered",
                           "category": "business procedure", "confidence": "high"})

    from codocs import llm as llm_mod
    monkeypatch.setattr(llm_mod.LLM, "complete", fake_complete)
    res = await run_bizlogic(root, out, settings, BizParams(clusters=(2,), utility_fanin=4,
                                                            min_reach=1, walks_per_root=8,
                                                            walk_length=6))
    data = json.loads((out / "bizlogic" / "k2" / "business_logic.json").read_text())
    assert data and all(d["logic"]["title"] == "User registration" for d in data)
    k2 = out / "bizlogic" / "k2"
    page = (k2 / "wiki" / "bizlogic-k2-T001.md").read_text()
    assert "# User registration" in page and "```mermaid" in page
    assert "## Where in the code" in page and "Evidence:" not in page
    assert "autonumber" not in page and "- **Step " in page
    assert list((k2 / "wiki").glob("bizlogic-k2-p*.md"))           # group pages
    assert (k2 / "nodes.json").is_file()
    assert any("cluster" in c for c in calls) and any(c.startswith("bizlogic T") for c in calls)


NEW_REPLY = {
    "title": "UE registration", "short_title": "Register UE", "summary": "Registers a UE.",
    "scope": "single", "trigger": "Registration Request from the UE", "trigger_sources": ["UE"],
    "actors": [{"name": "UE", "via": "N1"}, {"name": "UDM", "via": "N8"}],
    "steps": [{"action": "receive the request", "at": 1, "actor": "UE", "direction": "in",
               "message": "Registration Request"},
              {"action": "store the context", "at": 1, "actor": "UDM", "direction": "out",
               "message": "Save"}],
    "alternatives": [{"at": 1, "condition": "authentication fails", "result": "reject"}],
    "state_changes": [{"entity": "UE RM state", "from": "RM-DEREGISTERED",
                       "to": "RM-REGISTERED", "at": 1, "evidence": "save"}],
    "implements": ["Initial registration"], "variants": ["mobility update"],
    "outcome": "UE registered", "category": "network procedure", "confidence": "high"}


def _fake_llm(monkeypatch, reply: dict, sent: list | None = None) -> None:
    async def fake_complete(self, system, user, *, tag="", temperature=None, max_tokens=None,
                            validate=None):
        if sent is not None:
            sent.append(system + "\n" + user)
        if "cluster" in tag or "subsystem" in tag:
            return json.dumps({"name": "Registration", "summary": "Handles UE registration.",
                               "kind": "business"})
        return json.dumps(reply)

    from codocs import llm as llm_mod
    monkeypatch.setattr(llm_mod.LLM, "complete", fake_complete)


async def _run_new_schema(tmp_path, monkeypatch, profile="3gpp-5gc", sent=None) -> Path:
    from codocs.bizlogic import diagrams as G
    monkeypatch.setattr(G, "target_level", lambda k: ("procedure", ""))   # K=2 is a family
    _fake_llm(monkeypatch, NEW_REPLY, sent)
    out = tmp_path / "out"
    await run_bizlogic(_module(tmp_path / "m"), out,
                       Settings(cache_path=str(tmp_path / "cache.db")),
                       BizParams(clusters=(2,), utility_fanin=4, min_reach=1,
                                 walks_per_root=8, walk_length=6),
                       profile=load_profile(profile))
    return out / "bizlogic" / "k2"


async def test_run_with_new_schema_reply(tmp_path, monkeypatch):
    """the 3gpp-5gc profile: procedures, interfaces and states reach the pages."""
    from codocs.bizlogic import domain as D

    sent: list[str] = []
    kdir = await _run_new_schema(tmp_path, monkeypatch, sent=sent)
    assert D.active().empty                                # restored after the run
    assert any("(a 3GPP 5G core network)" in x and "[KNOWN PROCEDURES]" in x for x in sent)
    stored = json.loads((kdir.parent / "domain_profile.json").read_text())
    assert stored["name"] == "3gpp-5gc" and stored["procedures"]
    assert json.loads((kdir.parent / "summary.json").read_text())["domain_profile"] == "3gpp-5gc"
    wiki = kdir / "wiki"
    logic = [p.read_text() for p in wiki.glob("bizlogic-k2-T*.md")]
    assert logic
    joined = "\n".join(logic)
    assert "sequenceDiagram" in joined and "\n  break " in joined
    assert "stateDiagram-v2" in joined
    assert all("| **Initiated by** | UE" in t for t in logic)
    assert all("## Variants" in t and "## Where in the code" in t for t in logic)
    assert "autonumber" not in joined and "> Diagram omitted" not in joined
    assert "## Procedures" in (wiki / "bizlogic-k2.md").read_text()
    assert "Initial registration (TS 23.502 4.2.2.2.2)" in joined


async def test_run_without_a_profile_is_domain_neutral(tmp_path, monkeypatch):
    """the same reply without a profile: no 3gpp prompt text, no procedures, free-text
    actors; an earlier run's stored profile is removed."""
    import re

    stale = tmp_path / "out" / "bizlogic" / "domain_profile.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    sent: list[str] = []
    kdir = await _run_new_schema(tmp_path, monkeypatch, profile=None, sent=sent)
    systems = [x for x in sent if "You infer the BUSINESS LOGIC" in x or "You name one" in x]
    assert systems
    for x in systems:
        system = x.split("\n[OUTPUT LANGUAGE]", 1)[0]
        assert not re.search(r"3GPP|5G|\bAMF\b|\bSMSF\b|\bNRF\b|PROCEDURES\]|ALLOWED ACTORS",
                             system), system[:300]
    assert not stale.exists()
    data = json.loads((kdir / "business_logic.json").read_text())
    assert all(d["logic"]["implements"] == [] for d in data)
    assert all([a["name"] for a in d["logic"]["actors"]] == ["UE", "UDM"] for d in data)
    ov = (kdir / "wiki" / "bizlogic-k2.md").read_text()
    assert "## Procedures" not in ov and "3GPP" not in ov
    assert "domain_profile" not in json.loads((kdir.parent / "summary.json").read_text())


def _diagrams_ok(kdir: Path) -> None:
    """every generator's diagram for the stored results passes its check."""
    from codocs.bizlogic import diagrams as G
    from codocs.bizlogic.run import load_results
    r = load_results(kdir)
    m = G.build_model(r.k, r.clusters, r.parents, r.parent_infos, r.trajs, r.logic, "Proj")
    ds = [G.context_diagram(m)[0], G.procedure_usecases(m)[0], G.component_view(m)[0],
          G.call_map(m)[0]]
    ds += [G.call_map(m, p)[0] for p in m.children]
    ds += [G.usecase_group(m, p)[0] for p in m.children]
    ds += [G.ego_map(m, c)[0] for c in m.clusters]
    for lv in m.logics:
        ds += [G.participants(m, lv)[0], G.flow_diagram(m, lv)[0], G.state_logic(lv)[0]]
    for d in ds:
        if isinstance(d, G.Dia):
            assert d.check() == [], (d.check(), d.src()[:300])


async def test_render_from_json_roundtrip(tmp_path, monkeypatch):
    import shutil

    from codocs.bizlogic.run import render_from_json
    kdir = await _run_new_schema(tmp_path, monkeypatch)
    wiki = kdir / "wiki"
    before = sorted(p.stem for p in wiki.glob("bizlogic-*.md"))
    old = kdir.parent / "old" / "k2"                    # the same results in the old shape
    shutil.copytree(kdir, old)
    entries = json.loads((old / "business_logic.json").read_text())
    for e in entries:
        for key in ("own_support", "step_walks", "variants", "root_groups"):
            e.pop(key, None)
    (old / "business_logic.json").write_text(json.dumps(entries))
    (old / "nodes.json").unlink()
    for d in (kdir, old):
        for f in (d / "wiki").glob("bizlogic-*.md"):
            f.unlink()
        html = render_from_json(d, 2, profile=load_profile("3gpp-5gc") if d is old else None)
        assert html.is_file() and "overflow-x: auto" in html.read_text()
        assert sorted(p.stem for p in (d / "wiki").glob("bizlogic-*.md")) == before
        _diagrams_ok(d)
    page = (old / "wiki" / "bizlogic-k2-T001.md").read_text()
    assert "- Based on " in page and "walks following the whole path" not in page


@pytest.mark.parametrize("k", [1, 2])
def test_trajectory_count_reported_matches_files(tmp_path, k):
    root = _module(tmp_path)
    fg = build_graph(root, utility_fanin=4)
    h = build_hierarchy(fg)
    walks = sample_walks(fg, [r for r, _ in find_roots(fg, min_reach=1)], walks_per_root=5)
    trajs, stats = deduplicate(fg, walks, h.assign(k))
    assert stats["kept"] == len(trajs)


def test_grouped_var_hooks_are_aliases_not_tables(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "s.go").write_text(
        "package s\n\nvar (\n\tAFunc = a\n\tBFunc func() = b\n)\n\n"
        "var table = []func(){c, d}\n\n"
        "func a() {}\n\nfunc b() {}\n\nfunc c() {}\n\nfunc d() {}\n\n"
        "func useA() { AFunc() }\n\nfunc useB() { BFunc() }\n\n"
        "func run() {\n\tfor _, f := range table {\n\t\tf()\n\t}\n}\n")
    fg = build_graph(tmp_path)
    g = fg.graph
    assert set(g.successors("s::useA")) == {"s::a"}          # hook resolves to its target only
    assert set(g.successors("s::useB")) == {"s::b"}          # typed hook too
    assert {v for v in g.successors("s::run")} == {"s::c", "s::d"}
    assert g["s::run"]["s::c"]["kind"] == "table"
    assert fg.stats["handler_tables"] == 1


def test_dedup_aggregates_evidence_over_merged_walks(tmp_path):
    root = _module(tmp_path)
    fg = build_graph(root, utility_fanin=4)
    assign = {n: (0 if n.startswith(("app", "reg", "dereg")) else 1) for n in fg.nodes}
    walks = [["reg::Handle", "reg::save", "store::Store.Put"],
             ["reg::Handle", "reg::save", "store::commitImpl"],
             ["dereg::Handle", "dereg::release", "store::Remove"]]
    trajs, _ = deduplicate(fg, walks, assign)
    assert len(trajs) == 1 and trajs[0].support == 3
    t = trajs[0]
    assert dict(t.root_counts) == {"reg::Handle": 2, "dereg::Handle": 1}
    step0 = dict(t.step_functions[0])
    assert step0["reg::Handle"] == 2 and step0["reg::save"] == 2 and step0["dereg::Handle"] == 1
    assert set(dict(t.step_functions[1])) == {"store::Store.Put", "store::commitImpl",
                                              "store::Remove"}
