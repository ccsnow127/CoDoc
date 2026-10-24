"""domain profiles of the bizlogic step: loading by name and by path, validation errors, the
built-in example, and the --domain-profile option of `codocs bizlogic`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from codocs.bizlogic import actors as A
from codocs.bizlogic import domain as D
from codocs.bizlogic.profile import (
    BUILTIN_DIR,
    DEFAULT,
    DomainProfile,
    ProfileError,
    builtin_profiles,
    from_dict,
    load_profile,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

MINIMAL = {
    "name": "shop",
    "system_description": "an online shop",
    "deployables": {"checkout": "Checkout service", "billing": "Billing service"},
    "self": {"Payment provider": "Billing service"},
    "actors": [
        {"name": "Customer", "kind": "role", "pattern": r"\bcustomers?\b|\bbuyers?\b",
         "primary": True, "aliases": ["customer", "buyer"]},
        {"name": "Payment provider", "kind": "system",
         "pattern": r"payment (provider|gateway)|psp"},
    ],
    "interfaces": {"Checkout service": {"Customer": "HTTPS (REST)",
                                        "Payment provider": "PSP API (REST)"}},
    "pkg_evidence": [{"prefix": "checkout/psp", "interface": "PSP API (REST)",
                      "actors": ["Payment provider"]}],
    "prompt_actors": ["Customer", {"name": "payment gateway", "actor": "Payment provider"},
                      {"name": "operator", "actor": "Operator / OAM"}],
    "procedures": [{"name": "Place order", "reference": "shop handbook 2.1",
                    "family": "Ordering"}],
    "states": {"Order": ["NEW", "PAID", "SHIPPED"]},
    "state_entities": [{"state": "Order", "pattern": r"\border\b"}],
    "plumbing": "Metrics$",
}


@pytest.fixture
def restore():
    prev = D.active()
    yield
    D.use_profile(prev)


# ---- loading ---------------------------------------------------------------------------

def test_builtin_profiles_and_default():
    assert builtin_profiles() == ["3gpp-5gc"]
    for spec in (None, "", "none", " None "):
        assert load_profile(spec) is DEFAULT
    assert DEFAULT.empty and DomainProfile(name="x", description="y").empty
    assert not load_profile("3gpp-5gc").empty


def test_load_by_name_and_by_path(tmp_path):
    p = load_profile("3gpp-5gc")
    assert p.name == "3gpp-5gc" and p.system_description == "a 3GPP 5G core network"
    assert load_profile(BUILTIN_DIR / "3gpp-5gc.json") == p
    f = tmp_path / "my-shop.json"
    f.write_text(json.dumps({k: v for k, v in MINIMAL.items() if k != "name"}))
    shop = load_profile(str(f))
    assert shop.name == "my-shop"                               # defaults to the file name
    assert shop.deployables["checkout"] == "Checkout service"
    assert shop.procedures == {"Place order": ("shop handbook 2.1", "Ordering")}
    assert shop.prompt_actors[1] == ("payment gateway", "Payment provider")
    assert from_dict(shop.to_dict()) == shop and from_dict(p.to_dict()) == p


def test_a_custom_profile_drives_actors_prompts_and_states(restore):
    from codocs.bizlogic.infer import _system, prompt_vars

    D.use_profile(from_dict(MINIMAL))
    assert [c.name for c in A.normalize("buyer via web")] == ["Customer"]
    assert A.normalize("PSP")[0].kind == "system"
    assert A.deployable_of("checkout/cart::Add") == "Checkout service"
    assert A.allowed_actors(["Checkout service"]) == \
        "Customer (HTTPS), payment gateway (PSP API), operator"
    assert A.pkg_evidence(["checkout/psp/client::Pay"]) == {"Payment provider":
                                                             "PSP API (REST)"}
    assert A.bl_actors(["payment provider"], ["Billing service"]) == []     # self
    assert D.procedure_key("place ORDER") == "Place order"
    assert D.state_entity("customer order") == "Order"
    assert "Customer" in A.DEFAULT_PRIMARY and "customer" in A.ALIASES["Customer"]
    system = _system("bizlogic_trajectory_system")
    assert "large codebase (an online shop)." in system
    assert "For Order use NEW/PAID/SHIPPED." in system
    assert prompt_vars()["ENTITIES"] == "Order | other"


# ---- validation ------------------------------------------------------------------------

@pytest.mark.parametrize("data,message", [
    ([], "top level: expected an object"),
    ({"colour": "red"}, "top level: unknown field(s) colour"),
    ({"deployables": ["amf"]}, "deployables: expected an object"),
    ({"deployables": {"amf": 1}}, "deployables.amf: expected a string"),
    ({"actors": [{"name": "X"}]}, "actors[0]: missing pattern"),
    ({"actors": [{"name": "X", "pattern": "(", "kind": "nf"}]}, "actors[0].pattern: invalid regex"),
    ({"actors": [{"name": "X", "pattern": "x", "kind": "robot"}]}, "actors[0].kind: 'robot'"),
    ({"actors": [{"name": "_x", "pattern": "x"}]}, "is not a usable actor name"),
    ({"actors": [{"name": "X", "pattern": "x", "primary": "yes"}]}, "expected true or false"),
    ({"self": {"X": "Nowhere"}}, "self.X: 'Nowhere' is not a deployables value"),
    ({"interfaces": {"A": {"Ghost": "N1"}}}, "interfaces.A.Ghost: not an actor name"),
    ({"prompt_actors": ["Ghost"]}, "prompt_actors: 'Ghost' is not an actor name"),
    ({"procedures": [{"name": "P"}]}, "procedures[0]: missing family"),
    ({"states": {"S": ["A"]}, "state_entities": [{"state": "T", "pattern": "t"}]},
     "state_entities[0].state: 'T' is not a key of states"),
    ({"plumbing": "("}, "plumbing: invalid regex"),
])
def test_invalid_profiles_name_the_field(data, message):
    with pytest.raises(ProfileError) as e:
        from_dict(data, "test.json")
    assert str(e.value).startswith("domain profile test.json: ")
    assert message in str(e.value)


def test_unknown_and_unreadable_profiles(tmp_path):
    with pytest.raises(ProfileError, match=r"unknown domain profile 'nope': not a built-in "
                                           r"profile \(built-in: 3gpp-5gc\) and no such file"):
        load_profile("nope")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ProfileError, match="invalid json"):
        load_profile(bad)
    assert isinstance(ProfileError("x"), ValueError)          # the cli prints it, exit 1


# ---- the built-in example ---------------------------------------------------------------

def test_builtin_profile_is_public_3gpp_vocabulary_only():
    p = load_profile("3gpp-5gc")
    nfs = {"amf", "smf", "smsf", "ausf", "udm", "udr", "pcf", "nrf", "nssf", "chf", "upf"}
    assert set(p.deployables) <= nfs and set(p.deployables.values()) == {x.upper()
                                                                      for x in p.deployables}
    assert not p.pkg_evidence and not p.controller_roots and not p.plumbing
    assert all(ref.startswith("TS ") for ref, _ in p.procedures.values())
    assert set(p.states) == {"RM", "CM", "NF registration", "PDU session"}
    # every party is a 3gpp function or role, or one of the generic built-in actors
    assert {r.kind for r in p.actors} <= {"nf", "system", "generic"}
    assert {c for _, c in p.prompt_actors} - {r.name for r in p.actors} <= set(A.BUILTIN_ACTORS)


# ---- the cli option ---------------------------------------------------------------------

def _invoke(*args):
    """exit code and the output with rich's line wrapping undone."""
    from codocs.cli import cli
    res = CliRunner().invoke(cli, [str(a) for a in args])
    return res.exit_code, " ".join(res.output.split())


