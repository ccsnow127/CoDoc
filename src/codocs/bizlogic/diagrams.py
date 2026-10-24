"""deterministic mermaid diagrams for the bizlogic wiki (no llm).

label safety, a builder that checks what it emits, the per-K model and one generator per
diagram: D1 context, D2 procedure use cases, actor x group matrix, D3 components, D4/D8 call
map, D7 group use cases, D9 participants, D10 flow (sequence, activity, family view), D11
state changes, D13 ego map, related logics and the compressed "where in the code" bullets.
every drawn fact carries an evidence level (bold / solid / dashed "?") explained in the
caption; lifelines, lanes and use-case subjects are deployables or outside parties, never
code clusters.
"""

from __future__ import annotations

import base64
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from codocs.bizlogic import actors as A
from codocs.bizlogic import domain as D
from codocs.bizlogic.actors import DRAWABLE, HUMAN, Canon
from codocs.bizlogic.domain import (
    ADJACENT_LIFELINES,
    MESSAGE_PREFIXES,
    PARTY_TOKENS,
    PROCEDURES,
    SELF,
    SHORT_TOKENS,
    STATE_VOCAB,
    TOKEN_ALIASES,
    interface,
    state_entity,
)
from codocs.bizlogic.infer import (
    OPTIONAL_SHARE,
    Actor,
    BusinessLogic,
    ClusterInfo,
    Step,
    target_level,
)
from codocs.bizlogic.walk import Trajectory

log = logging.getLogger(__name__)

# ---- label safety -----------------------------------------------------------------------

_ENT = {'#': '#35;', '"': '#quot;', '<': '#lt;', '>': '#gt;', ';': '#59;', ':': '#58;'}
# sequence text is wrapped (wrap: true) after mermaid turns each entity into a placeholder;
# a line break inside a placeholder leaves its pieces (e.g. "ﬂ°°59¶ß") on screen. so the
# characters a sequence statement cannot hold get look-alikes there, never entities.
_SEQ_LOOK = {";": ",", "#": "＃", "<": "‹", ">": "›"}
_CTX = {"quoted": '#"<>', "seq": "".join(_SEQ_LOOK), "colon": '#;:<>'}
_CTX_RX = {k: re.compile("[" + re.escape(v) + "]") for k, v in _CTX.items()}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

LIM_NODE, LIM_OVAL, LIM_ACTOR, LIM_SUBJECT = 40, 44, 26, 56
LIM_SEQ_MSG, LIM_SEQ_PART, LIM_NOTE, LIM_COND = 60, 36, 90, 36
LIM_STATE, LIM_TAG = 30, 24


def clip(text: str, limit: int = 0) -> str:
    """one line, no control characters or backticks; cut at a word to `limit` with "…"
    (text without spaces, such as cjk, is cut hard)."""
    t = " ".join(_CTRL.sub(" ", str(text or "")).split()).replace("`", "'")
    if limit and len(t) > limit:
        cut = t[: limit - 1]
        sp = cut.rsplit(" ", 1)[0]
        t = (sp if len(sp) >= limit // 2 else cut).rstrip(" ,;:-·…") + "…"
    return t


def mm(text: str, ctx: str = "quoted", limit: int = 0) -> str:
    """mermaid-safe label text for a context (quoted | seq | colon), encoded in one pass
    (seq: look-alike characters instead of entities)."""
    table = _SEQ_LOOK if ctx == "seq" else _ENT
    return _CTX_RX[ctx].sub(lambda m: table[m.group(0)], clip(text, limit))


def wrap_lines(text: str, width: int = 32, lines: int = 3) -> list[str]:
    """`text` as at most `lines` lines of about `width` characters, cut at words; the last
    line ends with "…" when text is left over (text without spaces is cut hard)."""
    t = clip(text)
    out: list[str] = []
    while t and len(out) < lines:
        if len(t) <= width:
            out.append(t)
            t = ""
            break
        cut = t[:width + 1].rsplit(" ", 1)[0]
        if len(cut) < width // 2:
            cut = t[:width]
        out.append(cut.rstrip())
        t = t[len(cut):].lstrip()
    if t and out:
        out[-1] = clip(out[-1] + " " + t, width)
    return out


# ---- ids and the builder -----------------------------------------------------------------

ID_RX = re.compile(r"^[a-z][a-z0-9_]*$")
KEYWORDS = {"end", "graph", "subgraph", "flowchart", "class", "classdef", "style", "linkstyle",
            "click", "direction", "default", "loop", "alt", "else", "opt", "par", "and",
            "rect", "box", "note", "critical", "option", "break", "actor", "participant",
            "create", "destroy", "autonumber", "activate", "deactivate", "state", "title"}
_OPENERS = {"subgraph", "box", "opt", "alt", "break", "rect", "critical", "loop", "par"}
MAX_SRC, MAX_DIA_EDGES = 45_000, 450


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


class IdReg:
    """unique, valid ids: the same (prefix, name) always gets the same id."""

    def __init__(self) -> None:
        self._used: set[str] = set()
        self._keys: dict[tuple[str, str], str] = {}

    def reserve(self, id_: str) -> None:
        self._used.add(id_)

    def get(self, prefix: str, name: str = "", index: int = 0) -> str:
        key = (prefix, str(name))
        if key in self._keys:
            return self._keys[key]
        s = slug(name)
        base = f"{prefix}{s}" if s else f"{prefix}{index}"
        if not ID_RX.match(base) or base in KEYWORDS or base[0] in "ox":
            base = "id_" + slug(base)
        out, n = base, 2
        while out in self._used:
            out, n = f"{base}_{n}", n + 1
        self._used.add(out)
        self._keys[key] = out
        return out


class Dia:
    """a mermaid source built line by line; records declared ids and edge endpoints so
    `check` needs no parsing of the output."""

    def __init__(self, header: list[str]):
        self.header = list(header)
        self.body: list[str] = []
        self.ids: set[str] = set()
        self.edges: list[tuple[str, str]] = []
        self.refs: list[str] = []
        self.reg = IdReg()

    def id(self, prefix: str, name: str = "", index: int = 0) -> str:
        return self.reg.get(prefix, name, index)

    def has(self, id_: str) -> bool:
        return id_ in self.ids

    def node(self, id_: str, line: str) -> str:
        """declares `id_` (nodes, participants, subgraphs, lanes)."""
        self.ids.add(id_)
        self.reg.reserve(id_)
        self.body.append(line)
        return id_

    def edge(self, a: str, b: str, line: str) -> int:
        """records the endpoints; returns the edge index for linkStyle."""
        self.edges.append((a, b))
        self.body.append(line)
        return len(self.edges) - 1

    def raw(self, line: str, refs: tuple[str, ...] | list[str] = ()) -> None:
        """classDef, linkStyle, notes, blocks; `refs` are ids the line must find declared."""
        self.refs.extend(refs)
        self.body.append(line)

    def src(self) -> str:
        out, depth = list(self.header), 1
        for line in self.body:
            word = line.split(maxsplit=1)[0] if line.strip() else ""
            if word in ("end", "}"):
                depth = max(1, depth - 1)
            ind = "  " * (depth - 1 if word == "else" else depth)
            out.append(ind + line)
            if word in _OPENERS or line.rstrip().endswith("{") and not line.lstrip().startswith(
                    ("%%", "classDef")) and "@{" not in line:
                depth += 1
        return "\n".join(out)

    def __str__(self) -> str:
        return self.src()

    def check(self) -> list[str]:
        errs: list[str] = []
        for a, b in self.edges:
            for x in (a, b):
                if x not in self.ids:
                    errs.append(f"undeclared endpoint {x}")
        for x in self.refs:
            if x not in self.ids:
                errs.append(f"undeclared reference {x}")
        for x in sorted(self.ids | {e for ab in self.edges for e in ab}):
            if not ID_RX.match(x):
                errs.append(f"bad id {x!r}")
        src = self.src()
        if len(src) > MAX_SRC:
            errs.append(f"source too long ({len(src)} characters)")
        if len(self.edges) > MAX_DIA_EDGES:
            errs.append(f"too many edges ({len(self.edges)})")
        stack: list[str] = []
        for line in self.body:
            s = line.strip()
            word = s.split(maxsplit=1)[0] if s else ""
            if s.count('"') % 2:
                errs.append(f"odd quotes in {s[:40]!r}")
            if word == "linkStyle":
                for i in re.findall(r"\d+", s.split()[1] if len(s.split()) > 1 else ""):
                    if int(i) >= len(self.edges):
                        errs.append(f"linkStyle index {i} out of range")
            if word in _OPENERS:
                stack.append(word)
            elif word == "else":
                if not stack or stack[-1] != "alt":
                    errs.append("else outside alt")
            elif word == "end":
                if not stack or stack[-1] == "{":
                    errs.append("unbalanced end")
                else:
                    stack.pop()
            elif s.endswith("{") and word == "state":
                stack.append("{")
            elif s == "}":
                if not stack or stack[-1] != "{":
                    errs.append("unbalanced }")
                else:
                    stack.pop()
        if stack:
            errs.append(f"unclosed block {stack[-1]}")
        return errs


def fence(d) -> list[str]:
    """a mermaid code fence, or a one-line note when the diagram fails its check."""
    if d is None or d == "":
        return []
    if isinstance(d, str):
        return ["```mermaid", *d.splitlines(), "```"]
    errs = d.check()
    if errs:
        log.warning("diagram omitted: %s", errs[0])
        return [f"> Diagram omitted: {errs[0]}."]
    return ["```mermaid", *d.src().splitlines(), "```"]


# ---- shared styles ---------------------------------------------------------------------

FLOW_INIT = '%%{init: {"flowchart": {"useMaxWidth": false}} }%%'
SEQ_INIT = ('%%{init: {"sequence": {"wrap": true, "width": 150, "actorMargin": 40, '
            '"mirrorActors": false, "useMaxWidth": false}} }%%')
STATE_INIT = '%%{init: {"state": {"useMaxWidth": false}} }%%'

CLASSDEF_UC = ["classDef uc fill:#e6f4ff,stroke:#1677ff,color:#000",
               "classDef sysactor fill:#fff7e6,stroke:#d48806,color:#000",
               "classDef more fill:#fafafa,stroke:#bfbfbf,stroke-dasharray:3 3,color:#555"]
CLASSDEF_KIND = {
    "business": "fill:#e6f4ff,stroke:#1677ff,color:#000",
    "protocol": "fill:#f9f0ff,stroke:#722ed1,color:#000",
    "platform": "fill:#fff7e6,stroke:#d48806,color:#000",
    "data": "fill:#e6fffb,stroke:#13c2c2,color:#000",
    "ops": "fill:#fff1f0,stroke:#cf1322,color:#000",
    "infrastructure": "fill:#f5f5f5,stroke:#595959,color:#000",
    "unknown": "fill:#ffffff,stroke:#8c8c8c,color:#000",
    "none": "fill:#fafafa,stroke:#bfbfbf,stroke-dasharray:3 3,color:#888"}
CLASSDEF_MIXED = "classDef mixed stroke-dasharray:6 3,stroke-width:2px"
CLASSDEF_DEP = "classDef dep fill:#f6ffed,stroke:#52c41a,color:#000"
CLASSDEF_INFRA = "classDef infra fill:#f5f5f5,stroke:#8c8c8c,stroke-dasharray:4 3,color:#333"
CLASSDEF_FOCUS = "classDef focus fill:#fffbe6,stroke:#faad14,stroke-width:3px,color:#000"
CLASSDEF_ACT = ["classDef act fill:#e6f4ff,stroke:#1677ff,color:#000",
                "classDef optional fill:#fafafa,stroke:#1677ff,stroke-dasharray:4 3,color:#000",
                "classDef variant fill:#fafafa,stroke:#8c8c8c,stroke-dasharray:4 3,color:#555",
                "classDef who fill:#fff7e6,stroke:#d48806,color:#000"]
STICK_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="60" '
             b'viewBox="0 0 40 60"><g fill="none" stroke="#333" stroke-width="2.5" '
             b'stroke-linecap="round"><circle cx="20" cy="10" r="7"/>'
             b'<path d="M20 17v22M6 26h28M20 39L8 56M20 39l12 17"/></g></svg>')
