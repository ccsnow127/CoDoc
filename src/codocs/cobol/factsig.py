"""the facts signal of docgen: do the static facts of the regenerated program agree with the
original's?

`facts(rel, text, index)` parses a program file with the parser that computed the originals
(copybooks resolved for the original program) and emits fact ids per top-level program.
Structural classes (id, entry, using, file, copy, data, para) are carried verbatim by the
doc format and form a floor; the scored classes (perform, call, sql, cics, xfer, resp,
cond, when, move, text) depend on the prose. phi = J(scored) x J(structural).

case rules: the <PID> qualifier is the upper-cased program key; the linking names (id,
entry, call and xfer targets) keep the spelling as written (GnuCOBOL resolves CALL literals
case-sensitively); COPY members and data names are upper-cased.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from codocs.cobol.facts import program_span
from codocs.cobol.layout import exact_program_name, file_records, record_fields
from codocs.cobol.models import CobolProgram
from codocs.cobol.mutate import _conditions, _relops
from codocs.cobol.oracle import _VERBS
from codocs.cobol.parser import Tok, literal_value, parse_program_file, tokenize
from codocs.cobol.source import detect_format, logical_lines

__all__ = ["facts", "fact_class", "split", "evaluate", "readable", "STRUCTURAL", "SCORED",
           "CLASSES"]

STRUCTURAL = ("id", "entry", "using", "file", "copy", "data", "para")
SCORED = ("perform", "call", "sql", "cics", "xfer", "resp", "cond", "when", "move", "text")
CLASSES = STRUCTURAL + SCORED
_PREFIX_CLASS = {"id": "id", "kind": "id", "entry": "entry", "using": "using",
                 "returning": "using", "file": "file", "fd": "file", "copy": "copy",
                 "data": "data", "para": "para", "perform": "perform", "call": "call",
                 "sql": "sql", "sqlw": "sql", "cics": "cics", "xfer": "xfer", "resp": "resp",
                 "cond": "cond", "when": "when", "move": "move", "text": "text"}
_STATIC_KINDS = ("call", "cics-link", "cics-xctl", "cics-start", "function")
_FIGURATIVE = {"ZERO": "ZERO", "ZEROS": "ZERO", "ZEROES": "ZERO", "SPACE": "SPACE",
               "SPACES": "SPACE", "LOW-VALUE": "LOW-VALUE", "LOW-VALUES": "LOW-VALUE",
               "HIGH-VALUE": "HIGH-VALUE", "HIGH-VALUES": "HIGH-VALUE", "QUOTE": "QUOTE",
               "QUOTES": "QUOTE", "NULL": "NULL", "NULLS": "NULL"}
_OPS = {"=": "=", "NOT =": "<>", "<>": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">=",
        "NOT <": ">=", "NOT >": "<=", "NOT <=": ">", "NOT >=": "<"}
_SQL_OPS = {"=", "<", ">", "LIKE", "IN", "BETWEEN", "IS", "!"}
_TRIVIAL = {"EXIT", "PARAGRAPH", "SECTION", "CONTINUE"}


def fact_class(fact: str) -> str:
    """the class of a fact id (an `absent:` prefix is ignored)."""
    f = fact.removeprefix("absent:")
    return _PREFIX_CLASS.get(f.split(":", 1)[0], "other")


def split(ids: set[str]) -> tuple[set[str], set[str]]:
    """(structural, scored) facts."""
    s = {f for f in ids if fact_class(f) in STRUCTURAL}
    return s, ids - s


def _norm(text: str) -> str:
    return " ".join(text.split())


def _num(text: str) -> str | None:
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    return format(d.normalize(), "f") if d != d.to_integral_value() else str(int(d))


def _tok_text(t: Tok) -> str:
    if t.kind == "L":
        return t.text
    if t.kind == "W":
        n = _num(t.text) if t.text[:1].isdigit() else None
        return n if n is not None else _FIGURATIVE.get(t.up, t.up)
    return t.text


def _join(toks: list[Tok]) -> str:
    out = ""
    for t in toks:
        text = _tok_text(t)
        if out and not out.endswith("(") and text not in (")", "(", ",", ":"):
            out += " "
        elif out and text == "(" and not out[-1].isalnum() and out[-1] not in "-_)":
            out += " "
        out += text
    return _norm(out)


def _operand(toks: list[Tok], k: int, lo: int, hi: int, step: int) -> tuple[list[Tok], int]:
    """the operand tokens next to a relational operator (subscripts and function arguments
    kept whole), walking from k by step (-1: left, 1: right) inside [lo, hi)."""
    out: list[Tok] = []
    depth = 0
    while lo <= k < hi:
        t = toks[k]
        opening, closing = ("(", ")") if step > 0 else (")", "(")
        if t.text == opening:
            depth += 1
        elif t.text == closing:
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and t.up in ("AND", "OR", "NOT"):
            break
        out.append(t)
        k += step
    if step < 0:
        out.reverse()
    return out, k


def _lookup(index, rel: str):
    def lookup(member: str) -> str | None:
        if index is None:
            return None
        path = index.resolve_copybook(member, rel)
        if path is None:
            return None
        texts = getattr(index, "_texts", {})
        if path in texts:
            return texts[path]
        return index.copybook_source(member)
    return lookup


def _dialect(fmt: str) -> str:
    return "ibm" if fmt == "fixed" else "default"


def _size(item, dialect: str) -> int:
    try:
        return record_fields(item, dialect)[0]
    except Exception:
        return 0


class _Prog:
    """the tokens of one top-level program and its paragraph map."""

    def __init__(self, prog: CobolProgram, toks: list[Tok]):
        self.prog = prog
        self.toks = toks
        heads = [(p.line, p.name) for p in prog.paragraphs if p.name != "(entry)"]
        self.heads = sorted(heads)
        self.proc, self.exec_ = _masks(toks)

    def para(self, line: int) -> str:
        name = "(entry)"
        for ln, n in self.heads:
            if ln <= line:
                name = n
            else:
                break
        return name


def _masks(toks: list[Tok]) -> tuple[list[bool], list[bool]]:
    """(procedure code outside EXEC, inside an EXEC block) per token."""
    proc = [False] * len(toks)
    exe = [False] * len(toks)
    inside = header = in_exec = False
    for i, t in enumerate(toks):
        nxt = toks[i + 1].up if i + 1 < len(toks) else ""
        if t.up == "PROCEDURE" and nxt == "DIVISION":
            inside, header = True, True
            continue
        if t.up in ("IDENTIFICATION", "ID") and nxt == "DIVISION":
            inside = False
        if not inside:
            continue
        if header:
            if t.kind == "P":
                header = False
            continue
        if in_exec:
            exe[i] = True
            if t.up == "END-EXEC":
                in_exec = False
            continue
        if t.up == "EXEC":
            in_exec = True
            exe[i] = True
            continue
        proc[i] = True
    return proc, exe


def _spelled(toks: list[Tok]) -> dict[str, str]:
    """upper-cased literal value / word -> its first spelling as written."""
    out: dict[str, str] = {}
    for t in toks:
        if t.kind == "L":
            v = literal_value(t.text)
            out.setdefault(v.upper(), v)
        elif t.kind == "W":
            out.setdefault(t.up, t.text)
    return out


def _structural(p: _Prog, text: str, fmt: str) -> set[str]:
    prog = p.prog
    pid = prog.program_id.upper()
    dialect = _dialect(fmt)
    out = {f"id:{exact_program_name(text, prog.program_id)}",
           f"kind:{pid}:{'function' if prog.unit_kind == 'function' else 'program'}"}
    spelled = _spelled(p.toks)
    for e in prog.entry_points:
        out.add(f"entry:{pid}:{spelled.get(e.upper(), e)}")
    items = {d.name.upper(): d for d in prog.data_items}
    for i, name in enumerate(prog.using_params, 1):
        item = items.get(name.upper())
        out.add(f"using:{pid}:{i}:{_size(item, dialect) if item else 0}")
    if prog.returning:
        item = items.get(prog.returning.upper())
        out.add(f"returning:{pid}:{_size(item, dialect) if item else 0}")
    for f in prog.files:
        dd = (f.assign if f.via == "native" and f.assign else f.name).strip("'\"").upper()
        modes = ",".join(sorted({m.upper() for m in f.modes}))
        out.add(f"file:{pid}:{dd}:{(f.organization or 'SEQUENTIAL').upper()}:"
                f"{(f.access or 'SEQUENTIAL').upper()}:{modes}")
        if f.via == "native":
            recs = file_records(prog, f, dialect)
            if recs:
                out.add(f"fd:{pid}:{dd}:{_size(recs[0], dialect)}")
    for member in prog.copybooks:
        out.add(f"copy:{pid}:{member.upper()}")
    for d in prog.data_items:
        if d.copybook or d.name.upper() == "FILLER" or d.level not in (1, 77):
            continue
        out.add(f"data:{pid}:{d.section}:{d.name.upper()}:{_size(d, dialect)}")
    for name in _nontrivial(p):
        out.add(f"para:{pid}:{name}")
    return out


def _nontrivial(p: _Prog) -> list[str]:
    """paragraph names whose body has a statement other than EXIT / CONTINUE."""
    lines = sorted(p.heads)
    out: list[str] = []
    for k, (ln, name) in enumerate(lines):
        nxt = lines[k + 1][0] if k + 1 < len(lines) else None
        body = [t for i, t in enumerate(p.toks)
                if (p.proc[i] or p.exec_[i]) and t.line >= ln and (nxt is None or t.line < nxt)
                and not (t.line == ln and t.up in (name, "SECTION")) and t.kind != "P"]
        if any(t.up not in _TRIVIAL for t in body):
            out.append(name)
    return out


def _conds_88(prog: CobolProgram) -> set[str]:
    out: set[str] = set()
    for d in prog.data_items:
        for line in d.children:
            parts = line.split()
            if len(parts) > 1 and parts[0] == "88":
                out.add(parts[1].upper())
    return out


def _scored(p: _Prog, index) -> set[str]:
    prog = p.prog
    pid = prog.program_id.upper()
    toks = p.toks
    out: set[str] = set()
    spelled = _spelled(toks)
    for para in prog.paragraphs:
        for target in para.performs:
            out.add(f"perform:{pid}:{para.name}->{target}")
    for c in prog.calls:
        if c.kind in _STATIC_KINDS and not c.via:
            out.add(f"call:{pid}:{c.kind}:{spelled.get(c.target.upper(), c.target)}")
        elif c.kind == "call-dynamic" or c.via:
            out.add(f"call:{pid}:dynamic")
    for s in prog.sql:
        for t in s.tables:
            out.add(f"sql:{pid}:{s.op}:{t.upper()}")
    for cu in prog.cics:
        kv = ",".join(f"{k}={'|'.join(v)}" for k, v in sorted(cu.resolved.items()) if v)
        out.add(f"cics:{pid}:{cu.command}:{kv}")
    out |= _exec_facts(p, pid, spelled)
    conds88 = _conds_88(prog)
    mask = [p.proc[i] for i in range(len(toks))]
    for c in _conditions(toks, mask):
        para = p.para(toks[c.start].line)
        rels = _relops(toks, c.start, c.end)
        used: set[int] = set()
        for a, b, op in rels:
            used.update(range(a, b))
            lhs, _ = _operand(toks, a - 1, c.start, c.end, -1)
            lhs = [t for t in lhs if t.up != "IS"]
            rhs, _ = _operand(toks, b, c.start, c.end, 1)
            used.update(range(a - len(lhs), a))
            used.update(range(b, b + len(rhs)))
            canon = _OPS.get(op.removesuffix(":w"), op)
            if lhs and rhs:
                out.add(f"cond:{pid}:{para}:{_join(lhs)}{canon}{_join(rhs)}")
        for k in range(c.start, c.end):
            t = toks[k]
            if k not in used and t.kind == "W" and t.up in conds88:
                out.add(f"cond:{pid}:{para}:{t.up}")
    out |= _evaluate_facts(p, pid)
    out |= _move_text_facts(p, pid)
    return out


def _exec_facts(p: _Prog, pid: str, spelled: dict[str, str]) -> set[str]:
    """xfer (LINK / XCTL with COMMAREA), sqlw (WHERE columns) and resp facts."""
    toks = p.toks
    out: set[str] = set()
    i = 0
    while i < len(toks):
        if not (p.exec_[i] and toks[i].up == "EXEC"):
            i += 1
            continue
        j = i + 1
        while j < len(toks) and p.exec_[j] and toks[j].up != "END-EXEC":
            j += 1
        block = toks[i + 1:j]
        kind = block[0].up if block else ""
        words = [t.up for t in block]
        if kind == "CICS" and len(block) > 1 and block[1].up in ("LINK", "XCTL"):
            target, area = "", "-"
            for k, t in enumerate(block):
                if t.up in ("PROGRAM", "COMMAREA") and k + 2 < len(block) and \
                        block[k + 1].text == "(":
                    arg = block[k + 2]
                    val = literal_value(arg.text) if arg.kind == "L" else arg.up
                    if t.up == "PROGRAM":
                        target = val
                    else:
                        area = val
            if target:
                out.add(f"xfer:{pid}:{block[1].up}:{target}:{area}")
        if kind == "SQL" and "WHERE" in words:
            op = block[1].up if len(block) > 1 else ""
            if op == "DECLARE":
                op = "DECLARE-CURSOR"
            line = toks[i].line
            tables = [t for s in p.prog.sql if s.line == line for t in s.tables]
            cols: set[str] = set()
            w = words.index("WHERE")
            for k in range(w + 1, len(block) - 1):
                t, nxt = block[k], block[k + 1]
                if t.kind == "W" and (nxt.up in _SQL_OPS or nxt.text in _SQL_OPS) and \
                        not (k > 0 and block[k - 1].text == ":"):
                    cols.add(t.up.split(".")[-1])
            for tab in tables:
                out.add(f"sqlw:{pid}:{op}:{tab.upper()}:{','.join(sorted(cols))}")
        i = j + 1
    for k, t in enumerate(toks):
        if p.proc[k] and t.up in ("DFHRESP", "DFHRESP2") and k + 2 < len(toks) and \
                toks[k + 1].text == "(":
            out.add(f"resp:{pid}:{p.para(t.line)}:{toks[k + 2].up}")
    return out


def _evaluate_facts(p: _Prog, pid: str) -> set[str]:
    toks = p.toks
    out: set[str] = set()
    stack: list[str] = []
    for i, t in enumerate(toks):
        if not p.proc[i]:
            continue
        if t.kind == "P":
            stack.clear()
            continue
        if t.up == "EVALUATE":
            j = i + 1
            subj: list[Tok] = []
            while j < len(toks) and p.proc[j] and toks[j].up not in ("WHEN",) and \
                    toks[j].kind != "P":
                subj.append(toks[j])
                j += 1
            stack.append(_join(subj))
        elif t.up == "END-EVALUATE" and stack:
            stack.pop()
        elif t.up == "WHEN" and stack and stack[-1] not in ("TRUE",):
            j = i + 1
            val: list[Tok] = []
            while j < len(toks) and p.proc[j] and toks[j].kind != "P" and \
                    not (toks[j].kind == "W" and toks[j].up in _VERBS):
                val.append(toks[j])
                j += 1
            out.add(f"when:{pid}:{p.para(t.line)}:{stack[-1]}:{_join(val) or 'OTHER'}")
    return out


def _move_text_facts(p: _Prog, pid: str) -> set[str]:
    toks = p.toks
    out: set[str] = set()
    verb = ""
    for i, t in enumerate(toks):
        if not p.proc[i]:
            continue
        if t.kind == "P":
            verb = ""
            continue
        if t.kind == "W" and t.up in _VERBS:
            verb = t.up
        if t.kind == "L" and verb in ("DISPLAY", "MOVE", "STRING"):
            v = _norm(literal_value(t.text))
            if len(v) >= 4 and t.text[:1].upper() not in ("X", "Z", "N", "G", "B"):
                out.add(f"text:{pid}:{v}")
        if t.up == "MOVE" and i + 2 < len(toks):
            src = toks[i + 1]
            lit = src.kind == "L" or (src.kind == "W" and (src.up in _FIGURATIVE
                                                            or _num(src.text) is not None
                                                            and src.text[:1].isdigit()))
            if not lit or toks[i + 2].up != "TO":
                continue
            k = i + 3
            while k < len(toks) and p.proc[k] and toks[k].kind == "W" and \
                    toks[k].up not in _VERBS:
                if toks[k].up not in ("OF", "IN"):
                    out.add(f"move:{pid}:{p.para(t.line)}:{_tok_text(src)}->{toks[k].up}")
                k += 1
    return out


def facts(rel: str, text: str, index=None) -> set[str]:
    """the fact ids of a program file's text (copybooks resolved for the original)."""
    fmt = detect_format(text)
    try:
        progs = parse_program_file(rel, text, _lookup(index, rel))
    except Exception:
        return set()
    toks = tokenize(logical_lines(text, fmt))
    out: set[str] = set()
    for prog in progs:
        span = program_span(text, prog.program_id)
        mine = toks if span is None else [t for t in toks if span[0] < t.line <= span[1]]
        p = _Prog(prog, mine)
        out |= _structural(p, text, fmt)
        out |= _scored(p, index)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def readable(fact: str) -> str:
    """a fact id in words, for the refine prompt."""
    parts = fact.split(":")
    head = parts[0]
    pid = parts[1] if len(parts) > 1 else ""
    rest = ":".join(parts[2:])
    words = {
        "id": f"PROGRAM-ID `{':'.join(parts[1:])}` (exact spelling)",
        "kind": f"`{pid}` as a {rest}",
        "entry": f"ENTRY '{rest}' in `{pid}`",
        "using": f"USING parameter {rest.replace(':', ' of ')} bytes in `{pid}`",
        "returning": f"a RETURNING item of {rest} bytes in `{pid}`",
        "file": f"file `{rest}` (DD:organization:access:open modes) in `{pid}`",
        "fd": f"an FD record of file `{rest.rsplit(':', 1)[0]}` of "
              f"{rest.rsplit(':', 1)[-1]} bytes in `{pid}`",
        "copy": f"COPY {rest} in `{pid}`",
        "data": f"data item `{rest}` (section:name:bytes) in `{pid}`",
        "para": f"paragraph `{rest}` in `{pid}`",
        "perform": f"PERFORM `{rest}` in `{pid}`",
        "call": f"a {rest.split(':', 1)[0]} of '{rest.split(':', 1)[-1]}' in `{pid}`",
        "sql": f"SQL {rest.replace(':', ' on ')} in `{pid}`",
        "sqlw": f"SQL `{rest}` (op:table:WHERE columns) in `{pid}`",
        "cics": f"EXEC CICS `{rest}` in `{pid}`",
        "xfer": f"EXEC CICS `{rest}` (LINK|XCTL:program:COMMAREA) in `{pid}`",
        "resp": f"a DFHRESP branch `{rest}` (paragraph:condition) in `{pid}`",
        "cond": f"the condition `{rest.split(':', 1)[-1]}` in paragraph "
                f"`{rest.split(':', 1)[0]}` of `{pid}`",
        "when": f"EVALUATE `{rest}` (paragraph:subject:WHEN value) in `{pid}`",
        "move": f"MOVE `{rest.split(':', 1)[-1]}` in paragraph `{rest.split(':', 1)[0]}` "
                f"of `{pid}`",
        "text": f"the literal '{rest}' in `{pid}`",
    }
    return words.get(head, fact)


