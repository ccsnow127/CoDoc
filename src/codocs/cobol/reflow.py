"""fixed-format safety for LLM-written COBOL (drivers, stubs, generated loaders).

`reflow_fixed()` rewrites any roughly-COBOL text into strict fixed reference format:
cols 1-6 blank, col 7 indicator, code in cols 8-72. Area A items (division /
section / paragraph headers, FD and 01/77 levels) start in col 8, everything
else in col 12 or later. Long lines are wrapped at token boundaries outside
literals; literals that cannot fit are split with '-' continuation lines.
Input may be fixed format (indicator in col 7, any line length) or free format.
"""

from __future__ import annotations

import re
from collections.abc import Callable

CODE_WIDTH = 65  # cols 8-72
_AREA_B = 4  # col 12, relative to col 8
_MAX_INDENT = 32
_CONT_STEP = 4

# single-word statements / scope terminators that are never paragraph headers
_NOT_PARAGRAPH = {
    "EXIT", "GOBACK", "CONTINUE", "ELSE", "THEN", "NEXT", "STOP", "END", "WHEN", "OTHER",
    "DECLARATIVES", "REPLACE", "EJECT", "SKIP1", "SKIP2", "SKIP3",
}
_AREA_A_HEAD = {"FD", "SD", "RD", "CD"}
_HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\s+(DIVISION|SECTION)\b", re.I)
_PARA_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\.(\s|$)")
_LEVEL_A_RE = re.compile(r"^(0?1|77|78)(\s|$)")
_DIVISION_RE = re.compile(r"^(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)
# the paragraphs of the IDENTIFICATION and ENVIRONMENT DIVISIONs: any other line there is a
# comment-entry or clause continuation (`AUTHOR.` / `ACME.`), never an area A item
_ID_PARAGRAPHS = {
    "PROGRAM-ID", "FUNCTION-ID", "CLASS-ID", "METHOD-ID", "INTERFACE-ID", "AUTHOR",
    "INSTALLATION", "DATE-WRITTEN", "DATE-COMPILED", "SECURITY", "REMARKS", "OPTIONS",
}
_ENV_PARAGRAPHS = {
    "SOURCE-COMPUTER", "OBJECT-COMPUTER", "SPECIAL-NAMES", "REPOSITORY", "FILE-CONTROL",
    "I-O-CONTROL",
}


# -- line classification ----------------------------------------------------


def _is_fixed_line(line: str) -> bool:
    """a line written for fixed format: 6 blank/digit cols, then an indicator."""
    if len(line) < 7:
        return False
    seq = line[:6]
    if not all(c == " " or c.isdigit() for c in seq):
        return False
    return line[6] in " *-/Dd"


def is_area_a(code: str) -> bool:
    """whether a (left-stripped) code line belongs in area A."""
    up = code.upper()
    if up.startswith(">>"):
        return True
    if up.startswith("END PROGRAM") or up.startswith("END DECLARATIVES") or \
            up.startswith("DECLARATIVES"):
        return True
    if _HEADER_RE.match(code):
        return True
    if _LEVEL_A_RE.match(code):
        return True
    head = up.split(None, 1)[0] if up.split() else ""
    if head in _AREA_A_HEAD:
        return True
    m = _PARA_RE.match(code)
    if m:
        word = m.group(1).upper()
        if word not in _NOT_PARAGRAPH and not word.startswith("END-") and not word.isdigit():
            return True
    return False


class _Place:
    """the area A decision in context: a `WORD.` line that continues an unfinished
    sentence (`MOVE X TO` / `WS-DATE.`) is an operand, not a paragraph header, and in the
    IDENTIFICATION / ENVIRONMENT DIVISIONs only their own paragraphs start in area A."""

    def __init__(self) -> None:
        self.division = ""
        self.closed = True  # the previous code entry ended its sentence

    def area_a(self, text: str) -> bool:
        up = text.upper()
        head = re.split(r"[\s.]", up, maxsplit=1)[0]
        if _DIVISION_RE.match(text) or up.startswith(("END PROGRAM", ">>")):
            ok = True
        elif self.division in ("IDENTIFICATION", "ID"):
            ok = head in _ID_PARAGRAPHS
        elif self.division == "ENVIRONMENT" and _PARA_RE.match(text):
            ok = head in _ENV_PARAGRAPHS
        else:
            ok = is_area_a(text) and (self.closed or not _PARA_RE.match(text))
        m = _DIVISION_RE.match(text)
        if m:
            self.division = m.group(1).upper()
        elif up.startswith("END PROGRAM"):
            self.division = ""
        self.closed = _ends_sentence(text)
        return ok


def _strip_inline_comment(code: str) -> str:
    """drop a '*>' comment that starts outside literals."""
    quote = ""
    i = 0
    while i < len(code):
        c = code[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif code.startswith("*>", i):
            return code[:i].rstrip()
        i += 1
    return code


def _open_quote(code: str) -> str:
    """the quote char of a literal left open at the end of *code*, or ''."""
    quote = ""
    for c in code:
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
    return quote


# -- tokenizing ---------------------------------------------------------------


def tokens(code: str) -> list[str]:
    """whitespace-separated tokens; a literal (with its spaces) stays inside one token."""
    out: list[str] = []
    cur = ""
    quote = ""
    for c in code:
        if quote:
            cur += c
            if c == quote:
                quote = ""
            continue
        if c in "'\"":
            quote = c
            cur += c
        elif c.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += c
    if cur:
        out.append(cur)
    return out


def _literal_parts(tok: str) -> tuple[str, str, str, str] | None:
    """split a token into (prefix, quote, body, suffix) when it holds one literal."""
    m = re.match(r"^([^'\"]*)(['\"])", tok)
    if not m:
        return None
    prefix, quote = m.group(1), m.group(2)
    i = len(m.group(0))
    body = ""
    while i < len(tok):
        c = tok[i]
        if c == quote:
            if i + 1 < len(tok) and tok[i + 1] == quote:
                body += c + c
                i += 2
                continue
            return prefix, quote, body, tok[i + 1:]
        body += c
        i += 1
    return prefix, quote, body, ""  # unterminated: treat the rest as body


def _body_cut(body: str, room: int) -> int:
    """largest cut <= room that does not split a doubled quote pair."""
    if room >= len(body):
        return len(body)
    cut = 0
    i = 0
    while i < len(body):
        step = 2 if i + 1 < len(body) and body[i] in "'\"" and body[i + 1] == body[i] else 1
        if i + step > room:
            break
        i += step
        cut = i
    return cut


# -- emitting -----------------------------------------------------------------


def _code_line(text: str, indicator: str = " ") -> str:
    return "      " + indicator + text


def _wrap_comment(text: str) -> list[str]:
    text = text.strip()
    if len(text) + 1 <= CODE_WIDTH:
        return [_code_line(" " + text if text else "", "*")]
    out: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > CODE_WIDTH - 1:
            if line:
                out.append(line)
                line = ""
            out.append(" " + word[: CODE_WIDTH - 1])
            word = word[CODE_WIDTH - 1:]
        cand = f"{line} {word}" if line else f" {word}"
        if len(cand) > CODE_WIDTH and line:
            out.append(line)
            line = f" {word}"
        else:
            line = cand
    if line:
        out.append(line)
    return [_code_line(x, "*") for x in out]


def _emit_code(indent: int, code: str) -> list[str]:
    """wrap one logical code line into fixed-format physical lines."""
    out: list[str] = []
    cont = min(max(indent, 4), _MAX_INDENT) + _CONT_STEP
    cur = " " * indent
    has = False  # cur holds a token
    ind = " "  # indicator of the line being built

    def flush() -> None:
        nonlocal cur, has, ind
        out.append(_code_line(cur.rstrip(), ind))
        cur, has, ind = " " * cont, False, " "

    def put(tok: str, glue: bool = False) -> None:
        nonlocal cur, has
        sep = "" if glue or not has else " "
        if len(cur) + len(sep) + len(tok) > CODE_WIDTH and has:
            flush()
            sep = ""
        cur += sep + tok
        has = True

    prev = ""
    for tok in tokens(code):
        after_copy, prev = prev == "COPY", tok.upper()
        sep = " " if has else ""
        if len(cur) + len(sep) + len(tok) <= CODE_WIDTH or \
                (has and cont + len(tok) <= CODE_WIDTH):
            put(tok)
            continue
        parts = _literal_parts(tok)
        if after_copy and parts is not None:
            # a COPY member literal is never continued (cobc rejects that): it gets a
            # line of its own in area B; a path too long even for that is cut to its
            # file name, which the copybook directories on the include path resolve
            prefix, quote, body, suffix = parts
            if _AREA_B + len(tok) > CODE_WIDTH:
                tok = prefix + quote + body.rsplit("/", 1)[-1] + quote + suffix
            if has:
                flush()
            if len(cur) + len(tok) > CODE_WIDTH:
                cur = " " * _AREA_B
            cur += tok
            has = True
            continue
        if parts is None:
            put(tok)  # an over-long word: it gets a line of its own
            continue
        prefix, quote, body, suffix = parts
        if CODE_WIDTH - len(cur + sep + prefix) < 4:
            flush()
            sep = ""
        pre, tail = cur + sep, prefix + quote
        while True:
            room = CODE_WIDTH - len(pre) - len(tail)
            if len(body) + 1 <= room:
                cur, has = pre + tail + body + quote, True
                break
            # split so the literal runs exactly to col 72 and >= 1 char continues
            if room >= len(body) or _body_cut(body, room) != room:
                pre += " "
                continue
            out.append(_code_line(pre + tail + body[:room], ind))
            body = body[room:]
            pre, tail, ind = " " * cont, quote, "-"
        if suffix:
            put(suffix, glue=True)
    if has:
        out.append(_code_line(cur.rstrip(), ind))
    return out


# -- public -------------------------------------------------------------------


def _entries(source: str) -> list[tuple[str, int, str]]:
    """normalize input into (kind, indent, text): kind code | comment | blank | directive."""
    entries: list[tuple[str, int, str]] = []
    lines = [ln.expandtabs(8).rstrip() for ln in
             source.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    fixed = _fixed_document(lines)
    for line in lines:
        if not line.strip():
            entries.append(("blank", 0, ""))
            continue
        if _is_fixed_line(line) and (fixed or (line[6] in "*/-" and _sequence_area(line))):
            ind = line[6]
            body = line[7:]
            if ind in "*/Dd":
                entries.append(("comment", 0, body.strip()))
                continue
            if ind == "-":
                _join_continuation(entries, body)
                continue
            code = body
        else:
            code = line
        stripped = code.strip()
        if stripped.startswith("*>"):
            entries.append(("comment", 0, stripped[2:].strip()))
            continue
        if stripped.startswith(">>") or stripped.upper().startswith("$SET"):
            entries.append(("directive", 0, stripped))
            continue
        indent = len(code) - len(code.lstrip())
        code = _strip_inline_comment(stripped)
        if code:
            entries.append(("code", indent, code))
    return entries


def _sequence_area(line: str) -> bool:
    """columns 1-6 are blank or a sequence number: in a free-format document ` 10031-P.`
    (a paragraph of a display-form reply) is code, not a '-' continuation line."""
    seq = line[:6]
    return not seq.strip() or seq.isdigit()


def _fixed_document(lines: list[str]) -> bool:
    """fixed unless some code line starts left of col 8 (a free-format document)."""
    for line in lines:
        if not line.strip() or line.strip().startswith("*>"):
            continue
        if not _is_fixed_line(line):
            return False
    return True


def _join_continuation(entries: list[tuple[str, int, str]], body: str) -> None:
    """merge a '-' continuation line into the previous code entry."""
    for i in range(len(entries) - 1, -1, -1):
        kind, indent, prev = entries[i]
        if kind != "code":
            continue
        quote = _open_quote(prev)
        rest = body.strip()
        last = tokens(prev)[-1] if tokens(prev) else ""
        if quote and rest.startswith(quote):
            # a real continued literal
            entries[i] = (kind, indent, prev + rest[1:])
        elif not quote and rest[:1] in "'\"" and len(last) > 1 and last[0] == rest[0] \
                and last.endswith(rest[0]):
            # 'abc' on one line and '-' 'def' on the next: the writer meant one literal
            entries[i] = (kind, indent, prev.rstrip()[:-1] + rest[1:])
        else:
            entries[i] = (kind, indent, prev + " " + rest)
        return
    entries.append(("code", 4, body.strip()))


def reflow_fixed(source: str) -> str:
    """rewrite COBOL text so every line is valid fixed reference format (cols 8-72)."""
    out: list[str] = []
    place = _Place()
    for kind, indent, text in _entries(source):
        if kind == "blank":
            out.append("")
        elif kind == "comment":
            out.extend(_wrap_comment(text))
        elif kind == "directive":
            out.append(_code_line(text))
        elif place.area_a(text):
            out.extend(_emit_code(0, text))
        else:
            out.extend(_emit_code(min(max(indent, 4), _MAX_INDENT), text))
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"


def keep_fixed(lines: list[str]) -> str | None:
    """fixed-format lines (cols 1-6 blank) kept where they are: every line keeps its
    indicator and its area; only a code or comment line past col 72 is re-wrapped. None
    when the text is not consistent fixed format or an over-long line is part of a literal
    continuation, which reflow_fixed must rejoin first."""
    out: list[str] = []
    quote = ""  # literal left open by the previous code line
    for i, line in enumerate(lines):
        line = line.rstrip()
        if not line:
            out.append("")
            continue
        if len(line) < 7 or line[:6].strip() or line[6] not in " *-/Dd":
            return None
        ind, body = line[6], line[7:]
        if ind in "*/":
            out.extend([line] if len(line) <= 72 else _wrap_comment(body))
            continue
        if ind == "-" and (not quote or not body.strip().startswith(quote)):
            return None
        quote = _open_quote(body)  # a '-' line's text starts inside the literal
        if len(line) <= 72:
            out.append(line)
            continue
        nxt = next((n for n in lines[i + 1:] if n.strip()), "")
        if ind != " " or quote or nxt[6:7] == "-":
            return None
        code = _strip_inline_comment(body.strip())
        out.extend(_emit_code(min(len(body) - len(body.lstrip()), _MAX_INDENT), code))
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"


# -- helpers for multi-program sources and copybook formats -------------------

# FUNCTION-ID too: an LLM stub for a user-defined function replaces that repo function
_PROGRAM_ID_RE = re.compile(r"(?:PROGRAM|FUNCTION)-ID\s*\.?\s*['\"]?([A-Za-z0-9_$#@-]+)",
                            re.I)
_ID_DIV_RE = re.compile(r"^\s{0,12}(IDENTIFICATION|ID)\s+DIVISION\b", re.I)


def program_ids(source: str) -> list[str]:
    """PROGRAM-IDs (and FUNCTION-IDs) declared in a source text, in order, as written."""
    return [m.group(1).rstrip(".") for m in _PROGRAM_ID_RE.finditer(_code_only(source))]


def rename_program(source: str, old: str, new: str) -> str:
    """rename the PROGRAM-ID (and a matching END PROGRAM) of a driver."""
    pat = re.compile(rf"(PROGRAM-ID\s*\.?\s*['\"]?){re.escape(old)}(?![A-Za-z0-9-])", re.I)
    source = pat.sub(lambda m: m.group(1) + new, source, count=1)
    end = re.compile(rf"(END\s+PROGRAM\s+['\"]?){re.escape(old)}(?![A-Za-z0-9-])", re.I)
    return end.sub(lambda m: m.group(1) + new, source)


def _code_only(source: str) -> str:
    lines = []
    for line in source.split("\n"):
        if _is_fixed_line(line) and line[6] in "*/":
            continue
        if line.strip().startswith("*>"):
            continue
        lines.append(line)
    return "\n".join(lines)


def split_programs(source: str) -> list[str]:
    """split a fixed-format text holding several programs at their IDENTIFICATION headers."""
    lines = source.split("\n")
    starts = [i for i, ln in enumerate(lines) if _ID_DIV_RE.match(ln[7:] if _is_fixed_line(ln)
                                                                    else ln)]
    if len(starts) <= 1:
        return [source] if source.strip() else []
    chunks = []
    starts[0] = 0
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[s:e]).strip("\n")
        if chunk.strip():
            chunks.append(chunk + "\n")
    return chunks


_COPY_RE = re.compile(r"^COPY\s+(['\"]?)([A-Za-z0-9_$#@./-]+)\1", re.I)
_COPY_START_RE = re.compile(r"^COPY(\s|$)", re.I)


def wrap_free_copies(source: str, is_free: Callable[[str], bool]) -> str:
    """surround COPY statements of free-format members with >>SOURCE FORMAT directives.

    a fixed-format driver cannot COPY a free-format copybook directly (cols 1-7 of
    the copybook would be read as sequence/indicator area); switching the format
    around the COPY lets GnuCOBOL read the member in its own format.
    """
    lines = source.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        code = line[7:].strip() if _is_fixed_line(line) and line[6] == " " else ""
        found = _copy_statement(lines, i, code)
        if found is None:
            out.append(line)
            i += 1
            continue
        j, _stmt, operand = found
        member = _member_name(operand)
        if is_free(member.upper()):
            out.append(_code_line(">>SOURCE FORMAT FREE"))
            out.extend(lines[i:j + 1])
            out.append(_code_line(">>SOURCE FORMAT FIXED"))
        else:
            out.extend(lines[i:j + 1])
        i = j + 1
    return "\n".join(out)


def _member_name(name: str) -> str:
    """copybook member of a COPY operand: 'dir/sub/x.cpy' -> 'x'."""
    member = name.rstrip(".").rsplit("/", 1)[-1]
    return re.sub(r"\.(cpy|cbl|cob|copy|cpb)$", "", member, flags=re.I)


def _copy_statement(lines: list[str], i: int, code: str) -> tuple[int, str, str] | None:
    """(last line, joined statement, member operand) of a COPY statement that starts on
    line *i* (whose code area is *code*), else None; the operand may be on a later line."""
    if not _COPY_START_RE.match(code):
        return None
    j = _copy_span(lines, i)
    stmt = " ".join(ln[7:].strip() for ln in lines[i:j + 1] if len(ln) > 7)
    m = _COPY_RE.match(stmt)
    return (j, stmt, m.group(2)) if m else None


def _copy_span(lines: list[str], i: int) -> int:
    """index of the line that ends the COPY statement starting on line *i*."""
    j = i
    pseudo = 0  # '==' seen so far: a period inside pseudo-text ends nothing
    while j < len(lines):
        code_j = lines[j][7:] if len(lines[j]) > 7 else ""
        pseudo += code_j.count("==")
        if pseudo % 2 == 0 and _ends_sentence(code_j):
            break
        j += 1
    return min(j, len(lines) - 1)


_SECTION_RE = re.compile(
    r"^(WORKING-STORAGE|LOCAL-STORAGE|LINKAGE|FILE|SCREEN|REPORT)\s+SECTION\b"
    r"|^PROCEDURE\s+DIVISION\b", re.I)
_UNBOUNDED_RE = re.compile(r"(?<![A-Za-z0-9-])UNBOUNDED(?![A-Za-z0-9-])", re.I)


def bound_unbounded(source: str, member_text: Callable[[str], str | None],
                    limit: int = 9999) -> tuple[str, list[str]]:
    """give `OCCURS ... TO UNBOUNDED` tables a fixed maximum outside the LINKAGE SECTION.

    UNBOUNDED is only legal in LINKAGE (cobc: "'x' is not in LINKAGE SECTION"), yet a
    driver must declare the SUT's LINKAGE records in WORKING-STORAGE, typically by
    COPYing the same copybook. Inline clauses are rewritten, and COPY statements of
    members that use UNBOUNDED get `REPLACING ==UNBOUNDED== BY ==<limit>==`.
    *source* is fixed format (reflowed); returns (new source, notes).
    """
    lines = source.split("\n")
    out: list[str] = []
    notes: list[str] = []
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        code = line[7:].strip() if _is_fixed_line(line) and line[6] in " -" else ""
        m = _SECTION_RE.match(code)
        if m:
            section = (m.group(1) or "PROCEDURE").upper()
        if section in ("", "LINKAGE", "PROCEDURE") or not code:
            out.append(line)
            i += 1
            continue
        found = _copy_statement(lines, i, code)
        if found:
            j, stmt, operand = found
            text = member_text(_member_name(operand).upper())
            if text and _UNBOUNDED_RE.search(_code_only(text)):
                stmt = stmt.rstrip().rstrip(".").rstrip()
                pair = f"==UNBOUNDED== BY =={limit}=="
                if re.search(r"\bREPLACING\b", stmt, re.I):
                    stmt = re.sub(r"\bREPLACING\b", "REPLACING " + pair, stmt, count=1,
                                  flags=re.I)
                else:
                    stmt += " REPLACING " + pair
                out.extend(_emit_code(_AREA_B, stmt + "."))
                notes.append(f"COPY {operand} in {section}: UNBOUNDED tables bounded "
                             f"to {limit} entries")
            else:
                out.extend(lines[i:j + 1])
            i = j + 1
            continue
        if _UNBOUNDED_RE.search(code) and not _open_quote(code):
            new = _UNBOUNDED_RE.sub(str(limit), line)
            notes.append(f"line {i + 1} in {section}: UNBOUNDED table bounded to {limit} "
                         f"entries")
            line = new
        out.append(line)
        i += 1
    return "\n".join(out), notes


def _ends_sentence(code: str) -> bool:
    code = _strip_inline_comment(code.rstrip())
    return code.endswith(".") and not _open_quote(code)
