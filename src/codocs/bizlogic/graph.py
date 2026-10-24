"""step 1: a function-level dependency graph of a Go module (static, no go toolchain).

nodes are top-level funcs and methods of non-test, non-generated files. edges:
  call  `F(...)`, `pkg.F(...)`, `x.M(...)`. a method call is resolved by light receiver
        typing (receiver, parameters, locals, package vars, struct fields, call results);
        when the receiver type is unknown, only a method name with a single candidate in the
        package and its imports is linked. calls on external types (sync, error, ...) are not.
  ref   a function used as a value (`Submit(handle)`, `var Get = getImpl`)
  table a package var holding function values (`var handlers = map[string]func(){...}`):
        users of the var reach every entry, or the one field they select (`Store.Del(...)`)
function-variable aliases (`var XFunc = pkg.X`, the usual dependency-injection hook) are
followed across packages, so `XFunc(...)` is a call of pkg.X.

utility nodes (fan-in >= utility_fanin, also summed over same-named copies; tiny accessors;
plumbing names such as loggers, lock and print helpers; every function of an
infrastructure package that many packages call) are marked; walks and clustering skip
them. functions referenced only from _test.go files are test support and are left out, as
are vendored third-party trees (LICENSE + AUTHORS/CONTRIBUTORS).

languages: a Go module repo (auto) or ["go"] builds exactly this Go-only graph. any other
language set adds Python/Java/C/C++ functions (tree-sitter facts, `bizlogic.xlang`),
COBOL programs, paragraphs, JCL jobs and CICS transactions (the COBOL index), and cgo /
COBOL-to-C edges into one graph; the Go part's nodes and edges are the same in both modes.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import networkx as nx

from codocs.bizlogic.docs import DocIndex
from codocs.golang.source import imports, is_generated, mask, top_level_decls

log = logging.getLogger(__name__)

SKIP_DIRS = {"vendor", "testdata", "node_modules", "markdown_docs"}
# test-support code is not business logic: unit-test tools, stubs, mocks, test_*.go helpers
DEFAULT_EXCLUDE = ("*/ut/*", "ut/*", "*/stub/*", "stub/*", "*/stubs/*", "*/mock/*", "*/mocks/*",
                   "*/test/*", "test/*", "*/tests/*", "*/testtool/*", "*/testutil/*",
                   "*/test_*.go", "test_*.go")
# helpers that are plumbing whatever their fan-in: logging, locking, printing, error text
# (a domain profile's `plumbing` regex is added to this)
DEFAULT_PLUMBING = r"Lock(?![a-z])|^(un)?lock|Log$|Print$|ErrMsg$|^new\w*Error$"
_TEST_FUNC_RE = re.compile(r"^(test|Test|mock|Mock|stub|Stub|fake|Fake|dummy|Dummy)([A-Z_]|$)")
MAX_METHOD_TARGETS = 3
_MODULE_RE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
_QUAL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)(\s*\()?")
_METHOD_RE = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(")
_BARE_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)(\s*\()?")
_SEL_RE = re.compile(r"\s*\.\s*([A-Za-z_]\w*)(\s*\()?")
_KEY_RE = re.compile(r"\s*:(?!=)")
# `var F = f`, `var F func(int) = f`, `var F = pkg.F` (grouped members are normalised to this)
_VAR_ALIAS_RE = re.compile(r"^var (\w+)\b[^=]*=\s*(?:(\w+)\s*\.\s*)?(\w+)$")
_FIELD_ENTRY_RE = re.compile(
    r"(?<![\w.])([A-Za-z_]\w*)\s*:(?!=)\s*(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)\s*(?=[,}\n])")
_TYPE_HEAD_RE = re.compile(r"^[*&\s]*([A-Za-z_]\w*)(?:\s*\.\s*([A-Za-z_]\w*))?")
_CHAIN_TAIL_RE = re.compile(r"([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)\s*$")
_BUILTIN_TYPES = frozenset(
    "bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune "
    "string uint uint8 uint16 uint32 uint64 uintptr any".split())
_ASSIGN = r"\s*(?::=|(?<![=!<>+\-*/%&|^:])=(?!=))\s*"
_CHAIN = r"[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*"
_LOCAL_RES = (
    ("type", re.compile(r"\bvar\s+(\w+)(?:\s*,\s*\w+)*\s+([*\w.]+)")),
    ("type", re.compile(r"(?<![\w.])(\w+)" + _ASSIGN + r"&?\s*([A-Za-z_][\w.]*)\s*\{")),
    ("type", re.compile(r"(?<![\w.])(\w+)" + _ASSIGN + r"new\(\s*([*\w.]+)\s*\)")),
    ("type", re.compile(r"(?<![\w.])(\w+)(?:\s*,\s*\w+)?" + _ASSIGN + _CHAIN
                        + r"\s*\.\(\s*([*\w.]+)\s*\)")),
    ("call", re.compile(r"(?<![\w.])(\w+)(?:\s*,\s*\w+)*" + _ASSIGN + "(" + _CHAIN
                        + r")\s*\(")),
    ("chain", re.compile(r"(?<![\w.])(\w+)" + _ASSIGN + "(" + _CHAIN + r")\s*(?=[;\n)}])")),
)
EXTERNAL = ("", "")                 # a type from outside the module (or builtin)


@dataclass
class FuncNode:
    id: str            # "<pkg>::<Name>" or "<pkg>::<Recv>.<Name>"
    pkg: str
    file: str          # rel path
    name: str          # "Name" or "Recv.Name"
    kind: str          # "func" | "method"
    line: int
    loc: int
    summary: str = ""
    doc: str = ""
    lang: str = "go"   # not serialized: nodes.json keeps (file, line, summary)


@dataclass
class FuncGraph:
    module: str
    graph: nx.DiGraph
    nodes: dict[str, FuncNode]
    utilities: set[str] = field(default_factory=set)
    stats: dict = field(default_factory=dict)

    def business(self) -> list[str]:
        return [n for n in self.nodes if n not in self.utilities]

    def fanin(self, node: str) -> int:
        return self.graph.in_degree(node)


@dataclass
class _File:
    rel: str
    pkg: str
    src: str
    masked: str
    imports: dict[str, str]        # name used in code -> package rel dir (module-internal only)
    ext: set[str] = field(default_factory=set)   # names of imports from outside the module


@dataclass
class _Type:
    kind: str                                      # "struct" | "interface" | "other"
    file: _File
    fields: dict[str, str | None] = field(default_factory=dict)   # field -> type expression
    embedded: list[str] = field(default_factory=list)


@dataclass
class _Table:
    values: list[str]                              # every function the initializer holds
    fields: dict[str, str] = field(default_factory=dict)   # keyed entry -> function


def _module_name(root: Path) -> str:
    try:
        m = _MODULE_RE.search((root / "go.mod").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def _vendored(dirpath: Path, filenames: list[str]) -> bool:
    """a copied third-party tree: its own licence plus an authors/contributors list."""
    names = {f.upper().split(".", 1)[0] for f in filenames}
    return any(n.startswith("LICEN") for n in names) and bool(names & {"AUTHORS", "CONTRIBUTORS"})


def _scan_files(root: Path, subtree: str | None, exclude: tuple[str, ...] = (),
                tests: bool = False, skipped: list[str] | None = None,
                prune: Path | None = None) -> list[tuple[str, str]]:
    """(rel path, source) of production files, or of _test.go files when `tests`.

    prune: a directory left out entirely (multi mode: OUT inside the repo).
    """
    base = root / subtree if subtree else root
    out: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if prune is not None:
            dirnames[:] = [d for d in dirnames if Path(dirpath, d) != prune]
        if Path(dirpath) != root and _vendored(Path(dirpath), filenames):
            dirnames[:] = []
            if skipped is not None:
                skipped.append(Path(dirpath).relative_to(root).as_posix())
            continue
        for f in sorted(filenames):
            if not f.endswith(".go") or f.endswith("_test.go") != tests:
                continue
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            if not tests and any(fnmatchcase(rel, pat) for pat in exclude):
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if is_generated(src):
                continue
            out.append((rel, src))
    return out


_ACCESSOR_RE = re.compile(r"^(Get|Set|Is|Has)[A-Z_]?")


def _close(text: str, i: int) -> int:
    """index of the bracket closing the one at text[i] (len(text) if unbalanced)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for j in range(i, len(text)):
        ch = text[j]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return j
    return len(text)


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _split_sig(sig: str, method: bool) -> tuple[str, str, str]:
    """(receiver, parameters, results) text of a `func ...` signature."""
    i = sig.find("(")
    recv = ""
    if method and i >= 0:
        j = _close(sig, i)
        recv, sig, i = sig[i + 1:j], sig[j + 1:], -1
        i = sig.find("(")
    if i < 0:
        return recv, "", ""
    head = sig[:i]
    if "[" in head:                              # type parameters before the parameters
        k = head.index("[")
        i = sig.find("(", _close(sig, k))
    j = _close(sig, i)
    return recv, sig[i + 1:j], sig[j + 1:].strip()


