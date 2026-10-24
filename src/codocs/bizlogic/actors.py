"""actor normalisation: raw llm actor strings -> canonical parties, initiators, deployables.

pure functions over the stored json; no llm. every raw string maps to a canonical actor,
an internal component or data ("_internal" / "_data", never drawn), or an "unknown" actor.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

from codocs.bizlogic import domain as D
from codocs.bizlogic.domain import (
    CONTROLLER_ROOTS,
    DEPLOYABLES,
    PKG_EVIDENCE,
    PROMPT_ACTORS,
    SELF,
    interface,
    ref_point,
)


@dataclass(frozen=True)
class Canon:
    name: str         # canonical key, e.g. "Operator / OAM"
    kind: str         # nf | system | role | colocated | infra | generic | unknown | data
                      # | internal | timer | process | event | various | unknown_trigger
    label: str = ""   # display label; "" -> name
    pos: int = 0
    members: tuple[str, ...] = ()   # kind == "various": the starters found

    @property
    def display(self) -> str:
        return self.label or self.name


HUMAN = {"Operator / OAM"}                                   # only stick figure
DRAWABLE = {"nf", "system", "role", "colocated", "generic", "unknown"}
HIDDEN = {"data", "internal"}

K8S_OP = (r"(kubernetes|k8s)[^,]*\boperator|operator (process|controller|pod|startup|initiali\w*)"
          r"|reconcil|custom ?resource|\bcrd\b")
# (canonical, kind, pattern, flags, unless): ordered; `unless` skips the rule when that
# canonical already matched. the active profile's rules go between _HEAD and _TAIL.
_HEAD = [
    ("Kubernetes operator", "system", K8S_OP, re.I, None),
    ("Operator / OAM", "role",
     r"\boperators?\b(?=\s*(/|,|;|\bor\b|\band\b|$))"
     r"|operator[^,]*\b(command|cli|oam|o&m|maintenance|audit|craft|console)\b"
     r"|\boam\b|\bo&m\b|craft (terminal|user)|maintenance (cli|command|user)",
     re.I, "Kubernetes operator"),
]
_TAIL = [
    ("Monitoring / observability", "system",
     r"monitor|observab|metrics|prometheus|traffic measurement", re.I, None),
    ("DNS server", "infra", r"^dns$|\bdns (server|resolver)", re.I, None),
    ("Load balancer", "infra", r"load balancer", re.I, None),
    ("Kubernetes platform", "infra", r"kubernetes|\bk8s\b|paas", re.I, "Kubernetes operator"),
    ("Key-value store", "infra", r"key-value|\bkv[ -]?stores?\b|redis|data store", re.I, None),
    ("_data", "data",
     r"context data|session contexts?|\btables?\b|\bcache\b|\bmemory\b|\bdatabase\b", re.I,
     None),
    ("_internal", "internal",
     r"internal|timer service|subsystem|runtime|test framework|under audit|control processes"
     r"|platform services", re.I, None),
    ("Other services", "generic",
     r"partner services|remote http peer|callback (notification )?consumer|callback endpoint"
     r"|external (services?|systems?)", re.I, None),
]
BUILTIN_ACTORS = [name for name, *_ in _HEAD + _TAIL if not name.startswith("_")]
_BUILTIN_ALIASES = {
    "Operator / OAM": ["operator command", "maintenance command", "OAM", "O&M", "CLI"],
    "Kubernetes operator": ["reconciliation", "operator startup", "custom resource"],
}

# derived from the active profile (filled in place by _configure)
RULES: list[tuple[str, str, re.Pattern, str | None]] = []
KINDS: dict[str, str] = {}
LABELS: dict[str, str] = {}
DEVICE: set[str] = set()                    # «device» box
DEFAULT_PRIMARY: set[str] = set()           # results without trigger_sources only
# canonical -> literal phrases searched in trigger text (results without trigger_sources);
# a phrase in CASE_SENSITIVE_ALIASES (e.g. "AN") matches case-sensitively
ALIASES: dict[str, list[str]] = {}
CASE_SENSITIVE_ALIASES: set[str] = set()
_DEP_KEYS: list[str] = []


def _configure(p) -> None:
    rules = list(_HEAD)
    rules += [(r.name, r.kind, r.pattern, 0 if r.case_sensitive else re.I, r.unless or None)
              for r in p.actors]
    rules += _TAIL
    RULES[:] = [(name, kind, re.compile(rx, flags), unless)
                for name, kind, rx, flags, unless in rules]
    KINDS.clear()
    for name, kind, *_ in rules:
        KINDS.setdefault(name, kind)
    LABELS.clear()
    LABELS.update({r.name: r.label for r in p.actors if r.label})
    DEVICE.clear()
    DEVICE.update(r.name for r in p.actors if r.device)
    DEFAULT_PRIMARY.clear()
    DEFAULT_PRIMARY.update({"Operator / OAM"} | {r.name for r in p.actors if r.primary})
    ALIASES.clear()
    ALIASES.update({k: list(v) for k, v in _BUILTIN_ALIASES.items()})
    CASE_SENSITIVE_ALIASES.clear()
    for r in p.actors:
        for x in r.aliases + r.case_sensitive_aliases:
            if x not in ALIASES.setdefault(r.name, []):
                ALIASES[r.name].append(x)
        CASE_SENSITIVE_ALIASES.update(r.case_sensitive_aliases)
    for name in KINDS:
        if not name.startswith("_"):
            ALIASES.setdefault(name, [re.split(r" \(| /", name, maxsplit=1)[0]])
    _DEP_KEYS[:] = sorted(DEPLOYABLES, key=lambda x: (-len(x), x))


D.on_profile_change(_configure)

_EXAMPLE_RX = re.compile(r"(?<![\w-])(such as|e\.g\.|including|like)(?![\w-])", re.I)

TIMER_RX = re.compile(r"\btimer\b|time-?out|expir|periodic|scheduled|retry interval|ticker"
                      r"|guard time", re.I)
PROCESS_RX = re.compile(r"process start|start-?up|initiali[sz]|\bboot|main function|launch"
                        r"|program start", re.I)
EVENT_RX = re.compile(r"incoming|receiv|receipt|arriv|request|message|signal+ing|notification"
                      r"|command|callback", re.I)
ENUM_RX = re.compile(r",|;|\bor\b|\band\b|such as|e\.g\.|including|/", re.I)

TIMER = Canon("Timer", "timer")
PROCESS = Canon("Process start", "process")
EVENT = Canon("signalling", "event", "External signalling")
SEE_TRIGGER = Canon("See trigger", "unknown_trigger")
_SPECIAL = {c.name: c for c in (TIMER, PROCESS, EVENT)}


def clip(s: str, n: int) -> str:
    """collapse whitespace; cut to `n` characters ending in "…"."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _name_of(a) -> str:
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return str(a.get("name") or "")
    return str(getattr(a, "name", "") or "")


