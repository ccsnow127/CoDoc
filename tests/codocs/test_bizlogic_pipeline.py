"""the business-logic pipeline: graph resolution, clustering, walks,
deduplication, prompts, llm parsing and wiki export."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import networkx as nx
import numpy as np
import pytest
from click.testing import CliRunner

from codocs.bizlogic import cluster as cl
from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.export import add_section, build_pages, load_wiki
from codocs.bizlogic.graph import FuncGraph, FuncNode, build_graph
from codocs.bizlogic.infer import (
    BusinessLogic,
    ClusterInfo,
    cluster_prompt,
    describe_clusters,
    infer_trajectories,
    trajectory_prompt,
)
from codocs.bizlogic.walk import (
    Trajectory,
    deduplicate,
    find_roots,
    sample_walks,
    signature,
)

MOD = "module example.com/m\n\ngo 1.21\n"


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "go.mod").write_text(MOD)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _fg(edges: list[tuple[str, str]], summaries: dict[str, str] | None = None,
        kinds: dict[tuple[str, str], str] | None = None) -> FuncGraph:
    """a FuncGraph from 'pkg::Name' edges (call edges unless `kinds` says otherwise)."""
    g = nx.DiGraph()
    nodes: dict[str, FuncNode] = {}
    for u, v in edges:
        for n in (u, v):
            if n not in nodes:
                pkg, name = n.split("::")
                nodes[n] = FuncNode(id=n, pkg=pkg, file=f"{pkg}/x.go", name=name, kind="func",
                                    line=len(nodes) + 1, loc=5,
                                    summary=(summaries or {}).get(n, ""))
                g.add_node(n)
        g.add_edge(u, v, kind=(kinds or {}).get((u, v), "call"), weight=1.0)
    return FuncGraph(module="m", graph=g, nodes=nodes)


# ---- graph resolution

def test_cross_package_function_alias_resolves_to_a_call(tmp_path):
    root = _write(tmp_path, {
        "b/b.go": "package b\n\nfunc FindPeer() { query() }\n\nfunc query() { send() }\n\n"
                  "func send() {}\n",
        "a/hooks.go": 'package a\n\nimport "example.com/m/b"\n\n'
                         "var (\n\tFindPeerFunc = b.FindPeer // hook for tests\n)\n",
        "a/a.go": "package a\n\nfunc Caller() {\n\tFindPeerFunc()\n}\n",
        "c/c.go": 'package c\n\nimport "example.com/m/a"\n\nfunc Remote() { a.FindPeerFunc() }\n',
    })
    fg = build_graph(root)
    g = fg.graph
    assert g.has_edge("a::Caller", "b::FindPeer") and g["a::Caller"]["b::FindPeer"]["kind"] == "call"
    assert g.has_edge("c::Remote", "b::FindPeer")            # qualified use of the alias
    assert "b::FindPeer" not in dict(find_roots(fg, min_reach=1, max_roots=None))


def test_method_calls_follow_the_receiver_type(tmp_path):
    root = _write(tmp_path, {"m/m.go": (
        'package m\n\nimport (\n\t"encoding/json"\n\t"sync"\n)\n\n'
        "type A struct {\n\tmu  sync.Mutex\n\tSub *B\n}\n\n"
        "type B struct{}\n\ntype L struct{}\n\ntype E struct{}\n\n"
        "func (a *A) Tai() int {\n\tx := 1\n\treturn x\n}\n\n"
        "func (b *B) Tai() int {\n\tx := 2\n\treturn x\n}\n\n"
        "func (b *B) Marshal() []byte {\n\tvar out []byte\n\treturn out\n}\n\n"
        "func (l *L) Lock() {\n\tn := 0\n\t_ = n\n}\n\n"
        "func (e *E) Error() string {\n\ts := \"\"\n\treturn s\n}\n\n"
        "func NewA() *A { return &A{} }\n\n"
        "func doit() error { return nil }\n\n"
        "func use(x *A) {\n\tx.Tai()\n\tx.Sub.Tai()\n\ty := NewA()\n\ty.Tai()\n"
        "\tx.mu.Lock()\n\tjson.Marshal(x)\n\terr := doit()\n\t_ = err.Error()\n}\n\n"
        "func useB() {\n\tb := &B{}\n\tb.Tai()\n}\n")})
    g = build_graph(root, fold_accessors=False, plumbing=None).graph
    methods = {v for v in g.successors("m::use") if "." in v.split("::")[1]}
    assert methods == {"m::A.Tai", "m::B.Tai"}                 # x.Tai, x.Sub.Tai, y.Tai
    assert set(g.successors("m::useB")) == {"m::B.Tai"}        # not the same-named A.Tai
    for wrong in ("m::L.Lock", "m::B.Marshal", "m::E.Error"):  # sync, json and error calls
        assert not g.has_edge("m::use", wrong)


def test_struct_table_field_selects_one_entry(tmp_path):
    root = _write(tmp_path, {
        "store/store.go": "package store\n\nfunc Load(k string) {}\n\n"
                          "func Drop(k string) {}\n\n"
                          "func Snapshot() {}\n\n"
                          "func Has(k string) bool { return true }\n",
        "session/mem.go": (
            'package session\n\nimport "example.com/m/store"\n\n'
            "type Database struct {\n\tGet    func(string)\n\tDel    func(string)\n"
            "\tRead   func()\n\tExists func(string) bool\n}\n\n"
            "var Store = Database{\n\tGet:    store.Load,\n\tDel:    store.Drop,\n"
            "\tRead:   store.Snapshot,\n\tExists: store.Has,\n}\n\n"
            "func Exists(k string) bool { return false }\n\n"
            "func Delete(k string) { Store.Del(k) }\n\n"
            "func Dump() { keep(Store) }\n\nfunc keep(d Database) {}\n"),
    })
    g = build_graph(root).graph
    assert set(g.successors("session::Delete")) == {"store::Drop"}
    assert g["session::Delete"]["store::Drop"]["kind"] == "call"
    dump = {v for v in g.successors("session::Dump") if g["session::Dump"][v]["kind"] == "table"}
    assert dump == {"store::Load", "store::Drop", "store::Snapshot", "store::Has"}


def test_functions_only_tests_use_and_test_helper_files_are_left_out(tmp_path):
    root = _write(tmp_path, {
        "t/t.go": "package t\n\nfunc NewTable() { handler() }\n\nfunc handler() { leaf() }\n\n"
                  "func leaf() {}\n\nfunc Used() { other() }\n\nfunc other() { leaf() }\n",
        "t/t_test.go": 'package t\n\nimport "testing"\n\nfunc TestX(t *testing.T) { NewTable() }\n',
        "t/test_helper.go": "package t\n\nfunc HelperInit() { leaf() }\n",
        "app/main.go": 'package main\n\nimport "example.com/m/t"\n\nfunc main() { t.Used() }\n',
    })
    fg = build_graph(root)
    assert "t::NewTable" not in fg.nodes and "t::HelperInit" not in fg.nodes
    assert {"t::Used", "t::handler", "app::main"} <= set(fg.nodes)
    assert fg.stats["test_only_functions"] == 1


def test_vendored_third_party_tree_is_skipped(tmp_path):
    root = _write(tmp_path, {
        "app/main.go": "package main\n\nfunc main() { run() }\n\nfunc run() {}\n",
        "app/lib/LICENSE": "BSD\n", "app/lib/AUTHORS": "someone\n",
        "app/lib/lib.go": "package lib\n\nfunc Dial() { dial() }\n\nfunc dial() {}\n",
    })
    fg = build_graph(root)
    assert not any(n.startswith("app/lib::") for n in fg.nodes)
    assert fg.stats["vendored_dirs"] == ["app/lib"]


def test_nested_markdown_docs_belong_to_the_subpackage(tmp_path):
    d = tmp_path / "core" / "common" / "markdown_docs" / "sel" / "check"
    d.mkdir(parents=True)
    (d / "check.md").write_text("## FunctionDef Check\n**Check**: Checks the equipment id.\n")
    (tmp_path / "core" / "common" / "markdown_docs" / "c.md").write_text(
        "## FunctionDef Top\n**Top**: Top-level helper.\n")
    docs = DocIndex.build([tmp_path])
    assert docs.get("core/common/sel/check", "Check").summary == "Checks the equipment id."
    assert docs.get("core/common", "Check") is None
    assert docs.get("core/common", "Top") is not None


def test_utilities_cover_plumbing_infra_packages_and_helper_copies(tmp_path):
    callers = {f"p{i}/p.go": (f'package p{i}\n\nimport "example.com/m/infra"\n\n'
                              f"func Work{i}() {{ infra.Put() }}\n") for i in range(4)}
    root = _write(tmp_path, {
        **callers,
        "infra/infra.go": "package infra\n\nfunc Put() { put() }\n\nfunc put() {}\n",
        "a/a.go": "package a\n\nfunc WriteAuditLog() {}\n\nfunc AddDetail() {}\n\n"
                  "func CountTraffic() {}\n\n"
                  "func Flow() { WriteAuditLog(); AddDetail(); CountTraffic(); step() }\n\n"
                  "func step() {}\n",
        "b/b.go": "package b\n\nfunc AddDetail() {}\n\n"
                  "func X() { AddDetail() }\n\nfunc Y() { AddDetail() }\n",
    })
    fg = build_graph(root, utility_fanin=3, infra_callers=4)
    assert {"infra::Put", "infra::put"} <= fg.utilities           # called from 4 packages
    assert "a::WriteAuditLog" in fg.utilities                      # plumbing name (Log$)
    assert {"a::AddDetail", "b::AddDetail"} <= fg.utilities        # 1 + 2 callers over copies
    assert "a::step" not in fg.utilities and "a::CountTraffic" not in fg.utilities
    assert fg.stats["infra_packages"] == ["infra"]
    # a domain profile adds its own plumbing names to the default ones
    from codocs.bizlogic.profile import DomainProfile
    from codocs.bizlogic.run import plumbing_rx
    fg = build_graph(root, utility_fanin=3, infra_callers=4,
                     plumbing=plumbing_rx(DomainProfile(plumbing="Traffic$")))
    assert {"a::CountTraffic", "a::WriteAuditLog"} <= fg.utilities


# ---- clustering

def test_spectral_embedding_does_not_depend_on_rounding():
    C = 14
    S = np.zeros((C, C))
    for a, b in [(0, 1), (1, 2), (2, 3), (3, 4), (0, 4), (5, 6), (6, 7), (8, 9)]:
        S[a, b] = S[b, a] = 1.0 / (1 + a)
    rng = np.random.default_rng(3)
    X = cl._rownorm(rng.random((C, 10)))
    noise = rng.normal(size=S.shape) * 1e-13
    noise = (noise + noise.T) / 2
    noise[S == 0] = 0
    w = np.arange(1, C + 1, dtype=float)
    e1, e2 = cl._embed(S, X, None, 0.6), cl._embed(S + noise, X, None, 0.6)
    d1 = ((e1[:, None] - e1[None]) ** 2).sum(-1)
    d2 = ((e2[:, None] - e2[None]) ** 2).sum(-1)
    assert np.allclose(d1, d2, atol=1e-6)
    assert cl._bisect_all(e1, w, 0) == cl._bisect_all(e2, w, 0)
    assert not np.linalg.norm(cl._spectral(S)[10:], axis=1).any()   # isolated: no structure


def test_bisection_does_not_spend_splits_on_tiny_outliers():
    rng = np.random.default_rng(0)
    emb = np.vstack([rng.normal(0, 0.1, (10, 2)), rng.normal(0, 0.1, (10, 2)) + [5, 0],
                     [[100.0, 100.0]]])
    w = np.array([10.0] * 20 + [1.0])
    splits = cl._bisect_all(emb, w, 0)
    first = splits[0][1]
    assert len(first) == 10 and 20 not in first                  # the two big groups first
    assert splits[-1][1] == [20] or 20 not in [c for _, m in splits[:2] for c in m]


def test_refine_splits_oversized_communities():
    g = nx.Graph()
    for blk in range(4):                                 # 4 cliques joined in a ring
        nodes = range(blk * 10, blk * 10 + 10)
        g.add_edges_from((a, b) for a in nodes for b in nodes if a < b)
        g.add_edge(blk * 10, ((blk + 1) % 4) * 10)
    parts = cl._refine(g, [list(range(40))], cap=15, resolution=1.0, seed=0)
    assert len(parts) >= 3 and max(len(p) for p in parts) <= 15


def test_disconnected_subsystems_never_share_a_coarse_cluster(tmp_path):
    # per subsystem: a chain of packages calling each other plus isolated packages, with
    # function names that borrow another subsystem's vocabulary; no call between subsystems
    files = {}
    words = {"orders": ("User", "Bill"), "users": ("Bill", "Order"), "billing": ("Order", "User")}
    for sub, (w1, w2) in words.items():
        for i in range(16):
            w = w1 if i % 2 else w2
            imp = (f'import "example.com/m/{sub}/scenario/c{i + 1}"\n\n' if i < 3 else "")
            nxt = f"c{i + 1}.{w2 if (i + 1) % 2 == 0 else w1}{w}0()" if i < 3 else ""
            body = "\n\n".join(f"func {w}{w}{j}() {{ {w}{w}{j + 1}() }}" for j in range(3))
            files[f"{sub}/scenario/c{i}/x.go"] = (f"package c{i}\n\n{imp}{body}\n\n"
                                                 f"func {w}{w}3() {{ {nxt} }}\n")
    fg = build_graph(_write(tmp_path, files))
    h = cl.build_hierarchy(fg, seed=0)
    a = h.assign(3)
    for sub in ("orders", "users", "billing"):
        assert len({c for n, c in a.items() if n.startswith(sub + "/")}) == 1


def test_walks_do_not_depend_on_hash_seed(tmp_path):
    entries = "".join(f"\t\"h{i}\": H{i},\n" for i in range(12))
    funcs = "".join(f"func H{i}() {{ s{i}() }}\n\nfunc s{i}() {{}}\n\n" for i in range(12))
    root = _write(tmp_path / "m", {"app/main.go": (
        f"package main\n\nvar handlers = map[string]func(){{\n{entries}}}\n\n"
        f"func main() {{\n\tfor _, h := range handlers {{\n\t\th()\n\t}}\n}}\n\n{funcs}")})
    script = (
        "import sys, json\n"
        "from codocs.bizlogic.graph import build_graph\n"
        "from codocs.bizlogic.cluster import build_hierarchy\n"
        "from codocs.bizlogic.walk import sample_walks\n"
        "fg = build_graph(sys.argv[1])\n"
        "h = build_hierarchy(fg, seed=0)\n"
        "w = sample_walks(fg, ['app::main'], 30, 6, 0)\n"
        "print(json.dumps([h.communities, h.splits, w]))\n")
    outs = []
    for seed in ("1", "2", "3", "4"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run([sys.executable, "-c", script, str(root)], env=env,
                             capture_output=True, text=True, check=True)
        outs.append(res.stdout)
    assert len(set(outs)) == 1


# ---- roots and walks

def test_dispatched_procedures_become_roots():
    edges = [("h::Handler", "h::Main")]
    for i in range(16):
        edges += [("h::Main", f"h::Proc{i}"), (f"h::Proc{i}", f"h::step{i}")]
    fg = _fg(edges)
    roots = dict(find_roots(fg, min_reach=1, max_roots=None))
    assert "h::Handler" in roots and all(f"h::Proc{i}" in roots for i in range(16))
    assert "h::Main" not in roots


def test_root_budget_is_proportional_and_prefers_new_coverage():
    edges = []
    for grp, n in (("big", 20), ("mid", 10), ("small", 1)):
        for i in range(n):
            edges += [(f"{grp}/x::R{i}", f"{grp}/x::a{i}"), (f"{grp}/x::a{i}", f"{grp}/x::b{i}")]
    fg = _fg(edges)
    roots = find_roots(fg, min_reach=1, max_roots=12)
    per = {g: sum(r.startswith(g + "/") for r, _ in roots) for g in ("big", "mid", "small")}
    assert per["big"] >= 7 and per["small"] == 1 and sum(per.values()) == 12

    fg = _fg([("p::X", "p::a"), ("p::X", "p::b"), ("p::X", "p::c"), ("p::Y", "p::a"),
              ("p::Y", "p::b"), ("p::Z", "p::e")])
    assert {r for r, _ in find_roots(fg, min_reach=1, max_roots=2)} == {"p::X", "p::Z"}


def test_walks_backtrack_to_sibling_steps():
    fg = _fg([("p::Root", "p::a"), ("p::Root", "p::b"), ("p::b", "p::c")])
    walks = sample_walks(fg, ["p::Root"], walks_per_root=10, max_len=4, seed=0)
    assert all(len(w) == 4 and set(w) == {"p::Root", "p::a", "p::b", "p::c"} for w in walks)


def test_walks_do_not_enter_other_roots_through_registrations():
    fg = _fg([("app::main", "app::Init"), ("app::main", "h::Handle"), ("h::Handle", "h::do")],
             kinds={("app::main", "h::Handle"): "ref"})
    walks = sample_walks(fg, ["app::main"], walks_per_root=5, max_len=6, seed=0,
                         stop_at={"app::main", "h::Handle"})
    assert all("h::Handle" not in w for w in walks)


# ---- deduplication

def _assign(fg: FuncGraph, mapping: dict[str, int]) -> dict[str, int]:
    return {n: mapping[n.split("::")[1]] for n in fg.nodes}


def test_rare_extension_folds_into_dominant_prefix_not_vice_versa():
    fg = _fg([("p::r", "p::a"), ("p::a", "p::b")])
    assign = _assign(fg, {"r": 0, "a": 0, "b": 1})
    walks = [["p::r", "p::a"]] * 1000 + [["p::r", "p::a", "p::b"]]
    trajs, stats = deduplicate(fg, walks, assign)
    assert [t.clusters for t in trajs] == [[0]]
    assert trajs[0].variants == [([0, 1], 1)] and trajs[0].support == 1001
    assert stats["folded_low_support"] == 1


def test_representative_and_ranking_use_the_trajectorys_own_walks():
    fg = _fg([("p::r", "p::a"), ("p::a", "p::b"), ("p::b", "p::c"), ("p::r2", "p::a"),
              ("p::u", "p::v")])
    assign = _assign(fg, {"r": 0, "r2": 0, "a": 0, "b": 1, "c": 2, "u": 3, "v": 3})
    walks = ([["p::r", "p::a", "p::b"]] * 20 + [["p::r2", "p::a", "p::b", "p::c"]]
             + [["p::u", "p::v"]] * 3)
    trajs, _ = deduplicate(fg, walks, assign, min_share=0)
    by = {tuple(t.clusters): t for t in trajs}
    t = by[(0, 1, 2)]
    assert signature(t.path, assign) == (0, 1, 2) and t.path[0] == "p::r2"
    assert (t.own_support, t.support, t.step_walks) == (1, 21, [21, 21, 1])
    capped, stats = deduplicate(fg, walks, assign, min_share=0, max_trajectories=1)
    assert capped[0].clusters == [3] and stats["dropped_own_walks"] == 1
    for t in trajs:
        assert signature(t.path, assign) == tuple(t.clusters)


def test_fewer_clusters_keep_fewer_trajectories(tmp_path):
    from test_bizlogic import _module
    fg = build_graph(_module(tmp_path), utility_fanin=4)
    h = cl.build_hierarchy(fg, seed=0)
    walks = sample_walks(fg, [r for r, _ in find_roots(fg, min_reach=1)], 30, 8, 1)
    counts = [len(deduplicate(fg, walks, h.assign(k), min_share=0.2)[0])
              for k in range(1, h.max_k + 1)]
    assert counts[0] == 1 and counts == sorted(counts)


def test_root_evidence_is_spread_over_root_subsystems():
    edges = [(f"x/p::R{i}", "x/p::a") for i in range(14)] + [("y/q::S", "x/p::a")]
    fg = _fg(edges)
    assign = {n: 0 for n in fg.nodes}
    walks = [[f"x/p::R{i}", "x/p::a"] for i in range(14) for _ in range(5)] + \
        [["y/q::S", "x/p::a"]]
    t = deduplicate(fg, walks, assign)[0][0]
    assert "y/q::S" in dict(t.root_counts)
    assert ("y", 1, 1) in [(g.split("/")[0], r, w) for g, r, w in t.root_groups]


# ---- prompts and llm replies

def _traj_fg(n_steps: int, path_len: int, doc: str):
    nodes = [f"p{i}::F{i}" for i in range(max(n_steps * 6, path_len))]
    edges = list(zip(nodes, nodes[1:]))
    return _fg(edges, summaries={n: doc for n in nodes}), nodes


def test_narrow_trajectory_prompt_keeps_the_whole_chain():
    fg, nodes = _traj_fg(8, 10, "x" * 400)
    t = Trajectory(id="T001", clusters=list(range(8)), path=nodes[:10], support=5,
                   roots=[nodes[0]], own_support=5, step_walks=[5] * 8,
                   root_counts=[(nodes[0], 5)],
                   step_functions=[[(nodes[i * 6 + j], 5) for j in range(6)] for i in range(8)])
    _sys, user = trajectory_prompt(fg, t, {}, 40, "en", {n: 0 for n in nodes})
    for n in nodes[:10]:
        assert f"{fg.nodes[n].pkg}.{fg.nodes[n].name} " in user.split("REPRESENTATIVE")[1]
    assert "more functions" not in user


def test_trajectory_prompt_states_coverage_and_level():
    fg, nodes = _traj_fg(1, 4, "does a thing")
    t = Trajectory(id="T001", clusters=[0, 1, 2], path=nodes[:3], support=100, roots=[nodes[0]],
                   own_support=2, step_walks=[100, 40, 2], root_counts=[(nodes[0], 100)],
                   step_functions=[[(nodes[0], 100)], [(nodes[1], 40)], [(nodes[2], 2)]])
    assign = {nodes[0]: 0, nodes[1]: 1, nodes[2]: 2}
    _s5, coarse = trajectory_prompt(fg, t, {}, 5, "en", assign)
    _s40, fine = trajectory_prompt(fg, t, {}, 40, "en", assign)
    assert "all pass through the same cluster sequence" not in coarse
    assert "reached by 2 of 100 walks; optional branch" in coarse
    assert "2 follow the whole cluster sequence" in coarse
    assert "[TARGET LEVEL] capability family" in coarse and "[TARGET LEVEL] concrete flow" in fine
    assert "one step per cluster" not in _s5


async def test_llm_json_is_coerced_not_mangled():
    fg, nodes = _traj_fg(1, 2, "doc")
    trajs = [Trajectory(id=f"T00{i}", clusters=[0], path=nodes[:2], support=1, roots=[nodes[0]],
                        own_support=1, step_walks=[1]) for i in range(1, 5)]
    replies = {
        "T001": json.dumps({"title": "Order checkout", "trigger": None,
                            "actors": "customer, payment gateway, warehouse",
                            "steps": "Receive request; authenticate; accept", "outcome": None,
                            "confidence": "high"}),
        "T002": json.dumps([{"title": "wrapped"}]),
        "T003": '{"title": "a"} note: use {x}',
        "T004": json.dumps({"summary": "no title"}),
    }

    class Fake:
        async def complete(self, system, user, *, tag="", max_tokens=None, **kw):
            return replies[tag.split()[1]]

    out = await infer_trajectories(Fake(), fg, trajs, {}, 5, "en", {n: 0 for n in nodes})
    a = out["T001"]
    assert a.trigger == "" and a.outcome == ""
    assert [x.name for x in a.actors] == ["customer", "payment gateway", "warehouse"]
    assert [s.action for s in a.steps] == ["Receive request", "authenticate", "accept"]
    assert all(s.at is None for s in a.steps)          # a reply's string steps stay unmapped
    assert a.confidence == "medium" and a.llm_confidence == "high"   # one walk: not high
    assert out["T002"].title == "wrapped" and out["T003"].title == "a"
    assert out["T004"].error == "missing title"


async def test_invalid_replies_are_not_cached(monkeypatch, tmp_path):
    from codocs.config import Settings
    from codocs.llm import LLM, LLMError
    from codocs.repowiki.llm.client import LLMClient

    replies = ["Here: {bad json", '{"title": "ok"}', "nope", "still nope"]
    sent = []

    async def complete(client, messages, **kw):
        sent.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    settings = Settings(api_key="k", cache_path=str(tmp_path / "c.db"))
    ok = lambda r: r.startswith("{")                      # noqa: E731
    async with LLM(settings) as llm:
        assert await llm.complete("s", "u", validate=ok) == '{"title": "ok"}'
        assert len(sent) == 2 and sent[1][-1]["role"] == "user"   # asked once more
        with pytest.raises(LLMError):
            await llm.complete("s", "u2", validate=ok)
    async with LLM(settings) as llm:
        assert await llm.complete("s", "u", validate=ok) == '{"title": "ok"}'
        assert llm.calls == 0                               # the valid reply was cached
        replies.append('{"title": "fresh"}')
        assert await llm.complete("s", "u2", validate=ok) == '{"title": "fresh"}'


def test_cluster_description_spreads_central_members_and_shows_shares():
    edges = [("core/a::Hub", f"core/a::f{i}") for i in range(30)]
    edges += [(f"core/a::f{i}", f"core/a::f{i + 1}") for i in range(29)]
    edges += [(f"cli/b::g{i}", f"cli/b::g{i + 1}") for i in range(19)]
    fg = _fg(edges, summaries={n: "doc" for _, n in edges})
    info = describe_clusters(fg, {n: 0 for n in fg.nodes})[0]
    assert sum(n.startswith("cli/") for n in info.central) >= 6
    _sys, user = cluster_prompt(fg, info, "en")
    assert "[SUBSYSTEMS] core 61%, cli 39%" in user


# ---- export

def _pages(k: int, n: int, tag: str) -> tuple[list, list, dict]:
    fg, nodes = _traj_fg(1, 3, "d")
    trajs = [Trajectory(id=f"T{i:03d}", clusters=[0], path=nodes[:2], support=1, roots=[nodes[0]],
                        own_support=1, step_walks=[1]) for i in range(1, n + 1)]
    logic = {t.id: BusinessLogic(trajectory=t.id, title=f"{tag} {t.id}") for t in trajs}
    clusters = {0: ClusterInfo(id=0, size=3, packages=[], central=[], name="C")}
    parents = {0: 0}
    pages = build_pages(fg, k, clusters, parents, clusters, trajs, logic, {nodes[0]: 0},
                        {"walks": 1})
    return pages, trajs, logic


def test_merging_a_section_twice_replaces_it_and_lands_on_the_index(tmp_path):
    w = tmp_path / "wiki"
    w.mkdir()
    (w / "aaa.md").write_text("# A page\n")
    (w / "index.md").write_text("# Overview\n")
    (w / "_sidebar.md").write_text("# Proj\n\n- [Overview](index.md)\n- [A page](aaa.md)\n")
    wiki = load_wiki(w)
    assert wiki.pages[0].id == "index"
    pages, trajs, logic = _pages(20, 5, "old")
    add_section(wiki, 20, pages, trajs, logic)
    assert [p.id for p in wiki.pages[:2]] == ["index", "bizlogic-k20"]
    pages, trajs, logic = _pages(20, 2, "new")
    add_section(wiki, 20, pages, trajs, logic)
    assert sum(s.title == "Business Logic (K=20)" for s in wiki.sidebar) == 1
    ids = [p.id for p in wiki.pages if p.id.startswith("bizlogic-k20-T")]
    assert ids == ["bizlogic-k20-T001", "bizlogic-k20-T002"]
    t1 = next(p for p in wiki.pages if p.id == "bizlogic-k20-T001")
    assert "new T001" in t1.content


def test_hierarchy_renders_as_lists_in_html():
    from codocs.repowiki.export.html import _markdown_to_html
    pages, _t, _l = _pages(5, 1, "x")
    html = _markdown_to_html(pages[0].content)
    assert "<p>  - " not in html and "<ul>" in html


def test_overview_says_plan_only_only_without_llm():
    fg, nodes = _traj_fg(1, 2, "d")
    t = Trajectory(id="T001", clusters=[0], path=nodes[:2], support=1, roots=[nodes[0]],
                   own_support=1, step_walks=[1])
    clusters = {0: ClusterInfo(id=0, size=2, packages=[], central=[])}
    args = (fg, 5, clusters, {0: 0}, clusters, [t], {}, {nodes[0]: 0}, {})
    assert "plan only" in build_pages(*args, plan_only=True)[0].content
    assert "plan only" not in build_pages(*args)[0].content


def test_cli_max_trajectories_zero_means_all_and_k_is_clamped(tmp_path, monkeypatch):
    from test_bizlogic import _module

    from codocs.cli import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    root = _module(tmp_path / "m")
    out = tmp_path / "out"
    res = CliRunner().invoke(cli, ["bizlogic", str(root), "-o", str(out), "-k", "999",
                                   "--max-trajectories", "0", "--plan-only",
                                   "--utility-fanin", "4", "--min-reach", "1"],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    kdirs = [p.name for p in (out / "bizlogic").iterdir() if p.is_dir()]
    assert kdirs and "k999" not in kdirs
    data = json.loads((out / "bizlogic" / kdirs[0] / "trajectories.json").read_text())
    assert data["trajectories"] and data["dedup"]["dropped_by_cap"] == 0
    assert math.isfinite(data["dedup"]["min_support"])


def test_representative_is_not_a_short_walk_repeated_by_a_narrow_root():
    fg = _fg([("p::r", "q::b"), ("p::r", "p::a"), ("p::a", "q::b")],
             summaries={"p::r": "root", "p::a": "step", "q::b": "end"})
    assign = _assign(fg, {"r": 0, "a": 0, "b": 1})
    walks = [["p::r", "q::b"]] * 20 + [["p::r", "p::a", "q::b"]]
    t = deduplicate(fg, walks, assign)[0][0]
    assert t.path == ["p::r", "p::a", "q::b"]
