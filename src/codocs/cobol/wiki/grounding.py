"""post-generation grounding of LLM-written mermaid diagrams against the static index.

The wiki prompts hand the LLM verified skeletons, but a one-shot page still invents
edges (a batch job "writing" a VSAM master it never opens, a program LINKing straight to
a program three hops down its chain). This module maps every diagram node back to the
things the index knows — wiki pages, programs, data stores, JCL jobs, CICS transactions
and the generic entry points (terminal user, CICS, JCL schedule) — and then:

* flowcharts: drops each edge whose endpoints both resolve but which no index fact
  supports; nodes stay declared, so the diagram remains valid;
* sequence diagrams: keeps a program -> program / program -> store message only when it is
  a direct call / LINK / XCTL or a real read / write; a message the index supports only
  transitively (through a callee chain) is relabeled "(via X)"; anything else is dropped.

Endpoints that resolve to nothing (an "MQ request" box, a "Customer" actor) are kept:
the validator only removes what it can disprove.
"""

from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import dataclass, field

import networkx as nx

from codocs.cobol.index import CobolRepoIndex

# chain depth for transitive support / call-chain facts
CHAIN_DEPTH = 4

Entity = tuple[str, str]  # (kind, name): prog | page | store | job | txn | entry

_TOKEN_RE = re.compile(r"[A-Za-z0-9$#@<][A-Za-z0-9$#@<>_\-.]*")
_ID_PREFIX_KIND = (
    (("ds_", "db_", "q_", "tsq_", "tdq_", "file_", "tbl_", "table_", "vsam_", "data_", "store_"),
     "store"),
    (("txn_", "tx_", "trn_", "trans_", "tran_"), "txn"),
    (("job_", "jcl_"), "job"),
    (("prog_", "pgm_", "p_"), "prog"),
    (("page_", "sub_", "grp_"), "page"),
)
# DSN qualifiers that say nothing about which file is meant
_LABEL_KIND = (
    ("store", re.compile(r"\b(vsam|ksds|esds|rrds|db2|table|file|dataset|queue|tsq|tdq|gdg)\b",
                         re.IGNORECASE)),
    ("job", re.compile(r"\b(job|jcl)\b", re.IGNORECASE)),
    ("txn", re.compile(r"\b(transaction|transid|txn)\b", re.IGNORECASE)),
    ("prog", re.compile(r"\b(program|pgm)\b", re.IGNORECASE)),
)
_GENERIC_QUALIFIERS = {
    "VSAM", "KSDS", "ESDS", "RRDS", "AIX", "PATH", "DATA", "INDEX", "PS", "SEQ", "FILE",
    "DB2", "TABLE", "QUEUE", "TS", "TD", "PROD", "TEST", "DEV", "BACKUP", "BKUP", "GDG",
    "<USRHLQ>", "SYS1", "LOADLIB",
}
_WRITE_HINT = re.compile(
    r"\b(writ|updat|insert|delet|rewrit|creat|output|post|put|stor|add|enqueue|log)",
    re.IGNORECASE,
)
_READ_HINT = re.compile(r"\b(read|fetch|brows|select|input|get|inquir|query|look|retriev|scan)",
                        re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _mid(name: str) -> str:
    """same node-id mangling as the generator's skeletons."""
    mid = re.sub(r"[^a-zA-Z0-9_]", "_", name) or "node"
    if mid[0].isdigit():
        mid = "n" + mid
    return mid + "_" if mid.lower() in {"end", "graph", "subgraph", "style", "class"} else mid


def edge_mode(label: str) -> str:
    """'w' / 'r' / '' (any) from an edge label or message text."""
    label = label or ""
    if re.search(r"read by|read from|returns?|reply|response", label, re.IGNORECASE):
        return "r"
    w, r = bool(_WRITE_HINT.search(label)), bool(_READ_HINT.search(label))
    if w and not r:
        return "w"
    if r and not w:
        return "r"
    return ""


@dataclass
class Grounding:
    """index facts in the shape the diagram validator needs."""

    index: CobolRepoIndex
    page_programs: dict[str, list[str]] = field(default_factory=dict)
    page_titles: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        idx = self.index
        self.pg: nx.DiGraph = idx.program_graph()
        self.undirected = self.pg.to_undirected(as_view=True)
        self.txn_progs: dict[str, set[str]] = {}
        for t, p in idx.transactions.items():
            if p in idx.programs:
                self.txn_progs.setdefault(t.upper(), set()).add(p)
        self.job_progs: dict[str, set[str]] = {name.upper(): set() for name in idx.jobs}
        for pid in idx.programs:
            for j in idx.jobs_running(pid):
                self.job_progs.setdefault(j.upper(), set()).add(pid)
        self.readers: dict[str, set[str]] = {}
        self.writers: dict[str, set[str]] = {}
        self.store_jobs: dict[str, set[str]] = {}
        for name, ds in idx.datastores.items():
            self.readers[name] = set(ds.readers)
            self.writers[name] = set(ds.writers)
            self.store_jobs[name] = {j.upper() for j in ds.jobs}
        self._reach_cache: dict[str, dict[str, list[str]]] = {}
        self._build_lookup()

    # -- name lookup -------------------------------------------------------

    def _build_lookup(self) -> None:
        idx = self.index
        lk: dict[str, set[Entity]] = {}
        mids: dict[str, set[Entity]] = {}

        def add(key: str, ent: Entity, mid_too: bool = True) -> None:
            if key:
                lk.setdefault(key.upper(), set()).add(ent)
                if mid_too:
                    mids.setdefault(_mid(key).upper(), set()).add(ent)

        for pid, prog in idx.programs.items():
            add(pid, ("prog", pid))
            for e in prog.entry_points:
                add(e, ("prog", pid))
        for t in self.txn_progs:
            add(t, ("txn", t))
        for j in self.job_progs:
            add(j, ("job", j))
        quals: dict[str, set[str]] = {}
        for name, ds in idx.datastores.items():
            add(name, ("store", name))
            mids.setdefault(_mid("ds_" + name).upper(), set()).add(("store", name))
            for a in ds.aliases:
                add(a, ("store", name))
            for q in name.upper().split("."):
                if q and q not in _GENERIC_QUALIFIERS and not q.startswith("&"):
                    quals.setdefault(q, set()).add(name)
        for q, names in quals.items():
            if len(names) == 1 and len(q) >= 4 and q not in lk:
                for n in names:
                    lk.setdefault(q, set()).add(("store", n))
        for page_id in self.page_titles:
            mids.setdefault(_mid(page_id).upper(), set()).add(("page", page_id))
            lk.setdefault(page_id.upper(), set()).add(("page", page_id))
        self._lookup = lk
        self._mids = mids
        self._title_norm = {_norm(t): p for p, t in self.page_titles.items() if _norm(t)}

    def _store_suffix(self, token: str) -> set[Entity]:
        """stores whose DSN ends with a dotted label token (CARDDEMO.ACCTDATA.VSAM.KSDS)."""
        t = token.upper().strip(".")
        if "." not in t:
            return set()
        return {("store", n) for n in self.index.datastores
                if n.upper() == t or n.upper().endswith("." + t)}

    def resolve(self, node_id: str, label: str = "", shape: str = "") -> set[Entity]:
        """entities a diagram node stands for (empty: unknown).

        The kind is narrowed by the id prefix (ds_, txn_, job_ ...), a cylinder shape, or
        a kind word in the label ("JOB TRANREPT", "TRANSACT VSAM KSDS"), so a job and a
        dataset sharing a name are told apart."""
        ents: set[Entity] = set()
        hint = ""
        nid = node_id.strip()
        low = nid.lower()
        bare = nid
        for prefixes, kind in _ID_PREFIX_KIND:
            for p in prefixes:
                if low.startswith(p) and len(nid) > len(p):
                    hint, bare = kind, nid[len(p):]
                    break
            if hint:
                break
        for key in (nid, bare):
            ents |= self._mids.get(key.upper(), set())
            ents |= self._lookup.get(key.upper(), set())
            ents |= self._lookup.get(key.upper().replace("_", "-"), set())
        text = label or ""
        if text:
            page = self._title_norm.get(_norm(text))
            if page is None and len(_norm(text)) >= 8:
                best = difflib.get_close_matches(_norm(text), list(self._title_norm), n=1,
                                                 cutoff=0.86)
                page = self._title_norm[best[0]] if best else None
            if page:
                ents.add(("page", page))
            for tok in _TOKEN_RE.findall(text):
                tok = tok.strip(".-_")
                # identifiers are written in capitals; prose words are not entities
                if not tok or tok != tok.upper() or not re.search(r"[A-Z]", tok):
                    continue
                found = self._lookup.get(tok, set()) | self._store_suffix(tok)
                if not found and "." in tok:
                    # a shortened DSN (USRHLQ.GENAPP.KSDSCUST): its distinctive qualifiers
                    for q in tok.split("."):
                        found |= {e for e in self._lookup.get(q, set()) if e[0] == "store"}
                ents |= found
        if not hint and shape.startswith("[("):
            hint = "store"
        if not hint:
            words = {k for k, rx in _LABEL_KIND if rx.search(text)}
            hint = words.pop() if len(words) == 1 else ""
        if hint and hint != "page":
            narrowed = {e for e in ents if e[0] == hint}
            ents = narrowed or ents
        if not ents:
            ents = self._entry_kind(nid, text)
        return ents

    @staticmethod
    def _entry_kind(node_id: str, label: str) -> set[Entity]:
        text = f"{node_id} {label}".lower().replace("_", " ")
        if re.search(r"\b(jcl|batch schedul|scheduler|job schedul|jcl jobs?|batch jobs?)\b", text):
            return {("entry", "jcl")}
        if re.search(r"\bcics\b.*\btrans|\btransactions?\b.*\bcics\b|entry cics", text):
            return {("entry", "cics")}
        if re.search(r"\b(terminal|3270|end user|user|users|operator|clerk)\b", text):
            return {("entry", "user")}
        return set()

    # -- program sets ------------------------------------------------------

    def programs_of(self, ent: Entity) -> set[str]:
        kind, name = ent
        if kind == "prog":
            return {name}
        if kind == "page":
            return set(self.page_programs.get(name, []))
        if kind == "txn":
            return set(self.txn_progs.get(name, set()))
        if kind == "job":
            return set(self.job_progs.get(name, set()))
        if kind == "entry" and name in ("cics", "user"):
            # the programs a transaction starts; without transaction definitions in the
            # repo, any CICS program nobody else transfers to
            started = {p for ps in self.txn_progs.values() for p in ps}
            return started or set(self.index.entrypoints()["cics"])
        if kind == "entry" and name == "jcl":
            return {p for ps in self.job_progs.values() for p in ps}
        return set()

    def reach(self, pid: str, depth: int = CHAIN_DEPTH) -> dict[str, list[str]]:
        """programs reachable from *pid* within *depth* transfers -> shortest path."""
        key = f"{pid}:{depth}"
        if key in self._reach_cache:
            return self._reach_cache[key]
        paths: dict[str, list[str]] = {pid: [pid]}
        todo = deque([pid])
        while todo:
            cur = todo.popleft()
            if len(paths[cur]) > depth or cur not in self.pg:
                continue
            for nxt in sorted(self.pg.successors(cur)):
                if nxt not in paths:
                    paths[nxt] = paths[cur] + [nxt]
                    todo.append(nxt)
        self._reach_cache[key] = paths
        return paths

    def _accessors(self, store: str, mode: str) -> set[str]:
        if mode == "w":
            return self.writers.get(store, set())
        if mode == "r":
            return self.readers.get(store, set())
        return self.readers.get(store, set()) | self.writers.get(store, set())

    # -- support -------------------------------------------------------------

    def supports(self, a: Entity, b: Entity, mode: str = "") -> bool:
        """is an edge between *a* and *b* backed by an index fact (direction-agnostic)?"""
        if a[0] == "store" and b[0] == "store":
            return a == b or bool(self.store_jobs.get(a[1], set()) & self.store_jobs.get(b[1], set()))
        if b[0] == "store":
            a, b = b, a
        if a[0] == "store":
            return self._store_support(a[1], b, mode)
        if a[0] == "entry" and b[0] == "entry":
            return True
        pa, pb = self.programs_of(a), self.programs_of(b)
        if not pa or not pb:
            # the repo's JCL and transaction definitions are known: "JCL jobs -> X" with no
            # job running a repo program is false, as is a job of utility steps "running" X;
            # only a generic user box with nothing behind it cannot be judged
            return any(e == ("entry", "user") and not self.programs_of(e) for e in (a, b))
        if pa & pb:
            return True
        if a[0] == "entry" or b[0] == "entry":
            # "CICS transactions -> subsystem" means a transaction starts one of its programs
            return False
        return any(self.undirected.has_edge(p, q) for p in pa for q in pb if p in self.pg
                   and q in self.pg)

    def _store_support(self, store: str, other: Entity, mode: str) -> bool:
        kind, name = other
        if kind == "entry":
            if name == "jcl" and any(self.store_jobs.get(store, set())):
                return True
        if kind == "job" and name in self.store_jobs.get(store, set()):
            progs = self.job_progs.get(name, set())
            touching = progs & self._accessors(store, "")
            # allocated by the job: trust the job's programs when they touch the store,
            # otherwise a utility step (IDCAMS / SORT) uses it in an unknown mode
            return not touching or bool(touching & self._accessors(store, mode))
        progs = self.programs_of(other)
        acc = self._accessors(store, mode)
        if progs & acc:
            return True
        if kind in ("txn", "job", "entry"):
            return any(set(self.reach(p)) & acc for p in progs)
        return False

    def node_supports(self, ea: set[Entity], eb: set[Entity], mode: str = "") -> bool | None:
        """None when either side is unknown (nothing to judge)."""
        if not ea or not eb:
            return None
        return any(self.supports(a, b, mode) for a in ea for b in eb)


# -- flowcharts ---------------------------------------------------------------

_FLOW_SKIP = ("subgraph", "end", "direction", "classdef", "class", "style", "linkstyle",
              "click", "%%", "flowchart", "graph")
_ID_RE = re.compile(r"[A-Za-z0-9_]+(?:-(?![-.>=])[A-Za-z0-9_]+)*")
_TEXT_LINK_RE = re.compile(
    r"\s*(?P<link>(?P<open><?(?:--|==|-\.))\s+(?P<text>[^|>]+?)\s+(?:-{2,}>|={2,}>|\.->|-{3,}|={3,}|\.-|--[ox]))\s*"
)
_LINK_RE = re.compile(
    r"\s*(?P<link>(?:<|o(?=-)|x(?=-))?(?:-{2,}|={2,}|-\.+-?|~{3,})(?:>|o\b|x\b)?)"
    r"(?:\|(?P<label>[^|]*)\|)?\s*"
)
_OPEN = {"[": "]", "(": ")", "{": "}", ">": "]"}


@dataclass
class _NodeRef:
    id: str
    text: str  # as written, incl. shape
    label: str = ""


@dataclass
class _Segment:
    left: list[_NodeRef]
    link: str
    label: str
    right: list[_NodeRef]


def _scan_shape(line: str, i: int) -> int:
    """end index (exclusive) of a node shape starting at line[i] in '[({>'; -1 if broken."""
    stack: list[str] = []
    in_q = False
    j = i
    while j < len(line):
        c = line[j]
        if in_q:
            if c == '"':
                in_q = False
        elif c == '"':
            in_q = True
        elif j == i and c == ">":
            stack.append("]")
        elif c in "[({":
            stack.append(_OPEN[c])
        elif c in "])}":
            if not stack or stack[-1] != c:
                return -1
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return -1


def _shape_label(shape: str) -> str:
    inner = shape.strip("[](){}>/\\ ")
    return inner.strip('"').strip()


def _parse_node(line: str, i: int) -> tuple[_NodeRef, int] | None:
    m = _ID_RE.match(line, i)
    if not m:
        return None
    j = m.end()
    label = ""
    if j < len(line) and line[j] in "[({>":
        end = _scan_shape(line, j)
        if end < 0:
            return None
        label = _shape_label(line[j:end])
        j = end
    cls = re.match(r":::[\w-]+", line[j:])
    if cls:
        j += cls.end()
    return _NodeRef(m.group(0), line[i:j], label), j


def _parse_group(line: str, i: int) -> tuple[list[_NodeRef], int] | None:
    nodes: list[_NodeRef] = []
    while True:
        while i < len(line) and line[i] == " ":
            i += 1
        got = _parse_node(line, i)
        if got is None:
            return None
        node, i = got
        nodes.append(node)
        m = re.match(r"\s*&\s*", line[i:])
        if not m:
            return nodes, i
        i += m.end()


def parse_flow_statement(line: str) -> tuple[list[list[_NodeRef]], list[tuple[str, str]]] | None:
    """(node groups, [(link, label)]) for a node / edge statement; None if not parseable."""
    s = line.strip().rstrip(";")
    got = _parse_group(s, 0)
    if got is None:
        return None
    groups, links = [got[0]], []
    i = got[1]
    while i < len(s):
        m = _TEXT_LINK_RE.match(s, i)
        label = ""
        if m and m.group("text").strip():
            link, label = m.group("link"), m.group("text").strip()
        else:
            m = _LINK_RE.match(s, i)
            if not m or not m.group("link").strip():
                return None
            link = m.group("link") + (f"|{m.group('label')}|" if m.group("label") is not None
                                      else "")
            label = (m.group("label") or "").strip().strip('"')
        i = m.end()
        got = _parse_group(s, i)
        if got is None:
            return None
        groups.append(got[0])
        links.append((link.strip(), label))
        i = got[1]
        while i < len(s) and s[i] == " ":
            i += 1
    return groups, links


def _flow_structure(
    lines: list[str],
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, set[str]]]]:
    """node (label, shape) and subgraph (label, member ids, incl. nested) of a flowchart."""
    labels: dict[str, tuple[str, str]] = {}
    subgraphs: dict[str, tuple[str, set[str]]] = {}
    stack: list[str] = []
    seen: set[str] = set()
    for ln in lines[1:]:
        s = ln.strip()
        word = s.split(" ", 1)[0].lower() if s else ""
        if word == "subgraph":
            rest = s[len("subgraph"):].strip()
            m = re.match(r"([A-Za-z0-9_\-]+)\s*(\[.*\])?\s*$", rest)
            if m:
                sid, lab = m.group(1), _shape_label(m.group(2) or "") or m.group(1)
            else:
                sid = lab = rest.strip('"')
            subgraphs[sid] = (lab, set())
            for outer in stack:
                subgraphs[outer][1].add(sid)
            stack.append(sid)
            continue
        if word == "end":
            if stack:
                stack.pop()
            continue
        if not s or word in _FLOW_SKIP:
            continue
        parsed = parse_flow_statement(s)
        if parsed is None:
            continue
        for group in parsed[0]:
            for n in group:
                if n.label and n.id not in labels:
                    labels[n.id] = (n.label, n.text[len(n.id):])
                if n.id not in seen:
                    seen.add(n.id)
                    for sg in stack:
                        subgraphs[sg][1].add(n.id)
    return labels, subgraphs


