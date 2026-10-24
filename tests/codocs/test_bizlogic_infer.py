"""the business-logic llm json: new-shape replies, malformed replies, old stored json and the
prompt inputs, with the 3gpp-5gc example profile and without a profile (domain-neutral)."""

from __future__ import annotations

import json
import re

import networkx as nx
import pytest

from codocs.bizlogic import domain as D
from codocs.bizlogic.actors import validate_via
from codocs.bizlogic.domain import PROCEDURES
from codocs.bizlogic.graph import FuncGraph, FuncNode
from codocs.bizlogic.infer import (
    Actor,
    Alternative,
    BusinessLogic,
    DataObject,
    StateChange,
    Step,
    _system,
    apply_logic,
    domain_lines,
    infer_trajectories,
    parse_logic,
    trajectory_prompt,
)
from codocs.bizlogic.profile import load_profile
from codocs.bizlogic.walk import Trajectory

# 3gpp / 5g words that must never reach a domain-neutral prompt
DOMAIN_WORDS = re.compile(r"3GPP|5G|\bAMF\b|\bSMF\b|\bSMSF\b|\bNRF\b|\bUE\b|\bRM\b|"
                          r"\bCM\b|PDU|NGAP|\bNAS\b|\bN1[01]?\b|PROCEDURES\]|\"implements\"")


@pytest.fixture
def gpp():
    prev = D.use_profile(load_profile("3gpp-5gc"))
    yield
    D.use_profile(prev)


def _fg(nodes: list[str], doc: str = "does a thing") -> FuncGraph:
    g = nx.DiGraph()
    fn = {}
    for i, n in enumerate(nodes):
        pkg, name = n.split("::")
        fn[n] = FuncNode(id=n, pkg=pkg, file=f"{pkg}/x.go", name=name, kind="func", line=i + 1,
                         loc=5, summary=doc)
        g.add_node(n)
    for u, v in zip(nodes, nodes[1:]):
        g.add_edge(u, v, kind="call", weight=1.0)
    return FuncGraph(module="m", graph=g, nodes=fn)


NODES = ["amf/nas::HandleRegistration", "amf/sm::SendUpdateSMContext", "core/store::Save"]


def _traj(support: int = 10, own: int = 10, nodes: list[str] = NODES) -> Trajectory:
    return Trajectory(id="T001", clusters=[0, 1, 2], path=nodes, support=support,
                      roots=[nodes[0]], own_support=own, step_walks=[support] * 3,
                      root_counts=[(nodes[0], support)],
                      step_functions=[[(n, support)] for n in nodes])


NEW_REPLY = {
    "title": "PDU session update", "short_title": "Update PDU session",
    "summary": "Updates the SM context.", "scope": "Single",
    "trigger": "UE request", "trigger_sources": ["UE", "Timer"],
    "actors": [{"name": "UE", "via": "N1"}, {"name": "SMF", "via": "Nsmf_PDUSession"},
               {"name": "UDM", "via": "N11", "role": "primary"}],
    "steps": [{"at": 1, "action": "Receive request", "actor": "UE", "direction": "in",
               "message": "Registration Request"},
              {"at": "2", "action": "Update SM context", "actor": "SMF", "direction": "OUT",
               "message": "Nsmf_PDUSession_UpdateSMContext"},
              {"at": 9, "action": "Store context", "direction": "out"}],
    "variants": ["with handover"],
    "alternatives": [{"at": 2, "condition": "SMF rejects", "result": "abort"},
                     {"at": 1, "condition": "", "result": "dropped"}],
    "preconditions": ["UE registered", "session exists", "third"],
    "state_changes": [{"entity": "RM", "from": "5GMM-DEREGISTERED", "to": "registered",
                       "at": 3, "evidence": "Save"},
                      {"entity": "CM", "from_state": "idle", "to_state": "CONNECTED"},
                      {"entity": "PDU session", "from": "", "to": "ACTIVE"}],
    "data_objects": [{"name": "UE context", "op": "ru"}, {"name": "PDU session context",
                                                         "op": "Dxc"}],
    "implements": ["pdu session MODIFICATION", "Not a procedure"],
    "supports": ["PDU session modification", "AN release"],
    "outcome": "context updated", "category": "Network procedure",
    "confidence": "High",
}


# ---- new-shape replies

