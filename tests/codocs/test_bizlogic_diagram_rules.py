"""diagram rules of the bizlogic wiki: label safety in sequences, lane titles, layout order,
reciprocal edges, edge caps, evidence per deployable, wildcard interfaces, message and
state verification, use-case colocation, captions, labels and the exported html."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
import test_bizlogic_diagrams as T

from codocs.bizlogic import actors as A
from codocs.bizlogic import diagrams as G
from codocs.bizlogic import domain as D
from codocs.bizlogic.domain import interface
from codocs.bizlogic.infer import BusinessLogic, ClusterInfo, parse_logic
from codocs.bizlogic.walk import Trajectory


@pytest.fixture(autouse=True)
def gpp():
    """the example profile, the synthetic package evidence of test_bizlogic_diagrams and a
    few synthetic deployables (a cli, a shared core with an sbi client, k8s controllers)."""
    base = T.gpp_profile()
    deps = {**base.deployables, "cli": "Maintenance CLI", "core": "Core platform",
            "core/sbi": "SBI client (shared)"}
    ev = base.pkg_evidence + (
        ("amf/communication", "Namf_Communication", frozenset({"Peer AMF", "SMF", "SMSF"})),
        ("core/sbi/discovery", "Nnrf", frozenset({"NRF"})))
    prev = D.use_profile(replace(base, deployables=deps, pkg_evidence=ev,
                                 controller_roots=("operators",)))
    yield
    D.use_profile(prev)


def _model(cl: dict[int, ClusterInfo], parents: dict[int, int], trajs: list[Trajectory],
           logic: dict[str, BusinessLogic], k: int = 20) -> G.Model:
    pinfo = {p: ClusterInfo(id=p, size=0, packages=[], central=[],
                            children=[c for c, q in parents.items() if q == p])
             for p in set(parents.values())}
    return G.build_model(k, cl, parents, pinfo, trajs, logic, "demo")


def _ci(c: int, pkgs, name: str = "", kind: str = "", size: int = 10) -> ClusterInfo:
    return ClusterInfo(id=c, size=size, packages=list(pkgs), central=[], name=name, kind=kind)


def _traj(tid: str, clusters: list[int], roots: list[str], path: list[str] | None = None,
          sf: list[list[tuple[str, int]]] | None = None, variants=()) -> Trajectory:
    path = path or roots
    sf = sf or [[(path[min(i, len(path) - 1)], 3)] for i in range(len(clusters))]
    return Trajectory(id=tid, clusters=clusters, path=path, support=10, roots=roots,
                      own_support=8, step_walks=[10] + [8] * (len(clusters) - 1),
                      variants=list(variants), root_counts=[(r, 3) for r in roots],
                      step_functions=sf)


def _old(tid: str, n: int, actors: list[str], trigger: str = "request") -> BusinessLogic:
    return BusinessLogic.from_dict({"title": f"Old {tid}", "trigger": trigger, "actors": actors,
                                    "steps": [f"step {j}" for j in range(n)],
                                    "confidence": "low"}, tid, n)


def _stack_ok(src: str) -> bool:
    """no "break" sits inside an "opt" (it would end only the opt)."""
    stack: list[str] = []
    for line in src.splitlines():
        w = line.split(maxsplit=1)[0] if line.strip() else ""
        if w == "break" and "opt" in stack:
            return False
        if w in ("opt", "alt", "break", "loop", "rect", "critical", "par"):
            stack.append(w)
        elif w == "end" and stack:
            stack.pop()
    return True


HOSTILE = dict(
    T.SEQ_DATA, trigger="UE sends <Deregistration Request>; cause #9",
    steps=[{"at": 1, "action": "validate; check <state> #1", "actor": "UE",
            "message": "Npcf_AMPolicyControl_Create; Npcf_AMPolicyControl_UpdateNotify",
            "direction": "in"},
           {"at": 2, "action": "release UE context; cause=<normal> #7"},
           {"at": 3, "action": "terminate AM policy; T3512 #9", "actor": "PCF",
            "message": "Npcf_AMPolicyControl_Delete; #59; <y>", "direction": "out"},
           {"at": 4, "action": "release UE context", "actor": "gNB",
            "message": "UE Context Release Command", "direction": "out"}],
    alternatives=[{"at": 2, "condition": "UE context not found; reject with 5GMM cause #9",
                   "result": "reject; cause #9 <abnormal>"}],
    outcome="UE deregistered; context #1 <gone>")


def hostile_sequence() -> str:
    m, lv = T.one(HOSTILE, T.seq_traj())
    return G.sequence(m, lv)[0].src()


# ---- sequence text never carries an entity ---------------------------------------------

def test_sequence_text_uses_look_alikes_not_entities():
    src = hostile_sequence()
    body = src.split("\n", 1)[1]
    assert not re.search(r"#\w+;", body), body
    assert "cause=‹normal› ＃7" in body and "context, cause" in body
    assert "break UE context not found, reject" in body


# ---- short lane titles; the starters are a box in the initiator lane --------------------

def test_activity_lane_titles_fit_the_header():
    data = {"title": "x", "trigger": "UE or signalling", "trigger_sources": ["UE", "MME"],
            "actors": [{"name": "UE"}, {"name": "MME"}],
            "steps": [{"at": 1, "action": "do it"}]}
    t = _traj("T001", [0], ["core/sbi/discovery::Discover"])
    cl = {0: _ci(0, [("core/sbi/discovery", 5)], "Discovery")}
    bl = parse_logic(data, "T001", 1, stored=False)
    m = _model(cl, {0: 0}, [t], {"T001": bl})
    src = G.activity(m, m.logics[0])[0].src()
    titles = re.findall(r'subgraph l_\w+ \["([^"]*)"\]', src)
    assert titles == ["Initiator", "SBI client"]
    assert re.search(r'who\["UE \(subscriber device\)<br>MME \(EPC\)"\]:::who', src)
    assert "(this code)" not in src
    assert G.lane_name("NRF (service impl.)") == "NRF"
    assert G.lane_name("Kubernetes operator (cluster)") == "Kubernetes operator"


# ---- D3 group 0 is declared last (dagre puts it left-most) -------------------------------

def test_component_view_declares_groups_in_reverse():
    src = G.component_view(T.synthetic())[0].src()
    pos = [src.index(f"subgraph p{p} ") for p in (0, 1, 2)]
    assert pos[0] > pos[1] > pos[2]


# ---- reciprocal call edges are one two-headed edge ---------------------------------------

def test_reciprocal_call_edges_are_merged():
    cl = {c: _ci(c, [("amf/x", 1)], f"K{c}") for c in range(3)}
    trajs = [_traj("T001", [0, 1, 0], ["amf/x::A"]), _traj("T002", [1, 0], ["amf/x::B"]),
             _traj("T003", [1, 0, 2], ["amf/x::C"])]
    m = _model(cl, {0: 0, 1: 0, 2: 1}, trajs, {})
    assert m.edges[(0, 1)] and m.edges[(1, 0)]
    hi, lo = max(m.edges[(0, 1)], m.edges[(1, 0)]), min(m.edges[(0, 1)], m.edges[(1, 0)])
    for d in (G.call_map(m)[0], G.call_map(m, 0)[0], G.ego_map(m, 0)[0]):
        assert d.check() == []
        src = d.src()
        assert f'<-->|"{hi} / {lo}"|' in src, src
        pairs = [tuple(sorted(e)) for e in d.edges]
        assert len(pairs) == len(set(pairs)), pairs        # no parallel edges
    assert "two-headed arrow" in G.CALLMAP_CAPTION


# ---- dashed associations are capped; D1 drops weak dashed edges --------------------------

def test_dashed_use_case_associations_are_capped():
    cl = {0: _ci(0, [("amf/ngap", 5)], "Ngap")}
    many = ["UE", "SMF", "PCF", "UDM", "AUSF", "NSSF", "CHF"]
    trajs = [_traj(f"T{i:03d}", [0], [f"amf/ngap::R{i}"]) for i in range(1, 9)]
    logic = {t.id: _old(t.id, 1, many) for t in trajs}
    m = _model(cl, {0: 0}, trajs, logic)
    d, _, _ = G.usecase_group(m, 0)
    assert d.check() == []
    assoc = [ln for ln in d.body if re.match(r"^u_t\w+ -\.- a_(?!other)|^a_\w+ -\.- u_t", ln)]
    assert len(assoc) <= G.MAX_DASHED_ASSOC
    per = {}
    for ln in assoc:
        u = re.search(r"u_t\d+", ln).group(0)
        per[u] = per.get(u, 0) + 1
    assert per and max(per.values()) <= G.MAX_DASHED_PER_OVAL
    assert len(per) == len(re.findall(r"^u_t\w+\(\[", "\n".join(d.body), re.M))   # none bare
    assert "a_other" in d.src()


def test_d1_dashed_edges_need_two_logics_and_an_interface():
    m = T.synthetic(k=20)
    d, _ = G.context_diagram(m)
    for line in d.body:
        e = re.match(r'^\w+ -\.->\|"([^"]*) · (\d+)"\| \w+$', line)
        if e:
            assert e.group(1) != "?" and int(e.group(2)) >= 2, line
    assert len([ln for ln in d.body if " -.->|" in ln]) <= G.MAX_D1_DASHED


# ---- plan-only D1 stacks the boxes and says nothing was inferred -------------------------

def test_plan_only_context_stacks_boxes_and_start_has_no_label():
    roots = ["amf/a::A", "smsf/b::B", "udm/c::C", "ausf/d::D", "nrf/e::E",
             "nssf/f::F", "cli/g::G", "pcf/h::H", "gw/i::I"]
    cl = {0: _ci(0, [("amf/a", 5)])}
    trajs = [_traj(f"T{i:03d}", [0], [r]) for i, r in enumerate(roots, start=1)]
    m = _model(cl, {0: 0}, trajs, {})
    d, caption = G.context_diagram(m)
    assert d.check() == [] and not m.inferred
    ids = re.findall(r"^(n_\w+)\[", "\n".join(d.body), re.M)
    assert len(ids) == 9
    for a, b in zip(ids, ids[4:]):
        assert f"{a} ~~~ {b}" in d.body
    assert caption == "No business logic was inferred, so no outside party is shown."
    src = G.activity(m, m.logics[0])[0].src()
    assert "st --> s1" in src and "Process start" not in src
    data = {"title": "Boot", "trigger": "", "trigger_sources": ["process start"],
            "steps": [{"at": 1, "action": "load configuration"}]}
    m2, lv2 = T.one(data, T.make_traj("T001", [0]))
    assert lv2.initiator.kind == "process"
    assert 'st -->|"Process start"| s1' in G.activity(m2, lv2)[0].src()


# ---- unmapped steps show packages, not the name twice; variant counts -------------------

def test_unmapped_step_shows_packages_and_variant_count():
    t = T.make_traj("T001", [0, 2], variants=[([0, 3], 1)])
    cl, parents, pinfo = T.clusters_and_parents()
    m = G.build_model(20, cl, parents, pinfo, [t], {}, "x")
    src = G.activity(m, m.logics[0])[0].src()
    assert 's1["[Component 0]<br>amf/ngap, amf/ran"]' in src
    for tag, body in re.findall(r's\d+\["\[([^\]]*)\]<br>([^"]*)"\]', src):
        assert tag != body
    assert "variant: Component 3<br>1 walk" in src and "1 walks" not in src


# ---- code evidence belongs to the deployable that holds the package -----------------------

def test_code_evidence_is_per_deployable_and_skips_test_code():
    assert A.pkg_evidence(["amf/ngap/ut::main", "amf/ngap/ut/ut::Info"]) == {}
    assert "NG-RAN (gNB)" in A.pkg_evidence(["amf/ngap/handler::Recv"])
    cl = {0: _ci(0, [("cli/cmd", 10)], "CLI"), 1: _ci(1, [("amf/communication", 9)])}
    path = ["cli/cmd::Run", "amf/ngap/ut::main", "amf/communication/h::Send"]
    t = _traj("T001", [0, 1], ["cli/cmd::Run"], path=path)
    t2 = _traj("T002", [1], ["amf/communication/h::Recv"])
    logic = {"T001": _old("T001", 2, ["gNB", "SMSF", "operator"]),
             "T002": _old("T002", 1, ["SMF"])}
    m = _model(cl, {0: 0, 1: 0}, [t, t2], logic)
    lv = m.logic("T001")
    ran, smsf = lv.ref("NG-RAN (gNB)"), lv.ref("SMSF")
    assert lv.deployables == ["Maintenance CLI"]
    assert not ran.code_evidence                        # unit-test driver only
    assert smsf.code_evidence and smsf.code_dep == "AMF"
    d, _ = G.context_diagram(m)
    src = d.src()
    thick = re.search(r"linkStyle ([\d,]+) stroke-width:3.5px", src)
    idx = [int(i) for i in thick.group(1).split(",")] if thick else []
    edges = [ln for ln in d.body if re.match(r"^\w+ (-->|-\.->)\|", ln)]
    for i in idx:
        assert '"? ·' not in edges[i], edges[i]
    assert 'n_amf -->|"N20 (Nsmsf) · 1"| a_smsf' in edges       # from the box holding it
    assert not any("a_ng_ran_gnb" in ln and "n_maintenance_cli" in ln for ln in edges)


# ---- lanes come from this logic's step functions, inside "Runs in" ------------------------

def test_activity_lanes_stay_inside_runs_in():
    # the cluster's largest package is in gw, but most of it (and this step) is amf code
    info = _ci(1, [("gw/api/grpc", 51), ("amf/lcs", 50), ("amf/location", 45)], "Mixed")
    assert G.cluster_deployable(info) == "AMF"
    cl = {0: _ci(0, [("amf/nas", 5)], "Nas"), 1: info,
          2: _ci(2, [("core/utility", 30)], "Util")}
    sf = [[("amf/nas::Recv", 5)], [("amf/lcs::Send", 7), ("core/utility::X", 6)],
          [("core/utility::Y", 4)]]
    t = _traj("T009", [0, 1, 2], ["amf/nas::Recv"], sf=sf)
    data = {"title": "x", "trigger": "UE request", "trigger_sources": ["UE"],
            "actors": [{"name": "UE"}],
            "steps": [{"at": i, "action": f"step {i}"} for i in (1, 2, 3)]}
    m = _model(cl, {0: 0, 1: 0, 2: 0}, [t], {"T009": parse_logic(data, "T009", 3,
                                                                  stored=False)})
    lv = m.logics[0]
    assert lv.deployables == ["AMF"] and G._phase_deps(m, lv) == ["AMF"] * 3
    titles = re.findall(r'subgraph l_\w+ \["([^"]*)"\]', G.activity(m, lv)[0].src())
    assert titles == ["Initiator", "AMF"]


# ---- "*" interface rows apply to network functions only -----------------------------------

def test_wildcard_interfaces_only_for_network_functions():
    assert interface("NRF", "AMF") == "Nnrf"
    assert interface("NSSF", "SMF") == "Nnssf"
    assert interface("NSSF", "Operator / OAM") == "O&M"
    assert interface("NRF", "UE") == ""
    assert interface("NSSF", "Monitoring / observability") == ""
    assert interface("UDM", "NSSF") == "Nudm" and interface("UDM", "AMF") == "N8 (Nudm)"
    assert interface("UDM", "OSS / BSS") == ""
    assert interface("Maintenance CLI", "AMF") == ""


def test_code_evidence_label_is_the_via_fallback():
    cl = {0: _ci(0, [("cli/x", 5)]), 1: _ci(1, [("core/sbi/discovery", 5)])}
    t = _traj("T006", [0, 1], ["cli/x::Run"], path=["cli/x::Run", "core/sbi/discovery::Send"])
    m = _model(cl, {0: 0, 1: 0}, [t], {"T006": _old("T006", 2, ["NRF"])})
    nrf = m.logics[0].ref("NRF")
    assert nrf.via == "Nnrf" and nrf.code_dep == "SBI client (shared)"


# ---- only a confirmed message name is a solid arrow --------------------------------------

def test_sequence_unconfirmed_arrows_are_dashed():
    data = dict(T.SEQ_DATA, trigger_sources=["UE", "timer"])
    data["steps"] = [dict(s) for s in T.SEQ_DATA["steps"]]
    data["steps"][3] = {"at": 4, "action": "release UE context", "actor": "gNB",
                        "direction": "out"}                       # no message
    m, lv = T.one(data, T.seq_traj())
    src = G.sequence(m, lv)[0].src()
    assert "a_ue->>+n_amf: N1 NAS: Deregistration Request" in src
    assert "tm-->>n_amf: starts the logic (see trigger)" in src
    assert "n_amf-->>a_ng_ran_gnb: Release UE context" in src
    for line in src.splitlines():
        if "->>" in line and "-->>" not in line:
            text = line.split(": ", 1)[1]
            assert "Deregistration Request" in text, line
    nomsg = dict(T.SEQ_DATA, steps=[{"at": 1, "action": "validate UE state", "actor": "UE",
                                     "direction": "in"}])
    m2, lv2 = T.one(nomsg, T.seq_traj())
    assert "a_ue-->>+n_amf: N1 NAS: Validate UE state" in G.sequence(m2, lv2)[0].src()
    assert "dashed arrow = described by the LLM only" in G.SEQ_CAPTION


# ---- the message-type word must be in the code -------------------------------------------

def test_verify_message_needs_the_message_type_word():
    m, lv = T.one(T.SEQ_DATA, T.seq_traj())
    cases = [("RecvDeregReq", "Deregistration Request", True),
             ("RecvDeregReq", "Deregistration Accept", False),
             ("RecvRegReq", "Registration Reject", False),
             ("HandleHoRequired", "Handover Failure", False),
             ("HandleHoRequired", "Handover Required", True),
             ("SendUeCxtRelCmd", "UE Context Release Complete", False),
             ("SendUeCxtRelCmd", "UE Context Release Command", True),
             ("SendRegRej", "Registration Reject", True)]
    for fn, msg, want in cases:
        lv.ev_tokens = G.tokens(fn)
        assert G.verify_message(msg, lv) is want, (fn, msg)


# ---- colocated subject links keep the solid / dashed evidence -----------------------------

def test_d7_colocated_links_are_dashed_for_old_json():
    cl = {0: _ci(0, [("smsf/service", 5)], "Sms"), 1: _ci(1, [("amf/sms", 5)], "Amf sms")}
    trajs = [_traj("T001", [0], ["smsf/service::Handle"]),
             _traj("T002", [1], ["amf/sms::Send"])]
    logic = {"T001": _old("T001", 1, ["UE"]), "T002": _old("T002", 1, ["SMSF", "UE"])}
    m = _model(cl, {0: 0, 1: 0}, trajs, logic)
    d, _, _ = G.usecase_group(m, 0)
    assert "u_t002 -.- sys_smsf" in d.body and "u_t002 --- sys_smsf" not in d.body


# ---- D2 never draws a deployable as an actor of its own subject --------------------------

def test_d2_skips_a_colocated_actor_of_its_own_subject():
    cl = {0: _ci(0, [("smsf/service", 5)], "Sms"), 1: _ci(1, [("amf/ngap", 5)], "Ngap")}
    trajs, logic = [], {}
    for i, (root, c) in enumerate([("smsf/service::A", 0), ("smsf/service::B", 0),
                                   ("amf/ngap::C", 1), ("amf/ngap::D", 1)], start=1):
        tid = f"T{i:03d}"
        trajs.append(_traj(tid, [c], [root]))
        if c == 0:
            data = {"title": "MO", "trigger": "AMF forwards", "trigger_sources": ["AMF"],
                    "actors": [{"name": "AMF"}], "implements": ["MO SMS over NAS"],
                    "steps": [{"at": 1, "action": "get", "actor": "AMF", "direction": "in",
                               "message": "Nsmsf_SMService_UplinkSMS"}]}
        else:
            data = {"title": "Reg", "trigger": "UE registers", "trigger_sources": ["UE"],
                    "actors": [{"name": "UE"}, {"name": "SMSF"}],
                    "implements": ["SMS over NAS registration"],
                    "steps": [{"at": 1, "action": "activate", "actor": "SMSF",
                               "direction": "out", "message": "Nsmsf_SMService_Activate"}]}
        logic[tid] = parse_logic(data, tid, 1, stored=False)
    m = _model(cl, {0: 0, 1: 0}, trajs, logic)
    d, lines = G.procedure_usecases(m)
    assert d.check() == []
    src = d.src()
    home = re.search(r'subgraph (sys_\w+) \["«system» (\w+)"\]', src)
    assert home and f"a_{home.group(2).lower()}[" not in src     # not its own outside actor
    assert "or the family's only logic" in lines[0]


# ---- (R)AN next to the UE; a break is checked first and never inside an opt ---------------

def test_sequence_ran_after_ue_and_break_placement():
    data = dict(T.SEQ_DATA, alternatives=[{"at": 2, "condition": "UE context not found",
                                           "result": "reject"}])
    data["steps"] = [dict(s) for s in T.SEQ_DATA["steps"]]
    data["steps"][1] = {"at": 2, "action": "release SM contexts", "actor": "PCF",
                        "message": "Npcf_AMPolicyControl_Delete", "direction": "out"}
    m, lv = T.one(data, T.seq_traj())
    src = G.sequence(m, lv)[0].src()
    parts = re.findall(r"^\s*(?:participant|actor) (\w+) as", src, re.M)
    assert parts[:3] == ["a_ue", "a_ng_ran_gnb", "n_amf"]
    assert _stack_ok(src), src
    assert src.index("break UE context not found") < src.index("Npcf_AMPolicyControl_Delete")
    assert "[2] optional · " in src
    assert _stack_ok(hostile_sequence())


# ---- state evidence names a function of that step ----------------------------------------

def test_state_evidence_is_exact_or_a_long_prefix_in_its_step():
    def drawn(evidence: str, at) -> bool:
        sc = {"entity": "RM", "from": "RM-REGISTERED", "to": "RM-DEREGISTERED",
              "evidence": evidence, "at": at}
        m, lv = T.one(dict(T.SEQ_DATA, state_changes=[sc]), T.seq_traj())
        return bool(G.state_logic(lv)[0])
    assert drawn("SetRegState", 2) and drawn("SetRegSt", 2) and drawn("SetRegState", None)
    assert not drawn("State", None) and not drawn("Set", 2) and not drawn("e", 2)
    assert not drawn("SetRegState", 1)                         # not a function of step 1


# ---- captions say no more than the rule --------------------------------------------------

def test_participants_caption_for_a_various_process_start():
    data = dict(T.SEQ_DATA, trigger_sources=["process start", "UE"],
                steps=[{"at": 1, "action": "check"}],
                actors=[{"name": "UE"}, {"name": "SMF"}, {"name": "PCF"}])
    m, lv = T.one(data, T.seq_traj())
    d, caption = G.participants(m, lv)
    assert lv.initiator.kind == "various" and d
    assert caption.endswith(" Can also start at process start.")


# ---- full group labels outside mermaid ---------------------------------------------------

def test_group_labels_keep_every_name_and_the_count():
    names = ["Authentication security and NAS", "N26 mobility and EPS interworking",
             "Location services and event exposure", "Paging", "Timers"]
    cl = {c: _ci(c, [("amf/x", 1)], names[c], size=50 - c) for c in range(5)}
    m = _model(cl, {c: 0 for c in cl}, [_traj("T001", [0], ["amf/x::A"])], {})
    assert m.group_labels[0] == "Group 0: " + " · ".join(names[:3]) + " +2"
    short = m.group_short[0]
    assert len(short) <= G.LIM_SUBJECT and short.endswith(" +3")      # two names fit
    assert short.startswith("Group 0: Authentication")


# ---- activity boxes keep the verb and the message -----------------------------------------

def test_activity_box_wraps_and_drops_the_lead_clause():
    data = {"title": "x", "trigger": "Path Switch Request", "trigger_sources": ["gNB"],
            "actors": [{"name": "gNB"}],
            "steps": [{"at": 1, "action": "In AMF path-switch handling, build and send an NGAP "
                                          "Path Switch Request Failure to the gNB"}]}
    m, lv = T.one(data, T.make_traj("T010", [0]))
    src = G.activity(m, lv)[0].src()
    node = re.search(r's1\["([^"]*)"\]', src).group(1)
    lines = node.split("<br>")
    assert 2 <= len(lines) <= 1 + G.NODE_LINES
    assert lines[1].startswith("Build and send")
    assert "Path Switch Request Failure" in " ".join(lines[1:])
    assert all(len(x) <= G.NODE_WIDTH + 1 for x in lines[1:])
    assert G.wrap_lines("word " * 60, 32, 3)[-1].endswith("…")


# ---- components without an llm kind are not drawn as "no business logic" -----------------

def test_unlabelled_components_with_logics_are_neutral():
    cl = {0: _ci(0, [("amf/a", 5)]), 1: _ci(1, [("amf/b", 5)])}
    m = _model(cl, {0: 0, 1: 0}, [_traj("T001", [0], ["amf/a::A"])], {})
    d, legend = G.component_view(m)
    src = d.src()
    assert re.search(r'^\s*c0\[.*\]:::k_unknown$', src, re.M)
    assert re.search(r'^\s*c1\[.*\]:::k_none$', src, re.M)
    assert legend == G.D3_LEGEND_PLAN


# ---- D9 only when it adds something to the table rows ------------------------------------

def test_participants_skipped_for_listed_only_actors():
    t = T.make_traj("T001", [0, 2], old=True)
    m, lv = T.one({"title": "x", "actors": ["UE", "SMF", "PCF", "UDM"], "steps": ["a", "b"],
                   "trigger": "timer expiry"}, t, stored=True)
    assert not lv.new_schema and len(lv.actors) >= 3
    assert G.participants(m, lv) == ("", "")


# ---- entry examples stay distinct after clipping -----------------------------------------

def test_entry_examples_keep_their_distinguishing_tail():
    base = "amf/handover/scenario::PathSwitchRequestFailureDueToConflictingN26Handover"
    t = T.make_traj("T010", [0], roots=[base + "Source", base + "Target"])
    line = G.where_in_code(t)[1]
    ex = re.findall(r"`([^`]*)`", line.split("e.g. ", 1)[1])
    assert len(ex) == 2 and len(set(ex)) == 2
    assert ex[0].endswith("HandoverSource") and ex[1].endswith("HandoverTarget")
    assert all(len(x) <= 60 for x in ex)
    assert G.mid_clip("short", 60) == "short"


# ---- the exported html highlights the sidebar entry without throwing -----------------------

def test_exported_html_show_page_is_patched(tmp_path):
    from codocs.bizlogic.run import patch_html
    from codocs.repowiki.core.wiki_builder import SidebarItem, Wiki, WikiPage
    from codocs.repowiki.export.html import export_html

    wiki = Wiki(project_name="p")
    wiki.pages = [WikiPage(id="a", title="A", content="# A\n"),
                  WikiPage(id="b", title="B", content="# B\n")]
    wiki.sidebar = [SidebarItem(title="A", page_id="a"), SidebarItem(title="B", page_id="b")]
    out = tmp_path / "w.html"
    export_html(wiki, out, branding=False, mermaid_js="")
    raw = out.read_text(encoding="utf-8")
    assert "event.target.classList.add('active');" in raw        # the template as shipped
    fixed = patch_html(raw)
    assert "event.target" not in fixed
    assert "a.classList.add('active')" in fixed and "showPage('a');" in fixed


def test_k8s_controller_logic_is_not_started_by_the_operator_role():
    op = next(c for c in A.normalize("operator (O&M)"))
    ctrl = ["operators/gw-operator/controllers::PairReconciler.Reconcile"]
    assert A.controller_initiator(op, ctrl).name == "Kubernetes platform"
    mixed = ctrl + ["gw/api::Serve"]
    assert A.controller_initiator(op, mixed).name == "Operator / OAM"
    cli = ["cli/gw::SwitchOverHandler"]
    assert A.controller_initiator(op, cli).name == "Operator / OAM"
    assert A.controller_initiator(op, None).name == "Operator / OAM"
    assert [c.name for c in A.normalize("AMF (N20)")] == ["AMF"]
    assert [c.name for c in A.normalize("AMF (N14)")] == ["Peer AMF"]


def test_patch_html_widens_the_content_column():
    import tempfile
    from pathlib import Path

    from codocs.bizlogic.run import CONTENT_CSS, patch_html
    from codocs.repowiki.core.wiki_builder import Wiki, WikiPage
    from codocs.repowiki.export.html import export_html

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "w.html"
        export_html(Wiki(pages=[WikiPage(id="a", title="A", content="# A")], project_name="P"),
                    out, branding=False)
        raw = out.read_text()
        assert CONTENT_CSS in raw                 # the template still has the 900px cap
        html = patch_html(raw)
    assert "max-width: 900px" not in html and "max-width: none" in html
    assert ".content > p" in html


def test_logic_page_header_is_a_table():
    from codocs.bizlogic.export import _badge
    from codocs.bizlogic.infer import BusinessLogic

    bl = BusinessLogic(trajectory="T1", confidence="medium", llm_confidence="high")
    assert _badge(bl) == "🟡 medium (llm: high)"
    assert _badge(BusinessLogic(trajectory="T1")) == "-"