def validate_flowchart(code: str, g: Grounding) -> tuple[str, list[str]]:
    """drop edges the index disproves; returns (new code, human-readable drop notes)."""
    lines = code.splitlines()
    if not lines:
        return code, []
    labels, subgraphs = _flow_structure(lines)
    cache: dict[str, set[Entity]] = {}

    def ents(nid: str) -> set[Entity]:
        if nid in cache:
            return cache[nid]
        cache[nid] = set()  # cycle guard for nested subgraphs
        if nid in subgraphs:
            lab, members = subgraphs[nid]
            out = g.resolve(nid, lab) - {("entry", "user"), ("entry", "cics"), ("entry", "jcl")}
            for m in members:
                out |= ents(m)
        else:
            out = g.resolve(nid, *labels.get(nid, ("", "")))
        cache[nid] = out
        return out

    out: list[str] = []
    dropped: list[str] = []
    for ln in lines:
        s = ln.strip()
        word = s.split(" ", 1)[0].lower() if s else ""
        if not s or word in _FLOW_SKIP:
            out.append(ln)
            continue
        parsed = parse_flow_statement(s)
        if parsed is None or not parsed[1]:
            out.append(ln)
            continue
        groups, links = parsed
        indent = ln[: len(ln) - len(ln.lstrip())]
        kept: list[str] = []
        changed = False
        for k, (link, label) in enumerate(links):
            left, right = groups[k], groups[k + 1]
            mode = edge_mode(label)
            pairs = [(a, b) for a in left for b in right]
            ok = [(a, b) for a, b in pairs if g.node_supports(ents(a.id), ents(b.id), mode)
                  is not False]
            if len(ok) == len(pairs):
                kept.append(f"{' & '.join(a.text for a in left)} {link} "
                            f"{' & '.join(b.text for b in right)}")
                continue
            changed = True
            for a, b in pairs:
                if (a, b) in ok:
                    kept.append(f"{a.text} {link} {b.text}")
                else:
                    dropped.append(f"{a.id} {link} {b.id}")
        if not changed:
            out.append(ln)
            continue
        out.extend(indent + k for k in kept)
        # keep the shapes / labels of nodes first declared in a dropped edge
        written = " ".join(kept)
        for group in groups:
            for n in group:
                if n.text != n.id and n.text not in written:
                    out.append(indent + n.text)
                    written += " " + n.text
    if dropped and any(x.strip().lower().startswith("linkstyle") for x in out):
        out = [x for x in out if not x.strip().lower().startswith("linkstyle")]
    return "\n".join(out), dropped


