"""step 6: llm naming of clusters and business-logic inference per trajectory."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from string import Template

from codocs.bizlogic import domain as D
from codocs.bizlogic.actors import (
    allowed_actors,
    deployable_weights,
    deployables,
    normalize,
    validate_via,
)
from codocs.bizlogic.domain import (
    DATA_OBJECTS,
    PROCEDURES,
    PROMPT_ACTORS,
    STATE_VOCAB,
    norm_state,
    procedure_key,
)
from codocs.bizlogic.graph import FuncGraph
from codocs.bizlogic.walk import Trajectory, _quotas, caller_in_path
from codocs.llm import LLMError
from codocs.repowiki.llm.prompts import extract_json

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
LANGS = {"en": "English", "ja": "Japanese", "ko": "Korean"}
CLUSTER_MEMBERS = 20
CLUSTER_PACKAGES = 8
SUMMARY_CHARS = 300
DETAIL_CHARS = 700
DETAIL_NODES = 4
TRAJECTORY_BUDGET = 12000
DETAIL_MAX_SUPPORT = 20      # function-level detail only for narrow trajectories ...
DETAIL_MIN_K = 15            # ... at a fine granularity
OPTIONAL_SHARE = 0.2         # a step fewer walks reach is an optional branch
VARIANTS_SHOWN = 5
_LEVELS = ((8, "capability family",
            "name the capability and the family of procedures it covers; list the main "
            "variants; no function-level detail"),
           (30, "procedure",
            "one business procedure (or a tight group of them) with its main steps"),
           (10 ** 9, "concrete flow",
            "a specific flow with concrete steps, conditions and outcomes"))
_HELPER_RE = re.compile(r"^(Get|Set|Is|Has|Lock|Unlock|UnLock|Cnv|Convert|Copy|String|Error)"
                        r"|Cnv|Convert|ToString$")
_LIFECYCLE_RE = re.compile(r"^(main|init|Init\w*|Run|run|Start\w*|start\w*|Initialize)$")
_CONF = ("low", "medium", "high")
_LANG_TAGS = {"c": "C", "cpp": "C++", "java": "Java", "python": "Python"}


def _ltag(node) -> str:
    """" [C]" etc. after a non-Go function in prompts; Go functions stay untagged."""
    t = _LANG_TAGS.get(getattr(node, "lang", "go"))
    return f" [{t}]" if t else ""


def prompt_vars() -> dict[str, str]:
    """the ${...} fillers of the bizlogic system prompts, from the active domain profile:
    without a profile they carry no system description, actor list, state vocabulary or
    procedure fields."""
    sd = D.active().system_description
    v = {"SYSTEM": f" ({sd})" if sd else "",
         "ACTOR_NAMING": "named as in [ALLOWED ACTORS] (without the bracketed interface)"
         if PROMPT_ACTORS else
         'each named by its role in a few words (e.g. "payment provider", "operator", '
         '"data store")',
         "ENTITIES": " | ".join([*STATE_VOCAB, "other"]) if STATE_VOCAB else
         "the stateful entity, e.g. order, session, connection",
         "DATA_OBJECTS": " | ".join([*DATA_OBJECTS, "other business data"]) if DATA_OBJECTS
         else "business data object, e.g. order, customer record, session",
         "STATE_RULE": "", "PROCEDURE_FIELDS": "", "PROCEDURE_RULE": "", "FIXED_FIELDS": ""}
    if STATE_VOCAB:
        rule = ", ".join(f"for {k} use {'/'.join(vals)}" for k, vals in STATE_VOCAB.items())
        v["STATE_RULE"] = f" {rule[0].upper()}{rule[1:]}."
    if PROCEDURES:
        v["PROCEDURE_FIELDS"] = (
            ' "implements": ["<names from [KNOWN PROCEDURES] whose steps the code under study '
            'performs>"],\n'
            ' "supports": ["<names from [KNOWN PROCEDURES] during which this internal mechanism '
            'is used>"],\n')
        v["PROCEDURE_RULE"] = (
            '- "implements": only names from [KNOWN PROCEDURES], only when this logic performs '
            "that procedure's steps for the code under study. Internal mechanisms (conflict "
            'tables, locks, audits, caches) never "implement" a procedure; list the procedures '
            'they serve under "supports". [] if none fits.\n')
        v["FIXED_FIELDS"] = '"implements", "supports", '
    return v


def _system(name: str) -> str:
    text = (PROMPTS / f"{name}.txt").read_text(encoding="utf-8").strip()
    return Template(text).substitute(prompt_vars())


def _lang(language: str) -> str:
    return LANGS.get(language, language)


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _s(v) -> str:
    """a json value as text: null -> ''."""
    if v is None:
        return ""
    if isinstance(v, list | dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def _l(v, commas: bool = False) -> list[str]:
    """a json value as a list of strings: a string is split on newlines or ';' (and ','
    with `commas`), not into characters."""
    if v is None:
        return []
    if isinstance(v, str):
        parts = re.split(r"[\n;,]" if commas else r"[\n;]", v)
        return [p.strip(" -*\t") for p in parts if p.strip(" -*\t")]
    if isinstance(v, list | tuple):
        return [x for x in (_s(i) for i in v) if x]
    return [_s(v)] if _s(v) else []


def parse_reply(reply: str) -> dict | None:
    """the json object of a reply: tolerates fences, a list around it and trailing prose."""
    data = extract_json(reply)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if isinstance(data, dict):
        return data
    start = reply.find("{")
    while start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(reply[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = reply.find("{", start + 1)
    return None


def _valid(key: str):
    def check(reply: str) -> bool:
        data = parse_reply(reply)
        return isinstance(data, dict) and bool(_s(data.get(key)))
    return check


@dataclass
class ClusterInfo:
    id: int
    size: int
    packages: list[tuple[str, int]]
    central: list[str]
    subsystems: list[tuple[str, float]] = field(default_factory=list)   # top dir, share
    children: list[int] = field(default_factory=list)                   # parent level only
    name: str = ""
    summary: str = ""
    kind: str = ""
    coherence: str = ""
    also_contains: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Actor:
    name: str
    via: str = ""                # validated interface label or ""


@dataclass
class Step:
    action: str
    at: int | None = None        # [CLUSTER SEQUENCE] step number (1-based), None when unmapped
    actor: str = ""
    message: str = ""
    direction: str = "internal"  # in | out | internal


@dataclass
class Alternative:
    at: int | None
    condition: str
    result: str = ""


@dataclass
class StateChange:
    entity: str
    from_state: str
    to_state: str
    at: int | None = None
    evidence: str = ""


@dataclass
class DataObject:
    name: str
    op: str = ""                 # letters from CRUD


@dataclass
class BusinessLogic:
    trajectory: str
    title: str = ""
    short_title: str = ""
    summary: str = ""
    scope: str = ""              # single | family | ""
    trigger: str = ""
    trigger_sources: list[str] = field(default_factory=list)
    actors: list[Actor] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    alternatives: list[Alternative] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    state_changes: list[StateChange] = field(default_factory=list)
    data_objects: list[DataObject] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)      # PROCEDURES keys only
    supports: list[str] = field(default_factory=list)        # PROCEDURES keys only
    outcome: str = ""
    category: str = ""
    confidence: str = ""         # min(llm rating, what the evidence supports)
    llm_confidence: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None, trajectory: str, n_steps: int) -> BusinessLogic:
        """a stored entry, in the older shape (string actors and steps) or the current one."""
        return parse_logic(d or {}, trajectory, n_steps, stored=True)


CATEGORIES = ("business procedure", "service operation", "platform/lifecycle",
              "operations & maintenance", "data management", "infrastructure")
_OLD_CATEGORIES = {"network procedure": "business procedure"}   # stored by older runs
SCOPES = ("single", "family")
DIRECTIONS = ("in", "out", "internal")
MAX_TRIGGER_SOURCES = 3
MAX_ALTERNATIVES = 3
MAX_PRECONDITIONS = 2
MAX_STATE_CHANGES = 3
MAX_DATA_OBJECTS = 4
MAX_PROCEDURES = 3
_SPECIAL_SOURCES = ("timer", "process start")


def _key(s: str) -> str:
    return " ".join(s.split()).casefold()


def _category(v: str) -> str:
    """the canonical category: exact, then substring, else 'other' ('' stays '')."""
    k = _key(v)
    if not k:
        return ""
    k = _OLD_CATEGORIES.get(k, k)
    if k in CATEGORIES:
        return k
    for c in CATEGORIES:
        if c in k or k in c:
            return c
    return "other"


def _at(v, n_steps: int) -> int | None:
    """a step number in 1..n_steps, else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        at = int(v) if not isinstance(v, str) else int(v.strip())
    except (TypeError, ValueError):
        return None
    return at if 1 <= at <= n_steps else None


