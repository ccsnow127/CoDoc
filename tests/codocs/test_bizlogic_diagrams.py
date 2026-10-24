"""mermaid diagrams of the bizlogic wiki: label safety, builder guard, model and generators
(with the 3gpp-5gc example profile, and domain-neutral without a profile)."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from codocs.bizlogic import diagrams as G
from codocs.bizlogic import domain as D
from codocs.bizlogic.domain import SELF
from codocs.bizlogic.infer import BusinessLogic, ClusterInfo, parse_logic
from codocs.bizlogic.profile import load_profile
from codocs.bizlogic.walk import Trajectory

CHROME = Path(os.environ.get("CODOCS_CHROME")
              or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
BUNDLE = Path.home() / ".repowiki" / "mermaid.min.js"


# ---- fixtures ------------------------------------------------------------------------------

# the example profile plus synthetic package evidence (an amf/sm package implements N11)
EVIDENCE = (("amf/ngap", "N2 (NGAP)", frozenset({"NG-RAN (gNB)"})),
            ("amf/n26", "N26 (GTPv2-C)", frozenset({"MME (EPC)"})),
            ("amf/sm", "N11 (Nsmf)", frozenset({"SMF"})),
            ("amf/policy", "N15 (Npcf)", frozenset({"PCF"})),
            ("smsf/service", "N20 (Nsmsf)", frozenset({"AMF"})))


def gpp_profile():
    base = load_profile("3gpp-5gc")       # plus one more procedure family for the D2 cap
    procs = {**base.procedures, "Converged charging": ("TS 32.290", "Charging")}
    return replace(base, pkg_evidence=EVIDENCE, procedures=procs)


@pytest.fixture(autouse=True)
def gpp():
    prev = D.use_profile(gpp_profile())
    yield
    D.use_profile(prev)


PKGS = {0: [("amf/ngap/handler", 50), ("amf/ran", 10)],
        1: [("amf/n26", 40), ("smsf/diameter", 30), ("nrf/discovery", 5)],
        2: [("amf/sm", 30)], 3: [("smsf/service", 25)],
        4: [("nrf/ha", 20)], 5: [("amf/nas", 15)]}
SIZES = {0: 100, 1: 90, 2: 80, 3: 70, 4: 30, 5: 20}
KINDS = {0: "protocol", 1: "business", 2: "business", 3: "data", 4: "ops", 5: "platform"}
PARENTS = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2}
RAW_ACTORS = ["UE", "gNB/RAN", "SMF", "PCF", "UDM", "operator", "MME", "source AMF",
              "OSS", "monitoring system", "other network functions",
              "Zeta box", "SMSF", "Redis", "Kubernetes"]
PATHS = [[0, 2, 0], [0, 1, 2], [2, 3], [0, 5, 2, 3], [4, 5], [1, 3], [5, 0, 2]]
FNS = {0: "amf/ngap::SendUeCxtRelCmd", 1: "amf/n26::SendRelocReq",
       2: "amf/sm::SetRegState", 3: "smsf/service::SmServiceMain",
       4: "nrf/ha::HaSwitch", 5: "amf/nas::RecvDeregReq"}
FAMILY_PROCS = ["Initial registration", "AN release", "Xn-based handover",
                "PDU session establishment", "AM policy association establishment",
                "Primary authentication", "MO SMS over NAS", "Location services",
                "Public warning message delivery", "NF registration",
                "Converged charging", "UE-triggered service request"]


def clusters_and_parents(n_groups: int = 3):
    cl = {c: ClusterInfo(id=c, size=SIZES[c], packages=PKGS[c], central=[FNS[c]],
                         name=f"Component {c}", kind=KINDS[c],
                         summary=f"Component {c} does things. More text.")
        for c in SIZES}
    if n_groups == 3:
        parents = dict(PARENTS)
    else:
        parents = {c: c for c in SIZES}
    pinfo = {p: ClusterInfo(id=p, size=0, packages=[], central=[], name=f"LLM parent {p}",
                            children=[c for c, q in parents.items() if q == p])
             for p in set(parents.values())}
    return cl, parents, pinfo


def make_traj(tid: str, clusters: list[int], roots=None, support=20, own=10, walks=None,
              variants=(), old=False, path=None) -> Trajectory:
    roots = list(roots or ["amf/ngap::RecvInitialUeMessage"])
    fns = [FNS.get(c, f"amf/x{c}::F{c}") for c in clusters]
    sf = [[(f, 5), ("amf/common::GetUeCtx", 9)] for f in fns]
    path = path or fns
    if old:
        return Trajectory(id=tid, clusters=clusters, path=path, support=support, roots=roots,
                          root_counts=[(r, 3) for r in roots], step_functions=sf)
    return Trajectory(id=tid, clusters=clusters, path=path, support=support, roots=roots,
                      own_support=own,
                      step_walks=walks or [support] + [own] * (len(clusters) - 1),
                      variants=list(variants), root_counts=[(r, 3) for r in roots],
                      root_groups=[], step_functions=sf)


def new_bl(tid: str, n: int, i: int) -> BusinessLogic:
    a1, a2, a3 = RAW_ACTORS[i % 12], RAW_ACTORS[(i + 3) % 12], RAW_ACTORS[(i + 7) % 15]
    steps = [{"at": 1, "action": f"receive request {i}", "actor": a1,
              "message": "Initial UE Message", "direction": "in"}]
    if n > 1:
        steps.append({"at": 2, "action": "update context", "actor": "", "direction": "internal"})
        steps.append({"at": n, "action": "notify party", "actor": a2,
                      "message": "Nsmf_PDUSession_UpdateSMContext", "direction": "out"})
    data = {"title": f"Logic {i} | with [brackets]", "short_title": f"Do thing {i}",
            "summary": "s", "scope": "single" if i % 4 else "",
            "trigger": f"{a1} sends a request",
            "trigger_sources": ["timer"] if i % 7 == 0 and i else [a1] if i % 5 else
            [a1, "timer"],
            "actors": [{"name": a1}, {"name": a2, "via": "N11"}, {"name": a3}, {"name": "AMF"}],
            "steps": steps,
            "alternatives": [{"at": 1, "condition": "context missing", "result": "reject"}],
            "implements": [FAMILY_PROCS[(i // 2) % len(FAMILY_PROCS)]] if i % 8 else [],
            "state_changes": [{"entity": "RM", "from": "RM-REGISTERED", "to": "RM-DEREGISTERED",
                               "at": 1, "evidence": "SetRegState"}],
            "supports": [FAMILY_PROCS[(i + 1) % len(FAMILY_PROCS)]],
            "category": "network procedure", "confidence": "medium", "outcome": "done"}
    return parse_logic(data, tid, n, stored=False)


def old_bl(tid: str, n: int, i: int) -> BusinessLogic:
    acts = [RAW_ACTORS[(i + j) % 15] for j in range(3)]
    data = {"title": f"Old logic {i}", "summary": "s",
            "trigger": "Incoming AMF signalling, notification or timer expiry" if i % 2
            else f"{acts[0]} request", "actors": acts,
            "steps": [f"step {j}" for j in range(n)], "outcome": "o",
            "category": "operations & maintenance" if i % 3 == 0 else "network procedure",
            "confidence": "low", "error": ""}
    return BusinessLogic.from_dict(data, tid, n)


def synthetic(k: int = 6, n_groups: int = 3):
    cl, parents, pinfo = clusters_and_parents(n_groups)
    trajs, logic = [], {}
    for i in range(30):
        tid = f"T{i + 1:03d}"
        seq = PATHS[i % len(PATHS)]
        roots = ["smsf/service::Handle"] if i % 6 == 5 else \
            ["amf/ngap::RecvInitialUeMessage", f"amf/comm::Recv{i}"]
        old = i % 2 == 1
        variants = [] if old else [([seq[0], 3], 4), ([seq[0], 4, 5], 2)]
        trajs.append(make_traj(tid, seq, roots=roots, support=20 + i, own=10 + i, old=old,
                               variants=variants))
        logic[tid] = old_bl(tid, len(seq), i) if old else new_bl(tid, len(seq), i)
    logic["T030"] = BusinessLogic(trajectory="T030", title="Actorless logic", actors=[])
    return G.build_model(k, cl, parents, pinfo, trajs, logic, "demo")


def one(bl_data: dict, traj: Trajectory, k: int = 20, stored: bool = False):
    cl, parents, pinfo = clusters_and_parents()
    bl = parse_logic(bl_data, traj.id, len(traj.clusters), stored=stored)
    m = G.build_model(k, cl, parents, pinfo, [traj], {traj.id: bl}, "demo")
    return m, m.logics[0]


SEQ_DATA = {
    "title": "UE-initiated deregistration", "short_title": "Deregister UE", "scope": "single",
    "trigger": "UE sends Deregistration Request", "trigger_sources": ["UE"],
    "actors": [{"name": "UE"}, {"name": "gNB"}, {"name": "PCF", "via": "N15"}],
    "steps": [
        {"at": 1, "action": "validate UE state", "actor": "UE",
         "message": "Deregistration Request", "direction": "in"},
        {"at": 2, "action": "release SM contexts"},
        {"at": 3, "action": "terminate AM policy association", "actor": "PCF",
         "message": "Npcf_AMPolicyControl_Delete", "direction": "out"},
        {"at": 4, "action": "release UE context", "actor": "gNB",
         "message": "UE Context Release Command", "direction": "out"}],
    "alternatives": [{"at": 3, "condition": "UE context not found",
                      "result": "reject and log the request"}],
    "state_changes": [
        {"entity": "RM", "from": "RM-REGISTERED", "to": "RM-DEREGISTERED", "at": 2,
         "evidence": "SetRegState"},
        {"entity": "RM", "from": "RM-REGISTERED", "to": "WEIRD", "evidence": "SetRegState"},
        {"entity": "CM", "from": "CM-CONNECTED", "to": "CM-IDLE", "evidence": "NotInCode"}],
    "outcome": "UE deregistered", "implements": ["UE-initiated deregistration"],
    "category": "network procedure", "confidence": "high"}
SEQ_PATH = ["amf/nas::RecvDeregReq", "amf/sm::SetRegState",
            "amf/policy::CallMainPCF", "amf/ngap::SendUeCxtRelCmd"]


def seq_traj(**kw) -> Trajectory:
    t = make_traj("T001", [5, 2, 0, 1], roots=["amf/nas::RecvDeregReq"], support=20, own=10,
                  walks=[20, 3, 18, 15], path=SEQ_PATH, **kw)
    t.step_functions = [[(n, 4)] for n in SEQ_PATH]
    return t


def edge_labels(src: str) -> list[str]:
    return re.findall(r'\|"([^"]*)"\|', src)


# ---- label safety --------------------------------------------------------------------------

@pytest.mark.parametrize("ctx,ch,enc", [
    ("quoted", "#", "#35;"), ("quoted", '"', "#quot;"), ("quoted", "<", "#lt;"),
    ("quoted", ">", "#gt;"), ("quoted", ";", ";"), ("quoted", ":", ":"),
    ("seq", "#", "＃"), ("seq", ";", ","), ("seq", "<", "‹"), ("seq", ">", "›"),
    ("seq", '"', '"'), ("seq", ":", ":"),
    ("colon", ":", "#58;"), ("colon", ";", "#59;"), ("colon", "#", "#35;"),
])
def test_mm_encodes_each_entity_per_context(ctx, ch, enc):
    assert G.mm(f"a{ch}b", ctx) == f"a{enc}b"


def test_mm_and_clip():
    assert G.mm("a#59;b", "quoted") == "a#35;59;b"        # one pass, no double encoding
    # sequence text: look-alikes, never an entity (mermaid wraps inside its placeholders)
    assert G.mm("cause=<normal>; reject #9", "seq") == "cause=‹normal›, reject ＃9"
    assert not re.search(r"#\w+;", G.mm("a;b#c<d>e#59;", "seq"))
    assert G.mm("«system» 登録手順（UE）") == "«system» 登録手順（UE）"
    c = G.clip("x" * 100, 40)
    assert len(c) == 40 and c.endswith("…")
    cjk = G.clip("登録" * 40, 20)
    assert len(cjk) == 20 and cjk.endswith("…")
    assert G.clip("a\x00b\x1fc `d`") == "a b c 'd'"
    assert G.clip("alpha beta gamma delta", 12) == "alpha beta…"
    assert G.clip(G.clip("word " * 20, 30) + " more", 30).count("…") == 1


# ---- ids and builder -----------------------------------------------------------------------

def test_slug_and_idreg():
    assert G.slug("NG-RAN (gNB)") == "ng_ran_gnb"
    reg = G.IdReg()
    assert reg.get("a_", "NG-RAN (gNB)") == "a_ng_ran_gnb"
    assert reg.get("a_", "NG-RAN (gNB)") == "a_ng_ran_gnb"     # stable per name
    assert reg.get("a_", "ng ran gnb") == "a_ng_ran_gnb_2"      # collision
    assert reg.get("u_", "登録", 3) == "u_3"                     # cjk -> index form
    assert reg.get("", "end") != "end" and G.ID_RX.match(reg.get("", "end"))
    assert not reg.get("", "xray").startswith(("o", "x"))


def test_dia_check_catches_undeclared_and_unbalanced():
    d = G.Dia(["flowchart LR"])
    d.node("a_x", 'a_x["X"]')
    d.edge("a_x", "b_y", "a_x --> b_y")
    assert any("b_y" in e for e in d.check())
    s = G.Dia(["sequenceDiagram"])
    s.node("n_amf", "participant n_amf as AMF")
    s.edge("n_amf", "i_redis", "n_amf->>i_redis: GET")
    assert any("i_redis" in e for e in s.check())
    for block in (["break x"], ["alt x", "else y"], ["rect rgb(1,1,1)"], ["opt x", "end", "end"],
                  ["else y"]):
        b = G.Dia(["sequenceDiagram"])
        b.node("n_amf", "participant n_amf as AMF")
        for line in block:
            b.raw(line)
        assert b.check(), block
    q = G.Dia(["flowchart LR"])
    q.node("a", 'a["odd "quote"]')
    assert any("quotes" in e for e in q.check())
    bad = G.Dia(["flowchart LR"])
    bad.node("Bad-Id", 'Bad-Id["x"]')
    assert any("bad id" in e for e in bad.check())
    ls = G.Dia(["flowchart LR"])
    ls.node("a", 'a["a"]')
    ls.raw("linkStyle 0 stroke-width:2px")
    assert any("linkStyle" in e for e in ls.check())
    st = G.Dia(["stateDiagram-v2"])
    st.node("e0", 'state "RM" as e0 {')
    assert st.check()


def test_fence_guard():
    big = G.Dia(["flowchart LR"])
    big.node("a", 'a["' + "x" * 60_000 + '"]')
    assert G.fence(big)[0].startswith("> Diagram omitted:")
    many = G.Dia(["flowchart LR"])
    many.node("a", 'a["a"]')
    many.node("b", 'b["b"]')
    for _ in range(600):
        many.edge("a", "b", "a --> b")
    assert G.fence(many)[0].startswith("> Diagram omitted:")
    ok = G.Dia(["flowchart LR"])
    ok.node("a", 'a["a"]')
    assert G.fence(ok) == ["```mermaid", "flowchart LR", '  a["a"]', "```"]
    assert G.fence("") == [] and G.fence(None) == []


# ---- message verification ------------------------------------------------------------------

def test_verify_message():
    m, lv = one(SEQ_DATA, seq_traj())
    lv.ev_tokens = G.tokens("SendUeCxtRelCmd")
    assert G.verify_message("UE Context Release Command", lv)
    lv.ev_tokens = G.tokens("CallMainPCF")
    assert not G.verify_message("Npcf_AMPolicyControl_Update", lv)
    assert not G.verify_message("PCF", lv)
    assert not G.verify_message("", lv)


# ---- model ---------------------------------------------------------------------------------

def test_model_basics():
    m = synthetic()
    assert m.level == "capability family"
    assert set(m.all_deployables) >= {"AMF", "SMSF"}
    assert list(m.children) == [0, 1, 2]
    assert m.group_labels[0].startswith("Group 0: Component 0 · Component 1")
    assert all("LLM parent" not in v for v in m.group_labels.values())
    lv = m.logic("T001")
    assert lv.group == 0 and lv.deployables == ["AMF"]
    for lv in m.logics:
        rank = [0 if a.starts else 1 if a.evidenced else 2 for a in lv.actors]
        assert rank == sorted(rank)                     # starters, evidenced, listed
        for a in lv.actors:
            assert SELF.get(a.name) not in lv.deployables
            assert a.kind in {"nf", "system", "role", "colocated", "generic", "unknown"}
    kinds = {a.kind for lv in m.logics for a in lv.actors} | \
        {a.kind for lv in m.logics for a in lv.infra} | {lv.initiator.kind for lv in m.logics}
    assert kinds >= {"nf", "system", "role", "colocated", "generic", "unknown", "infra",
                     "timer", "various"}
    # the cycle c0 -> c2 -> c0 is recorded both ways, weighted by step_walks
    assert m.edges[(0, 2)] and m.edges[(2, 0)]


def test_old_json_defaults():
    t = make_traj("T009", [0, 2], old=True, support=7)
    m, lv = one({"title": "x", "actors": ["UE", "SMF"], "steps": ["a", "b"],
                 "trigger": "UE request"}, t, stored=True)
    assert lv.own_support == 7 and lv.step_walks == [7, 7]
    assert not lv.new_schema and lv.mapped
    assert lv.flow == "activity"                       # never a sequence for older json
    assert not any(a.evidenced for a in lv.actors)


# ---- D1 ------------------------------------------------------------------------------------

def test_context_diagram():
    m = synthetic()
    d, caption = G.context_diagram(m)
    assert d.check() == []
    src = d.src()
    ext = [x for x in re.findall(r"^\s*(a_\w+)[\[@]", src, re.M) if x != "a_other"]
    assert len(ext) <= 10
    assert "a_other" in src and src.count("a_other -.- sys") == 1
    for lab in edge_labels(src):
        assert re.match(r"^.+ · \d+$", lab), lab
    assert '[("Key-value store")]' in src
    # code-evidenced edges (e.g. amf/sm on the path -> SMF) are the 3.5px ones
    coded = {d.reg.get("n_", SELF[a.name]) if a.kind == "colocated" else d.reg.get("a_", a.name)
             for lv in m.logics for a in lv.actors if a.code_evidence}
    styled = re.search(r"linkStyle ([\d,]+) stroke-width:3.5px", src).group(1).split(",")
    assert styled and all(set(d.edges[int(i)]) & coded for i in styled)
    assert d.reg.get("a_", "SMF") in {x for i in styled for x in d.edges[int(i)]}
    assert "Boxes inside the boundary" in caption
    # no deployable of a logic is drawn as its external actor
    boxes = set(re.findall(r"^\s*(n_\w+)\[", src, re.M))
    assert {"n_amf", "n_smsf"} <= boxes
    assert "a_amf" not in src and "a_smsf[" not in src


def test_context_diagram_without_actors():
    t = make_traj("T001", [0, 2])
    m, _ = one({"title": "x", "actors": []}, t)
    d, caption = G.context_diagram(m)
    assert d.check() == [] and "No external party" in caption


# ---- D2 and the procedure index ------------------------------------------------------------

def test_procedure_usecases():
    m = synthetic(k=20)
    d, lines = G.procedure_usecases(m)
    assert d.check() == []
    src = d.src()
    ovals = re.findall(r"^\s*(g_\w+)\(\[", src, re.M)
    assert ovals and len([o for o in ovals if o != "g_more"]) <= 8 and "g_more" in ovals
    listed_only = {a.name for lv in m.logics for a in lv.actors} - \
        {a.name for lv in m.logics for a in lv.actors if a.evidenced}
    assert listed_only
    ends = {x for e in d.edges for x in e}
    for name in listed_only:
        assert d.reg.get("a_", name) not in ends or name == "_other"
    assert lines[0] == G.D2_CAPTION
    assert any(r.startswith("| procedure | reference |") for r in lines)
    assert any("(bizlogic-k20-T" in r for r in lines)
    # no implements anywhere -> no D2
    t = make_traj("T001", [0, 2])
    m2, _ = one({"title": "x", "actors": ["UE"]}, t)
    assert G.procedure_usecases(m2) == (None, [])


# ---- matrix --------------------------------------------------------------------------------

def test_actor_matrix():
    m = synthetic()
    rows = G.actor_matrix(m)
    head = rows[0]
    assert "[G0](bizlogic-k6-p0.md)" in head and "[G2](bizlogic-k6-p2.md)" in head
    body = [r for r in rows[2:] if r.startswith("| ")]
    assert 1 <= len(body) <= 10
    for r in body:
        assert r.count("|") == head.count("|")
    assert any(re.search(r"\| \d+ \(\d+\) \|", r) or "(" in r for r in body)
    assert "- G1 = Group 1: Component 2 · Component 3" in rows


# ---- D3 ------------------------------------------------------------------------------------

def test_component_view():
    m = synthetic()
    d, legend = G.component_view(m)
    assert d.check() == []
    src = d.src()
    for c in m.clusters:
        assert len(re.findall(rf"^\s*c{c}\[", src, re.M)) == 1
    assert "amf/ngap, amf/ran" in src
    assert re.search(r"^\s*class [\w,]*\bc1\b[\w,]* mixed$", src, re.M)   # c1: amf+smsf+nrf
    assert "· mixed" in src and "~~~" in src
    assert src.count("direction TB") == len(m.children)    # no flipped one-row groups
    assert "Colour = component kind" in legend
    m6 = synthetic(n_groups=6)
    d6, _ = G.component_view(m6)
    assert d6.check() == [] and "p0 ~~~ p4" in d6.src()
    table = G.component_table(m)
    assert table[0].startswith("| group | component |")
    assert "[Component 0](bizlogic-k6-c0.md)" in table[2]


# ---- D4 / D8 / D13 -------------------------------------------------------------------------

def test_call_map_caps_and_weights():
    cl, parents, pinfo = clusters_and_parents()
    many = {c: ClusterInfo(id=c, size=10 + c, packages=[("amf/x", 1)], central=[],
                           name=f"K{c}") for c in range(20)}
    trajs = [make_traj(f"T{i:03d}", [i % 20, (i * 7 + 3) % 20, (i * 11 + 5) % 20],
                       support=10, own=4, walks=[10, 8, 3]) for i in range(60)]
    m = G.build_model(20, many, {c: c % 3 for c in many}, pinfo, trajs, {}, "x")
    d, caption = G.call_map(m)
    assert d.check() == [] and len(d.edges) <= 25
    for line in re.findall(r"linkStyle ([\d,]+)", d.src()):
        assert all(int(i) < len(d.edges) for i in line.split(","))
    assert "Showing 25 of" in caption
    t = make_traj("T001", [0, 1], support=10, own=7, walks=[10, 7])
    m1, _ = one({"title": "x"}, t)
    d1, _ = G.call_map(m1)
    assert edge_labels(d1.src()) == ["7"]
    d8, _ = G.call_map(m, 0)
    assert d8.check() == [] and "ext_p1" in d8.src()


def test_call_map_and_ego_missing():
    t = make_traj("T001", [0])
    m, _ = one({"title": "x"}, t)
    assert G.call_map(m) == ("", G.NO_CALLS)
    assert G.call_map(m, 0) == ("", G.NO_CALLS)
    assert G.ego_map(m, 0) == ("", G.NO_EGO_CALLS)
    m2 = synthetic()
    d, _ = G.ego_map(m2, 2)
    assert d.check() == [] and ":::focus" in d.src()


# ---- D7 / D9 -------------------------------------------------------------------------------

def test_usecase_group():
    m = synthetic(k=20)
    for pid in m.children:
        d, caption, internal = G.usecase_group(m, pid)
        if d is None:
            assert caption == G.NO_OUTSIDE
            continue
        assert d.check() == []
        src = d.src()
        ovals = re.findall(r"^\s*(u_t\w+)\(\[", src, re.M)
        assert 1 <= len(ovals) <= 8
        actors = [x for x in re.findall(r"^\s*(a_\w+)[\[@]", src, re.M) if x != "a_other"]
        assert len(actors) <= 6
        assert len(d.edges) <= 30
        others = [e for e in d.edges if "a_other" in e]
        assert len(others) <= 1
        if others:
            assert others[0][1].startswith("sys_")
        assert re.search(r'subgraph sys_\w+ \["«system» ', src)
        assert "Solid line" in caption
    groups = {lv.group for lv in m.logics if lv.tid == "T030"}
    _, _, internal = G.usecase_group(m, groups.pop())
    assert any("T030" in x for x in internal)
    assert all("u_t030" not in G.usecase_group(m, p)[0].src()
               for p in m.children if G.usecase_group(m, p)[0])


def test_usecase_group_old_json_is_dashed():
    m = synthetic(k=20)
    olds = {lv.tid for lv in m.logics if not lv.new_schema and lv.actors}
    seen = 0
    for pid in m.children:
        d, _, _ = G.usecase_group(m, pid)
        if not d:
            continue
        old_ids = {d.reg.get("u_", t) for t in olds}
        for line in d.body:
            e = re.match(r"^(\w+) (---|-\.-) (\w+)$", line)
            if e and {e.group(1), e.group(3)} & old_ids and \
                    any(x.startswith("a_") for x in (e.group(1), e.group(3))):
                seen += 1
                assert e.group(2) == "-.-", line
    assert seen


def test_participants_conditional():
    m, lv = one(SEQ_DATA, seq_traj())
    assert lv.flow == "sequence"
    assert G.participants(m, lv) == ("", "")
    data = dict(SEQ_DATA, steps=[{"at": 1, "action": "check"}],
                actors=[{"name": "UE"}, {"name": "SMF"}, {"name": "PCF"}])
    m2, lv2 = one(data, seq_traj())
    assert lv2.flow == "activity"
    d, caption = G.participants(m2, lv2)
    assert d.check() == [] and "u_t001" in d.src() and "«system» AMF" in d.src()
    m3, lv3 = one(dict(data, actors=[{"name": "UE"}, {"name": "SMF"}]), seq_traj())
    assert G.participants(m3, lv3) == ("", "")
    timer = dict(data, trigger_sources=["timer"], trigger="T3512 expiry")
    m4, lv4 = one(timer, seq_traj())
    d4, _ = G.participants(m4, lv4)
    assert "a_timer" in d4.src() and "«timer»" in d4.src()


# ---- D10 -----------------------------------------------------------------------------------

def test_sequence():
    m, lv = one(SEQ_DATA, seq_traj())
    d, caption = G.flow_diagram(m, lv)
    assert d.check() == []
    src = d.src()
    assert src.splitlines()[1] == "sequenceDiagram"
    assert "autonumber" not in src and "\n  box" not in src
    parts = re.findall(r"^\s*(participant|actor) (\S+) as", src, re.M)
    assert len(parts) <= 5
    assert not any(re.match(r"^c\d+$", p) for _, p in parts)
    assert ("participant", "n_amf") in parts
    for i in range(1, 5):
        assert len(re.findall(rf"\[{i}\] ", src)) == 1, i
    assert "break UE context not found" in src
    assert "opt optional: reached by 3 of 20 walks" in src
    assert "opt UE" not in src
    assert "n_amf-->>a_pcf: Npcf_AMPolicyControl_Delete?" in src     # unverified: dashed ?
    assert "n_amf->>a_ng_ran_gnb: UE Context Release Command" in src  # verified: solid
    assert "a_ue->>+n_amf: N1 NAS: Deregistration Request" in src
    assert "Note over a_ue,a_ng_ran_gnb: NAS carried in NGAP (N2)" in src
    assert src.count("->>+") == src.count("deactivate ") == 1
    assert "Outcome - UE deregistered" in src
    assert "confidence: " in caption and "break" in caption


def test_sequence_with_several_starters_uses_alt():
    m, lv = one(dict(SEQ_DATA, trigger_sources=["UE", "timer"]), seq_traj())
    assert lv.initiator.kind == "various"
    d, _ = G.sequence(m, lv)
    src = d.src()
    assert d.check() == []
    assert re.search(r"^\s*alt ", src, re.M) and re.search(r"^\s*else Timer", src, re.M)
    assert "participant tm as Timer" in src
    assert src.count("->>+") == 1 and src.count("deactivate ") == 1


def test_sequence_process_start_and_lifeline_cap():
    data = dict(SEQ_DATA, trigger_sources=["process start"], trigger="AMF process start",
                actors=[{"name": x} for x in ("UE", "gNB", "PCF", "SMF", "UDM", "AUSF")],
                steps=[{"at": 1, "action": "a", "actor": x, "direction": "out"}
                       for x in ("UE", "gNB", "PCF", "SMF", "UDM", "AUSF")]
                + [{"at": 2, "action": "store", "actor": "Redis", "direction": "out"}])
    m, lv = one(data, seq_traj())
    d, _ = G.sequence(m, lv)
    src = d.src()
    assert d.check() == []
    assert len(re.findall(r"^\s*(participant|actor) ", src, re.M)) <= 5
    assert "activate n_amf" in src and "Started at process start" in src
    assert "Note over n_amf: a (with " in src       # cut parties become notes


def test_activity():
    data = {"title": "Switch NRF instance", "scope": "single", "trigger": "Maintenance command",
            "trigger_sources": ["operator"], "actors": [{"name": "operator"}],
            "steps": [{"at": 1, "action": "validate pair state"},
                      {"at": 2, "action": "switch active instance"}],
            "alternatives": [{"at": 1, "condition": "peer unreachable",
                              "result": "mark pair degraded"}],
            "confidence": "medium"}
    t = make_traj("T002", [4, 1], roots=["nrf/ha::Main"], support=20, own=12,
                  walks=[20, 12], variants=[([4, 3], 5)])
    m, lv = one(data, t)
    assert lv.flow == "activity" and lv.deployables == ["NRF"]
    d, caption = G.flow_diagram(m, lv)
    assert d.check() == []
    src = d.src()
    assert src.splitlines()[1] == "swimlane-beta TB"
    assert src.count("sm-circ") == 1 and src.count("fr-circ") == 2       # 1 alternative + en
    assert re.search(r'v1\["variant: [^"]*<br>5 walks"\]:::variant', src)
    assert re.search(r"^\s*s1 -\.-> v1$", src, re.M)
    titles = re.findall(r'subgraph l_\w+ \["([^"]*)"\]', src)
    # short lane titles (one line in the header band); the starters are a box in the lane
    assert titles[0] == "Initiator" and "NRF" in titles
    assert 'who["Operator / OAM"]:::who' in src and "who ~~~ st" in src
    assert all(len(x) <= 16 for x in titles)
    assert not any(t.startswith("Group ") for t in titles)
    assert "Lanes = the initiator, then the deployables" in caption
    old = G.USE_SWIMLANE_BETA
    try:
        G.USE_SWIMLANE_BETA = False
        assert G.flow_diagram(m, lv)[0].src().splitlines()[1] == "flowchart TB"
    finally:
        G.USE_SWIMLANE_BETA = old


def test_family_view():
    m = synthetic(k=6)
    fam = [lv for lv in m.logics if len(lv.t.roots) > 1 or lv.t.variants
           or len(lv.bl.implements) + len(lv.bl.supports) >= 2]
    assert fam and {lv.flow for lv in fam} == {"family"}
    for lv in fam:
        d, caption = G.flow_diagram(m, lv)
        assert "sequenceDiagram" not in d.src() and "r1[" not in d.src()
        assert caption == G.FAMILY_CAPTION
    # one entry root, one path, no variants: a single procedure even at capability level
    single = [lv for lv in m.logics if lv not in fam]
    assert single and all(lv.flow != "family" for lv in single)
    assert all(G.flow_diagram(m, lv)[1] != G.FAMILY_CAPTION for lv in single)
    m2, lv2 = one(dict(SEQ_DATA, scope="family"), seq_traj())
    assert lv2.flow == "family"
    lines = G.variants_lines(m2, lv2)
    assert lines == [] or lines[-1].startswith("Observed in walks")
    m3, lv3 = one(dict(SEQ_DATA, variants=["MO", "MT"]),
                  seq_traj(variants=[([5, 3], 4)]))
    assert G.variants_lines(m3, lv3)[:2] == ["- MO", "- MT"]
    assert G.variants_lines(m3, lv3)[-1] == "Observed in walks: Component 5 → Component 3 (4 walks)"


def test_unmapped_steps_never_draw_a_sequence():
    data = dict(SEQ_DATA, steps=[{"action": s["action"], "actor": s.get("actor", ""),
                                  "message": s.get("message", ""),
                                  "direction": s.get("direction", "internal")}
                                 for s in SEQ_DATA["steps"]])
    m, lv = one(data, seq_traj())
    assert not lv.mapped and lv.flow == "activity"
    src = G.flow_diagram(m, lv)[0].src()
    assert "Component 5" in src and "sequenceDiagram" not in src


# ---- D11 -----------------------------------------------------------------------------------

def test_state_logic():
    m, lv = one(SEQ_DATA, seq_traj())
    d, mentioned = G.state_logic(lv)
    assert d.check() == []
    src = d.src()
    assert '"RM-REGISTERED"' in src and '"RM-DEREGISTERED"' in src
    assert "WEIRD" not in src and "CM-IDLE" not in src
    assert "WEIRD" in mentioned and "CM: CM-CONNECTED → CM-IDLE" in mentioned
    assert mentioned.startswith("Mentioned state changes (no code evidence): ")
    for line in src.splitlines():
        if "-->" in line:
            assert line.count(":") == 1 and "step 2 (SetRegState)" in line
    assert G.mm("a: b", "colon") == "a#58; b"
    m2, lv2 = one(dict(SEQ_DATA, state_changes=[]), seq_traj())
    assert G.state_logic(lv2) == ("", "")


# ---- related -------------------------------------------------------------------------------

def test_related():
    m = synthetic(k=20)
    lv = m.logic("T004")                                # path [0, 5, 2, 3]
    rel = G.related(m, lv)
    assert 0 < len(rel) <= 5
    assert all(o is not lv for o, _ in rel)
    assert any("component path" in r or "shares procedure" in r for _, r in rel)


# ---- compressed evidence -------------------------------------------------------------------

def test_where_in_code():
    t = make_traj("T007", [0, 2], roots=[
        "amf/comm::NewRetryTable", "amf/comm::FooTable",
        "amf/n26::FrameEncode", "amf/n26::Unmarshal", "amf/n26::GetX", "amf/n26::AddErrMsg",
        "vendor/diameter/diam::Serve", "amf/comm::HandleN1N2Transfer",
        "amf/n26::RecvRelocationRequest", "amf/sm::Recv", "amf/policy::Recv"])
    t.root_counts = [(r, 5) for r in t.roots]
    t.step_functions = [[("vendor/diameter/diam::Serve", 99), ("amf/n26::Unmarshal", 50),
                         ("amf/sm::UpdateSession", 7)], [("amf/policy::Create", 3)]]
    t.subsumed = 2
    out = G.where_in_code(t)
    assert len(out) == 3
    assert out[0].startswith("- **Main packages:** `amf/sm`, `amf/policy`")
    assert "vendor/" not in out[0] and "amf/n26" not in out[0]
    assert out[1].startswith("- **Entry points:** 11 roots — `amf/n26` 5, "
                             "`amf/comm` 3, `amf/policy` 1, +2 more; e.g. ")
    assert "comm.HandleN1N2Transfer" in out[1]
    for noise in ("NewRetryTable", "FooTable", "FrameEncode", "Unmarshal", "GetX",
                  "AddErrMsg", "vendor/"):
        assert noise not in out[1]
    assert out[2].startswith("- Based on 10 walks following the whole path (20 including")
    assert out[2].endswith("Also stands for 2 shorter trajectories.")
    text = "\n".join(out)
    assert "Key functions" not in text and "call chain" not in text.lower()
    old = make_traj("T008", [0], old=True, support=9)
    assert G.where_in_code(old)[-1].startswith("- Based on 9 walks from 1 entry roots.")
    noisy = make_traj("T009", [0], roots=["amf/x::GetY"])
    noisy.root_counts, noisy.step_functions = [], [[("amf/x::GetY", 1)]]
    assert "`x.GetY`" in G.where_in_code(noisy)[1] and "`amf/x`" in G.where_in_code(noisy)[0]


# ---- markdown safety -----------------------------------------------------------------------

def test_markdown_cells_and_links_are_safe():
    m = synthetic(k=20)
    m.clusters[0].name = "Bad | name ] here ["
    row = G.component_table(m)[2]
    assert row.count("|") == 7
    assert "[Bad / name ) here (](bizlogic-k20-c0.md)" in row
    for pid in m.children:
        for line in G.usecase_group(m, pid)[2]:
            assert re.match(r"^- \[[^\[\]]*\]\(bizlogic-k20-T\d+\.md\)$", line), line


# ---- every synthetic diagram passes its check ----------------------------------------------

def collect(m) -> list[tuple[str, object]]:
    out = [("D1", G.context_diagram(m)[0]), ("D2", G.procedure_usecases(m)[0]),
           ("D3", G.component_view(m)[0]), ("D4", G.call_map(m)[0])]
    for p in m.children:
        out += [(f"D7 p{p}", G.usecase_group(m, p)[0]), (f"D8 p{p}", G.call_map(m, p)[0])]
    for c in m.clusters:
        out.append((f"D13 c{c}", G.ego_map(m, c)[0]))
    for lv in m.logics:
        out += [(f"D9 {lv.tid}", G.participants(m, lv)[0]),
                (f"D10 {lv.tid} {lv.flow}", G.flow_diagram(m, lv)[0]),
                (f"D11 {lv.tid}", G.state_logic(lv)[0])]
    return [(n, d) for n, d in out if d]


def synthetic_diagrams() -> list[tuple[str, object]]:
    out = collect(synthetic(6)) + collect(synthetic(20)) + collect(synthetic(6, n_groups=6))
    for data in (SEQ_DATA, dict(SEQ_DATA, trigger_sources=["UE", "timer"])):
        m, lv = one(data, seq_traj())
        out += [(f"seq {lv.initiator.name}", G.sequence(m, lv)[0]),
                ("activity", G.activity(m, lv)[0]), ("state", G.state_logic(lv)[0])]
    old = G.USE_SWIMLANE_BETA
    try:
        G.USE_SWIMLANE_BETA = False
        m, lv = one(SEQ_DATA, seq_traj())
        out.append(("activity flowchart", G.activity(m, lv)[0]))
    finally:
        G.USE_SWIMLANE_BETA = old
    return out


def test_all_synthetic_diagrams_check():
    ds = synthetic_diagrams()
    kinds = {n.split()[0] for n, _ in ds}
    assert kinds >= {"D1", "D2", "D3", "D4", "D7", "D8", "D9", "D10", "D11", "D13"}
    for name, d in ds:
        assert d.check() == [], (name, d.check())
        assert "```" not in d.src()


def test_default_profile_diagrams_are_domain_neutral():
    D.use_profile(None)                         # the autouse fixture restores the profile
    m = synthetic(k=20)
    assert set(m.all_deployables) == {"amf", "smsf"}               # top directories
    assert not any(lv.bl.implements or lv.bl.supports for lv in m.logics)
    assert G.procedure_usecases(m) == (None, [])
    ds = collect(m)
    for name, d in ds:
        assert d.check() == [], (name, d.check())
    m1, lv = one(SEQ_DATA, seq_traj())
    src = G.flow_diagram(m1, lv)[0].src()
    assert "sequenceDiagram" in src and "n_amf" in src
    assert "NAS carried" not in src and "N1 NAS:" not in src
    joined = "\n".join(d.src() for _, d in ds) + src
    assert "Other 5GC NFs" not in joined and "NG-RAN" not in joined
    caption = G.context_diagram(m)[1]
    assert "e.g." not in caption


# ---- browser render check (slow) -----------------------------------------------------------

def _render_chunk(srcs: list[str]) -> list[dict]:
    js = BUNDLE.read_text()
    data = json.dumps(srcs).replace("<", "\\u003c")
    page = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<pre id='results'>PENDING</pre><script>" + js + "</script><script>"
            "const S=" + data + ";"
            "mermaid.initialize({startOnLoad:false,theme:'default'});"
            "(async()=>{const out=[];for(let i=0;i<S.length;i++){"
            "try{await mermaid.parse(S[i]);const r=await mermaid.render('d'+i,S[i]);"
            "const bad=/Syntax error in text|Maximum text size/.test(r.svg);"
            "out.push({ok:!bad,err:bad?'error svg':''});"
            "}catch(e){out.push({ok:false,err:String(e&&e.message||e).slice(0,300)});}}"
            "document.getElementById('results').textContent=JSON.stringify(out);})();"
            "</script></body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "page.html"
        f.write_text(page, encoding="utf-8")
        dom = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
                              f"--user-data-dir={tmp}/prof", "--virtual-time-budget=120000",
                              "--dump-dom", f.as_uri()],
                             capture_output=True, timeout=900).stdout.decode()
    m = re.search(r'<pre id="results">(.*?)</pre>', dom, re.S)
    raw = html.unescape(m.group(1)) if m else ""
    assert raw.startswith("["), f"no render results: {raw[:200]!r}"
    return json.loads(raw)


def render_all(srcs: list[str], chunk: int = 25) -> list[dict]:
    parts = [srcs[i:i + chunk] for i in range(0, len(srcs), chunk)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [r for rs in pool.map(_render_chunk, parts) for r in rs]


@pytest.mark.slow
def test_diagrams_render_in_mermaid_11():
    """every diagram these tests generate parses and renders with the bundled mermaid."""
    if not CHROME.exists() or not BUNDLE.is_file():
        pytest.skip("chrome or ~/.repowiki/mermaid.min.js missing")
    ds = synthetic_diagrams()
    srcs = list(dict.fromkeys(d.src() for _, d in ds))
    joined = "\n".join(srcs)
    for construct in ("swimlane-beta TB", "sm-circ", "fr-circ", "~~~", "break ", "opt ",
                      "alt ", "else ", "->>+", "deactivate ", "%%{init:", "stateDiagram-v2",
                      "img: \"data:image/svg+xml;base64,", "a_other -.- sys_", " mixed"):
        assert construct in joined, construct
    assert re.search(r"^\s*p0 ~~~ p4$", joined, re.M)
    res = render_all(srcs)
    assert len(res) == len(srcs)
    bad = [(s.splitlines()[:3], r["err"]) for s, r in zip(srcs, res) if not r["ok"]]
    hint = " (if swimlane shapes fail, set USE_SWIMLANE_BETA = False)" \
        if any("swimlane" in "\n".join(s) for s, _ in bad) else ""
    assert not bad, f"{len(bad)} diagrams failed to render{hint}: {bad[:3]}"