def _params(text: str) -> dict[str, str]:
    """name -> type expression of a Go parameter list (`a, b int, c *T`)."""
    out: dict[str, str] = {}
    carry = None
    for item in reversed(_split_top(text)):
        m = re.match(r"^([A-Za-z_]\w*)\s+(\S.*)$", item)
        if m:
            carry = m.group(2)
            out[m.group(1)] = carry
        elif re.match(r"^[A-Za-z_]\w*$", item) and carry is not None:
            out[item] = carry
    return out


def _first_result(text: str) -> str:
    text = text.strip()
    if text.startswith("("):
        items = _split_top(text[1:_close(text, 0)])
        if not items:
            return ""
        m = re.match(r"^[A-Za-z_]\w*\s+(\S.*)$", items[0])
        return m.group(1) if m else items[0]
    return text


def _flatten(body: str) -> str:
    """struct body with nested braces emptied, so each field is one line."""
    out, depth = [], 0
    for ch in body:
        if ch == "{":
            depth += 1
            if depth == 1:
                out.append("{")
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                out.append("}")
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _struct_fields(body: str) -> tuple[dict[str, str | None], list[str]]:
    fields: dict[str, str | None] = {}
    embedded: list[str] = []
    for seg in re.split(r"[\n;]", _flatten(body)):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"^([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+([^`\s].*)$", seg)
        if m:
            typ = m.group(2).strip()
            typ = None if typ.startswith(("func", "map", "[", "chan", "interface", "struct")) \
                else typ
            for name in re.split(r"\s*,\s*", m.group(1)):
                fields[name] = typ
            continue
        m = re.match(r"^\*?\s*([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)?)", seg)
        if m:
            embedded.append(m.group(1))
            fields[m.group(1).rsplit(".", 1)[-1].strip()] = m.group(1)
    return fields, embedded


