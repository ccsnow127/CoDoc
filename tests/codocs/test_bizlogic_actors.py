"""actor normalisation, initiators, deployables and the domain tables (bizlogic): the
generic rules without a profile, and the built-in 3gpp-5gc example profile."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from codocs.bizlogic import actors as A
from codocs.bizlogic import domain as D
from codocs.bizlogic.profile import load_profile

# the example profile plus synthetic package evidence and two extra deployables
EVIDENCE = (("amf/ngap", "N2 (NGAP)", frozenset({"NG-RAN (gNB)"})),
            ("amf/gtp", "N26 (GTPv2-C)", frozenset({"MME (EPC)"})),
            ("amf/sm", "N11 (Nsmf)", frozenset({"SMF"})),
            ("smsf/service", "N20 (Nsmsf)", frozenset({"AMF"})),
            ("smsf/service/charging", "Nchf", frozenset({"CHF"})),
            ("core/sbi/discovery", "Nnrf", frozenset({"NRF"})))


def gpp(**extra):
    base = load_profile("3gpp-5gc")
    deps = {**base.deployables, "core": "Core platform", "core/sbi": "SBI client"}
    return replace(base, deployables=deps, pkg_evidence=EVIDENCE, **extra)


@pytest.fixture
def profile():
    prev = D.use_profile(gpp())
    yield
    D.use_profile(prev)


def names(raw: str) -> list[str]:
    return [c.name for c in A.normalize(raw) if c.kind not in A.HIDDEN]


# ---- without a profile (the default) -----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("operator/monitoring system", ["Operator / OAM", "Monitoring / observability"]),
    ("operator or internal maintenance/audit process", ["Operator / OAM"]),
    ("Kubernetes operator", ["Kubernetes operator"]),
    ("Operator startup, Kubernetes reconciliation event", ["Kubernetes operator"]),
    ("operator storage/key-value store", ["Key-value store"]),
    ("distributed Redis/KV store", ["Key-value store"]),
    ("load balancer", ["Load balancer"]), ("DNS", ["DNS server"]),
    ("Kubernetes API server", ["Kubernetes platform"]),
    ("partner services", ["Other services"]),
    ("internal control processes", []),
    ("payment gateway", ["payment gateway"]),          # unknown: kept as free text
    ("gNB/RAN", ["gNB/RAN"]), ("AMF", ["AMF"]),        # no 3gpp vocabulary by default
])
def test_generic_normalize_table(raw, expected):
    assert names(raw) == expected


def test_generic_defaults_have_no_domain_tables():
    assert D.active().empty and D.DEPLOYABLES == {} and D.PROCEDURES == {}
    assert A.normalize("gNB")[0].kind == "unknown"
    assert A.deployable_of("billing/api::Run") == "billing"     # top dir fallback
    assert A.allowed_actors(["billing"]) == ""
    assert A.pkg_evidence(["amf/ngap::Recv"]) == {}
    assert D.interface("AMF", "SMF") == "" and D.procedure_key("Initial registration") == ""
    assert D.norm_state("RM", "registered") == "REGISTERED" and D.state_entity("RM") == ""
    assert A.DEFAULT_PRIMARY == {"Operator / OAM"} and not A.DEVICE and not A.LABELS
    assert set(A.BUILTIN_ACTORS) <= set(A.KINDS)


def test_generic_initiators():
    def init(trigger, actors, sources=None):
        bl = NS(trigger=trigger, actors=actors, trigger_sources=sources or [])
        return A.resolve_initiators(bl, A.bl_actors(actors, ["billing"]))

    assert init("", [], ["timer"]).name == "Timer"
    assert init("operator command received", ["operator"]).name == "Operator / OAM"
    assert init("", ["payment gateway"], ["payment gateway"]).name == "payment gateway"
    assert init("Process start of the worker", []).name == "Process start"
    op = A.normalize("operator")[0]
    assert A.controller_initiator(op, ["operators/x::Reconcile"]).name == "Operator / OAM"


def test_only_one_catch_all_actor():
    prev = D.use_profile(load_profile("3gpp-5gc"))
    try:
        assert names("partner services") == ["Other 5GC NFs"]   # the profile's comes first
    finally:
        D.use_profile(prev)
    assert names("partner services") == ["Other services"]


# ---- the 3gpp-5gc example profile -----------------------------------------------------

GPP_TABLE = [
    ("gNB/RAN", ["NG-RAN (gNB)"]), ("RAN/gNB", ["NG-RAN (gNB)"]),
    ("gNB/NG-RAN", ["NG-RAN (gNB)"]), ("AN release", ["NG-RAN (gNB)"]),
    ("(R)AN", ["NG-RAN (gNB)"]),
    ("source AMF", ["Peer AMF"]), ("target AMF", ["Peer AMF"]), ("another AMF", ["Peer AMF"]),
    ("AMF", ["AMF"]),
    ("operator/monitoring system", ["Operator / OAM", "Monitoring / observability"]),
    ("operator or internal maintenance/audit process", ["Operator / OAM"]),
    ("Kubernetes operator", ["Kubernetes operator"]),
    ("Operator startup, Kubernetes reconciliation event", ["Kubernetes operator"]),
    ("operator-facing AMF control processes", []),
    ("operator storage/key-value store", ["Key-value store"]),
    ("operator monitoring/charging systems", ["Monitoring / observability", "CHF"]),
    ("NRF/cache layer", ["NRF"]), ("NRF/cache store", ["NRF"]),
    ("SMF/PDU session contexts", ["SMF"]), ("SMF/PDU session context data", ["SMF"]),
    ("other 5G core functions such as UDM/PCF/partner services", ["Other 5GC NFs"]),
    ("monitoring/OSS client", ["Monitoring / observability", "OSS / BSS"]),  # by position
    ("Diameter peer", ["SMS-GMSC / IWMSC (SGd, Diameter)"]),
    ("subscriber data management", ["UDM"]),
    ("internal AMF control/notification logic", []),
    ("distributed Redis/KV store", ["Key-value store"]),
    ("load balancer", ["Load balancer"]),
    ("external network functions subscribed by the AMF", ["Other 5GC NFs"]),
    ("I-SMF", ["SMF"]), ("UDM-like notifier", ["UDM"]),
    ("peer mobility node over N26", ["MME (EPC)"]),
    ("policy-related network functions", ["PCF"]),
    ("SMS-related functions", ["SMSF"]),
    ("user plane function", ["UPF"]), ("UDR", ["UDR"]),
]


@pytest.mark.parametrize("raw,expected", GPP_TABLE)
def test_normalize_table(profile, raw, expected):
    assert names(raw) == expected


def test_normalize_kinds_and_edge_cases(profile):
    assert "NG-RAN (gNB)" not in names("An AMF procedure starts")
    assert "Peer AMF" not in names("target TAI update")
    assert [c.kind for c in A.normalize("distributed Redis/KV store")] == ["infra"]
    assert [c.kind for c in A.normalize("load balancer")] == ["infra"]
    assert [c.kind for c in A.normalize("operator-facing AMF control processes")] == ["internal"]
    assert [c.kind for c in A.normalize("service-switch database")] == ["data"]
    unk = A.normalize("Zeta box")
    assert [(c.name, c.kind) for c in unk] == [("Zeta box", "unknown")]
    assert A.normalize("x" * 50)[0].name == "x" * 29 + "…"
    assert A.normalize("") == [] and A.normalize("  ") == []
    assert A.normalize("UE")[0].display == "UE (subscriber device)"
    assert A.normalize("operator")[0].kind == "role"
    # dict / object actors (new json) normalise by their name
    assert names({"name": "SMF", "via": "N11"}) == ["SMF"]
    assert names(NS(name="gNB", via="")) == ["NG-RAN (gNB)"]


def test_normalize_all_dedups_in_order(profile):
    got = A.normalize_all(["gNB/RAN", "UE", "RAN/gNB", "subscriber/UE", "SMF"])
    assert [c.name for c in got] == ["NG-RAN (gNB)", "UE", "SMF"]


def test_self_suppression_and_colocation(profile):
    nrf = ["NRF"]
    assert [c.name for c in A.bl_actors(["NRF", "AMF"], nrf)] == ["AMF"]
    assert A.bl_actors(["AMF"], ["AMF"]) == []
    assert [c.name for c in A.bl_actors(["AMF", "SMSF", "UE"], ["AMF"])] == ["SMSF", "UE"]
    smsf = A.normalize("SMSF")[0]
    co = A.colocate(smsf, ["AMF", "SMSF"])
    assert co.kind == "colocated" and co.display == "SMSF (this repo)"
    assert A.colocate(smsf, ["AMF"]).kind == "nf"
    assert A.colocate(A.normalize("UE")[0], ["AMF", "SMSF"]).kind == "nf"


def _bl(trigger: str, actors: list[str], sources=None):
    return NS(trigger=trigger, actors=actors, trigger_sources=sources or [])


def _init(trigger: str, actors: list[str], sources=None, deps=("AMF",)) -> A.Canon:
    bl = _bl(trigger, actors, sources)
    return A.resolve_initiators(bl, A.bl_actors(actors, list(deps)))


def test_resolve_initiators_new_schema(profile):
    assert _init("", [], ["timer"]).name == "Timer"
    v = _init("", ["UE"], ["UE", "timer"])
    assert v.kind == "various" and v.members == ("UE", "Timer")
    assert A.initiator_text(v) == "Various: UE, Timer"
    assert _init("", [], ["process start"]).kind == "process"
    assert _init("", [], ["Redis"]).kind == "infra"
    assert _init("", [], ["UE", "gNB", "SMF", "timer"]).members == ("UE", "NG-RAN (gNB)", "SMF")


def test_resolve_initiators_old_json(profile):
    assert _init("T3550 expiry for UE", ["UE"]).name == "Timer"
    assert _init("UE sends Registration Request via gNB", ["UE", "gNB"]).name == "UE"
    r = _init("An AMF audit scenario runs", ["gNB/RAN"])
    assert r.name != "NG-RAN (gNB)" and "NG-RAN (gNB)" not in r.members
    r = _init("Incoming AMF signaling, notification or timer expiry", ["UE"])
    assert r.kind == "various" and r.name != "Timer"
    assert A.initiator_text(r) == "Various: external signalling, Timer"
    assert _init("subscriber data modification notification", ["UDM", "UE"]).name == "UDM"
    r = _init("AMF internal handling of a source-complete cleanup", ["peer AMF"])
    assert r.name != "Peer AMF" and "Peer AMF" not in r.members
    assert A.initiator_text(_init("", ["UE"])) == "See trigger"
    assert A.initiator_text(_init("a notification arrives", [])) == "external signalling"
    assert _init("Process start of the monitor service", []).name == "Process start"
    # the case-sensitive AN alias, and self is never an initiator
    assert _init("AN release triggered", ["RAN/AN"]).name == "NG-RAN (gNB)"
    assert "AN" in A.CASE_SENSITIVE_ALIASES and "AN" in A.ALIASES["NG-RAN (gNB)"]
    assert _init("AMF starts", ["AMF"]).kind == "unknown_trigger"


def test_initiator_display(profile):
    assert A.initiator_text(A.normalize("UE")[0]) == "UE (subscriber device)"
    assert A.initiator_text(A.EVENT) == "external signalling"
    assert A.initiator_text(A.SEE_TRIGGER) == "See trigger"
    assert A.starts(A.TIMER, A.Canon("Various", "various", members=("UE", "Timer")))


def test_controller_roots_come_from_the_profile():
    op = A.normalize("operator")[0]
    prev = D.use_profile(gpp(controller_roots=("operators",)))
    try:
        ctrl = ["operators/gw-operator/controllers::PairReconciler.Reconcile"]
        assert A.controller_initiator(op, ctrl).name == "Kubernetes platform"
        mixed = ctrl + ["cli/gw::SwitchOverHandler"]
        assert A.controller_initiator(op, mixed).name == "Operator / OAM"
        assert A.controller_initiator(op, None).name == "Operator / OAM"
    finally:
        D.use_profile(prev)


def _traj(groups=None, counts=None, roots=None):
    return NS(root_groups=groups or [], root_counts=counts or [], roots=roots or [])


def test_deployables(profile):
    assert A.deployables(_traj([("amf", 10, 100), ("smsf", 1, 5)])) == ["AMF"]
    assert A.deployables(_traj([("amf", 1, 60), ("smsf", 1, 40)])) == ["AMF", "SMSF"]
    assert A.deployables(_traj([("amf", 1, 40), ("smsf", 1, 35), ("nrf", 1, 25)])) \
        == ["AMF", "SMSF"]
    assert A.deployables(_traj(counts=[("amf/x::F", 9)], roots=["amf/x::F", "smsf/y::G"])) \
        == ["AMF", "SMSF"]
    assert A.deployables(_traj()) == []
    assert A.deployable_of("core/sbi/x::F") == "SBI client"          # longest prefix
    assert A.deployable_of("core/store") == "Core platform"
    assert A.deployable_of("nrf::main") == "NRF"
    assert A.deployable_of("amfx/y") == "amfx"
    assert A.top_dir("nrf::main") == "nrf"
    assert A.top_dir("amf/sm/x::F") == "amf"


def test_validate_via_and_interfaces(profile):
    assert A.validate_via("N11", "SMF", ["AMF"])
    assert A.validate_via("Nsmf_PDUSession", "SMF", ["AMF"])
    assert not A.validate_via("N8", "SMF", ["AMF"])
    assert not A.validate_via("N11", "UE", ["AMF"])          # N1 is not N11
    assert A.validate_via("N1 NAS", "UE", ["AMF"])
    assert A.validate_via("Nnrf_NFDiscovery", "SMF", ["NRF"])
    assert not A.validate_via("", "SMF", ["AMF"])
    assert D.interface("AMF", "SMF") == "N11 (Nsmf)"
    assert D.interface("NRF", "AMF") == "Nnrf"
    assert D.interface("AMF", "Zeta") == ""
    assert D.ref_point("N11 (Nsmf)") == "N11"
    line = A.allowed_actors(["AMF"])
    assert line.startswith("UE (N1), NG-RAN (N2), peer AMF (N14), MME (N26), SMF (N11)")
    assert ", AMF" not in line and "SMSF (N20)" in line
    assert "AMF (N20)" in A.allowed_actors(["SMSF"])


def test_pkg_evidence(profile):
    ev = A.pkg_evidence(["amf/gtp/scenario::X", "core/sbi/discovery::Y",
                         "amf/common/scenario::Z"])
    assert ev == {"MME (EPC)": "N26 (GTPv2-C)", "NRF": "Nnrf"}
    assert A.pkg_evidence(["smsf/service/charging::C"]) == {"CHF": "Nchf"}   # longest prefix
    assert A.pkg_evidence(["amf/ngap/ut::main", "amf/ngap/ut/ut::Info"]) == {}  # test code


def test_procedures_and_states(profile):
    assert D.procedure_key("  initial   REGISTRATION ") == "Initial registration"
    assert D.procedure_key("Registration") == ""
    assert D.norm_state("UE RM state", "5GMM-REGISTERED") == "RM-REGISTERED"
    assert D.norm_state("RM", "registered") == "RM-REGISTERED"
    assert D.norm_state("UE CM state", "idle") == "CM-IDLE"
    assert D.norm_state("NF registration", "registered") == "REGISTERED"
    assert D.norm_state("PDU session", "active") == "ACTIVE"
    assert D.norm_state("other", "pdu_session active") == "PDU-SESSION-ACTIVE"
    assert D.state_entity("UE CM state") == "CM"
    assert D.state_entity("NF registration") == "NF registration"


def test_render_by_kind(profile):
    ue, op = A.normalize("UE")[0], A.normalize("operator")[0]
    assert A.usecase_shape(ue) == "device" and A.stereotype(ue) == "«device»"
    assert A.usecase_shape(op) == "stick" and A.seq_keyword(op) == "actor"
    assert A.stereotype(A.normalize("SMF")[0]) == "«system»"
    assert A.stereotype(A.TIMER) == "«timer»" and A.lane_title(A.TIMER) == "Timer"
    assert A.usecase_shape(A.PROCESS) is None and A.seq_keyword(A.PROCESS) == "note"
    assert A.lane_title(A.EVENT) == "External signalling"
    redis = A.normalize("Redis")[0]
    assert A.usecase_shape(redis) is None and A.context_role(redis) == "infra"
    assert A.lane_title(ue) == "UE (subscriber device)"
    assert A.context_role(A.colocate(A.normalize("SMSF")[0], ["SMSF"])) == "boundary"
    for kind in ("data", "internal"):
        c = A.Canon("_x", kind)
        assert (A.usecase_shape(c), A.context_role(c), A.seq_keyword(c), A.lane_title(c)) \
            == (None, None, None, None)


def test_domain_tables_consistent(profile):
    canon = set(A.KINDS)
    for dep, row in D.INTERFACES.items():
        assert dep in D.DEPLOYABLES.values(), dep
        assert all(c == "*" or c in canon for c in row), dep
    assert all(dep in D.DEPLOYABLES.values() for dep in D.SELF.values())
    assert all(c in canon for _n, c in D.PROMPT_ACTORS)
    assert all(c in canon for c in A.DEFAULT_PRIMARY | A.HUMAN | A.DEVICE)
    assert all(c in canon for c in D.WILDCARD_PARTIES)
    assert all(len(v) == 2 for v in D.PROCEDURES.values())
    assert set(D.STATE_VOCAB) == {"RM", "CM", "NF registration", "PDU session"}


def test_switching_back_to_the_default_clears_the_tables():
    prev = D.use_profile(gpp())
    assert D.active().name == "3gpp-5gc" and "UE" in A.KINDS
    D.use_profile(prev)
    assert D.active().empty and "UE" not in A.KINDS and not D.PKG_EVIDENCE
    assert A.deployable_of("amf/sm::F") == "amf"