STICK = base64.b64encode(STICK_SVG).decode()
USE_SWIMLANE_BETA = True
MAX_UC, MAX_ACTORS, MAX_SEQ_LIFELINES, MAX_D1_EXTERNALS = 8, 6, 5, 10
MAX_D1_EDGES, MAX_D1_INFRA, MAX_ASSOC = 40, 4, 30
# dashed ("only mentioned") lines: more than this tangles the drawing; the tables list all
MAX_D1_DASHED, MAX_DASHED_ASSOC, MAX_DASHED_PER_OVAL = 20, 14, 2
MAX_EDGES, MAX_GROUP_INTRA, MAX_GROUP_CROSS, MAX_EGO = 25, 15, 10, 6
MAX_DEP_LANES, MAX_VARIANTS_DRAWN, MAX_COMPONENTS_PER_GROUP = 3, 3, 10

UC_LEGEND = ("Stick figure = human role; «device»/«system»/«timer» boxes = devices, systems, "
             "timers. Inferred from code.")
CALLMAP_CAPTION = ("Static call-graph paths sampled from entry points: an arrow means code in "
                   "one component calls into another. Label and thickness = walk-weighted "
                   "transitions in the inferred trajectories (walks reaching the later step); "
                   "a two-headed arrow carries calls both ways, labelled heavier / lighter "
                   "direction. This reflects code structure and how many entry points lead "
                   "there, not runtime traffic.")
NO_CALLS = "No calls observed between components at this granularity."
NO_EGO_CALLS = "No calls observed into or out of this component."
NO_OUTSIDE = "No outside party identified for these business logics."


# ---- the model -----------------------------------------------------------------------------

@dataclass
class ActorRef:
    name: str
    label: str
    kind: str
    starts: bool = False        # in the initiator(s)
    evidenced: bool = False     # a trigger source, or a step exchange (direction in/out)
    via: str = ""               # validated via, else interface(deployable, name)
    code_evidence: bool = False  # PKG_EVIDENCE matched for this logic
    via_ok: bool = False        # via came from the llm and passed validate_via
    code_dep: str = ""          # the deployable whose packages hold that evidence


@dataclass
class LogicView:
    t: Trajectory
    bl: BusinessLogic
    tid: str
    title: str
    short: str
    category: str
    group: int                         # primary parent group (-1: ungrouped)
    deployables: list[str]             # A.deployables, else a fallback (never empty)
    actors: list[ActorRef]             # drawable externals, dedup; starters first
    infra: list[ActorRef]
    initiator: Canon
    steps_at: dict[int, list[Step]]
    mapped: bool                       # any step has a valid at
    flow: str                          # sequence | activity | family
    own_support: int
    step_walks: list[int]
    steps: list[Step] = field(default_factory=list)
    party: list[str] = field(default_factory=list)   # per step: canonical actor or ""
    new_schema: bool = False
    ev_tokens: set[str] = field(default_factory=set)
    fnames: set[str] = field(default_factory=set)    # last name segment of evidence functions

    def ref(self, name: str) -> ActorRef | None:
        return next((a for a in self.actors + self.infra if a.name == name), None)

    def party_of(self, step: Step) -> str:
        for s, p in zip(self.steps, self.party):
            if s is step:
                return p
        return ""


@dataclass
class Model:
    k: int
    level: str
    system_name: str
    clusters: dict[int, ClusterInfo]
    parents: dict[int, int]
    parent_infos: dict[int, ClusterInfo]
    group_labels: dict[int, str]       # full: page titles, sidebar, legends
    logics: list[LogicView]
    edges: Counter
    by_cluster: dict[int, list[str]]
    children: dict[int, list[int]]     # group -> clusters by size desc; groups by size desc
    cluster_dep: dict[int, str]
    all_deployables: list[str]
    group_short: dict[int, str] = field(default_factory=dict)   # mermaid subgraph titles
    inferred: bool = True              # some business logic was inferred (not plan only)

    def logic(self, tid: str) -> LogicView | None:
        return next((lv for lv in self.logics if lv.tid == tid), None)