def test_cli_help_lists_the_option():
    for cmd in ("bizlogic", "bizlogic-render"):
        code, out = _invoke(cmd, "--help")
        assert code == 0 and "--domain-profile NAME|PATH" in out, cmd
    assert "(e.g. 3gpp-5gc)" in _invoke("bizlogic", "--help")[1]


def test_cli_bizlogic_plan_only_with_and_without_a_profile(tmp_path, restore):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "gomod", repo)
    code, out = _invoke("bizlogic", repo, "-o", tmp_path / "plain", "--plan-only", "-k", "2")
    assert code == 0, out
    assert not (tmp_path / "plain" / "bizlogic" / "domain_profile.json").exists()
    code, out = _invoke("bizlogic", repo, "-o", tmp_path / "gpp", "--plan-only", "-k", "2",
                        "--domain-profile", "3gpp-5gc")
    assert code == 0, out
    stored = json.loads((tmp_path / "gpp" / "bizlogic" / "domain_profile.json").read_text())
    assert stored == load_profile("3gpp-5gc").to_dict()
    f = tmp_path / "shop.json"
    f.write_text(json.dumps(MINIMAL))
    code, out = _invoke("bizlogic", repo, "-o", tmp_path / "shop", "--plan-only", "-k", "2",
                        "--domain-profile", f)
    assert code == 0, out
    stored = json.loads((tmp_path / "shop" / "bizlogic" / "domain_profile.json").read_text())
    assert stored["name"] == "shop"
    code, out = _invoke("bizlogic", repo, "-o", tmp_path / "bad", "--plan-only",
                        "--domain-profile", "no-such-profile")
    assert code == 1 and "unknown domain profile 'no-such-profile'" in out
    assert not (tmp_path / "bad").exists()                    # failed before any work
    f.write_text(json.dumps({"deployables": {"x": 1}}))
    code, out = _invoke("bizlogic", repo, "-o", tmp_path / "bad", "--plan-only",
                        "--domain-profile", f)
    assert code == 1 and "deployables.x: expected a string, got int" in out
    assert D.active().empty
