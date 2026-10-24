"""JCL parsing: jobs, catalogued/in-stream procedures, steps, DD datasets, PARM.

Only what documentation needs: JOB card, leading //* description, EXEC PGM= / PROC= steps,
DD DSN=, PARM=, symbolic parameters (&NAME) from PROC defaults / EXEC overrides / SET, and
the application program behind IMS (DFSRRC00) and DB2 TSO batch (IKJEFT01) steps.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from codocs.cobol.models import JclJob, JclStep

# system utilities: kept as steps, never mapped to application programs
UTILITY_PROGRAMS = frozenset(
    """
    IDCAMS IEFBR14 SORT ICETOOL ICEMAN SYNCSORT DFSORT ICEGENER IEBGENER IEBCOPY IEBDG
    IEBUPDTE IEBPTPCH IEBCOMPR IEHLIST IEHPROGM IEHMOVE IKJEFT01 IKJEFT1A IKJEFT1B
    DFSRRC00 DFHCSDUP DSNUTILB DSNTEP2 DSNTEP4 DSNTIAUL DSNTIAD ADRDSSU FTP BPXBATCH
    BPXBATSL IRXJCL IGYCRCTL IEWL HEWL IEWBLINK HEWLH096 DFHEAP1$ DFHECP1$ DSNHPC IGYWCL
    DSNUPROC ASMA90 IEV90 DFSUPRT0 DFSURGU0 DFSUDMP0 CEEWUCHA EQAWISTR AMBLIST IEBEDIT
    TXT2PDF IKJEFT01 IGYWCLG IEFBR15 DSNTIJUZ CBLTDLI DFSDDLT0 CSQUTIL
    """.split()
)
_UTILITY_PREFIXES = ("IEB", "IEH", "IEF", "ICE", "DFH", "DSN", "IGY", "IEW", "HEW", "ADR",
                     "BPX", "IKJ", "CSQ", "EQA")

_STMT_RE = re.compile(r"^//(\S*)\s+(\S+)(?:\s+(.*))?$")
_SYMBOL_RE = re.compile(r"&&?([A-Z@#$][A-Z0-9@#$]{0,7})\.?", re.IGNORECASE)
_RUN_PROGRAM_RE = re.compile(r"\bRUN\s+PROG(?:RAM)?\s*\(\s*([A-Z0-9@#$]+)\s*\)", re.IGNORECASE)
_EXEC_RESERVED = frozenset(
    "PGM PROC PARM COND REGION TIME ACCT ADDRSPC DPRTY DYNAMNBR PERFORM RD CCSID MEMLIMIT "
    "TVSMSG PARMDD".split()
)
_LICENSE_RE = re.compile(
    r"copyright|all rights reserved|licen[sc]e|warrant|apache\.org|\"as is\" basis|"
    r"unless required by applicable|express or implied|you may not use|you may obtain|"
    r"governing permissions|limitations under|^ver:",
    re.IGNORECASE,
)


def is_utility(program: str) -> bool:
    p = program.upper()
    return p in UTILITY_PROGRAMS or p.startswith(_UTILITY_PREFIXES)


def is_jcl_text(text: str) -> bool:
    """sniff: JCL statements (//NAME JOB|EXEC|PROC|DD) near the top."""
    hits = 0
    for line in text.split("\n")[:200]:
        if re.match(r"^//\S*\s+(JOB|EXEC|PROC|DD)\b", line):
            hits += 1
            if hits >= 2 or re.match(r"^//\S+\s+(JOB|PROC)\b", line):
                return True
    return False


# -- operand parsing ------------------------------------------------------


def _operand_field(text: str) -> str:
    """operands end at the first blank outside quotes / parentheses."""
    depth = 0
    quote = False
    for i, ch in enumerate(text):
        if ch == "'":
            quote = not quote
        elif not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == " " and depth == 0:
                return text[:i]
    return text


def split_operands(text: str) -> list[str]:
    """split on top-level commas (outside quotes and parentheses)."""
    out: list[str] = []
    depth = 0
    quote = False
    cur = ""
    for ch in text:
        if ch == "'":
            quote = not quote
        elif not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                out.append(cur)
                cur = ""
                continue
        cur += ch
    if cur:
        out.append(cur)
    return [o.strip() for o in out if o.strip()]


def _kw(operands: list[str]) -> tuple[list[str], dict[str, str]]:
    pos: list[str] = []
    kw: dict[str, str] = {}
    for o in operands:
        k, eq, v = o.partition("=")
        if eq and re.match(r"^[A-Za-z@#$][\w@#$.]*$", k):
            kw[k.upper()] = v
        else:
            pos.append(o)
    return pos, kw


def _unquote(v: str) -> str:
    v = v.strip()
    if v.startswith("(") and v.endswith(")"):
        v = v[1:-1]
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        v = v[1:-1].replace("''", "'")
    return v


def _substitute(v: str, symbols: dict[str, str]) -> str:
    if "&" not in v or not symbols:
        return v

    def rep(m: re.Match) -> str:
        if m.group(0).startswith("&&"):
            return m.group(0)  # temporary dataset name
        return symbols.get(m.group(1).upper(), m.group(0))

    return _SYMBOL_RE.sub(rep, v)


# -- statements -----------------------------------------------------------


class _Stmt:
    __slots__ = ("name", "op", "operands", "line", "instream")

    def __init__(self, name: str, op: str, operands: str, line: int):
        self.name = name
        self.op = op
        self.operands = operands
        self.line = line
        self.instream: list[str] = []


def _statements(text: str) -> tuple[list[_Stmt], list[tuple[int, str]]]:
    """JCL statements (continuations joined, in-stream data attached) + //* comments."""
    stmts: list[_Stmt] = []
    comments: list[tuple[int, str]] = []
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i].rstrip()[:72].rstrip()
        i += 1
        if raw.startswith("//*"):
            comments.append((i, raw[3:].strip()))
            continue
        if not raw.startswith("//") or raw.strip() == "//":
            continue
        m = _STMT_RE.match(raw)
        if not m:
            continue
        name, op, rest = m.group(1), m.group(2).upper(), m.group(3) or ""
        ops = _operand_field(rest)
        # continuation: operand field ending with ',' continues on the next // line
        while ops.endswith(",") and i < n:
            nxt = lines[i].rstrip()[:72]
            if not nxt.startswith("//") or nxt.startswith("//*"):
                if nxt.startswith("//*"):
                    i += 1
                    continue
                break
            i += 1
            ops += _operand_field(nxt[2:].strip())
        st = _Stmt(name, op, ops, i)
        stmts.append(st)
        # in-stream data: DD * / DD DATA up to the next // or /* line (or DLM=)
        if op == "DD":
            first = split_operands(ops)[:1]
            if first and first[0].upper() in ("*", "DATA"):
                dlm = re.search(r"DLM=\(?'?([^',)\s]{2})", ops, re.IGNORECASE)
                end = dlm.group(1) if dlm else None
                while i < n:
                    dl = lines[i]
                    if end and dl.startswith(end):
                        i += 1
                        break
                    if not end and (dl.startswith("//") or dl.startswith("/*")):
                        if dl.startswith("/*") and not dl.startswith("//"):
                            i += 1
                        break
                    st.instream.append(dl.rstrip())
                    i += 1
    return stmts, comments