def normalize(raw: str) -> list[Canon]:
    """canonical actors named by one raw actor string, ordered by position.
    data/internal come back alone (and only) when nothing else matched."""
    s = re.sub(r"\s+", " ", _name_of(raw)).strip()
    if not s:
        return []
    s = _EXAMPLE_RX.split(s, 1)[0].strip() or s      # examples never become actors
    found: dict[str, tuple[int, int]] = {}             # name -> (pos, rule index)
    for i, (name, _kind, rx, unless) in enumerate(RULES):
        if unless and unless in found:
            continue
        m = rx.search(s)
        if m and (name not in found or m.start() < found[name][0]):
            found[name] = (m.start(), found.get(name, (0, i))[1])
    if not found:
        return [Canon(clip(s, 30), "unknown")]
    names = list(found)
    real = [n for n in names if KINDS[n] not in HIDDEN]
    if real:
        specific = [n for n in real if KINDS[n] != "generic"]
        names = specific or sorted(real, key=lambda n: found[n])[:1]   # one catch-all
    names.sort(key=lambda n: found[n])
    return [Canon(n, KINDS[n], LABELS.get(n, ""), found[n][0]) for n in names]


def normalize_all(raws) -> list[Canon]:
    """normalize every raw name; deduplicated by canonical name, first occurrence kept."""
    out: dict[str, Canon] = {}
    for raw in raws or []:
        for c in normalize(raw):
            out.setdefault(c.name, c)
    return list(out.values())