def _items(v) -> list:
    """a json list as items; a string is split like `_l`; a dict is one item."""
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list | tuple):
        return list(v)
    return _l(v)


def _procedures(v) -> list[str]:
    """allow-listed procedure names (the profile's canonical keys), deduplicated, in order."""
    out: list[str] = []
    for x in _l(v):
        p = procedure_key(x)
        if p and p not in out:
            out.append(p)
    return out[:MAX_PROCEDURES]


def _actor(d: dict, deps: list[str] | None) -> Actor | None:
    name = _s(d.get("name"))
    if not name:
        return None
    via = _s(d.get("via"))
    if via and deps is not None and \
            not any(validate_via(via, c.name, deps) for c in normalize(name)):
        via = ""
    return Actor(name=name, via=via)


def _step(d: dict, n_steps: int) -> Step | None:
    action = _s(d.get("action")) or _s(d.get("message"))
    if not action:
        return None
    actor = _s(d.get("actor"))
    direction = _s(d.get("direction")).lower()
    if direction not in DIRECTIONS or not actor:
        direction = "internal"
    return Step(action=action, at=_at(d.get("at"), n_steps), actor=actor,
                message=_s(d.get("message")), direction=direction)


def parse_logic(data: dict, trajectory: str, n_steps: int, stored: bool,
                deployables: list[str] | None = None) -> BusinessLogic:
    """one parser for the llm reply (`stored` False) and the stored json (True, either
    shape). `deployables` (the trajectory's) validates actor `via`; None keeps it."""
    d = data if isinstance(data, dict) else {}
    bl = BusinessLogic(trajectory=trajectory)
    for key in ("title", "short_title", "summary", "trigger", "outcome"):
        setattr(bl, key, _s(d.get(key)))
    scope = _s(d.get("scope")).lower()
    bl.scope = scope if scope in SCOPES else ""
    bl.category = _category(_s(d.get("category")))
    if stored:
        bl.confidence = _s(d.get("confidence"))
        bl.llm_confidence = _s(d.get("llm_confidence"))
        bl.error = _s(d.get("error"))
    else:
        conf = _s(d.get("confidence")).lower()
        bl.llm_confidence = conf if conf in _CONF else ""

    src = d.get("trigger_sources")
    if src is None:
        src = d.get("trigger_source")
    for x in ([_s(src)] if isinstance(src, str) else _l(src)):
        x = x.lower() if x.lower() in _SPECIAL_SOURCES else x
        if x and x not in bl.trigger_sources:
            bl.trigger_sources.append(x)
    bl.trigger_sources = bl.trigger_sources[:MAX_TRIGGER_SOURCES]

    raw = d.get("actors")
    for item in (_l(raw, commas=True) if isinstance(raw, str) else _items(raw)):
        a = _actor(item, deployables) if isinstance(item, dict) else \
            (Actor(name=_s(item)) if _s(item) else None)
        if a and a.name not in [x.name for x in bl.actors]:
            bl.actors.append(a)

    raw = d.get("steps")
    items = _items(raw)
    if all(not isinstance(x, dict) for x in items):
        texts = [x for x in (_s(i) for i in items) if x]
        mapped = stored and "variants" not in d and len(texts) == n_steps
        bl.steps = [Step(action=s, at=i + 1 if mapped else None) for i, s in enumerate(texts)]
    else:
        for x in items:
            s = _step(x, n_steps) if isinstance(x, dict) else \
                (Step(action=_s(x)) if _s(x) else None)
            if s:
                bl.steps.append(s)

    bl.variants = _l(d.get("variants"))
    for x in _items(d.get("alternatives")):
        if isinstance(x, dict):
            cond = _s(x.get("condition"))
            alt = Alternative(at=_at(x.get("at"), n_steps), condition=cond,
                              result=_s(x.get("result")))
        else:
            cond = _s(x)
            alt = Alternative(at=None, condition=cond)
        if cond:
            bl.alternatives.append(alt)
    bl.alternatives = bl.alternatives[:MAX_ALTERNATIVES]
    bl.preconditions = _l(d.get("preconditions"))[:MAX_PRECONDITIONS]

    for x in _items(d.get("state_changes")):
        if not isinstance(x, dict):
            continue
        entity = _s(x.get("entity"))
        a = _s(x.get("from", x.get("from_state")))
        b = _s(x.get("to", x.get("to_state")))
        if not a or not b:
            continue
        bl.state_changes.append(StateChange(entity=entity, from_state=norm_state(entity, a),
                                            to_state=norm_state(entity, b),
                                            at=_at(x.get("at"), n_steps),
                                            evidence=_s(x.get("evidence"))))
    bl.state_changes = bl.state_changes[:MAX_STATE_CHANGES]

    for x in _items(d.get("data_objects")):
        name = _s(x.get("name")) if isinstance(x, dict) else _s(x)
        if not name:
            continue
        op = _s(x.get("op")).upper() if isinstance(x, dict) else ""
        bl.data_objects.append(DataObject(name=name, op="".join(c for c in "CRUD" if c in op)))
    bl.data_objects = bl.data_objects[:MAX_DATA_OBJECTS]

    bl.implements = _procedures(d.get("implements"))
    bl.supports = [p for p in _procedures(d.get("supports")) if p not in bl.implements]
    return bl


