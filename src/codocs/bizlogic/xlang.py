"""multi-language bizlogic graph: per-language parts and cross-language linkers.

Python, Java, C and C++ functions come from the adapters' `graph_facts` (tree-sitter) and
are resolved here; Go keeps its own part (`graph._go_part`), whose nodes and edges are
those of the Go-only graph. node ids are "<unit dir>::<name>":

  python  `<module>.<f>`, `<module>.<Cls>.<m>` (`__init__` modules drop the prefix)
  java    `<Cls>.<m>`, nested `<Outer>.<Inner>.<m>`, constructors `<Cls>.<Cls>`
  c/c++   `<f>`, static `<f>@<file>`, methods `<Cls>.<m>`

a non-Go id that is already taken (Go ids never change; then LANG_ORDER, then file rel)
is renamed `<name>@<basename>`. cross-language edges come from LINKERS: `CgoLinker` links
Go `C.f(...)` calls/refs to C definitions (preamble, same dir, prototypes in included
headers, nearest in the repo) and C calls of `//export`ed Go functions back to Go;
`CobolCLinker` links unresolved COBOL `CALL 'name'` literals to C functions.

COBOL comes from the run's COBOL index (`codocs.cobol.graphfacts`):

  cobol   `<PID>` program entry, `<PID>.<PARA>` paragraphs and sections, `JOB.<J>`,
          `JOB.<J>.<STEP>` JCL jobs and steps, `TXN.<T>` CICS transactions
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import networkx as nx

from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.graph import (
    _TEST_FUNC_RE,
    MAX_METHOD_TARGETS,
    FuncGraph,
    FuncNode,
    _add,
    _finish,
    _go_part,
    _GoPart,
    _module_name,
    _vendored,
    short_name,
)
from codocs.cobol.facts import PLUMBING_PARAGRAPH
from codocs.cobol.graphfacts import ALL_LANGS
from codocs.lang.base import (
    CallFact,
    FuncFact,
    GraphFileFacts,
    GraphPart,
    RepoContext,
    StaticContext,
)

log = logging.getLogger(__name__)

TAGS = {"c": "C", "cpp": "C++", "java": "Java", "python": "Python", "cobol": "COBOL"}
# per-language plumbing on top of DEFAULT_PLUMBING (logging, dunders, object protocol)
EXTRA_PLUMBING = {
    "python": re.compile(r"^_?(?:log|logger|debug|trace|warn|warning|info|error)(?:_\w*)?$|"
                         r"^__(?!init__$|call__$)\w+__$"),
    "java": re.compile(r"^(?:toString|hashCode|equals|compareTo|log|debug|trace|info|warn|"
                       r"error)$"),
    "c": re.compile(r"(?i)(?:^|_)(?:log|dbg|debug|trace|dump)(?:_|$)|^(?:un)?lock(?:_|$)|"
                    r"_(?:un)?lock$"),
    "cpp": re.compile(r"(?i)(?:^|_)(?:log|dbg|debug|trace|dump)(?:_|$)|^(?:un)?lock(?:_|$)|"
                      r"_(?:un)?lock$|^operator\W"),
    # error exits, abend and I/O-status displays, logging paragraphs (also the mutation
    # sampler's plumbing rule)
    "cobol": PLUMBING_PARAGRAPH,
}
# tiny getters/setters named the Java/Python/C++ way (Go's Get/Set/Is/Has also apply)
EXTRA_ACCESSORS = re.compile(r"^_?(?:get|set|is|has)(?:[A-Z_]|$)")
# names after `C.` that are never C functions: cgo types, builtins and helpers
CGO_SKIP = frozenset(
    "char schar uchar short ushort int uint long ulong longlong ulonglong float double "
    "size_t complexfloat complexdouble CString GoString GoStringN GoBytes CBytes malloc "
    "free".split())
_CGO_SKIP_PREFIX = ("struct_", "union_", "enum_")
_C_SEL_RE = re.compile(r"(?<![\w.])C\s*\.\s*([A-Za-z_]\w*)(\s*\()?")
_EXPORT_RE = re.compile(r"^//export\s+([A-Za-z_]\w*)\s*$", re.MULTILINE)
_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', re.MULTILINE)
_PY_BUILTIN_TYPES = frozenset(
    "str int float bool bytes bytearray complex list dict set frozenset tuple object type "
    "None Any Callable Iterable Iterator Sequence Mapping".split())
_EXTERNAL = ("", "")                      # a receiver type from outside the repo


def tag(node: FuncNode) -> str:
    """" [C]" / " [C++]" / " [Java]" / " [Python]" for non-Go nodes, "" for Go."""
    t = TAGS.get(getattr(node, "lang", "go"))
    return f" [{t}]" if t else ""


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _under(rel: str, d: str) -> bool:
    return not d or rel == d or rel.startswith(d + "/")


def _common(a: str, b: str) -> int:
    pa, pb = a.split("/") if a else [], b.split("/") if b else []
    n = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        n += 1
    return n


# -- shared state --------------------------------------------------------------------------


@dataclass
class _Edge:
    u: str
    v: str
    kind: str
    w: float


class _Builder:
    """state shared by the parts of one multi-language graph (ids, renames, files)."""

    def __init__(self, root: Path, ctx: RepoContext, subtree: str | None,
                 exclude: tuple[str, ...], taken: set[str]):
        self.root = root
        self.ctx = ctx
        self.subtree = (subtree or "").strip().strip("/")
        self.exclude = exclude
        self.taken = taken
        self.renames = 0
        self.vendored = sorted(d for d, names in ctx.dirs.items()
                               if d and _vendored(root / d, names))
        owned = [r for lang in ctx.languages for r in ctx.files.get(lang, [])]
        self.vendored_used = [v for v in self.vendored
                              if (not self.subtree or _under(v, self.subtree))
                              and any(_under(r, v) for r in owned)]

    def in_scope(self, rel: str) -> bool:
        if self.subtree and not _under(rel, self.subtree):
            return False
        return not any(_under(rel, v) for v in self.vendored)

    def excluded(self, rel: str) -> bool:
        return any(fnmatchcase(rel, pat) for pat in self.exclude)

    def new_node(self, nodes: dict[str, FuncNode], lang: str, rel: str, name: str, kind: str,
                 line: int, end_line: int) -> str | None:
        """a node `<dir>::<name>`, renamed `<name>@<basename>` when the id is taken."""
        d = _parent(rel)
        nid = f"{d}::{name}"
        if nid in self.taken:
            base = PurePosixPath(rel).name
            for alt_name in (f"{name}@{base}", f"{name}@{rel}"):
                alt = f"{d}::{alt_name}"
                if alt not in self.taken:
                    nid, name = alt, alt_name
                    self.renames += 1
                    break
            else:
                return None
        self.taken.add(nid)
        nodes[nid] = FuncNode(id=nid, pkg=d, file=rel, name=name, kind=kind, line=line,
                              loc=max(1, end_line - line + 1), lang=lang)
        return nid


def _facts(adapter: Any, root: Path, rel: str, src: str, ctx: RepoContext
           ) -> GraphFileFacts | None:
    try:
        return adapter.graph_facts(root, rel, src, ctx)
    except Exception as e:  # noqa: BLE001 - one unparsable file must not sink the graph
        log.debug("graph facts of %s failed: %s", rel, e)
        return None


class _Part:
    """one language's (or C+C++'s) nodes and edges; subclasses resolve the call facts."""

    langs: tuple[str, ...] = ()

    def __init__(self, b: _Builder):
        self.b = b
        self.nodes: dict[str, FuncNode] = {}
        self.edges: list[_Edge] = []
        self.test_refs: set[str] = set()
        self.var_refd: set[str] = set()
        self.spans: dict[str, tuple[str, int, int]] = {}
        self.facts: dict[str, GraphFileFacts] = {}
        self.prod: dict[str, list[str]] = {lang: [] for lang in self.langs}
        self.tests: list[str] = []
        self.generated: set[str] = set()
        self.excluded_funcs = 0
        self.tables = 0

    def role(self, lang: str, rel: str, src: str) -> str:
        """prod | test | skip."""
        raise NotImplementedError

    def adapter(self, lang: str) -> Any:
        from codocs.lang.registry import adapter

        return adapter(lang, self.b.ctx)

    def classify(self) -> None:
        for lang in self.langs:
            ad = self.adapter(lang)
            for rel in self.b.ctx.files.get(lang, []):
                if not self.b.in_scope(rel):
                    continue
                src = self.b.ctx.read(rel)
                role = self.role(lang, rel, src)
                if role == "test":
                    self.tests.append(rel)
                elif role == "prod" and not self.b.excluded(rel):
                    if ad.is_generated(src, rel):
                        self.generated.add(rel)
                    else:
                        self.prod[lang].append(rel)

    def load(self, lang: str, rel: str) -> GraphFileFacts | None:
        if rel not in self.facts:
            f = _facts(self.adapter(lang), self.b.root, rel, self.b.ctx.read(rel), self.b.ctx)
            if f is None:
                return None
            self.facts[rel] = f
        return self.facts[rel]

    def add_edge(self, u: str, v: str | None, kind: str, w: float) -> None:
        if v is not None and v != u:
            self.edges.append(_Edge(u, v, kind, w))

    def link(self, u: str, cands: list[str], kind: str) -> None:
        cands = [c for c in dict.fromkeys(cands) if c != u]
        for c in cands:
            self.add_edge(u, c, kind, 1.0 / len(cands))

    def keep(self, fact: FuncFact) -> bool:
        """False for test helpers by name (mockX, stubY, test_z), counted as excluded."""
        if _TEST_FUNC_RE.match(short_name(fact.name)):
            self.excluded_funcs += 1
            return False
        return True

    def graph_part(self, lang: str) -> GraphPart:
        nodes = {k: n for k, n in self.nodes.items() if n.lang == lang}
        edges: dict[tuple[str, str], Any] = {}
        for e in self.edges:
            if e.u in nodes:
                edges[(e.u, e.v)] = (e.kind, e.w)
        prod = set(self.prod.get(lang, []))
        owned = set(self.b.ctx.files.get(lang, []))
        return GraphPart(lang=lang, nodes=nodes, edges=edges,
                         spans={k: v for k, v in self.spans.items() if k in nodes},
                         test_refs={n for n in self.test_refs if n in nodes},
                         var_refd={n for n in self.var_refd if n in nodes},
                         facts={r: f for r, f in self.facts.items() if r in prod},
                         stats={"files": len(prod),
                                "generated_files": len(self.generated & owned)})


# -- python ----------------------------------------------------------------------------------


@dataclass
class _PyScope:
    modmap: dict[str, str] = field(default_factory=dict)          # bound/dotted -> file
    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # bound -> (file, attr)
    stars: list[str] = field(default_factory=list)


class _PythonPart(_Part):
    langs = ("python",)

    def __init__(self, b: _Builder):
        super().__init__(b)
        self.ad = self.adapter("python")
        self.model = self.ad.model
        self.funcs: dict[tuple[str, str], str] = {}               # (file, fact name) -> id
        self.meths: dict[str, dict[str, list[str]]] = {}          # file -> method -> ids
        self.scopes: dict[str, _PyScope] = {}
        self.prodset: set[str] = set()

    def role(self, lang: str, rel: str, src: str) -> str:
        kind = self.model.kind.get(rel) if self.model is not None else None
        if kind == "source":
            return "prod"
        if kind in ("test", "support"):
            return "test"
        if kind == "config":
            return "skip"
        return "test" if self.ad.is_test_file(rel, src) else "prod"

    @staticmethod
    def _prefix(rel: str) -> str:
        stem = PurePosixPath(rel).stem
        return "" if stem == "__init__" else stem

    def build(self) -> None:
        self.classify()
        self.prodset = set(self.prod["python"])
        for rel in self.prod["python"]:
            f = self.load("python", rel)
            if f is None:
                continue
            pre = self._prefix(rel)
            for fn in f.funcs:
                if not self.keep(fn):
                    continue
                name = f"{pre}.{fn.name}" if pre else fn.name
                nid = self.b.new_node(self.nodes, "python", rel, name, fn.kind, fn.line,
                                      fn.end_line)
                if nid is None:
                    continue
                self.funcs[(rel, fn.name)] = nid
                self.spans[nid] = (rel, fn.start, fn.end)
                if fn.kind == "method":
                    self.meths.setdefault(rel, {}).setdefault(
                        fn.name.rsplit(".", 1)[-1], []).append(nid)
        for rel in self.prod["python"]:
            f = self.facts.get(rel)
            if f is None:
                continue
            for name, entries in f.tables.items():
                held = [h[1] for h in (self.lookup(rel, e) for e in entries)
                        if h is not None and h[0] == "func"]
                if held:
                    self.tables += 1
                    self.var_refd.update(held)
            for fn in f.funcs:
                nid = self.funcs.get((rel, fn.name))
                if nid is None:
                    continue
                for c in f.calls.get(fn.name, []):
                    self.link(nid, self.resolve(rel, fn.cls, c), c.kind)
        for rel in self.tests:
            f = self.load("python", rel)
            if f is None:
                continue
            for fn in f.funcs:
                for c in f.calls.get(fn.name, []):
                    self.test_refs.update(self.resolve(rel, fn.cls, c))

    # -- scopes and lookups

    def scope(self, rel: str) -> _PyScope:
        sc = self.scopes.get(rel)
        if sc is not None:
            return sc
        sc = self.scopes[rel] = _PyScope()
        f = self.facts.get(rel)
        if f is None or self.model is None:
            return sc
        for spec in f.imports:
            try:
                hits = self.model.resolve_spec(rel, spec)
            except Exception:  # noqa: BLE001 - an odd import must not stop the graph
                continue
            if spec.kind == "import":
                for frel, _mod in hits:
                    sc.modmap[spec.alias or spec.module] = frel
            elif spec.kind == "from":
                if spec.name == "*":
                    sc.stars.extend(frel for frel, _ in hits)
                    continue
                for frel, binds_module in hits:
                    if binds_module:
                        sc.modmap[spec.bound] = frel
                    else:
                        sc.names[spec.bound] = (frel, spec.name)
        return sc

    def _file(self, rel: str) -> GraphFileFacts | None:
        if rel in self.facts:
            return self.facts[rel]
        if rel in self.prodset:
            return self.load("python", rel)
        return None

    def lookup(self, rel: str, name: str, depth: int = 0):
        """("func", id) | ("class", (file, qual)) for a name bound in a module, or None."""
        f = self._file(rel)
        if f is None or depth > 4:
            return None
        nid = self.funcs.get((rel, name))
        if nid is not None and "." not in name:
            return ("func", nid)
        if name in f.types:
            return ("class", (rel, name))
        sc = self.scope(rel)
        if name in sc.names:
            frel, attr = sc.names[name]
            hit = self.lookup(frel, attr, depth + 1)
            if hit is not None:
                return hit
        for srel in sc.stars:
            hit = self.lookup(srel, name, depth + 1)
            if hit is not None:
                return hit
        return None

    def class_of(self, rel: str, expr: str | None, depth: int = 0):
        """(file, qual) of a type expression in a module, _EXTERNAL, or None (unknown)."""
        if not expr or depth > 4:
            return None
        e = expr.strip().strip("'\"")
        m = re.match(r"^(?:typing\.)?Optional\[(.+)\]$", e)
        if m:
            e = m.group(1)
        e = re.split(r"\s*\|\s*None|None\s*\|\s*", e)[0].strip() if "None" in e else e
        if "[" in e:
            head = e.split("[", 1)[0].strip()
            return _EXTERNAL if head.rsplit(".", 1)[-1] in _PY_BUILTIN_TYPES or \
                head.rsplit(".", 1)[-1][:1].islower() else None
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", e):
            return None
        if e in _PY_BUILTIN_TYPES:
            return _EXTERNAL
        f = self._file(rel)
        if f is None:
            return None
        if e in f.types:
            return (rel, e)
        head, _, rest = e.partition(".")
        sc = self.scope(rel)
        if rest:
            if head in sc.modmap:
                return self.class_of(sc.modmap[head], rest, depth + 1)
            for k, frel in sc.modmap.items():
                if e.startswith(k + "."):
                    return self.class_of(frel, e[len(k) + 1:], depth + 1)
            hit = self.class_of(rel, head, depth + 1)
            if hit and hit is not _EXTERNAL:
                inner = f"{hit[1]}.{rest}"
                hf = self._file(hit[0])
                if hf is not None and inner in hf.types:
                    return (hit[0], inner)
            return None
        hit = self.lookup(rel, e)
        if hit is not None and hit[0] == "class":
            return hit[1]
        return None

    def mro(self, cref, mname: str, depth: int = 0, skip_own: bool = False) -> str | None:
        if cref is None or cref is _EXTERNAL or depth > 8:
            return None
        crel, cq = cref
        if not skip_own:
            nid = self.funcs.get((crel, f"{cq}.{mname}"))
            if nid is not None:
                return nid
        f = self._file(crel)
        tf = f.types.get(cq) if f is not None else None
        if tf is None:
            return None
        for base in tf.bases:
            hit = self.mro(self.class_of(crel, base), mname, depth + 1)
            if hit is not None:
                return hit
        return None

    def _ctor(self, cref) -> list[str]:
        nid = self.mro(cref, "__init__")
        return [nid] if nid else []

    def resolve(self, rel: str, cls: str, c: CallFact) -> list[str]:
        q, name = c.qual, c.name
        if q == "":
            hit = self.lookup(rel, name)
            if hit is None:
                return []
            return [hit[1]] if hit[0] == "func" else self._ctor(hit[1])
        if q in ("self", "cls"):
            if not cls:
                return []
            nid = self.mro((rel, cls), name)
            return [nid] if nid else []
        if q == "super":
            nid = self.mro((rel, cls), name, skip_own=True) if cls else None
            return [nid] if nid else []
        sc = self.scope(rel)
        if c.recv_type:                 # a declared type that is no project class: external
            cref = self.class_of(rel, c.recv_type)
            if cref is None or cref is _EXTERNAL:
                return []
            nid = self.mro(cref, name)
            return [nid] if nid else []
        if q in sc.modmap:
            hit = self.lookup(sc.modmap[q], name)
            if hit is None:
                return []
            return [hit[1]] if hit[0] == "func" else self._ctor(hit[1])
        cref = self.class_of(rel, q)
        if cref is _EXTERNAL:
            return []
        if cref is not None:                     # Cls.m(...) / mod.Cls.m(...)
            nid = self.mro(cref, name)
            return [nid] if nid else []
        if c.kind != "call" or q.split(".", 1)[0] in sc.modmap:
            return []
        # unknown receiver: the single method of that name in the module and its imports
        files = [rel, *sc.modmap.values(), *(r for r, _ in sc.names.values()), *sc.stars]
        cands: list[str] = []
        for frel in dict.fromkeys(files):
            cands.extend(self.meths.get(frel, {}).get(name, []))
        cands = list(dict.fromkeys(cands))
        return cands if len(cands) == 1 else []


# -- java ------------------------------------------------------------------------------------


class _JavaPart(_Part):
    langs = ("java",)

    def __init__(self, b: _Builder):
        super().__init__(b)
        self.ad = self.adapter("java")
        self.lay = self.ad.layout()
        self.types: dict[str, tuple[str, str, Any]] = {}     # FQCN -> (file, qual, TypeFact)
        self.methods: dict[str, dict[str, str]] = {}         # FQCN -> method -> id
        self.by_pkg: dict[str, list[str]] = {}               # package -> FQCNs
        self._subs: dict[str, list[str]] | None = None
        self._scopes: dict[str, dict] = {}
        self.by_fact: dict[tuple[str, str], str] = {}        # (file, fact name) -> id

    def role(self, lang: str, rel: str, src: str) -> str:
        r = self.lay.file_role.get(rel)
        if r == "main":
            return "prod"
        if r in ("test", "support"):
            return "test"
        if r == "other":
            return "skip"
        return "test" if self.ad.is_test_file(rel, src) else "prod"

    def pkg(self, rel: str) -> str:
        p = self.lay.packages.get(rel)
        if p is None:
            from codocs.lang.java import package_of

            p = self.lay.packages[rel] = package_of(self.b.ctx.read(rel))
        return p

    def fq(self, rel: str, qual: str) -> str:
        p = self.pkg(rel)
        return f"{p}.{qual}" if p else qual

    def build(self) -> None:
        self.classify()
        prod = self.prod["java"]
        for rel in prod + self.tests:
            f = self.load("java", rel)
            if f is None:
                continue
            for qual, tf in f.types.items():
                fq = self.fq(rel, qual)
                if fq not in self.types:
                    self.types[fq] = (rel, qual, tf)
                    self.by_pkg.setdefault(self.pkg(rel), []).append(fq)
        for rel in prod:
            f = self.facts.get(rel)
            if f is None:
                continue
            for fn in f.funcs:
                if not self.keep(fn):
                    continue
                nid = self.b.new_node(self.nodes, "java", rel, fn.name, fn.kind, fn.line,
                                      fn.end_line)
                if nid is None:
                    continue
                self.spans[nid] = (rel, fn.start, fn.end)
                self.by_fact[(rel, fn.name)] = nid
                self.methods.setdefault(self.fq(rel, fn.cls), {}).setdefault(
                    fn.name[len(fn.cls) + 1:], nid)
        for rel in prod:
            f = self.facts.get(rel)
            if f is None:
                continue
            for fn in f.funcs:
                nid = self.by_fact.get((rel, fn.name))
                if nid is None:
                    continue
                for c in f.calls.get(fn.name, []):
                    self.link(nid, self.resolve(rel, fn.cls, c), c.kind)
        for rel in self.tests:
            f = self.facts.get(rel)
            if f is None:
                continue
            for fn in f.funcs:
                for c in f.calls.get(fn.name, []):
                    self.test_refs.update(self.resolve(rel, fn.cls, c))

    def scope(self, rel: str) -> dict:
        sc = self._scopes.get(rel)
        if sc is None:
            sc = {"single": {}, "static": {}, "wild": [], "static_wild": []}
            f = self.facts.get(rel)
            for imp in (f.imports if f is not None else []):
                if imp.kind == "single":
                    sc["single"][imp.bound] = imp.module
                elif imp.kind == "static":
                    if imp.name == "*":
                        sc["static_wild"].append(imp.module)
                    else:
                        sc["static"].setdefault(imp.name, []).append(imp.module)
                elif imp.kind == "wildcard":
                    sc["wild"].append(imp.module)
            self._scopes[rel] = sc
        return sc

    def type_of(self, rel: str, ctx_qual: str, name: str | None, depth: int = 0) -> str | None:
        """FQCN of a type name used in a file (inside class ctx_qual), _EXTERNAL or None."""
        if not name or depth > 4:
            return None
        n = re.sub(r"<.*>", "", name).replace("[]", "").replace("...", "").strip()
        if not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", n):
            return None
        if "." in n:
            if n in self.types:
                return n
            head, rest = n.split(".", 1)
            h = self.type_of(rel, ctx_qual, head, depth + 1)
            if h and h is not _EXTERNAL and f"{h}.{rest}" in self.types:
                return f"{h}.{rest}"
            return None
        pkg = self.pkg(rel)
        parts = ctx_qual.split(".") if ctx_qual else []
        for i in range(len(parts), 0, -1):            # nested types of the enclosing classes
            cand = self.fq(rel, ".".join(parts[:i]) + "." + n)
            if cand in self.types:
                return cand
        cand = f"{pkg}.{n}" if pkg else n
        sc = self.scope(rel)
        if n in sc["single"]:
            mod = sc["single"][n]
            return mod if mod in self.types else _EXTERNAL
        if cand in self.types:
            return cand
        for w in sc["wild"]:
            if f"{w}.{n}" in self.types:
                return f"{w}.{n}"
        return None

    def _base_fqs(self, fq: str) -> list[str]:
        rel, qual, tf = self.types[fq]
        outer = qual.rsplit(".", 1)[0] if "." in qual else ""
        out = []
        for b in tf.bases:
            t = self.type_of(rel, outer, b)
            if t and t is not _EXTERNAL and t != fq:
                out.append(t)
        return out

    def mro(self, fq: str | None, m: str, depth: int = 0, skip_own: bool = False) -> str | None:
        if not fq or fq is _EXTERNAL or fq not in self.types or depth > 8:
            return None
        if not skip_own:
            nid = self.methods.get(fq, {}).get(m)
            if nid is not None:
                return nid
        for b in self._base_fqs(fq):
            hit = self.mro(b, m, depth + 1)
            if hit is not None:
                return hit
        return None

    def implementors(self, fq: str, m: str) -> list[str]:
        if self._subs is None:
            self._subs = {}
            for t in self.types:
                for b in self._base_fqs(t):
                    self._subs.setdefault(b, []).append(t)
        out: list[str] = []
        seen = {fq}
        todo = list(self._subs.get(fq, []))
        while todo:
            t = todo.pop(0)
            if t in seen:
                continue
            seen.add(t)
            nid = self.methods.get(t, {}).get(m)
            if nid is not None and nid not in out:
                out.append(nid)
            todo.extend(self._subs.get(t, []))
        return out

    def _ctor(self, fq: str | None) -> list[str]:
        if not fq or fq is _EXTERNAL:
            return []
        nid = self.methods.get(fq, {}).get(fq.rsplit(".", 1)[-1])
        return [nid] if nid else []

    def _virtual(self, fq: str, m: str) -> list[str]:
        """targets of `obj.m()` with obj of declared type fq (interfaces: implementors)."""
        tf = self.types[fq][2]
        own = self.mro(fq, m)
        if tf.is_interface or own is None:
            impl = self.implementors(fq, m)
            if impl:
                return impl if len(impl) <= MAX_METHOD_TARGETS else []
        return [own] if own else []

    def resolve(self, rel: str, cls: str, c: CallFact) -> list[str]:
        own = self.fq(rel, cls) if cls else ""
        q, name = c.qual, c.name
        if q in ("this", "super") and name == q:          # this(...) / super(...)
            if q == "this":
                return self._ctor(own)
            bases = self._base_fqs(own) if own in self.types else []
            return self._ctor(bases[0]) if bases else []
        if c.kind == "ref" and name == "new":              # Cls::new
            return self._ctor(self.type_of(rel, cls, q))
        if q == "":
            parts = cls.split(".") if cls else []
            for i in range(len(parts), 0, -1):          # own class, then enclosing classes
                nid = self.mro(self.fq(rel, ".".join(parts[:i])), name)
                if nid:
                    return [nid]
            sc = self.scope(rel)
            for mod in sc["static"].get(name, []) + sc["static_wild"]:
                nid = self.mro(mod if mod in self.types else None, name)
                if nid:
                    return [nid]
            return []
        if q == "this":
            nid = self.mro(own, name)
            return [nid] if nid else []
        if q == "super":
            nid = self.mro(own, name, skip_own=True)
            return [nid] if nid else []
        if c.recv_type and q == name == c.recv_type:     # new Cls(...)
            return self._ctor(self.type_of(rel, cls, name))
        if c.recv_type:
            t = self.type_of(rel, cls, c.recv_type)
            if t is _EXTERNAL or t is None:
                return []
            return self._virtual(t, name)
        t = self.type_of(rel, cls, q)
        if t is _EXTERNAL:
            return []
        if t is not None:                                 # Cls.m(...): static call
            nid = self.mro(t, name)
            return [nid] if nid else []
        if c.kind != "call" or not re.fullmatch(r"[a-z_$][\w$]*", q):
            return []
        # unknown receiver: the single method of that name in the package and its imports
        sc = self.scope(rel)
        fqs = list(self.by_pkg.get(self.pkg(rel), []))
        fqs += [m for m in sc["single"].values() if m in self.types]
        for w in sc["wild"]:
            fqs += self.by_pkg.get(w, [])
        cands = [self.methods[t][name] for t in dict.fromkeys(fqs)
                 if name in self.methods.get(t, {})]
        cands = list(dict.fromkeys(cands))
        return cands if len(cands) == 1 else []


# -- c and c++ -------------------------------------------------------------------------------


@dataclass
class _CgoFile:
    rel: str
    src: str
    ctext: str                            # the preamble as C, aligned to the .go file's lines
    includes: list[tuple[bool, str]]      # (quoted, header)


def _grouped_cgo_span(src: str) -> tuple[int, int] | None:
    """(start, end) of the comment right before a `"C"` spec inside an import block (the
    grouped cgo form `import (\n /* preamble */\n "C"\n "fmt"\n)`), else None."""
    from codocs.golang.source import _doc_start, _lex, _parse

    if '"C"' not in src:
        return None
    lx = _lex(src)
    for d in _parse(src).imports:
        if d.lparen is None:
            continue
        for imp, a, _b in d.specs:
            if imp.path != "C":
                continue
            start = _doc_start(src, lx, a)
            if start == a and len(d.specs) == 1:
                start = _doc_start(src, lx, d.start)    # `/* ... */ import ("C")`
                return (start, d.start) if start < d.start else None
            return (start, a) if start < a else None
    return None


def has_cgo(src: str) -> bool:
    """whether a Go file imports "C" with a preamble, alone or inside an import block."""
    from codocs.golang.source import cgo_header

    if '"C"' not in src:
        return False
    return bool(cgo_header(src)) or _grouped_cgo_span(src) is not None


def _blank_comments(src: str, a: int, b: int) -> str:
    """the comments of src[a:b] as C text at their offsets (delimiters and code blanked)."""
    from codocs.golang.source import _lex

    keep = [False] * len(src)
    for ca, cb in _lex(src).comments:
        if ca < a or cb > b:
            continue
        block = src.startswith("/*", ca) and src.startswith("*/", cb - 2) and cb - ca >= 4
        for i in range(ca + 2, cb - 2 if block else cb):
            keep[i] = True
    return "".join(ch if ch == "\n" or keep[i] else " " for i, ch in enumerate(src[:b]))


def cgo_preamble(src: str) -> str:
    """the C preamble of a cgo Go file (lone `import "C"`, or a `"C"` spec in an import
    block), at its original offsets: every other character is blanked, so lines and columns
    match the .go file ("" without cgo)."""
    from codocs.golang.source import cgo_header

    header = cgo_header(src) if '"C"' in src else ""
    pre = _lone_preamble(src, header) if header else ""
    if pre:
        return pre
    span = _grouped_cgo_span(src)
    return _blank_comments(src, *span) if span is not None else ""


def _lone_preamble(src: str, header: str) -> str:
    k = header.rfind('import "C"')
    if k < 0:
        k = header.rfind("import")
    block = header[:k].rstrip()
    if "\n\n" in block and block.lstrip().startswith(("//go:build", "// +build")):
        block = block.split("\n\n", 1)[1]
    block = block.strip()
    if not block:
        return ""
    at = src.find(block)
    if at < 0:
        return ""
    if block.startswith("/*"):
        inner = "  " + block[2:-2] + "  " if block.endswith("*/") else "  " + block[2:]
    else:
        inner = "\n".join(re.sub(r"^(\s*)//", r"\1  ", ln) for ln in block.split("\n"))
    head = "".join(ch if ch == "\n" else " " for ch in src[:at])
    return head + inner


class _CcPart(_Part):
    langs = ("c", "cpp")

    def __init__(self, b: _Builder, go: _GoPart | None = None):
        super().__init__(b)
        self.go = go
        self.static_of: dict[tuple[str, str], str] = {}        # (file, f) -> id
        self.dir_funcs: dict[str, dict[str, list[str]]] = {}   # dir -> f -> ids
        self.all_funcs: dict[str, list[str]] = {}              # f -> non-static ids
        self.cls_methods: dict[str, dict[str, list[str]]] = {}  # class -> m -> ids
        self.method_names: set[str] = set()
        self.bases: dict[str, list[str]] = {}
        self.file_tables: dict[tuple[str, str], list[str]] = {}
        self.dir_tables: dict[tuple[str, str], list[str]] = {}
        self.preamble_of: dict[str, dict[str, str]] = {}      # go file -> f -> id
        self.cgo_files: dict[str, _CgoFile] = {}               # go file -> its preamble
        self.cgo_go: set[str] = set()                          # go files with import "C"
        self.go_exports: dict[str, list[str]] = {}            # name -> Go ids
        self.export_edges = 0
        self.fact_name: dict[str, str] = {}                   # id -> fact name

    def role(self, lang: str, rel: str, src: str) -> str:
        from codocs.lang.cc import in_test_dir

        ad = self.adapter(lang)
        if ad.is_test_file(rel, src) or in_test_dir(_parent(rel)):
            return "test"
        return "prod"

    def _cgo(self) -> None:
        go = self.go
        if go is None or not {"c", "cpp"} & set(self.b.ctx.languages):
            return              # cgo also compiles a package's .cc/.cpp (extern "C" calls)
        for f in go.files:
            if not has_cgo(f.src):
                continue
            self.cgo_go.add(f.rel)
            for m in _EXPORT_RE.finditer(f.src):
                nid = f"{f.pkg}::{m.group(1)}"
                if nid in go.nodes:
                    self.go_exports.setdefault(m.group(1), []).append(nid)
            ctext = cgo_preamble(f.src)
            if not ctext.strip():
                continue
            inc = [(m.group(1) == '"', m.group(2).strip()) for m in _INCLUDE_RE.finditer(ctext)]
            self.cgo_files[f.rel] = _CgoFile(f.rel, f.src, ctext, inc)

    def build(self) -> None:
        self.classify()
        self._cgo()
        order: list[tuple[str, str, bool]] = []                 # (lang, rel, is cgo)
        for lang in self.langs:
            rels = [(r, False) for r in self.prod[lang]]
            if lang == self.langs[0]:
                rels += [(r, True) for r in self.cgo_files]      # preambles: once, C grammar
            order += [(lang, r, cg) for r, cg in sorted(rels)]
        c_ad = self.adapter("c")
        for lang, rel, cg in order:
            if cg:
                f = _facts(c_ad, self.b.root, rel, self.cgo_files[rel].ctext, self.b.ctx)
                if f is None:
                    continue
                self.facts[rel] = f
            else:
                f = self.load(lang, rel)
                if f is None:
                    continue
            base = PurePosixPath(rel).name
            for fn in f.funcs:
                if not self.keep(fn):
                    continue
                bare = fn.name.split("@", 1)[0]
                name = f"{bare}@{base}" if cg else fn.name
                nid = self.b.new_node(self.nodes, lang, rel, name, fn.kind, fn.line,
                                      fn.end_line)
                if nid is None:
                    continue
                self.fact_name[nid] = fn.name
                self.spans[nid] = (rel, fn.start, fn.end)
                if cg:
                    self.preamble_of.setdefault(rel, {})[bare] = nid
                    self.static_of[(rel, bare)] = nid
                elif fn.kind == "method":
                    cls = fn.cls.split(".")[-1]
                    m = fn.name.rsplit(".", 1)[-1]
                    self.cls_methods.setdefault(cls, {}).setdefault(m, []).append(nid)
                    self.method_names.add(m)
                elif fn.static:
                    self.static_of[(rel, bare)] = nid
                else:
                    self.dir_funcs.setdefault(_parent(rel), {}).setdefault(bare, []).append(nid)
                    self.all_funcs.setdefault(bare, []).append(nid)
            for cname, tf in f.types.items():
                if tf.bases:
                    self.bases.setdefault(cname, [])
                    self.bases[cname] += [b for b in tf.bases if b not in self.bases[cname]]
        for _lang, rel, _cg in order:
            f = self.facts.get(rel)
            if f is None:
                continue
            for var, idents in f.tables.items():
                held = []
                for ident in idents:
                    t = self.func_targets(rel, ident)
                    if len(t) == 1 and t[0] not in held:
                        held.append(t[0])
                if held:
                    self.tables += 1
                    self.var_refd.update(held)
                    self.file_tables[(rel, var)] = held
                    self.dir_tables.setdefault((_parent(rel), var), held)
        for lang, rel, _cg in order:
            f = self.facts.get(rel)
            if f is None:
                continue
            for fn in f.funcs:
                nid = self._node_of(rel, fn)
                if nid is None:
                    continue
                for c in f.calls.get(fn.name, []):
                    self._edge(nid, rel, lang, fn.cls, c)
        cpp = set(self.b.ctx.files.get("cpp", []))
        for rel in self.tests:
            lang = "cpp" if rel in cpp else "c"
            f = self.load(lang, rel)
            if f is None:
                continue
            for fn in f.funcs:
                for c in f.calls.get(fn.name, []):
                    if c.kind == "call" or c.name in self.all_funcs:
                        self.test_refs.update(self.targets(rel, lang, fn.cls, c))

    def _node_of(self, rel: str, fn: FuncFact) -> str | None:
        bare = fn.name.split("@", 1)[0]
        if rel in self.cgo_files:
            return self.preamble_of.get(rel, {}).get(bare)
        if fn.kind == "method":
            m = fn.name.rsplit(".", 1)[-1]
            for nid in self.cls_methods.get(fn.cls.split(".")[-1], {}).get(m, []):
                if self.nodes[nid].file == rel and self.fact_name.get(nid) == fn.name:
                    return nid
            return None
        if fn.static:
            return self.static_of.get((rel, bare))
        for nid in self.dir_funcs.get(_parent(rel), {}).get(bare, []):
            if self.nodes[nid].file == rel:
                return nid
        return None

    def nearest(self, ids: list[str], to: str) -> list[str]:
        """the ids defined nearest to file `to` by path (most common leading dirs)."""
        if len(ids) <= 1:
            return list(ids)
        base = _parent(to)
        best = max(_common(self.nodes[i].pkg, base) for i in ids)
        return sorted(i for i in ids if _common(self.nodes[i].pkg, base) == best)

    def func_targets(self, rel: str, name: str) -> list[str]:
        """a free function by name from file rel: static here, same dir, nearest in repo."""
        nid = self.static_of.get((rel, name))
        if nid is not None:
            return [nid]
        here = self.dir_funcs.get(_parent(rel), {}).get(name)
        if here:
            return here if len(here) <= MAX_METHOD_TARGETS else []
        hits = self.all_funcs.get(name)
        if hits:
            near = self.nearest(hits, rel)
            return near if len(near) <= MAX_METHOD_TARGETS else []
        return []

    def method_targets(self, cls: str, m: str, rel: str, depth: int = 0) -> list[str]:
        cls = cls.split("::")[-1].split(".")[-1]
        hits = self.cls_methods.get(cls, {}).get(m)
        if hits:
            near = self.nearest(hits, rel)
            return near if len(near) <= MAX_METHOD_TARGETS else []
        if depth < 6:
            for b in self.bases.get(cls, []):
                got = self.method_targets(b, m, rel, depth + 1)
                if got:
                    return got
        return []

    def targets(self, rel: str, lang: str, cls: str, c: CallFact) -> list[str]:
        q, name = c.qual, c.name
        if c.kind == "ref":
            return self.func_targets(rel, name)
        if lang == "cpp" and name in self.cls_methods.get(name, {}) and \
                q not in self.cls_methods:
            return self.method_targets(name, name, rel)        # Cls(...) / ns::Cls(...)
        if q == "":
            if cls:
                got = self.method_targets(cls, name, rel)
                if got:
                    return got
            return self.func_targets(rel, name)
        if q == "this":
            return self.method_targets(cls, name, rel) if cls else []
        if c.recv_type:
            return self.method_targets(c.recv_type, name, rel)
        if lang == "c" or q == "std":
            return []                     # struct member calls are function pointers
        if q in self.cls_methods:
            return self.method_targets(q, name, rel)
        if q[:1].isupper() or name in self.method_names:
            return []
        return self.func_targets(rel, name)        # ns::f()

    def _edge(self, nid: str, rel: str, lang: str, cls: str, c: CallFact) -> None:
        if c.kind == "ref":
            tab = self.file_tables.get((rel, c.name)) or self.dir_tables.get(
                (_parent(rel), c.name))
            if tab:
                for t in tab:
                    self.add_edge(nid, t, "table", 1.0 / len(tab))
                return
        got = self.targets(rel, lang, cls, c)
        if got:
            self.link(nid, got, c.kind)
            return
        if c.kind == "call" and c.qual == "" and c.name in self.go_exports:
            gos = self.go_exports[c.name]
            if len(gos) > 1:
                base = _parent(rel)
                pkg = {g: self.go.nodes[g].pkg for g in gos}
                best = max(_common(pkg[g], base) for g in gos)
                gos = [g for g in gos if _common(pkg[g], base) == best]
            if len(gos) <= MAX_METHOD_TARGETS:
                for g in gos:
                    self.edges.append(_Edge(nid, g, "call", 1.0 / len(gos)))
                    self.export_edges += 1


# -- cobol -----------------------------------------------------------------------------------


class _CobolPart(_Part):
    """programs, paragraphs, JCL jobs and steps and CICS transactions of the COBOL index."""

    langs = ("cobol",)

    def __init__(self, b: _Builder):
        super().__init__(b)
        self.index: Any = None
        self.ids: dict[str, str] = {}                 # node name -> node id
        self.xfer: dict[tuple[str, str], str] = {}    # program transfer edges -> kind
        self.unresolved: list[tuple[str, str, str]] = []   # (node id, CALL literal, file)
        self.jobs = 0
        self.transactions = 0

    def role(self, lang: str, rel: str, src: str) -> str:
        return "prod"

    def build(self) -> None:
        from codocs.cobol import graphfacts

        b = self.b
        self.index = graphfacts.cobol_index(b.ctx, b.subtree or None)
        facts = graphfacts.graph_part_facts(
            self.index, b.ctx, in_scope=lambda rel: b.in_scope(rel) and not b.excluded(rel))
        self.prod["cobol"] = list(facts.files)
        self.generated = set(facts.generated)
        for n in facts.nodes:
            nid = b.new_node(self.nodes, "cobol", n.file, n.name, n.kind, n.line, n.end_line)
            if nid is not None:
                self.ids[n.name] = nid
        for e in facts.edges:
            u, v = self.ids.get(e.src), self.ids.get(e.dst)
            if u is None or v is None or u == v:
                continue
            self.add_edge(u, v, e.kind, e.weight)
            if e.xfer:
                self.xfer[(u, v)] = e.xfer
        self.test_refs = {self.ids[n] for n in facts.test_refs if n in self.ids}
        self.unresolved = [(self.ids[u], lit, rel) for u, lit, rel in facts.unresolved
                           if u in self.ids]
        self.jobs, self.transactions = facts.jobs, facts.transactions

    @staticmethod
    def utility_key(node: FuncNode) -> str:
        """the qualified name: paragraph and step names repeat across programs and jobs."""
        return node.name.split("@", 1)[0]


# -- cross-language linkers ------------------------------------------------------------------


class CrossLinker(Protocol):
    name: str

    def link(self, parts: dict[str, GraphPart], ctx: StaticContext
             ) -> list[tuple[str, str, str, float]]: ...


class CgoLinker:
    """Go -> C edges for `C.f(...)` in cgo files (and the C -> Go //export edges' stats)."""

    name = "cgo"

    def __init__(self) -> None:
        self.stats = {"cgo_calls": 0, "cgo_refs": 0, "cgo_exports": 0,
                      "cgo_unresolved_names": 0, "cgo_to_generated": 0}

    def link(self, parts: dict[str, GraphPart], ctx: StaticContext
             ) -> list[tuple[str, str, str, float]]:
        go = parts.get("go")
        cc: _CcPart | None = getattr(parts.get("c") or parts.get("cpp"), "owner", None)
        if go is None or cc is None or not cc.cgo_go:
            return []
        self.stats["cgo_exports"] = cc.export_edges
        masked: dict[str, str] = getattr(go, "masked", {})
        defined = self._defined(cc, ctx)
        gen_only = defined["generated"]
        any_def = defined["all"]
        out: dict[tuple[str, str], tuple[str, float]] = {}
        unresolved: set[str] = set()
        protos: dict[str, dict[str, list[str]]] = {}
        for nid, (rel, start, end) in go.spans.items():
            if rel not in cc.cgo_go or rel not in masked:
                continue
            body = masked[rel][start:end]
            for m in _C_SEL_RE.finditer(body):
                name, paren = m.group(1), m.group(2)
                if name in CGO_SKIP or name.startswith(_CGO_SKIP_PREFIX):
                    continue
                cands = self._candidates(cc, ctx, rel, name, protos)
                if not cands:
                    if name in gen_only:
                        self.stats["cgo_to_generated"] += 1
                    elif name in any_def:
                        unresolved.add(name)
                    continue
                if len(cands) > MAX_METHOD_TARGETS:
                    unresolved.add(name)
                    continue
                kind = "call" if paren else "ref"
                for c in cands:
                    key = (nid, c)
                    w = 1.0 / len(cands)
                    old = out.get(key)
                    if old is None or (old[0] == "ref" and kind == "call") or old[1] < w:
                        out[key] = (kind if old is None or kind == "call" else old[0],
                                    max(w, old[1]) if old else w)
        self.stats["cgo_calls"] = sum(1 for k, _ in out.values() if k == "call")
        self.stats["cgo_refs"] = sum(1 for k, _ in out.values() if k == "ref")
        self.stats["cgo_unresolved_names"] = len(unresolved)
        return [(u, v, k, w) for (u, v), (k, w) in out.items()]

    @staticmethod
    def _defined(cc: _CcPart, ctx: StaticContext) -> dict[str, set[str]]:
        """names defined (non-static, non-test) anywhere in C/C++, and only in generated code."""
        prod = set(cc.all_funcs)
        try:
            index = cc.adapter("c").symbols()
        except Exception:  # noqa: BLE001 - statistics only
            index = {}
        gen = {n for n, rels in index.items()
               if n not in prod and rels and all(r in cc.generated for r in rels)}
        return {"all": prod | set(index), "generated": gen}

    def _candidates(self, cc: _CcPart, ctx: StaticContext, rel: str, name: str,
                    protos: dict[str, dict[str, list[str]]]) -> list[str]:
        pre = cc.preamble_of.get(rel, {}).get(name)          # 1. the file's own preamble
        if pre is not None:
            return [pre]
        here = cc.dir_funcs.get(_parent(rel), {}).get(name)   # 2. C files of the package dir
        if here:
            return list(here)
        hits = cc.all_funcs.get(name)
        if not hits:
            return []
        if rel not in protos:
            protos[rel] = self._prototypes(cc, ctx, rel)
        headers = protos[rel].get(name)
        if headers:                                          # 3. prototyped in an include
            return self._by_header(cc, ctx, hits, headers)
        return cc.nearest(hits, rel)                         # 4. nearest in the repo

    @staticmethod
    def _by_header(cc: _CcPart, ctx: StaticContext, hits: list[str], headers: list[str]
                   ) -> list[str]:
        """the definitions of the library a prototyping header belongs to: those whose file
        includes the header, else those nearest the header by path."""
        c_ad = cc.adapter("c")
        wanted = set(headers)
        own: list[str] = []
        for nid in hits:
            f = cc.nodes[nid].file
            facts = cc.facts.get(f)
            for imp in facts.imports if facts is not None else []:
                spec = f"<{imp.module}>" if imp.kind == "angle" else imp.module
                try:
                    got = c_ad.resolve_include(f, spec, ctx)
                except Exception:  # noqa: BLE001
                    got = None
                if got in wanted:
                    own.append(nid)
                    break
        pool = own or hits
        if len(pool) <= 1:
            return list(pool)
        score = {i: max(_common(_parent(cc.nodes[i].file), _parent(h)) for h in headers)
                 for i in pool}
        best = max(score.values())
        return sorted(i for i in pool if score[i] == best)

    @staticmethod
    def _prototypes(cc: _CcPart, ctx: StaticContext, rel: str) -> dict[str, list[str]]:
        """function name -> the headers (rels) the preamble includes (2 levels deep) that
        declare it."""
        from codocs.lang.cc import scan_decls

        cf = cc.cgo_files.get(rel)
        if cf is None:
            return {}
        c_ad = cc.adapter("c")
        names: dict[str, list[str]] = {}
        todo = [(rel, q, h, 0) for q, h in cf.includes]
        seen: set[str] = set()
        while todo:
            frm, quoted, header, depth = todo.pop(0)
            try:
                got = c_ad.resolve_include(frm, f'"{header}"' if quoted else f"<{header}>",
                                           ctx)
            except Exception:  # noqa: BLE001
                got = None
            if got is None or got in seen:
                continue
            seen.add(got)
            text = ctx.read(got)
            try:
                decls = scan_decls(text, c_ad.lang_of(got), prototypes=True)
            except Exception:  # noqa: BLE001
                decls = []
            for d in decls:
                if d.kind == "func" and got not in names.setdefault(d.name, []):
                    names[d.name].append(got)
            if depth < 1:
                todo += [(got, m.group(1) == '"', m.group(2).strip(), depth + 1)
                         for m in _INCLUDE_RE.finditer(text)]
        return names


class CobolCLinker:
    """COBOL -> C edges: an unresolved static `CALL 'name'` (exact spelling, as GnuCOBOL
    resolves C symbols) to the non-static C function of that name nearest by path."""

    name = "cobol-c"
    languages = ("cobol",)

    def __init__(self) -> None:
        self.stats = {"cobol_c_calls": 0, "cobol_c_unresolved": 0}

    def link(self, parts: dict[str, GraphPart], ctx: StaticContext
             ) -> list[tuple[str, str, str, float]]:
        cob: _CobolPart | None = getattr(parts.get("cobol"), "owner", None)
        cc: _CcPart | None = getattr(parts.get("c") or parts.get("cpp"), "owner", None)
        if cob is None or cc is None:
            return []
        out: dict[tuple[str, str], float] = {}
        unresolved: set[str] = set()
        for nid, literal, rel in cob.unresolved:
            hits = cc.all_funcs.get(literal)
            cands = cc.nearest(hits, rel) if hits else []
            if not cands or len(cands) > MAX_METHOD_TARGETS:
                unresolved.add(literal)
                continue
            for c in cands:
                out[(nid, c)] = max(out.get((nid, c), 0.0), 1.0 / len(cands))
        self.stats["cobol_c_calls"] = len(out)
        self.stats["cobol_c_unresolved"] = len(unresolved)
        return [(u, v, "call", w) for (u, v), w in out.items()]


LINKERS: list[Any] = [CgoLinker, CobolCLinker]


def _linker_applies(name: str, languages: list[str], has_go: bool) -> bool:
    """each linker runs only for its languages (cgo: Go + C/C++; cobol-c: COBOL + C/C++)."""
    cc = "c" in languages or "cpp" in languages
    if name == "cgo":
        return has_go and cc
    if name == "cobol-c":
        return "cobol" in languages and cc
    return True


# -- assembly --------------------------------------------------------------------------------


@dataclass
class _OwnedPart(GraphPart):
    owner: Any = None
    masked: dict[str, str] = field(default_factory=dict)


def lang_parts(root: Path, languages: list[str], ctx: RepoContext, *, subtree: str | None,
               exclude: tuple[str, ...], go: _GoPart | None, taken: set[str]
               ) -> tuple[dict[str, _OwnedPart], list[_Part], _Builder]:
    """the non-Go parts of the graph, built in ALL_LANGS order (ids taken by Go stay Go's)."""
    b = _Builder(root, ctx, subtree, exclude, taken)
    built: list[_Part] = []
    parts: dict[str, _OwnedPart] = {}
    cc_done = False
    for lang in ALL_LANGS:
        if lang == "go" or lang not in languages:
            continue
        if lang == "cobol":
            p: _Part = _CobolPart(b)
        elif lang in ("c", "cpp"):
            if cc_done:
                continue
            cc_done = True
            p = _CcPart(b, go)
            p.langs = tuple(x for x in ("c", "cpp") if x in languages)
            p.prod = {x: [] for x in p.langs}
        elif lang == "java":
            p = _JavaPart(b)
        else:
            p = _PythonPart(b)
        p.build()
        built.append(p)
        for x in p.langs:
            gp = p.graph_part(x)
            parts[x] = _OwnedPart(**{k: getattr(gp, k) for k in GraphPart.__dataclass_fields__},
                                  owner=p)
    return parts, built, b


def build_multi(root: Path, languages: list[str], *, subtree: str | None = None,
                docs: DocIndex | None = None, utility_fanin: int = 40,
                fold_accessors: bool = True, exclude: tuple[str, ...] = (),
                plumbing: str | None = None, infra_callers: int = 40,
                business_pkgs: tuple[str, ...] = (), out_dir=None,
                exclude_paths: tuple[str, ...] = ()) -> FuncGraph:
    """one graph over every selected language (Go part as in Go-only mode, others tree-sitter,
    COBOL from the COBOL index; exclude_paths: the COBOL scope's `--exclude-path` globs)."""
    prune = None
    if out_dir is not None:                   # OUT copies of Go files are not the repo's
        with contextlib.suppress(OSError):
            prune = Path(out_dir).expanduser().resolve()
    go = _go_part(root, subtree, exclude, prune) if "go" in languages else None
    if go is not None:
        g, nodes = go.graph, go.nodes
        test_refs, var_refd = set(go.test_refs), set(go.var_refd)
        module = go.module
    else:
        g, nodes, test_refs, var_refd = nx.DiGraph(), {}, set(), set()
        module = _module_name(root) or root.name
    others = [lang for lang in languages if lang != "go"]
    settings = None
    if exclude_paths:                         # the COBOL index sees the analysis scope
        from codocs.config import Settings

        settings = Settings(exclude_paths="\n".join(exclude_paths))
    ctx = RepoContext.build(root, others, settings, out_dir)
    parts, built, b = lang_parts(root, languages, ctx, subtree=subtree, exclude=exclude, go=go,
                                 taken=set(nodes))
    for p in built:
        new = {k: n for k, n in p.nodes.items() if k not in nodes}
        g.add_nodes_from(new)
        nodes.update(new)
    for p in built:
        for e in p.edges:
            if e.u in nodes and e.v in nodes:
                _add(g, e.u, e.v, e.kind, e.w)
                if "go" in (nodes[e.u].lang, nodes[e.v].lang):
                    g[e.u][e.v]["xlang"] = "cgo"
        for (u, v), xfer in getattr(p, "xfer", {}).items():
            if g.has_edge(u, v):
                g[u][v]["xfer"] = xfer
        test_refs |= p.test_refs
        var_refd |= p.var_refd
    cross: dict[str, int] = {}
    all_parts: dict[str, GraphPart] = dict(parts)
    if go is not None:
        all_parts = {"go": _OwnedPart(lang="go", nodes=go.nodes, spans=go.spans, owner=go,
                                      masked={f.rel: f.masked for f in go.files}), **parts}
    for factory in LINKERS:                   # linkers run with or without a Go part
        linker = factory()
        if not _linker_applies(linker.name, languages, go is not None):
            continue
        for u, v, kind, w in linker.link(all_parts, ctx):
            if u in nodes and v in nodes:
                _add(g, u, v, kind, w)
                g[u][v]["xlang"] = linker.name
        cross.update(linker.stats)
    langs_stats: dict[str, dict] = {}
    if go is not None:
        langs_stats["go"] = {"files": len(go.files)}
    for lang in others:
        if lang in parts:
            langs_stats[lang] = {"files": parts[lang].stats.get("files", 0)}
            owner = parts[lang].owner
            if isinstance(owner, _CobolPart):
                langs_stats[lang].update(jobs=owner.jobs, transactions=owner.transactions)
    vendored = list(go.vendored) if go is not None else []
    vendored += [v for v in b.vendored_used if v not in vendored]
    part_stats = {
        "files": sum(s["files"] for s in langs_stats.values()),
        "handler_tables": (go.tables if go is not None else 0) + sum(p.tables for p in built),
        "function_aliases": go.aliases if go is not None else 0,
        "excluded_test_functions": (go.excluded_funcs if go is not None else 0)
        + sum(p.excluded_funcs for p in built),
        "vendored_dirs": list(dict.fromkeys(vendored)),
        "languages": langs_stats,
        "cross_edges": cross,
        "id_renames": b.renames,
    }
    utility_keys = {lang: p.utility_key for p in built if hasattr(p, "utility_key")
                    for lang in p.langs}
    return _finish(module, g, nodes, test_refs, var_refd, docs, utility_fanin, fold_accessors,
                   plumbing, infra_callers, business_pkgs, part_stats, multi=True,
                   extra_plumbing=EXTRA_PLUMBING if plumbing else None,
                   extra_accessors=EXTRA_ACCESSORS, utility_keys=utility_keys or None)