def _dd_value(operands: str) -> str:
    pos, kw = _kw(split_operands(operands))
    if pos and pos[0].upper() in ("*", "DATA"):
        return "*"
    if pos and pos[0].upper() == "DUMMY":
        return "DUMMY"
    for key in ("DSN", "DSNAME"):
        if key in kw:
            return kw[key].strip("'").upper()
    if "PATH" in kw:
        return _unquote(kw["PATH"])
    if "SYSOUT" in kw:
        return "SYSOUT"
    return ""


def _step_from(st: _Stmt, symbols: dict[str, str]) -> tuple[JclStep, dict[str, str]]:
    pos, kw = _kw(split_operands(st.operands))
    step = JclStep(name=st.name.upper())
    if "PGM" in kw:
        step.program = _substitute(kw["PGM"], symbols).upper()
    elif "PROC" in kw:
        step.proc = kw["PROC"].upper()
    elif pos:
        step.proc = pos[0].upper()
    if "PARM" in kw:
        step.parm = _unquote(_substitute(kw["PARM"], symbols))
    overrides = {k: _unquote(v) for k, v in kw.items() if k not in _EXEC_RESERVED}
    return step, overrides


def _app_program(step: JclStep, instream: dict[str, list[str]]) -> str:
    prog = step.program
    if not prog:
        return ""
    if prog == "DFSRRC00":
        parts = [p.strip().strip("'") for p in step.parm.split(",")]
        if len(parts) >= 2 and parts[0].upper() in ("DLI", "BMP", "DBB", "IFP", "MPP", "JBP"):
            return parts[1].upper()
        return ""
    if prog.startswith("IKJEFT"):
        cards = "\n".join(instream.get("SYSTSIN", []))
        m = _RUN_PROGRAM_RE.search(cards)
        return m.group(1).upper() if m else ""
    if is_utility(prog) or "&" in prog:
        return ""
    return prog