def test_new_reply_is_parsed_into_typed_fields(gpp):
    bl = parse_logic(NEW_REPLY, "T001", 3, stored=False, deployables=["AMF"])
    assert bl.short_title == "Update PDU session" and bl.scope == "single"
    assert bl.trigger_sources == ["UE", "timer"]
    assert bl.actors == [Actor("UE", "N1"), Actor("SMF", "Nsmf_PDUSession"), Actor("UDM", "")]
    assert bl.steps == [
        Step("Receive request", 1, "UE", "Registration Request", "in"),
        Step("Update SM context", 2, "SMF", "Nsmf_PDUSession_UpdateSMContext", "out"),
        Step("Store context", None, "", "", "internal")]       # bad at, no actor
    assert bl.variants == ["with handover"]
    assert bl.alternatives == [Alternative(2, "SMF rejects", "abort")]
    assert bl.preconditions == ["UE registered", "session exists"]
    assert bl.state_changes == [StateChange("RM", "RM-DEREGISTERED", "RM-REGISTERED", 3, "Save"),
                                StateChange("CM", "CM-IDLE", "CM-CONNECTED", None, "")]
    assert bl.data_objects == [DataObject("UE context", "RU"),
                               DataObject("PDU session context", "CD")]
    assert bl.implements == ["PDU session modification"]
    assert bl.supports == ["AN release"]                       # implements wins
    assert bl.category == "business procedure"                 # the older category name
    assert bl.llm_confidence == "high" and bl.confidence == ""  # apply_logic sets it


def test_reply_string_steps_are_never_mapped():
    bl = parse_logic({"title": "x", "steps": ["a", "b", "c"]}, "T001", 3, stored=False)
    assert [s.action for s in bl.steps] == ["a", "b", "c"]
    assert all(s.at is None for s in bl.steps)


def test_scalar_fields_are_coerced():
    bl = parse_logic({"category": "Operations & Maintenance", "scope": "several",
                      "trigger_source": "Process start", "confidence": "very"}, "T", 2,
                     stored=False)
    assert bl.category == "operations & maintenance" and bl.scope == ""
    assert bl.trigger_sources == ["process start"] and bl.llm_confidence == ""
    assert parse_logic({"category": "an infrastructure task"}, "T", 1, False).category == \
        "infrastructure"
    assert parse_logic({"category": "misc"}, "T", 1, False).category == "other"
    assert parse_logic({}, "T", 1, False).category == ""
    many = parse_logic({"trigger_sources": ["UE", "timer", "SMF", "PCF"]}, "T", 1, False)
    assert many.trigger_sources == ["UE", "timer", "SMF"]


def test_lists_are_capped(gpp):
    data = {"alternatives": [{"condition": f"c{i}", "at": 1} for i in range(5)],
            "state_changes": [{"entity": "CM", "from": "IDLE", "to": "CONNECTED"}] * 5,
            "data_objects": [{"name": f"d{i}", "op": "C"} for i in range(6)],
            "implements": list(PROCEDURES)[:5], "supports": list(PROCEDURES)[5:10]}
    bl = parse_logic(data, "T", 1, stored=False)
    assert len(bl.alternatives) == 3 and len(bl.state_changes) == 3
    assert len(bl.data_objects) == 4
    assert bl.implements == list(PROCEDURES)[:3] and bl.supports == list(PROCEDURES)[5:8]


def test_via_is_validated_against_the_deployable_interface(gpp):
    assert validate_via("N11", "SMF", ["AMF"])
    assert validate_via("Nsmf_PDUSession", "SMF", ["AMF"])
    assert validate_via(" n11 ", "SMF", ["AMF"])
    assert not validate_via("N8", "SMF", ["AMF"])
    assert not validate_via("N11", "UE", ["AMF"])                # N1 is not a prefix of N11
    assert not validate_via("", "SMF", ["AMF"])
    assert validate_via("N20", "AMF", ["SMSF"])
    kept = parse_logic({"actors": [{"name": "SMF", "via": "N8"}]}, "T", 1, stored=False)
    assert kept.actors == [Actor("SMF", "N8")]                   # no deployables: kept as given


async def test_apply_logic_and_infer_with_a_new_reply(gpp):
    fg = _fg(NODES)
    t = _traj(support=1, own=1)

    class Fake:
        async def complete(self, system, user, *, tag="", max_tokens=None, **kw):
            assert max_tokens == 4000
            return "```json\n" + json.dumps(NEW_REPLY) + "\n```"

    out = await infer_trajectories(Fake(), fg, [t], {}, 20, "en", {n: 0 for n in NODES})
    bl = out["T001"]
    assert bl.title == "PDU session update" and not bl.error
    assert bl.llm_confidence == "high" and bl.confidence == "medium"   # one walk: not high
    assert bl.steps[1].at == 2 and bl.actors[2].via == ""              # UDM via N11 dropped
    rich = _traj(support=10, own=10)
    bl = BusinessLogic(trajectory="T001")
    apply_logic(bl, {**NEW_REPLY, "confidence": "low"}, fg, rich)
    assert bl.confidence == "low"                                      # min(llm, evidence)
    apply_logic(bl, NEW_REPLY, fg, rich)
    assert bl.confidence == "high"