def target_level(k: int) -> tuple[str, str]:
    """(level, what to write) the granularity K asks for."""
    for limit, name, how in _LEVELS:
        if k <= limit:
            return name, how
    return _LEVELS[-1][1], _LEVELS[-1][2]


def describe_clusters(fg: FuncGraph, assign: dict[str, int],
                      children: dict[int, list[int]] | None = None) -> dict[int, ClusterInfo]:
    """sizes, package and subsystem spread, and central members of each cluster (no llm).
    central members are spread over the cluster's packages in proportion to their size
    and ranked by links inside the cluster, so no hub sub-community fills the list."""
    members: dict[int, list[str]] = {}
    for n, c in assign.items():
        members.setdefault(c, []).append(n)
    out: dict[int, ClusterInfo] = {}
    for c, ns in sorted(members.items()):
        inside = set(ns)
        by_pkg: dict[str, list[str]] = {}
        for n in ns:
            by_pkg.setdefault(fg.nodes[n].pkg or "(root)", []).append(n)
        quota = _quotas({p: len(v) for p, v in by_pkg.items()}, min(CLUSTER_MEMBERS, len(ns)))

        def score(n: str) -> tuple:
            g = fg.graph
            links = sum(1 for x in g.predecessors(n) if x in inside) + \
                sum(1 for x in g.successors(n) if x in inside)
            return (-(bool(fg.nodes[n].summary)), -links, n)

        central = [n for p in sorted(by_pkg, key=lambda p: (-len(by_pkg[p]), p))
                   for n in sorted(by_pkg[p], key=score)[:quota.get(p, 0)]]
        pk = sorted(((p, len(v)) for p, v in by_pkg.items()), key=lambda x: (-x[1], x[0]))
        top = Counter((fg.nodes[n].pkg or "(root)").split("/")[0] for n in ns)
        subsystems = [(s, round(v / len(ns), 3)) for s, v in top.most_common()]
        out[c] = ClusterInfo(id=c, size=len(ns), packages=pk[:CLUSTER_PACKAGES],
                             central=central, subsystems=subsystems,
                             children=sorted((children or {}).get(c, [])))
    return out