# -- sequence diagrams ----------------------------------------------------------

_PART_RE = re.compile(r"^\s*(participant|actor)\s+(\"[^\"]+\"|\S+)(?:\s+as\s+(.+?))?\s*$",
                      re.IGNORECASE)
_MSG_RE = re.compile(
    r"^(?P<indent>\s*)(?P<a>[^\s:>+-][^:>]*?)\s*(?P<arrow><<-->>|<<->>|-->>|->>|-->|->|--x|-x|--\)|-\))"
    r"(?P<act>[+-]?)\s*(?P<b>[^:+-][^:]*?)\s*:\s*(?P<text>.*)$"
)


def validate_sequence(code: str, g: Grounding) -> tuple[str, list[str]]:
    """drop / relabel program and store messages the index does not support."""
    lines = code.splitlines()
    labels: dict[str, str] = {}
    for ln in lines:
        m = _PART_RE.match(ln)
        if m:
            pid = m.group(2).strip('"')
            labels[pid] = (m.group(3) or pid).strip()
    resolved: dict[str, set[Entity]] = {}

    def ents(pid: str) -> set[Entity]:
        if pid not in resolved:
            resolved[pid] = g.resolve(pid, labels.get(pid, ""))
        return resolved[pid]

    out: list[str] = []
    notes: list[str] = []
    dropped_any = False
    for ln in lines:
        m = _MSG_RE.match(ln)
        if not m:
            out.append(ln)
            continue
        a, b, text = m.group("a").strip('"'), m.group("b").strip('"'), m.group("text")
        verdict, via = _message_support(ents(a), ents(b), text, g)
        if verdict == "drop":
            notes.append(f"dropped message {a} {m.group('arrow')} {b}: {text.strip()[:60]}")
            dropped_any = True
            continue
        if verdict == "via" and "via" not in text.lower():
            new_text = f"{text.rstrip()} (via {' -> '.join(via)})"
            notes.append(f"relabeled {a} -> {b}: via {' -> '.join(via)}")
            ln = f"{m.group('indent')}{m.group('a')}{m.group('arrow')}{m.group('act')}" \
                 f"{m.group('b')}: {new_text}"
        out.append(ln)
    if dropped_any:
        # activation markers of a dropped message would leave the rest unbalanced
        out = [_strip_activation(x) for x in out
               if not re.match(r"^\s*(activate|deactivate)\b", x, re.IGNORECASE)]
    return "\n".join(out), notes


