"""deterministic program context for COBOL prompts: source display, copybooks, FACTS.

The display form of a program (sequence area removed, line numbers kept), its copybooks,
budget trimming, and the FACTS block computed from the repo index. Docgen, the judge and
the wiki use these as they are.
"""

from __future__ import annotations

import re

from codocs.cobol.index import SYSTEM_PROGRAMS_RE, CobolRepoIndex
from codocs.cobol.models import CobolProgram
from codocs.cobol.source import detect_format
from codocs.repowiki.core.cache import content_hash

# chars of source + copybooks per prompt (a 600k-char prompt is ~200k tokens). Sized for
# long-context models, so whole programs fit: COACTUPC, the largest program in the sample
# repos, is ~285k chars with its copybooks. Trimming is the exception and is reported in
# FACTS ("Context limits").
_CONTEXT_BUDGET = 600_000
_MIN_COPY_ROOM = 12_000  # copybook chars kept even when the source alone is over budget
_MIN_COPY_SLICE = 1_500  # smaller remainders are not worth a truncated copybook
# a line of render_source() output that is a comment / the PROCEDURE DIVISION header
_RENDERED_COMMENT_RE = re.compile(r"^\s*\d+ (?:[*/]|\s*\*>)")
_RENDERED_PROC_RE = re.compile(r"^\s*\d+ \s*PROCEDURE\s+DIVISION\b", re.IGNORECASE)
_DECORATION_RE = re.compile(r"^[\s*\-=_+#~.:/|]*$")
_OMITTED_RE = re.compile(r"^\s*\.\.\. \[(\d+) lines omitted\] \.\.\.$", re.MULTILINE)
_RENDERED_LINE_RE = re.compile(r"^\s*(\d+) ", re.MULTILINE)
# statement-position only: not inside literals (no quote / asterisk before) or data names
_PROGRAM_ID_RE = re.compile(
    r"^[^'\"*]{0,12}?(?<![\w-])PROGRAM-ID\s*\.?\s*['\"]?([A-Za-z0-9$#@_-]+)", re.IGNORECASE
)
_END_PROGRAM_RE = re.compile(
    r"^[^'\"*]{0,12}?(?<![\w-])END\s+PROGRAM\s+['\"]?([A-Za-z0-9$#@_-]+)", re.IGNORECASE
)
_ID_DIVISION_RE = re.compile(r"\b(?:IDENTIFICATION|ID)\s+DIVISION\b", re.IGNORECASE)

# plumbing paragraphs (error exits, abend and I/O-status displays, logging): the bizlogic
# graph treats them as plumbing and the mutation sampler skips them
PLUMBING_PARAGRAPH = re.compile(
    r"(?i)(?:^|-)(?:EXIT|ABEND(?:-ROUTINE|-PROGRAM)?|DISPLAY-IO-STATUS|IO-STATUS|LOG(?:GING)?)$"
)


# -- context ------------------------------------------------------------------


def render_source(
    text: str, fmt: str | None = None, numbered: bool = True, start: int = 1
) -> str:
    """display form of COBOL text: sequence area and cols 73+ removed (fixed format),
    blank and pure-decoration comment lines dropped, physical line numbers on the left
    (*start* is the file line number of the first line of *text*)."""
    fmt = fmt or detect_format(text)
    out: list[str] = []
    for n, raw in enumerate(text.split("\n"), start=start):
        raw = raw.expandtabs(8).rstrip()
        if fmt == "fixed":
            body = raw[6:72].rstrip() if len(raw) > 6 else ""
            indicator = body[:1]
            comment = indicator in ("*", "/")
            code = body[1:] if comment else body
        else:
            body = raw
            comment = body.lstrip().startswith("*>")
            code = body.lstrip()[2:] if comment else body
        if not body.strip() or (comment and _DECORATION_RE.match(code)):
            continue
        out.append(f"{n:5d} {body}" if numbered else body)
    return "\n".join(out)


