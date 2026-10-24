"""domain profiles: optional vocabulary for the bizlogic prompts and diagrams.

the default profile is empty: deployables are top directories, actors are free text
normalised by the generic rules in `actors`, and the prompts carry no procedure, actor or
state lists. a profile (a json file, or a built-in name from profiles/*.json) adds named
deployables, outside parties and their interfaces, procedures and state vocabularies.
docs/bizlogic.md documents every field.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent / "profiles"
ACTOR_KINDS = ("nf", "system", "role", "infra", "generic")


class ProfileError(ValueError):
    """an unknown, unreadable or malformed domain profile."""


@dataclass(frozen=True)
class ActorRule:
    name: str                     # canonical actor, e.g. "Payment provider"
    kind: str                     # nf | system | role | infra | generic
    pattern: str                  # regex searched in the llm's actor strings
    case_sensitive: bool = False
    unless: str = ""              # skip this rule when that canonical already matched
    label: str = ""               # display label ("" -> name)
    aliases: tuple[str, ...] = ()  # literal phrases searched in trigger text
    case_sensitive_aliases: tuple[str, ...] = ()   # the same, matched case-sensitively
    primary: bool = False         # drawn left of the system when old data has no sources
    device: bool = False          # «device» box instead of «system»


@dataclass(frozen=True)
class DomainProfile:
    name: str = ""
    description: str = ""
    system_description: str = ""                  # "(...)" after "a large codebase"
    deployables: dict[str, str] = field(default_factory=dict)       # pkg prefix -> name
    self_map: dict[str, str] = field(default_factory=dict)          # actor -> deployable
    interfaces: dict[str, dict[str, str]] = field(default_factory=dict)
    wildcard_parties: tuple[str, ...] = ()
    pkg_evidence: tuple[tuple[str, str, frozenset[str]], ...] = ()
    controller_roots: tuple[str, ...] = ()
    actors: tuple[ActorRule, ...] = ()
    prompt_actors: tuple[tuple[str, str], ...] = ()                 # (prompt name, actor)
    procedures: dict[str, tuple[str, str]] = field(default_factory=dict)  # (ref, family)
    ops_family: str = ""
    data_objects: tuple[str, ...] = ()
    states: dict[str, tuple[str, ...]] = field(default_factory=dict)
    state_aliases: dict[str, str] = field(default_factory=dict)
    state_entities: tuple[tuple[str, str, bool], ...] = ()  # (state key, regex, case_sens.)
    plumbing: str = ""
    party_tokens: frozenset[str] = frozenset()
    token_aliases: dict[str, str] = field(default_factory=dict)
    short_tokens: frozenset[str] = frozenset()
    adjacent_lifelines: tuple[tuple[str, str, str], ...] = ()       # (first, second, note)
    message_prefixes: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """no tables at all (a name or description alone changes nothing)."""
        return replace(self, name="", description="") == DomainProfile()

    def to_dict(self) -> dict:
        """the json form `from_dict` reads back."""
        d: dict = {"name": self.name}
        for key, val in (
                ("description", self.description),
                ("system_description", self.system_description),
                ("deployables", dict(self.deployables)), ("self", dict(self.self_map)),
                ("interfaces", {k: dict(v) for k, v in self.interfaces.items()}),
                ("wildcard_parties", list(self.wildcard_parties)),
                ("pkg_evidence", [{"prefix": p, "interface": i, "actors": sorted(a)}
                                  for p, i, a in self.pkg_evidence]),
                ("controller_roots", list(self.controller_roots)),
                ("actors", [_rule_dict(r) for r in self.actors]),
                ("prompt_actors", [n if n == c else {"name": n, "actor": c}
                                   for n, c in self.prompt_actors]),
                ("procedures", [{"name": n, "reference": r, "family": f}
                                for n, (r, f) in self.procedures.items()]),
                ("ops_family", self.ops_family), ("data_objects", list(self.data_objects)),
                ("states", {k: list(v) for k, v in self.states.items()}),
                ("state_aliases", dict(self.state_aliases)),
                ("state_entities", [_entity_dict(k, p, c) for k, p, c in self.state_entities]),
                ("plumbing", self.plumbing), ("party_tokens", sorted(self.party_tokens)),
                ("token_aliases", dict(self.token_aliases)),
                ("short_tokens", sorted(self.short_tokens)),
                ("adjacent_lifelines", [{"first": a, "second": b, "note": n}
                                        for a, b, n in self.adjacent_lifelines]),
                ("message_prefixes", dict(self.message_prefixes))):
            if val:
                d[key] = val
        return d


def _rule_dict(r: ActorRule) -> dict:
    d = {"name": r.name, "kind": r.kind, "pattern": r.pattern}
    for key in ("case_sensitive", "unless", "label", "primary", "device"):
        if getattr(r, key):
            d[key] = getattr(r, key)
    if r.aliases:
        d["aliases"] = list(r.aliases)
    if r.case_sensitive_aliases:
        d["case_sensitive_aliases"] = list(r.case_sensitive_aliases)
    return d


def _entity_dict(key: str, pattern: str, case_sensitive: bool) -> dict:
    d = {"state": key, "pattern": pattern}
    if case_sensitive:
        d["case_sensitive"] = True
    return d


DEFAULT = DomainProfile()

_FIELDS = {"name", "description", "system_description", "deployables", "self", "interfaces",
           "wildcard_parties", "pkg_evidence", "controller_roots", "actors", "prompt_actors",
           "procedures", "ops_family", "data_objects", "states", "state_aliases",
           "state_entities", "plumbing", "party_tokens", "token_aliases", "short_tokens",
           "adjacent_lifelines", "message_prefixes"}


class _Check:
    """type checks that name the offending field."""

    def __init__(self, src: str):
        self.src = src

    def fail(self, where: str, what: str):
        raise ProfileError(f"domain profile {self.src}: {where}: {what}")

    def text(self, v, where: str) -> str:
        if not isinstance(v, str):
            self.fail(where, f"expected a string, got {type(v).__name__}")
        return v

    def flag(self, v, where: str) -> bool:
        if not isinstance(v, bool):
            self.fail(where, f"expected true or false, got {type(v).__name__}")
        return v

    def obj(self, v, where: str) -> dict:
        if not isinstance(v, dict):
            self.fail(where, f"expected an object, got {type(v).__name__}")
        return v

    def seq(self, v, where: str) -> list:
        if not isinstance(v, list):
            self.fail(where, f"expected a list, got {type(v).__name__}")
        return v

    def texts(self, v, where: str) -> list[str]:
        return [self.text(x, f"{where}[{i}]") for i, x in enumerate(self.seq(v, where))]

    def text_map(self, v, where: str) -> dict[str, str]:
        return {k: self.text(x, f"{where}.{k}") for k, x in self.obj(v, where).items()}

    def regex(self, v, where: str, flags: int = 0) -> str:
        s = self.text(v, where)
        try:
            re.compile(s, flags)
        except re.error as e:
            self.fail(where, f"invalid regex {s!r}: {e}")
        return s

    def keys(self, d: dict, where: str, required: set[str], optional: set[str]) -> None:
        missing = sorted(required - set(d))
        if missing:
            self.fail(where, f"missing {', '.join(missing)}")
        extra = sorted(set(d) - required - optional)
        if extra:
            self.fail(where, f"unknown field(s) {', '.join(extra)}")


def from_dict(data, source: str = "<dict>") -> DomainProfile:
    """a validated profile; raises ProfileError naming the field at fault."""
    ck = _Check(source)
    d = ck.obj(data, "top level")
    ck.keys(d, "top level", set(), _FIELDS)
    kw: dict = {}
    for key in ("name", "description", "system_description", "ops_family"):
        if key in d:
            kw[key] = ck.text(d[key], key).strip()
    if "deployables" in d:
        kw["deployables"] = ck.text_map(d["deployables"], "deployables")
    if "self" in d:
        kw["self_map"] = ck.text_map(d["self"], "self")
    if "interfaces" in d:
        kw["interfaces"] = {k: ck.text_map(v, f"interfaces.{k}")
                            for k, v in ck.obj(d["interfaces"], "interfaces").items()}
    for key in ("wildcard_parties", "controller_roots", "data_objects"):
        if key in d:
            kw[key] = tuple(ck.texts(d[key], key))
    if "pkg_evidence" in d:
        rows = []
        for i, e in enumerate(ck.seq(d["pkg_evidence"], "pkg_evidence")):
            w = f"pkg_evidence[{i}]"
            ck.keys(ck.obj(e, w), w, {"prefix", "interface", "actors"}, set())
            rows.append((ck.text(e["prefix"], w + ".prefix").strip("/"),
                         ck.text(e["interface"], w + ".interface"),
                         frozenset(ck.texts(e["actors"], w + ".actors"))))
        kw["pkg_evidence"] = tuple(rows)
    if "actors" in d:
        kw["actors"] = tuple(_actor_rule(ck, e, f"actors[{i}]")
                             for i, e in enumerate(ck.seq(d["actors"], "actors")))
    if "prompt_actors" in d:
        pa = []
        for i, e in enumerate(ck.seq(d["prompt_actors"], "prompt_actors")):
            w = f"prompt_actors[{i}]"
            if isinstance(e, str):
                pa.append((e, e))
                continue
            ck.keys(ck.obj(e, w), w, {"name", "actor"}, set())
            pa.append((ck.text(e["name"], w + ".name"), ck.text(e["actor"], w + ".actor")))
        kw["prompt_actors"] = tuple(pa)
    if "procedures" in d:
        procs: dict[str, tuple[str, str]] = {}
        for i, e in enumerate(ck.seq(d["procedures"], "procedures")):
            w = f"procedures[{i}]"
            ck.keys(ck.obj(e, w), w, {"name", "family"}, {"reference"})
            procs[ck.text(e["name"], w + ".name")] = (
                ck.text(e.get("reference", ""), w + ".reference"),
                ck.text(e["family"], w + ".family"))
        kw["procedures"] = procs
    if "states" in d:
        kw["states"] = {k: tuple(ck.texts(v, f"states.{k}"))
                        for k, v in ck.obj(d["states"], "states").items()}
    if "state_aliases" in d:
        kw["state_aliases"] = ck.text_map(d["state_aliases"], "state_aliases")
    if "state_entities" in d:
        ents = []
        for i, e in enumerate(ck.seq(d["state_entities"], "state_entities")):
            w = f"state_entities[{i}]"
            ck.keys(ck.obj(e, w), w, {"state", "pattern"}, {"case_sensitive"})
            cs = ck.flag(e.get("case_sensitive", False), w + ".case_sensitive")
            ents.append((ck.text(e["state"], w + ".state"),
                         ck.regex(e["pattern"], w + ".pattern", 0 if cs else re.I), cs))
        kw["state_entities"] = tuple(ents)
    if "plumbing" in d:
        kw["plumbing"] = ck.regex(d["plumbing"], "plumbing")
    for key in ("party_tokens", "short_tokens"):
        if key in d:
            kw[key] = frozenset(x.lower() for x in ck.texts(d[key], key))
    if "token_aliases" in d:
        kw["token_aliases"] = {k.lower(): v.lower() for k, v in
                               ck.text_map(d["token_aliases"], "token_aliases").items()}
    if "adjacent_lifelines" in d:
        rows = []
        for i, e in enumerate(ck.seq(d["adjacent_lifelines"], "adjacent_lifelines")):
            w = f"adjacent_lifelines[{i}]"
            ck.keys(ck.obj(e, w), w, {"first", "second"}, {"note"})
            rows.append((ck.text(e["first"], w + ".first"), ck.text(e["second"], w + ".second"),
                         ck.text(e.get("note", ""), w + ".note")))
        kw["adjacent_lifelines"] = tuple(rows)
    if "message_prefixes" in d:
        kw["message_prefixes"] = ck.text_map(d["message_prefixes"], "message_prefixes")
    p = DomainProfile(**kw)
    _cross_check(ck, p)
    return p


def _actor_rule(ck: _Check, e, where: str) -> ActorRule:
    ck.keys(ck.obj(e, where), where, {"name", "pattern"},
            {"kind", "case_sensitive", "unless", "label", "aliases", "case_sensitive_aliases",
             "primary", "device"})
    name = ck.text(e["name"], where + ".name").strip()
    if not name or name.startswith("_"):
        ck.fail(where + ".name", f"{name!r} is not a usable actor name")
    kind = ck.text(e.get("kind", "system"), where + ".kind")
    if kind not in ACTOR_KINDS:
        ck.fail(where + ".kind", f"{kind!r} is not one of {', '.join(ACTOR_KINDS)}")
    cs = ck.flag(e.get("case_sensitive", False), where + ".case_sensitive")
    return ActorRule(
        name=name, kind=kind, pattern=ck.regex(e["pattern"], where + ".pattern", 0 if cs else re.I),
        case_sensitive=cs, unless=ck.text(e.get("unless", ""), where + ".unless"),
        label=ck.text(e.get("label", ""), where + ".label"),
        aliases=tuple(ck.texts(e.get("aliases", []), where + ".aliases")),
        case_sensitive_aliases=tuple(ck.texts(e.get("case_sensitive_aliases", []),
                                              where + ".case_sensitive_aliases")),
        primary=ck.flag(e.get("primary", False), where + ".primary"),
        device=ck.flag(e.get("device", False), where + ".device"))


def _cross_check(ck: _Check, p: DomainProfile) -> None:
    """names that refer to other tables must exist there."""
    from codocs.bizlogic.actors import BUILTIN_ACTORS

    actors = {r.name for r in p.actors} | set(BUILTIN_ACTORS)
    deps = set(p.deployables.values())
    for a, dep in p.self_map.items():
        if dep not in deps:
            ck.fail(f"self.{a}", f"{dep!r} is not a deployables value")
    for dep, row in p.interfaces.items():
        for a in row:
            if a != "*" and a not in actors:
                ck.fail(f"interfaces.{dep}.{a}", "not an actor name (actors[].name)")
    for where, names in (("wildcard_parties", p.wildcard_parties),
                         ("prompt_actors", [c for _, c in p.prompt_actors]),
                         ("pkg_evidence", [a for _, _, s in p.pkg_evidence for a in sorted(s)])):
        for a in names:
            if a not in actors:
                ck.fail(where, f"{a!r} is not an actor name (actors[].name)")
    for i, (key, _, _) in enumerate(p.state_entities):
        if key not in p.states:
            ck.fail(f"state_entities[{i}].state", f"{key!r} is not a key of states")


def builtin_profiles() -> list[str]:
    """names of the profiles shipped in profiles/*.json."""
    return sorted(f.stem for f in BUILTIN_DIR.glob("*.json"))


def load_profile(spec: str | Path | None) -> DomainProfile:
    """a built-in profile name, a path to a json file, or None / "" / "none" (the empty
    default)."""
    if spec is None or str(spec).strip().lower() in ("", "none"):
        return DEFAULT
    s = str(spec).strip()
    path = Path(s).expanduser()
    if not path.is_file():
        if s in builtin_profiles():
            path = BUILTIN_DIR / f"{s}.json"
        else:
            known = ", ".join(builtin_profiles()) or "none"
            raise ProfileError(f"unknown domain profile {s!r}: not a built-in profile "
                               f"(built-in: {known}) and no such file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ProfileError(f"domain profile {path}: cannot read: {e}") from e
    except json.JSONDecodeError as e:
        raise ProfileError(f"domain profile {path}: invalid json: {e}") from e
    p = from_dict(data, str(path))
    return p if p.name else replace(p, name=path.stem)