def _shares(items: list[tuple[str, float]], n: int = 6) -> str:
    return ", ".join(f"{s} {round(100 * v)}%" for s, v in items[:n])


def cluster_prompt(fg: FuncGraph, info: ClusterInfo, language: str) -> tuple[str, str]:
    covered = sum(n for _, n in info.packages)
    lines = [f"[OUTPUT LANGUAGE] {_lang(language)}",
             f"[CLUSTER] {info.size} functions",
             f"[SUBSYSTEMS] {_shares(info.subsystems)} (share of the functions per top-level "
             "directory)",
             f"[PACKAGES] the largest {len(info.packages)} cover "
             f"{round(100 * covered / max(1, info.size))}%: "
             + ", ".join(f"{p} ({round(100 * n / max(1, info.size))}%)" for p, n in info.packages),
             "[CENTRAL FUNCTIONS] spread over the packages in proportion to their size"]
    for n in info.central:
        node = fg.nodes[n]
        lines.append(f"- {node.pkg}.{node.name}{_ltag(node)}: "
                     f"{_clip(node.summary, SUMMARY_CHARS) or '(no doc)'}")
    return _system("bizlogic_cluster_system"), "\n".join(lines)


def parent_prompt(info: ClusterInfo, children: dict[int, ClusterInfo],
                  language: str) -> tuple[str, str]:
    lines = [f"[OUTPUT LANGUAGE] {_lang(language)}",
             f"[SUBSYSTEM] {info.size} functions",
             f"[SUBSYSTEMS] {_shares(info.subsystems)} (share of the functions per top-level "
             "directory)",
             "[COMPONENTS] (functions, name, kind: summary)"]
    for c in sorted(info.children, key=lambda c: -children[c].size if c in children else 0):
        ch = children.get(c)
        if ch is None:
            continue
        name = ch.name or f"cluster {c} ({_shares(ch.subsystems, 3)})"
        lines.append(f"- ({ch.size}) {name}, {ch.kind or '-'}: {_clip(ch.summary, 400)}")
    return _system("bizlogic_parent_system"), "\n".join(lines)


