"""record layouts and source display for COBOL prompts.

Byte-exact record layouts (PIC/USAGE sizes, field positions, VALUE images), the display
form of a source (sequence area stripped), the file table, CICS COMMAREA facts and the
control-flow hazard block. The prompt text that uses them lives in cobol/testprompts.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from codocs.cobol.models import CobolProgram, DataItem, FileUse
from codocs.cobol.source import detect_format, expand_tabs_fixed, logical_lines
from codocs.cobol.testmodels import Testability

_SOURCE_CAP = 120_000
_COPYBOOK_CAP = 16_000
_COPYBOOKS_TOTAL_CAP = 60_000
_LEAFDOC_CAP = 12_000
_LAYOUT_ROWS = 80
_LANG_NAMES = {"en": "English", "ja": "Japanese", "ko": "Korean"}
# the prose-language sentence of the COBOL doc prompts
_PROSE_LANG = {
    "en": "Write all prose in English.",
    "ja": "本文は日本語で書いてください。COBOL の名前や技術用語は英語のままにしてください。",
    "ko": "본문은 한국어로 작성하고, COBOL 이름과 기술 용어는 영어로 유지하세요.",
}


def prose_note(language: str) -> str:
    """the doc prose-language instruction ('' for English, the default): headings, the
    exact COBOL blocks and names stay as the output format gives them."""
    if not language or language == "en":
        return ""
    lang = _PROSE_LANG.get(language,
                           f"Write all prose in the language with code {language!r}.")
    return (f"{lang} Keep the `#`/`##`/`###` headings, the ```cobol blocks and every COBOL "
            "name exactly as the required format gives them (English).")


# -- source display -----------------------------------------------------------


def display_source(text: str, fmt: str | None = None) -> str:
    """source as the model should read it: fixed format without cols 1-6 and 73+."""
    fmt = fmt or detect_format(text)
    lines = text.replace("\r\n", "\n").split("\n")
    if fmt == "fixed":
        lines = [expand_tabs_fixed(ln)[6:72].rstrip() for ln in lines]
    else:
        lines = [ln.rstrip() for ln in lines]
    return "\n".join(lines).strip("\n")


def exact_program_name(source: str, pid: str) -> str:
    """the PROGRAM-ID as written in the source (case kept), for CALL literals."""
    for m in re.finditer(r"(?:PROGRAM|FUNCTION)-ID\s*\.?\s*['\"]?([A-Za-z0-9_$#@-]+)", source,
                         re.I):
        name = m.group(1).rstrip(".")
        if name.upper() == pid.upper():
            return name
    return pid


def dialect_of(fmt: str) -> str:
    """the runner's preferred dialect for a source format: IBM for fixed-format sources,
    GnuCOBOL for free (the runner may fall back per program, see harness.choose_dialect)."""
    return "ibm" if fmt == "fixed" else "default"


# -- record layouts -------------------------------------------------------------

_BINARY = {"COMP", "COMPUTATIONAL", "COMP-4", "COMPUTATIONAL-4", "BINARY", "COMP-5",
           "COMPUTATIONAL-5", "COMP-X"}
_PACKED = {"COMP-3", "COMPUTATIONAL-3", "PACKED-DECIMAL"}
_FIXED_USAGE = {"COMP-1": 4, "COMPUTATIONAL-1": 4, "COMP-2": 8, "COMPUTATIONAL-2": 8,
                "POINTER": 8, "PROCEDURE-POINTER": 8, "PROGRAM-POINTER": 8, "INDEX": 4,
                "FLOAT-SHORT": 4, "FLOAT-LONG": 8}


def pic_info(pic: str) -> tuple[int, int]:
    """(display length in bytes, digit count) of a PICTURE string."""
    pic = pic.upper().rstrip(".")
    length = digits = 0
    i = 0
    prev = ""
    while i < len(pic):
        c = pic[i]
        if c == "(":
            j = pic.find(")", i)
            n = int(pic[i + 1:j]) if j > i and pic[i + 1:j].isdigit() else 1
            if prev and prev not in "SVP":
                length += (n - 1) * (2 if prev in "NG" else 1)
            if prev in "9P":
                digits += n - 1
            i = j + 1 if j > i else len(pic)
            continue
        if pic.startswith(("CR", "DB"), i):
            length += 2
            prev = ""
            i += 2
            continue
        if c in "9":
            digits += 1
            length += 1
        elif c == "P":
            digits += 1
        elif c in "NG":
            length += 2
        elif c not in "SV":
            length += 1
        prev = c
        i += 1
    return length, digits


def item_size(pic: str, usage: str, dialect: str = "ibm") -> int:
    usage = usage.upper()
    if usage in _FIXED_USAGE:
        return _FIXED_USAGE[usage]
    length, digits = pic_info(pic) if pic else (0, 0)
    if usage in _BINARY:
        if dialect != "ibm" and digits <= 2:
            return 1
        return 2 if digits <= 4 else 4 if digits <= 9 else 8
    if usage in _PACKED:
        return digits // 2 + 1
    if usage in ("NATIONAL",):
        return length * 2 if length else 0
    return length


class _Node:
    __slots__ = ("level", "name", "pic", "usage", "occurs", "redefines", "kids", "size", "off",
                 "value")

    def __init__(self, level: int, name: str, pic: str = "", usage: str = "", occurs: int = 1,
                 redefines: str = ""):
        self.level, self.name, self.pic, self.usage = level, name, pic, usage
        self.occurs, self.redefines = occurs, redefines
        self.kids: list[_Node] = []
        self.size = 0
        self.off = 0
        self.value = ""


_VALUE_RE = re.compile(r"\sVALUES?\s+(?:IS\s+|ARE\s+)?(.*)$", re.I)


def _parse_child(line: str) -> _Node | None:
    toks = line.split()
    if len(toks) < 2 or not toks[0].isdigit():
        return None
    node = _Node(int(toks[0]), toks[1])
    m = _VALUE_RE.search(line)
    if m:
        node.value = m.group(1).strip()
    i = 2
    while i < len(toks):
        u = toks[i].upper()
        if u == "VALUE":
            break
        if u == "REDEFINES" and i + 1 < len(toks):
            node.redefines = toks[i + 1].upper()
            i += 2
            continue
        if u == "OCCURS" and i + 1 < len(toks):
            nums = [int(t) for t in toks[i + 1:i + 4] if t.isdigit()]
            node.occurs = max(nums) if nums else 1
            i += 2
            while i < len(toks) and (toks[i].isdigit() or toks[i].upper() == "TO"):
                i += 1
            continue
        if u == "PIC" and i + 1 < len(toks):
            node.pic = toks[i + 1]
            i += 2
            continue
        node.usage = node.usage or u
        i += 1
    return node


def _size_tree(node: _Node, inherited: str, dialect: str) -> None:
    usage = node.usage or inherited
    if not node.kids:
        node.size = item_size(node.pic, usage, dialect)
        return
    off = 0
    by_name: dict[str, _Node] = {}
    for k in node.kids:
        _size_tree(k, usage, dialect)
        if k.redefines and k.redefines in by_name:
            k.off = by_name[k.redefines].off
        else:
            k.off = off
            off += k.size * k.occurs
        by_name[k.name.upper()] = k
    node.size = off


@dataclass
class LayoutField:
    """one item of a record layout; offset is 0-based, of the first occurrence."""

    name: str
    level: int
    offset: int
    size: int  # one occurrence
    pic: str = ""
    usage: str = ""
    occurs: int = 1
    redefines: str = ""
    group: bool = False
    depth: int = 0
    dims: tuple[tuple[int, int], ...] = ()  # (count, stride) of enclosing + own OCCURS
    shadow: bool = False  # under a REDEFINES: not part of the default fill
    value: str = ""  # VALUE clause as written (initial content)


def record_fields(item: DataItem, dialect: str = "ibm") -> tuple[int, list[LayoutField], bool]:
    """(record length, fields in order, complete?) of a level-01 item."""
    root = _Node(1, item.name, item.picture)
    stack = [root]
    complete = True
    for line in item.children:
        if line.startswith("..."):
            complete = False
            continue
        node = _parse_child(line)
        if node is None or node.level in (66, 88):
            continue
        while len(stack) > 1 and stack[-1].level >= node.level:
            stack.pop()
        stack[-1].kids.append(node)
        stack.append(node)
    _size_tree(root, "", dialect)
    fields: list[LayoutField] = []

    def walk(n: _Node, base: int, depth: int, usage: str, dims: tuple, shadow: bool) -> None:
        for k in n.kids:
            u = k.usage or usage
            kd = dims + ((k.occurs, k.size),) if k.occurs > 1 else dims
            sh = shadow or bool(k.redefines)
            fields.append(LayoutField(
                name=k.name, level=k.level, offset=base + k.off, size=k.size, pic=k.pic,
                usage=u, occurs=k.occurs, redefines=k.redefines, group=bool(k.kids),
                depth=depth, dims=kd, shadow=sh, value=k.value,
            ))
            walk(k, base + k.off, depth + 1, u, kd, sh)

    walk(root, 0, 0, "", (), False)
    if not root.kids:
        fields.append(LayoutField(name=item.name, level=1, offset=0, size=root.size,
                                  pic=item.picture))
    return root.size, fields, complete


def record_layout(item: DataItem, dialect: str = "ibm") -> tuple[int, list[str], bool]:
    """(record length, layout rows, complete?) of a level-01 item; positions are 1-based."""
    size, fields, complete = record_fields(item, dialect)
    rows: list[str] = []
    for f in fields:
        if f.level == 1:
            continue
        if len(rows) >= _LAYOUT_ROWS:
            complete = False
            break
        desc = f"{f.level:02d} {f.name}"
        if f.redefines:
            desc += f" REDEFINES {f.redefines}"
        if f.pic:
            desc += f" PIC {f.pic}"
        if f.usage:
            desc += f" {f.usage}"
        if f.occurs > 1:
            desc += f" OCCURS {f.occurs} (each {f.size} bytes)"
        if f.value:
            desc += f" VALUE {f.value}"
            text = _value_text(f.value, f) if not f.group and not f.dims else None
            lit = re.fullmatch(r"(['\"])(.*)\1", f.value.strip().rstrip(".").strip(), re.S)
            if text is not None and lit and len(lit.group(2)) != f.size:
                desc += f" (holds {text!r}: {f.size} bytes)"
        rows.append(f"{f.offset + 1:>6} {f.size:>5}  {'  ' * f.depth}{desc}")
    return size, rows, complete


_IMAGE_CAP = 160


def _value_text(value: str, f: LayoutField) -> str | None:
    """the bytes a VALUE clause puts into an elementary DISPLAY field, or None."""
    v = value.strip().rstrip(".").strip()
    up = v.upper()
    if v.endswith("..."):
        return None  # truncated by the parser
    if up in ("SPACE", "SPACES"):
        return " " * f.size
    if up in ("ZERO", "ZEROS", "ZEROES"):
        return ("0" if pic_info(f.pic)[1] else " ") * f.size if f.pic else None
    m = re.fullmatch(r"(ALL\s+)?(['\"])(.*)\2", v, re.S | re.I)
    if m:
        text = m.group(3).replace(m.group(2) * 2, m.group(2))
        if m.group(1) and text:
            text = (text * (f.size // len(text) + 1))[:f.size]
        return text[:f.size].ljust(f.size)
    if re.fullmatch(r"[+-]?\d+", v) and f.pic and set(f.pic.upper()) <= set("9()0123456789"):
        return v.lstrip("+").rjust(f.size, "0")[-f.size:]
    return None


def value_image(item: DataItem, dialect: str = "ibm") -> str:
    """the group's initial bytes from its VALUE clauses ('~' = not set by a VALUE, '#' =
    binary / packed bytes), or '' when no VALUE applies."""
    size, fields, _ = record_fields(item, dialect)
    if not size or size > 4000:
        return ""
    buf = ["~"] * size
    hit = False
    for f in fields:
        if f.group or f.shadow or f.dims or f.level == 1:
            continue
        usage = f.usage.upper()
        if usage in _BINARY or usage in _PACKED or usage in _FIXED_USAGE:
            buf[f.offset:f.offset + f.size] = ["#"] * f.size
            hit = True
            continue
        text = _value_text(f.value, f) if f.value else None
        if text is not None:
            buf[f.offset:f.offset + f.size] = list(text[:f.size])
            hit = True
    if not hit:
        return ""
    image = "".join(buf)
    return image if len(image) <= _IMAGE_CAP else image[:_IMAGE_CAP] + "..."


def _layout_block(item: DataItem, dialect: str, note: str = "", image: bool = False) -> str:
    size, rows, complete = record_layout(item, dialect)
    head = f"{item.name} ({size} bytes{', ' + note if note else ''})"
    if item.picture and not rows:
        return f"{head}: PIC {item.picture}"
    body = "\n".join(rows) if rows else "   (no subordinate items)"
    tail = "" if complete else "\n   ... (layout truncated)"
    img = value_image(item, dialect) if image else ""
    if img:
        tail += f"\n   initial bytes: [{img}]"
    return f"{head}\n   pos   len  field\n{body}{tail}"


def file_records(program: CobolProgram, f: FileUse, dialect: str) -> list[DataItem]:
    """record layouts usable for a file: its FD record, then same-sized WORKING-STORAGE
    records (the usual READ INTO / WRITE FROM targets)."""
    items = program.data_items
    fds = [d for d in items if d.section == "FILE" and d.name == (f.fd_record or "FILLER")]
    if len(fds) != 1:
        return []  # unknown, or an ambiguous FILLER record
    fd = fds[0]
    out = [fd]
    size = record_fields(fd, dialect)[0]
    if size <= 20:
        return out
    for it in items:
        if it.section in ("WORKING-STORAGE", "LOCAL-STORAGE") and it.children and \
                record_fields(it, dialect)[0] == size:
            out.append(it)
            if len(out) >= 4:
                break
    return out


_MAX_WS_BLOCKS = 8
_MAX_LINKAGE_BLOCKS = 8
_PROC_RE = re.compile(r"\bPROCEDURE\s+DIVISION\b", re.I)
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)")


def procedure_tokens(source: str, fmt: str | None = None) -> list[str]:
    """tokens of the PROCEDURE DIVISION (comments dropped, separator periods split off)."""
    from codocs.cobol.oracle import _split_period
    from codocs.cobol.reflow import tokens

    out: list[str] = []
    started = False
    for ln in logical_lines(source, fmt):
        if not started:
            m = _PROC_RE.search(ln.text)
            if not m:
                continue
            started = True
            text = ln.text[m.end():]
        else:
            text = ln.text
        for tok in tokens(text):
            out.extend(_split_period(tok))
    return out


def group_uses(source: str, fmt: str | None = None) -> tuple[set[str], set[str]]:
    """(data names DISPLAYed, data names MOVEd as a sending item) in the procedure."""
    from codocs.cobol.oracle import _VERBS

    toks = procedure_tokens(source, fmt)
    displayed: set[str] = set()
    moved: set[str] = set()
    for i, tok in enumerate(toks):
        up = tok.upper()
        if up == "DISPLAY":
            for t in toks[i + 1:]:
                if t == "." or t.upper() in _VERBS or t.upper() in ("UPON", "WITH", "NO"):
                    break
                m = _NAME_RE.match(t)
                if m and t[0] not in "'\"":
                    displayed.add(m.group(1).upper())
        elif up == "MOVE" and i + 1 < len(toks):
            m = _NAME_RE.match(toks[i + 1])
            if m and toks[i + 1][0] not in "'\"" and i + 2 < len(toks) and \
                    toks[i + 2].upper() == "TO":
                moved.add(m.group(1).upper())
    return displayed, moved


def _redefines_inside(item: DataItem) -> bool:
    return any(" REDEFINES " in f" {c} ".upper() for c in item.children)


def linkage_layouts(program: CobolProgram, dialect: str, params: list[str]) -> list[str]:
    """layouts of the LINKAGE records: the USING parameters first, then the rest."""
    link = [d for d in program.data_items if d.section == "LINKAGE"]
    order = [d for p in params for d in link if d.name == p] + \
        [d for d in link if d.name not in params]
    blocks: list[str] = []
    for it in order[:_MAX_LINKAGE_BLOCKS]:
        src = f"from COPY {it.copybook}" if it.copybook else "LINKAGE"
        role = "parameter" if it.name in params else "LINKAGE, not a USING parameter"
        blocks.append(_layout_block(it, dialect, f"{role}, {src}"))
    return blocks


def working_storage_layouts(program: CobolProgram, source: str, dialect: str) -> list[str]:
    """layouts of WORKING-STORAGE groups the program DISPLAYs, MOVEs as a whole or
    REDEFINEs inside (the places where byte positions decide the output)."""
    if not source:
        return []
    displayed, moved = group_uses(source, program.format)
    picked: list[tuple[int, DataItem, str]] = []
    for it in program.data_items:
        if it.section not in ("WORKING-STORAGE", "LOCAL-STORAGE") or not it.children:
            continue
        if record_fields(it, dialect)[0] < 4:
            continue  # two-byte status groups and the like: nothing to miscount
        name = it.name.upper()
        why = []
        if name in displayed:
            why.append("DISPLAYed")
        if name in moved:
            why.append("MOVEd as a whole")
        if _redefines_inside(it):
            why.append("has REDEFINES")
        if why:
            rank = 0 if name in displayed or name in moved else 1
            picked.append((rank, it, ", ".join(why)))
    picked.sort(key=lambda x: x[0])
    return [_layout_block(it, dialect, f"{it.section}, {why}", image=True)
            for _, it, why in picked[:_MAX_WS_BLOCKS]]


def layouts_for(program: CobolProgram, t: Testability, dialect: str) -> list[str]:
    """layout tables for LINKAGE records and, per file, its record layouts."""
    blocks: list[str] = []
    if t.mode == "unit" or t.parm_param:
        blocks.extend(linkage_layouts(program, dialect, list(program.using_params)))
    for f in program.files:
        if f.via != "native":
            continue
        recs = file_records(program, f, dialect)
        for k, it in enumerate(recs):
            note = (f"FD record of {f.name}, dd {f.assign}" if k == 0 else
                    f"same size as the FD record of {f.name}: READ INTO / WRITE FROM; its "
                    f"field names can be used for dd {f.assign}")
            blocks.append(_layout_block(it, dialect, note))
    return blocks



# -- facts blocks shared by the prompts ----------------------------------------

# shown by flow_section() (cobol/testprompts.py uses the same text)
_FLOW_RULES = """\
### Control-flow hazards
{hazards}NEXT SENTENCE transfers control to the statement after the next separator period, \
skipping everything up to that period, including the rest of an inline PERFORM body, \
END-IF / END-PERFORM scope terminators and the statements after them in that sentence; \
inside an inline PERFORM ... END-PERFORM it leaves the loop (later iterations do not run). \
CONTINUE does nothing. Trace these paths exactly when deriving expected values."""


def _files_table(program: CobolProgram, callees: list[CobolProgram] | None = None) -> str:
    rows = []
    for owner in [program, *(callees or [])]:
        for f in owner.files:
            if f.via != "native":
                continue
            who = "SUT" if owner is program else owner.program_id
            rows.append(
                f"| {who} | {f.name} | {f.assign or '?'} | {f.organization or 'SEQUENTIAL'} | "
                f"{f.access or 'SEQUENTIAL'} | {f.record_key or '-'} | "
                f"{' '.join(f.modes) or '?'} | {f.fd_record or '-'} |"
            )
    if not rows:
        return ""
    head = ("| opened by | SELECT | ASSIGN (dd) | organization | access | record key | "
            "open modes | FD record |\n|---|---|---|---|---|---|---|---|")
    return head + "\n" + "\n".join(rows)


_COMMAREA_RE = re.compile(r"\bCOMMAREA\s*\(\s*([A-Za-z0-9][A-Za-z0-9-]*)", re.I)


def cics_facts(program: CobolProgram, source: str, dialect: str) -> str:
    """facts for a CICS spec: DFHCOMMAREA presence, EIBCALEN use and the computed layouts
    of DFHCOMMAREA and of the items passed as COMMAREA(...)."""
    items = {d.name.upper(): d for d in program.data_items}
    dfh = next((d for d in program.data_items
                if d.section == "LINKAGE" and d.name.upper() == "DFHCOMMAREA"), None)
    code = "\n".join(ln.text for ln in logical_lines(source, program.format)) if source else ""
    lines: list[str] = []
    if dfh is not None:
        size = record_fields(dfh, dialect)[0]
        lines.append(f"- DFHCOMMAREA is declared in LINKAGE ({size} bytes): the program reads "
                     "the COMMAREA passed to it through it.")
    else:
        lines.append("- The program has NO DFHCOMMAREA in LINKAGE: it cannot read a COMMAREA "
                     "passed to it (EIBCALEN > 0 only tells that one was passed). A COMMAREA "
                     "it RETURNs / LINKs with is its own WORKING-STORAGE item, so on each "
                     "re-entry that item starts again from its VALUE clauses (or what this "
                     "task MOVEs into it), never from the previous task's data.")
    if re.search(r"\bEIBCALEN\b", code, re.I):
        lines.append("- The program tests EIBCALEN.")
    passed: list[str] = []
    for name in _COMMAREA_RE.findall(code):
        up = name.upper()
        if up != "DFHCOMMAREA" and up not in passed and up in items:
            passed.append(up)
    if passed:
        lines.append("- Items passed as COMMAREA(...): " + ", ".join(
            f"{n} ({items[n].section}{', COPY ' + items[n].copybook if items[n].copybook else ''})"
            for n in passed))
    blocks = []
    if dfh is not None:
        blocks.append(_layout_block(dfh, dialect, "LINKAGE, the COMMAREA received"))
    for n in passed[:4]:
        blocks.append(_layout_block(items[n], dialect, f"{items[n].section}, passed as COMMAREA"))
    text = "\n".join(lines)
    if blocks:
        text += ("\n- Computed COMMAREA layouts (pos is 1-based; use these offsets for "
                 "COMMAREA field values):\n```\n" + "\n\n".join(blocks) + "\n```")
    return text


def flow_section(program: CobolProgram, source: str) -> str:
    """control-flow hazards from analysis (CobolProgram.flow_hazards) plus the NEXT
    SENTENCE rule, when the program has any; else ''."""
    hazards = [h for h in (getattr(program, "flow_hazards", None) or []) if h]
    code = "\n".join(ln.text for ln in logical_lines(source, program.format)) if source else ""
    if not hazards and not re.search(r"\bNEXT\s+SENTENCE\b", code, re.I):
        return ""
    listed = "".join(f"- {h}\n" for h in hazards[:20])
    return _FLOW_RULES.format(hazards=(listed + "\n") if listed else "")


def driver_hint(pid: str) -> str:
    base = re.sub(r"[^A-Z0-9]", "", pid.upper())
    return ("T" + base[:7]) if base else "TDRIVER"