@dataclass
class _GoPart:
    """the Go share of the graph: files, nodes and edges (no utilities yet)."""
    module: str
    files: list[_File]
    nodes: dict[str, FuncNode]
    graph: nx.DiGraph
    test_refs: set[str]
    var_refd: set[str]
    tables: int
    aliases: int
    excluded_funcs: int
    vendored: list[str]
    spans: dict[str, tuple[str, int, int]]


def _go_part(root: Path, subtree: str | None, exclude: tuple[str, ...],
             prune: Path | None = None) -> _GoPart:
    """files, nodes and edges of the Go code (the whole graph in Go-only mode).

    prune: a dir whose files are left out (multi mode passes OUT; Go-only mode never does).
    """
    module = _module_name(root)
    files: list[_File] = []
    funcs: dict[str, dict[str, str]] = {}          # pkg -> func name -> node id
    methods: dict[str, dict[str, list[str]]] = {}  # pkg -> method name -> node ids
    method_of: dict[tuple[str, str], dict[str, str]] = {}   # (pkg, type) -> method -> node id
    aliases: dict[str, dict[str, tuple[str, str]]] = {}     # pkg -> var -> (pkg, name)
    types: dict[tuple[str, str], _Type] = {}
    nodes: dict[str, FuncNode] = {}
    sigs: dict[str, str] = {}
    spans: dict[str, tuple[_File, int, int]] = {}
    var_spans: dict[str, dict[str, tuple[_File, int, int]]] = {}  # pkg -> var -> initializer

    def file_of(src: str, rel: str) -> _File:
        pkg = rel.rsplit("/", 1)[0] if "/" in rel else ""
        imap: dict[str, str] = {}
        ext: set[str] = set()
        for imp in imports(src):
            if imp.alias in ("_", "."):
                continue
            if module and imp.path.startswith(module + "/"):
                imap[imp.name] = imp.path[len(module) + 1:]
            else:
                ext.add(imp.name)
        return _File(rel, pkg, src, mask(src), imap, ext)

    vendored: list[str] = []
    excluded_funcs = 0
    for rel, src in _scan_files(root, subtree, tuple(exclude), skipped=vendored, prune=prune):
        f = file_of(src, rel)
        pkg = f.pkg
        files.append(f)
        decls = top_level_decls(src)
        for name, (a, b) in _var_slices(src, decls).items():
            var_spans.setdefault(pkg, {})[name] = (f, a, b)
        for d in decls:
            if d.kind == "var":
                sig = re.sub(r"\s*//.*$", "", " ".join(d.signature.split()))
                m = _VAR_ALIAS_RE.match(sig)
                if m and (m.group(2) is None or m.group(2) in f.imports):
                    target_pkg = f.imports[m.group(2)] if m.group(2) else pkg
                    aliases.setdefault(pkg, {})[m.group(1)] = (target_pkg, m.group(3))
                continue
            if d.kind == "type":
                _add_type(types, f, d)
                continue
            if d.kind not in ("func", "method") or d.name in ("init", "_"):
                continue
            if _TEST_FUNC_RE.match(d.name):
                excluded_funcs += 1
                continue
            name = f"{d.receiver}.{d.name}" if d.kind == "method" else d.name
            nid = f"{pkg}::{name}"
            if nid in nodes:
                continue
            nodes[nid] = FuncNode(id=nid, pkg=pkg, file=rel, name=name, kind=d.kind,
                                  line=d.line, loc=max(1, d.end_line - d.line + 1))
            spans[nid] = (f, d.start, d.end)    # masked: comments and strings are blank
            sigs[nid] = d.signature
            if d.kind == "method":
                methods.setdefault(pkg, {}).setdefault(d.name, []).append(nid)
                method_of.setdefault((pkg, d.receiver), {})[d.name] = nid
            else:
                funcs.setdefault(pkg, {})[d.name] = nid

    def resolve_func(pkg: str, name: str) -> str | None:
        """a function, following function-variable aliases (also across packages)."""
        for _ in range(5):
            tgt = funcs.get(pkg, {}).get(name)
            if tgt:
                return tgt
            nxt = aliases.get(pkg, {}).get(name)
            if nxt is None:
                return None
            pkg, name = nxt
        return None

    def func_values(f: _File, body: str) -> _Table:
        """functions an initializer holds as values (not calls): handler-table entries."""
        found: dict[str, None] = {}
        fields: dict[str, str] = {}
        for m in _FIELD_ENTRY_RE.finditer(body):
            if m.group(2) and m.group(2) not in f.imports:
                continue
            tgt = resolve_func(f.imports[m.group(2)] if m.group(2) else f.pkg, m.group(3))
            if tgt:
                fields[m.group(1)] = tgt
        for m in _QUAL_RE.finditer(body):
            if m.group(1) in f.imports and not m.group(3):
                tgt = resolve_func(f.imports[m.group(1)], m.group(2))
                if tgt:
                    found[tgt] = None
        for m in _BARE_RE.finditer(body):
            if not m.group(2) and not _KEY_RE.match(body, m.end(1)):   # `Key: value` keys
                tgt = resolve_func(f.pkg, m.group(1))
                if tgt:
                    found[tgt] = None
        return _Table(sorted(found), fields)

    # package-level tables (`var handlers = map[string]func(){"reg": reg.Handle, ...}`): a
    # function that uses the table reaches every function the table holds, or the one
    # keyed entry it selects (`Store.Del(...)`). aliases are resolved separately.
    table_refs: dict[str, dict[str, _Table]] = {}
    var_refd: set[str] = set()
    for pkg, vs in var_spans.items():
        for name, (f, start, end) in vs.items():
            refs = func_values(f, f.masked[start:end])
            var_refd.update(refs.values)
            if name in aliases.get(pkg, {}):
                continue
            if refs.values:
                table_refs.setdefault(pkg, {})[name] = refs

    # ---- light receiver typing for method calls
    def type_ref(f: _File, expr: str | None) -> tuple[str, str] | None:
        if not expr:
            return None
        expr = expr.strip()
        if expr.startswith(("[", "(", "map[", "func", "chan", "interface", "struct", "<-")):
            return None
        m = _TYPE_HEAD_RE.match(expr)
        if not m:
            return None
        a, b = m.group(1), m.group(2)
        if b:
            if a in f.imports:
                return (f.imports[a], b)
            return EXTERNAL if a in f.ext else None
        if a in _BUILTIN_TYPES:
            return EXTERNAL
        return (f.pkg, a)

    var_types: dict[tuple[str, str], tuple[str, _File, str] | None] = {}

    def var_type(pkg: str, name: str, depth: int) -> tuple[str, str] | None:
        key = (pkg, name)
        if key not in var_types:
            var_types[key] = None
            span = var_spans.get(pkg, {}).get(name)
            if span:
                f, a, b = span
                text = f.masked[a:b]
                pat = re.escape(name)
                m = re.search(r"(?<![\w.])" + pat + r"\s+([*\w.]+)\s*(?:=|$|\n)", text)
                if m:
                    var_types[key] = ("type", f, m.group(1))
                elif m := re.search(r"(?<![\w.])" + pat + r"\s*=\s*&?\s*([A-Za-z_][\w.]*)\s*\{",
                                    text):
                    var_types[key] = ("type", f, m.group(1))
                elif m := re.search(r"(?<![\w.])" + pat + r"\s*=\s*(" + _CHAIN + r")\s*\(", text):
                    var_types[key] = ("call", f, m.group(1))
        entry = var_types[key]
        if entry is None:
            return None
        kind, f, expr = entry
        return type_ref(f, expr) if kind == "type" else call_type(f, {}, expr, depth + 1)

    def result_type(nid: str | None, depth: int) -> tuple[str, str] | None:
        if nid is None or nid not in sigs:
            return None
        _recv, _params_text, res = _split_sig(sigs[nid], nodes[nid].kind == "method")
        return type_ref(spans[nid][0], _first_result(res))

    def find_member(t: tuple[str, str], name: str, depth: int = 0):
        """('method', node id) | ('field', type expression and its file) | None."""
        if t == EXTERNAL or depth > 3:
            return None
        nid = method_of.get(t, {}).get(name)
        if nid:
            return ("method", nid)
        info = types.get(t)
        if info is None:
            return None
        if name in info.fields and name not in {e.rsplit(".", 1)[-1] for e in info.embedded}:
            return ("field", (info.fields[name], info.file))
        for e in info.embedded:
            et = type_ref(info.file, e)
            if et is not None:
                hit = find_member(et, name, depth + 1)
                if hit:
                    return hit
        if name in info.fields:
            return ("field", (info.fields[name], info.file))
        return None

    def chain_type(f: _File, env: dict, chain: list[str], depth: int):
        if depth > 6 or not chain:
            return None
        head, rest = chain[0], chain[1:]
        if head in env:
            t = local_type(f, env, head, depth + 1)
        elif head in f.imports and rest:
            t = var_type(f.imports[head], rest[0], depth + 1)
            rest = rest[1:]
        elif head in f.ext:
            return EXTERNAL
        elif head in var_spans.get(f.pkg, {}):
            t = var_type(f.pkg, head, depth + 1)
        else:
            return None
        for name in rest:
            if t is None or t == EXTERNAL:
                return t
            hit = find_member(t, name)
            if not hit or hit[0] != "field":
                return None
            expr, tf = hit[1]
            t = type_ref(tf, expr)
        return t

    def call_type(f: _File, env: dict, chain_text: str, depth: int):
        chain = [c.strip() for c in chain_text.split(".")]
        if depth > 6:
            return None
        if len(chain) == 1:
            return result_type(resolve_func(f.pkg, chain[0]), depth)
        if len(chain) == 2 and chain[0] in f.imports and chain[0] not in env:
            return result_type(resolve_func(f.imports[chain[0]], chain[1]), depth)
        if chain[0] in f.ext and chain[0] not in env:
            return EXTERNAL
        t = chain_type(f, env, chain[:-1], depth + 1)
        if t is None or t == EXTERNAL:
            return t
        hit = find_member(t, chain[-1])
        return result_type(hit[1], depth) if hit and hit[0] == "method" else None

    def local_type(f: _File, env: dict, name: str, depth: int):
        kind, expr = env[name]
        if kind == "resolved":
            return expr
        env[name] = ("resolved", None)              # cycle guard
        if kind == "type":
            t = type_ref(f, expr)
        elif kind == "call":
            t = call_type(f, env, expr, depth + 1)
        else:
            t = chain_type(f, env, [c.strip() for c in expr.split(".")], depth + 1)
        env[name] = ("resolved", t)
        return t

    def local_env(nid: str, body: str) -> dict:
        env: dict[str, tuple[str, object]] = {}
        recv, params, _res = _split_sig(sigs[nid], nodes[nid].kind == "method")
        for name, typ in list(_params(params).items()) + list(_params(recv).items()):
            env[name] = ("type", typ)
        for kind, rx in _LOCAL_RES:
            for m in rx.finditer(body):
                if m.group(1) != "_" and m.group(1) not in env:
                    env[m.group(1)] = (kind, m.group(2))
        return env

    def method_targets(f: _File, env: dict, body: str, m: re.Match, nid: str) -> list[str]:
        name = m.group(1)
        cands = list(methods.get(f.pkg, {}).get(name, []))
        for p in sorted(set(f.imports.values())):
            cands += methods.get(p, {}).get(name, [])
        cands = [c for c in dict.fromkeys(cands) if c != nid]
        if not cands:
            return []
        start = m.start()
        before = body[max(0, start - 200):start]
        cm = _CHAIN_TAIL_RE.search(before)
        t = None
        if cm:
            prev = before[:cm.start()].rstrip(" \t")[-1:]     # a newline starts a statement
            if prev not in (")", "]", "."):
                t = chain_type(f, env, [c.strip() for c in cm.group(1).split(".")], 0)
        if t == EXTERNAL:
            return []
        if t is not None:
            info = types.get(t)
            if info is not None and info.kind == "interface":
                return cands if len(cands) <= MAX_METHOD_TARGETS else []
            hit = find_member(t, name)
            if hit and hit[0] == "method":
                return [hit[1]] if hit[1] != nid else []
            if hit or info is not None:          # a field of a known type, or no such method
                return []
        return cands if len(cands) == 1 else []

    g = nx.DiGraph()
    g.add_nodes_from(nodes)

    def table_use(nid: str, tab: _Table, body: str, pos: int) -> None:
        sel = _SEL_RE.match(body, pos)
        if sel and tab.fields:
            tgt = tab.fields.get(sel.group(1))
            if tgt and tgt != nid:
                _add(g, nid, tgt, "call" if sel.group(2) else "ref", 1.0)
            return
        for tgt in tab.values:
            if tgt != nid:
                _add(g, nid, tgt, "table", 1.0 / len(tab.values))

    for nid, (f, start, end) in spans.items():
        body = f.masked[start:end]
        for m in _QUAL_RE.finditer(body):
            pkg_dir = f.imports.get(m.group(1))
            tab = table_refs.get(pkg_dir, {}).get(m.group(2)) if pkg_dir else None
            if tab:
                table_use(nid, tab, body, m.end(2))
        for m in _BARE_RE.finditer(body):
            tab = table_refs.get(f.pkg, {}).get(m.group(1))
            if tab:
                table_use(nid, tab, body, m.end(1))
        seen_q: set[tuple[int, int]] = set()
        for m in _QUAL_RE.finditer(body):
            qual, name, paren = m.group(1), m.group(2), m.group(3)
            if qual in f.ext and qual not in f.imports:
                seen_q.add(m.span(2))             # `json.Marshal(`: not a method call
                continue
            if qual not in f.imports:
                continue
            seen_q.add(m.span(2))
            tgt = resolve_func(f.imports[qual], name)
            if tgt and tgt != nid:
                _add(g, nid, tgt, "call" if paren else "ref", 1.0)
        env: dict | None = None
        for m in _METHOD_RE.finditer(body):
            if m.span(1) in seen_q:
                continue
            if env is None:
                env = local_env(nid, body)
            cands = method_targets(f, env, body, m, nid)
            for c in cands:
                _add(g, nid, c, "call", 1.0 / len(cands))
        for m in _BARE_RE.finditer(body):
            tgt = resolve_func(f.pkg, m.group(1))
            if tgt and tgt != nid:
                _add(g, nid, tgt, "call" if m.group(2) else "ref", 1.0)

    # functions only test files name (and nothing in production code uses) are test support
    test_refs: set[str] = set()
    for rel, src in _scan_files(root, subtree, tests=True, prune=prune):
        f = file_of(src, rel)
        for m in _QUAL_RE.finditer(f.masked):
            if m.group(1) in f.imports:
                tgt = resolve_func(f.imports[m.group(1)], m.group(2))
                if tgt:
                    test_refs.add(tgt)
        for m in _BARE_RE.finditer(f.masked):
            tgt = resolve_func(f.pkg, m.group(1))
            if tgt:
                test_refs.add(tgt)
    n_alias = sum(len(v) for v in aliases.values())
    return _GoPart(module=module, files=files, nodes=nodes, graph=g, test_refs=test_refs,
                   var_refd=var_refd, tables=sum(len(v) for v in table_refs.values()),
                   aliases=n_alias, excluded_funcs=excluded_funcs, vendored=vendored,
                   spans={n: (f.rel, a, b) for n, (f, a, b) in spans.items()})