def _description(comments: list[tuple[int, str]], first_exec: int) -> str:
    out: list[str] = []
    for line, c in comments:
        if line > first_exec:
            break
        s = c.strip().strip("*").strip()
        if not s or re.match(r"^[\s*\-=_+#~.`]*$", s) or _LICENSE_RE.search(s):
            continue
        if s not in out:
            out.append(s)
    return "\n".join(out[:15])


def parse_jcl(rel_path: str, text: str, procs: dict[str, JclJob] | None = None) -> JclJob:
    """JOB card name, //* description, steps, DD DSN=, PARM=; PROC files get kind="proc".

    Steps that EXEC an in-stream PROC, or a catalogued PROC found in *procs*, are expanded
    into the procedure's steps (named "STEP.PROCSTEP", DD overrides and symbols applied).
    """
    stmts, comments = _statements(text)
    stem = PurePosixPath(rel_path).stem.upper()
    job = JclJob(name=stem, path=rel_path)
    symbols: dict[str, str] = {}
    instream_procs: dict[str, JclJob] = {}
    entries: dict[str, list[_Entry]] = {}  # proc name -> its raw steps
    top: list[_Entry] = []
    cur_proc: str | None = None
    file_proc = False
    seen_job = False
    first_exec = 10**9
    entry: _Entry | None = None
    last_dd = ""

    for st in stmts:
        if st.op == "JOB" and not seen_job:
            seen_job = True
            job.name = (st.name or stem).upper()
        elif st.op == "PROC":
            _, kw = _kw(split_operands(st.operands))
            defaults = {k: _unquote(v) for k, v in kw.items()}
            name = (st.name or stem).upper()
            if seen_job or top:
                cur_proc = name  # in-stream procedure
                instream_procs[name] = JclJob(name=name, path=rel_path, kind="proc",
                                              symbols=defaults)
                entries[name] = []
            else:
                file_proc = True
                job.kind = "proc"
                job.name = stem  # catalogued procs are found by member name, not label
                job.symbols = defaults
            entry = None
        elif st.op == "PEND":
            cur_proc = None
            entry = None
        elif st.op == "SET":
            _, kw = _kw(split_operands(st.operands))
            symbols.update({k: _unquote(v) for k, v in kw.items()})
        elif st.op == "EXEC":
            first_exec = min(first_exec, st.line)
            raw = cur_proc is not None or file_proc  # procedures keep symbols unresolved
            step, ovr = _step_from(st, {} if raw else symbols)
            entry = _Entry(step, {}, ovr)
            last_dd = ""
            (entries[cur_proc] if cur_proc is not None else top).append(entry)
        elif st.op == "DD" and entry is not None:
            value = _dd_value(st.operands)
            if cur_proc is None and not file_proc:
                value = _substitute(value, symbols)
            ddname = st.name.upper()
            if not ddname:  # concatenation
                if last_dd and value and value != "*":
                    prev = entry.step.dd.get(last_dd, "")
                    entry.step.dd[last_dd] = f"{prev},{value}" if prev else value
                continue
            last_dd = ddname
            entry.step.dd[ddname] = value
            if st.instream:
                entry.instream[ddname] = st.instream

    for e in [*top, *(x for lst in entries.values() for x in lst)]:
        e.step.app_program = _app_program(e.step, e.instream)
    for name, lst in entries.items():
        instream_procs[name].steps = [e.step for e in lst]
    job.description = _description(comments, first_exec)
    if job.kind == "proc":
        job.steps = [e.step for e in top]
        return job

    known = {**(procs or {}), **instream_procs}
    for e in top:
        proc = known.get(e.step.proc) if e.step.proc else None
        if proc is None or not proc.steps:
            job.steps.append(e.step)
            continue
        scope = {**proc.symbols, **symbols, **e.overrides}
        for i, ps in enumerate(proc.steps):
            job.steps.append(expand_proc_step(e.step, ps, scope, first=i == 0))
    return job