def evaluate(original: set[str], regenerated: set[str]) -> dict:
    """relevant / passed / failing / phi / facts_by_class of the facts signal."""
    o, r = set(original), set(regenerated)
    os_, ok = split(o)
    rs, rk = split(r)
    phi_scored = _jaccard(ok, rk)
    phi_structural = _jaccard(os_, rs)
    invented = sorted(r - o)
    relevant = sorted(o) + [f"absent:{f}" for f in invented]
    passed = sorted(o & r)
    failing: dict[str, str] = {}
    for f in sorted(o - r):
        failing[f] = f"the regenerated program lacks {readable(f)}"
    for f in invented:
        failing[f"absent:{f}"] = (f"the regenerated program has {readable(f)}, which the "
                                  "original does not")
    by_class: dict[str, dict[str, int]] = {}
    for c in CLASSES:
        oc = {f for f in o if fact_class(f) == c}
        rc = {f for f in r if fact_class(f) == c}
        if oc or rc:
            by_class[c] = {"original": len(oc), "matched": len(oc & rc),
                           "invented": len(rc - oc)}
    return {"relevant": relevant, "passed": passed, "failing": failing,
            "phi": round(phi_scored * phi_structural, 6), "phi_scored": round(phi_scored, 6),
            "phi_structural": round(phi_structural, 6), "facts_by_class": by_class}
