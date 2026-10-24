"""completeness checklist for a leaf doc, derived deterministically from the static index.

Each item is a yes/no question about one fact the analysis knows to be true (CheckEval /
TICK style, but the items come from ground truth, not from an LLM). The judge only answers
whether the doc states the fact; it never decides what should be asked.
"""

from __future__ import annotations

import re

from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.judge.models import ChecklistItem
from codocs.cobol.models import CobolProgram
from codocs.cobol.source import logical_lines

MAX_ITEMS = 40
MAX_PARAGRAPHS = 10
MIN_PARAGRAPH_LINES = 12  # smaller paragraphs are not worth a question of their own

_CALL_VERB = {
    "call": "CALLs", "call-dynamic": "CALLs (dynamically)", "function": "invokes the FUNCTION",
    "cics-link": "LINKs to", "cics-xctl": "XCTLs to", "cics-start": "STARTs",
}
_FILE_STATUS_RE = re.compile(r"\bFILE\s+STATUS\b", re.I)
_RESP_RE = re.compile(r"\bRESP\s*\(", re.I)
_SQLCODE_RE = re.compile(r"\bSQLCODE\b", re.I)
_ABCODE_RE = re.compile(r"\bABCODE\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
_ABEND_CALL_RE = re.compile(r"\bCALL\s+['\"](CEE3ABD|ILBOABN0|ABEND)['\"]", re.I)


def _names(values, limit: int = 8) -> str:
    values = list(dict.fromkeys(values))
    shown = ", ".join(values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def _paragraph_sizes(prog: CobolProgram) -> list[tuple[str, int]]:
    """(paragraph, physical lines up to the next paragraph) in source order."""
    paras = [p for p in prog.paragraphs if p.line > 0 and p.name != "(entry)"]
    paras.sort(key=lambda p: p.line)
    out = []
    for i, p in enumerate(paras):
        end = paras[i + 1].line if i + 1 < len(paras) else max(prog.loc + 1, p.line + 1)
        out.append((p.name, end - p.line))
    return out


def build_checklist(index: CobolRepoIndex, pid: str, max_items: int = MAX_ITEMS
                    ) -> list[ChecklistItem]:
    """yes/no questions for program *pid*, highest-value categories first, capped."""
    prog = index.programs[pid]
    items: list[ChecklistItem] = []

    def add(category: str, question: str, fact: str) -> None:
        items.append(ChecklistItem(id=f"K{len(items) + 1:02d}", category=category,
                                   question=question, fact=fact))

    # interface
    for p in prog.using_params:
        add("interface", f"Does the doc describe the parameter `{p}` received through "
            "PROCEDURE DIVISION USING (what it carries, input and/or output)?",
            f"PROCEDURE DIVISION USING ... {p}")
    if prog.returning:
        add("interface", f"Does the doc describe the RETURNING item `{prog.returning}`?",
            f"RETURNING {prog.returning}")
    if prog.has_cics and any(d.name.upper() == "DFHCOMMAREA" for d in prog.data_items):
        add("interface", "Does the doc describe the COMMAREA the program receives "
            "(DFHCOMMAREA) and what it is used for?", "LINKAGE SECTION has DFHCOMMAREA")

    # invocation
    txns = index.transactions_of(pid)
    if txns:
        add("invocation", f"Does the doc state that CICS transaction(s) {_names(txns)} start "
            "this program?", f"transactions: {', '.join(txns)}")
    jobs = index.jobs_running(pid)
    if jobs:
        add("invocation", f"Does the doc state that the program runs as a batch step of JCL "
            f"job(s) {_names(jobs)}?", f"jobs: {', '.join(jobs)}")
    callers = index.callers(pid)
    if callers:
        add("invocation", f"Does the doc name the program(s) that invoke it ({_names(callers)})?",
            f"callers: {', '.join(callers)}")

    # files
    for f in prog.files:
        where = f"CICS FILE {f.name}" if f.via == "cics" else (
            f"SELECT {f.name}" + (f" ASSIGN TO {f.assign}" if f.assign else ""))
        modes = ", ".join(f.modes) or "unknown mode"
        org = f" ({f.organization})" if f.organization else ""
        add("io", f"Does the doc identify the file {f.name}{org} and how it is used "
            f"({modes})?", f"{where}{org}; modes {modes}")

    # SQL tables
    ops_by_table: dict[str, set[str]] = {}
    for s in prog.sql:
        for t in s.tables:
            ops_by_table.setdefault(t, set()).add(s.op)
    for t, ops in sorted(ops_by_table.items()):
        add("sql", f"Does the doc state that DB2 table {t} is accessed "
            f"({', '.join(sorted(ops))})?", f"EXEC SQL {', '.join(sorted(ops))} on {t}")

    # CICS screens / queues / pseudo-conversation
    mapsets = sorted({m for c in prog.cics for m in c.resolved.get("MAPSET", [])}
                     | set(prog.maps))
    if mapsets:
        add("cics", f"Does the doc describe the BMS screen(s) the program sends/receives "
            f"({_names(mapsets)})?", f"maps: {', '.join(mapsets)}")
    ret_txn = sorted({t for c in prog.cics if c.command == "RETURN"
                      for t in c.resolved.get("TRANSID", [])})
    if ret_txn:
        add("cics", f"Does the doc explain the pseudo-conversational RETURN TRANSID "
            f"({_names(ret_txn)})?", f"RETURN TRANSID {', '.join(ret_txn)}")
    queues = sorted({q for c in prog.cics if c.command.startswith(("WRITEQ", "READQ"))
                     for q in c.resolved.get("QUEUE", []) + c.resolved.get("QNAME", [])})
    if queues:
        add("cics", f"Does the doc mention the TS/TD queue(s) {_names(queues)}?",
            f"queues: {', '.join(queues)}")

    # control transfers out
    seen: set[tuple[str, str]] = set()
    for c in prog.calls:
        if c.kind == "jcl-exec" or (c.kind == "call-dynamic" and not c.resolved):
            continue
        key = (c.kind, c.target)
        if key in seen:
            continue
        seen.add(key)
        target = c.target + (f" (ENTRY of {c.owner})" if c.owner else "")
        add("transfer", f"Does the doc state that the program {_CALL_VERB.get(c.kind, 'calls')} "
            f"{target}?", f"{c.kind} {target} at line {c.line}")

    # control-flow hazards: the doc must not contradict them
    for h in getattr(prog, "flow_hazards", []) or []:
        add("hazard", "Is the doc's description of the control flow consistent with this "
            f"fact (answer no if it contradicts or ignores it where it matters): {h}", h)

    # error handling
    code = "\n".join(ll.text for ll in logical_lines(index.program_source(pid), prog.format))
    kinds = [label for label, rx in (("FILE STATUS", _FILE_STATUS_RE), ("CICS RESP", _RESP_RE),
                                     ("SQLCODE", _SQLCODE_RE)) if rx.search(code)]
    if kinds:
        add("error", f"Does the doc describe how the program reacts to {' / '.join(kinds)} "
            "errors?", f"checks {', '.join(kinds)}")
    abends = sorted(set(_ABCODE_RE.findall(code)) | set(m.upper() for m in
                                                         _ABEND_CALL_RE.findall(code)))
    if abends:
        add("error", f"Does the doc mention the abend(s) the program raises ({_names(abends)})?",
            f"abends: {', '.join(abends)}")

    # the largest paragraphs
    big = sorted((p for p in _paragraph_sizes(prog) if p[1] >= MIN_PARAGRAPH_LINES),
                 key=lambda p: -p[1])[:MAX_PARAGRAPHS]
    for name, n in sorted(big, key=lambda p: [x.name for x in prog.paragraphs].index(p[0])):
        add("paragraph", f"Does the doc explain what paragraph/section {name} does?",
            f"{name}: {n} lines")

    if len(items) > max_items:
        # keep every category represented: trim the longest categories first
        order = ["interface", "invocation", "io", "sql", "cics", "transfer", "hazard", "error",
                 "paragraph"]
        kept: list[ChecklistItem] = []
        by_cat = {c: [i for i in items if i.category == c] for c in order}
        while len(kept) < max_items and any(by_cat.values()):
            for c in order:
                if by_cat[c] and len(kept) < max_items:
                    kept.append(by_cat[c].pop(0))
        items = sorted(kept, key=lambda i: i.id)
        for n, i in enumerate(items, 1):
            i.id = f"K{n:02d}"
    return items
