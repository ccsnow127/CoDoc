"""static check of unit drivers: does each case's PASS line depend on an observed value?

A characterization test only checks something when its `DISPLAY 'TEST <id> PASS'` is
reached through a condition (IF / EVALUATE WHEN / an AT END-style phrase) and the case
can also print FAIL. A driver that CALLs the SUT and then DISPLAYs PASS unconditionally
passes whatever the SUT does; those cases are reported as *weak*, not as passed checks.

The check is lexical and conservative (it flags only what it can see):
  - a PASS display is conditional when it sits inside an IF / EVALUATE / conditional
    phrase of its sentence, or in a paragraph that is PERFORMed only from conditional
    places (transitively); IF conditions made of literals only (IF 1 = 1) do not count;
  - a case is weak when every PASS display naming it is unconditional, or when no FAIL
    display can print for it (neither 'TEST <id> FAIL' nor a FAIL line with a variable id);
  - PASS displays with a variable id ('TEST ' TD-ID ' PASS') vouch for every case that
    has no literal PASS line, when at least one of them is conditional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from codocs.cobol.reflow import tokens
from codocs.cobol.source import logical_lines

_PARA_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)(\s+SECTION)?\s*\.\s*$", re.I)
_PROC_RE = re.compile(r"\bPROCEDURE\s+DIVISION\b", re.I)
_TEST_RE = re.compile(r"\bTEST\s+(\S+)\s+(PASS|FAIL)\b", re.I)
_VAR = "\x00"  # placeholder for a data item inside a DISPLAY

# statement verbs: they end the operand list of DISPLAY and the condition of IF
_VERBS = {
    "ACCEPT", "ADD", "ALLOCATE", "CALL", "CANCEL", "CLOSE", "COMPUTE", "CONTINUE", "DELETE",
    "DISPLAY", "DIVIDE", "EVALUATE", "EXIT", "FREE", "GO", "GOBACK", "IF", "INITIALIZE",
    "INSPECT", "MOVE", "MULTIPLY", "OPEN", "PERFORM", "READ", "RELEASE", "RETURN", "REWRITE",
    "SEARCH", "SET", "SORT", "START", "STOP", "STRING", "SUBTRACT", "UNSTRING", "WRITE",
    "ELSE", "WHEN", "THEN", "NEXT", "END-IF", "END-EVALUATE", "END-PERFORM", "END-CALL",
    "END-READ", "END-WRITE", "END-SEARCH", "END-COMPUTE", "END-ADD", "END-SUBTRACT",
    "END-MULTIPLY", "END-DIVIDE", "END-STRING", "END-UNSTRING", "END-RETURN", "END-START",
    "END-DELETE", "END-REWRITE", "END-ACCEPT", "END-DISPLAY",
}
# words of a condition that are not data items
_COND_WORDS = {
    "AND", "OR", "NOT", "IS", "EQUAL", "EQUALS", "TO", "THAN", "GREATER", "LESS", "OR",
    "=", ">", "<", ">=", "<=", "<>", "ZERO", "ZEROS", "ZEROES", "SPACE", "SPACES", "TRUE",
    "FALSE", "HIGH-VALUE", "HIGH-VALUES", "LOW-VALUE", "LOW-VALUES", "QUOTE", "QUOTES",
    "NULL", "NULLS", "(", ")", "ALL",
}
# phrases that run their statements only on an observed outcome
_PHRASE_START = {"EXCEPTION", "OVERFLOW", "INVALID", "AT", "SIZE"}
_PHRASE_PREV = {"AT": {"END", "END-OF-PAGE", "EOP"}, "SIZE": {"ERROR"}}


@dataclass
class _Display:
    text: str  # literal parts joined, data items as _VAR
    conditional: bool  # inside a condition of its own paragraph
    para: str


@dataclass
class _Scan:
    displays: list[_Display] = field(default_factory=list)
    # paragraph -> [(caller paragraph, conditional at the call site)]
    performs: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)  # paragraphs in source order


def _split_period(tok: str) -> list[str]:
    """'X.' -> ['X', '.'] (a separator period), literals keep their inner periods."""
    if len(tok) > 1 and tok.endswith(".") and (tok[0] not in "'\"" or tok[-2] in "'\""):
        return [tok[:-1], "."]
    return [tok]


def _literal(tok: str) -> str | None:
    m = re.match(r"^[XxNnGgZz]?(['\"])(.*)\1$", tok, re.S)
    if m:
        return m.group(2).replace(m.group(1) * 2, m.group(1))
    return None


def _is_data_name(tok: str) -> bool:
    up = tok.upper()
    if up in _COND_WORDS or _literal(tok) is not None:
        return False
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", tok):
        return False
    return bool(re.match(r"[A-Za-z0-9]", tok)) and not re.fullmatch(r"[<>=()+*/-]+", tok)


def _procedure(source: str) -> list[tuple[str | None, list[str]]]:
    """(paragraph header or None, tokens of the line) for the PROCEDURE DIVISION."""
    out: list[tuple[str | None, list[str]]] = []
    started = False
    for ln in logical_lines(source, "fixed"):
        text = ln.text
        if not started:
            if _PROC_RE.search(text):
                started = True
            continue
        stripped = text.strip()
        indent = len(text) - len(text.lstrip())
        m = _PARA_RE.match(stripped)
        if m and indent < 4 and m.group(1).upper() not in _VERBS:
            out.append((m.group(1).upper(), []))
            continue
        if re.match(r"^END\s+PROGRAM\b", stripped, re.I):
            break
        toks: list[str] = []
        for t in tokens(stripped):
            toks.extend(_split_period(t))
        out.append((None, toks))
    return out


def _scan(source: str) -> _Scan:
    sc = _Scan()
    para = "(main)"
    sc.order.append(para)
    toks: list[tuple[str, str]] = []  # (paragraph, token)
    for header, line_toks in _procedure(source):
        if header is not None:
            para = header
            if para not in sc.order:
                sc.order.append(para)
            toks.append((para, "."))
            continue
        toks.extend((para, t) for t in line_toks)

    stack: list[str] = []  # IF | EVAL | PHRASE | LOOP | IF-TRIVIAL
    i = 0
    n = len(toks)

    def conditional() -> bool:
        return any(s in ("IF", "EVAL", "PHRASE") for s in stack)

    while i < n:
        para, tok = toks[i]
        up = tok.upper()
        if up == ".":
            stack.clear()
        elif up == "IF":
            j = i + 1
            cond: list[str] = []
            while j < n and toks[j][1].upper() not in _VERBS and toks[j][1] != ".":
                cond.append(toks[j][1])
                j += 1
            stack.append("IF" if any(_is_data_name(c) for c in cond) else "IF-TRIVIAL")
        elif up == "EVALUATE":
            stack.append("EVAL")
        elif up in ("END-IF", "END-EVALUATE"):
            want = ("IF", "IF-TRIVIAL") if up == "END-IF" else ("EVAL",)
            while stack:
                top = stack.pop()
                if top in want:
                    break
        elif up == "END-PERFORM":
            while stack:
                if stack.pop() == "LOOP":
                    break
        elif up.startswith("END-") and "PHRASE" in stack:
            while stack:
                if stack.pop() == "PHRASE":
                    break
        elif up in _PHRASE_START and _phrase_at(toks, i):
            stack.append("PHRASE")
        elif up == "PERFORM":
            nxt = toks[i + 1][1].upper() if i + 1 < n else ""
            if nxt in ("UNTIL", "VARYING", "WITH", "TEST", "FOREVER") or (
                    i + 2 < n and toks[i + 2][1].upper() == "TIMES" and
                    nxt not in sc.order and not _later_para(toks, nxt)):
                stack.append("LOOP")
            elif nxt and nxt != ".":
                targets = [nxt]
                if i + 3 < n and toks[i + 2][1].upper() in ("THRU", "THROUGH"):
                    targets.append("THRU:" + toks[i + 3][1].upper())
                for t in targets:
                    sc.performs.setdefault(t, []).append((para, conditional()))
        elif up == "DISPLAY":
            j = i + 1
            parts: list[str] = []
            while j < n and toks[j][1].upper() not in _VERBS and toks[j][1] != "." and \
                    toks[j][1].upper() not in ("UPON", "WITH", "NO"):
                lit = _literal(toks[j][1])
                parts.append(lit if lit is not None else _VAR)
                j += 1
            sc.displays.append(_Display("".join(p if p != _VAR else f" {_VAR} " for p in parts),
                                        conditional(), para))
        i += 1
    _expand_thru(sc)
    return sc


def _phrase_at(toks: list[tuple[str, str]], i: int) -> bool:
    """AT END / ON EXCEPTION / INVALID KEY / ON SIZE ERROR / ON OVERFLOW starts here."""
    up = toks[i][1].upper()
    nxt = toks[i + 1][1].upper() if i + 1 < len(toks) else ""
    if up in _PHRASE_PREV:
        return nxt in _PHRASE_PREV[up]
    if up == "INVALID":
        return True
    prev = toks[i - 1][1].upper() if i else ""
    return prev in ("ON", "NOT") or up in ("EXCEPTION", "OVERFLOW")


def _later_para(toks: list[tuple[str, str]], name: str) -> bool:
    return any(p == name for p, _ in toks)


def _expand_thru(sc: _Scan) -> None:
    """PERFORM A THRU B: every paragraph from A to B gets A's call sites."""
    for key in [k for k in sc.performs if k.startswith("THRU:")]:
        end = key[5:]
        sites = sc.performs.pop(key)
        for start, sites_a in list(sc.performs.items()):
            if start not in sc.order or end not in sc.order:
                continue
            a, b = sc.order.index(start), sc.order.index(end)
            if a < b and all(s in sites_a for s in sites):
                for p in sc.order[a + 1:b + 1]:
                    sc.performs.setdefault(p, []).extend(sites)