def is_self(c: Canon, deployables: list[str]) -> bool:
    return SELF.get(c.name) in set(deployables or [])


def drop_self(canons: list[Canon], deployables: list[str]) -> list[Canon]:
    """drop every canonical that IS one of the deployables under study."""
    return [c for c in canons if not is_self(c, deployables)]


def bl_actors(raws, deployables: list[str]) -> list[Canon]:
    """a business logic's drawable external actors: normalised, minus self."""
    return [c for c in drop_self(normalize_all(raws), deployables) if c.kind in DRAWABLE]


def colocate(c: Canon, model_deployables: list[str]) -> Canon:
    """kind "colocated" when the party's own deployable is in this repository's model."""
    if c.kind in DRAWABLE and SELF.get(c.name) in set(model_deployables or []):
        return replace(c, kind="colocated", label=f"{c.name} (this repo)")
    return c


# ---- initiator resolution ----------------------------------------------------------

def _phrase_rx(phrase: str) -> re.Pattern:
    flags = 0 if phrase in CASE_SENSITIVE_ALIASES else re.I
    return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", flags)


def _various(names: list[str]) -> Canon:
    return Canon("Various", "various", members=tuple(names))


def resolve_initiators(bl, actor_canons: list[Canon], roots: list[str] | None = None) -> Canon:
    """who or what starts a business logic: its trigger_sources when present, else the
    earliest of its own actors (and their aliases), a timer, process start or an
    external event named in the trigger; an enumeration between distinct hits -> Various.
    with roots, a logic run only by controllers (the profile's controller_roots) is never
    started by the operator role."""
    return controller_initiator(_resolve_initiators(bl, actor_canons), roots)


def controller_initiator(init: Canon, roots: list[str] | None) -> Canon:
    """roots under the profile's controller_roots are k8s controllers, started by api watch
    events on their custom resources (which an operator may have edited), not by a person
    directly."""
    if init.name != "Operator / OAM" or not roots or not CONTROLLER_ROOTS:
        return init
    prefixes = tuple(x.rstrip("/") + "/" for x in CONTROLLER_ROOTS)
    if all(r.startswith(prefixes) for r in roots):
        return next((c for c in normalize("Kubernetes") if c.kind == "infra"), SEE_TRIGGER)
    return init


def _resolve_initiators(bl, actor_canons: list[Canon]) -> Canon:
    sources = [s for s in (getattr(bl, "trigger_sources", None) or []) if str(s).strip()]
    picked: list[Canon] = []
    for src in sources:
        low = str(src).strip().lower()
        if low == "timer":
            c = TIMER
        elif low == "process start":
            c = PROCESS
        else:
            c = next((x for x in normalize(src) if x.kind in DRAWABLE or x.kind == "infra"),
                     None)
        if c is not None and all(c.name != p.name for p in picked):
            picked.append(c)
    if len(picked) == 1:
        return picked[0]
    if len(picked) > 1:
        return _various([p.name for p in picked[:3]])
    return _resolve_old(str(getattr(bl, "trigger", "") or ""), bl, actor_canons)