def _graph_languages(root: Path, subtree: str | None, languages, out_dir) -> list[str]:
    """languages of the graph: detection for None / a --source-lang string, else the list."""
    from codocs.cobol.graphfacts import ALL_LANGS, COBOL_ALIASES, detect, require_programs
    from codocs.lang.registry import ALIASES

    if languages is None or isinstance(languages, str):
        req = (languages or "auto").strip() or "auto"
        try:
            return detect(root, subtree, req, out_dir=out_dir)
        except ValueError:
            if req.lower() == "auto":           # nothing detected: a Go-only scan
                return ["go"]
            raise
    aliases = {**COBOL_ALIASES, **ALIASES}
    names = {aliases.get(str(x).strip().lower(), str(x).strip().lower()) for x in languages}
    unknown = sorted(names - set(ALL_LANGS))
    if unknown:
        raise ValueError(f"unknown language(s) for the graph: {', '.join(unknown)}")
    if "cobol" in names:
        require_programs(root, subtree, out_dir=out_dir)
    return [lang for lang in ALL_LANGS if lang in names]


def build_graph(module_root: str | Path, subtree: str | None = None,
                docs: DocIndex | None = None, utility_fanin: int = 40,
                fold_accessors: bool = True,
                exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
                plumbing: str | None = DEFAULT_PLUMBING, infra_callers: int = 40,
                business_pkgs: tuple[str, ...] = (), *, languages=None,
                out_dir: str | Path | None = None,
                exclude_paths: tuple[str, ...] = ()) -> FuncGraph:
    """parse the module (or one subtree) into a FuncGraph. a package called from at least
    `infra_callers` other packages is infrastructure (key-value stores, loggers, tracing,
    ...): all of its functions are utilities, unless it matches a `business_pkgs` glob
    (0 disables).

    languages: None (auto-detect, as `--source-lang auto`), a `--source-lang` string, or a
    list. a Go module repo, or ["go"], gives the Go-only graph; other language sets
    add Python/Java/C/C++ functions (tree-sitter) and cgo edges into one graph.
    exclude_paths: the COBOL scope's `--exclude-path` globs (the COBOL index only)."""
    root = Path(module_root).resolve()
    langs = _graph_languages(root, subtree, languages, out_dir)
    if langs != ["go"]:
        from codocs.lang.treesitter import with_deps

        langs = with_deps(langs, "bizlogic graph")
    if langs == ["go"]:
        part = _go_part(root, subtree, tuple(exclude))
        return _finish(part.module, part.graph, part.nodes, part.test_refs, part.var_refd,
                       docs, utility_fanin, fold_accessors, plumbing, infra_callers,
                       business_pkgs, _go_stats(part))
    from codocs.bizlogic import xlang

    return xlang.build_multi(root, langs, subtree=subtree, docs=docs,
                             utility_fanin=utility_fanin, fold_accessors=fold_accessors,
                             exclude=tuple(exclude), plumbing=plumbing,
                             infra_callers=infra_callers, business_pkgs=business_pkgs,
                             out_dir=out_dir, exclude_paths=tuple(exclude_paths))