def _strip_activation(line: str) -> str:
    m = _MSG_RE.match(line)
    if not m or not m.group("act"):
        return line
    return (f"{m.group('indent')}{m.group('a')}{m.group('arrow')}{m.group('b')}: "
            f"{m.group('text')}")


_RANK = {"keep": 0, "via": 1, "drop": 2}


def _message_support(
    ea: set[Entity], eb: set[Entity], text: str, g: Grounding
) -> tuple[str, list[str]]:
    """('keep' | 'via' | 'drop', intermediate programs).

    A name that is both a store and a job / program (CardDemo's TCATBALF is a dataset and
    the job that defines it) is judged under each reading; the best verdict wins."""
    if not ea or not eb:
        return "keep", []

    def split(es: set[Entity]) -> list[set[Entity]]:
        parts = [{e for e in es if e[0] == "store"}, {e for e in es if e[0] != "store"}]
        return [p for p in parts if p]

    best: tuple[str, list[str]] = ("drop", [])
    for a in split(ea):
        for b in split(eb):
            got = _message_support_one(a, b, text, g)
            if _RANK[got[0]] < _RANK[best[0]]:
                best = got
    return best


def _message_support_one(
    ea: set[Entity], eb: set[Entity], text: str, g: Grounding
) -> tuple[str, list[str]]:
    stores_a = {e for e in ea if e[0] == "store"}
    stores_b = {e for e in eb if e[0] == "store"}
    if stores_a and stores_b:
        return ("keep" if g.node_supports(ea, eb) else "drop"), []
    if stores_a or stores_b:
        stores = stores_a or stores_b
        other = eb if stores_a else ea
        # a message from the store back to a program answers a read or a write
        mode = "" if stores_a else edge_mode(text)
        progs_like = {o for o in other if o[0] in ("prog", "page")}
        if not progs_like:
            # transaction / job / entry -> store: supported through its program chain
            return ("keep" if any(g.supports(s, o, mode) for s in stores for o in other)
                    else "drop"), []
        if any(g.supports(s, o, mode) for s in stores for o in progs_like):
            return "keep", []
        other = progs_like
        # the store is touched further down this program's chain: name who does it
        acc = set().union(*(g._accessors(s[1], mode) for s in stores))
        for p in sorted(p for o in other for p in g.programs_of(o)):
            paths = g.reach(p)
            hits = sorted((len(paths[q]), q) for q in acc if q in paths and q != p)
            if hits:
                return "via", paths[hits[0][1]][1:]
        return "drop", []
    if not any(e[0] in ("prog", "page") for e in ea | eb):
        return ("keep" if g.node_supports(ea, eb) is not False else "drop"), []
    if g.node_supports(ea, eb):
        return "keep", []
    # transitive: a -> ... -> b down a call chain (either direction for replies)
    for src, dst in ((ea, eb), (eb, ea)):
        for p in sorted(x for e in src for x in g.programs_of(e)):
            paths = g.reach(p)
            for q in sorted(x for e in dst for x in g.programs_of(e)):
                if q in paths and q != p:
                    return "via", paths[q][1:-1]
    return "drop", []


