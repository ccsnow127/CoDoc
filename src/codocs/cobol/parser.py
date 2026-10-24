"""COBOL program parser: per-program facts from one source file.

Works on a token stream built from normalized logical lines (see source.py), so statements
may span lines freely. Extracts PROGRAM-ID (incl. nested programs), COPY / SQL INCLUDE,
CALL (static + dynamic with VALUE / MOVE resolution), EXEC CICS transfers and commands,
EXEC SQL tables, FILE-CONTROL / FD / OPEN modes, LINKAGE / USING, data items, paragraphs
and the PERFORM graph.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

from codocs.cobol.models import (
    CallSite,
    CicsUse,
    CobolProgram,
    DataItem,
    FileUse,
    Paragraph,
    SqlUse,
)
from codocs.cobol.source import CommentLine, LogicalLine, detect_format, scan

CopybookLookup = Callable[[str], "str | None"]

_TOKEN_RE = re.compile(
    r"""
    (?P<pseudo>==.*?==)
    |(?P<lit>[XxNnGgZzBbUu]?(?:"(?:[^"]|"")*"?|'(?:[^']|'')*'?))
    |(?P<word>[A-Za-z0-9_$#@][A-Za-z0-9_\-$#@]*(?:\.[A-Za-z0-9_$#@][A-Za-z0-9_\-$#@]*)*)
    |(?P<tag>:[A-Za-z0-9\-]+:(?:-[A-Za-z0-9_\-]*)?(?=[\s.,()]|$))
    |(?P<period>\.(?=\s|$))
    |(?P<sym>\S)
    """,
    re.VERBOSE,
)

# words that end a list of data names (verbs, scope terminators, clause keywords)
_RESERVED = frozenset(
    """
    ACCEPT ADD ALTER CALL CANCEL CLOSE COMPUTE CONTINUE DELETE DISPLAY DIVIDE ELSE END
    EVALUATE EXEC EXECUTE EXIT GENERATE GO GOBACK IF INITIALIZE INITIATE INSPECT INVOKE
    MERGE MOVE MULTIPLY NEXT OPEN PERFORM READ RELEASE RETURN REWRITE SEARCH SET SORT START
    STOP STRING SUBTRACT TERMINATE UNSTRING UNLOCK WHEN WRITE ALLOCATE FREE RAISE RESUME
    JSON XML COMMIT ROLLBACK ENTRY USE COPY REPLACE NOT ON AT INVALID THEN ALSO OTHER THRU
    THROUGH UNTIL VARYING TIMES WITH TEST AFTER BEFORE BY FROM GIVING INTO TO USING
    RETURNING OF IN AND OR OVERFLOW EXCEPTION SIZE ERROR DIVISION SECTION PROCEDURE
    INPUT OUTPUT I-O EXTEND REFERENCE CONTENT VALUE ADDRESS LENGTH OMITTED UPON
    DECLARATIVES CORRESPONDING CORR ALL LEADING TRAILING REPLACING TALLYING CONVERTING
    DELIMITED POINTER COUNT SENTENCE PROGRAM RUN EOP END-OF-PAGE KEY INDEXED ASCENDING
    DESCENDING DUPLICATES COLLATING SEQUENCE TRUE FALSE WHEN-COMPILED FUNCTION
    """.split()
)
# single words that may stand alone with a period but are never paragraph names
_NOT_PARAGRAPH = _RESERVED | frozenset(
    "GOBACK EXIT CONTINUE ELSE STOP DECLARATIVES SKIP1 SKIP2 SKIP3 EJECT".split()
)
_ID_ENTRY_RE = re.compile(
    r"^\s*(AUTHOR|INSTALLATION|DATE-WRITTEN|DATE-COMPILED|SECURITY|REMARKS)\s*\.?(.*)$",
    re.IGNORECASE,
)
_ID_END_RE = re.compile(
    r"^\s*(?:(?:IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION|PROGRAM-ID|"
    r"FUNCTION-ID|END\s+PROGRAM|CONFIGURATION\s+SECTION|INPUT-OUTPUT\s+SECTION|"
    r"(?:WORKING-STORAGE|LINKAGE|FILE|LOCAL-STORAGE)\s+SECTION|"
    r"AUTHOR|INSTALLATION|DATE-WRITTEN|DATE-COMPILED|SECURITY|REMARKS)\b",
    re.IGNORECASE,
)
_ID_START_RE = re.compile(
    r"^\s*(?:(?:IDENTIFICATION|ID)\s+DIVISION|PROGRAM-ID|FUNCTION-ID)\b", re.IGNORECASE
)
_ID_LEAVE_RE = re.compile(
    r"^\s*(?:ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b|^\s*END\s+PROGRAM\b", re.IGNORECASE
)

_DATA_SECTIONS = {
    "WORKING-STORAGE": "WORKING-STORAGE",
    "LOCAL-STORAGE": "LOCAL-STORAGE",
    "LINKAGE": "LINKAGE",
    "FILE": "FILE",
    "SCREEN": "SCREEN",
    "REPORT": "REPORT",
    "COMMUNICATION": "COMMUNICATION",
}
_USAGE_WORDS = frozenset(
    """
    COMP COMP-1 COMP-2 COMP-3 COMP-4 COMP-5 COMP-6 COMP-X COMP-N COMPUTATIONAL
    COMPUTATIONAL-1 COMPUTATIONAL-2 COMPUTATIONAL-3 COMPUTATIONAL-4 COMPUTATIONAL-5
    BINARY PACKED-DECIMAL POINTER INDEX NATIONAL PROGRAM-POINTER PROCEDURE-POINTER
    BINARY-CHAR BINARY-SHORT BINARY-LONG BINARY-DOUBLE BINARY-C-LONG FLOAT-SHORT FLOAT-LONG
    FLOAT-DECIMAL-16 FLOAT-DECIMAL-34 FLOAT-EXTENDED SIGNED UNSIGNED
    """.split()
)
_CLAUSE_WORDS = _USAGE_WORDS | frozenset(
    """
    PIC PICTURE REDEFINES OCCURS VALUE VALUES USAGE JUSTIFIED JUST SYNC SYNCHRONIZED BLANK
    SIGN EXTERNAL GLOBAL IS RENAMES BASED ANY TYPEDEF DISPLAY CONSTANT AS DEPENDING
    """.split()
)
# ENVIRONMENT DIVISION paragraph / section headers that end a REPOSITORY paragraph
_ENV_PARAGRAPHS = frozenset(
    "SPECIAL-NAMES SOURCE-COMPUTER OBJECT-COMPUTER INPUT-OUTPUT FILE-CONTROL I-O-CONTROL "
    "DATA PROCEDURE WORKING-STORAGE LINKAGE LOCAL-STORAGE".split()
)
# clauses that make ACCEPT / DISPLAY a screen (curses) statement
_SCREEN_CLAUSES = frozenset(
    "AT LINE COLUMN COL POSITION NO-ECHO TIME-OUT TIMEOUT HIGHLIGHT LOWLIGHT BLINK "
    "REVERSE-VIDEO UNDERLINE FOREGROUND-COLOR FOREGROUND-COLOUR BACKGROUND-COLOR "
    "BACKGROUND-COLOUR BELL BEEP ERASE BLANK SECURE AUTO AUTO-SKIP PROMPT UPDATE "
    "SCROLL".split()
)
# the subset that lays out a screen (position, colors): the program has a screen UI
_SCREEN_LAYOUT_CLAUSES = frozenset(
    "AT LINE COLUMN COL POSITION FOREGROUND-COLOR FOREGROUND-COLOUR BACKGROUND-COLOR "
    "BACKGROUND-COLOUR HIGHLIGHT LOWLIGHT BLINK REVERSE-VIDEO UNDERLINE ERASE".split()
)
# words that end an ACCEPT / DISPLAY statement without a period
_STATEMENT_END = _RESERVED - {"WITH", "AT", "UPON", "FROM", "NOT", "ON", "EXCEPTION", "SIZE",
                              "ERROR", "LINE", "COLUMN", "COL", "BY", "TO", "OF", "IN",
                              "ALL", "COUNT", "OMITTED"} | {"END-ACCEPT", "END-DISPLAY"}
_SCREEN_ROUTINES = frozenset(
    "CBL_GET_SCR_SIZE CBL_SET_CSR_POS CBL_GET_CSR_POS CBL_READ_SCR_CHATTRS "
    "CBL_WRITE_SCR_CHATTRS CBL_READ_SCR_CHARS CBL_WRITE_SCR_CHARS CBL_CLEAR_SCR "
    "CBL_READ_KBD_CHAR CBL_GET_KBD_STATUS".split()
)
_DLI_CALLS = {"CBLTDLI", "AIBTDLI", "CEETDLI", "PLITDLI", "AERTDLI"}
_SQL_NOT_COPYBOOK = {"SQLCA", "SQLDA"}
_CICS_JOIN_ANY = {"INQUIRE", "SET", "CREATE", "DISCARD", "ENABLE", "DISABLE", "PERFORM"}
_CICS_JOIN_FIRST = {
    "SEND", "RECEIVE", "WRITEQ", "READQ", "DELETEQ", "HANDLE", "GET", "PUT", "PUSH", "POP",
    "IGNORE", "WEB", "DOCUMENT", "STARTBROWSE", "GETNEXT", "ENDBROWSE",
}
_CICS_SUBCOMMANDS = {
    "MAP", "TEXT", "CONTROL", "PAGE", "PARTNSET", "TS", "TD", "CONDITION", "AID", "ABEND",
    "CONTAINER", "CHANNEL", "COUNTER", "DCOUNTER", "HANDLE", "OPEN", "CLOSE", "READ",
    "SEND", "RECEIVE", "CONVERSE", "CREATE", "INSERT", "RETRIEVE", "SET", "ACTIVITY",
}
_CICS_FILE_CMDS = {
    "READ", "WRITE", "REWRITE", "DELETE", "STARTBR", "READNEXT", "READPREV", "ENDBR",
    "RESETBR", "UNLOCK",
}
_CICS_RESOLVE_ARGS = (
    "PROGRAM", "TRANSID", "MAP", "MAPSET", "FILE", "DATASET", "QUEUE", "QNAME",
)
_SQL_TABLE_STOP = frozenset(
    """
    WHERE GROUP ORDER HAVING JOIN INNER LEFT RIGHT FULL OUTER CROSS ON FETCH WITH FOR UNION
    EXCEPT INTERSECT SET VALUES SELECT INTO OPTIMIZE QUERYNO WITHOUT LIMIT OFFSET AS USING
    END-EXEC FINAL NEW OLD TABLE LATERAL
    """.split()
)
_PROGRAM_NAME_RE = re.compile(r"^[A-Z0-9#@$][A-Z0-9_#@$\-]{0,29}$")
_LICENSE_RE = re.compile(
    r"copyright|all rights reserved|licen[sc]e|warrant|apache\.org|"
    r"\"as is\" basis|unless required by applicable|express or implied|you may not use"
    r"|you may obtain|governing permissions|limitations under",
    re.IGNORECASE,
)
_BORDER_RE = re.compile(r"^[\s*\-=_+#~.:|/\\]*$")
_MAX_CHILDREN = 300
_MAX_COPY_DEPTH = 6


class Tok(NamedTuple):
    kind: str  # W word, L literal, P period, X pseudo-text, S symbol
    text: str
    up: str
    line: int  # physical line
    li: int  # logical line index (per source)
    pos: int  # column within the logical line text
    end: int


@dataclass
class _Entry:
    level: int
    name: str
    line: int
    section: str
    copybook: str = ""
    picture: str = ""
    usage: str = ""
    value: str = ""  # raw first VALUE operand (literal with quotes, or word)
    value_lit: str | None = None  # literal value without quotes
    redefines: str = ""
    occurs: str = ""
    parent: int = -1  # index of the parent entry


@dataclass
class _ProgSpan:
    name: str
    start: int  # token index (inclusive)
    end: int  # token index (exclusive)
    parent: int = -1
    children: list[int] = field(default_factory=list)
    function: bool = False  # a FUNCTION-ID unit (user-defined function)


# -- tokenizing -----------------------------------------------------------


def tokenize(lines: list[LogicalLine]) -> list[Tok]:
    toks: list[Tok] = []
    for li, ll in enumerate(lines):
        for m in _TOKEN_RE.finditer(ll.text):
            kind = m.lastgroup
            text = m.group()
            if kind in ("word", "tag"):
                k = "W"
            elif kind == "lit":
                k = "L"
            elif kind == "period":
                k = "P"
            elif kind == "pseudo":
                k = "X"
            else:
                k = "S"
            up = text.upper() if k in ("W", "S") else text
            toks.append(Tok(k, text, up, ll.line, li, m.start(), m.end()))
    return toks


def literal_value(text: str) -> str:
    """'ABC' / "A""B" / X'00' -> unquoted value."""
    t = text
    if len(t) > 1 and t[0] not in "'\"" and t[1] in "'\"":
        t = t[1:]
    if not t or t[0] not in "'\"":
        return t
    q = t[0]
    inner = t[1:-1] if len(t) > 1 and t.endswith(q) else t[1:]
    return inner.replace(q + q, q)


def _chunk(toks: list[Tok], k: int) -> tuple[str, int]:
    """contiguous text starting at token k (no whitespace between tokens) -> (text, last idx)."""
    text = toks[k].text
    j = k
    while j + 1 < len(toks) and toks[j + 1].li == toks[j].li and toks[j + 1].pos == toks[j].end:
        j += 1
        text += toks[j].text
    return text, j


def _skip_parens(toks: list[Tok], k: int) -> int:
    """index just past the ')' matching the '(' at k."""
    depth = 0
    n = len(toks)
    while k < n:
        t = toks[k]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
            if depth <= 0:
                return k + 1
        elif t.kind == "P":
            return k
        k += 1
    return k


def _split_sentences(toks: list[Tok]) -> list[list[Tok]]:
    """split at separator periods (and after END-EXEC so EXEC blocks stand alone)."""
    out: list[list[Tok]] = []
    cur: list[Tok] = []
    for t in toks:
        if t.kind == "P":
            if cur:
                out.append(cur)
            cur = []
            continue
        cur.append(t)
        if t.up == "END-EXEC":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _norm_member(name: str) -> str:
    """COPY operand -> upper member name (basename without a known extension)."""
    n = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    base, dot, ext = n.rpartition(".")
    if dot and ext.lower() in {"cpy", "cbl", "cob", "copy", "cpb", "inc", "dcl", "txt", "cobol"}:
        n = base
    return n.upper()


# -- identification-division comment entries ------------------------------


def _extract_id_entries(
    lines: list[LogicalLine],
) -> tuple[list[LogicalLine], list[tuple[int, str, str]]]:
    """drop AUTHOR/REMARKS/... comment-entries (free text) -> (kept lines, entries)."""
    kept: list[LogicalLine] = []
    entries: list[tuple[int, str, str]] = []
    in_id = False
    cur: list | None = None
    for ll in lines:
        text = ll.text
        if _ID_START_RE.match(text):
            in_id = True
            cur = None
        elif _ID_LEAVE_RE.match(text):
            in_id = False
            cur = None
        if in_id:
            m = _ID_ENTRY_RE.match(text)
            if m:
                cur = [ll.line, m.group(1).upper(), m.group(2).strip()]
                entries.append(cur)  # type: ignore[arg-type]
                continue
            if cur is not None and not _ID_END_RE.match(text):
                cur[2] = (cur[2] + " " + text.strip()).strip()
                continue
            cur = None
        kept.append(ll)
    return kept, [(e[0], e[1], e[2].rstrip(".").strip()) for e in entries]


# -- program segmentation -------------------------------------------------


def _find_programs(toks: list[Tok]) -> list[_ProgSpan]:
    """locate PROGRAM-ID ... END PROGRAM spans; nested programs get a parent index."""
    events: list[tuple[int, str, str, int]] = []  # (tok idx, kind, name, end idx)
    functions: set[int] = set()  # start tok idx of FUNCTION-ID units
    n = len(toks)
    for i, t in enumerate(toks):
        if t.kind != "W":
            continue
        if t.up in ("PROGRAM-ID", "FUNCTION-ID") and i + 1 < n:
            j = i + 1
            if toks[j].kind == "P":
                j += 1
            if j < n and toks[j].kind in ("W", "L"):
                name = toks[j].text if toks[j].kind == "W" else literal_value(toks[j].text)
                start = i
                # include a directly preceding IDENTIFICATION DIVISION .
                if i >= 3 and toks[i - 1].kind == "P" and toks[i - 2].up == "DIVISION" and \
                        toks[i - 3].up in ("IDENTIFICATION", "ID"):
                    start = i - 3
                events.append((start, "start", name.upper(), 0))
                if t.up == "FUNCTION-ID":
                    functions.add(start)
        elif t.up == "END" and i + 2 < n and toks[i + 1].up in ("PROGRAM", "FUNCTION") and \
                toks[i + 2].kind in ("W", "L"):
            nt = toks[i + 2]
            name = nt.text if nt.kind == "W" else literal_value(nt.text)
            end = i + 3
            if end < n and toks[end].kind == "P":
                end += 1
            events.append((i, "end", name.upper(), end))

    has_end = any(e[1] == "end" for e in events)
    spans: list[_ProgSpan] = []
    stack: list[int] = []
    for idx, kind, name, end in events:
        if kind == "start":
            if stack and not has_end:
                spans[stack.pop()].end = idx  # sibling programs without END PROGRAM
            span = _ProgSpan(name=name, start=idx, end=n, function=idx in functions)
            if stack:
                span.parent = stack[-1]
                spans[stack[-1]].children.append(len(spans))
            spans.append(span)
            stack.append(len(spans) - 1)
        else:
            while stack:
                top = stack.pop()
                spans[top].end = end
                if spans[top].name == name:
                    break
    return spans


# -- data division --------------------------------------------------------


def _parse_entry(st: list[Tok], section: str, copybook: str) -> _Entry | None:
    if not st or st[0].kind != "W" or not st[0].text.isdigit():
        return None
    level = int(st[0].text)
    if not (1 <= level <= 49 or level in (66, 77, 78, 88)):
        return None
    k = 1
    name = "FILLER"
    if k < len(st) and st[k].kind == "W" and st[k].up not in _CLAUSE_WORDS:
        name = st[k].up
        k += 1
    e = _Entry(level=level, name=name, line=st[0].line, section=section, copybook=copybook)
    n = len(st)
    while k < n:
        t = st[k]
        u = t.up
        if u in ("PIC", "PICTURE"):
            k += 1
            if k < n and st[k].up == "IS":
                k += 1
            if k < n:
                text, k = _chunk(st, k)
                e.picture = text.rstrip(".,").upper()
        elif u == "REDEFINES" and k + 1 < n:
            e.redefines = st[k + 1].up
            k += 1
        elif u == "OCCURS" and k + 1 < n:
            e.occurs = st[k + 1].text
            if k + 3 < n and st[k + 2].up == "TO":
                e.occurs += " TO " + st[k + 3].text
                k += 2
            k += 1
        elif u in ("VALUE", "VALUES") and not e.value:
            k += 1
            while k < n and st[k].up in ("IS", "ARE"):
                k += 1
            if k < n:
                v = st[k]
                if v.kind == "L":
                    e.value = v.text
                    e.value_lit = literal_value(v.text)
                else:
                    e.value, _ = _chunk(st, k)
        elif u == "USAGE":
            if k + 1 < n and st[k + 1].up == "IS":
                k += 1
            if k + 1 < n:
                e.usage = st[k + 1].up
                k += 1
        elif u in _USAGE_WORDS and not e.usage:
            e.usage = u
        k += 1
    return e


def _entry_line(e: _Entry) -> str:
    parts = [f"{e.level:02d} {e.name}"]
    if e.redefines:
        parts.append(f"REDEFINES {e.redefines}")
    if e.occurs:
        parts.append(f"OCCURS {e.occurs}")
    if e.picture:
        parts.append(f"PIC {e.picture}")
    if e.usage:
        parts.append(e.usage)
    if e.value:
        v = e.value if len(e.value) <= 40 else e.value[:37] + "..."
        parts.append(f"VALUE {v}")
    return " ".join(parts)


# -- the parser -----------------------------------------------------------


class _Ctx:
    """shared state while parsing one file."""

    def __init__(self, rel_path: str, text: str, lookup: CopybookLookup | None):
        self.rel_path = rel_path
        self.text = text
        self.lookup = lookup
        self.fmt = detect_format(text)
        lines, comments = scan(text, self.fmt)
        self.lines, self.id_entries = _extract_id_entries(lines)
        self.comments: list[CommentLine] = comments
        self.toks = tokenize(self.lines)
        self.loc = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        self._copy_cache: dict[tuple[str, tuple], list[list[Tok]] | None] = {}

    def copybook_sentences(
        self, member: str, replacing: tuple[tuple[str, str], ...]
    ) -> list[list[Tok]] | None:
        """tokens of a copybook (REPLACING applied) split into sentences, or None."""
        key = (member, replacing)
        if key in self._copy_cache:
            return self._copy_cache[key]
        text = self.lookup(member) if self.lookup else None
        result = None
        if text is not None:
            text = _apply_replacing(text, replacing)
            lines, _ = scan(text)
            result = _split_sentences(tokenize(lines))
        self._copy_cache[key] = result
        return result


def _apply_replacing(text: str, replacing: tuple[tuple[str, str], ...]) -> str:
    for old, new in replacing:
        if not old:
            continue
        if old.startswith("==") and old.endswith("==") and len(old) >= 4:
            o = old[2:-2].strip()
            nw = new[2:-2].strip() if new.startswith("==") else new
            if o:
                text = re.sub(re.escape(o), lambda _m, nw=nw: nw, text, flags=re.IGNORECASE)
        else:
            nw = new[2:-2].strip() if new.startswith("==") else new
            pat = r"(?<![A-Za-z0-9\-])" + re.escape(old) + r"(?![A-Za-z0-9\-])"
            text = re.sub(pat, lambda _m, nw=nw: nw, text, flags=re.IGNORECASE)
    return text


def _parse_copy(toks: list[Tok], i: int) -> tuple[str, tuple[tuple[str, str], ...], int]:
    """COPY at index i -> (member, replacing pairs, index after the statement)."""
    n = len(toks)
    j = i + 1
    if j >= n:
        return "", (), j
    t = toks[j]
    member = _norm_member(literal_value(t.text) if t.kind == "L" else t.text)
    j += 1
    pairs: list[tuple[str, str]] = []
    while j < n and toks[j].kind != "P":
        u = toks[j].up
        if u == "REPLACING":
            j += 1
            while j < n and toks[j].kind != "P":
                if toks[j].up in ("LEADING", "TRAILING", "ALSO"):
                    j += 1
                    continue
                old = toks[j].text
                j += 1
                if j < n and toks[j].up == "BY" and j + 1 < n:
                    pairs.append((old, toks[j + 1].text))
                    j += 2
                else:
                    break
            continue
        j += 1
    return member, tuple(pairs), j + 1


class _ProgramParser:
    """facts for one top-level program (its token span, nested programs included)."""

    def __init__(self, ctx: _Ctx, span: _ProgSpan, spans: list[_ProgSpan], file_progs: int):
        self.ctx = ctx
        self.span = span
        self.toks = ctx.toks[span.start:span.end]
        nested = [spans[c] for c in _descendants(span, spans)]
        own_mask = [True] * len(self.toks)
        for ns in nested:
            for k in range(ns.start - span.start, min(ns.end, span.end) - span.start):
                own_mask[k] = False
        self.own = [t for t, keep in zip(self.toks, own_mask) if keep]
        self.nested = [ns.name for ns in nested]
        self.file_progs = file_progs
        self.prog = CobolProgram(program_id=span.name, path=ctx.rel_path, format=ctx.fmt,
                                 unit_kind="function" if span.function else "program")
        self.entries: list[_Entry] = []
        self.values: dict[str, set[str]] = {}
        self.moves: dict[str, list[tuple[str, str]]] = {}  # target -> [(kind L|W, value)]
        self.exec_mask = [False] * len(self.toks)
        self.copy_refs: list[tuple[int, str]] = []  # (line, member)
        self.cursors: dict[str, list[str]] = {}
        self.fd_records: dict[str, str] = {}

    # -- driver --

    def parse(self) -> CobolProgram:
        p = self.prog
        p.nested_programs = self.nested
        self._metrics()
        self._mark_exec()
        div = self._divisions()
        self._data_division(div)
        self._file_control(div)
        self._moves()
        self._copies()
        self._procedure(div)
        self._calls()
        self._functions(div)
        self._terminal_io(div)
        self._execs()
        self._opens()
        self._finish()
        return p

    # -- metrics / header --

    def _metrics(self) -> None:
        ctx, p = self.ctx, self.prog
        first_line = self.toks[0].line if self.toks else 1
        last_line = self.toks[-1].line if self.toks else ctx.loc
        if self.file_progs == 1:
            p.loc = ctx.loc
            p.sloc = len(ctx.lines) + sum(1 for _ in ctx.id_entries)
            lo = 1
        else:
            p.loc = last_line - first_line + 1
            p.sloc = sum(1 for ll in ctx.lines if first_line <= ll.line <= last_line)
            lo = self._header_floor(first_line)
        # header: comments from the previous program end up to the first non-ID division
        hi = last_line
        for t_i, t in enumerate(self.own):
            if t.up in ("ENVIRONMENT", "DATA", "PROCEDURE") and t_i + 1 < len(self.own) and \
                    self.own[t_i + 1].up == "DIVISION":
                hi = t.line
                break
        raw = [c.text for c in ctx.comments if lo <= c.line < hi]
        remarks = []
        for line, key, text in ctx.id_entries:
            if not (lo <= line <= hi):
                continue
            if key == "AUTHOR" and not p.author:
                p.author = text[:120]
            elif key == "REMARKS" and text:
                remarks.append(text)
        p.header_comment = _clean_comment_block(raw + remarks)

    def _header_floor(self, first_line: int) -> int:
        """first physical line after the code that precedes this program in the file."""
        prev = [t.line for t in self.ctx.toks if t.line < first_line]
        return (max(prev) + 1) if prev else 1

    # -- structure --

    def _mark_exec(self) -> None:
        toks = self.toks
        n = len(toks)
        i = 0
        while i < n:
            if toks[i].up in ("EXEC", "EXECUTE") and i + 1 < n and toks[i + 1].kind == "W":
                j = i + 1
                while j < n and toks[j].up != "END-EXEC":
                    j += 1
                if j >= n:
                    self.prog.parse_warnings.append(
                        f"line {toks[i].line}: EXEC {toks[i + 1].up} without END-EXEC"
                    )
                    j = i + 1
                    while j < n and toks[j].kind != "P":
                        j += 1
                for k in range(i, min(j + 1, n)):
                    self.exec_mask[k] = True
                i = j + 1
                continue
            i += 1

    def _divisions(self) -> dict[str, int]:
        div: dict[str, int] = {}
        own = self.own
        for i in range(len(own) - 1):
            t = own[i]
            if own[i + 1].up == "DIVISION" and t.up in ("ENVIRONMENT", "DATA", "PROCEDURE"):
                div.setdefault(t.up, i)
        return div

    def _data_division(self, div: dict[str, int]) -> None:
        if "DATA" not in div:
            return
        start = div["DATA"]
        end = div.get("PROCEDURE", len(self.own))
        section = ""
        fd_pending = ""
        fd_records: dict[str, str] = {}
        stack: list[int] = []

        def add(e: _Entry) -> None:
            nonlocal fd_pending
            if e.level in (1, 77):
                stack.clear()
            elif e.level in (66, 78, 88):
                pass
            else:
                while stack and self.entries[stack[-1]].level >= e.level:
                    stack.pop()
            if e.level == 88 or e.level == 66:
                e.parent = stack[-1] if stack else -1
            else:
                e.parent = stack[-1] if stack else -1
                if e.level not in (77, 78):
                    stack.append(len(self.entries))
            self.entries.append(e)
            if e.level == 1 and fd_pending:
                fd_records[fd_pending] = e.name
                fd_pending = ""

        def walk(sentences: list[list[Tok]], copybook: str, depth: int) -> None:
            nonlocal section, fd_pending
            for st in sentences:
                head = st[0].up
                if len(st) >= 2 and st[1].up == "SECTION" and head in _DATA_SECTIONS:
                    section = _DATA_SECTIONS[head]
                    if section == "SCREEN":
                        self.prog.uses_screen_io = True
                    continue
                if head in ("FD", "SD", "RD") and len(st) > 1:
                    fd_pending = st[1].up
                    continue
                if head == "COPY":
                    member, repl, _ = _parse_copy(st + [_period(st[-1])], 0)
                    self._expand(member, repl, depth, walk, copybook)
                    continue
                if head in ("EXEC", "EXECUTE") and len(st) > 3 and st[1].up == "SQL" and \
                        st[2].up == "INCLUDE":
                    member = _norm_member(literal_value(st[3].text))
                    if member not in _SQL_NOT_COPYBOOK:
                        self._expand(member, (), depth, walk, copybook)
                    continue
                e = _parse_entry(st, section, copybook)
                if e is not None:
                    add(e)

        walk(_split_sentences(self.own[start + 2:end]), "", 0)
        self.fd_records = fd_records
        self._collect_items()

    def _expand(self, member: str, repl, depth: int, walk, copybook: str) -> None:
        if not member or self.ctx.lookup is None:
            return
        if depth >= _MAX_COPY_DEPTH:
            self.prog.parse_warnings.append(f"COPY {member}: nesting deeper than {depth}")
            return
        sentences = self.ctx.copybook_sentences(member, repl)
        if sentences is None:
            return
        walk(sentences, copybook or member, depth + 1)

    def _collect_items(self) -> None:
        items: list[DataItem] = []
        cur: DataItem | None = None
        extra = 0
        for e in self.entries:
            if e.value_lit is not None and e.level not in (88,) and e.name != "FILLER":
                self.values.setdefault(e.name, set()).add(e.value_lit)
            if e.section == "SCREEN":
                continue
            if e.level in (1, 77):
                if cur is not None and extra:
                    cur.children.append(f"... ({extra} more)")
                cur = DataItem(
                    name=e.name, level=e.level, section=e.section, picture=e.picture,
                    copybook=e.copybook,
                )
                extra = 0
                items.append(cur)
            elif cur is not None:
                if len(cur.children) < _MAX_CHILDREN:
                    cur.children.append(_entry_line(e))
                else:
                    extra += 1
        if cur is not None and extra:
            cur.children.append(f"... ({extra} more)")
        self.prog.data_items = items

    def _table_candidates(self, name: str) -> set[str]:
        """values of a REDEFINES'd FILLER table, for an OCCURS field like PGMNAME(I)."""
        idx = [i for i, e in enumerate(self.entries) if e.name == name]
        out: set[str] = set()
        for i in idx:
            target = self.entries[i]
            j = i
            while j >= 0 and not self.entries[j].redefines:
                j = self.entries[j].parent
            if j < 0:
                continue
            base_name = self.entries[j].redefines
            bases = [b for b, e in enumerate(self.entries) if e.name == base_name]
            for b in bases:
                for k in range(b + 1, len(self.entries)):
                    e = self.entries[k]
                    if e.level <= self.entries[b].level and e.level not in (88, 66):
                        break
                    if e.value_lit is not None and e.picture == target.picture and \
                            e.level != 88:
                        out.add(e.value_lit)
        return out

    def _file_control(self, div: dict[str, int]) -> None:
        start = div.get("ENVIRONMENT", 0)
        end = div.get("DATA", div.get("PROCEDURE", len(self.own)))
        files: list[FileUse] = []
        for st in _split_sentences(self.own[start:end]):
            for i, t in enumerate(st):
                if t.up == "SELECT":
                    files.append(_parse_select(st[i:]))
                    break
        for f in files:
            f.fd_record = self.fd_records.get(f.name, "")
        self.prog.files = files

    def _moves(self) -> None:
        """MOVE x TO a b c  -> a/b/c may hold x (literal or data name)."""
        toks = self.toks
        n = len(toks)
        for i, t in enumerate(toks):
            if t.up != "MOVE" or self.exec_mask[i] or i + 2 >= n:
                continue
            src = toks[i + 1]
            if src.kind == "L":
                val = ("L", literal_value(src.text))
                j = i + 2
            elif src.kind == "W" and src.up not in _RESERVED:
                val = ("W", src.up)
                j = i + 2
                while j < n and (toks[j].up in ("OF", "IN") or toks[j].text == "("):
                    j = _skip_parens(toks, j) if toks[j].text == "(" else j + 2
            else:
                continue
            if j >= n or toks[j].up != "TO":
                continue
            for target in _name_list(toks, j + 1):
                self.moves.setdefault(target, []).append(val)

    def resolve(self, name: str, depth: int = 0, seen: set[str] | None = None) -> set[str]:
        """literal values a data name may hold (VALUE clause, MOVE literal / constant)."""
        seen = seen if seen is not None else set()
        if name in seen:
            return set()
        seen.add(name)
        vals = set(self.values.get(name, set()))
        for kind, v in self.moves.get(name, []):
            if kind == "L":
                vals.add(v)
            elif depth < 3:
                vals |= self.resolve(v, depth + 1, seen)
        if not vals and depth == 0:
            vals = self._table_candidates(name)
        return {v.strip() for v in vals if v.strip()}

    def _copies(self) -> None:
        toks = self.toks
        for i, t in enumerate(toks):
            if t.up == "COPY" and t.kind == "W" and not self.exec_mask[i] and i + 1 < len(toks):
                nt = toks[i + 1]
                if nt.kind in ("W", "L"):
                    member = _norm_member(literal_value(nt.text) if nt.kind == "L" else nt.text)
                    self.copy_refs.append((t.line, member))

    # -- procedure division --

    def _procedure(self, div: dict[str, int]) -> None:
        p = self.prog
        if "PROCEDURE" not in div:
            if self.toks:
                p.parse_warnings.append("no PROCEDURE DIVISION found")
            return
        own = self.own
        i = div["PROCEDURE"] + 2
        n = len(own)
        # header: USING ... RETURNING ...
        mode = ""
        while i < n and own[i].kind != "P":
            u = own[i].up
            if u in ("USING", "CHAINING"):
                mode = "using"
            elif u == "RETURNING":
                mode = "returning"
            elif own[i].text == "(":
                i = _skip_parens(own, i)
                continue
            elif own[i].kind == "W" and u not in ("BY", "REFERENCE", "VALUE", "CONTENT",
                                                  "OPTIONAL", "DELIMITED", "SIZE"):
                if mode == "using":
                    p.using_params.append(u)
                elif mode == "returning" and not p.returning:
                    p.returning = u
            i += 1
        body = own[i + 1:] if i < n else []
        self._paragraphs(body)

    def _paragraphs(self, body: list[Tok]) -> None:
        p = self.prog
        fixed = self.ctx.fmt == "fixed"
        headers: list[tuple[int, Paragraph]] = []
        prev: Tok | None = None
        for k, t in enumerate(body):
            first_in_line = prev is None or prev.li != t.li
            if t.kind == "W" and first_in_line and k + 1 < len(body) and \
                    t.up not in _NOT_PARAGRAPH and not t.up.startswith("END-"):
                nxt = body[k + 1]
                is_section = nxt.up == "SECTION" and k + 2 < len(body) and \
                    body[k + 2].kind in ("P",)
                if (nxt.kind == "P" or is_section) and nxt.li == t.li:
                    ok = t.pos < 4 if fixed else (prev is None or prev.kind == "P")
                    if ok:
                        kind = "section" if is_section else "paragraph"
                        headers.append((k, Paragraph(name=t.up, kind=kind, line=t.line)))
            prev = t
        names = {h.name for _, h in headers}
        paras = [h for _, h in headers]
        # statements before the first header form an implicit entry paragraph
        first_k = headers[0][0] if headers else len(body)
        lead = Paragraph(name="(entry)", line=body[0].line if body else 0)
        bounds = [(0, first_k, lead)] + [
            (k, headers[x + 1][0] if x + 1 < len(headers) else len(body), h)
            for x, (k, h) in enumerate(headers)
        ]
        for lo, hi, para in bounds:
            seg = body[lo:hi]
            for j, t in enumerate(seg):
                if t.up == "PERFORM" and j + 1 < len(seg):
                    a = seg[j + 1]
                    if a.kind == "W" and a.up in names:
                        _add_unique(para.performs, a.up)
                        if j + 3 < len(seg) and seg[j + 2].up in ("THRU", "THROUGH") and \
                                seg[j + 3].up in names:
                            _add_unique(para.performs, seg[j + 3].up)
                elif t.up == "GO" and j + 1 < len(seg):
                    k = j + 1
                    if seg[k].up == "TO":
                        k += 1
                    while k < len(seg) and seg[k].kind == "W" and seg[k].up in names:
                        _add_unique(para.performs, seg[k].up)
                        k += 1
                elif t.up == "ENTRY" and j + 1 < len(seg) and seg[j + 1].kind == "L":
                    _add_unique(p.entry_points, literal_value(seg[j + 1].text).upper())
        if lead.performs and headers:
            paras.insert(0, lead)
        p.paragraphs = paras
        try:
            p.flow_hazards = _FlowScan(body, headers, self._exec_keys()).hazards()
        except Exception as e:  # analysis aid only: never fail the parse over it
            p.parse_warnings.append(f"flow-hazard scan failed: {type(e).__name__}: {e}")

    def _exec_keys(self) -> set[tuple[int, int]]:
        """(logical line, column) of every token inside an EXEC ... END-EXEC block."""
        return {(t.li, t.pos) for t, m in zip(self.toks, self.exec_mask) if m}

    def _calls(self) -> None:
        toks = self.toks
        n = len(toks)
        p = self.prog
        for i, t in enumerate(toks):
            if t.up != "CALL" or t.kind != "W" or self.exec_mask[i] or i + 1 >= n:
                continue
            j = i + 1
            static = False
            while j < n and toks[j].up in ("STATIC", "STDCALL", "WINAPI"):
                static = static or toks[j].up == "STATIC"
                j += 1
            if j >= n:
                continue
            tgt = toks[j]
            using = _using_args(toks, j + 1)
            if tgt.kind == "L":
                name = literal_value(tgt.text).strip().upper()
                if name in _DLI_CALLS:
                    p.has_dli = True
                p.calls.append(CallSite(target=name, kind="call", line=t.line, using=using,
                                        static=static))
            elif tgt.kind == "W" and tgt.up not in _RESERVED:
                self._dynamic(tgt.up, "call-dynamic", t.line, using)

    def _functions(self, div: dict[str, int]) -> None:
        """user-defined functions declared in the REPOSITORY paragraph (CallSite kind
        'function'); intrinsic declarations (FUNCTION ALL INTRINSIC, FUNCTION x INTRINSIC)
        are skipped. The line is the first FUNCTION reference in the procedure division."""
        own = self.own
        end = div.get("DATA", div.get("PROCEDURE", len(own)))
        i = next((k for k in range(min(end, len(own))) if own[k].up == "REPOSITORY"), None)
        if i is None:
            return
        groups: list[list[Tok]] = []
        k = i + 1
        while k < end:
            t = own[k]
            if t.up in _ENV_PARAGRAPHS or t.up in ("SECTION", "DIVISION"):
                break
            if t.up in ("FUNCTION", "PROGRAM", "CLASS", "INTERFACE", "PROPERTY"):
                groups.append([t])
            elif groups and t.kind in ("W", "L"):
                groups[-1].append(t)
            k += 1
        proc = div.get("PROCEDURE", len(own))
        seen: set[str] = set()
        for g in groups:
            if g[0].up != "FUNCTION" or any(t.up == "INTRINSIC" for t in g):
                continue
            j = 1
            while j < len(g):
                t = g[j]
                j += 1
                if t.kind != "W" or t.up == "AS":
                    continue
                target = t.up  # FUNCTION proto-name [AS 'external-name']
                if j + 1 < len(g) and g[j].up == "AS" and g[j + 1].kind == "L":
                    target = literal_value(g[j + 1].text).strip().upper()
                    j += 2
                if target in seen:
                    continue
                seen.add(target)
                line = next((x.line for x in own[proc:] if x.up == t.up), t.line)
                self.prog.calls.append(CallSite(target=target, kind="function", line=line))

    def _terminal_io(self, div: dict[str, int]) -> None:
        """extended (screen) ACCEPT / DISPLAY or screen runtime routines: the program
        needs a terminal (curses), so it cannot run headless."""
        own = self.own
        proc = div.get("PROCEDURE")
        if proc is None:
            return
        p = self.prog
        if any(c.kind == "call" and c.target in _SCREEN_ROUTINES for c in p.calls):
            p.uses_terminal_io = True  # keep scanning: positioned DISPLAYs set uses_screen_io
        n = len(own)
        k = proc
        while k < n:
            t = own[k]
            if t.kind == "W" and t.up in ("ACCEPT", "DISPLAY"):
                j = k + 1
                while j < n and own[j].kind != "P" and own[j].up not in _STATEMENT_END:
                    u = own[j].up
                    if u in _SCREEN_CLAUSES or (u == "OMITTED" and t.up == "ACCEPT"):
                        p.uses_terminal_io = True
                        if u in _SCREEN_LAYOUT_CLAUSES:
                            p.uses_screen_io = True  # a positioned / colored screen UI
                            return
                    j += 1
                k = j
                continue
            k += 1

    def _dynamic(self, data_name: str, kind: str, line: int, using: list[str]) -> None:
        """record a transfer through a data name, resolved to literals when possible."""
        p = self.prog
        vals = [v.upper() for v in sorted(self.resolve(data_name))]
        vals = [v for v in vals if _PROGRAM_NAME_RE.match(v) and v != p.program_id]
        if vals:
            for v in vals:
                if v in _DLI_CALLS:
                    p.has_dli = True
                p.calls.append(
                    CallSite(target=v, kind=kind, line=line, using=using, via=data_name)
                )
        else:
            p.calls.append(
                CallSite(target=data_name, kind=kind, line=line, using=using, via=data_name)
            )
            p.parse_warnings.append(f"line {line}: {kind} target {data_name} not resolved")

    # -- EXEC blocks --

    def _execs(self) -> None:
        toks = self.toks
        n = len(toks)
        i = 0
        while i < n:
            if self.exec_mask[i] and toks[i].up in ("EXEC", "EXECUTE"):
                j = i
                while j < n and self.exec_mask[j] and toks[j].up != "END-EXEC":
                    j += 1
                kind = toks[i + 1].up if i + 1 < n else ""
                block = toks[i + 2:j]
                if kind == "CICS":
                    self.prog.has_cics = True
                    self._cics(block, toks[i].line)
                elif kind == "SQL":
                    self.prog.has_sql = True
                    self._sql(block, toks[i].line)
                elif kind in ("DLI", "SQLIMS"):
                    self.prog.has_dli = True
                i = j + 1
                continue
            i += 1

    def _arg_values(self, raw: list[Tok]) -> list[str]:
        """values of a CICS option operand: a literal, or a resolvable data name."""
        if len(raw) == 1 and raw[0].kind == "L":
            return [literal_value(raw[0].text).strip().upper()]
        if raw and raw[0].kind == "W":
            return sorted(v.upper() for v in self.resolve(raw[0].up))
        return []

    def _cics(self, block: list[Tok], line: int) -> None:
        p = self.prog
        if not block:
            return
        cmd = block[0].up
        if len(block) > 1 and block[1].kind == "W":
            second = block[1].up
            if cmd in _CICS_JOIN_ANY or (cmd in _CICS_JOIN_FIRST and second in _CICS_SUBCOMMANDS):
                cmd = f"{cmd} {second}"
        args: dict[str, str] = {}
        raw_args: dict[str, list[Tok]] = {}
        k = 1
        while k < len(block):
            t = block[k]
            if t.kind == "W":
                if k + 1 < len(block) and block[k + 1].text == "(":
                    end = _skip_parens(block, k + 1)
                    inner = block[k + 2:end - 1]
                    args[t.up] = _join(inner)
                    raw_args[t.up] = inner
                    k = end
                    continue
                args.setdefault(t.up, "")
            k += 1
        resolved: dict[str, list[str]] = {}
        for key in _CICS_RESOLVE_ARGS:
            if key in raw_args:
                vals = self._arg_values(raw_args[key])
                if vals:
                    resolved[key] = vals
        verb = block[0].up
        if verb in ("LINK", "XCTL") and "PROGRAM" in raw_args:
            kind = "cics-link" if verb == "LINK" else "cics-xctl"
            self._cics_transfer(raw_args["PROGRAM"], kind, line, resolved.get("PROGRAM"))
            return
        if verb == "START" and "TRANSID" in raw_args:
            self._cics_transfer(raw_args["TRANSID"], "cics-start", line, resolved.get("TRANSID"))
            for v in resolved.get("TRANSID", []):
                _add_unique(p.transactions, v)
            return
        use = CicsUse(command=cmd, args=args, line=line)
        if resolved:
            use.resolved = resolved
        p.cics.append(use)
        if verb == "RETURN":
            for v in resolved.get("TRANSID", []):
                _add_unique(p.transactions, v)
        if verb in ("SEND", "RECEIVE") and ("MAP" in args or "MAPSET" in args):
            p.uses_screen_io = True
            for v in resolved.get("MAPSET") or resolved.get("MAP") or []:
                _add_unique(p.maps, v)
        if verb in _CICS_FILE_CMDS:
            key = "FILE" if "FILE" in raw_args else "DATASET" if "DATASET" in raw_args else ""
            if key:
                names = resolved.get(key) or [_join(raw_args[key]).upper()]
                for name in names:
                    fu = next((f for f in p.files if f.via == "cics" and f.name == name), None)
                    if fu is None:
                        fu = FileUse(name=name, via="cics")
                        p.files.append(fu)
                    _add_unique(fu.modes, verb)

    def _cics_transfer(self, raw: list[Tok], kind: str, line: int, vals: list[str] | None):
        if len(raw) == 1 and raw[0].kind == "L":
            target = literal_value(raw[0].text).strip().upper()
            self.prog.calls.append(CallSite(target=target, kind=kind, line=line))
        elif raw and raw[0].kind == "W":
            if kind == "cics-start":
                via = raw[0].up
                for v in vals or [via]:
                    self.prog.calls.append(CallSite(target=v, kind=kind, line=line, via=via))
            else:
                self._dynamic(raw[0].up, kind, line, [])

    def _sql(self, block: list[Tok], line: int) -> None:
        p = self.prog
        if not block:
            return
        op = block[0].up
        if op == "INCLUDE":
            member = _norm_member(literal_value(block[1].text)) if len(block) > 1 else ""
            if member and member not in _SQL_NOT_COPYBOOK:
                p.sql.append(SqlUse(op="INCLUDE", member=member, line=line))
                self.copy_refs.append((line, member))
            return
        if op == "DECLARE" and len(block) > 2:
            if any(t.up == "CURSOR" for t in block[:5]):
                tables = _sql_tables(block)
                self.cursors[block[1].up] = tables
                p.sql.append(SqlUse(op="DECLARE-CURSOR", tables=tables, line=line))
                return
            elif any(t.up == "TABLE" for t in block[:4]):
                p.sql.append(SqlUse(op="DECLARE-TABLE", tables=[_table_name(block[1].up)],
                                    line=line))
                return
        tables = _sql_tables(block)
        if op in ("OPEN", "FETCH", "CLOSE") and len(block) > 1:
            rest = [t.up for t in block[1:6] if t.kind == "W"]
            names = [u for u in rest if u in self.cursors]
            tables = list(self.cursors[names[0]]) if names else []
        p.sql.append(SqlUse(op=op, tables=tables, line=line))

    # -- OPEN modes --

    def _opens(self) -> None:
        native = {f.name: f for f in self.prog.files if f.via == "native"}
        if not native:
            return
        toks = self.toks
        n = len(toks)
        for i, t in enumerate(toks):
            if self.exec_mask[i]:
                continue
            if t.up == "OPEN":
                mode = ""
                j = i + 1
                while j < n and toks[j].kind == "W":
                    u = toks[j].up
                    if u in ("INPUT", "OUTPUT", "I-O", "EXTEND"):
                        mode = u
                    elif u in native and mode:
                        _add_unique(native[u].modes, mode)
                    elif u not in ("WITH", "NO", "REWIND", "LOCK", "SHARING", "REVERSED"):
                        break
                    j += 1
            elif t.up in ("SORT", "MERGE"):
                mode = ""
                j = i + 1
                while j < n and toks[j].kind != "P":
                    u = toks[j].up
                    if u == "USING":
                        mode = "INPUT"
                    elif u == "GIVING":
                        mode = "OUTPUT"
                    elif u in native and mode:
                        _add_unique(native[u].modes, mode)
                    elif mode and toks[j].kind == "W" and u in _RESERVED:
                        break
                    j += 1

    # -- wrap-up --

    def _finish(self) -> None:
        p = self.prog
        seen: list[str] = []
        for _, member in sorted(self.copy_refs, key=lambda x: x[0]):
            if member and member not in seen:
                seen.append(member)
        p.copybooks = seen
        # IMS message programs get their PCBs through ENTRY 'DLITCBL' USING instead
        p.is_batch_main = not p.using_params and not p.has_cics and \
            "DLITCBL" not in p.entry_points
        if p.unit_kind == "function":  # invoked as FUNCTION x(...), never run as a main
            p.is_batch_main = False
        nested = set(self.nested)
        for c in p.calls:
            if c.target in nested:
                c.resolved = True


# -- control-flow hazards -------------------------------------------------

_SCOPES = {"IF": "END-IF", "EVALUATE": "END-EVALUATE", "SEARCH": "END-SEARCH",
           "PERFORM": "END-PERFORM"}
_INLINE_PERFORM_WORDS = {"UNTIL", "VARYING", "WITH", "TEST", "FOREVER"}
_TERMINAL_WORDS = {"GOBACK", "GO", "XCTL", "ABEND", "CEE3ABD", "ILBOABN0"}
_TRIVIAL_WORDS = {"EXIT", "CONTINUE", "SECTION"}
_MAX_HAZARDS = 40


@dataclass
class _Scope:
    kind: str  # IF | EVALUATE | SEARCH | PERFORM (inline)
    line: int
    end_idx: int | None = None  # body index of the explicit END-xxx, None if implicit
    end_line: int = 0


class _FlowScan:
    """control-flow traps in a PROCEDURE DIVISION token stream (see flow_hazards)."""

    def __init__(self, body: list[Tok], headers: list[tuple[int, Paragraph]],
                 exec_keys: set[tuple[int, int]]):
        self.body = body
        self.skip = [(t.li, t.pos) in exec_keys for t in body]
        self.headers = headers
        self.names = {h.name for _, h in headers}
        self.first: dict[str, int] = {}
        for i, (_, h) in enumerate(headers):
            self.first.setdefault(h.name, i)
        # section index each header belongs to (-1: none)
        self.section_of: list[int] = []
        cur = -1
        for i, (_, h) in enumerate(headers):
            if h.kind == "section":
                cur = i
            self.section_of.append(cur)

    # -- helpers --

    def _next(self, k: int) -> int | None:
        k += 1
        while k < len(self.body) and self.skip[k]:
            k += 1
        return k if k < len(self.body) else None

    def _segment(self, k: int) -> int:
        """header index whose paragraph contains body index k (-1: before the first)."""
        seg = -1
        for i, (hk, _) in enumerate(self.headers):
            if hk > k:
                break
            seg = i
        return seg

    def _inline_perform(self, k: int) -> bool:
        j = self._next(k)
        if j is None or self.body[j].kind != "W":
            return False
        a = self.body[j]
        if a.up in self.names:
            return False
        if a.up in _INLINE_PERFORM_WORDS:
            return True
        after = self._next(j)
        if after is not None and self.body[after].up == "TIMES":
            return True
        # a verb starts the inline body; any other word is an (unknown) procedure name
        return a.up in _RESERVED or a.up in ("COMPUTE", "INITIALIZE", "EXHIBIT")

    def _section_end(self, i: int) -> int:
        """last header index inside the section starting at header i."""
        j = i + 1
        while j < len(self.headers) and self.headers[j][1].kind != "section":
            j += 1
        return j - 1

    def _trivial(self, i: int) -> bool:
        """header i's paragraph holds nothing but EXIT / CONTINUE."""
        lo = self.headers[i][0] + 1
        hi = self.headers[i + 1][0] if i + 1 < len(self.headers) else len(self.body)
        words = {t.up for t in self.body[lo:hi] if t.kind == "W"}
        return words <= _TRIVIAL_WORDS

    def _terminal_paragraph(self, name: str) -> bool:
        i = self.first[name]
        lo = self.headers[i][0]
        h = self.headers[i][1]
        last = self._section_end(i) if h.kind == "section" else i
        hi = self.headers[last + 1][0] if last + 1 < len(self.headers) else len(self.body)
        return _ends_run(self.body[lo:hi])

    # -- scans --

    def hazards(self) -> list[str]:
        out: list[str] = []
        out += self._next_sentence()
        ranges, thru = self._ranges()
        out += thru
        out += self._go_to(ranges)
        out += self._alter()
        out += self._fall_through(ranges)
        if len(out) > _MAX_HAZARDS:
            out = out[:_MAX_HAZARDS] + [f"... {len(out) - _MAX_HAZARDS} more flow notes"]
        return out

    def _next_sentence(self) -> list[str]:
        body, out = self.body, []
        stack: list[_Scope] = []
        pending: list[tuple[int, list[_Scope]]] = []  # (line, enclosing scopes)

        def settle(period_idx: int) -> None:
            period_line = body[period_idx].line if period_idx < len(body) else body[-1].line
            for line, scopes in pending:
                closed = [s for s in scopes if s.end_idx is not None]
                if not closed:
                    continue  # plain IF ... NEXT SENTENCE ... . : same as CONTINUE
                inner = closed[-1]
                rest = [
                    x for x in range(inner.end_idx + 1, min(period_idx, len(body)))
                    if not self.skip[x]
                ]
                if not rest:
                    continue  # END-xxx directly followed by the period
                desc = (f"{inner.kind} (line {inner.line}) ... {_SCOPES[inner.kind]} "
                        f"(line {inner.end_line})")
                if inner.kind == "PERFORM":
                    desc = f"inline PERFORM (lines {inner.line}-{inner.end_line})"
                loops = [s for s in closed if s.kind == "PERFORM"]
                if loops and loops[-1] is not inner:
                    lp = loops[-1]
                    desc += f" within inline PERFORM (lines {lp.line}-{lp.end_line})"
                if loops:
                    lp = loops[-1]
                    what = (f"control jumps past the next period (line {period_line}), so it "
                            f"leaves the PERFORM loop (END-PERFORM at line {lp.end_line}): the "
                            "loop ends at once and the rest of the loop body is skipped")
                else:
                    what = (f"control jumps past the next period (line {period_line}), "
                            f"skipping the statements after {_SCOPES[inner.kind]} "
                            f"(line {inner.end_line})")
                out.append(f"line {line}: NEXT SENTENCE inside {desc} — {what}; NEXT "
                           "SENTENCE is not CONTINUE")
            pending.clear()
            stack.clear()

        for k, t in enumerate(body):
            if self.skip[k]:
                continue
            if t.kind == "P":
                settle(k)
                continue
            if t.kind != "W":
                continue
            u = t.up
            if u in ("IF", "EVALUATE", "SEARCH"):
                stack.append(_Scope(u, t.line))
            elif u == "PERFORM" and self._inline_perform(k):
                stack.append(_Scope("PERFORM", t.line))
            elif u in ("END-IF", "END-EVALUATE", "END-SEARCH", "END-PERFORM"):
                kind = u[4:]
                if any(s.kind == kind for s in stack):
                    while stack:
                        s = stack.pop()
                        s.end_idx, s.end_line = k, t.line
                        if s.kind == kind:
                            break
            elif u == "NEXT":
                j = self._next(k)
                if j is not None and body[j].up == "SENTENCE":
                    pending.append((t.line, list(stack)))
        if pending:
            settle(len(body))
        return out

    def _ranges(self) -> tuple[list[tuple[int, int, str, int]], list[str]]:
        """PERFORMed ranges (first header, last header, label, line) + THRU notes."""
        body, ranges, notes = self.body, [], []
        seen: set[tuple[str, str]] = set()
        for k, t in enumerate(body):
            if self.skip[k] or t.up != "PERFORM" or t.kind != "W":
                continue
            j = self._next(k)
            if j is None or body[j].up not in self.first:
                continue
            a = body[j].up
            b = a
            j2 = self._next(j)
            if j2 is not None and body[j2].up in ("THRU", "THROUGH"):
                j3 = self._next(j2)
                if j3 is not None and body[j3].up in self.first:
                    b = body[j3].up
            ia, ib = self.first[a], self.first[b]
            ha, hb = self.headers[ia][1], self.headers[ib][1]
            end = self._section_end(ib) if hb.kind == "section" else ib
            label = a if a == b else f"{a} THRU {b}"
            if a != b and ib < ia:
                if (a, b) not in seen:
                    seen.add((a, b))
                    notes.append(f"line {t.line}: PERFORM {a} THRU {b}, but {b} comes before "
                                 f"{a} in the source — the range is not what it looks like")
                continue
            ranges.append((ia, end, label, t.line))
            if a != b and (a, b) not in seen:
                seen.add((a, b))
                inner = [h.name for i, (_, h) in enumerate(self.headers[ia + 1:ib], ia + 1)
                         if (ha.kind != "section" or h.kind == "section")
                         and not self._trivial(i)]
                if inner:
                    shown = ", ".join(inner[:8]) + (" ..." if len(inner) > 8 else "")
                    notes.append(f"line {t.line}: PERFORM {a} THRU {b} also runs the "
                                 f"{len(inner)} paragraph(s) between them by fall-through "
                                 f"({shown})")
        return ranges, notes

    def _go_to(self, ranges: list[tuple[int, int, str, int]]) -> list[str]:
        body = self.body
        grouped: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for k, t in enumerate(body):
            if self.skip[k] or t.up != "GO" or t.kind != "W":
                continue
            j = self._next(k)
            if j is not None and body[j].up == "TO":
                j = self._next(j)
            targets: list[str] = []
            while j is not None and body[j].kind == "W" and body[j].up in self.first:
                targets.append(body[j].up)
                j = self._next(j)
            if not targets:
                continue
            seg = self._segment(k)
            inside = [r for r in ranges if r[0] <= seg <= r[1]] if seg >= 0 else []
            if not inside:
                continue
            for tgt in targets:
                it = self.first[tgt]
                if any(r[0] <= it <= r[1] for r in inside):
                    continue
                rng = min(inside, key=lambda r: (r[1] - r[0], r[3]))
                grouped.setdefault((tgt, rng[2]), []).append((t.line, self.headers[seg][1].name))
        out = []
        for (tgt, label), sites in grouped.items():
            lines = ", ".join(str(ln) for ln, _ in sites[:6]) + (" ..." if len(sites) > 6 else "")
            paras = ", ".join(dict.fromkeys(p for _, p in sites))
            out.append(f"line {lines}: GO TO {tgt} in {paras} leaves the PERFORMed range "
                       f"{label} — control does not come back to the PERFORM statement")
        return out

    def _alter(self) -> list[str]:
        body, out = self.body, []
        for k, t in enumerate(body):
            if self.skip[k] or t.up != "ALTER" or t.kind != "W":
                continue
            j = self._next(k)
            while j is not None and body[j].kind == "W" and body[j].up not in _RESERVED:
                src = body[j].up
                j = self._next(j)
                if j is None or body[j].up != "TO":
                    break
                j = self._next(j)
                if j is not None and body[j].up == "PROCEED":
                    j = self._next(j)
                    if j is not None and body[j].up == "TO":
                        j = self._next(j)
                if j is None or body[j].kind != "W":
                    break
                dst = body[j].up
                out.append(f"line {t.line}: ALTER {src} TO PROCEED TO {dst} — the GO TO in "
                           f"{src} is changed at run time; after this statement it goes to {dst}")
                j = self._next(j)
                if j is not None and body[j].text == ",":
                    j = self._next(j)
        return out

    def _fall_through(self, ranges: list[tuple[int, int, str, int]]) -> list[str]:
        """main line that does not end with STOP RUN / GOBACK / GO TO / CICS RETURN."""
        body, headers = self.body, self.headers
        if not headers:
            return []
        start = 0
        for k, t in enumerate(body):
            if t.up == "DECLARATIVES" and k > 0 and body[k - 1].up == "END":
                start = k + 1
        first_h = next((i for i, (hk, _) in enumerate(headers) if hk >= start), None)
        if first_h is None:
            return []
        lead = [t for t in body[start:headers[first_h][0]] if t.kind != "P"]
        if lead:
            name, lo, hi, nxt = "PROCEDURE DIVISION entry code", start, headers[first_h][0], \
                first_h
        else:
            h = headers[first_h][1]
            last = self._section_end(first_h) if h.kind == "section" else first_h
            if last + 1 >= len(headers):
                return []
            name = f"{h.name}{' SECTION' if h.kind == 'section' else ''}"
            lo, hi, nxt = headers[first_h][0], headers[last + 1][0], last + 1
        if nxt >= len(headers):
            return []
        sentences: list[list[Tok]] = [[]]
        for t in body[lo:hi]:
            if t.kind == "P":
                sentences.append([])
            else:
                sentences[-1].append(t)
        for st in reversed(sentences):
            words = {t.up for t in st if t.kind == "W"}
            if not words or words <= (self.names | _TRIVIAL_WORDS):
                continue
            if _ends_run(st):
                return []
            # PERFORM 9000-TERMINATION as the last statement, where that paragraph ends the run
            performed = [st[x + 1].up for x in range(len(st) - 1)
                         if st[x].up == "PERFORM" and st[x + 1].up in self.first]
            if any(self._terminal_paragraph(n) for n in performed[-1:]):
                return []
            break
        else:
            return []
        target = headers[nxt][1]
        again = any(r[0] <= nxt <= r[1] for r in ranges)
        if lead and not again:
            return []  # unnamed entry code running on into the first paragraph: the norm
        line = body[hi - 1].line if hi > 0 else 0
        return [f"line {line}: the main line ({name}) does not end with STOP RUN, GOBACK, "
                f"GO TO or a CICS RETURN — control falls through into {target.name} "
                f"(line {target.line})" + (", which is also PERFORMed elsewhere" if again
                                            else "")]


def _ends_run(toks: list[Tok]) -> bool:
    """tokens hold a statement that ends the run / leaves the program (or a GO TO)."""
    words = {t.up for t in toks if t.kind == "W"}
    words |= {literal_value(t.text).strip().upper() for t in toks if t.kind == "L"}
    ups = [t.up for t in toks]
    return bool(words & _TERMINAL_WORDS) or "STOP" in words or \
        ("EXIT" in words and "PROGRAM" in words) or ("EXEC" in ups and "RETURN" in ups)


# -- helpers --------------------------------------------------------------


def _period(t: Tok) -> Tok:
    return Tok("P", ".", ".", t.line, t.li, t.end, t.end + 1)


def _descendants(span: _ProgSpan, spans: list[_ProgSpan]) -> list[int]:
    out: list[int] = []
    todo = list(span.children)
    while todo:
        c = todo.pop()
        out.append(c)
        todo.extend(spans[c].children)
    return out


def _add_unique(lst: list[str], v: str) -> None:
    if v and v not in lst:
        lst.append(v)


def _join(toks: list[Tok]) -> str:
    out = ""
    prev: Tok | None = None
    for t in toks:
        if prev is not None and not (prev.li == t.li and prev.end == t.pos):
            out += " "
        out += t.text
        prev = t
    return out


def _name_list(toks: list[Tok], j: int) -> list[str]:
    """data names starting at j (qualifiers and subscripts skipped)."""
    names: list[str] = []
    n = len(toks)
    while j < n:
        t = toks[j]
        if t.text == "(":
            j = _skip_parens(toks, j)
            continue
        if t.up in ("OF", "IN"):
            j += 2
            continue
        if t.kind == "S" and t.text in (",", ";"):
            j += 1
            continue
        if t.kind != "W" or t.up in _RESERVED or t.up.startswith("END-"):
            break
        names.append(t.up)
        j += 1
    return names


def _using_args(toks: list[Tok], j: int) -> list[str]:
    n = len(toks)
    if j >= n or toks[j].up != "USING":
        return []
    j += 1
    args: list[str] = []
    while j < n:
        t = toks[j]
        u = t.up
        if u in ("BY", "REFERENCE", "CONTENT", "VALUE", "OMITTED"):
            j += 1
            continue
        if u in ("ADDRESS", "LENGTH") and j + 1 < n and toks[j + 1].up == "OF":
            j += 2
            continue
        if u in ("OF", "IN"):
            j += 2
            continue
        if t.text == "(":
            j = _skip_parens(toks, j)
            continue
        if t.kind == "L" or (t.kind == "S" and t.text in (",", ";")):
            j += 1
            continue
        if t.kind != "W" or u in _RESERVED or u.startswith("END-"):
            break
        _add_unique(args, u)
        j += 1
    return args


def _parse_select(st: list[Tok]) -> FileUse:
    n = len(st)
    k = 1
    if k < n and st[k].up == "OPTIONAL":
        k += 1
    fu = FileUse(name=st[k].up if k < n else "")
    k += 1
    while k < n:
        u = st[k].up
        if u == "ASSIGN":
            k += 1
            while k < n and st[k].up in ("TO", "USING", "EXTERNAL", "DYNAMIC", "IS"):
                k += 1
            if k < n:
                t = st[k]
                if t.up in ("DISK", "PRINTER", "KEYBOARD", "DISPLAY") and k + 1 < n and \
                        st[k + 1].kind == "L":
                    t = st[k + 1]
                    k += 1
                fu.assign = literal_value(t.text) if t.kind == "L" else _chunk(st, k)[0]
                if t.kind != "L":
                    k = _chunk(st, k)[1]
        elif u == "ORGANIZATION":
            k += 1
            while k < n and st[k].up == "IS":
                k += 1
            if k < n:
                if st[k].up in ("LINE", "RECORD") and k + 1 < n:
                    fu.organization = f"{st[k].up} {st[k + 1].up}"
                    k += 1
                else:
                    fu.organization = st[k].up
        elif u in ("INDEXED", "RELATIVE") and not fu.organization:
            fu.organization = u
        elif u == "LINE" and k + 1 < n and st[k + 1].up == "SEQUENTIAL" and not fu.organization:
            fu.organization = "LINE SEQUENTIAL"
            k += 1
        elif u == "ACCESS":
            k += 1
            while k < n and st[k].up in ("MODE", "IS"):
                k += 1
            if k < n:
                fu.access = st[k].up
        elif u == "RECORD" and k + 1 < n and st[k + 1].up == "KEY" and \
                (k == 0 or st[k - 1].up != "ALTERNATE"):
            k += 2
            while k < n and st[k].up == "IS":
                k += 1
            if k < n:
                fu.record_key = st[k].up
        elif u == "SEQUENTIAL" and not fu.organization:
            fu.organization = "SEQUENTIAL"
        k += 1
    return fu


def _table_name(name: str) -> str:
    return name.strip('"').upper()


def _sql_tables(block: list[Tok]) -> list[str]:
    """table names referenced by one SQL statement (host variables ignored)."""
    tables: list[str] = []
    n = len(block)
    groups: list[bool] = []  # per open paren: does it start a sub-select?
    k = 0
    op = block[0].up if block else ""

    def take_list(j: int) -> int:
        """read 'tab [alias] [, tab [alias]]...' starting at j."""
        while j < n:
            t = block[j]
            if t.text == "(":
                return j
            if t.kind != "W" or t.up in _SQL_TABLE_STOP:
                return j
            if j > 0 and block[j - 1].text == ":":
                return j
            _add_unique(tables, _table_name(t.up))
            j += 1
            if j < n and block[j].kind == "W" and block[j].up not in _SQL_TABLE_STOP:
                j += 1  # alias
            if j < n and block[j].text == ",":
                j += 1
                continue
            return j
        return j

    while k < n:
        t = block[k]
        if t.text == "(":
            groups.append(k + 1 < n and block[k + 1].up == "SELECT")
        elif t.text == ")":
            if groups:
                groups.pop()
        elif t.kind == "W":
            in_sub = not groups or groups[-1]
            u = t.up
            prev_colon = k > 0 and block[k - 1].text == ":"
            if prev_colon:
                pass
            elif u == "FROM" and in_sub:
                k = take_list(k + 1)
                continue
            elif u == "JOIN" and in_sub:
                k = take_list(k + 1)
                continue
            elif u == "INTO" and op in ("INSERT", "MERGE") and in_sub:
                k = take_list(k + 1)
                continue
            elif u == "UPDATE" and k == 0:
                k = take_list(k + 1)
                continue
            elif u == "TABLE" and op in ("LOCK", "TRUNCATE"):
                k = take_list(k + 1)
                continue
        k += 1
    return tables


def _clean_comment_block(lines: list[str], limit: int = 40) -> str:
    out: list[str] = []
    for raw in lines:
        s = raw.strip().strip("*").strip()
        if not s or _BORDER_RE.match(s):
            continue
        if _LICENSE_RE.search(s):
            continue
        if out and out[-1] == s:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return "\n".join(out)


# -- public API -----------------------------------------------------------


def parse_program_file(
    rel_path: str, text: str, copybook_lookup: CopybookLookup | None = None
) -> list[CobolProgram]:
    """one CobolProgram per top-level PROGRAM-ID; nested programs are listed in their parent.

    *copybook_lookup* (member name -> text or None) lets the parser expand COPY / SQL INCLUDE
    in the DATA DIVISION, so data items and VALUE clauses from copybooks are visible.
    """
    ctx = _Ctx(rel_path, text, copybook_lookup)
    spans = _find_programs(ctx.toks)
    tops = [s for s in spans if s.parent < 0]
    return [_ProgramParser(ctx, s, spans, len(tops)).parse() for s in tops]


_LEVEL_LINE_RE = re.compile(r"^\s*(0?[1-9]|[1-4][0-9]|66|77|78|88)\s+[A-Za-z0-9:]")
_COBOL_CLAUSE_RE = re.compile(
    r"\b(PIC|PICTURE|VALUE|REDEFINES|OCCURS|COMP(?:-\d)?|USAGE|FILLER)\b|\.\s*$", re.IGNORECASE
)
_PROC_RE = re.compile(
    r"^\s*(MOVE|PERFORM|IF|EVALUATE|DISPLAY|CALL|COMPUTE|ADD|SUBTRACT|SET|GO\s+TO|EXEC\s+(?:SQL|CICS)|"
    r"INITIALIZE|STRING|UNSTRING|READ|WRITE|OPEN|CLOSE)\b",
    re.IGNORECASE,
)


def is_copybook_text(text: str) -> bool:
    """heuristic: no IDENTIFICATION / PROGRAM-ID, but data entries or procedure fragments."""
    lines = scan(text)[0]
    if not lines:
        return False
    head = "\n".join(ll.text for ll in lines[:400])
    if re.search(r"\b(PROGRAM-ID|FUNCTION-ID)\b|\b(IDENTIFICATION|ID)\s+DIVISION\b", head,
                 re.IGNORECASE):
        return False
    data = sum(
        1 for ll in lines[:400]
        if _LEVEL_LINE_RE.match(ll.text) and _COBOL_CLAUSE_RE.search(ll.text)
    )
    proc = sum(1 for ll in lines[:400] if _PROC_RE.match(ll.text))
    sql = bool(re.search(r"EXEC\s+SQL\s+DECLARE", head, re.IGNORECASE))
    return data >= 1 or proc >= 2 or sql


def has_procedure_code(text: str) -> bool:
    """at least two lines that start with a procedure-division verb."""
    return sum(1 for ll in _lines(text)[:400] if _PROC_RE.match(ll.text)) >= 2


def copy_members(text: str) -> list[str]:
    """COPY / EXEC SQL INCLUDE members referenced by a source text (e.g. a copybook)."""
    toks = tokenize(_lines(text))
    out: list[str] = []
    for i, t in enumerate(toks[:-1]):
        nt = toks[i + 1]
        if t.up == "COPY" and nt.kind in ("W", "L"):
            _add_unique(out, _norm_member(literal_value(nt.text) if nt.kind == "L" else nt.text))
        elif t.up == "INCLUDE" and i > 0 and toks[i - 1].up == "SQL" and nt.kind in ("W", "L"):
            m = _norm_member(literal_value(nt.text))
            if m not in _SQL_NOT_COPYBOOK:
                _add_unique(out, m)
    return out


def top_level_items(text: str) -> list[str]:
    """level-01/77 names a copybook defines (else the names at its smallest level)."""
    entries = []
    for st in _split_sentences(tokenize(_lines(text))):
        e = _parse_entry(st, "", "")
        if e is not None and e.level not in (88, 66, 78):
            entries.append(e)
    if not entries:
        return []
    top = [e.name for e in entries if e.level in (1, 77)]
    if not top:
        lowest = min(e.level for e in entries)
        top = [e.name for e in entries if e.level == lowest]
    out: list[str] = []
    for name in top:
        if name != "FILLER":
            _add_unique(out, name)
    return out


def sql_declared_tables(text: str) -> list[str]:
    """tables of EXEC SQL DECLARE x TABLE (DCLGEN members)."""
    return [
        _table_name(m.group(1))
        for m in re.finditer(r"EXEC\s+SQL\s+DECLARE\s+([\w.\"]+)\s+TABLE", text, re.IGNORECASE)
    ]


@lru_cache(maxsize=512)
def _lines(text: str) -> list[LogicalLine]:
    return scan(text)[0]