def _go_stats(part: _GoPart) -> dict:
    return {"files": len(part.files), "handler_tables": part.tables,
            "function_aliases": part.aliases, "excluded_test_functions": part.excluded_funcs,
            "vendored_dirs": part.vendored}


def short_name(name: str) -> str:
    """the bare function name of a node name ("Recv.M" -> "M", static C "f@x.c" -> "f")."""
    return name.rsplit(".", 1)[-1].split("@", 1)[0]


def _finish(module: str, g: nx.DiGraph, nodes: dict[str, FuncNode], test_refs: set[str],
            var_refd: set[str], docs: DocIndex | None, utility_fanin: int,
            fold_accessors: bool, plumbing: str | None, infra_callers: int,
            business_pkgs: tuple[str, ...], part_stats: dict, *, multi: bool = False,
            extra_plumbing: dict[str, re.Pattern] | None = None,
            extra_accessors: re.Pattern | None = None,
            utility_keys: dict | None = None) -> FuncGraph:
    """test-only removal, utilities, docs and stats over the (merged) graph.

    multi=False builds the Go-only graph; multi=True also
    strips static/renamed `@file` suffixes before name rules and adds per-language rules.
    utility_keys: language -> node -> the key same-named copies are grouped by for the
    summed fan-in rule (default: the short name; COBOL uses qualified names)."""
    base = short_name if multi else (lambda name: name.rsplit(".", 1)[-1])
    whole = short_name if multi else (lambda name: name)      # for the `main` checks
    # functions only test files name (and nothing in production code uses) are test support
    test_only = sorted(n for n in test_refs if n in g and g.in_degree(n) == 0
                       and n not in var_refd and whole(nodes[n].name) != "main")
    g.remove_nodes_from(test_only)
    for n in test_only:
        del nodes[n]

    n_call = n_ref = n_table = 0
    for _u, _v, d in g.edges(data=True):
        n_call += d["kind"] == "call"
        n_ref += d["kind"] == "ref"
        n_table += d["kind"] == "table"

    # fan-in counts direct callers and references only: a handler listed in a dispatch
    # table is not a utility because many functions consult the table. helpers copied into
    # several packages are judged by their summed fan-in.
    fanin = {n: sum(1 for _p, _n, d in g.in_edges(n, data=True) if d["kind"] != "table")
             for n in g}
    utilities = {n for n, k in fanin.items() if k >= utility_fanin}
    by_name: dict[str, list[str]] = {}
    for n in g:
        ukey = (utility_keys or {}).get(nodes[n].lang)
        by_name.setdefault(ukey(nodes[n]) if ukey else base(nodes[n].name), []).append(n)
    for name, copies in by_name.items():
        if len(copies) > 1 and name != "main" and sum(fanin[n] for n in copies) >= utility_fanin \
                and all(fanin[n] > 0 for n in copies):
            utilities.update(copies)
    if fold_accessors:      # tiny getters/setters carry no business step of their own
        utilities |= {nid for nid, n in nodes.items() if n.kind == "method" and n.loc <= 4
                      and _ACCESSOR_RE.match(base(n.name))}
        if extra_accessors is not None:
            utilities |= {nid for nid, n in nodes.items() if n.lang != "go"
                          and n.kind == "method" and n.loc <= 4
                          and extra_accessors.match(base(n.name))}
    if plumbing:
        prx = re.compile(plumbing)
        utilities |= {nid for nid, n in nodes.items() if whole(n.name) != "main"
                      and prx.search(base(n.name))}
        for lang, rx in (extra_plumbing or {}).items():
            utilities |= {nid for nid, n in nodes.items() if n.lang == lang
                          and whole(n.name) != "main" and rx.search(base(n.name))}
    infra: list[str] = []
    if infra_callers > 0:
        callers: dict[str, set[str]] = {}
        for u, v, d in g.edges(data=True):
            if d["kind"] != "table" and nodes[u].pkg != nodes[v].pkg:
                callers.setdefault(nodes[v].pkg, set()).add(nodes[u].pkg)
        infra = sorted(p for p, c in callers.items() if len(c) >= infra_callers
                       and not any(fnmatchcase(p, pat) for pat in business_pkgs))
        infra_set = set(infra)
        utilities |= {nid for nid, n in nodes.items() if n.pkg in infra_set}
    if docs is not None:
        for nid, node in nodes.items():
            sec = docs.get(node.pkg, node.name)
            if sec is None and multi and node.lang != "go":
                sec = _doc_fallback(docs, node)
            if sec is not None:
                node.summary, node.doc = sec.summary, sec.body
    stats = {
        "files": part_stats["files"],
        "functions": sum(n.kind == "func" for n in nodes.values()),
        "methods": sum(n.kind == "method" for n in nodes.values()),
        "edges": g.number_of_edges(), "call_edges": n_call, "ref_edges": n_ref,
        "table_edges": n_table,
        "handler_tables": part_stats["handler_tables"],
        "function_aliases": part_stats["function_aliases"],
        "utilities": len(utilities), "utility_fanin": utility_fanin,
        "infra_packages": infra,
        "documented": sum(bool(n.summary) for n in nodes.values()),
        "excluded_test_functions": part_stats["excluded_test_functions"],
        "test_only_functions": len(test_only),
        "vendored_dirs": part_stats["vendored_dirs"],
    }
    if multi:
        stats.update(_multi_stats(g, nodes, part_stats))
    log.info("graph: %s", stats)
    return FuncGraph(module=module, graph=g, nodes=nodes, utilities=utilities, stats=stats)


