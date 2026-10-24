"""COBOL source handling: decoding, fixed/free format detection, logical-line normalization.

Fixed format: cols 1-6 sequence area, col 7 indicator (* / comment, D debug, - continuation,
$ directive), cols 8-72 code, cols 73-80 ignored. Free format: `*>` comments anywhere,
`>>` directives. `>>SOURCE FORMAT` / `$SET SOURCEFORMAT` switch formats mid-file.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

_CODE_END = 72  # last code column in fixed format (1-based)
_AREA_WIDTH = _CODE_END - 7  # width of cols 8-72

_SOURCE_DIRECTIVE_RE = re.compile(
    r">>\s*SOURCE\s+(?:FORMAT\s+)?(?:IS\s+)?(FREE|FIXED|VARIABLE|TERMINAL|COBOL85|X\w*)",
    re.IGNORECASE,
)
_SET_DIRECTIVE_RE = re.compile(r"\$SET\s+.*SOURCEFORMAT\s*\(?\s*[\"']?(\w+)", re.IGNORECASE)
# compiler-option and listing-control statements that are not part of the program text
_OPTION_LINE_RE = re.compile(r"^\s*(?:CBL|PROCESS)(?:\s|$)", re.IGNORECASE)
_LISTING_RE = re.compile(r"^\s*(?:EJECT|SKIP[123])\s*\.?\s*$|^\s*TITLE\s+['\"]", re.IGNORECASE)
_KEYWORD_START_RE = re.compile(
    r"^(?:IDENTIFICATION|ID\s+DIVISION|PROGRAM-ID|ENVIRONMENT|DATA\s+DIVISION|PROCEDURE"
    r"|WORKING-STORAGE|LINKAGE|FILE\s+SECTION|FD\s|0?1\s+[A-Z]|77\s)",
    re.IGNORECASE,
)


class LogicalLine(BaseModel):
    """one source line after format normalization; continuation lines are joined in."""

    line: int  # 1-based physical line where it starts
    text: str  # code area only (fixed: cols 8-72 with indentation kept)


class CommentLine(BaseModel):
    line: int
    text: str  # comment body without the indicator / `*>`


# -- reading --------------------------------------------------------------


def read_text(path: Path) -> tuple[str, str]:
    """read a source file -> (text, encoding); utf-8, then cp1252, then latin-1."""
    data = Path(path).read_bytes()
    return decode_bytes(data)


def decode_bytes(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 never fails
        text, enc = data.decode("latin-1", errors="replace"), "latin-1"
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x1a", "")
    if text.startswith("﻿"):
        text = text[1:]
    return text, enc


# -- tabs -------------------------------------------------------------------

_TAB_WIDTHS = (8, 4, 3, 2)


def expand_tabs_fixed(line: str) -> str:
    """expand the tabs of a fixed-format line so that its code stays inside col 72.

    Editors disagree on tab stops; the usual 8 can push the end of a statement past
    col 72 (e.g. two leading tabs written with 3-column stops). The largest width in
    8, 4, 3, 2 whose expansion keeps the line within col 72 wins (8 when none does),
    as long as tab-indented code does not move into the sequence / indicator area.
    """
    if "\t" not in line:
        return line
    first = line.expandtabs(8)
    if len(first.rstrip()) <= _CODE_END:
        return first
    lead_tab = "\t" in line[: len(line) - len(line.lstrip())]
    for width in _TAB_WIDTHS[1:]:
        cand = line.expandtabs(width)
        if len(cand.rstrip()) > _CODE_END:
            continue
        if lead_tab and len(cand) - len(cand.lstrip()) < 7:
            continue
        return cand
    return first


def expand_tabs(text: str, fmt: str | None = None) -> str:
    """the text with tabs expanded the way the scanner reads it (fixed: see
    expand_tabs_fixed; free: 8 columns)."""
    if "\t" not in text:
        return text
    fmt = fmt or detect_format(text)
    if fmt == "free":
        return text.expandtabs(8)
    return "\n".join(expand_tabs_fixed(ln) for ln in text.split("\n"))


# -- format detection -----------------------------------------------------


def _directive_format(line: str) -> str | None:
    """format named by a >>SOURCE / $SET SOURCEFORMAT directive on this line, if any."""
    if ">>" not in line and "$" not in line:
        return None
    m = _SOURCE_DIRECTIVE_RE.search(line) or _SET_DIRECTIVE_RE.search(line)
    if not m:
        return None
    return "free" if m.group(1).upper() == "FREE" else "fixed"


def detect_format(text: str) -> str:
    """'fixed' or 'free'. A source-format directive in the first lines wins, else heuristics."""
    lines = text.split("\n")
    for line in lines[:60]:
        fmt = _directive_format(line)
        if fmt:
            return fmt

    nonblank = 0
    free_hits = 0
    strong_free = 0
    for line in lines[:4000]:
        line = expand_tabs_fixed(line)
        if not line.strip():
            continue
        nonblank += 1
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("*>"):
            # floating comments in area A/B (col 8+) or at the indicator are legal in
            # fixed format (GnuCOBOL); only one inside the sequence area means free
            if indent < 6:
                free_hits += 1
            continue
        if len(line.rstrip()) > 80 and line[80:].strip():
            free_hits += 1
        if indent < 7 and _KEYWORD_START_RE.match(stripped):
            strong_free += 1
            continue
        if len(line) <= 6:
            if stripped[:1].isalpha():
                free_hits += 1
            continue
        seq, ind = line[:6], line[6]
        if ind in " *-/$":
            if ind == " " and seq.strip() and seq[0] == " ":
                free_hits += 1  # partially filled "sequence area" = code indented < 7
        elif ind in "Dd" and line[7:8] == " ":
            continue  # debug line
        else:
            free_hits += 1
    if strong_free >= 1 and strong_free + free_hits >= 2:
        return "free"
    if nonblank and free_hits >= 2 and free_hits > 0.05 * nonblank:
        return "free"
    return "fixed"


# -- literal helpers ------------------------------------------------------


def _open_quote(text: str, state: str = "") -> str:
    """quote char of a literal left open at the end of *text* ('' when closed)."""
    q = state
    for ch in text:
        if q:
            if ch == q:
                q = ""  # doubled quotes toggle twice, which is fine
        elif ch in "'\"":
            q = ch
    return q


def _strip_inline_comment(code: str, state: str = "") -> str:
    """drop a floating `*>` comment that is outside literals."""
    if "*>" not in code:
        return code
    q = state
    for i, ch in enumerate(code):
        if q:
            if ch == q:
                q = ""
        elif ch in "'\"":
            q = ch
        elif ch == "*" and code[i + 1:i + 2] == ">":
            return code[:i].rstrip()
    return code


def _is_noise(code: str) -> bool:
    """compiler options / listing control / directives that never reach the parser."""
    s = code.strip()
    if not s:
        return True
    if s.startswith(">>"):
        return True
    if s.upper().startswith("$SET"):
        return True
    if _OPTION_LINE_RE.match(s) and not s.endswith("."):
        return True
    return bool(_LISTING_RE.match(s))


# -- scanning -------------------------------------------------------------


def scan(text: str, fmt: str | None = None) -> tuple[list[LogicalLine], list[CommentLine]]:
    """normalize *text* into logical code lines plus the full-line comments."""
    fmt = fmt or detect_format(text)
    out: list[LogicalLine] = []
    comments: list[CommentLine] = []
    cur_full = False  # previous fixed code line reached col 72 (word continuation w/o space)

    for idx, raw in enumerate(text.split("\n"), start=1):
        raw = raw.expandtabs(8) if fmt == "free" else expand_tabs_fixed(raw)
        if not raw.strip():
            continue
        switched = _directive_format(raw)
        if switched:
            fmt = switched
            continue

        if fmt == "free":
            stripped = raw.strip()
            if stripped.startswith("*>"):
                comments.append(CommentLine(line=idx, text=stripped[2:].strip()))
                continue
            if stripped.startswith(">>D ") or stripped.startswith("D "):
                continue  # debug line
            code = _strip_inline_comment(raw.rstrip())
            if _is_noise(code):
                continue
            out.append(LogicalLine(line=idx, text=code))
            continue

        # fixed format
        ind = raw[6] if len(raw) > 6 else " "
        if ind in "*/":
            comments.append(CommentLine(line=idx, text=raw[7:_CODE_END].rstrip()))
            continue
        if ind in "Dd":
            continue  # debugging line, compiled only WITH DEBUGGING MODE
        if ind == "$":
            continue  # Micro Focus directive line
        if ind in " -":
            area = raw[7:_CODE_END]
        else:
            area = raw[6:_CODE_END]  # stray char in col 7: keep it as code
        stripped = area.strip()
        if stripped.startswith("*>"):
            comments.append(CommentLine(line=idx, text=stripped[2:].strip()))
            continue

        if ind == "-" and out:
            prev = out[-1]
            q = _open_quote(prev.text)
            cont = _strip_inline_comment(area.lstrip(), q)
            if q and cont[:1] in ("'", '"'):
                # continued literal: previous line is significant up to col 72
                body = prev.text.ljust(_AREA_WIDTH) if len(prev.text) < _AREA_WIDTH else prev.text
                prev.text = body + cont[1:].rstrip()
            elif cur_full:
                prev.text = prev.text + cont.rstrip()
            else:
                prev.text = prev.text.rstrip() + " " + cont.rstrip()
            cur_full = len(area.rstrip()) >= _AREA_WIDTH
            continue

        code = _strip_inline_comment(area)
        if _is_noise(code):
            continue
        # keep trailing spaces only while a literal is open (it may be continued)
        code = code if _open_quote(code) else code.rstrip()
        cur_full = len(area.rstrip()) >= _AREA_WIDTH
        out.append(LogicalLine(line=idx, text=code))

    for ll in out:
        ll.text = ll.text.rstrip()
    return out, comments


def logical_lines(text: str, fmt: str | None = None) -> list[LogicalLine]:
    """code lines with sequence area, comments, debug lines and directives removed."""
    return scan(text, fmt)[0]


def comment_lines(text: str, fmt: str | None = None) -> list[CommentLine]:
    """full-line comments (fixed `*` / `/` indicator, or `*>` lines)."""
    return scan(text, fmt)[1]


def strip_comments_keep_layout(text: str, fmt: str | None = None) -> str:
    """the same text with comment lines blanked and inline `*>` comments removed."""
    fmt = fmt or detect_format(text)
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.expandtabs(8) if fmt == "free" else expand_tabs_fixed(raw)
        switched = _directive_format(line)
        if switched:
            fmt = switched
            out.append(line.rstrip())
            continue
        if fmt == "free":
            if line.strip().startswith("*>"):
                out.append("")
            else:
                out.append(_strip_inline_comment(line.rstrip()))
            continue
        ind = line[6] if len(line) > 6 else " "
        if ind in "*/" or line[7:].strip().startswith("*>"):
            out.append("")
        else:
            out.append(line[:7] + _strip_inline_comment(line[7:_CODE_END]).rstrip())
    return "\n".join(out)


def count_sloc(text: str, fmt: str | None = None) -> int:
    """non-blank, non-comment physical lines."""
    return len(logical_lines(text, fmt))