def program_span(text: str, pid: str) -> tuple[int, int] | None:
    """0-based [start, end) line span of top-level program *pid* in a file that holds
    several programs (leading comment block and nested programs included), else None."""
    lines = text.split("\n")
    ids = [i for i, ln in enumerate(lines) if _PROGRAM_ID_RE.search(ln) and not _is_comment(ln)]
    if len(ids) < 2:
        return None
    start = next(
        (i for i in ids if _PROGRAM_ID_RE.search(lines[i]).group(1).upper() == pid.upper()), None
    )
    if start is None:
        return None
    # back up over the IDENTIFICATION DIVISION header and the comment block above it
    i = start
    if i > 0 and _ID_DIVISION_RE.search(lines[i - 1]):
        i -= 1
    while i > 0 and (not lines[i - 1].strip() or _is_comment(lines[i - 1])):
        i -= 1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _END_PROGRAM_RE.search(lines[j])
        if m and m.group(1).upper() == pid.upper() and not _is_comment(lines[j]):
            end = j + 1
            break
    return i, end


def _is_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("*>") or (len(line) > 6 and line[6] in "*/")


def program_text(index: CobolRepoIndex, pid: str) -> tuple[str, int]:
    """(text of program *pid* only, file line number of its first line)."""
    raw = index.program_source(pid)
    span = program_span(raw, pid)
    if span is None:
        return raw, 1
    lines = raw.split("\n")
    return "\n".join(lines[span[0]: span[1]]), span[0] + 1


def trim_middle(text: str, max_chars: int, head_share: float = 0.35) -> str:
    """keep the head and tail of *text* within *max_chars*, marking the omitted lines."""
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    max_chars = max(max_chars - 40, 0)  # room for the marker line
    head_room = int(max_chars * head_share)
    tail_room = max_chars - head_room
    head: list[str] = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > head_room:
            break
        head.append(ln)
        used += len(ln) + 1
    tail: list[str] = []
    used = 0
    for ln in reversed(lines[len(head):]):
        if used + len(ln) + 1 > tail_room:
            break
        tail.append(ln)
        used += len(ln) + 1
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"      ... [{omitted} lines omitted] ...", *tail])


