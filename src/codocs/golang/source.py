"""pure go source helpers: masking, top-level declarations, imports, test functions, code extraction."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

KEYWORDS = frozenset(
    "break case chan const continue default defer else fallthrough for func go goto if import "
    "interface map package range return select struct switch type var".split()
)

_OPS3 = frozenset({"<<=", ">>=", "&^=", "..."})
_OPS2 = frozenset("<< >> &^ += -= *= /= %= &= |= ^= && || <- ++ -- == != <= >= :=".split())
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}
_SEMI_KW = frozenset({"break", "continue", "fallthrough", "return"})
_SEMI_OPS = frozenset({")", "]", "}", "++", "--"})
_VERSION_RE = re.compile(r"^v\d+$")
_IDENT_RE = re.compile(r"(?<!\w)[^\W\d]\w*")
_GENERATED_RE = re.compile(r"^// Code generated .* DO NOT EDIT\.\r?$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```[ \t]*([\w+#.-]*)[^\n]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_EDGE_BLANKS_RE = re.compile(r"\A\n(?:[ \t]*\n)+")
_TAIL_BLANKS_RE = re.compile(r"\n(?:[ \t]*\n)+([ \t]*)\Z")
_TEST_PARAM_RE = re.compile(r"^(?:[^\W\d]\w*\s+)?\*\s*(?:([^\W\d]\w*)\s*\.\s*)?T\s*,?$")


class Tok(NamedTuple):
    kind: str  # "ident" | "kw" | "num" | "str" | "op" (auto semicolons: zero-width ";")
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _Lex:
    toks: tuple[Tok, ...]
    comments: tuple[tuple[int, int], ...]
    match: tuple[int, ...]  # opener <-> closer token index, -1 when unmatched
    line_starts: tuple[int, ...]
    open_strs: frozenset[int]  # start offsets of unterminated string/rune literals

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def line_start(self, offset: int) -> int:
        return self.line_starts[self.line(offset) - 1]


def _semi_due(toks: list[Tok]) -> bool:
    if not toks:
        return False
    t = toks[-1]
    if t.kind in ("ident", "num", "str"):
        return True
    if t.kind == "kw":
        return t.text in _SEMI_KW
    return t.text in _SEMI_OPS and t.end > t.start


@lru_cache(maxsize=64)
def _lex(src: str) -> _Lex:
    """tokenize go source (with automatic semicolons) and record comment spans."""
    toks: list[Tok] = []
    comments: list[tuple[int, int]] = []
    open_strs: set[int] = set()
    n = len(src)
    i = 0
    while i < n:
        c = src[i]
        if c == "\n":
            if _semi_due(toks):
                toks.append(Tok("op", ";", i, i))
            i += 1
            continue
        if c in " \t\r\f\v﻿":
            i += 1
            continue
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            comments.append((i, j))
            i = j
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            comments.append((i, j))
            if "\n" in src[i:j] and _semi_due(toks):
                toks.append(Tok("op", ";", i, i))
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" and j + 1 < n and src[j + 1] != "\n" else 1
            if j < n and src[j] == c:
                j += 1
            else:
                open_strs.add(i)
            toks.append(Tok("str", src[i:j], i, j))
            i = j
            continue
        if c == "`":
            j = src.find("`", i + 1)
            if j < 0:
                open_strs.add(i)
                j = n
            else:
                j += 1
            toks.append(Tok("str", src[i:j], i, j))
            i = j
            continue
        if c == "_" or c.isalpha():
            j = i + 1
            while j < n and (src[j] == "_" or src[j].isalnum()):
                j += 1
            word = src[i:j]
            toks.append(Tok("kw" if word in KEYWORDS else "ident", word, i, j))
            i = j
            continue
        if c.isdigit() or (c == "." and nxt.isdigit()):
            exps = "pP" if src.startswith(("0x", "0X"), i) else "eE"
            j = i + 1
            while j < n:
                ch = src[j]
                if ch.isalnum() or ch in "_.":
                    j += 1
                elif ch in "+-" and src[j - 1] in exps:
                    j += 1
                else:
                    break
            toks.append(Tok("num", src[i:j], i, j))
            i = j
            continue
        if src[i : i + 3] in _OPS3:
            width = 3
        elif src[i : i + 2] in _OPS2:
            width = 2
        else:
            width = 1
        toks.append(Tok("op", src[i : i + width], i, i + width))
        i += width
    if _semi_due(toks):
        toks.append(Tok("op", ";", n, n))

    match = [-1] * len(toks)
    stack: list[int] = []
    for k, t in enumerate(toks):
        if t.kind != "op":
            continue
        if t.text in _OPENERS:
            stack.append(k)
        elif t.text in _CLOSERS:
            want = _CLOSERS[t.text]
            for d in range(len(stack) - 1, -1, -1):
                if toks[stack[d]].text == want:
                    for o in stack[d:]:
                        match[o] = k
                    match[k] = stack[d]
                    del stack[d:]
                    break
    for o in stack:
        match[o] = len(toks) - 1
    line_starts = [0] + [m.end() for m in re.finditer("\n", src)]
    return _Lex(
        tuple(toks), tuple(comments), tuple(match), tuple(line_starts), frozenset(open_strs)
    )


def mask(src: str) -> str:
    """blank out comment, string and rune contents (delimiters and newlines kept)."""
    lx = _lex(src)
    if not lx.comments and not any(t.kind == "str" for t in lx.toks):
        return src
    parts: list[str] = []
    pos = 0

    def blank(a: int, b: int) -> None:
        nonlocal pos
        if b <= a:
            return
        parts.append(src[pos:a])
        parts.append(re.sub(r"[^\r\n]", " ", src[a:b]))
        pos = b

    spans = [(a, b, "c") for a, b in lx.comments] + [
        (t.start, t.end, "s") for t in lx.toks if t.kind == "str"
    ]
    for a, b, what in sorted(spans):
        if what == "c":
            closed = src.startswith("//", a) or (b - a >= 4 and src.startswith("*/", b - 2))
            blank(a + 2, b if not closed or src.startswith("//", a) else b - 2)
        else:
            blank(a + 1, b if a in lx.open_strs else b - 1)
    parts.append(src[pos:])
    return "".join(parts)


@dataclass
class Decl:
    kind: str
    name: str
    receiver: str = ""
    start: int = 0
    end: int = 0
    line: int = 0
    end_line: int = 0
    signature: str = ""

    @property
    def qualname(self) -> str:
        return (
            f"{self.receiver}.{self.name}" if self.kind == "method" and self.receiver else self.name
        )


@dataclass
class Import:
    path: str
    alias: str | None = None
    line: int = 0

    @property
    def name(self) -> str:
        return self.alias if self.alias else assumed_name(self.path)


def assumed_name(path: str) -> str:
    """package name guessed from an import path (goimports' heuristic)."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    base = parts[-1]
    if _VERSION_RE.match(base) and len(parts) > 1:
        base = parts[-2]
    if base.startswith("go-"):
        base = base[3:]
    m = re.match(r"\w*", base)
    return m.group(0) if m else base


@dataclass
class _Func:
    decl: Decl
    params: tuple[int, int] | None  # token indices of '(' and ')'
    body: tuple[int, int] | None  # token indices of '{' and '}'
    type_params: bool
    result: bool


@dataclass
class _ImportDecl:
    start: int
    end: int
    lparen: int | None
    rparen: int | None
    specs: list[tuple[Import, int, int]]


@dataclass
class _Parsed:
    package: tuple[int, int, str] | None
    imports: list[_ImportDecl]
    decls: list[Decl]
    funcs: list[_Func]
    type_specs: dict[str, tuple[int, int]]


def _is(toks: tuple[Tok, ...], k: int, text: str) -> bool:
    return k < len(toks) and toks[k].kind == "op" and toks[k].text == text


def _close(lx: _Lex, k: int) -> int:
    m = lx.match[k]
    return m if m >= k else len(lx.toks) - 1


def _skip_stmt(lx: _Lex, i: int, stop: int | None = None) -> int:
    """index of the ';' ending the statement at i (or stop / end of tokens)."""
    toks = lx.toks
    stop = len(toks) if stop is None else stop
    while i < stop:
        t = toks[i]
        if t.kind == "op":
            if t.text == ";":
                return i
            if t.text in _OPENERS:
                i = _close(lx, i) + 1
                continue
        i += 1
    return stop


def _uncomment(src: str, lx: _Lex, a: int, b: int) -> str:
    out: list[str] = []
    pos = a
    for ca, cb in lx.comments:
        if cb <= a or ca >= b:
            continue
        out.append(src[pos : max(ca, pos)])
        out.append(" ")
        pos = min(cb, b)
    out.append(src[pos:b])
    return "".join(out)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _line_text(src: str, lx: _Lex, offset: int) -> str:
    a = lx.line_start(offset)
    b = src.find("\n", a)
    return _uncomment(src, lx, a, len(src) if b < 0 else b).strip()


def _doc_start(src: str, lx: _Lex, kw_start: int) -> int:
    start = kw_start
    ls = lx.line_start(kw_start)
    if src[ls:kw_start].strip():
        return start
    ends = [b for _, b in lx.comments]
    k = bisect.bisect_right(ends, ls) - 1
    while k >= 0:
        ca, cb = lx.comments[k]
        gap = src[cb:ls]
        if gap.count("\n") != 1 or gap.strip():
            break
        cls = lx.line_start(ca)
        if src[cls:ca].strip():
            break
        start, ls = ca, cls
        k -= 1
    return start


def _receiver_name(lx: _Lex, k: int) -> str:
    toks = lx.toks
    close = _close(lx, k)
    name = ""
    j = k + 1
    while j < close:
        t = toks[j]
        if _is(toks, j, "["):
            j = _close(lx, j) + 1
            continue
        if t.kind == "ident":
            name = t.text
        j += 1
    return name


def _parse_func(src: str, lx: _Lex, i: int, out: _Parsed) -> int:
    toks = lx.toks
    n = len(toks)
    kw = toks[i]
    j = i + 1
    kind, recv = "func", ""
    if _is(toks, j, "("):
        kind, recv = "method", _receiver_name(lx, j)
        j = _close(lx, j) + 1
    if j >= n or toks[j].kind != "ident":
        return _skip_stmt(lx, i)
    name = toks[j].text
    j += 1
    type_params = _is(toks, j, "[")
    if type_params:
        j = _close(lx, j) + 1
    params = None
    if _is(toks, j, "("):
        params = (j, _close(lx, j))
        j = params[1] + 1
    result_start = j
    body = None
    while j < n:
        t = toks[j]
        if t.kind == "op":
            if t.text == "{":
                body = (j, _close(lx, j))
                break
            if t.text == ";":
                break
            if t.text in ("(", "["):
                j = _close(lx, j) + 1
                continue
        elif t.kind == "kw" and t.text in ("interface", "struct") and _is(toks, j + 1, "{"):
            j = _close(lx, j + 1) + 1
            continue
        j += 1
    result = min(j, n) > result_start
    if body:
        end = toks[body[1]].end
        sig_end = toks[body[0]].start
        nxt = body[1] + 1
    else:
        end = toks[min(j, n) - 1].end
        sig_end = end
        nxt = j
    decl = Decl(
        kind=kind,
        name=name,
        receiver=recv,
        start=_doc_start(src, lx, kw.start),
        end=end,
        line=lx.line(kw.start),
        end_line=lx.line(max(end - 1, kw.start)),
        signature=_collapse(_uncomment(src, lx, kw.start, sig_end)),
    )
    out.decls.append(decl)
    out.funcs.append(_Func(decl, params, body, type_params, result))
    return nxt


def _specs(lx: _Lex, a: int, b: int) -> list[tuple[int, int]]:
    """token ranges [s, e) of the ';'-separated specs in tokens a..b."""
    toks = lx.toks
    specs = []
    k = a
    while k < b:
        if _is(toks, k, ";"):
            k += 1
            continue
        e = min(_skip_stmt(lx, k, b), b)
        if e > k:
            specs.append((k, e))
        k = e
    return specs


def _type_signature(src: str, lx: _Lex, a: int, b: int, first_line: int) -> str:
    toks = lx.toks
    s, e = toks[a].start, toks[b - 1].end
    if lx.line(max(e - 1, s)) - first_line + 1 <= 3:
        return "type " + _collapse(_uncomment(src, lx, s, e))
    k = a
    while k < b:
        if _is(toks, k, "{"):
            return "type " + _collapse(_uncomment(src, lx, s, toks[k].start))
        if toks[k].kind == "op" and toks[k].text in ("(", "["):
            k = _close(lx, k) + 1
            continue
        k += 1
    return "type " + _collapse(_uncomment(src, lx, s, e))


def _parse_group(src: str, lx: _Lex, i: int, out: _Parsed) -> int:
    toks = lx.toks
    kw = toks[i]
    kind = kw.text
    start = _doc_start(src, lx, kw.start)
    j = i + 1
    grouped = _is(toks, j, "(")
    if grouped:
        close = _close(lx, j)
        specs = _specs(lx, j + 1, close)
        end = toks[close].end
        nxt = close + 1
    else:
        e = _skip_stmt(lx, j)
        specs = [(j, e)] if e > j else []
        end = toks[e - 1].end if e > j else kw.end
        nxt = e
    end_line = lx.line(max(end - 1, kw.start))
    for a, b in specs:
        first = toks[a]
        if first.kind != "ident":
            continue
        line = lx.line(first.start) if grouped else lx.line(kw.start)
        if kind == "type":
            names = [first]
            sig = _type_signature(src, lx, a, b, line)
            out.type_specs.setdefault(first.text, (first.start, toks[b - 1].end))
        else:
            names = [first]
            k = a + 1
            while _is(toks, k, ",") and k + 1 < b and toks[k + 1].kind == "ident":
                names.append(toks[k + 1])
                k += 2
            text = _line_text(src, lx, first.start if grouped else kw.start)
            sig = f"{kind} {text}" if grouped else text
        for t in names:
            if t.text != "_":
                ln = lx.line(t.start) if grouped else line
                out.decls.append(Decl(kind, t.text, "", start, end, ln, end_line, sig))
    return nxt


def _parse_import(src: str, lx: _Lex, i: int, out: _Parsed) -> int:
    toks = lx.toks
    kw = toks[i]
    j = i + 1
    if _is(toks, j, "("):
        close = _close(lx, j)
        specs = _specs(lx, j + 1, close)
        decl = _ImportDecl(kw.start, toks[close].end, toks[j].start, toks[close].start, [])
        nxt = close + 1
    else:
        e = _skip_stmt(lx, j)
        specs = [(j, e)] if e > j else []
        decl = _ImportDecl(kw.start, toks[e - 1].end if e > j else kw.end, None, None, [])
        nxt = e
    for a, b in specs:
        k = a
        alias = None
        if toks[k].kind == "ident" or _is(toks, k, "."):
            alias = toks[k].text
            k += 1
        if k >= b or toks[k].kind != "str":
            continue
        raw = toks[k].text
        path = raw[1:-1] if toks[k].start not in lx.open_strs else raw[1:]
        imp = Import(path, alias, lx.line(toks[a].start))
        decl.specs.append((imp, toks[a].start, toks[b - 1].end))
    out.imports.append(decl)
    return nxt


def _parse(src: str) -> _Parsed:
    lx = _lex(src)
    toks = lx.toks
    out = _Parsed(None, [], [], [], {})
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "op" and t.text == ";":
            i += 1
            continue
        if t.kind == "kw":
            if t.text == "package" and i + 1 < n and toks[i + 1].kind == "ident":
                if out.package is None:
                    out.package = (t.start, toks[i + 1].end, toks[i + 1].text)
                i += 2
                continue
            if t.text == "import":
                i = _parse_import(src, lx, i, out)
                continue
            if t.text == "func":
                i = _parse_func(src, lx, i, out)
                continue
            if t.text in ("type", "var", "const"):
                i = _parse_group(src, lx, i, out)
                continue
        i = _skip_stmt(lx, i)
    return out


def top_level_decls(src: str) -> list[Decl]:
    """top-level func/method/type/var/const declarations in source order."""
    return _parse(src).decls


def package_name(src: str) -> str | None:
    pkg = _parse(src).package
    return pkg[2] if pkg else None


def imports(src: str) -> list[Import]:
    return [imp for d in _parse(src).imports for imp, _, _ in d.specs]


def _line_extent(src: str, lx: _Lex, a: int, b: int) -> tuple[int, int]:
    """widen [a, b) to whole lines when it owns them (trailing line comment and newline included)."""
    ls = lx.line_start(a)
    own = not src[ls:a].strip()
    if own:
        a = ls
    n = len(src)
    while b < n and src[b] in " \t\r":
        b += 1
    if own:
        if src.startswith("//", b):
            nl = src.find("\n", b)
            b = n if nl < 0 else nl
        return a, b + 1 if b < n and src[b] == "\n" else b
    if b < n and src[b] == ";":
        b += 1
        while b < n and src[b] in " \t":
            b += 1
    return a, b


def _collapse_at(s: str, p: int) -> str:
    left = p
    while left > 0 and s[left - 1] == "\n":
        left -= 1
    right = p
    while right < len(s) and s[right] == "\n":
        right += 1
    run = right - left
    if left == 0:
        keep = 0
    elif right == len(s):
        keep = min(run, 1)
    else:
        keep = min(run, 2)
    if keep == run:
        return s
    return s[:left] + "\n" * keep + s[right:]


def _apply_cuts(src: str, cuts: list[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for a, b in sorted(cuts):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in reversed(merged):
        src = _collapse_at(src[:a] + src[b:], a)
    return src


def remove_imports(src: str, paths: set[str]) -> str:
    """drop the import specs with those paths; an emptied import declaration is removed."""
    lx = _lex(src)
    cuts = []
    for d in _parse(src).imports:
        gone = [s for s in d.specs if s[0].path in paths]
        if not gone:
            continue
        if len(gone) == len(d.specs):
            cuts.append(_line_extent(src, lx, d.start, d.end))
        else:
            cuts.extend(_line_extent(src, lx, a, b) for _, a, b in gone)
    if not cuts:
        return src
    src = _apply_cuts(src, cuts)
    for d in reversed(_parse(src).imports):
        if d.lparen is None or d.rparen is None:
            continue
        inner = src[d.lparen + 1 : d.rparen]
        trimmed = _EDGE_BLANKS_RE.sub("\n", inner)
        trimmed = _TAIL_BLANKS_RE.sub(r"\n\1", trimmed)
        if trimmed != inner:
            src = src[: d.lparen + 1] + trimmed + src[d.rparen :]
    return src


def _spec_text(imp: Import) -> str:
    return f'{imp.alias} "{imp.path}"' if imp.alias else f'"{imp.path}"'


def _is_std(path: str) -> bool:
    return "." not in path.split("/", 1)[0]


def _insert_into_group(src: str, lx: _Lex, d: _ImportDecl, new: list[Import]) -> str:
    """insert specs into a parenthesized import block, keeping each run sorted like gofmt."""
    rp = d.rparen
    assert rp is not None
    own = not src[lx.line_start(rp) : rp].strip() and all(
        not src[lx.line_start(a) : a].strip() for _, a, _ in d.specs
    )
    if not own:
        return src[:rp] + "".join(f"\n\t{_spec_text(i)}" for i in new) + "\n" + src[rp:]
    runs: list[list[tuple[Import, int, int]]] = []
    prev = None
    for spec in d.specs:
        if prev is None or re.search(r"\n[ \t]*\n", src[prev : spec[1]]):
            runs.append([])
        runs[-1].append(spec)
        prev = spec[2]
    inserts: list[tuple[int, str, str]] = []
    for imp in new:
        if not runs:
            pos = lx.line_start(rp)
        else:
            run = runs[0] if _is_std(imp.path) else runs[-1]
            after = next((a for i, a, _ in run if i.path > imp.path), None)
            if after is not None:
                pos = lx.line_start(after)
            else:
                nl = src.find("\n", run[-1][2])
                pos = len(src) if nl < 0 else nl + 1
        inserts.append((pos, imp.path, f"\t{_spec_text(imp)}\n"))
    for pos, _, text in sorted(inserts, reverse=True):
        src = src[:pos] + text + src[pos:]
    return src


def _has_c(d: _ImportDecl) -> bool:
    return any(imp.path == "C" for imp, _, _ in d.specs)


def add_imports(src: str, specs: list[Import]) -> str:
    """add import specs to the first import declaration, or create one after the package clause.

    a declaration holding the cgo `import "C"` is never touched: its preamble only works
    while "C" stands alone, so the specs then go to another declaration or a new one.
    """
    parsed = _parse(src)
    have = {imp.path for d in parsed.imports for imp, _, _ in d.specs}
    new: list[Import] = []
    for imp in specs:
        if imp.path not in have:
            have.add(imp.path)
            new.append(imp)
    if not new:
        return src
    new.sort(key=lambda i: i.path)
    plain = [d for d in parsed.imports if not _has_c(d)]
    if plain:
        d = plain[0]
        if d.rparen is not None:
            return _insert_into_group(src, _lex(src), d, new)
        items = [(imp, src[a:b]) for imp, a, b in d.specs] + [(i, _spec_text(i)) for i in new]
        items.sort(key=lambda x: x[0].path)
        block = "import (\n" + "".join(f"\t{t}\n" for _, t in items) + ")"
        return src[: d.start] + block + src[d.end :]
    if len(new) == 1:
        block = f"import {_spec_text(new[0])}"
    else:
        block = "import (\n" + "".join(f"\t{_spec_text(i)}\n" for i in new) + ")"
    if parsed.imports:
        end = parsed.imports[-1].end
        return src[:end] + "\n\n" + block + src[end:]
    if parsed.package is None:
        return block + "\n\n" + src
    eol = src.find("\n", parsed.package[1])
    if eol < 0:
        return src + "\n\n" + block + "\n"
    rest = src[eol:]
    if not rest.startswith("\n\n") and rest.strip():
        rest = "\n" + rest
    return src[:eol] + "\n\n" + block + rest


_CONSTRAINT_RE = re.compile(r"^//(?:go:build\s|\s*\+build\s).*$", re.MULTILINE)


def cgo_header(src: str) -> str:
    """build constraints plus the cgo preamble and its `import "C"`, verbatim ("" without cgo)."""
    parsed = _parse(src)
    decl = next((d for d in parsed.imports if _has_c(d)), None)
    if decl is None or len(decl.specs) != 1:
        return ""
    lx = _lex(src)
    pkg_start = parsed.package[0] if parsed.package else 0
    constraints = _CONSTRAINT_RE.findall(src[:pkg_start])
    block = src[_doc_start(src, lx, decl.start) : decl.end]
    return "\n".join(constraints) + "\n\n" + block if constraints else block


def _split_header(header: str) -> tuple[list[str], str]:
    lines = header.split("\n")
    k = 0
    while k < len(lines) and _CONSTRAINT_RE.match(lines[k]):
        k += 1
    if k and k < len(lines) and not lines[k].strip():
        return lines[:k], "\n".join(lines[k + 1 :])
    return [], header


def apply_cgo_header(code: str, header: str) -> str:
    """put the original cgo header into regenerated code, replacing any `import "C"` it has."""
    if not header.strip():
        return code
    lx = _lex(code)
    cuts = []
    for d in _parse(code).imports:
        if not _has_c(d):
            continue
        if len(d.specs) == 1:
            cuts.append(_line_extent(code, lx, _doc_start(code, lx, d.start), d.end))
        else:
            cuts.extend(_line_extent(code, lx, a, b) for imp, a, b in d.specs if imp.path == "C")
    if cuts:
        code = _apply_cuts(code, cuts)
    constraints, block = _split_header(header)
    if constraints:
        pkg = _parse(code).package
        cut = pkg[0] if pkg else 0
        head = _CONSTRAINT_RE.sub("", code[:cut]).strip()
        code = "\n".join(constraints) + "\n\n" + (head + "\n" if head else "") + code[cut:]
    pkg = _parse(code).package
    if pkg is None:
        return block + "\n\n" + code
    eol = code.find("\n", pkg[1])
    rest = code[eol:].lstrip("\n") if eol >= 0 else ""
    head = code if eol < 0 else code[:eol]
    return head + "\n\n" + block + ("\n\n" + rest if rest else "\n")


def type_definitions(src: str) -> dict[str, str]:
    """name -> full `type` spec text of each top-level type, comments and blank lines removed."""
    lx = _lex(src)
    out: dict[str, str] = {}
    for name, (a, b) in _parse(src).type_specs.items():
        lines = [ln.rstrip() for ln in _uncomment(src, lx, a, b).splitlines()]
        out[name] = "type " + "\n".join(ln for ln in lines if ln.strip())
    return out


def test_functions(src: str) -> list[str]:
    """top-level `func TestXxx(t *testing.T)` names (testing possibly aliased or dot-imported)."""
    parsed = _parse(src)
    quals: set[str] = set()
    dot = False
    for d in parsed.imports:
        for imp, _, _ in d.specs:
            if imp.path != "testing" or imp.alias == "_":
                continue
            if imp.alias == ".":
                dot = True
            else:
                quals.add(imp.alias or "testing")
    if not quals and not dot:
        return []
    lx = _lex(src)
    masked = mask(src)
    names = []
    for f in parsed.funcs:
        d = f.decl
        if d.kind != "func" or not d.name.startswith("Test") or f.params is None:
            continue
        rest = d.name[4:]
        if (rest and rest[0].islower()) or f.type_params or f.result or f.body is None:
            continue
        a = lx.toks[f.params[0]].end
        b = lx.toks[f.params[1]].start
        m = _TEST_PARAM_RE.match(_collapse(masked[a:b]))
        if not m:
            continue
        qual = m.group(1)
        if (qual is None and dot) or (qual is not None and qual in quals):
            names.append(d.name)
    return names


def function_spans(src: str) -> dict[str, tuple[int, int]]:
    """top-level non-method funcs: name -> (start incl doc comment, end)."""
    return {d.name: (d.start, d.end) for d in _parse(src).decls if d.kind == "func"}


def remove_functions(src: str, names: set[str]) -> str:
    """remove those top-level funcs (with doc comments), collapsing the blank lines left behind."""
    lx = _lex(src)
    cuts = [
        _line_extent(src, lx, d.start, d.end)
        for d in _parse(src).decls
        if d.kind == "func" and d.name in names
    ]
    return _apply_cuts(src, cuts) if cuts else src


def identifiers(src: str) -> set[str]:
    return {w for w in _IDENT_RE.findall(mask(src)) if w not in KEYWORDS}


def is_generated(src: str) -> bool:
    pkg = _parse(src).package
    head = src[: pkg[0]] if pkg else src
    return bool(_GENERATED_RE.search(head))


def extract_go_code(reply: str) -> str:
    """largest go (or untagged) fenced block of an llm reply, else the reply itself."""
    blocks = [
        m.group(2) for m in _FENCE_RE.finditer(reply) if m.group(1).lower() in ("", "go", "golang")
    ]
    if blocks:
        code = max(blocks, key=len)
    else:
        code = reply.strip()
        nl = code.find("\n")
        tag = code[3 : nl if nl >= 0 else len(code)].strip().lower()
        if code.startswith("```") and tag in ("", "go", "golang"):
            code = "" if nl < 0 else code[nl + 1 :]
            if code.rstrip().endswith("```"):
                code = code.rstrip()[:-3]
    return code.strip() + "\n"
