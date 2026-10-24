"""EXEC precompile API and the neutraliser of the docgen compile gate.

`precompile` rewrites the EXEC CICS / SQL / DLI blocks of a program so that GnuCOBOL can
compile it: PROCEDURE DIVISION blocks become `rewrite(block)` (the neutraliser:
CONTINUE), DATA DIVISION SQL is blanked, EXEC SQL INCLUDE becomes COPY, DFHRESP(x) and
DFHVALUE(x) become 0, the CICS/SQL/DLI/MQ system copybooks resolve to the stub copybooks
of codocs/cobol/stubs, implicit EIB / SQLCA / DIB blocks are inserted, and missing BMS
symbolic maps are generated from the mapset. It never feeds an executed test.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from codocs.cobol.parser import Tok, tokenize
from codocs.cobol.source import detect_format, logical_lines

__all__ = ["ExecBlock", "Precompiled", "STUB_DIR", "scan_exec", "precompile", "neutralize",
           "neutral_rewrite", "stub_members"]

STUB_DIR = Path(__file__).resolve().parent / "stubs"
CODE_END = 72
_TWO_WORD = {"SEND", "RECEIVE", "WRITEQ", "READQ", "DELETEQ", "HANDLE", "IGNORE", "PUSH",
             "POP", "START"}
_SECOND_WORDS = {"MAP", "TEXT", "CONTROL", "PAGE", "TS", "TD", "BROWSE", "CONDITION",
                 "ABEND", "AID", "LABEL"}
_RESP_RE = re.compile(r"\bDFH(?:RESP|VALUE)\s*\(\s*[A-Za-z0-9-]+\s*\)", re.I)
_EIB_RE = re.compile(r"^EIB[A-Z0-9]+$")
_SQLCA_NAMES = re.compile(r"^(?:SQLCODE|SQLSTATE|SQLERRM|SQLERRD|SQLWARN[0-9A-Z]*)$")
_DIB_RE = re.compile(r"^DIB[A-Z0-9]+$")
_COPY_EXTS = ("", ".cpy", ".copy", ".cbl", ".cob", ".dcl", ".dclgen", ".cpb", ".cblcpy",
              ".cpybk", ".copybook")
_SECTION_RE = re.compile(r"^\s*(WORKING-STORAGE|LOCAL-STORAGE|LINKAGE|FILE|REPORT|SCREEN|"
                         r"COMMUNICATION)\s+SECTION\s*\.", re.I)
_PROC_RE = re.compile(r"^\s*PROCEDURE\s+DIVISION\b", re.I)
_DATA_RE = re.compile(r"^\s*DATA\s+DIVISION\b", re.I)


@dataclass
class ExecBlock:
    kind: str                 # CICS | SQL | DLI
    command: str              # first word(s): READ, SEND MAP, INCLUDE, DECLARE, SELECT, GU, ...
    options: dict[str, str]   # option -> argument text (MAP('X'), RESP(WS-RESP), INTO(...), ...)
    division: str             # DATA | PROCEDURE
    start_line: int           # physical lines, period included
    end_line: int
    start_col: int = -1       # 0-based column of EXEC on start_line (-1: unknown)
    end_col: int = -1         # 0-based column just past END-EXEC (or its period)


@dataclass
class Precompiled:
    text: str
    include_dirs: list[Path]  # stub dir + generated dir (BMS maps)
    line_map: list[int]       # new line -> original line (insertions map to the line they precede)
    blocks: list[ExecBlock]
    notes: list[str] = field(default_factory=list)
    # searched before the repository's copybook dirs: neutralised copies of COPYed members
    # that hold EXEC blocks (DCLGEN members)
    override_dirs: list[Path] = field(default_factory=list)


def neutral_rewrite(block: ExecBlock) -> list[str]:
    """the neutraliser: every PROCEDURE DIVISION EXEC block becomes CONTINUE."""
    return ["CONTINUE"]


def stub_members() -> set[str]:
    """upper-cased member names the stub copybooks provide."""
    return {p.stem.upper() for p in STUB_DIR.glob("*.cpy")}


# -- scanning --------------------------------------------------------------------------------


def _col(t: Tok, lines: list[str], fixed: bool) -> int:
    """0-based physical column of a token, -1 when it is not verbatim on its line."""
    n = t.line - 1
    if n < 0 or n >= len(lines):
        return -1
    col = (7 + t.pos) if fixed else t.pos
    return col if lines[n][col:col + len(t.text)] == t.text else -1


def _options(toks: list[Tok]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "W" and i + 1 < len(toks) and toks[i + 1].text == "(":
            depth = 0
            j = i + 1
            parts: list[str] = []
            while j < len(toks):
                if toks[j].text == "(":
                    depth += 1
                    if depth > 1:
                        parts.append("(")
                elif toks[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                    parts.append(")")
                else:
                    parts.append(toks[j].text)
                j += 1
            out.setdefault(t.up, " ".join(parts))
            i = j + 1
            continue
        if t.kind == "W":
            out.setdefault(t.up, "")
        i += 1
    return out


def scan_exec(text: str, fmt: str | None = None, *, division: str = "PROCEDURE"
              ) -> list[ExecBlock]:
    """every EXEC ... END-EXEC block, from the parser tokens (*division*: the division of
    text before any DIVISION header, e.g. of a copybook)."""
    default = division
    fmt = fmt or detect_format(text)
    fixed = fmt == "fixed"
    lines = text.split("\n")
    toks = tokenize(logical_lines(text, fmt))
    out: list[ExecBlock] = []
    division = ""
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1].up if i + 1 < len(toks) else ""
        if t.kind == "W" and nxt == "DIVISION" and t.up in ("IDENTIFICATION", "ID",
                                                            "ENVIRONMENT", "DATA",
                                                            "PROCEDURE"):
            division = t.up if t.up in ("DATA", "PROCEDURE") else ""
        if t.up != "EXEC" or t.kind != "W" or i + 1 >= len(toks):
            i += 1
            continue
        kind = toks[i + 1].up
        if kind not in ("CICS", "SQL", "DLI"):
            i += 1
            continue
        j = i + 2
        while j < len(toks) and toks[j].up != "END-EXEC":
            j += 1
        body = toks[i + 2:j]
        end_tok = toks[j] if j < len(toks) else toks[-1]
        end_line, end_col = end_tok.line, _col(end_tok, lines, fixed)
        if end_col >= 0:
            end_col += len(end_tok.text)
        if j + 1 < len(toks) and toks[j + 1].kind == "P" and toks[j + 1].line == end_tok.line:
            pc = _col(toks[j + 1], lines, fixed)
            if pc >= 0 and end_col >= 0:
                end_col = pc + 1
        words = [b.up for b in body if b.kind == "W"]
        command = words[0] if words else ""
        if kind == "CICS" and command in _TWO_WORD and len(body) > 1 and \
                body[1].kind == "W" and (body[1].up in _SECOND_WORDS
                                         or not (len(body) > 2 and body[2].text == "(")):
            command = f"{command} {body[1].up}"
        opts = _options(body)
        if kind == "SQL" and command == "INCLUDE" and len(words) > 1:
            opts = {"MEMBER": words[1]}
        out.append(ExecBlock(kind=kind, command=command, options=opts,
                             division=division or default, start_line=t.line,
                             end_line=end_line, start_col=_col(t, lines, fixed),
                             end_col=end_col))
        i = j + 1
    return out


# -- copybook resolution ----------------------------------------------------------------------


def _resolver(dirs: list[Path]) -> Callable[[str], Path | None]:
    cache: dict[Path, dict[str, Path]] = {}

    def listing(d: Path) -> dict[str, Path]:
        if d not in cache:
            names: dict[str, Path] = {}
            try:
                for p in d.iterdir():
                    if p.is_file():
                        names.setdefault(p.name.upper(), p)
            except OSError:
                pass
            cache[d] = names
        return cache[d]

    def find(member: str) -> Path | None:
        up = member.upper().strip("'\"")
        for d in dirs:
            names = listing(Path(d))
            for ext in _COPY_EXTS:
                hit = names.get(up + ext.upper())
                if hit is not None:
                    return hit
        return None

    return find


# -- BMS symbolic maps ---------------------------------------------------------------------


def bms_copybook(text: str, mapset: str) -> str | None:
    """the standard symbolic-map copybook of a BMS mapset source (I and O records)."""
    from codocs.cobol.bms import _statements

    maps: list[tuple[str, list[tuple[str, int]]]] = []
    for st in _statements(text):
        m = re.match(r"^([A-Za-z@#$][\w@#$]*)?\s+(DFHMSD|DFHMDI|DFHMDF)\b(.*)$", st, re.I)
        if not m:
            continue
        label, macro, rest = (m.group(1) or "").upper(), m.group(2).upper(), m.group(3)
        if macro == "DFHMDI" and label:
            maps.append((label, []))
        elif macro == "DFHMDF" and label and maps:
            ln = re.search(r"\bLENGTH=(\d+)", rest, re.I)
            maps[-1][1].append((label, int(ln.group(1)) if ln else 1))
    if not maps:
        return None
    out = [f"      * {mapset}: symbolic map generated from the BMS mapset (compile gate)"]
    for name, fields in maps:
        out.append(f"       01  {name}I.")
        out.append("           02  FILLER PIC X(12).")
        for f, n in fields:
            out += [f"           02  {f}L PIC S9(4) COMP.", f"           02  {f}F PIC X.",
                    f"           02  FILLER REDEFINES {f}F.", f"               03  {f}A PIC X.",
                    f"           02  {f}I PIC X({n})."]
        out.append(f"       01  {name}O REDEFINES {name}I.")
        out.append("           02  FILLER PIC X(12).")
        for f, n in fields:
            out += ["           02  FILLER PIC X(3).", f"           02  {f}O PIC X({n})."]
    return "\n".join(out) + "\n"


# -- rewriting -------------------------------------------------------------------------------


def _blank_code(line: str, fixed: bool, start: int = -1, end: int = -1) -> str:
    """the line with its code area (or [start, end)) blanked; cols 1-6 and 73+ kept."""
    if not fixed:
        a = 0 if start < 0 else start
        b = len(line) if end < 0 else end
        return (line[:a] + " " * max(0, min(b, len(line)) - a) + line[b:]).rstrip()
    lo = 7 if start < 0 else max(start, 7)
    hi = CODE_END if end < 0 else min(end, CODE_END)
    if len(line) < hi:
        line = line.ljust(hi)
    out = line[:lo] + " " * (hi - lo) + line[hi:]
    if len(out) > 6 and start < 0:
        out = out[:6] + " " + out[7:]
    return out.rstrip() if len(line) <= CODE_END else out


def _put(line: str, col: int, text: str, fixed: bool) -> str:
    """text written over the (blank) code area of a line starting at col."""
    end = col + len(text)
    if len(line) < end:
        line = line.ljust(end)
    out = line[:col] + text + line[end:]
    if fixed and len(line) <= CODE_END:
        out = out.rstrip()
    return out


def _code_lines(text: str, fmt: str) -> list[tuple[int, Tok]]:
    return [(t.line, t) for t in tokenize(logical_lines(text, fmt))]


def precompile(text: str, fmt: str | None, copybook_dirs: list[Path], *, index=None,
               rewrite: Callable[[ExecBlock], list[str]] = neutral_rewrite,
               gen_dir: Path | None = None, division: str = "PROCEDURE",
               members: bool = True) -> Precompiled:
    """the program text with every EXEC block rewritten (see the module doc).

    gen_dir: where generated copybooks go (BMS maps, neutralised members; default a temp
    dir); division: the division of text before its first DIVISION header (copybooks);
    members: also neutralise COPYed members that hold EXEC blocks."""
    fmt = fmt or detect_format(text)
    fixed = fmt == "fixed"
    lines = text.split("\n")
    blocks = scan_exec(text, fmt, division=division)
    notes: list[str] = []
    stubs = stub_members()
    find = _resolver([Path(d) for d in copybook_dirs] + [STUB_DIR])
    toks = tokenize(logical_lines(text, fmt))
    added: dict[int, list[str]] = {}       # 0-based line -> lines inserted after it
    before: dict[int, list[str]] = {}      # 0-based line -> lines inserted before it
    indent = "       " if fixed else ""

    def place_block(b: ExecBlock, repl: list[str], keep_period: bool) -> None:
        """blank the block (with the period ending its sentence) and write repl at its
        column; the period follows the last replacement line when keep_period."""
        s, e = b.start_line - 1, b.end_line - 1
        known = b.start_col >= 0 and b.end_col >= 0
        sc = b.start_col if known else (11 if fixed else 0)
        if known:
            ec = b.end_col
            period = lines[e][ec - 1:ec] == "."
            if s == e:
                lines[s] = _blank_code(lines[s], fixed, sc, ec)
            else:
                lines[s] = _blank_code(lines[s], fixed, sc, -1)
                for n in range(s + 1, e):
                    lines[n] = _blank_code(lines[n], fixed)
                lines[e] = _blank_code(lines[e], fixed, 7 if fixed else 0, ec)
        else:
            period = _ends_period(b, text, fmt)
            for n in range(s, e + 1):
                lines[n] = _blank_code(lines[n], fixed)
        if not repl:
            return
        out = list(repl)
        if keep_period and period:
            out[-1] += "."
        room = (CODE_END - sc) if fixed else 1 << 30
        if len(out[0]) <= room:
            lines[s] = _put(lines[s], sc, out[0], fixed)
            out = out[1:]
        col = sc if len(out) and all(len(r) <= room for r in out) else (11 if fixed else 0)
        added.setdefault(s, []).extend(" " * col + r for r in out)

    for b in blocks:
        if b.kind == "SQL" and b.command == "INCLUDE":
            member = b.options.get("MEMBER", "")
            hit = find(member) if member else None
            repl = [f"COPY {member}."] if hit is not None else []
            if hit is None and member:
                notes.append(f"EXEC SQL INCLUDE {member}: member not found")
            place_block(b, repl, keep_period=False)
        elif b.division == "DATA":
            place_block(b, [], keep_period=False)
        else:
            place_block(b, rewrite(b), keep_period=True)

    # DFHRESP(x) / DFHVALUE(x) -> 0 in code lines of the PROCEDURE DIVISION
    proc_line = next((t.line for k, t in enumerate(toks) if t.up == "PROCEDURE"
                      and k + 1 < len(toks) and toks[k + 1].up == "DIVISION"), None)
    if proc_line is not None:
        for n in range(proc_line - 1, len(lines)):
            ln = lines[n]
            if fixed:
                if len(ln) < 8 or ln[6:7] in ("*", "/"):
                    continue
                area = ln[7:CODE_END]
                new = _RESP_RE.sub(lambda m: "0".ljust(len(m.group(0))), area)
                if new != area:
                    lines[n] = ln[:7] + new + ln[CODE_END:]
            else:
                if ln.lstrip().startswith("*>"):
                    continue
                lines[n] = _RESP_RE.sub(lambda m: "0".ljust(len(m.group(0))), ln)

    # system members resolve to the stubs; missing BMS maps are generated
    dirs: list[Path] = [STUB_DIR]
    copies = [toks[k + 1].up.strip("'\"") for k, t in enumerate(toks)
              if t.up == "COPY" and t.kind == "W" and k + 1 < len(toks)]
    gen: Path | None = None
    for member in dict.fromkeys(copies):
        if find(member) is not None:
            continue
        mapset = getattr(index, "mapsets", {}).get(member) if index is not None else None
        if mapset is not None:
            src = _bms_text(index, mapset.path)
            cpy = bms_copybook(src, member) if src else None
            if cpy:
                if gen is None:
                    gen = Path(gen_dir) if gen_dir is not None else \
                        Path(tempfile.mkdtemp(prefix="codocs-bms-"))
                    gen.mkdir(parents=True, exist_ok=True)
                (gen / f"{member}.cpy").write_text(cpy, encoding="utf-8")
                notes.append(f"BMS symbolic map {member} generated from {mapset.path}")
                continue
        if member.startswith("CMQ") and member not in stubs:
            notes.append(f"compile gate off: no stub for {member}")
        else:
            notes.append(f"COPY {member}: member not found")
    if gen is not None:
        dirs.append(gen)

    # COPYed members (DCLGEN) holding EXEC blocks: neutralised copies searched first
    over: list[Path] = []
    if members:
        ov: Path | None = None
        for member, div in _copies_by_division(toks, division):
            hit = find(member)
            if hit is None or hit.parent == STUB_DIR:
                continue
            try:
                body = hit.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _EXEC_RE.search(body):
                continue
            if ov is None:
                base = Path(gen_dir) if gen_dir is not None else \
                    Path(tempfile.mkdtemp(prefix="codocs-ov-"))
                ov = base / "members"
                ov.mkdir(parents=True, exist_ok=True)
                over.append(ov)
            sub = precompile(body, None, copybook_dirs, index=index, rewrite=rewrite,
                             division=div, members=False, gen_dir=ov / "_gen")
            # the resolver matches names case-insensitively, cobc on Linux does not: the
            # neutralised copy is written under the upper- and lower-case member names too,
            # so it shadows every case variant of the original
            stem, dot, ext = hit.name.rpartition(".")
            if not dot:
                stem, ext = hit.name, ""
            suffix = f".{ext}" if dot else ""
            for name in dict.fromkeys((hit.name, stem.upper() + suffix, stem.lower() + suffix)):
                (ov / name).write_text(sub.text, encoding="utf-8")
            notes.append(f"COPY {member}: EXEC blocks of {hit.name} neutralised")

    # implicit EIB / SQLCA / DIB blocks
    proc_words = {t.up for t in toks if proc_line is not None and t.line >= proc_line
                  and t.kind == "W"}
    copied = set(copies) | {b.options.get("MEMBER", "") for b in blocks
                            if b.kind == "SQL" and b.command == "INCLUDE"}
    need_eib = any(_EIB_RE.match(w) for w in proc_words) and "DFHEIBLK" not in copied
    need_sqlca = any(_SQLCA_NAMES.match(w) for w in proc_words) and "SQLCA" not in copied
    need_dib = (any(b.kind == "DLI" for b in blocks) or any(_DIB_RE.match(w)
                                                           for w in proc_words)) \
        and "DLIDIB" not in copied
    if need_eib or need_sqlca or need_dib:
        _implicit(lines, before, added, indent, need_eib, need_sqlca, need_dib)

    out: list[str] = []
    line_map: list[int] = []
    for n, ln in enumerate(lines):
        for extra in before.get(n, []):
            out.append(extra)
            line_map.append(n + 1)
        out.append(ln)
        line_map.append(n + 1)
        for extra in added.get(n, []):
            out.append(extra)
            line_map.append(n + 2 if n + 1 < len(lines) else n + 1)
    return Precompiled(text="\n".join(out), include_dirs=dirs, line_map=line_map,
                       blocks=blocks, notes=notes, override_dirs=over)


_EXEC_RE = re.compile(r"\bEXEC\s+(?:SQL|CICS|DLI)\b", re.I)


def _copies_by_division(toks: list[Tok], division: str) -> list[tuple[str, str]]:
    """(member, division) of every COPY statement and EXEC SQL INCLUDE, in order."""
    out: list[tuple[str, str]] = []
    div = division
    for k, t in enumerate(toks):
        nxt = toks[k + 1].up if k + 1 < len(toks) else ""
        if t.kind == "W" and nxt == "DIVISION" and t.up in ("DATA", "PROCEDURE"):
            div = t.up
        if t.kind == "W" and t.up == "COPY" and nxt:
            out.append((nxt.strip("'\""), div))
        if t.up == "INCLUDE" and k >= 2 and toks[k - 1].up == "SQL" and \
                toks[k - 2].up == "EXEC" and nxt:
            out.append((nxt.strip("'\""), div))
    seen: set[str] = set()
    return [(m, d) for m, d in out if not (m in seen or seen.add(m))]


def _ends_period(b: ExecBlock, text: str, fmt: str) -> bool:
    lines = text.split("\n")
    n = b.end_line - 1
    return 0 <= n < len(lines) and lines[n][:CODE_END].rstrip().endswith(".")


def _bms_text(index, rel: str) -> str:
    texts = getattr(index, "_texts", {})
    if rel in texts:
        return texts[rel]
    try:
        return (Path(index.root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _implicit(lines: list[str], before: dict[int, list[str]], added: dict[int, list[str]],
              indent: str, eib: bool, sqlca: bool, dib: bool) -> None:
    """insert COPY DFHEIBLK / DLIDIB first in LINKAGE and COPY SQLCA last in WORKING-STORAGE
    (adding the sections, and DATA DIVISION, when missing)."""
    def code(n: int) -> str:
        ln = lines[n]
        if indent and (len(ln) < 7 or ln[6:7] in ("*", "/")):
            return ""
        return ln[7:CODE_END] if indent else ln

    proc = next((n for n in range(len(lines)) if _PROC_RE.match(code(n))), None)
    if proc is None:
        return
    data = next((n for n in range(proc) if _DATA_RE.match(code(n))), None)
    sections = [(n, _SECTION_RE.match(code(n)).group(1).upper()) for n in range(proc)
                if _SECTION_RE.match(code(n))]
    link = next((n for n, s in sections if s == "LINKAGE"), None)
    ws = next((n for n, s in sections if s == "WORKING-STORAGE"), None)
    head: list[str] = []
    if data is None:
        head.append(f"{indent}DATA DIVISION.")
    if sqlca:
        if ws is not None:
            nxt = next((n for n, _ in sections if n > ws), proc)
            before.setdefault(nxt, []).append(f"{indent}COPY SQLCA.")
        else:
            target = link if link is not None else proc
            before.setdefault(target, [])[:0] = [f"{indent}WORKING-STORAGE SECTION.",
                                                f"{indent}COPY SQLCA."]
    link_copies = ([f"{indent}COPY DFHEIBLK."] if eib else []) + \
        ([f"{indent}COPY DLIDIB."] if dib else [])
    if link_copies:
        if link is not None:
            added.setdefault(link, [])[:0] = link_copies
        else:
            before.setdefault(proc, []).extend([f"{indent}LINKAGE SECTION.", *link_copies])
    if head:
        first = min([n for n, _ in sections] + [proc])
        before.setdefault(first, [])[:0] = head


def neutralize(text: str, fmt: str | None, copybook_dirs: list[Path], *, index=None,
               gen_dir: Path | None = None) -> Precompiled:
    """precompile(..., rewrite=neutral_rewrite): the compile-gate form of a program."""
    return precompile(text, fmt, copybook_dirs, index=index, rewrite=neutral_rewrite,
                      gen_dir=gen_dir)