class _Entry:
    __slots__ = ("step", "instream", "overrides")

    def __init__(self, step: JclStep, instream: dict[str, list[str]], overrides: dict[str, str]):
        self.step = step
        self.instream = instream
        self.overrides = overrides


def expand_proc_step(
    call: JclStep, ps: JclStep, symbols: dict[str, str] | None = None, first: bool = False
) -> JclStep:
    """one procedure step as run by *call* (EXEC PROC=...): 'PSTEP.DD' overrides applied.

    Unqualified DDs on the calling step are added to the first procedure step.
    """
    symbols = symbols or {}
    dd = {k: _substitute(v, symbols) for k, v in ps.dd.items()}
    for k, v in call.dd.items():
        pstep, dot, ddname = k.partition(".")
        if dot and pstep == ps.name:
            dd[ddname] = v
        elif not dot and first:
            dd[k] = v
    step = JclStep(
        name=f"{call.name}.{ps.name}" if call.name else ps.name,
        program=_substitute(ps.program, symbols).upper(),
        proc=call.proc,
        dd=dd,
        parm=_substitute(ps.parm, symbols),
    )
    if ps.app_program and "&" not in ps.app_program:
        step.app_program = ps.app_program
    else:
        step.app_program = _app_program(step, {})
    return step


def define_statements(text: str) -> list[tuple[str, dict[str, str]]]:
    """CICS RDO 'DEFINE TYPE(NAME) KEY(VALUE)...' statements (CSD files, DFHCSDUP SYSIN)."""
    out: list[tuple[str, dict[str, str]]] = []
    chunks = re.split(r"(?im)^\s*DEFINE\s+", text)
    for chunk in chunks[1:]:
        attrs: dict[str, str] = {}
        for m in re.finditer(r"([A-Z][A-Z0-9]*)\s*\(([^()]*)\)", chunk[:4000], re.IGNORECASE):
            attrs.setdefault(m.group(1).upper(), m.group(2).strip().strip("'").upper())
        head = re.match(r"\s*([A-Z]+)\s*\(", chunk, re.IGNORECASE)
        if head:
            out.append((head.group(1).upper(), attrs))
    return out


_IDCAMS_DEFINE_RE = re.compile(r"\bDEFINE\s+(CLUSTER|PATH|AIX|ALTERNATEINDEX)\b", re.IGNORECASE)
_IDCAMS_ORG = {"INDEXED": "INDEXED", "IXD": "INDEXED", "NUMBERED": "RELATIVE",
               "NUMD": "RELATIVE", "NONINDEXED": "SEQUENTIAL", "NIXD": "SEQUENTIAL",
               "LINEAR": "LINEAR"}


def vsam_definitions(text: str) -> list[tuple[str, str]]:
    """(dataset name, organization) of IDCAMS DEFINE CLUSTER / PATH statements.

    A cluster without INDEXED / NONINDEXED / NUMBERED / LINEAR is INDEXED (the IDCAMS
    default, a KSDS); a PATH reaches its base cluster through an alternate index, so it is
    keyed (INDEXED) as well. AIX definitions are skipped: programs open the PATH.
    """
    out: list[tuple[str, str]] = []
    starts = list(_IDCAMS_DEFINE_RE.finditer(text))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        chunk = text[m.end(): min(end, m.end() + 4000)]
        kind = m.group(1).upper()
        if kind in ("AIX", "ALTERNATEINDEX"):
            continue
        name = re.search(r"\bNAME\s*\(\s*([^)\s]+)\s*\)", chunk, re.IGNORECASE)
        if not name:
            continue
        org = "INDEXED"
        if kind == "CLUSTER":
            words = re.findall(r"(?<![\w.-])(INDEXED|IXD|NONINDEXED|NIXD|NUMBERED|NUMD|LINEAR)"
                               r"(?![\w.-])", chunk, re.IGNORECASE)
            if words:
                org = _IDCAMS_ORG[words[0].upper()]
        out.append((name.group(1).strip("'").upper(), org))
    return out