def _n(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def cname(m: Model, c: int) -> str:
    info = m.clusters.get(c)
    return info.name if info and info.name else f"C{c}"


def page_logic(m: Model, tid: str) -> str:
    return f"bizlogic-k{m.k}-{tid}.md"


def page_group(m: Model, pid: int) -> str:
    return f"bizlogic-k{m.k}-p{pid}.md"


def page_cluster(m: Model, c: int) -> str:
    return f"bizlogic-k{m.k}-c{c}.md"


def _cell(s: str) -> str:
    return " ".join(str(s or "").replace("|", "/").split())


def _link(label: str, target: str) -> str:
    return f"[{_cell(label).replace('[', '(').replace(']', ')')}]({target})"


def _coerce(bl: BusinessLogic | None, t: Trajectory) -> BusinessLogic:
    """a business logic with typed actors and steps (older shapes hold plain strings)."""
    bl = bl or BusinessLogic(trajectory=t.id)
    acts = [a if isinstance(a, Actor) else Actor(name=A._name_of(a)) for a in bl.actors or []]
    raw = list(bl.steps or [])
    if raw and all(isinstance(s, str) for s in raw):
        n = len(t.clusters)
        steps = [Step(action=s, at=i + 1 if len(raw) == n else None) for i, s in enumerate(raw)]
    else:
        steps = [s if isinstance(s, Step) else Step(action=str(s)) for s in raw]
    return replace(bl, actors=[a for a in acts if a.name], steps=steps)


def _new_schema(bl: BusinessLogic) -> bool:
    return bool(bl.trigger_sources or bl.scope or bl.implements or bl.supports
                or bl.alternatives or bl.state_changes or bl.data_objects
                or any(a.via for a in bl.actors)
                or any(s.message or s.actor for s in bl.steps))


def _children(clusters: dict[int, ClusterInfo], parents: dict[int, int]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for c in clusters:
        kids.setdefault(parents.get(c, -1), []).append(c)
    size = {c: clusters[c].size for c in clusters}
    for p in kids:
        kids[p].sort(key=lambda c: (-size[c], c))
    order = sorted(kids, key=lambda p: (p < 0, -sum(size[c] for c in kids[p]), p))
    return {p: kids[p] for p in order}


def group_label(m: Model, pid: int) -> str:
    """the group's three largest components in full and "+n" for the rest (never the llm
    parent name): page title, sidebar, legends and the "Group" row."""
    kids = m.children.get(pid, [])
    if pid < 0:
        return "Ungrouped"
    more = f" +{len(kids) - 3}" if len(kids) > 3 else ""
    return f"Group {pid}: " + " · ".join(cname(m, c) for c in kids[:3]) + more


def group_short(m: Model, pid: int) -> str:
    """the group label for a mermaid subgraph title: at most LIM_SUBJECT characters with
    room kept for "+n", so a clipped title still says how many components it has."""
    kids = m.children.get(pid, [])
    if pid < 0:
        return "Ungrouped"
    head = f"Group {pid}: "
    if not kids:
        return head.strip()
    for n in (3, 2, 1):
        n = min(n, len(kids))
        more = f" +{len(kids) - n}" if len(kids) > n else ""
        each = (LIM_SUBJECT - len(head) - len(more) - 3 * max(0, n - 1)) // max(1, n)
        if each >= 14 or n <= 1:
            names = [clip(cname(m, c), max(8, each)) for c in kids[:n]]
            return head + " · ".join(names) + more
    return head.strip()


def group_id(pid: int) -> str:
    return f"p{pid}" if pid >= 0 else "p_none"


def fname(nid: str) -> str:
    return str(nid).split("::", 1)[-1]


def qname(nid: str) -> str:
    pk, _, n = str(nid).partition("::")
    return f"{pk.rsplit('/', 1)[-1]}.{n}" if n else pk


def pkg(nid: str) -> str:
    return str(nid).split("::", 1)[0]


def subservice(nid: str) -> str:
    return "/".join(pkg(nid).split("/")[:2])


NOISE_NAME = re.compile(r"^(Get|get|Set|set|Is|is|Has|has)[A-Z_]|^[Nn]ew\w*Err(or)?$|Err(or)?$"
                        r"|ErrMsg$|Trace|History|^Log|Logger|^String$|^Error$"
                        r"|^(Un)?[Ll]ock$|^New\w*Table$|Table$"
                        r"|(Enc|Dec|Encode|Decode|Marshal|Unmarshal)$")
NOISE_PKG = re.compile(r"(^|/)(vendor|third_party|logger|log|trace)(/|$)")


def is_noise(nid: str) -> bool:
    return bool(NOISE_NAME.search(fname(nid).rsplit(".", 1)[-1]) or NOISE_PKG.search(pkg(nid)))


def _evidence_nodes(t: Trajectory) -> list[str]:
    return list(t.path or []) + [n for step in t.step_functions or [] for n, _ in step]


def _fix_initiator(init: Canon, deps: list[str]) -> Canon:
    """the code under study never starts itself: such an initiator becomes "See trigger"."""
    special = {c.name: c for c in (A.TIMER, A.PROCESS, A.EVENT)}
    if init.kind == "various":
        keep = [x for x in init.members if SELF.get(x) not in deps]
        if len(keep) == len(init.members):
            return init
        if len(keep) > 1:
            return replace(init, members=tuple(keep))
        if not keep:
            return A.SEE_TRIGGER
        x = keep[0]
        return special.get(x) or Canon(x, A.KINDS.get(x, "unknown"), A.LABELS.get(x, ""))
    if init.kind in DRAWABLE and SELF.get(init.name) in deps:
        return A.SEE_TRIGGER
    return init


def _trajectory_deps(t: Trajectory, cluster_dep: dict[int, str]) -> list[str]:
    deps = A.deployables(t)
    if deps:
        return deps
    if t.path:
        return [A.deployable_of(t.path[0])]
    for c in t.clusters:
        if cluster_dep.get(c):
            return [cluster_dep[c]]
    return ["System"]


def _family_by_trigger(trigger: str) -> int:
    low = (trigger or "").lower()
    found = {p for p in PROCEDURES if p.lower() in low}
    found |= {f for _, f in PROCEDURES.values() if f.lower() in low}
    return len(found)


def _view(m: Model, t: Trajectory, bl: BusinessLogic, deps: list[str]) -> LogicView:
    canons = A.drop_self(A.normalize_all([a.name for a in bl.actors]), deps)
    drawable = [c for c in canons if c.kind in DRAWABLE]
    infra = [c for c in canons if c.kind == "infra"]
    init = A.controller_initiator(
        _fix_initiator(A.resolve_initiators(bl, drawable), deps), t.roots)
    new = _new_schema(bl)
    starters = [init] if init.kind != "various" else [
        Canon(x, A.KINDS[x], A.LABELS.get(x, "")) for x in init.members if x in A.KINDS]
    for c in starters:
        if c.kind in DRAWABLE | {"infra"} and all(x.name != c.name for x in drawable + infra):
            (infra if c.kind == "infra" else drawable).append(c)
    # the outside party of every step
    party: list[str] = []
    for s in bl.steps:
        c = next((x for x in A.normalize(s.actor) if (x.kind in DRAWABLE or x.kind == "infra")
                  and not A.is_self(x, deps)), None) if s.actor else None
        party.append(c.name if c else "")
        if c and c.kind in DRAWABLE and s.direction != "internal" and \
                all(x.name != c.name for x in drawable):
            drawable.append(c)
        if c and c.kind == "infra" and all(x.name != c.name for x in infra):
            infra.append(c)
    nodes = _evidence_nodes(t)
    # code evidence per deployable: an interface package counts for the deployable that
    # holds it, never for whichever deployable the logic happens to be rooted in
    by_dep: dict[str, list[str]] = {}
    for nid in list(t.roots or []) + nodes:
        by_dep.setdefault(A.deployable_of(nid), []).append(nid)
    pe_dep = {dp: A.pkg_evidence(ns) for dp, ns in by_dep.items()}
    dep_rank = sorted(by_dep, key=lambda dp: (dp not in deps,
                                              deps.index(dp) if dp in deps else 0,
                                              -len(by_dep[dp]), dp))

    def evidence_of(name: str) -> tuple[str, str]:
        """(deployable, interface label) of the code evidencing `name`, else ("", "")."""
        for dp in dep_rank:
            if name in pe_dep[dp] and SELF.get(name) != dp:
                return dp, pe_dep[dp][name]
        return "", ""

    exch = {p for p, s in zip(party, bl.steps) if p and s.direction in ("in", "out")}
    named = {p for p in party if p}

    def via_of(name: str) -> str:
        for a in bl.actors:
            if a.via and any(c.name == name for c in A.normalize(a.name)) and \
                    A.validate_via(a.via, name, deps):
                return a.via
        return ""

    refs: list[ActorRef] = []
    for c in drawable:
        c = A.colocate(c, m.all_deployables)
        st = A.starts(c, init)
        v = via_of(c.name)
        cdep, clabel = evidence_of(c.name)
        refs.append(ActorRef(c.name, c.display, c.kind, starts=st,
                             evidenced=(st and bool(bl.trigger_sources)) or c.name in exch,
                             via=v or clabel or interface(deps[0], c.name),
                             code_evidence=bool(cdep), via_ok=bool(v), code_dep=cdep))
    order = {id(r): i for i, r in enumerate(refs)}
    refs.sort(key=lambda r: (0 if r.starts else 1 if r.evidenced else 2, order[id(r)]))
    irefs = [ActorRef(c.name, c.display, "infra", starts=A.starts(c, init),
                      evidenced=c.name in named) for c in infra]
    steps_at: dict[int, list[Step]] = {}
    for s in bl.steps:
        if s.at is not None and 1 <= s.at <= len(t.clusters):
            steps_at.setdefault(s.at, []).append(s)
    n = len(t.clusters)
    own = t.own_support if t.step_walks else t.support
    walks = list(t.step_walks) if len(t.step_walks or []) == n else [t.support] * n
    gcount = Counter(m.parents.get(c, -1) for c in t.clusters)
    first = {}
    for i, c in enumerate(t.clusters):
        first.setdefault(m.parents.get(c, -1), i)
    group = min(gcount, key=lambda g: (-gcount[g], first[g])) if gcount else -1
    drawn = {r.name for r in refs}
    exchange = any(p in drawn and s.direction in ("in", "out") and s.at is not None
                   for p, s in zip(party, bl.steps))
    # at the capability level a logic is drawn as a family only when it looks like one:
    # one entry root with one path and no variants is a single procedure at any zoom
    family_signs = (len(t.roots or []) > 1 or bool(t.variants)
                    or len(bl.implements) + len(bl.supports) >= 2)
    if (m.level == "capability family" and family_signs) or bl.scope == "family" or (
            not bl.scope and (len(bl.implements) + len(bl.supports) >= 2
                              or _family_by_trigger(bl.trigger) >= 2
                              or len(t.roots or []) > 10)):
        flow = "family"
    elif steps_at and exchange:
        flow = "sequence"
    else:
        flow = "activity"
    ev: set[str] = set()
    for nid in nodes:
        ev |= tokens(fname(nid))
    title = bl.title or f"Trajectory {t.id}"
    return LogicView(
        t=t, bl=bl, tid=t.id, title=title, short=bl.short_title or clip(title, 36),
        category=bl.category, group=group, deployables=deps, actors=refs, infra=irefs,
        initiator=init, steps_at=steps_at, mapped=bool(steps_at), flow=flow,
        own_support=own, step_walks=walks, steps=list(bl.steps), party=party,
        new_schema=new, ev_tokens=ev,
        fnames={fname(nid).rsplit(".", 1)[-1] for nid in nodes})


def cluster_deployable(info: ClusterInfo) -> str:
    """the deployable holding most of a cluster's functions (all its packages, not only the
    largest one)."""
    w: Counter = Counter()
    for p, n in info.packages or []:
        w[A.deployable_of(p)] += n
    return min(w, key=lambda d: (-w[d], d)) if w else ""


def build_model(k: int, clusters: dict[int, ClusterInfo], parents: dict[int, int],
                parent_infos: dict[int, ClusterInfo], trajs: list[Trajectory],
                logic: dict[str, BusinessLogic], system_name: str = "") -> Model:
    """everything the diagrams of one granularity need, computed once."""
    m = Model(k=k, level=target_level(k)[0], system_name=system_name or "",
              clusters=dict(clusters), parents=dict(parents or {}),
              parent_infos=dict(parent_infos or {}), group_labels={}, logics=[],
              edges=Counter(), by_cluster={}, children=_children(clusters, parents or {}),
              cluster_dep={c: cluster_deployable(i) for c, i in clusters.items()},
              all_deployables=[], inferred=any(b and b.title for b in (logic or {}).values()))
    m.group_labels = {p: group_label(m, p) for p in m.children}
    m.group_short = {p: group_short(m, p) for p in m.children}
    pre = []
    for t in trajs:
        bl = _coerce((logic or {}).get(t.id), t)
        pre.append((t, bl, _trajectory_deps(t, m.cluster_dep)))
    cnt = Counter(d for _, _, deps in pre for d in deps)
    m.all_deployables = sorted(cnt, key=lambda d: (-cnt[d], d))
    for t, bl, deps in pre:
        lv = _view(m, t, bl, deps)
        m.logics.append(lv)
        for i in range(len(t.clusters) - 1):
            a, b = t.clusters[i], t.clusters[i + 1]
            if a != b:
                m.edges[(a, b)] += lv.step_walks[i + 1]
        for c in dict.fromkeys(t.clusters):
            m.by_cluster.setdefault(c, []).append(t.id)
    return m


def phase_tag(m: Model, i_cluster: int) -> str:
    return "[" + clip(cname(m, i_cluster), LIM_TAG) + "]"


def phase_label(m: Model, lv: LogicView, i: int) -> str:
    """the actions of step i (1-based), "" when none is mapped (the [component] tag next
    to it already names the cluster)."""
    return "; ".join(s.action for s in lv.steps_at.get(i, []) if s.action)


def optional_phase(lv: LogicView, i: int) -> bool:
    return lv.step_walks[i - 1] < OPTIONAL_SHARE * lv.t.support


def parties(lv: LogicView, limit: int = 4) -> str:
    """outside parties for a table cell: evidenced first, at most `limit` and " +n"."""
    names = [a.label for a in lv.actors]
    more = f" +{len(names) - limit}" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def initiated_by(lv: LogicView) -> str:
    return A.initiator_text(lv.initiator)


# ---- message verification -----------------------------------------------------------------

_ABBR = {"cxt": "context", "ctx": "context", "rel": "release", "cmd": "command",
         "req": "request", "rsp": "response", "res": "response", "notif": "notification",
         "ntf": "notification", "reg": "registration", "dereg": "deregistration",
         "auth": "authentication", "sec": "security", "upd": "update",
         "del": "delete", "est": "establishment", "mod": "modification", "pol": "policy",
         "loc": "location", "sess": "session",
         "subs": "subscription", "rej": "reject", "cmp": "complete", "cmpl": "complete",
         "comp": "complete", "fail": "failure", "ack": "acknowledge", "notify": "notification",
         "ind": "indication", "cancellation": "cancel"}
_GENERIC = {"send", "recv", "receive", "handle", "handler", "proc", "process", "main", "func",
            "do", "on", "request", "response", "message", "msg", "the", "and", "for", "with",
            "from", "to", "of", "api", "client", "server", "scenario", "control", "ctl"}
# the active profile adds party names (PARTY_TOKENS: a message needs one word besides
# them), word aliases (TOKEN_ALIASES, e.g. a service prefix -> its party) and short words
# that still count (SHORT_TOKENS)


def tokens(s: str) -> set[str]:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", str(s or ""))
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    out: set[str] = set()
    for w in re.split(r"[^A-Za-z0-9]+", s.lower()):
        w = TOKEN_ALIASES.get(w, w)
        out |= set(_ABBR.get(w, w).split())
    return {w for w in out if w and w not in _GENERIC and (len(w) > 2 or w in SHORT_TOKENS)}


# the word that tells the outcomes of one procedure apart (Accept / Reject, Command /
# Complete): a message naming one must find it in the code, not only the procedure noun
_MSG_TYPE = {"accept", "reject", "complete", "failure", "command", "acknowledge",
             "notification", "cancel", "indication", "required", "confirm"}


def verify_message(msg: str, lv: LogicView) -> bool:
    """the message name matches function names on the logic's path or step evidence: half
    its words, one besides party names, and its message-type word (accept, reject, …)."""
    content = tokens(msg)
    if not content:
        return False
    hit = content & lv.ev_tokens
    if content & _MSG_TYPE - lv.ev_tokens:
        return False
    return len(hit) >= max(1, math.ceil(len(content) / 2)) and bool(hit - PARTY_TOKENS)


# ---- actor nodes -------------------------------------------------------------------------

def actor_node(d: Dia, ref: ActorRef) -> str:
    """a use-case / context actor: stick figure (human), «device», «timer» or «system» box."""
    aid = d.id("a_", ref.name)
    if d.has(aid):
        return aid
    label = ref.label or ref.name
    if ref.name in HUMAN:
        return d.node(aid, f'{aid}@{{ img: "data:image/svg+xml;base64,{STICK}", '
                           f'label: "{mm(label, limit=LIM_ACTOR)}", pos: "b", w: 28, h: 42, '
                           f'constraint: "on" }}')
    st = "«device»" if ref.name in A.DEVICE else "«timer»" if ref.kind == "timer" else "«system»"
    return d.node(aid, f'{aid}["{st}<br>{mm(label, limit=LIM_ACTOR)}"]:::sysactor')


def other_node(d: Dia, n: int) -> str:
    return d.node("a_other", f'a_other["«system»<br>Other parties ({n})"]:::more')


def _timer_ref(lv: LogicView) -> ActorRef | None:
    init = lv.initiator
    if init.kind == "timer" or "Timer" in init.members:
        return ActorRef("Timer", "Timer", "timer", starts=True,
                        evidenced="timer" in lv.bl.trigger_sources)
    return None


def _subject(d: Dia, dep: str) -> str:
    return d.id("sys_", dep)


def _strength(a: ActorRef, dep: str = "") -> int:
    """2 = the code of `dep` implements the interface to `a` (thick), 1 = the llm shows an
    exchange or a validated via (solid), 0 = only mentioned (dashed)."""
    if a.code_evidence and (not dep or (a.code_dep == dep and interface(dep, a.name))):
        return 2
    return 1 if (a.evidenced or a.via_ok) else 0


def _edge_dep(m: Model, lv: LogicView, a: ActorRef) -> str:
    """the deployable box an actor's D1 edge attaches to: the one whose packages evidence
    the interface, else the logic's first deployable."""
    for dep in (a.code_dep, lv.deployables[0]):
        if dep and dep in m.all_deployables and SELF.get(a.name) != dep:
            return dep
    return lv.deployables[0]


# ---- D1 system context --------------------------------------------------------------------

def context_diagram(m: Model) -> tuple[Dia, str]:
    d = Dia([FLOW_INIT, "flowchart LR"])
    for line in (CLASSDEF_UC[1], CLASSDEF_DEP, CLASSDEF_INFRA, CLASSDEF_UC[2]):
        d.raw(line)
    pair: dict[tuple[str, str], list[int]] = {}       # (dep, actor) -> [count, strength]
    ref: dict[str, ActorRef] = {}
    total, new_n, new_starts = Counter(), Counter(), Counter()
    infra_dep: Counter = Counter()
    runs = Counter(dep for lv in m.logics for dep in lv.deployables)
    for lv in m.logics:
        for a in lv.actors:
            ref.setdefault(a.name, a)
            dep = _edge_dep(m, lv, a)
            p = pair.setdefault((dep, a.name), [0, 0])
            p[0] += 1
            p[1] = max(p[1], _strength(a, dep))
            total[a.name] += 1
            if lv.new_schema:
                new_n[a.name] += 1
                new_starts[a.name] += a.starts
        for i in lv.infra:
            infra_dep[(i.name, lv.deployables[0])] += 1
    deps = list(m.all_deployables)
    box = {dep: d.id("n_", dep) for dep in deps or [m.system_name or "System"]}
    coloc = {x for x in ref if ref[x].kind == "colocated" and SELF.get(x) in deps}

    # an edge is drawn when something supports it; a dashed edge also needs a known
    # interface and two logics, else its party folds into "Other parties"
    def keep(dep: str, x: str, count: int, s: int) -> bool:
        return dep in box and (s > 0 or (count >= 2 and bool(interface(dep, x))))

    cands = sorted(((dep, x, c, s) for (dep, x), (c, s) in pair.items() if keep(dep, x, c, s)),
                   key=lambda e: (-e[3], -e[2], e[0], e[1]))
    externals = sorted((x for x in ref if x not in coloc and ref[x].kind != "colocated"),
                       key=lambda x: (-total[x], x))
    linked = [x for x in externals if any(e[1] == x for e in cands)]
    shown = set(linked[:MAX_D1_EXTERNALS])
    infra_names = sorted({i for i, _ in infra_dep},
                         key=lambda i: (-sum(v for (j, _), v in infra_dep.items() if j == i), i))
    infra_names = infra_names[:MAX_D1_INFRA]
    budget = MAX_D1_EDGES - len(infra_names) - 1
    chosen, dashed = [], 0
    for e in cands:
        if len(chosen) >= budget:
            break
        if e[1] not in shown and e[1] not in coloc:
            continue
        if e[3] == 0:
            if dashed >= MAX_D1_DASHED:
                continue
            dashed += 1
        chosen.append(e)
    drawn = {e[1] for e in chosen}
    shown_order = [x for x in externals if x in shown and x in drawn]
    folded = [x for x in externals if x not in drawn]
    convention = False
    left = set()
    for x in shown_order + [x for x in coloc if x in drawn]:
        if new_n[x]:
            if new_starts[x] * 2 >= new_n[x] and new_starts[x]:
                left.add(x)
        else:
            convention = True
            if x in A.DEFAULT_PRIMARY:
                left.add(x)
    for x in shown_order:
        if x in left:
            actor_node(d, ref[x])
    title = mm(m.system_name or "System under study", limit=LIM_SUBJECT)
    d.node("sys", f'subgraph sys ["{title}"]')
    for dep, nid in box.items():
        d.node(nid, f'{nid}["{mm(dep, limit=LIM_NODE)}<br>{_n(runs[dep], "logic")}"]:::dep')
    d.raw("end")
    for x in shown_order:
        if x not in left:
            actor_node(d, ref[x])
    if folded:
        other_node(d, len(folded))
    for i in infra_names:
        iid = d.id("i_", i)
        d.node(iid, f'{iid}[("{mm(i, limit=LIM_NODE)}")]:::infra')
    thick: list[int] = []
    for dep, x, count, s in chosen:
        lab = mm(f"{interface(dep, x) or '?'} · {count}", limit=LIM_NODE)
        arrow = "-->" if s else "-.->"
        if x in coloc:
            a, b = box[dep], box[SELF[x]]
            if x in left:
                a, b = b, a
        else:
            a, b = (actor_node(d, ref[x]), box[dep])
            if x not in left:
                a, b = b, a
        idx = d.edge(a, b, f'{a} {arrow}|"{lab}"| {b}')
        if s == 2:
            thick.append(idx)
    if folded:
        d.edge("a_other", "sys", "a_other -.- sys")
    for i in infra_names:
        dep = max((dp for (j, dp) in infra_dep if j == i and dp in box),
                  key=lambda dp: (infra_dep[(i, dp)], dp), default=None)
        src = box[dep] if dep else "sys"
        iid = d.id("i_", i)
        d.edge(src, iid, f"{src} -.- {iid}")
    if not d.edges:
        # an edge-free subgraph is laid out in one long row: invisible links stack the
        # boxes into columns of four
        ids = list(box.values())
        for a, b in zip(ids, ids[4:]):
            d.edge(a, b, f"{a} ~~~ {b}")
    if thick:
        d.raw(f"linkStyle {','.join(map(str, thick))} stroke-width:3.5px")
    if not ref:
        caption = ("No business logic was inferred, so no outside party is shown."
                   if not m.inferred else
                   "No external party was identified in the inferred business logic.")
    else:
        caption = ("Arrows are labelled interface · number of business logics. Thick = this "
                   "deployable's packages implement that interface; "
                   "solid = the LLM shows an exchange; dashed = the party is only mentioned "
                   "(drawn when at least two logics mention it over a known interface; the "
                   "rest are under Other parties). An arrow from a party into the system = "
                   "that party starts the logic"
                   + (" (by convention: old data)" if convention else "")
                   + ". Boxes inside the boundary are separate deployables of this repository "
                   "and act as peers to each other.")
    return d, caption


# ---- D2 procedure use cases ---------------------------------------------------------------

D2_CAPTION = ("Use cases = procedure families the business logics implement (LLM-assigned "
              "from the domain profile's fixed list). A line = at least two logics (or the "
              "family's only logic) show an exchange with that party; a line to another "
              "«system» box = a deployable of this repository takes part. Stick figure = human "
              "role; «device»/«system» boxes = devices and other systems.")


def procedure_usecases(m: Model) -> tuple[Dia | None, list[str]]:
    """(diagram, caption + the procedure index table); (None, []) without implements (always
    so without a domain profile that lists procedures)."""
    if not any(lv.bl.implements for lv in m.logics):
        return None, []
    fam: dict[str, list[LogicView]] = {}
    for lv in m.logics:
        fs = list(dict.fromkeys(PROCEDURES[p][1] for p in lv.bl.implements if p in PROCEDURES))
        if not fs and lv.category == "operations & maintenance" and D.active().ops_family:
            fs = [D.active().ops_family]
        for f in fs:
            fam.setdefault(f, []).append(lv)
    weight = {f: sum(lv.own_support for lv in ls) for f, ls in fam.items()}
    ranked = sorted(fam, key=lambda f: (-weight[f], f))
    shown, rest = ranked[:MAX_UC], ranked[MAX_UC:]
    d = Dia([FLOW_INIT, "flowchart LR"])
    for line in CLASSDEF_UC:
        d.raw(line)
    edges: dict[str, list[str]] = {}               # actor -> families
    ref: dict[str, ActorRef] = {}
    st, sup = Counter(), Counter()
    home = {}
    for f in shown:
        c = Counter(lv.deployables[0] for lv in fam[f])
        home[f] = min(c, key=lambda x: (-c[x], x))
    subjects = list(dict.fromkeys(home[f] for f in shown))
    coloc: list[tuple[str, str]] = []              # (family, subject) of a peer deployable
    for f in shown:
        ls = fam[f]
        cnt: Counter = Counter()
        for lv in ls:
            for a in lv.actors:
                if a.evidenced:
                    cnt[a.name] += 1
                    ref.setdefault(a.name, a)
                    sup[a.name] += 1
                    st[a.name] += a.starts
        for x, c in cnt.items():
            if c < min(2, len(ls)):
                continue
            # a deployable of this repository is never an outside actor of its own
            # subject; as another subject it is linked as that box (as in D7)
            dep = SELF.get(x) if ref[x].kind == "colocated" else None
            if dep and dep == home[f]:
                continue
            if dep and dep in subjects:
                coloc.append((f, dep))
                continue
            edges.setdefault(x, []).append(f)
    actors_ranked = sorted(edges, key=lambda x: (-len(edges[x]), x))
    keep, fold = actors_ranked[:MAX_ACTORS], actors_ranked[MAX_ACTORS:]
    left = {x for x in keep if st[x] * 2 >= sup[x] and st[x]}
    for x in keep:
        if x in left:
            actor_node(d, ref[x])
    gid: dict[str, str] = {}
    for dep in subjects:
        sid = _subject(d, dep)
        d.node(sid, f'subgraph {sid} ["«system» {mm(dep, limit=LIM_SUBJECT - 9)}"]')
        for f in shown:
            if home[f] == dep:
                g = gid[f] = d.id("g_", f)
                d.node(g, f'{g}(["{mm(f, limit=LIM_OVAL)}<br>{_n(len(fam[f]), "logic")}"]):::uc')
        if dep == subjects[0] and rest:
            d.node("g_more", f'g_more(["+{len(rest)} more families (see table)"]):::more')
        d.raw("end")
    for x in keep:
        if x not in left:
            actor_node(d, ref[x])
    if fold:
        other_node(d, len(fold))
    for x in keep:
        a = d.id("a_", x)
        for f in edges[x]:
            g = gid[f]
            d.edge(a, g, f"{a} --- {g}") if x in left else d.edge(g, a, f"{g} --- {a}")
    for f, dep in dict.fromkeys(coloc):
        g, sid = gid[f], _subject(d, dep)
        d.edge(g, sid, f"{g} --- {sid}")
    if fold:
        s0 = _subject(d, subjects[0])
        d.edge("a_other", s0, f"a_other -.- {s0}")
    return d, [D2_CAPTION, ""] + procedure_index(m)


def procedure_index(m: Model) -> list[str]:
    impl: dict[str, list[str]] = {}
    sup: dict[str, list[str]] = {}
    for lv in m.logics:
        for p in lv.bl.implements:
            impl.setdefault(p, []).append(lv.tid)
        for p in lv.bl.supports:
            sup.setdefault(p, []).append(lv.tid)
    if not impl:
        return []
    keys = list(PROCEDURES)
    rows = sorted((p for p in impl if p in PROCEDURES),
                  key=lambda p: (-len(impl[p]), keys.index(p)))
    out = ["| procedure | reference | implemented by | supported by |", "|---|---|---|---|"]
    for p in rows:
        s = sup.get(p, [])
        sl = ", ".join(_link(x, page_logic(m, x)) for x in s[:6]) + \
            (f" +{len(s) - 6}" if len(s) > 6 else "")
        out.append(f"| {_cell(p)} | {_cell(PROCEDURES[p][0])} | "
                   f"{', '.join(_link(x, page_logic(m, x)) for x in impl[p])} | {sl} |")
    return out


# ---- actor x group matrix ----------------------------------------------------------------

def actor_matrix(m: Model) -> list[str]:
    """markdown table: top 10 actors x groups; "3 (5)" = 3 evidenced, 5 only listed."""
    ev: Counter = Counter()
    lo: Counter = Counter()
    n: Counter = Counter()
    label: dict[str, str] = {}
    for lv in m.logics:
        for a in lv.actors:
            label.setdefault(a.name, a.label)
            n[a.name] += 1
            (ev if a.evidenced else lo)[(a.name, lv.group)] += 1
    if not n:
        return []
    rows = sorted(n, key=lambda x: (-n[x], x))[:10]
    groups = list(m.children)
    out = ["| actor | " + " | ".join(_link(f"G{p}", page_group(m, p)) for p in groups) + " |",
           "|---|" + "---|" * len(groups)]
    for x in rows:
        cells = []
        for p in groups:
            e, li = ev[(x, p)], lo[(x, p)]
            cells.append(f"{e} ({li})" if e and li else str(e) if e else f"({li})" if li else "·")
        out.append(f"| {_cell(label[x])} | " + " | ".join(cells) + " |")
    out += [""] + [f"- G{p} = {_cell(m.group_labels.get(p, ''))}" for p in groups]
    return out


# ---- D3 component view -------------------------------------------------------------------

D3_LEGEND = ("Colour = component kind (LLM label): business (blue), protocol (purple), platform "
             "(orange), data (teal), ops (red), infrastructure (grey), white = kind not "
             "labelled; dashed grey = no business logic passes through it; dashed border = "
             "mixed (packages from more than one deployable). Names are LLM labels; the "
             "package line shows what the component really contains.")
D3_LEGEND_PLAN = ("Components were not labelled by the LLM: white = business logic passes "
                  "through it, dashed grey = none does; dashed border = mixed (packages from "
                  "more than one deployable). The package line shows what each contains.")


def kind_class(info: ClusterInfo | None) -> str:
    k = (info.kind if info else "").strip().lower()
    for key in ("business", "protocol", "platform", "data", "infrastructure"):
        if key in k:
            return key
    if k in ("ops", "o&m") or "operation" in k or k.startswith("ops"):
        return "ops"
    if "infra" in k:
        return "infrastructure"
    return "business" if k else "unknown"


def top_packages(info: ClusterInfo | None, n: int = 2) -> list[str]:
    out: list[str] = []
    for p, _ in (info.packages if info else []):
        short = "/".join(p.split("/")[:2])
        if short not in out:
            out.append(short)
        if len(out) == n:
            break
    return out


def is_mixed(info: ClusterInfo | None) -> bool:
    return len({A.deployable_of(p) for p, _ in (info.packages if info else [])[:3]}) > 1


def _cls(m: Model, c: int) -> str:
    return "none" if not m.by_cluster.get(c) else kind_class(m.clusters.get(c))


def component_view(m: Model) -> tuple[Dia, str]:
    d = Dia([FLOW_INIT, "flowchart TB"])
    used = sorted({_cls(m, c) for c in m.clusters} | {"none"})
    for k in used:
        d.raw(f"classDef k_{k} {CLASSDEF_KIND[k]}")
    d.raw(CLASSDEF_MIXED)
    chains: list[list[str]] = []
    mixed_ids: list[str] = []
    groups = list(m.children)
    # dagre places unlinked subgraphs right to left in declaration order: declared in
    # reverse, group 0 (the largest) is the left-most column
    for pid in reversed(groups):
        gid = group_id(pid)
        d.node(gid, f'subgraph {gid} ["{mm(m.group_short.get(pid, ""), limit=LIM_SUBJECT)}"]')
        # a subgraph with no link to the outside is laid out in the flipped direction
        # (one long row per group) unless its direction is set
        d.raw("direction TB")
        kids = m.children[pid]
        shown = kids[:MAX_COMPONENTS_PER_GROUP] if len(m.clusters) > 40 else kids
        ids = []
        for c in shown:
            info = m.clusters.get(c)
            nl = len(m.by_cluster.get(c, []))
            mixed = is_mixed(info)
            pk = ", ".join(top_packages(info))
            lab = (f"{mm(cname(m, c), limit=34)}<br>{mm(pk, limit=LIM_NODE)}<br>"
                   f"{info.size if info else 0} fns · {_n(nl, 'logic')}"
                   + (" · mixed" if mixed else ""))
            d.node(f"c{c}", f'c{c}["{lab}"]:::k_{_cls(m, c)}')
            ids.append(f"c{c}")
            if mixed:
                mixed_ids.append(f"c{c}")
        if len(shown) < len(kids):
            mid = f"c_more_{gid}"
            d.node(mid, f'{mid}["+{len(kids) - len(shown)} more"]:::k_none')
            ids.append(mid)
        d.raw("end")
        chains.append(ids)
    if mixed_ids:      # 11.16 cannot chain ":::a:::b"; a class statement adds the border
        d.raw(f"class {','.join(mixed_ids)} mixed", refs=mixed_ids)
    for ids in chains:
        for a, b in zip(ids, ids[1:]):
            d.edge(a, b, f"{a} ~~~ {b}")
    for i in range(len(groups) - 4):
        a, b = group_id(groups[i]), group_id(groups[i + 4])
        d.edge(a, b, f"{a} ~~~ {b}")
    plan = not any(info.kind for info in m.clusters.values())
    return d, D3_LEGEND_PLAN if plan else D3_LEGEND


def component_table(m: Model) -> list[str]:
    out = ["| group | component | top packages | functions | kind | business logics |",
           "|---|---|---|---|---|---|"]
    for pid, kids in m.children.items():
        for c in kids:
            info = m.clusters.get(c)
            out.append(f"| G{pid} | {_link(cname(m, c), page_cluster(m, c))} | "
                       f"{_cell(', '.join(top_packages(info, 3)))} | "
                       f"{info.size if info else 0} | {_cell(info.kind if info else '')} | "
                       f"{len(m.by_cluster.get(c, []))} |")
    return out


# ---- D4 / D8 call map and D13 ego map ----------------------------------------------------

def _tiers(d: Dia, weighted: list[tuple[int, int]]) -> None:
    """linkStyle width tiers relative to the largest weight: (edge index, weight)."""
    if not weighted:
        return
    top = max(w for _, w in weighted) or 1
    tiers: dict[str, list[int]] = {"4px": [], "2.5px": [], "1.2px": []}
    for idx, w in weighted:
        key = "4px" if w >= 0.25 * top else "2.5px" if w >= 0.05 * top else "1.2px"
        tiers[key].append(idx)
    for width, idxs in tiers.items():
        if idxs:
            d.raw(f"linkStyle {','.join(map(str, idxs))} stroke-width:{width}")


def _weighted(d: Dia, items: list[tuple[str, str, int]]) -> list[tuple[int, int]]:
    """weighted call edges (from, to, weight) -> [(edge index, weight)]: a reciprocal pair
    becomes one two-headed edge "heavier / lighter" (two parallel edges get the same route
    and their labels land on top of each other)."""
    w = {(a, b): x for a, b, x in items}
    out: list[tuple[int, int]] = []
    done: set[tuple[str, str]] = set()
    for a, b, x in items:
        if (a, b) in done:
            continue
        y = w.get((b, a)) if a != b else None
        done |= {(a, b), (b, a)}
        if y is None:
            out.append((d.edge(a, b, f'{a} -->|"{x}"| {b}'), x))
            continue
        if y > x:
            a, b, x, y = b, a, y, x
        out.append((d.edge(a, b, f'{a} <-->|"{x} / {y}"| {b}'), x))
    return out


def _kind_defs(d: Dia, m: Model, cs) -> None:
    for k in sorted({_cls(m, c) for c in cs} | {"none"}):
        d.raw(f"classDef k_{k} {CLASSDEF_KIND[k]}")


def call_map(m: Model, pid: int | None = None) -> tuple[Dia | str, str]:
    """D4 (overview, pid None) or D8 (one group); ("", caption) when no call is observed."""
    total = sum(m.edges.values())
    if pid is None:
        kept = sorted(m.edges.items(), key=lambda e: (-e[1], e[0]))[:MAX_EDGES]
        if not kept:
            return "", NO_CALLS
        d = Dia([FLOW_INIT, "flowchart LR"])
        nodes = list(dict.fromkeys(c for (a, b), _ in kept for c in (a, b)))
        _kind_defs(d, m, nodes)
        for p in m.children:
            inside = [c for c in nodes if m.parents.get(c, -1) == p]
            if not inside:
                continue
            gid = group_id(p)
            d.node(gid, f'subgraph {gid} ["{mm(m.group_short.get(p, ""), limit=LIM_SUBJECT)}"]')
            for c in inside:
                d.node(f"c{c}", f'c{c}["{mm(cname(m, c), limit=30)}"]:::k_{_cls(m, c)}')
            d.raw("end")
        for c in nodes:
            if not d.has(f"c{c}"):
                d.node(f"c{c}", f'c{c}["{mm(cname(m, c), limit=30)}"]:::k_{_cls(m, c)}')
        weighted = _weighted(d, [(f"c{a}", f"c{b}", w) for (a, b), w in kept])
        _tiers(d, weighted)
        w_kept = sum(w for _, w in kept)
        pct = round(100 * w_kept / total) if total else 0
        return d, (CALLMAP_CAPTION + f" Showing {len(kept)} of {len(m.edges)} transitions "
                   f"({pct}% of the weight).")
    kids = m.children.get(pid, [])
    mine = set(kids)
    intra = sorted(((ab, w) for ab, w in m.edges.items() if ab[0] in mine and ab[1] in mine),
                   key=lambda e: (-e[1], e[0]))[:MAX_GROUP_INTRA]
    cross: Counter = Counter()
    for (a, b), w in m.edges.items():
        if a in mine and b not in mine:
            cross[(f"c{a}", ("ext", m.parents.get(b, -1)))] += w
        elif b in mine and a not in mine:
            cross[(("ext", m.parents.get(a, -1)), f"c{b}")] += w
    cross_kept = sorted(cross.items(), key=lambda e: (-e[1], str(e[0])))[:MAX_GROUP_CROSS]
    if not intra and not cross_kept:
        return "", NO_CALLS
    d = Dia([FLOW_INIT, "flowchart LR"])
    _kind_defs(d, m, kids)
    gid = group_id(pid)
    d.node(gid, f'subgraph {gid} ["{mm(m.group_short.get(pid, ""), limit=LIM_SUBJECT)}"]')
    for c in kids:
        d.node(f"c{c}", f'c{c}["{mm(cname(m, c), limit=30)}"]:::k_{_cls(m, c)}')
    d.raw("end")

    def ext(x) -> str:
        if isinstance(x, str):
            return x
        q = x[1]
        eid = f"ext_{group_id(q)}"
        if not d.has(eid):
            d.node(eid, f'{eid}[["{mm(m.group_short.get(q, "Ungrouped"), limit=LIM_SUBJECT)}"]]'
                        ":::k_none")
        return eid

    items = [(f"c{a}", f"c{b}", w) for (a, b), w in intra]
    items += [(ext(a), ext(b), w) for (a, b), w in cross_kept]
    _tiers(d, _weighted(d, items))
    return d, CALLMAP_CAPTION


def ego_map(m: Model, c: int) -> tuple[Dia | str, str]:
    """D13: the strongest callers and callees of one component."""
    pre = sorted(((a, w) for (a, b), w in m.edges.items() if b == c and a != c),
                 key=lambda e: (-e[1], e[0]))[:MAX_EGO]
    post = sorted(((b, w) for (a, b), w in m.edges.items() if a == c and b != c),
                  key=lambda e: (-e[1], e[0]))[:MAX_EGO]
    if not pre and not post:
        return "", NO_EGO_CALLS
    d = Dia([FLOW_INIT, "flowchart LR"])
    _kind_defs(d, m, [x for x, _ in pre + post])
    d.raw(CLASSDEF_FOCUS)
    d.node(f"c{c}", f'c{c}["{mm(cname(m, c), limit=30)}"]:::focus')
    for x, _ in pre + post:
        if not d.has(f"c{x}"):
            d.node(f"c{x}", f'c{x}["{mm(cname(m, x), limit=30)}"]:::k_{_cls(m, x)}')
    items = [(f"c{a}", f"c{c}", w) for a, w in pre] + [(f"c{c}", f"c{b}", w) for b, w in post]
    _tiers(d, _weighted(d, items))
    return d, CALLMAP_CAPTION


# ---- D7 group use cases and D9 participants -----------------------------------------------

def _uc_edges(lv: LogicView) -> list[tuple[ActorRef, bool]]:
    """(actor, solid) pairs a use-case oval links to: evidenced (new schema) or listed."""
    out = [(a, True) for a in lv.actors if a.evidenced] if lv.new_schema else \
        [(a, False) for a in lv.actors]
    tm = _timer_ref(lv)
    if tm and out:
        out.insert(0, (tm, tm.evidenced))
    return out


def _uc_label(lv: LogicView) -> str:
    return mm(f"{lv.tid} · {lv.short}", "quoted", LIM_OVAL)


def _draw_usecases(d: Dia, ovals: list[LogicView], more: int, oid) -> int:
    """subjects with ovals, actors left/right, associations; returns the association count.
    every solid line is drawn (up to MAX_ASSOC); dashed ones only for each oval's
    MAX_DASHED_PER_OVAL most linked parties, MAX_DASHED_ASSOC in all."""
    links: dict[str, list[tuple[LogicView, bool]]] = {}
    ref: dict[str, ActorRef] = {}
    subjects = list(dict.fromkeys(lv.deployables[0] for lv in ovals))
    coloc_links: list[tuple[LogicView, str, bool]] = []
    for lv in ovals:
        for a, solid in _uc_edges(lv):
            if a.kind == "colocated" and SELF.get(a.name) in subjects:
                coloc_links.append((lv, SELF[a.name], solid))
                continue
            ref.setdefault(a.name, a)
            links.setdefault(a.name, []).append((lv, solid))
    ranked = sorted(links, key=lambda x: (-len(links[x]), x))
    top = ranked[:MAX_ACTORS]
    budget = MAX_ASSOC - 1
    chosen: list[tuple[str, LogicView, bool]] = []      # ("" = colocated subject link)
    coloc_chosen: list[tuple[LogicView, str, bool]] = []
    dashed = 0
    for lv, dep, solid in coloc_links:
        if len(coloc_chosen) >= budget or (not solid and dashed >= MAX_DASHED_ASSOC):
            continue
        dashed += not solid
        coloc_chosen.append((lv, dep, solid))
    room = budget - len(coloc_chosen)
    for lv in ovals:                                   # solid first: they carry evidence
        for x in top:
            if room > 0 and any(o is lv and s for o, s in links[x]):
                chosen.append((x, lv, True))
                room -= 1
    only = {id(lv): [x for x in top if any(o is lv and not s for o, s in links[x])
                     and not any(o is lv and s for o, s in links[x])] for lv in ovals}
    for r in range(MAX_DASHED_PER_OVAL):              # round robin: every oval gets one
        for lv in ovals:
            xs = only[id(lv)]
            if r < len(xs) and room > 0 and dashed < MAX_DASHED_ASSOC:
                chosen.append((xs[r], lv, False))
                room -= 1
                dashed += 1
    keep = [x for x in top if any(c[0] == x for c in chosen)]
    fold = [x for x in ranked if x not in keep]
    left = set()
    for x in keep:
        st = sum(1 for lv, _ in links[x] if x in _starter_names(lv))
        if st and st * 2 >= len(links[x]):
            left.add(x)
    for x in keep:
        if x in left:
            actor_node(d, ref[x])
    for dep in subjects:
        sid = _subject(d, dep)
        d.node(sid, f'subgraph {sid} ["«system» {mm(dep, limit=LIM_SUBJECT - 9)}"]')
        for lv in ovals:
            if lv.deployables[0] == dep:
                u = oid(lv)
                d.node(u, f'{u}(["{_uc_label(lv)}"]):::uc')
        if dep == subjects[0] and more:
            d.node("u_more", f'u_more(["+{more} more (see table)"]):::more')
        d.raw("end")
    for x in keep:
        if x not in left:
            actor_node(d, ref[x])
    if fold:
        other_node(d, len(fold))
    n = 0
    for lv, dep, solid in coloc_chosen:
        u, s = oid(lv), _subject(d, dep)
        d.edge(u, s, f"{u} {'---' if solid else '-.-'} {s}")
        n += 1
    order = {id(lv): i for i, lv in enumerate(ovals)}
    for x, lv, solid in sorted(chosen, key=lambda c: (order[id(c[1])], top.index(c[0]))):
        a, u = d.id("a_", x), oid(lv)
        arrow = "---" if solid else "-.-"
        d.edge(a, u, f"{a} {arrow} {u}") if x in left else d.edge(u, a, f"{u} {arrow} {a}")
        n += 1
    if fold:
        s0 = _subject(d, subjects[0])
        d.edge("a_other", s0, f"a_other -.- {s0}")
        n += 1
    return n


def _starter_names(lv: LogicView) -> set[str]:
    init = lv.initiator
    return set(init.members) | {init.name}


def usecase_group(m: Model, pid: int) -> tuple[Dia | None, str, list[str]]:
    """D7: (diagram or None, caption or the no-party line, internal-mechanism bullets)."""
    mine = sorted((lv for lv in m.logics if lv.group == pid),
                  key=lambda lv: (-lv.own_support, lv.tid))
    eligible = [lv for lv in mine
                if any(a.kind in DRAWABLE for a, _ in _uc_edges(lv) if a.kind != "timer")]
    internal = [f"- {_link(f'{lv.tid} · {lv.title}', page_logic(m, lv.tid))}"
                for lv in mine if lv not in eligible]
    if not eligible:
        return None, NO_OUTSIDE, internal
    ovals, more = eligible[:MAX_UC], len(eligible) - MAX_UC
    d = Dia([FLOW_INIT, "flowchart LR"])
    for line in CLASSDEF_UC:
        d.raw(line)
    _draw_usecases(d, ovals, max(0, more), lambda lv: d.id("u_", lv.tid))
    caption = (f"Ovals = business logics of this group with an outside party (top {len(ovals)} "
               f"of {len(eligible)} by walks following the whole path). Solid line = the logic "
               "shows an exchange with that party; dashed = only mentioned (at most "
               f"{MAX_DASHED_PER_OVAL} per logic; the table below lists all parties). "
               + UC_LEGEND)
    return d, caption, internal


def participants(m: Model, lv: LogicView) -> tuple[Dia | str, str]:
    """D9: only for non-sequence flows with >= 2 drawable parties besides the initiator(s)
    and at least one evidenced party (without one it repeats the table rows above it)."""
    starters = _starter_names(lv)
    others = [a for a in lv.actors if a.name not in starters]
    if lv.flow == "sequence" or len(others) < 2:
        return "", ""
    tm = _timer_ref(lv)
    if not lv.new_schema or not (any(a.evidenced for a in lv.actors) or (tm and tm.evidenced)):
        return "", ""
    d = Dia([FLOW_INIT, "flowchart LR"])
    for line in CLASSDEF_UC:
        d.raw(line)
    pairs = [(a, a.evidenced) for a in lv.actors]
    if tm:
        pairs.insert(0, (tm, tm.evidenced))
    keep, fold = pairs[:MAX_ACTORS], pairs[MAX_ACTORS:]
    for a, _ in keep:
        if a.starts:
            actor_node(d, a)
    sid = _subject(d, lv.deployables[0])
    d.node(sid, f'subgraph {sid} ["«system» {mm(lv.deployables[0], limit=LIM_SUBJECT - 9)}"]')
    u = d.id("u_", lv.tid)
    d.node(u, f'{u}(["{_uc_label(lv)}"]):::uc')
    d.raw("end")
    for a, _ in keep:
        if not a.starts:
            actor_node(d, a)
    if fold:
        other_node(d, len(fold))
    for a, solid in keep:
        aid = d.id("a_", a.name)
        arrow = "---" if solid else "-.-"
        d.edge(aid, u, f"{aid} {arrow} {u}") if a.starts else d.edge(u, aid, f"{u} {arrow} {aid}")
    if fold:
        d.edge("a_other", sid, f"a_other -.- {sid}")
    caption = "Solid line = the logic shows an exchange with that party; dashed = only " \
              "mentioned. " + UC_LEGEND
    if lv.initiator.kind == "process":
        caption += " Started at process start."
    elif A.PROCESS.name in lv.initiator.members:
        caption += " Can also start at process start."
    return d, caption


# ---- D10 flow ----------------------------------------------------------------------------

SEQ_CAPTION = ("One representative path (confidence: {conf}); ordering inferred from call order "
               "in sampled walks, not traced. Lifelines are network entities; [i] = step i, "
               "[component] = the code component doing it. Solid arrow = message name matches a "
               "function name in the code; dashed arrow = described by the LLM only (with ? = "
               "a message name the code does not confirm). \"break\" = the logic ends there.")
ACT_CAPTION = ("Activity inferred from code (confidence: {conf}); arrows = call order in sampled "
               "walks, not a timed sequence. Lanes = the initiator, then the deployables of "
               "this repository that run the steps; [component] = code component. Labelled "
               "side branches end the logic; dashed boxes = rarer variants observed in the "
               "walks (walk counts), or optional steps.")
FAMILY_CAPTION = ("This business logic is a family of procedures; the diagram shows the shared "
                  "component path, not one procedure's message order. See Variants.")
NODE_WIDTH, NODE_LINES = 32, 3          # activity step text: about 90 characters on 3 lines
_LEAD = re.compile(r"^(when|in|if|after|once|upon|on|while|during|for|before|as)\b[^,;]{3,80},"
                   r"\s+", re.I)


def _conf(lv: LogicView) -> str:
    return lv.bl.confidence or "-"


def flow_diagram(m: Model, lv: LogicView) -> tuple[Dia, str]:
    if lv.flow == "family":
        return activity(m, lv, family=True)
    if lv.flow == "sequence" and lv.mapped:
        return sequence(m, lv)
    return activity(m, lv)


def _phase_deps(m: Model, lv: LogicView) -> list[str]:
    """the deployable of each step: where this logic's step functions mostly are (only the
    logic's own deployables count), else the cluster's deployable, always one of
    lv.deployables so that lanes and lifelines match "Runs in"."""
    sf = lv.t.step_functions or []
    out = []
    for i, c in enumerate(lv.t.clusters):
        w: Counter = Counter()
        for nid, cnt in (sf[i] if i < len(sf) else []):
            dep = A.deployable_of(nid)
            if dep in lv.deployables:
                w[dep] += cnt
        if w:
            out.append(min(w, key=lambda x: (-w[x], lv.deployables.index(x))))
        else:
            dep = m.cluster_dep.get(c)
            out.append(dep if dep in lv.deployables else lv.deployables[0])
    return out


def _sentence(s: str) -> str:
    s = clip(s)
    return s[:1].upper() + s[1:]


def _lead_cut(s: str) -> str:
    """an action without a leading "When …," / "In …," clause, when enough is left: the
    verb and the message come first in a small diagram box."""
    rest = _LEAD.sub("", clip(s), count=1)
    return _sentence(rest) if rest != clip(s) and len(rest) >= 12 else clip(s)


def _lifeline_order(lines: list[tuple[str, str, str, str]]) -> list[tuple[str, str, str, str]]:
    """the profile's adjacent lifelines side by side, the second right after the first
    (e.g. UE, (R)AN, AMF, … under the 3gpp-5gc profile)."""
    out = list(lines)
    for first, second, _ in ADJACENT_LIFELINES:
        names = [x[3] for x in out]
        if first not in names or second not in names:
            continue
        a, b = names.index(first), names.index(second)
        if a < b:
            x = out.pop(b)
            out.insert(a + 1, x)
        else:
            x = out.pop(a)
            out.insert(b, x)
    return out


def sequence(m: Model, lv: LogicView) -> tuple[Dia, str]:
    """call-flow style: entity lifelines, phases as notes, exchanges as arrows."""
    d = Dia([SEQ_INIT, "sequenceDiagram"])
    t, n = lv.t, len(lv.t.clusters)
    pdeps = _phase_deps(m, lv)
    under = list(dict.fromkeys(pdeps))[:2] or [lv.deployables[0]]
    fallback = lv.deployables[0] if lv.deployables[0] in under else under[0]
    ll_dep = {dep: d.id("n_", dep) for dep in under}
    phase_ll = [ll_dep.get(dep, ll_dep[fallback]) for dep in pdeps]

    lines: list[tuple[str, str, str, str]] = []     # (id, keyword, label, actor name)

    def add(id_: str, kw: str, label: str, name: str) -> None:
        if all(x[0] != id_ for x in lines):
            lines.append((id_, kw, label, name))

    init = lv.initiator
    names = list(init.members) if init.kind == "various" else [init.name]
    starters: list[tuple[str, str, str, str]] = []
    for name in names:
        if name == A.TIMER.name:
            starters.append(("tm", "participant", "Timer", name))
        elif name == A.EVENT.name:
            starters.append(("ev", "participant", "External signalling", name))
        else:
            r = lv.ref(name)
            if r is not None:
                kw = "actor" if r.name in HUMAN else "participant"
                starters.append((d.id("a_", r.name), kw, r.label, r.name))
    for x in starters[:2]:
        add(*x)
    for dep in under:
        add(ll_dep[dep], "participant", f"{dep} (this code)", "")
    ext: list[tuple[str, str, str, str]] = []
    inf: list[tuple[str, str, str, str]] = []
    ordered = [s for i in sorted(lv.steps_at) for s in lv.steps_at[i]]
    for s in ordered:
        p = lv.party_of(s)
        r = lv.ref(p) if p else None
        if r is None or any(x[3] == p for x in lines + ext + inf):
            continue
        if r.kind == "infra":
            inf.append((d.id("i_", r.name), "participant", r.label, r.name))
        elif s.direction in ("in", "out"):
            kw = "actor" if r.name in HUMAN else "participant"
            ext.append((d.id("a_", r.name), kw, r.label, r.name))
    for first, second, _ in ADJACENT_LIFELINES:    # keep the second under the lifeline cap
        if any(x[3] == first for x in lines + ext):
            ext.sort(key=lambda x, second=second: x[3] != second)
    room = MAX_SEQ_LIFELINES - len(lines)
    ext = ext[:max(0, room)]
    inf = inf[:max(0, room - len(ext))]
    lines = _lifeline_order(lines + ext + inf)
    for id_, kw, label, _ in lines:
        d.node(id_, f"{kw} {id_} as {mm(label, 'seq', LIM_SEQ_PART)}")
    drawn = {x[3]: x[0] for x in lines if x[3]}
    for first, second, note in ADJACENT_LIFELINES:
        a, b = drawn.get(first), drawn.get(second)
        if a and b and note:
            d.raw(f"Note over {a},{b}: {mm(note, 'seq', LIM_NOTE)}", refs=(a, b))

    def prefix(name: str) -> str:
        return MESSAGE_PREFIXES.get(name, "")

    def message(name: str, s: Step) -> tuple[str, bool]:
        """(arrow label, solid): solid only for a message name the code confirms."""
        if s.message:
            ok = verify_message(s.message, lv)
            return mm(prefix(name) + s.message, "seq", LIM_SEQ_MSG) + ("" if ok else "?"), ok
        return mm(prefix(name) + _sentence(s.action), "seq", LIM_SEQ_MSG), False

    used: set[int] = set()
    d1 = phase_ll[0] if phase_ll else ll_dep[fallback]
    starter_ll = [x for x in starters[:2] if d.has(x[0])]
    if starter_ll:
        for j, (sid, _, _, name) in enumerate(starter_ll):
            label, ok = "", False
            for s in lv.steps_at.get(1, []):
                if s.direction == "in" and lv.party_of(s) == name and id(s) not in used:
                    used.add(id(s))
                    label, ok = message(name, s)
                    break
            if not label:          # the trigger sentence is shared: only the first gets it
                label = mm(prefix(name) + lv.bl.trigger, "seq", LIM_SEQ_MSG) \
                    if j == 0 and lv.bl.trigger else "starts the logic (see trigger)"
            arrow = ("->>" if ok else "-->>") + ("+" if j == 0 else "")
            if len(starter_ll) > 1:
                guard = mm(starter_ll[j][2], "seq", LIM_COND)
                d.raw(f"alt {guard}" if j == 0 else f"else {guard}")
            d.edge(sid, d1, f"{sid}{arrow}{d1}: {label}")
        if len(starter_ll) > 1:
            d.raw("end")
    else:
        d.raw(f"activate {d1}", refs=(d1,))
        trig = lv.bl.trigger or "-"
        what = "Started at process start" if init.kind == "process" or \
            A.PROCESS.name in init.members else "Trigger"
        d.raw(f"Note over {d1}: {mm(f'{what}: {trig}', 'seq', LIM_NOTE)}", refs=(d1,))
    prev = None
    rdep = {v: k for k, v in ll_dep.items()}
    for i in range(1, n + 1):
        D = phase_ll[i - 1]
        opt = optional_phase(lv, i)
        alts = [a for a in lv.bl.alternatives if a.at == i]
        tag = mm(phase_tag(m, t.clusters[i - 1]), "seq")
        label = mm(phase_label(m, lv, i), "seq", LIM_NOTE)
        # a break inside opt would leave only the opt: an optional phase with an
        # alternative gets its note and guard at top level and opt around its exchanges
        lift = opt and bool(alts)
        mark = "optional · " if lift else ""
        notes: list[tuple[str, tuple[str, ...]]] = []
        if prev is not None and D != prev:
            notes.append((f"Note over {prev},{D}: [{i}] continues in {mm(rdep[D], 'seq')}",
                          (prev, D)))
            notes.append((f"Note over {D}: {mark}{tag} {label}".rstrip(), (D,)))
        else:
            notes.append((f"Note over {D}: [{i}] {mark}{tag} {label}".rstrip(), (D,)))
        body: list[tuple[str, str, str]] = []           # (from, to, line); to "" = a note
        for s in lv.steps_at.get(i, []):
            if id(s) in used:
                continue
            p = lv.party_of(s)
            a = drawn.get(p)
            r = lv.ref(p) if p else None
            who = r.label if r else (s.actor or "")
            if a and s.direction == "out":
                text, ok = message(p, s)
                body.append((D, a, f"{D}->>{a}: {text}" if ok else f"{D}-->>{a}: {text}"))
            elif a and s.direction == "in":
                what = s.message or s.action
                body.append((D, "", f"Note over {D}: "
                                    f"{mm(f'receives {what} from {who}', 'seq', LIM_NOTE)}"))
            elif s.actor:
                body.append((D, "", f"Note over {D}: "
                                    f"{mm(f'{s.action} (with {who})', 'seq', LIM_NOTE)}"))
        if opt and not lift:
            d.raw(f"opt optional: reached by {lv.step_walks[i - 1]} of {t.support} walks")
        for line, refs in notes:
            d.raw(line, refs=refs)
        for alt in alts:              # the guard is checked before the phase's exchanges
            d.raw(f"break {mm(alt.condition, 'seq', LIM_COND)}")
            res = alt.result or "the logic ends here"
            d.raw(f"Note over {D}: {mm(res, 'seq', LIM_NOTE)}", refs=(D,))
            d.raw("end")
        if lift and body:
            d.raw(f"opt optional: reached by {lv.step_walks[i - 1]} of {t.support} walks")
        for a, b, line in body:
            if b:
                d.edge(a, b, line)
            else:
                d.raw(line, refs=(a,))
        if opt and (body or not lift):
            d.raw("end")
        prev = D
    d.raw(f"deactivate {d1}", refs=(d1,))
    if lv.bl.outcome:
        d.raw(f"Note over {d1}: {mm('Outcome - ' + lv.bl.outcome, 'seq', LIM_NOTE)}",
              refs=(d1,))
    return d, SEQ_CAPTION.format(conf=_conf(lv))


def _common_prefix(a: list[int], b: list[int]) -> int:
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    return p


def lane_name(dep: str) -> str:
    """a deployable as a lane title, without its parenthesis ("Billing (worker)" ->
    "Billing"): the header band holds one line, and a lane is only as wide as its boxes."""
    return re.sub(r"\s*\([^)]*\)$", "", dep) or dep


def _step_node(m: Model, lv: LogicView, i: int) -> str:
    """the label of activity step i: [component] and the step's actions on up to three
    lines, or the component's packages when no step is mapped (never the name twice)."""
    c = lv.t.clusters[i - 1]
    tag = mm(phase_tag(m, c), limit=LIM_NODE)
    acts = [s.action for s in lv.steps_at.get(i, []) if s.action]
    if acts:
        text = "; ".join(_lead_cut(a) for a in acts)
        body = [mm(x) for x in wrap_lines(text, NODE_WIDTH, NODE_LINES)]
    else:
        pk = ", ".join(top_packages(m.clusters.get(c)))
        body = [mm(pk, limit=LIM_NODE)] if pk else []
    return "<br>".join([tag] + body)


def activity(m: Model, lv: LogicView, family: bool = False) -> tuple[Dia, str]:
    """UML activity with partitions: an initiator lane and one lane per deployable."""
    d = Dia([FLOW_INIT, "swimlane-beta TB" if USE_SWIMLANE_BETA else "flowchart TB"])
    for line in CLASSDEF_ACT:
        d.raw(line)
    t, n = lv.t, len(lv.t.clusters)
    init = lv.initiator
    lanes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    d.reg.reserve("l_src")
    d.reg.reserve("who")
    if init.kind == "various":
        who = ["External signalling" if x == A.EVENT.name else (lv.ref(x).label if lv.ref(x)
                                                                else x)
               for x in init.members if x != A.PROCESS.name]
    else:
        title = A.lane_title(init)
        who = [title] if title else []
    if who:      # a short title; the starters are a box in the lane, which also widens it
        lanes["l_src"] = ("Initiator", [])
    pdeps = _phase_deps(m, lv)
    dep_lane: dict[str, str] = {}
    for dep in pdeps:
        if dep not in dep_lane:
            if len(dep_lane) < MAX_DEP_LANES:
                dep_lane[dep] = d.id("l_", dep)
                lanes[dep_lane[dep]] = (lane_name(dep), [])
            else:
                dep_lane[dep] = next(iter(dep_lane.values()))
    if not dep_lane:
        dep = lv.deployables[0]
        dep_lane[dep] = d.id("l_", dep)
        lanes[dep_lane[dep]] = (lane_name(dep), [])
    first_lane = next(iter(dep_lane.values()))
    lane_of = [dep_lane[dep] for dep in pdeps]

    def put(lane: str, id_: str, line: str) -> None:
        lanes[lane][1].append((id_, line))

    if who:
        put("l_src", "who", f'who["{"<br>".join(mm(x, limit=LIM_ACTOR) for x in who[:4])}"]'
                            ":::who")
    put("l_src" if "l_src" in lanes else first_lane, "st", "st@{ shape: sm-circ }")
    for i in range(1, n + 1):
        cls = "optional" if optional_phase(lv, i) else "act"
        put(lane_of[i - 1], f"s{i}", f's{i}["{_step_node(m, lv, i)}"]:::{cls}')
    alts = [] if family else [a for a in lv.bl.alternatives if a.at and 1 <= a.at <= n]
    for j, alt in enumerate(alts, start=1):
        lane = lane_of[alt.at - 1]
        put(lane, f"r{j}", f'r{j}["{mm(alt.result or "the logic ends here", limit=LIM_NODE)}"]'
                           ":::act")
        put(lane, f"f{j}", f"f{j}@{{ shape: fr-circ }}")
    variants = sorted(t.variants or [], key=lambda v: (-v[1], list(v[0])))[:MAX_VARIANTS_DRAWN]
    vdraw = []
    for j, (seq, w) in enumerate(variants, start=1):
        seq = list(seq)
        p = _common_prefix(seq, list(t.clusters))
        suffix = seq[p:]
        if suffix:
            text = "variant: " + " → ".join(clip(cname(m, c), 18) for c in suffix)
            dep = m.cluster_dep.get(suffix[0]) or lv.deployables[0]
            lane = dep_lane.get(dep, lane_of[p - 1] if p else first_lane)
        else:
            text = f"variant: ends after step {p}"
            lane = lane_of[p - 1] if p else first_lane
        # the walk count is in the box: labels of two variants leaving the same step
        # would land on top of each other
        put(lane, f"v{j}", f'v{j}["{mm(text, limit=LIM_NODE + 20)}<br>{_n(w, "walk")}"]'
                           ":::variant")
        vdraw.append((j, p, w))
    put(lane_of[-1] if lane_of else first_lane, "en", "en@{ shape: fr-circ }")
    for lane, (title, nodes) in lanes.items():
        if not nodes:
            continue
        d.node(lane, f'subgraph {lane} ["{mm(title, limit=LIM_SUBJECT)}"]')
        for id_, line in nodes:
            d.node(id_, line)
        d.raw("end")
    if who:
        d.edge("who", "st", "who ~~~ st")
    # "Process start" only when the logic starts there; no trigger -> no label
    trig = lv.bl.trigger or ("Process start" if init.kind == "process" else "")
    arrow = f'-->|"{mm(trig, limit=LIM_NODE)}"|' if trig else "-->"
    if n:
        d.edge("st", "s1", f"st {arrow} s1")
        for i in range(1, n):
            d.edge(f"s{i}", f"s{i + 1}", f"s{i} --> s{i + 1}")
        d.edge(f"s{n}", "en", f"s{n} --> en")
    else:
        d.edge("st", "en", f"st {arrow} en")
    for j, alt in enumerate(alts, start=1):
        d.edge(f"s{alt.at}", f"r{j}",
               f's{alt.at} -->|"{mm(alt.condition, limit=LIM_COND)}"| r{j}')
        d.edge(f"r{j}", f"f{j}", f"r{j} --> f{j}")
    for j, p, w in vdraw:
        src = f"s{p}" if p else "st"
        d.edge(src, f"v{j}", f"{src} -.-> v{j}")
        d.edge(f"v{j}", "en", f"v{j} -.-> en")
    return d, FAMILY_CAPTION if family else ACT_CAPTION.format(conf=_conf(lv))


def variants_lines(m: Model, lv: LogicView, top: int = 5) -> list[str]:
    """the Variants list: the llm's variants, then the top sequences observed in the walks."""
    out = [f"- {clip(v)}" for v in lv.bl.variants]
    vs = sorted(lv.t.variants or [], key=lambda v: (-v[1], list(v[0])))
    if vs:
        shown = "; ".join(" → ".join(cname(m, c) for c in seq) + f" ({w} walks)"
                          for seq, w in vs[:top])
        more = f"; +{len(vs) - top} more" if len(vs) > top else ""
        out += ([""] if out else []) + [f"Observed in walks: {shown}{more}"]
    return out


# ---- D11 state changes -------------------------------------------------------------------

STATE_CAPTION = ("Transitions this logic performs, each tied to the function that sets the "
                 "state; not a complete state machine.")


def _evidenced_state(sc, lv: LogicView) -> bool:
    """the evidence names a function of the logic (exactly, or a prefix of at least 6
    characters), in step `at` when given, and the states are in the vocabulary."""
    ev = re.sub(r"\(.*$", "", (sc.evidence or "").strip()).rsplit(".", 1)[-1].lower()
    names = lv.fnames
    sf = lv.t.step_functions or []
    if sc.at and 1 <= sc.at <= len(sf):
        names = {fname(nid).rsplit(".", 1)[-1] for nid, _ in sf[sc.at - 1]}
    if not ev or not any(ev == f.lower() or (len(ev) >= 6 and f.lower().startswith(ev))
                         for f in names):
        return False
    key = state_entity(sc.entity)
    vocab = STATE_VOCAB.get(key)
    return not vocab or (sc.from_state in vocab and sc.to_state in vocab)


def state_logic(lv: LogicView) -> tuple[Dia | str, str]:
    """D11: evidenced, vocabulary-bound transitions; the rest as one "Mentioned" line."""
    good = [sc for sc in lv.bl.state_changes if _evidenced_state(sc, lv)]
    bad = [sc for sc in lv.bl.state_changes if sc not in good]
    line = ""
    if bad:
        line = "Mentioned state changes (no code evidence): " + "; ".join(
            f"{clip(sc.entity) or 'state'}: {clip(sc.from_state)} → {clip(sc.to_state)}"
            for sc in bad)
    if not good:
        return "", line
    d = Dia([STATE_INIT, "stateDiagram-v2"])
    d.raw("direction LR")
    ents = list(dict.fromkeys(sc.entity or "state" for sc in good))
    for e, ent in enumerate(ents):
        d.node(f"e{e}", f'state "{mm(ent, limit=LIM_STATE)}" as e{e} {{')
        states: list[str] = []
        for sc in good:
            if (sc.entity or "state") == ent:
                for s in (sc.from_state, sc.to_state):
                    if s not in states:
                        states.append(s)
        for j, s in enumerate(states):
            d.node(f"e{e}s{j}", f'state "{mm(s, limit=LIM_STATE)}" as e{e}s{j}')
        for sc in good:
            if (sc.entity or "state") == ent:
                a, b = states.index(sc.from_state), states.index(sc.to_state)
                ev = clip(sc.evidence, 40)
                lab = f"step {sc.at} ({ev})" if sc.at else ev
                d.edge(f"e{e}s{a}", f"e{e}s{b}", f"e{e}s{a} --> e{e}s{b}: {mm(lab, 'colon')}")
        d.raw("}")
    return d, line


# ---- related logics ----------------------------------------------------------------------

def _contains(a: list[int], b: list[int]) -> bool:
    """b is a contiguous sub-sequence of a."""
    return len(b) >= 2 and any(a[i:i + len(b)] == b for i in range(len(a) - len(b) + 1))


def related(m: Model, lv: LogicView, top: int = 5) -> list[tuple[LogicView, str]]:
    mine = set(lv.bl.implements) | set(lv.bl.supports)
    out = []
    for o in m.logics:
        if o is lv:
            continue
        shared = sorted(mine & (set(o.bl.implements) | set(o.bl.supports)))
        a, b = list(lv.t.clusters), list(o.t.clusters)
        cont = _contains(a, b) or _contains(b, a)
        if not shared and not cont:
            continue
        reasons = [f'shares procedure "{p}"' for p in shared]
        if cont:
            reasons.append("its component path is part of this one" if _contains(a, b)
                           else "this component path is part of its own")
        out.append((len(shared), cont, o.own_support, o, "; ".join(reasons)))
    out.sort(key=lambda x: (-x[0], not x[1], -x[2], x[3].tid))
    return [(o, r) for _, _, _, o, r in out[:top]]


# ---- compressed evidence -----------------------------------------------------------------

def mid_clip(text: str, limit: int) -> str:
    """`text` cut in the middle to `limit` characters, keeping its start and its end."""
    t = clip(text)
    if len(t) <= limit:
        return t
    tail = (limit - 1) // 3
    return t[:limit - 1 - tail] + "…" + t[-tail:]


def where_in_code(t: Trajectory, tid: str | None = None) -> list[str]:
    """the three "Where in the code" bullets (heading not included)."""
    tid = tid or t.id
    out: list[str] = []
    pk: Counter = Counter()
    raw: Counter = Counter()
    for step in t.step_functions or []:
        for nid, cnt in step:
            raw[pkg(nid)] += cnt
            if not is_noise(nid):
                pk[pkg(nid)] += cnt
    top = (pk or raw).most_common(4)
    if top:
        out.append("- **Main packages:** " + ", ".join(f"`{clip(p, 60)}`" for p, _ in top))
    roots = list(t.roots or [])
    r = len(roots)
    if roots:
        sub = Counter(subservice(x) for x in roots)
        subs = sorted(sub, key=lambda s: (-sub[s], s))
        parts = ", ".join(f"`{clip(s, 60)}` {sub[s]}" for s in subs[:3])
        if len(subs) > 3:
            parts += f", +{len(subs) - 3} more"
        cands = [n for n, _ in t.root_counts] if t.root_counts else roots
        ex: list[str] = []
        for n in [n for n in cands if not is_noise(n)] or cands:
            q = mid_clip(qname(n), 60)
            if q not in ex:           # variants differ in their tail (…Source / …Target)
                ex.append(q)
            if len(ex) == 3:
                break
        eg = "; e.g. " + ", ".join(f"`{q}`" for q in ex) if ex else ""
        out.append(f"- **Entry points:** {r} roots — {parts}{eg}")
    if t.own_support:
        based = (f"- Based on {t.own_support} walks following the whole path ({t.support} "
                 f"including partial walks and variants) from {r} entry roots.")
    else:
        based = f"- Based on {t.support} walks from {r} entry roots."
    based += f" Full evidence: `business_logic.json`, entry `{tid}`."
    if t.subsumed:
        based += f" Also stands for {t.subsumed} shorter trajectories."
    out.append(based)
    return out