def _resolve_old(trigger: str, bl, actor_canons: list[Canon]) -> Canon:
    allowed = {c.name for c in actor_canons if c.kind in DRAWABLE}
    phrases: list[tuple[str, str]] = []           # (phrase, canonical)
    for raw in getattr(bl, "actors", None) or []:
        text = _name_of(raw).strip()
        cs = normalize(text)
        if text and len(cs) == 1 and cs[0].name in allowed:
            phrases.append((text, cs[0].name))
    for name in actor_canons:
        if name.name in allowed:
            phrases += [(p, name.name) for p in ALIASES.get(name.name, [])]
    hits: list[tuple[int, int, str]] = []
    for phrase, name in phrases:
        m = _phrase_rx(phrase).search(trigger)
        if m:
            hits.append((m.start(), m.end(), name))
    for rx, name in ((TIMER_RX, TIMER.name), (PROCESS_RX, PROCESS.name),
                     (EVENT_RX, EVENT.name)):
        m = rx.search(trigger)
        if m:
            hits.append((m.start(), m.end(), name))
    if not hits:
        return SEE_TRIGGER
    hits.sort(key=lambda h: (h[0], -h[1], h[2]))
    seq: list[tuple[int, int, str]] = []
    for h in hits:
        if seq and seq[-1][2] == h[2]:
            continue
        seq.append(h)
    order = list(dict.fromkeys(h[2] for h in seq))
    for a, b in zip(seq, seq[1:]):
        if ENUM_RX.search(trigger[a[1]:b[0]]):
            return _various(order)
    first = seq[0][2]
    if first in _SPECIAL:
        return replace(_SPECIAL[first], pos=seq[0][0])
    c = next(x for x in actor_canons if x.name == first)
    return replace(c, pos=seq[0][0])


def _member_text(name: str) -> str:
    if name == EVENT.name:
        return "external signalling"
    return name


def initiator_text(c: Canon) -> str:
    """the "Initiated by" text."""
    if c.kind == "various":
        return "Various: " + ", ".join(_member_text(m) for m in c.members)
    if c.kind == "event":
        return "external signalling"
    if c.kind == "unknown_trigger":
        return "See trigger"
    return c.display


def starts(c: Canon, initiator: Canon) -> bool:
    """`c` is (one of) the initiator(s)."""
    return c.name == initiator.name or c.name in initiator.members


# ---- deployables ----------------------------------------------------------------------

def _pkg(nid: str) -> str:
    return str(nid or "").split("::", 1)[0]


def top_dir(nid: str) -> str:
    """first path segment of a node id's package: "billing/api::Run" -> "billing"."""
    return _pkg(nid).split("/", 1)[0]


def deployable_of(pkg: str) -> str:
    """the deployable a package (or node id) belongs to: longest DEPLOYABLES prefix,
    else its top dir."""
    pkg = _pkg(pkg)
    for p in _DEP_KEYS:
        if pkg == p or pkg.startswith(p + "/"):
            return DEPLOYABLES[p]
    return pkg.split("/", 1)[0]


DEP_SHARE = 0.10
DEP_CAP = 2


def deployable_weights(t) -> Counter:
    """root walks per deployable: root_groups when present, else root_counts plus one per
    remaining root."""
    w: Counter = Counter()
    groups = getattr(t, "root_groups", None) or []
    if groups:
        for g in groups:
            w[deployable_of(g[0])] += int(g[2])
        return w
    counts = getattr(t, "root_counts", None) or []
    seen = set()
    for nid, n in counts:
        w[deployable_of(nid)] += int(n)
        seen.add(nid)
    for r in getattr(t, "roots", None) or []:
        if r not in seen:
            w[deployable_of(r)] += 1
    return w


def deployables(t) -> list[str]:
    """the deployables a trajectory runs in: >= 10 % of the root walks, by weight, at
    most 2, always the top one."""
    w = deployable_weights(t)
    total = sum(w.values())
    if not total:
        return []
    ranked = sorted(w, key=lambda d: (-w[d], d))
    keep = [d for d in ranked if w[d] >= DEP_SHARE * total][:DEP_CAP]
    return keep or ranked[:1]


# ---- interfaces -----------------------------------------------------------------------

def _via_tokens(label: str) -> list[str]:
    """reference points and service prefixes of an interface label:
    "API (REST)" -> ["API", "REST"]; "Bus (Orders / Billing)" -> ["Bus", "Orders", "Billing"]."""
    out = []
    head, _, rest = label.partition(" (")
    out += [p.strip() for p in head.split("/") if p.strip()]
    for p in rest.rstrip(")").split("/"):
        p = p.strip().split("_", 1)[0]
        if p and p[0].isupper():
            out.append(p)
    return out