def fit_context(
    source: str,
    copybooks: dict[str, str],
    budget: int = _CONTEXT_BUDGET,
    low_priority: set[str] | None = None,
) -> tuple[str, dict[str, str]]:
    """fit source + copybooks into *budget* chars.

    Copybooks are trimmed first: low-priority ones (e.g. BMS symbolic maps) and the largest
    go first, so as many record layouts as possible stay whole. Then the source is cut by
    trim_source(). The copybook order of the input is kept.
    """
    total = len(source) + sum(len(t) for t in copybooks.values())
    if total <= budget:
        return source, dict(copybooks)
    low = low_priority or set()
    order = sorted(copybooks, key=lambda k: (k in low, len(copybooks[k])))
    room = max(budget - len(source), min(_MIN_COPY_ROOM, budget // 8))
    kept: dict[str, str] = {}
    for name in order:
        text = copybooks[name]
        if len(text) <= room:
            kept[name] = text
            room -= len(text)
        elif room >= _MIN_COPY_SLICE:
            kept[name] = trim_middle(text, room - 60, head_share=0.8)
            room = 0
        else:
            kept[name] = "(omitted to fit the context budget)"
    kept = {k: kept[k] for k in copybooks}
    used = sum(len(t) for t in kept.values())
    return trim_source(source, max(budget - used, budget // 2)), kept


def trim_source(source: str, room: int) -> str:
    """shrink a rendered program to *room* chars: drop comment lines, then cut the middle
    of the DATA DIVISION, and only then the middle of the PROCEDURE DIVISION."""
    if len(source) <= room:
        return source
    lines = source.split("\n")
    # keep the leading header comment block; later comments go first
    code_start = next((i for i, ln in enumerate(lines) if not _RENDERED_COMMENT_RE.match(ln)), 0)
    lines = lines[:code_start] + [
        ln for ln in lines[code_start:] if not _RENDERED_COMMENT_RE.match(ln)
    ]
    text = "\n".join(lines)
    if len(text) <= room:
        return text
    split = next((i for i, ln in enumerate(lines) if _RENDERED_PROC_RE.match(ln)), None)
    if split is None:
        return trim_middle(text, room)
    data, proc = "\n".join(lines[:split]), "\n".join(lines[split:])
    if len(proc) <= room * 0.75:
        return trim_middle(data, room - len(proc), head_share=0.5) + "\n" + proc
    return (
        trim_middle(data, int(room * 0.25), head_share=0.5)
        + "\n"
        + trim_middle(proc, int(room * 0.75), head_share=0.5)
    )


def program_copybooks(index: CobolRepoIndex, pid: str) -> dict[str, str]:
    """display text of the copybooks *pid* COPYs (as resolved for it), deduplicated."""
    prog = index.programs[pid]
    out: dict[str, str] = {}
    seen: set[str] = set()
    for member in prog.copybooks:
        if member in out:
            continue
        text = index.copybook_source(member, pid)
        if text is None:
            continue
        key = content_hash(text)
        if key in seen:
            continue
        seen.add(key)
        out[member] = render_source(text, numbered=False)
    return out


def build_facts(index: CobolRepoIndex, pid: str, context_note: str = "") -> str:
    """deterministic FACTS block for the prompt, computed from the repo index
    (*context_note*: what context_limits() says was left out of the prompt)."""
    prog = index.programs[pid]
    parts = [
        _facts_identity(index, prog),
        _facts_invocation(index, prog),
        _facts_interface(prog),
        _facts_calls(index, prog),
        _facts_files(index, prog),
        _facts_sql(prog),
        _facts_cics(index, prog),
        _facts_copybooks(index, prog),
        _facts_paragraphs(prog),
        _facts_flow_hazards(prog),
        context_note,
    ]
    return "\n\n".join(p for p in parts if p)


def _facts_flow_hazards(prog: CobolProgram) -> str:
    hazards = getattr(prog, "flow_hazards", None) or []
    if not hazards:
        return ""
    return (
        "Control-flow hazards (static analysis of the PROCEDURE DIVISION; authoritative — "
        "the Processing Logic, Business Rules and diagram must follow them):\n"
        + "\n".join(f"- {h}" for h in hazards)
    )


def context_limits(
    prog: CobolProgram, full_source: str, source: str,
    full_copybooks: dict[str, str], copybooks: dict[str, str],
) -> tuple[str, list[str]]:
    """(FACTS note, LeafDoc warnings) describing what fit_context() left out; ('', [])
    when the prompt holds the whole program and all its copybooks."""
    if source == full_source and copybooks == full_copybooks:
        return "", []
    shown = {int(m.group(1)) for m in _RENDERED_LINE_RE.finditer(source)}
    full_lines = [
        int(m.group(1)) for ln in full_source.split("\n")
        if not _RENDERED_COMMENT_RE.match(ln) and (m := _RENDERED_LINE_RE.match(ln))
    ]
    omitted_lines = sum(int(m.group(1)) for m in _OMITTED_RE.finditer(source))
    paras = [p for p in prog.paragraphs if p.line and p.name != "(entry)"]
    missing: list[str] = []
    partial: list[str] = []
    for i, para in enumerate(paras):
        end = paras[i + 1].line if i + 1 < len(paras) else float("inf")
        own = [n for n in full_lines if para.line <= n < end]
        if not own:
            continue
        seen = sum(1 for n in own if n in shown)
        if seen == 0:
            missing.append(para.name)
        elif seen < len(own):
            partial.append(para.name)
    cb_cut = [k for k, v in copybooks.items() if v != full_copybooks.get(k)]
    lines = [
        "Context limits: the program is larger than the prompt budget, so the SOURCE and "
        "COPYBOOKS below are abridged."
    ]
    warnings: list[str] = []
    if source != full_source:
        lines.append(
            f"- SOURCE: comment lines were dropped and {omitted_lines} code lines were cut "
            "(marked '... [N lines omitted] ...')."
        )
        warnings.append(f"context trimmed: {omitted_lines} source lines omitted")
    if missing:
        lines.append(
            "- Paragraphs whose code is NOT shown (only their names and PERFORM targets "
            "above are known): " + ", ".join(missing)
        )
        warnings.append(f"{len(missing)} paragraphs not shown to the model: "
                        + ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""))
    if partial:
        lines.append("- Paragraphs shown only in part: " + ", ".join(partial))
    if cb_cut:
        lines.append("- Copybooks trimmed or omitted: " + ", ".join(cb_cut))
        warnings.append("copybooks trimmed: " + ", ".join(cb_cut[:20]))
    lines.append(
        "- The document must say so: add a sentence to Overview naming what was not visible, "
        "and do not describe the logic of paragraphs whose code is not shown beyond what "
        "FACTS state."
    )
    return "\n".join(lines), warnings


def _facts_identity(index: CobolRepoIndex, prog: CobolProgram) -> str:
    lines = [
        f"Program id: {prog.program_id}",
        f"Path: {prog.path} ({prog.format} format, {prog.loc} lines, {prog.sloc} code lines)",
    ]
    if prog.author:
        lines.append(f"Author: {prog.author}")
    kinds = []
    if getattr(prog, "unit_kind", "program") == "function":
        ret = f" RETURNING {prog.returning}" if prog.returning else ""
        kinds.append(
            f"user-defined FUNCTION (FUNCTION-ID {prog.program_id}), not a program: callers "
            f"declare it in their REPOSITORY paragraph and invoke it as FUNCTION "
            f"{prog.program_id}(...); the value of the function is its{ret or ' RETURNING item'}"
        )
    if prog.has_cics:
        kinds.append("CICS online program")
    if prog.has_sql:
        kinds.append("embedded SQL (DB2)")
    if prog.has_dli:
        kinds.append("IMS DL/I")
    if prog.using_params:
        kinds.append("called with parameters (PROCEDURE DIVISION USING)")
    if prog.is_batch_main:
        kinds.append("main program (no USING; ends with STOP RUN / GOBACK)")
    if prog.uses_screen_io and not prog.has_cics:
        kinds.append("screen I/O (SCREEN SECTION or positioned / colored ACCEPT-DISPLAY)")
    elif getattr(prog, "uses_terminal_io", False):
        kinds.append("terminal I/O (extended ACCEPT / DISPLAY or screen routines)")
    if kinds:
        lines.append("Kind: " + "; ".join(kinds))
    if prog.nested_programs:
        lines.append("Nested programs: " + ", ".join(prog.nested_programs))
    lines.append(f"Application group: {index.group_of(prog.program_id)}")
    if prog.header_comment:
        header = prog.header_comment.strip()
        if len(header) > 1500:
            header = header[:1500] + " ..."
        lines.append("Header comment:\n" + "\n".join("  " + ln for ln in header.splitlines()))
    return "\n".join(lines)


def _facts_invocation(index: CobolRepoIndex, prog: CobolProgram) -> str:
    pid = prog.program_id
    lines = ["Invocation (who starts this program):"]
    for t in index.transactions_of(pid):
        src = "CSD definition" if t in index.csd_transactions else "RETURN TRANSID in code"
        lines.append(f"- CICS transaction {t} ({src})")
    node = f"prog:{pid}"
    for caller in index.callers(pid):
        data = index.graph.edges[f"prog:{caller}", node]
        kinds = ", ".join(data.get("kinds", [data.get("kind", "")]))
        entries = sorted({
            c.target for c in index.programs[caller].calls
            if c.target != pid
            and (getattr(c, "owner", "") == pid or index.entry_owner.get(c.target) == pid)
        })
        via = f" through ENTRY {', '.join(entries)}" if entries else ""
        if kinds == "function":
            lines.append(f"- invoked as FUNCTION {pid}(...) by program {caller}")
        else:
            lines.append(f"- transferred to by program {caller} ({kinds}){via}")
    for jname in index.jobs_running(pid):
        job = index.jobs[jname]
        desc = f" — {job.description.splitlines()[0][:100]}" if job.description else ""
        for step in job.steps:
            if step.app_program != pid:
                continue
            how = f"PGM={step.program}" if step.program else f"PROC={step.proc}"
            if step.program and step.program != pid:
                how += f" running {pid}"
            parm = f", PARM={step.parm[:60]}" if step.parm else ""
            dds = [f"{dd}={dsn}" for dd, dsn in step.dd.items()][:14]
            more = f" (+{len(step.dd) - 14} more)" if len(step.dd) > 14 else ""
            lines.append(
                f"- JCL job {jname} ({job.path}){desc}; step {step.name}: {how}{parm}"
                + (f"; DD {', '.join(dds)}{more}" if dds else "")
            )
    if len(lines) == 1:
        lines.append(
            "- none found: no transaction, JCL job or program in the repository invokes it"
        )
    return "\n".join(lines)


def _facts_interface(prog: CobolProgram) -> str:
    lines: list[str] = []
    if prog.using_params:
        lines.append("PROCEDURE DIVISION USING: " + ", ".join(prog.using_params))
    if prog.returning:
        lines.append(f"RETURNING: {prog.returning}")
    if prog.entry_points:
        lines.append("ENTRY points: " + ", ".join(prog.entry_points))
    linkage = [d for d in prog.data_items if d.section == "LINKAGE"]
    if linkage:
        lines.append("LINKAGE SECTION items:")
        for d in linkage[:12]:
            pic = f" PIC {d.picture}" if d.picture else ""
            src = f" (from copybook {d.copybook})" if d.copybook else ""
            lines.append(f"- {d.level:02d} {d.name}{pic}{src}")
            for child in d.children[:30]:
                lines.append(f"    {child}")
            if len(d.children) > 30:
                lines.append(f"    ... ({len(d.children) - 30} more fields)")
    return "\n".join(["Interface:", *lines]) if lines else "Interface: no USING parameters"


def _call_status(index: CobolRepoIndex, target: str, resolved: bool) -> str:
    if resolved:
        owner = index.program_of(target)
        prog = index.programs.get(owner or "")
        return f"in repo ({prog.path})" if prog else "in repo"
    if SYSTEM_PROGRAMS_RE.match(target):
        return "external system/runtime routine"
    return "not found in the repository"


def _facts_calls(index: CobolRepoIndex, prog: CobolProgram) -> str:
    grouped: dict[tuple[str, str], list] = {}
    for c in prog.calls:
        grouped.setdefault((c.kind, c.target), []).append(c)
    if not grouped:
        return "Calls out: none"
    lines = ["Calls out (transfers of control):"]
    for (kind, target), sites in grouped.items():
        first = sites[0]
        where = ", ".join(str(s.line) for s in sites[:6])
        detail = f"{kind} {target} (line {where})"
        owner = _entry_owner(index, first)
        if owner:
            detail = (f"{kind} {target} (ENTRY of {owner}; line {where}) — {target} is an "
                      f"ENTRY point inside program {owner}, not a program of its own")
        if kind == "cics-start":
            started = index.transactions.get(target)
            detail += f" -> starts program {started}" if started else ""
        if first.via and first.via != target:
            detail += f" target taken from data item {first.via}"
        elif first.via:
            detail += " target is a data item whose value is unknown statically"
        if first.using:
            detail += f" USING {', '.join(first.using[:8])}"
        status = _call_status(index, target, first.resolved) if kind != "cics-start" else (
            "in repo" if first.resolved else "transaction not defined in the repository"
        )
        lines.append(f"- {detail} — {status}")
    return "\n".join(lines)


def _entry_owner(index: CobolRepoIndex, call) -> str:
    """program that declares the ENTRY a call names ('' for a PROGRAM-ID target)."""
    if call.kind in ("cics-start",) or call.target in index.programs:
        return ""
    return getattr(call, "owner", "") or (index.entry_owner.get(call.target, "")
                                          if call.resolved else "")


def _facts_files(index: CobolRepoIndex, prog: CobolProgram) -> str:
    pid = prog.program_id
    lines: list[str] = []
    for f in prog.files:
        if f.via == "cics":
            dsn = index.csd_files.get(f.name)
            ds = f" -> dataset {dsn} (CSD)" if dsn else ""
            store = next((d for d in index.datastores.values()
                          if d.kind == "cics-file" and f.name in d.aliases), None)
            org = store.organization if store is not None else ""
            if org == "INDEXED":
                ds += "; VSAM KSDS (INDEXED)"
            elif org:
                ds += f"; VSAM {org}"
            lines.append(f"- CICS file {f.name}: {' '.join(f.modes) or 'unknown ops'}{ds}")
            continue
        attrs = [a for a in (f.organization, f"ACCESS {f.access}" if f.access else "") if a]
        if f.record_key:
            attrs.append(f"RECORD KEY {f.record_key}")
        if f.modes:
            attrs.append("opened " + " ".join(f.modes))
        if f.fd_record:
            attrs.append(f"FD record {f.fd_record}")
        dsn = _dataset_of(index, pid, f.assign)
        if dsn:
            attrs.append(f"dataset {dsn}")
        lines.append(f"- SELECT {f.name} ASSIGN TO {f.assign or '?'}: " + "; ".join(attrs))
    shared: list[str] = []
    for ds in index.datastores.values():
        if pid not in ds.readers and pid not in ds.writers:
            continue
        others = sorted((set(ds.readers) | set(ds.writers)) - {pid})
        if not others and not ds.jobs:
            continue
        acc = "/".join(a for a, who in (("read", ds.readers), ("write", ds.writers)) if pid in who)
        bits = [f"{ds.kind}{f' ({ds.organization})' if ds.organization else ''}", acc]
        if others:
            bits.append("also used by " + ", ".join(others[:8]) + (" ..." if len(others) > 8 else ""))
        if ds.jobs:
            bits.append("allocated in jobs " + ", ".join(ds.jobs[:5]))
        shared.append(f"- {ds.name}: " + "; ".join(bits))
    out = []
    if lines:
        out.append("Files:\n" + "\n".join(lines))
    if shared:
        out.append("Data stores shared with other programs/jobs:\n" + "\n".join(shared[:25]))
    return "\n\n".join(out)


def _dataset_of(index: CobolRepoIndex, pid: str, assign: str) -> str:
    dd = assign.strip().upper().rsplit("-", 1)[-1] if assign else ""
    if not dd:
        return ""
    names = [
        ds.name for ds in index.datastores.values()
        if ds.kind in ("dataset", "cics-file") and dd in ds.aliases
        and (pid in ds.readers or pid in ds.writers)
    ]
    return ", ".join(names[:3])


def _facts_sql(prog: CobolProgram) -> str:
    tables: dict[str, list[str]] = {}
    includes: list[str] = []
    for s in prog.sql:
        if s.op == "INCLUDE":
            if s.member and s.member not in includes:
                includes.append(s.member)
            continue
        for t in s.tables:
            ops = tables.setdefault(t, [])
            if s.op not in ops:
                ops.append(s.op)
    lines = [f"- table {t}: {', '.join(ops)}" for t, ops in tables.items()]
    if includes:
        lines.append("- EXEC SQL INCLUDE: " + ", ".join(includes))
    return "SQL:\n" + "\n".join(lines) if lines else ""


def _facts_cics(index: CobolRepoIndex, prog: CobolProgram) -> str:
    seen: dict[str, list[int]] = {}
    for use in prog.cics:
        vals = [f"{k}={'|'.join(v)}" for k, v in use.resolved.items() if v]
        for opt in ("ABCODE", "LABEL", "QUEUE", "INTERVAL"):
            if opt in use.args and use.args[opt] and opt not in use.resolved:
                vals.append(f"{opt}={use.args[opt]}")
        key = use.command + (f" ({', '.join(vals)})" if vals else "")
        seen.setdefault(key, []).append(use.line)
    lines = [
        f"- {k} (line {', '.join(str(n) for n in v[:5])}{' ...' if len(v) > 5 else ''})"
        for k, v in list(seen.items())[:40]
    ]
    out = []
    if lines:
        out.append("EXEC CICS commands:\n" + "\n".join(lines))
    if prog.maps:
        maps = []
        for m in prog.maps:
            ms = index.mapsets.get(m)
            maps.append(f"{m} ({ms.path}; maps {', '.join(ms.maps)})" if ms else m)
        out.append("BMS maps/mapsets: " + "; ".join(maps))
    if prog.transactions:
        out.append("Transactions referenced in code: " + ", ".join(prog.transactions))
    return "\n".join(out)


def _facts_copybooks(index: CobolRepoIndex, prog: CobolProgram) -> str:
    if not prog.copybooks:
        return "Copybooks: none"
    lines = ["Copybooks (COPY order):"]
    for member in dict.fromkeys(prog.copybooks):
        cb = index.copybooks.get(member)
        if cb is None:
            note = "BMS symbolic map" if member in index.mapsets else \
                "not in repository (system/IBM or missing)"
            lines.append(f"- {member}: {note}")
            continue
        items = f"; defines {', '.join(cb.top_items[:5])}" if cb.top_items else ""
        lines.append(f"- {member} ({cb.path}, {cb.kind}{items})")
    return "\n".join(lines)


def _facts_paragraphs(prog: CobolProgram, cap: int = 200) -> str:
    if not prog.paragraphs:
        return "Paragraphs: none detected"
    lines = ["Paragraphs/sections in source order (with PERFORM targets):"]
    seen: set[str] = set()
    for p in prog.paragraphs[:cap]:
        if p.name in seen:
            continue
        seen.add(p.name)
        kind = " SECTION" if p.kind == "section" else ""
        perf = f" -> {', '.join(p.performs[:12])}" if p.performs else ""
        lines.append(f"- {p.name}{kind} (line {p.line}){perf}")
    if len(prog.paragraphs) > cap:
        lines.append(f"- ... ({len(prog.paragraphs) - cap} more)")
    return "\n".join(lines)