# ---- malformed and partial replies

def test_malformed_reply_values_do_not_crash(gpp):
    data = {"title": ["a", "b"], "actors": [None, 3, "", {"via": "N11"}, {"name": "UE"},
                                            {"name": "UE"}],
            "steps": [{"action": ""}, "plain", {"message": "Nsmf_X"}, 7,
                      {"action": "go", "at": True}, {"action": "go2", "at": "x"}],
            "alternatives": "if rejected", "state_changes": ["RM up", {"entity": "RM"}],
            "data_objects": {"name": "UE context", "op": 5}, "preconditions": "a; b; c",
            "implements": "Initial registration", "variants": None,
            "trigger_sources": [None, "", "UE"]}
    bl = parse_logic(data, "T", 2, stored=False)
    assert bl.title == '["a", "b"]'
    assert bl.actors == [Actor("3"), Actor("UE")]
    assert [s.action for s in bl.steps] == ["plain", "Nsmf_X", "7", "go", "go2"]
    assert all(s.at is None for s in bl.steps)
    assert bl.alternatives == [Alternative(None, "if rejected", "")]
    assert bl.state_changes == []
    assert bl.data_objects == [DataObject("UE context", "")]
    assert bl.preconditions == ["a", "b"] and bl.implements == ["Initial registration"]
    assert bl.variants == [] and bl.trigger_sources == ["UE"]


def test_unparsed_and_untitled_replies_set_the_error():
    fg, t = _fg(NODES), _traj()
    bl = BusinessLogic(trajectory="T001")
    apply_logic(bl, None, fg, t)
    assert bl.error == "unparsed reply"
    bl = BusinessLogic(trajectory="T001")
    apply_logic(bl, {"summary": "only"}, fg, t)
    assert bl.error == "missing title" and bl.summary == "only"
    assert parse_logic("not a dict", "T", 1, stored=False).title == ""  # type: ignore[arg-type]


# ---- stored json (backward compatibility)

OLD = {"trajectory": "T001", "title": "UE registration", "summary": "s", "trigger": "UE request",
       "actors": ["UE", "gNB/RAN"], "steps": ["receive", "authenticate", "store"],
       "outcome": "registered", "category": "network procedure", "confidence": "medium",
       "error": ""}


def test_old_stored_json_loads_and_maps_steps_only_when_counts_agree():
    bl = BusinessLogic.from_dict(OLD, "T001", 3)
    assert bl.actors == [Actor("UE"), Actor("gNB/RAN")]
    assert [(s.action, s.at) for s in bl.steps] == [("receive", 1), ("authenticate", 2),
                                                    ("store", 3)]
    assert all(s.direction == "internal" and not s.actor for s in bl.steps)
    assert bl.confidence == "medium" and bl.llm_confidence == "" and bl.variants == []
    assert bl.trigger_sources == [] and bl.implements == [] and bl.state_changes == []
    assert all(s.at is None for s in BusinessLogic.from_dict(OLD, "T001", 4).steps)
    with_variants = BusinessLogic.from_dict({**OLD, "variants": []}, "T001", 3)
    assert all(s.at is None for s in with_variants.steps)       # current schema: never mapped
    assert BusinessLogic.from_dict({**OLD, "actors": "UE, AMF"}, "T001", 3).actors == \
        [Actor("UE"), Actor("AMF")]
    assert BusinessLogic.from_dict(None, "T009", 2) == BusinessLogic(trajectory="T009")


def test_stored_confidence_is_not_recomputed_and_round_trips(gpp):
    bl = parse_logic(NEW_REPLY, "T001", 3, stored=False, deployables=["AMF"])
    bl.confidence = "medium"
    d = json.loads(json.dumps(bl.to_dict()))
    assert isinstance(d["actors"][0], dict) and d["steps"][0]["at"] == 1
    assert d["state_changes"][0]["from_state"] == "RM-DEREGISTERED"
    back = BusinessLogic.from_dict(d, "T001", 3)
    assert back == bl
    assert back.variants == ["with handover"] and back.llm_confidence == "high"
    assert back.confidence == "medium"
    d["actors"][0]["via"] = "not validated again"
    assert BusinessLogic.from_dict(d, "T001", 3).actors[0].via == "not validated again"


# ---- prompt

