"""the active domain tables for the bizlogic prompts and diagrams: deployables, interfaces,
procedures, states.

empty by default (see `profile`): deployables fall back to top directories, no interface,
procedure or state vocabulary. `use_profile` fills the tables in place, so modules that
imported them see the active profile.
"""

from __future__ import annotations

import re

from codocs.bizlogic.profile import DEFAULT, DomainProfile

# top dir (or longer prefix) of a node id's package -> deployable display name
DEPLOYABLES: dict[str, str] = {}
# canonical actor -> the deployable that IS that party (self-suppression and colocation)
SELF: dict[str, str] = {}
# interfaces from each deployable's point of view; "*" = any party in WILDCARD_PARTIES
INTERFACES: dict[str, dict[str, str]] = {}
WILDCARD_PARTIES: set[str] = set()
# (pkg prefix, interface label, canonical actors it evidences); longest prefix wins
PKG_EVIDENCE: list[tuple[str, str, frozenset[str]]] = []
# roots under these prefixes are controllers, started by api events, never by a person
CONTROLLER_ROOTS: list[str] = []
# prompt name -> canonical, in [ALLOWED ACTORS] order
PROMPT_ACTORS: list[tuple[str, str]] = []
# the llm may only name these in implements / supports; name -> (reference, family)
PROCEDURES: dict[str, tuple[str, str]] = {}
DATA_OBJECTS: list[str] = []
# D11 state vocabulary, aliases and the entity patterns that pick a vocabulary
STATE_VOCAB: dict[str, tuple[str, ...]] = {}
STATE_ALIASES: dict[str, str] = {}
_ENTITY_RX: list[tuple[str, re.Pattern]] = []
# message verification: party names, word aliases, short words kept
PARTY_TOKENS: set[str] = set()
TOKEN_ALIASES: dict[str, str] = {}
SHORT_TOKENS: set[str] = set()
# sequence diagrams: lifelines kept side by side (first, second, note), message prefixes
ADJACENT_LIFELINES: list[tuple[str, str, str]] = []
MESSAGE_PREFIXES: dict[str, str] = {}

_PROC_KEYS: dict[str, str] = {}
_ACTIVE: list[DomainProfile] = [DEFAULT]
_LISTENERS: list = []


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _refill(table, items) -> None:
    if isinstance(table, dict):
        table.clear()
        table.update(items)
    elif isinstance(table, set):
        table.clear()
        table.update(items)
    else:
        table[:] = list(items)


def use_profile(p: DomainProfile | None) -> DomainProfile:
    """make `p` (None = the empty default) the active profile; returns the previous one."""
    p = p or DEFAULT
    prev = _ACTIVE[0]
    _ACTIVE[0] = p
    _refill(DEPLOYABLES, p.deployables)
    _refill(SELF, p.self_map)
    _refill(INTERFACES, {k: dict(v) for k, v in p.interfaces.items()})
    _refill(WILDCARD_PARTIES, p.wildcard_parties)
    _refill(PKG_EVIDENCE, p.pkg_evidence)
    _refill(CONTROLLER_ROOTS, p.controller_roots)
    _refill(PROMPT_ACTORS, p.prompt_actors)
    _refill(PROCEDURES, p.procedures)
    _refill(DATA_OBJECTS, p.data_objects)
    _refill(STATE_VOCAB, p.states)
    _refill(STATE_ALIASES, p.state_aliases)
    _refill(_ENTITY_RX, [(k, re.compile(rx, 0 if cs else re.I))
                         for k, rx, cs in p.state_entities])
    _refill(PARTY_TOKENS, p.party_tokens)
    _refill(TOKEN_ALIASES, p.token_aliases)
    _refill(SHORT_TOKENS, p.short_tokens)
    _refill(ADJACENT_LIFELINES, p.adjacent_lifelines)
    _refill(MESSAGE_PREFIXES, p.message_prefixes)
    _refill(_PROC_KEYS, {_fold(k): k for k in p.procedures})
    for fn in _LISTENERS:
        fn(p)
    return prev


def active() -> DomainProfile:
    """the active profile."""
    return _ACTIVE[0]


def on_profile_change(fn) -> None:
    """call fn(profile) now and after every `use_profile` (derived tables of other modules)."""
    _LISTENERS.append(fn)
    fn(_ACTIVE[0])


def interface(deployable: str, canonical: str) -> str:
    """the interface `canonical` uses towards `deployable`: exact, then "*" (wildcard
    parties only), else ""."""
    row = INTERFACES.get(deployable, {})
    if canonical in row:
        return row[canonical]
    return row.get("*", "") if canonical in WILDCARD_PARTIES else ""


def ref_point(label: str) -> str:
    """the reference-point part of an interface label: "API (REST)" -> "API"."""
    return label.split(" (", 1)[0].strip()


def procedure_key(value: str) -> str:
    """the PROCEDURES key `value` names (case and spaces ignored), else ""."""
    return _PROC_KEYS.get(_fold(value), "")


def state_entity(entity: str) -> str:
    """the STATE_VOCAB key whose profile pattern matches an llm entity name (e.g. "order
    state" -> "Order"), else ""."""
    for key, rx in _ENTITY_RX:
        if rx.search(entity or ""):
            return key
    return ""


def norm_state(entity: str, s: str) -> str:
    """upper-case, spaces/underscores -> "-", and STATE_ALIASES whose target carries the
    entity's vocabulary key as prefix ("idle" -> "CM-IDLE" for a CM entity)."""
    out = re.sub(r"[\s_]+", "-", str(s or "").strip().upper())
    kind = state_entity(entity)
    target = STATE_ALIASES.get(out, "")
    if kind and target.startswith(kind + "-"):
        return target
    return out
