"""COBOL mutation operators.

sites come only from parser tokens (`cobol.parser.tokenize` over `logical_lines`), mapped
back to physical character offsets of the text, inside the PROCEDURE DIVISION code area:
never in comments, literals (except a MOVE's literal operand), EXEC blocks, COPY statements,
the other divisions or copybooks. Multi-word relational operators are one operator token.

categories: cmp (relational operator to its off-by-one or negated form), logic (AND <-> OR),
negate (NOT (condition)), arith (ADD <-> SUBTRACT, MULTIPLY <-> DIVIDE), compute (+ - * /
swapped in COMPUTE), move (a MOVE literal changed: Y <-> N, ZERO -> 1, numbers see
_move_value), perform (PERFORM <para> -> CONTINUE).
`Mutant.line` is the verb line of the enclosing statement (the line cobc -C and the trace
report). Fixed format: columns 1-6 and 73-80 of every mutated line are kept byte for byte;
code that would pass column 72 is split at a token boundary onto an added line with blank
sequence and identification areas; a site that cannot fit is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from codocs.cobol.facts import PLUMBING_PARAGRAPH
from codocs.cobol.oracle import _VERBS
from codocs.cobol.parser import Tok, tokenize
from codocs.cobol.reflow import _emit_code
from codocs.cobol.source import detect_format, logical_lines
from codocs.golang.mutate import Mutant

__all__ = ["mutation_sites", "statement_verb_lines", "STATEMENT_VERBS", "Site"]

CODE_END = 72  # last code column (1-based)
AREA = CODE_END - 7  # width of columns 8-72
_TERMINATORS = {v for v in _VERBS if v.startswith("END-")}
# statement verbs (a statement starts with one); WHEN is a statement line for cobc
STATEMENT_VERBS = frozenset(
    (_VERBS - _TERMINATORS - {"ELSE", "WHEN", "THEN"})
    | {"EXEC", "ALTER", "ENTRY", "MERGE", "RELEASE", "INITIATE", "GENERATE", "TERMINATE",
       "INVOKE", "UNLOCK", "COMMIT", "ROLLBACK", "JSON", "XML", "RAISE", "RESUME"})
_STMT_LINE_WORDS = STATEMENT_VERBS | {"WHEN"}
# words that end a condition (besides the period)
_COND_STOP = _VERBS | {"ALSO", "AFTER", "EXEC", "ALTER", "ENTRY", "VARYING"}
_REL_SYMBOLS = {">", "<", "=", ">=", "<=", "<>"}
_MOVE_FLAGS = {"'Y'": "'N'", "'N'": "'Y'", "'y'": "'n'", "'n'": "'y'",
               '"Y"': '"N"', '"N"': '"Y"'}
_ZEROS = {"ZERO", "ZEROS", "ZEROES"}
_PERFORM_LOOP = {"THRU", "THROUGH", "UNTIL", "TIMES", "VARYING", "WITH", "TEST"}
_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


@dataclass
class Site:
    """one mutation before it is placed in the text: edits (offset, end, new)."""

    operator: str
    line: int                 # statement verb line
    edits: list[tuple[int, int, str]]
    para: str = ""


@dataclass
class _Cond:
    start: int                # token index of the first condition token
    end: int                  # token index just past the last one
    verb_line: int
    negatable: bool


@dataclass
class _Ctx:
    text: str
    fixed: bool
    lines: list[str]
    starts: list[int]
    toks: list[Tok]
    offsets: list[int | None] = field(default_factory=list)


# -- token placement --------------------------------------------------------------------


def _line_starts(text: str) -> tuple[list[str], list[int]]:
    lines = text.split("\n")
    starts: list[int] = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    return lines, starts


def _offset(ctx: _Ctx, t: Tok) -> int | None:
    """physical offset of a token (None when it does not sit verbatim on its line)."""
    n = t.line - 1
    if n < 0 or n >= len(ctx.lines):
        return None
    raw = ctx.lines[n]
    if "\t" in raw:
        return None
    col = (7 + t.pos) if ctx.fixed else t.pos
    end = col + (t.end - t.pos)
    if ctx.fixed and end > CODE_END:
        return None
    if raw[col:end] != t.text:
        return None
    return ctx.starts[n] + col


def _procedure_mask(toks: list[Tok]) -> list[bool]:
    """tokens of PROCEDURE DIVISION code (headers, EXEC blocks and COPY statements out)."""
    mask = [False] * len(toks)
    inside = False
    header = False
    exec_ = False
    copy = False
    i = 0
    while i < len(toks):
        t = toks[i]
        u = t.up
        nxt = toks[i + 1].up if i + 1 < len(toks) else ""
        if u == "PROCEDURE" and nxt == "DIVISION":
            inside, header = True, True
            i += 2
            continue
        if u in ("IDENTIFICATION", "ID") and nxt == "DIVISION":
            inside = False
        if u == "END" and nxt in ("PROGRAM", "FUNCTION"):
            inside = False
        if not inside:
            i += 1
            continue
        if header:
            if t.kind == "P":
                header = False
            i += 1
            continue
        if exec_:
            if u == "END-EXEC":
                exec_ = False
            i += 1
            continue
        if u == "EXEC":
            exec_ = True
            i += 1
            continue
        if copy:
            if t.kind == "P":
                copy = False
            i += 1
            continue
        if u in ("COPY", "REPLACE") and t.kind == "W":
            copy = True
            i += 1
            continue
        mask[i] = True
        i += 1
    return mask


def statement_verb_lines(text: str, fmt: str | None = None) -> set[int]:
    """lines of PROCEDURE DIVISION statement verbs (the fallback statement universe)."""
    fmt = fmt or detect_format(text)
    toks = tokenize(logical_lines(text, fmt))
    mask = _procedure_mask(toks)
    out: set[int] = set()
    for i, t in enumerate(toks):
        if mask[i] and t.kind == "W" and t.up in STATEMENT_VERBS and _starts_statement(toks, i):
            out.add(t.line)
    return out


def _starts_statement(toks: list[Tok], i: int) -> bool:
    """a verb token that starts a statement (not a word of a phrase such as NOT AT END)."""
    if i == 0:
        return True
    prev = toks[i - 1]
    if toks[i].up in ("NEXT",) and (i + 1 >= len(toks) or toks[i + 1].up != "SENTENCE"):
        return False
    if prev.up in ("TO", "FROM", "BY", "INTO", "GIVING", "OF", "IN", "USING"):
        return False
    return True


# -- condition spans -------------------------------------------------------------------------


def _cond_end(toks: list[Tok], k: int, mask: list[bool]) -> int:
    """index just past a condition that starts at k."""
    j = k
    depth = 0
    while j < len(toks) and mask[j]:
        t = toks[j]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            if t.kind == "P":
                break
            if t.kind == "W" and t.up in _COND_STOP:
                if t.up == "NEXT" and j + 1 < len(toks) and toks[j + 1].up != "SENTENCE":
                    pass
                else:
                    break
        j += 1
    return j


def _conditions(toks: list[Tok], mask: list[bool]) -> list[_Cond]:
    """IF, PERFORM ... UNTIL, WHEN of EVALUATE TRUE and SEARCH WHEN conditions."""
    out: list[_Cond] = []
    stack: list[str] = []      # evaluate-true | evaluate | search
    last_verb_line = 0
    perform_line = 0
    for i, t in enumerate(toks):
        if not mask[i]:
            continue
        if t.kind == "P":
            stack.clear()
            continue
        u = t.up
        if t.kind != "W":
            continue
        if u in STATEMENT_VERBS and _starts_statement(toks, i):
            last_verb_line = t.line
            if u == "PERFORM":
                perform_line = t.line
        if u == "EVALUATE":
            subj = toks[i + 1].up if i + 1 < len(toks) else ""
            nxt2 = toks[i + 2].up if i + 2 < len(toks) else ""
            stack.append("evaluate-true" if subj == "TRUE" and nxt2 in ("WHEN", "ALSO")
                         else "evaluate")
        elif u == "SEARCH":
            stack.append("search")
        elif u in ("END-EVALUATE", "END-SEARCH") and stack:
            stack.pop()
        elif u == "IF" and i + 1 < len(toks):
            e = _cond_end(toks, i + 1, mask)
            if e > i + 1:
                out.append(_Cond(i + 1, e, t.line, True))
        elif u == "UNTIL" and i + 1 < len(toks):
            e = _cond_end(toks, i + 1, mask)
            if e > i + 1:
                out.append(_Cond(i + 1, e, perform_line or last_verb_line, True))
        elif u == "WHEN" and stack and stack[-1] in ("evaluate-true", "search"):
            if i + 1 < len(toks) and toks[i + 1].up == "OTHER":
                continue
            e = _cond_end(toks, i + 1, mask)
            if e > i + 1:
                out.append(_Cond(i + 1, e, t.line, stack[-1] == "evaluate-true"))
    return out


def _relops(toks: list[Tok], a: int, b: int) -> list[tuple[int, int, str]]:
    """(first token, past-last token, canonical op) of the relational operators in a span:
    multi-word forms are one operator; adjacent symbols join (> =, < >)."""
    out: list[tuple[int, int, str]] = []
    i = a

    def up(k: int) -> str:
        return toks[k].up if k < b else ""

    while i < b:
        t = toks[i]
        if t.kind == "S" and t.text in "<>=":
            j = i + 1
            sym = t.text
            if j < b and toks[j].kind == "S" and toks[j].pos == t.end and \
                    toks[j].li == t.li and sym + toks[j].text in _REL_SYMBOLS:
                sym += toks[j].text
                j += 1
            neg = i > a and up(i - 1) == "NOT"
            start = i - 1 if neg else i
            out.append((start, j, ("NOT " if neg else "") + sym))
            i = j
            continue
        if t.kind == "W" and t.up in ("GREATER", "LESS", "EQUAL"):
            neg = i > a and up(i - 1) == "NOT"
            j = i + 1
            word = t.up
            if word in ("GREATER", "LESS"):
                if up(j) == "THAN":
                    j += 1
                if up(j) == "OR" and up(j + 1) == "EQUAL":
                    j += 2
                    if up(j) == "TO":
                        j += 1
                    op = ">=" if word == "GREATER" else "<="
                else:
                    op = ">" if word == "GREATER" else "<"
            else:
                if up(j) == "TO":
                    j += 1
                op = "="
            start = i - 1 if neg else i
            out.append((start, j, ("NOT " if neg else "") + op + ":w"))
            i = j
            continue
        i += 1
    return out


_CMP = {">": ">=", ">=": ">", "<": "<=", "<=": "<", "=": "NOT =", "NOT =": "=", "<>": "=",
        ">:w": "NOT LESS THAN", "<:w": "NOT GREATER THAN", ">=:w": "GREATER THAN",
        "<=:w": "LESS THAN", "=:w": "NOT EQUAL TO", "NOT =:w": "EQUAL TO"}


# -- placing edits in the text --------------------------------------------------------------


def _open_quote(code: str) -> bool:
    quote = ""
    for c in code:
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
    return bool(quote)


def _line_of(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _glue(left: str, new: str, right: str) -> str:
    """the replacement with a blank added where it would otherwise touch the neighbouring
    text: `X='Y'` with `=` -> `NOT =` gives `X NOT ='Y'`, not `XNOT ='Y'`."""
    if new[:1].isalnum() and left and not left.isspace() and left != "(":
        new = " " + new
    if new[-1:].isalnum() and right and not right.isspace() and right not in ").":
        new += " "
    return new


def _fit(raw: str, area: str, edit_col: int) -> list[str] | None:
    """a mutated fixed-format line: cols 1-7 and 73-80 kept; code past col 72 is split at a
    token boundary (after edit_col) onto added lines with blank cols 1-6 and 73-80. a line
    that ends inside a literal continued on the next '-' line is never split: the literal
    runs to col 72, so moving it would change its value."""
    prefix, suffix = raw[:7], raw[CODE_END:]
    body = area.rstrip()
    if len(body) <= AREA:
        return [prefix + (body.ljust(AREA) + suffix if suffix else body)]
    if _open_quote(body):
        return None
    cut = -1
    for p in range(min(AREA, len(body) - 1), max(edit_col, 0), -1):
        if body[p] == " " and not _open_quote(body[:p]):
            cut = p
            break
    if cut < 0:
        return None
    first = body[:cut].rstrip()
    rest = body[cut:].strip()
    indent = len(area) - len(area.lstrip())
    cont = min(max(indent, 4), 32) + 4
    more = _emit_code(cont, rest)
    if any(len(x) > CODE_END for x in more):
        return None
    return [prefix + (first.ljust(AREA) + suffix if suffix else first)] + more


def place(ctx: _Ctx, site: Site) -> Mutant | None:
    """the Mutant of a site: offset = first edit, original = up to the end of the last
    edited physical line (fixed) or of the last edit (free)."""
    edits = sorted(site.edits)
    first = edits[0][0]
    last = edits[-1][1]
    if not ctx.fixed:
        original = ctx.text[first:last]
        out = []
        pos = first
        for a, b, new in edits:
            out.append(ctx.text[pos:a])
            out.append(_glue(ctx.text[a - 1:a], new, ctx.text[b:b + 1]))
            pos = b
        out.append(ctx.text[pos:last])
        return Mutant(id=f"m{first}", offset=first, line=site.line, operator=site.operator,
                      original=original, replacement="".join(out))
    la = _line_of(ctx.starts, first)
    lb = _line_of(ctx.starts, last)
    new_lines: list[str] = []
    for n in range(la, lb + 1):
        raw = ctx.lines[n]
        base = ctx.starts[n]
        mine = [(a - base - 7, b - base - 7, new) for a, b, new in edits
                if base <= a <= base + len(raw)]
        if not mine:
            new_lines.append(raw)
            continue
        area = raw[7:CODE_END]
        pieces = []
        pos = 0
        for a, b, new in mine:
            pieces.append(area[pos:a])
            pieces.append(_glue(area[a - 1:a] if a > 0 else " ", new, area[b:b + 1]))
            pos = b
        pieces.append(area[pos:])
        if _open_quote(area):
            # the line's last literal continues on the next line and runs to col 72: the
            # edit must keep its columns (pad a shorter replacement, drop a longer one)
            grow = sum(len(x) for x in pieces) - len(area)
            if grow > 0:
                return None
            pieces[-2] += " " * -grow
        fitted = _fit(raw, "".join(pieces), mine[0][0])
        if fitted is None:
            return None
        new_lines.extend(fitted)
    end = ctx.starts[lb] + len(ctx.lines[lb])
    head = first - ctx.starts[la]
    joined = "\n".join(new_lines)
    if joined[:head] != ctx.lines[la][:head]:
        return None
    return Mutant(id=f"m{first}", offset=first, line=site.line, operator=site.operator,
                  original=ctx.text[first:end], replacement=joined[head:])


# -- the operators -------------------------------------------------------------------------


def _num(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _conditions_of(program) -> dict[str, list[tuple[str, str]]]:
    """data item -> its 88-level (condition name, first VALUE) in declaration order."""
    out: dict[str, list[tuple[str, str]]] = {}
    if program is None:
        return out
    for item in program.data_items:
        owner = item.name.upper()
        level = item.level
        for line in item.children:
            parts = line.split()
            if len(parts) < 2:
                continue
            if parts[0] == "88":
                m = re.search(r"\bVALUE\s+(.+)$", line)
                if m:
                    out.setdefault(owner, []).append((parts[1].upper(), m.group(1).strip()))
                continue
            if parts[0].isdigit() and int(parts[0]) > level:
                owner = parts[1].upper()
    return out


def _move_value(lit: str, field_name: str, conds: dict[str, list[tuple[str, str]]],
                compared: set[str]) -> str | None:
    """the replacement of a numeric MOVE literal: value + 1 when the target has no 88-levels
    or is compared directly, else the first 88 VALUE that sets a different set of condition
    names (None when there is none)."""
    n = _num(lit)
    if n is None:
        return None
    levels = conds.get(field_name.upper(), [])
    numeric = [(name, _num(v)) for name, v in levels if _num(v) is not None]
    if not levels or field_name.upper() in compared:
        if n == n.to_integral_value():
            return str(int(n) + 1)
        return str(n + 1)

    def sat(x: Decimal) -> set[str]:
        return {name for name, v in numeric if v == x}

    want = sat(n)
    for _name, v in numeric:
        if sat(v) != want:
            return str(int(v)) if v == v.to_integral_value() else str(v)
    return None


def _negated_subject(toks: list[Tok], a: int, lo: int) -> bool:
    """the operand left of a relational operator at a is preceded by NOT."""
    k = a - 1
    if k >= lo and toks[k].text == ")":
        depth = 0
        while k >= lo:
            if toks[k].text == ")":
                depth += 1
            elif toks[k].text == "(":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        k -= 1                              # the subscripted name or function
    k -= 1                                  # the token before the operand
    return k >= lo and toks[k].up == "NOT"


def _statement_end(toks: list[Tok], k: int, mask: list[bool]) -> int:
    j = k + 1
    while j < len(toks) and mask[j]:
        t = toks[j]
        if t.kind == "P":
            return j
        if t.kind == "W" and t.up in _VERBS and _starts_statement(toks, j) and t.up != "NOT":
            return j
        j += 1
    return j


def _sites(ctx: _Ctx, program=None) -> list[Site]:
    toks = ctx.toks
    mask = _procedure_mask(toks)
    off = ctx.offsets

    def at(k: int) -> int | None:
        return off[k] if 0 <= k < len(off) else None

    def span_end(k: int) -> int | None:
        o = at(k)
        return None if o is None else o + len(toks[k].text)

    sites: list[Site] = []
    conds = _conditions(toks, mask)
    in_cond: set[int] = set()
    compared: set[str] = set()
    for c in conds:
        in_cond.update(range(c.start, c.end))
        for k in range(c.start, c.end):
            if toks[k].kind == "W":
                compared.add(toks[k].up)
        # cmp
        rel_tokens: set[int] = set()
        for a, b, op in _relops(toks, c.start, c.end):
            rel_tokens.update(range(a, b))
            rep = _CMP.get(op)
            o1, o2 = at(a), span_end(b - 1)
            if rep is None or o1 is None or o2 is None:
                continue
            if toks[a].line != toks[b - 1].line:
                continue
            if rep.startswith("NOT") and _negated_subject(toks, a, c.start):
                continue        # NOT X NOT = 1 does not compile
            sites.append(Site("cmp", c.verb_line, [(o1, o2, rep)]))
        # logic
        for k in range(c.start, c.end):
            t = toks[k]
            if t.kind == "W" and t.up in ("AND", "OR") and k not in rel_tokens:
                o = at(k)
                if o is not None:
                    sites.append(Site("logic", c.verb_line,
                                      [(o, o + len(t.text), "OR" if t.up == "AND" else "AND")]))
        # negate
        if c.negatable:
            o1, o2 = at(c.start), span_end(c.end - 1)
            if o1 is not None and o2 is not None:
                sites.append(Site("negate", c.verb_line, [(o1, o1, "NOT ("), (o2, o2, ")")]))
    # EVALUATE subjects count as compared items
    for i, t in enumerate(toks):
        if mask[i] and t.up == "EVALUATE" and i + 1 < len(toks) and toks[i + 1].kind == "W":
            compared.add(toks[i + 1].up)
    levels = _conditions_of(program)

    for i, t in enumerate(toks):
        if not mask[i] or t.kind != "W" or i in in_cond or not _starts_statement(toks, i):
            continue
        u = t.up
        end = _statement_end(toks, i, mask)
        words = [x.up for x in toks[i:end] if x.kind == "W"]
        o = at(i)
        if o is None:
            continue
        if u in ("ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"):
            if "REMAINDER" in words and u in ("DIVIDE", "MULTIPLY"):
                continue
            prep = {"ADD": "TO", "SUBTRACT": "FROM", "MULTIPLY": "BY", "DIVIDE": None}[u]
            k = None
            if u == "DIVIDE":
                for j in range(i + 1, end):
                    if toks[j].up in ("INTO", "BY") and toks[j].kind == "W":
                        k = j
                        break
            else:
                k = next((j for j in range(i + 1, end) if toks[j].up == prep
                          and toks[j].kind == "W"), None)
            if k is None:
                continue
            ok = at(k)
            if ok is None:
                continue
            new_verb, new_prep = {
                ("ADD", "TO"): ("SUBTRACT", "FROM"), ("SUBTRACT", "FROM"): ("ADD", "TO"),
                ("MULTIPLY", "BY"): ("DIVIDE", "INTO"), ("DIVIDE", "INTO"): ("MULTIPLY", "BY"),
                ("DIVIDE", "BY"): ("MULTIPLY", "BY"),
            }[(u, toks[k].up)]
            sites.append(Site("arith", t.line, [(o, o + len(t.text), new_verb),
                                                (ok, ok + len(toks[k].text), new_prep)]))
        elif u == "COMPUTE":
            eq = next((j for j in range(i + 1, end) if toks[j].text == "="), None)
            if eq is None:
                continue
            for j in range(eq + 1, end):
                s = toks[j]
                if s.kind != "S" or s.text not in "+-*/":
                    continue
                prev, nxt = toks[j - 1], toks[j + 1] if j + 1 < end else None
                if s.text == "*" and ((nxt is not None and nxt.text == "*" and nxt.pos == s.end)
                                      or (prev.text == "*" and prev.end == s.pos)):
                    continue
                if not (prev.kind in ("W", "L") or prev.text == ")"):
                    continue
                if prev.kind == "W" and prev.up in ("ROUNDED", "ON", "SIZE", "ERROR"):
                    continue
                os_ = at(j)
                if os_ is None:
                    continue
                swap = {"+": "-", "-": "+", "*": "/", "/": "*"}[s.text]
                sites.append(Site("compute", t.line, [(os_, os_ + 1, swap)]))
        elif u == "MOVE" and i + 2 < end:
            lit = toks[i + 1]
            if toks[i + 2].up != "TO":
                continue
            ol = at(i + 1)
            if ol is None:
                continue
            target = toks[i + 3].up if i + 3 < end and toks[i + 3].kind == "W" else ""
            rep = None
            if lit.kind == "L" and lit.text in _MOVE_FLAGS:
                rep = _MOVE_FLAGS[lit.text]
            elif lit.kind == "W" and lit.up in _ZEROS:
                rep = "1"
            elif lit.kind == "W" and _NUM_RE.match(lit.text):
                rep = _move_value(lit.text, target, levels, compared)
            if rep is not None and rep != lit.text:
                sites.append(Site("move", t.line, [(ol, ol + len(lit.text), rep)]))
        elif u == "PERFORM" and i + 1 < len(toks):
            para = toks[i + 1]
            if para.kind != "W" or para.up in STATEMENT_VERBS or para.up in _PERFORM_LOOP \
                    or para.up.startswith("END-"):
                continue
            nxt = toks[i + 2] if i + 2 < len(toks) else None
            if nxt is not None and nxt.kind == "W" and (nxt.up in _PERFORM_LOOP
                                                       or nxt.up not in _VERBS):
                continue
            if nxt is not None and nxt.kind not in ("W", "P"):
                continue
            if _NUM_RE.match(para.text):
                continue
            o2 = span_end(i + 1)
            if o2 is None:
                continue
            sites.append(Site("perform", t.line, [(o, o2, "CONTINUE")]))
    return sites


def _paragraph_of(program, line: int) -> str:
    name = ""
    for p in getattr(program, "paragraphs", None) or []:
        if p.name != "(entry)" and p.line <= line:
            name = p.name
    return name


def mutation_sites(text: str, *, fmt: str | None = None, program=None,
                   universe: set[int] | None = None, skip_plumbing: bool = True
                   ) -> list[Mutant]:
    """the COBOL mutants of a program file, in source order.

    program: its parsed CobolProgram (88-levels of MOVE targets, paragraph names); universe:
    statement lines to keep (cobc's statement markers); skip_plumbing: drop sites inside
    plumbing paragraphs (codocs.cobol.facts.PLUMBING_PARAGRAPH)."""
    fmt = fmt or detect_format(text)
    lines, starts = _line_starts(text)
    toks = tokenize(logical_lines(text, fmt))
    ctx = _Ctx(text=text, fixed=fmt == "fixed", lines=lines, starts=starts, toks=toks)
    ctx.offsets = [_offset(ctx, t) for t in toks]
    out: list[Mutant] = []
    seen: set[str] = set()
    progs = program if isinstance(program, list) else [program] if program else []
    for site in _sites(ctx, progs[0] if progs else None):
        if universe is not None and site.line not in universe:
            continue
        if skip_plumbing and progs:
            para = _paragraph_of(progs[0], site.line)
            if para and PLUMBING_PARAGRAPH.search(para):
                continue
        m = place(ctx, site)
        if m is None or m.id in seen:
            continue
        seen.add(m.id)
        out.append(m)
    return sorted(out, key=lambda m: m.offset)