def test_prompt_has_the_domain_sections_after_the_target_level(gpp):
    fg = _fg(NODES)
    _sys, user = trajectory_prompt(fg, _traj(), {}, 20, "en", {n: 0 for n in NODES})
    lines = user.splitlines()
    tags = [x.split("]")[0] + "]" for x in lines if x.startswith("[")]
    i = tags.index("[TARGET LEVEL]")
    assert tags[i + 1: i + 5] == ["[CODE UNDER STUDY]", "[ALLOWED ACTORS]", "[KNOWN PROCEDURES]",
                                  "[WALKS]"]
    code = next(x for x in lines if x.startswith("[CODE UNDER STUDY]"))
    assert "AMF (100 % of root walks)" in code and "never list it as an actor" in code
    allowed = next(x for x in lines if x.startswith("[ALLOWED ACTORS]"))
    names = [a.split(" (")[0] for a in allowed.removeprefix("[ALLOWED ACTORS] ").split(", ")]
    assert "AMF" not in names and "peer AMF" in names                # self suppressed
    assert "SMF (N11)" in allowed and "UE (N1)" in allowed and "NG-RAN (N2)" in allowed
    assert "Kubernetes" in names and "operator" in names
    procs = next(x for x in lines if x.startswith("[KNOWN PROCEDURES]"))
    assert procs == "[KNOWN PROCEDURES] " + "; ".join(PROCEDURES)
    seq = next(x for x in lines if x.startswith("[CLUSTER SEQUENCE]"))
    assert seq.endswith('"at" in your answer refers to these step numbers')


def test_prompt_names_amf_as_an_actor_for_smsf_code(gpp):
    nodes = ["smsf/service::Handle"] + NODES[1:]
    _sys, user = trajectory_prompt(_fg(nodes), _traj(nodes=nodes), {}, 20, "en",
                                   {n: 0 for n in nodes})
    allowed = next(x for x in user.splitlines() if x.startswith("[ALLOWED ACTORS]"))
    names = [a.split(" (")[0] for a in allowed.removeprefix("[ALLOWED ACTORS] ").split(", ")]
    assert "AMF" in names and "SMSF" not in names
    assert "[CODE UNDER STUDY] the entry roots live in: SMSF (100 % of root walks)" in user


def test_system_prompt_has_the_new_schema_and_rules(gpp):
    system = _system("bizlogic_trajectory_system")
    assert system.startswith("You infer the BUSINESS LOGIC realised by one behaviour trajectory "
                             "of a large codebase (a 3GPP 5G core network).")
    for key in ('"short_title"', '"scope"', '"trigger_sources"', '"via"', '"direction"',
                '"alternatives"', '"state_changes"', '"data_objects"', '"implements"',
                '"supports"', "[CODE UNDER STUDY]", "[ALLOWED ACTORS]", "[KNOWN PROCEDURES]",
                '"Kubernetes operator"', "Use \"\" or [] rather than guessing.",
                "for CM use CM-IDLE/CM-CONNECTED", "RM | CM | NF registration | PDU session",
                '"implements", "supports", and states.'):
        assert key in system, key
    assert "Write at the TARGET LEVEL first" in system and "${" not in system
    assert system.rstrip().endswith("and states.")
    for name in ("bizlogic_cluster_system", "bizlogic_parent_system"):
        assert " a large codebase (a 3GPP 5G core network). " in _system(name)


# ---- without a profile: domain-neutral prompts

def test_default_system_prompts_are_domain_neutral():
    assert D.active().empty
    for name in ("bizlogic_trajectory_system", "bizlogic_cluster_system",
                 "bizlogic_parent_system"):
        system = _system(name)
        assert "large codebase." in system and "${" not in system
        assert not DOMAIN_WORDS.search(system), (name, DOMAIN_WORDS.search(system))
    system = _system("bizlogic_trajectory_system")
    assert "[ALLOWED ACTORS]" not in system and '"supports"' not in system
    assert 'each named by its role in a few words' in system
    assert '"trigger_sources", and states.' in system


def test_default_prompt_has_no_actor_or_procedure_lists():
    fg = _fg(NODES)
    _sys, user = trajectory_prompt(fg, _traj(), {}, 20, "en", {n: 0 for n in NODES})
    assert "[ALLOWED ACTORS]" not in user and "PROCEDURES]" not in user
    assert domain_lines(_traj()) == [
        "[CODE UNDER STUDY] the entry roots live in: amf (100 % of root walks). This code is "
        "the system being described: never list it as an actor."]


def test_default_parse_keeps_free_text_actors_and_drops_procedures():
    bl = parse_logic(NEW_REPLY, "T001", 3, stored=False, deployables=["amf"])
    assert [a.name for a in bl.actors] == ["UE", "SMF", "UDM"]
    assert all(a.via == "" for a in bl.actors)                 # no interface table: no via
    assert bl.implements == [] and bl.supports == []           # no procedure list
    assert bl.state_changes[0] == StateChange("RM", "5GMM-DEREGISTERED", "REGISTERED", 3,
                                              "Save")          # no aliases
    stored = BusinessLogic.from_dict({**NEW_REPLY, "implements": ["Initial registration"]},
                                     "T001", 3)
    assert stored.implements == []