def _doc_fallback(docs: DocIndex, node: FuncNode):
    """doc section of a non-Go node under its documented name (no module prefix / @file)."""
    name = node.name.split("@", 1)[0]
    cands = [name]
    if node.lang == "python" and "." in name:
        cands.append(name.split(".", 1)[1])
    for c in cands:
        sec = docs.get(node.pkg, c)
        if sec is not None:
            return sec
    return None


def _multi_stats(g: nx.DiGraph, nodes: dict[str, FuncNode], part_stats: dict) -> dict:
    """per-language counts, cross-language edge counts and id renames (multi mode only)."""
    langs: dict[str, dict] = {}
    for lang, info in part_stats.get("languages", {}).items():
        langs[lang] = {"files": info.get("files", 0), "functions": 0, "methods": 0,
                       "edges": 0}
        for extra in ("jobs", "transactions"):          # COBOL: JCL jobs, CICS transactions
            if extra in info:
                langs[lang][extra] = info[extra]
    for n in nodes.values():
        row = langs.setdefault(n.lang, {"files": 0, "functions": 0, "methods": 0, "edges": 0})
        row["functions" if n.kind == "func" else "methods"] += 1
    cross = dict(part_stats.get("cross_edges", {}))
    counted = {"cgo_calls": 0, "cgo_refs": 0, "cgo_exports": 0}
    for u, v, d in g.edges(data=True):
        langs[nodes[u].lang]["edges"] += 1
        if d.get("xlang") == "cgo":           # counted on the final graph (after test-only)
            if nodes[u].lang == "go":
                counted["cgo_calls" if d["kind"] == "call" else "cgo_refs"] += 1
            else:
                counted["cgo_exports"] += 1
    if cross and any(k.startswith("cgo_") for k in cross):
        cross.update(counted)
    return {"languages": langs, "cross_edges": cross,
            "id_renames": part_stats.get("id_renames", 0)}