def _apply_cluster(info: ClusterInfo, data: dict | None) -> None:
    if not isinstance(data, dict):
        info.error = "unparsed reply"
        return
    info.name = _s(data.get("name"))
    info.summary = _s(data.get("summary"))
    info.kind = _s(data.get("kind"))
    info.coherence = _s(data.get("coherence"))
    info.also_contains = _l(data.get("also_contains"), commas=True)
    if not info.name:
        info.error = "missing name"


async def label_clusters(llm, fg: FuncGraph, clusters: dict[int, ClusterInfo], language: str,
                         concurrency: int = 4, on_progress=None,
                         children: dict[int, ClusterInfo] | None = None) -> None:
    """name each cluster; with `children` (the labelled finer level) the clusters are
    subsystems named from their components."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(info: ClusterInfo) -> None:
        if children is not None:
            system, user = parent_prompt(info, children, language)
            tag = f"bizlogic subsystem P{info.id}"
        else:
            system, user = cluster_prompt(fg, info, language)
            tag = f"bizlogic cluster C{info.id}"
        async with sem:
            try:
                reply = await llm.complete(system, user, tag=tag, max_tokens=2000,
                                           validate=_valid("name"))
            except LLMError as e:
                info.error = str(e)
                return
        _apply_cluster(info, parse_reply(reply))
        if on_progress:
            on_progress(f"{tag}: {info.name or info.error}")

    await asyncio.gather(*(one(i) for i in clusters.values()))


def _helper_only(fg: FuncGraph, evidence: list[tuple[str, int]]) -> bool:
    names = [fg.nodes[n].name.rsplit(".", 1)[-1] for n, _ in evidence]
    return bool(names) and all(_HELPER_RE.search(x) for x in names)


def _root_line(fg: FuncGraph, n: str, cnt: int) -> str:
    node = fg.nodes[n]
    tags = []
    callers = [p for p, _, d in fg.graph.in_edges(n, data=True)
               if d.get("kind") == "call" and p not in fg.utilities]
    if callers:
        p = fg.nodes[sorted(callers)[0]]
        tags.append(f"dispatched from {p.pkg}.{p.name}")
    if _LIFECYCLE_RE.match(node.name.rsplit(".", 1)[-1]):
        tags.append("process start/lifecycle: describe it only as the trigger")
    tag = f" [{'; '.join(tags)}]" if tags else ""
    return (f"- ({cnt}) {node.pkg}.{node.name}{_ltag(node)}{tag}: "
            f"{_clip(node.summary, SUMMARY_CHARS) or '(no doc)'}")


def domain_lines(traj: Trajectory) -> list[str]:
    """[CODE UNDER STUDY], and with a domain profile [ALLOWED ACTORS] and [KNOWN PROCEDURES]
    prompt lines."""
    deps = deployables(traj)
    weights = deployable_weights(traj)
    total = max(1, sum(weights.values()))
    out = []
    if deps:
        where = ", ".join(f"{d} ({round(100 * weights[d] / total)} % of root walks)"
                          for d in deps)
        out.append(f"[CODE UNDER STUDY] the entry roots live in: {where}. This code is the "
                   "system being described: never list it as an actor.")
    if PROMPT_ACTORS:
        out.append("[ALLOWED ACTORS] " + allowed_actors(deps))
    if PROCEDURES:
        out.append("[KNOWN PROCEDURES] " + "; ".join(PROCEDURES))
    return out


def trajectory_prompt(fg: FuncGraph, traj: Trajectory, clusters: dict[int, ClusterInfo],
                      k: int, language: str, assign: dict[str, int]) -> tuple[str, str]:
    level, how = target_level(k)
    rest = traj.support - traj.own_support
    head = [f"[OUTPUT LANGUAGE] {_lang(language)}",
            f"[GRANULARITY] the system was divided into {k} clusters",
            f"[TARGET LEVEL] {level}: {how}"]
    head += domain_lines(traj)
    head += [f"[WALKS] this trajectory merges {traj.support} sampled walks from "
            f"{len(traj.roots)} entry root(s): {traj.own_support} follow the whole cluster "
            f"sequence; {rest} follow only its first steps or are rarer variants folded in "
            "(per-step counts below). Steps few walks reach are optional branches, not "
            "shared behaviour"]
    if traj.root_groups:
        head.append("[ROOT SUBSYSTEMS] " + ", ".join(
            f"{g} {r} root(s) / {w} walks" for g, r, w in traj.root_groups[:8]))
    if traj.root_counts:
        head.append(f"[ENTRY ROOTS] most frequent of {len(traj.roots)}, spread over the root "
                    "subsystems (walks, function, doc)")
        head += [_root_line(fg, n, cnt) for n, cnt in traj.root_counts]
    head.append("[CLUSTER SEQUENCE] each step: the cluster (an approximate label; the "
                "functions listed under it are the evidence), how many walks reach it, and "
                "the functions the walks visit most in that step (walks, function, doc); "
                '"at" in your answer refers to these step numbers')
    steps: list[tuple[str, list[tuple[str, int]]]] = []
    for i, c in enumerate(traj.clusters):
        info = clusters.get(c)
        name = info.name if info and info.name else f"cluster {c}"
        desc = _clip(info.summary, 400) if info else ""
        reached = traj.step_walks[i] if i < len(traj.step_walks) else traj.support
        evidence = traj.step_functions[i] if i < len(traj.step_functions) else []
        tags = [f"reached by {reached} of {traj.support} walks"]
        if reached < OPTIONAL_SHARE * traj.support:
            tags.append("optional branch")
        if _helper_only(fg, evidence):
            tags.append("helper-only: merge into a neighbouring step")
        steps.append((f"{i + 1}. C{c} {name} ({'; '.join(tags)}): {desc}", evidence))
    variants = []
    if traj.variants:
        variants.append("[VARIANTS] rarer cluster sequences folded into this trajectory "
                        "(walks: sequence)")
        for seq, cnt in traj.variants[:VARIANTS_SHOWN]:
            names = " -> ".join(clusters[c].name if c in clusters and clusters[c].name
                                else f"C{c}" for c in seq)
            variants.append(f"- ({cnt}) {names}")
        if len(traj.variants) > VARIANTS_SHOWN:
            variants.append(f"- ... {len(traj.variants) - VARIANTS_SHOWN} more")
    chain = ["[ONE REPRESENTATIVE WALK] one walk that follows the whole sequence, in visiting "
             "order (depth first: a step is called by the function named after 'via', or by "
             "the step before it); cluster, function, doc"]
    for i, n in enumerate(traj.path):
        node = fg.nodes[n]
        caller = caller_in_path(fg, traj.path, i)
        via = f" via {fg.nodes[caller].name}" if caller and i and caller != traj.path[i - 1] \
            else ""
        chain.append(f"{i + 1}. [C{assign.get(n, '?')}] {node.pkg}.{node.name}{_ltag(node)}{via} "
                     f"({node.file}:{node.line}): "
                     f"{_clip(node.summary, SUMMARY_CHARS) or '(no doc)'}")
    tail = ["Infer the business logic of this trajectory now. JSON only."]

    # the chain always fits: evidence docs are shortened first when the budget is tight
    def render(clip: int) -> list[str]:
        out = []
        for line, evidence in steps:
            out.append(line)
            for n, cnt in evidence:
                node = fg.nodes[n]
                doc = (_clip(node.summary, clip) or "(no doc)") if clip else ""
                out.append(f"   - ({cnt}) {node.pkg}.{node.name}{_ltag(node)}"
                           + (f": {doc}" if doc else ""))
        return out

    fixed = sum(len(x) + 1 for x in head + variants + chain + tail)
    for clip in (SUMMARY_CHARS, 150, 60, 0):
        body = render(clip)
        if fixed + sum(len(x) + 1 for x in body) <= TRAJECTORY_BUDGET:
            break
    size = fixed + sum(len(x) + 1 for x in body)
    if level != _LEVELS[0][1] and k >= DETAIL_MIN_K and traj.support <= DETAIL_MAX_SUPPORT:
        for i, n in enumerate(traj.path[:DETAIL_NODES]):
            doc = fg.nodes[n].doc
            if not doc:
                continue
            extra = f"\n   detail: {_clip(doc, DETAIL_CHARS)}"
            if size + len(extra) > TRAJECTORY_BUDGET:
                continue
            chain[i + 1] += extra
            size += len(extra)
    lines = head + body + variants + chain + tail
    return _system("bizlogic_trajectory_system"), "\n".join(lines)


def evidence_confidence(fg: FuncGraph, traj: Trajectory) -> str:
    """the most the evidence supports: thin (few walks) or undocumented trajectories are
    never rated high."""
    funcs = set(traj.path) | {n for step in traj.step_functions for n, _ in step}
    documented = sum(bool(fg.nodes[n].summary) for n in funcs) / max(1, len(funcs))
    if documented < 0.25:
        return "low"
    if traj.own_support < 3 or documented < 0.5:
        return "medium"
    return "high"


def _min_conf(a: str, b: str) -> str:
    ia = _CONF.index(a) if a in _CONF else 1
    return _CONF[min(ia, _CONF.index(b))]


def apply_logic(bl: BusinessLogic, data: dict | None, fg: FuncGraph, traj: Trajectory) -> None:
    if not isinstance(data, dict):
        bl.error = "unparsed reply"
        return
    new = parse_logic(data, traj.id, len(traj.clusters), stored=False,
                      deployables=deployables(traj))
    for f in fields(BusinessLogic):
        if f.name not in ("trajectory", "error"):
            setattr(bl, f.name, getattr(new, f.name))
    bl.confidence = _min_conf(bl.llm_confidence, evidence_confidence(fg, traj))
    if not bl.title:
        bl.error = "missing title"


async def infer_trajectories(llm, fg: FuncGraph, trajs: list[Trajectory],
                             clusters: dict[int, ClusterInfo], k: int, language: str,
                             assign: dict[str, int], concurrency: int = 4,
                             on_progress=None) -> dict[str, BusinessLogic]:
    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[str, BusinessLogic] = {}

    async def one(t: Trajectory) -> None:
        system, user = trajectory_prompt(fg, t, clusters, k, language, assign)
        bl = BusinessLogic(trajectory=t.id)
        async with sem:
            try:
                reply = await llm.complete(system, user, tag=f"bizlogic {t.id} k{k}",
                                           max_tokens=4000, validate=_valid("title"))
            except LLMError as e:
                bl.error = str(e)
                out[t.id] = bl
                return
        apply_logic(bl, parse_reply(reply), fg, t)
        out[t.id] = bl
        if on_progress:
            on_progress(f"{t.id}: {bl.title or bl.error}")

    await asyncio.gather(*(one(t) for t in trajs))
    return {t.id: out[t.id] for t in trajs if t.id in out}