def _para_conditional(sc: _Scan) -> dict[str, bool]:
    """paragraph -> reached only through conditional PERFORMs (never by fall-through)."""
    memo: dict[str, bool] = {}

    def cond(p: str, seen: frozenset[str]) -> bool:
        if p in memo:
            return memo[p]
        sites = sc.performs.get(p, [])
        if not sites or p in seen:
            return False  # main flow / fall-through / recursion: treat as unconditional
        result = all(c or cond(caller, seen | {p}) for caller, c in sites)
        memo[p] = result
        return result

    return {p: cond(p, frozenset()) for p in sc.order}


def _id_of(text: str, word: str) -> tuple[str | None, bool]:
    """(case id, dynamic?) of a 'TEST <id> PASS|FAIL' display text, or (None, False)."""
    norm = re.sub(r"\s+", " ", text).strip()
    m = re.search(rf"\bTEST\s+(\S+)\s+{word}\b", norm, re.I)
    if m:
        cid = m.group(1)
        return (None, True) if _VAR in cid else (cid, False)
    if re.search(rf"\bTEST\b.*{_VAR}.*\b{word}\b", norm, re.I):
        return None, True
    return None, False


def display_texts(driver_source: str) -> list[str]:
    """the literal text of each DISPLAY in a driver (data items as NUL placeholders)."""
    if not driver_source.strip():
        return []
    return [d.text for d in _scan(driver_source).displays]