def _add_type(types: dict, f: _File, d) -> None:
    text = f.masked[d.start:d.end]
    m = re.search(r"(?<![\w.])" + re.escape(d.name) + r"\s*(?:\[[^\]]*\])?\s+(=\s*)?"
                  r"(struct|interface)\s*\{", text)
    if not m:
        types.setdefault((f.pkg, d.name), _Type("other", f))
        return
    brace = m.end() - 1
    body = text[brace + 1:_close(text, brace)]
    if m.group(2) == "interface":
        types.setdefault((f.pkg, d.name), _Type("interface", f))
        return
    fields, embedded = _struct_fields(body)
    types.setdefault((f.pkg, d.name), _Type("struct", f, fields, embedded))


_RANK = {"table": 0, "ref": 1, "call": 2}


def _var_slices(src: str, decls) -> dict[str, tuple[int, int]]:
    """each top-level var's own initializer text: members of a `var ( ... )` group share
    the group's span, so each is cut from its own line to the next member's line."""
    line_starts = [0] + [i + 1 for i, ch in enumerate(src) if ch == "\n"]
    groups: dict[tuple[int, int], list] = {}
    for d in decls:
        if d.kind == "var":
            groups.setdefault((d.start, d.end), []).append(d)
    out: dict[str, tuple[int, int]] = {}
    for (start, end), members in groups.items():
        members.sort(key=lambda d: d.line)
        if len(members) == 1:
            out[members[0].name] = (start, end)
            continue
        for i, d in enumerate(members):
            a = line_starts[d.line - 1] if d.line - 1 < len(line_starts) else start
            nxt = members[i + 1].line if i + 1 < len(members) else None
            b = line_starts[nxt - 1] if nxt and nxt - 1 < len(line_starts) else end
            if b <= a:                       # several names on one line: share it
                b = line_starts[d.line] if d.line < len(line_starts) else end
            out[d.name] = (max(a, start), min(b, end))
    return out


def _add(g: nx.DiGraph, u: str, v: str, kind: str, w: float) -> None:
    if g.has_edge(u, v):
        d = g[u][v]
        d["weight"] = max(d["weight"], w)
        if _RANK[kind] > _RANK[d["kind"]]:
            d["kind"] = kind
    else:
        g.add_edge(u, v, kind=kind, weight=w)