def validate_via(via: str, canonical: str, deployables: list[str]) -> bool:
    """`via` names the reference point or service prefix of the canonical's interface
    towards one of the deployables."""
    v = re.sub(r"\s+", " ", str(via or "")).strip().lower()
    if not v:
        return False
    for dep in deployables or []:
        for tok in _via_tokens(interface(dep, canonical)):
            if re.search(r"(?<![a-z0-9])" + re.escape(tok.lower()) + r"(?![0-9])", v):
                return True
    return False


TEST_PKG = re.compile(r"(^|/)(ut|test|tests|testing|testdata|stubs?|mocks?|fakes?|drivers?)"
                      r"(/|$)|_test$")


def pkg_evidence(nodes) -> dict[str, str]:
    """canonical -> interface label, for parties whose interface code the nodes run
    (longest PKG_EVIDENCE prefix of each node's package; test and driver packages such as
    pkg/foo/ut are no evidence)."""
    rows = sorted(PKG_EVIDENCE, key=lambda r: (-len(r[0]), r[0]))
    out: dict[str, str] = {}
    for nid in nodes or []:
        pkg = _pkg(nid)
        if TEST_PKG.search(pkg):
            continue
        for prefix, label, cans in rows:
            if pkg == prefix or pkg.startswith(prefix + "/"):
                for c in sorted(cans):
                    out.setdefault(c, label)
                break
    return out


def allowed_actors(deployables: list[str]) -> str:
    """the [ALLOWED ACTORS] list: prompt names with reference-point hints, minus self."""
    deps = set(deployables or [])
    first = (deployables or [""])[0]
    parts = []
    for name, canon in PROMPT_ACTORS:
        if SELF.get(canon) in deps:
            continue
        hint = ref_point(interface(first, canon)) if first else ""
        parts.append(f"{name} ({hint})" if hint and name.lower() not in hint.lower()
                     else name)
    return ", ".join(parts)


# ---- rendering by kind ---------------------------------------------------------------

# kind -> (use case, context D1, sequence D10, activity lane)
RENDER = {
    "nf": ("system", "external", "participant", "initiator"),
    "system": ("system", "external", "participant", "initiator"),
    "generic": ("system", "external", "participant", "initiator"),
    "unknown": ("system", "external", "participant", "initiator"),
    "role": ("stick", "external", "actor", "initiator"),
    "colocated": ("system", "boundary", "participant", None),
    "infra": (None, "infra", "participant", None),      # sequence: only when a step names it
    "timer": ("timer", None, "participant", "Timer"),
    "process": (None, None, "note", "edge"),             # not an actor: initial-node label
    "event": (None, None, "participant", "External signalling"),
    "data": (None, None, None, None),
    "internal": (None, None, None, None),
}


def usecase_shape(c: Canon) -> str | None:
    """"stick" (human), "device" (a profile's device actor), "system", "timer", or None
    (not drawn)."""
    shape = RENDER.get(c.kind, (None,) * 4)[0]
    if c.name in HUMAN and shape:
        return "stick"
    if c.name in DEVICE and shape == "system":
        return "device"
    return shape


def stereotype(c: Canon) -> str:
    """the «…» box stereotype of a use-case actor, "" for the stick figure or none."""
    return {"system": "«system»", "device": "«device»", "timer": "«timer»"}.get(
        usecase_shape(c) or "", "")


def context_role(c: Canon) -> str | None:
    """D1: "external", "boundary" (inside the system box), "infra" (dashed), or None."""
    return RENDER.get(c.kind, (None,) * 4)[1]


def seq_keyword(c: Canon) -> str | None:
    """D10 sequence: "actor" (human), "participant", "note" (process start), or None."""
    return RENDER.get(c.kind, (None,) * 4)[2]


def lane_title(c: Canon) -> str | None:
    """D10 activity lane for an initiator, or None (no lane)."""
    lane = RENDER.get(c.kind, (None,) * 4)[3]
    if lane == "initiator":
        return c.display
    return None if lane == "edge" else lane