def weak_cases(driver_source: str, case_ids: list[str]) -> dict[str, str]:
    """case id -> reason, for cases whose PASS line checks nothing (see module doc)."""
    if not driver_source.strip() or not case_ids:
        return {}
    sc = _scan(driver_source)
    pcond = _para_conditional(sc)
    passes: dict[str, list[bool]] = {}
    fails: set[str] = set()
    dyn_pass: list[bool] = []
    dyn_fail = False
    for d in sc.displays:
        effective = d.conditional or pcond.get(d.para, False)
        cid, dyn = _id_of(d.text, "PASS")
        if cid:
            passes.setdefault(cid.upper(), []).append(effective)
        elif dyn:
            dyn_pass.append(effective)
        cid, dyn = _id_of(d.text, "FAIL")
        if cid:
            fails.add(cid.upper())
        elif dyn:
            dyn_fail = True
    out: dict[str, str] = {}
    for cid in case_ids:
        key = cid.upper()
        conds = passes.get(key)
        if conds is None:
            if dyn_pass and not any(dyn_pass):
                out[cid] = "PASS is printed unconditionally (variable case id)"
            continue  # no PASS line at all: the case shows up as not-run
        if not any(conds):
            out[cid] = ("PASS is printed unconditionally: no IF/EVALUATE compares an observed "
                        "value before it")
        elif key not in fails and not dyn_fail:
            out[cid] = "the case has no FAIL path (no 'TEST <id> FAIL' display)"
    return out