# -- pages ----------------------------------------------------------------------

_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def ground_markdown(markdown: str, g: Grounding) -> tuple[str, list[str]]:
    """validate every flowchart / sequenceDiagram block of a page against the index."""
    notes: list[str] = []

    def fix(m: re.Match) -> str:
        code = m.group(1).strip()
        head = next((x.strip() for x in code.splitlines() if x.strip()), "")
        if head.split(" ", 1)[0] in ("flowchart", "graph"):
            code, n = validate_flowchart(code, g)
            notes.extend(f"dropped edge {x}" for x in n)
        elif head.startswith("sequenceDiagram"):
            code, n = validate_sequence(code, g)
            notes.extend(n)
        return f"```mermaid\n{code}\n```"

    return _BLOCK_RE.sub(fix, markdown), notes


# -- call chains (system facts) -----------------------------------------------


def chain_tree(
    pg: nx.DiGraph, root: str, depth: int = CHAIN_DEPTH, skip: set[str] | None = None,
    stop: set[str] | None = None,
) -> list[list[str]]:
    """root-to-leaf paths of the BFS tree from *root* (each program once, shortest path).

    *skip* programs (hubs) are left out entirely; *stop* programs (other entry points)
    appear as a leaf without being expanded, so a shared menu is described once.
    """
    skip = skip or set()
    stop = stop or set()
    parent: dict[str, str | None] = {root: None}
    level = {root: 0}
    order = [root]
    todo = deque([root])
    while todo:
        cur = todo.popleft()
        if level[cur] >= depth or (cur in stop and cur != root) or cur not in pg:
            continue
        for nxt in sorted(pg.successors(cur)):
            if nxt in parent or nxt in skip:
                continue
            parent[nxt] = cur
            level[nxt] = level[cur] + 1
            order.append(nxt)
            todo.append(nxt)
    children = {p for p in parent.values() if p}
    paths = []
    for leaf in order:
        if leaf in children:
            continue
        path = [leaf]
        while parent[path[-1]] is not None:
            path.append(parent[path[-1]])
        paths.append(path[::-1])
    return paths
