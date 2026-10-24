"""GnuCOBOL build, run and grading engine.

Build model: every program a test needs (the SUT, its repo callees and the user-defined
FUNCTIONs they reference, LLM stubs) is compiled once to an object file cached by
content hash under work_dir/_modules, and statically linked into the test executable
(`cobc -x driver.cbl sut.o callee.o ...`); GnuCOBOL resolves CALL literals, CALL
data-names and user-defined FUNCTIONs against linked programs, so no module naming is
needed (runs still set COB_LIBRARY_PATH to work_dir/_modules). CALL STATIC targets
outside the repository (C routines such as ncurses' curs_set) that no stub provides get a
generated no-op, since the linker needs the symbol. Traced runs build the SUT with -ftrace
(paragraph mode) or every linked repo program with -ftraceall (statement mode);
COB_SET_TRACE / COB_TRACE_FILE / COB_TRACE_FORMAT=%I|%L|%S give the paragraphs and
statements entered.

Sources: programs and copybooks are compiled from a sanitized mirror of the repository
(work_dir/_src, or mirror_dir; same layout; tabs of fixed-format files expanded so code
stays inside col 72, CR / EOF bytes dropped), rewritten only when their content changes.
Copybooks are also mirrored under their upper- and lower-case member names (Linux
filesystems are case-sensitive; the names are listed in .names.json). An overlay
(rel -> text) replaces a program file for one call, compiled from <call dir>/ov/<rel>.

Dialect: chosen per program by probing `cobc -fsyntax-only` with an ordered list of
flag sets (fixed format: -std=ibm, GnuCOBOL default, -std=mf; free format: default,
-std=mf, -std=ibm; all with -frelax-syntax-checks). The first set that compiles without
[-Wdialect] warnings wins, else the first that compiles at all (a GnuCOBOL-native
fixed-format project compiles under -std=ibm but warns, e.g. REPOSITORY before
SPECIAL-NAMES); the driver, stubs and loaders of a test use the SUT's dialect. The
choice is written to the compile log and TestResult.dialect.

Files: every SELECT ... ASSIGN name of the SUT (and callees) is mapped to a file in
the run dir via DD_<name> / dd_<name> / <name>. Fixture records given as field maps
are encoded through the computed record layout (zoned, COMP-3, binary). SEQUENTIAL
and INDEXED files go through generated loader / unloader programs that reuse the
SUT's own SELECT / FD and exchange fixed 32000-byte records with the harness, so
binary fields survive.

Stubs: an LLM stub whose PROGRAM-ID names a repository callee replaces that callee (the
model chose to isolate the unit, and its expectations follow the stub); the callee's own
callees are then linked only when something else still reaches them. A stub for one
ENTRY of a program that is still needed for other names is skipped (duplicate symbol);
stubs for the program under test are always skipped. TestResult records stubbed vs real
callees and the skipped stubs.

Checks: unit drivers print `TEST <id> PASS|FAIL` (and `TEST <id> RUN` when a case
starts); cases whose PASS is printed unconditionally (codocs.cobol.oracle) are graded
'weak', not 'pass'. Unit and batch runs both check expected_return_code and
expected_stdout; stdout expectations match as an ordered subsequence (each occurrence
consumed once, so order and repeats are enforced), substrings by default, whole lines for
{"line": ...}. A unit case whose SUT path ends the run (ends_run, or the last case without
a TEST line) is graded by those checks.

PARM programs: a batch program that receives the JCL PARM through PROCEDURE DIVISION
USING runs through a generated wrapper main that CALLs it with the PARM structure
(binary halfword length + text), like z/OS does for EXEC PGM=...,PARM=.

Processes run in their own session (killed as a group on timeout) with COB_* variables
removed, TMPDIR in the run dir, RLIMIT_FSIZE (1 GiB, 128 MiB when traced) and no core.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from codocs.cobol.classify import LINK_KINDS, is_runtime_routine
from codocs.cobol.layout import (
    LayoutField,
    dialect_of,
    exact_program_name,
    file_records,
    record_fields,
)
from codocs.cobol.models import FileUse
from codocs.cobol.oracle import display_texts, weak_cases
from codocs.cobol.reflow import (
    bound_unbounded,
    program_ids,
    reflow_fixed,
    rename_program,
    split_programs,
    wrap_free_copies,
)
from codocs.cobol.source import decode_bytes, detect_format, expand_tabs, logical_lines
from codocs.cobol.testmodels import CaseResult, FixtureFile, GeneratedTest, TestResult
from codocs.repowiki.core.cache import content_hash

# flag sets per dialect name, and the order they are tried in per source format
DIALECTS: dict[str, list[str]] = {
    "ibm": ["-std=ibm", "-frelax-syntax-checks"],
    "default": ["-frelax-syntax-checks"],
    "mf": ["-std=mf", "-frelax-syntax-checks"],
}
DIALECT_ORDER: dict[str, tuple[str, ...]] = {
    "fixed": ("ibm", "default", "mf"),
    "free": ("default", "mf", "ibm"),
}
# the preferred flag set per format (first of DIALECT_ORDER)
DIALECT_FLAGS: dict[str, list[str]] = {
    fmt: list(DIALECTS[order[0]]) for fmt, order in DIALECT_ORDER.items()
}
_COMPILE_TIMEOUT = 300
_LOG_CAP = 20_000
# the verdict protocol, matched anywhere in a line (DISPLAY without a newline happens)
_TEST_LINE = re.compile(r"\bTEST\s+(\S+)\s+(PASS|FAIL)\b\s?(.*)$", re.I)
_RUN_LINE = re.compile(r"\bTEST\s+(\S+)\s+RUN\b", re.I)
_LIBCOB_ERROR = re.compile(r"^libcob: (?:\S+: )?error:", re.M)
_SIGNAL_MESSAGE = re.compile(r"attempt to reference invalid memory address \(signal(?: \w+)?\)|"
                             r"\(signal SIG[A-Z]+\)")
# default trace format (fallback): program or function id, then a marker or a statement
_TRACE_LINE = re.compile(r"(?:Program|Function)-Id:\s+(\S+)\s+(?:Paragraph|Section):\s+(\S+)")
_TRACE_DEFAULT = re.compile(
    r"^(?:Program|Function)-Id:\s+(\S+)\s+(?:(\w+):\s+(\S+)|(\S+))\s+Line:\s+(\d+)\s*$")
_TRACE_HEADER = re.compile(r"^(?:Program|Function)-Id:\s+(\S+)\s*$")
_TRACE_SOURCE = re.compile(r"^Source:\s+'(.*)'\s*$")
_TRACE_MARKER = re.compile(r"^(\w+):\s+(\S+)")
TRACE_FORMAT = "%I|%L|%S"
TRACE_MODES = ("none", "paragraph", "statement")
FSIZE_LIMIT = 1 << 30
TRACE_FSIZE_LIMIT = 128 << 20
XFSZ_NOTE = "statement trace exceeded 128 MiB; paragraph coverage used"
OVERLAY_FAILED = "harness: overlaid program failed to compile:"
# cobc -C markers: /* Line: 16        : ADD                : t.cbl */
_C_MARKER = re.compile(r"/\*\s*Line:\s*(\d+)\s*:\s*(.*?)\s*:\s*(\S.*?)\s*\*/")
_NOT_STATEMENTS = ("Entry", "Paragraph", "Section", "last source line", "Exit")
_ERROR_LINE = re.compile(r"^(.+?):(\d+):\s*error:\s*(.*)$")
# C symbols of the screen library (curses) that repository programs CALL
_SCREEN_SYMBOLS = frozenset({
    "curs_set", "initscr", "endwin", "refresh", "wrefresh", "getch", "wgetch", "clear",
    "erase", "move", "mvprintw", "printw", "cbreak", "noecho", "echo", "keypad", "beep",
    "napms", "timeout", "nodelay", "halfdelay", "attron", "attroff", "start_color",
})
_DIALECT_WARNING = "[-Wdialect]"
_LINE_SEQ = "LINE SEQUENTIAL"
_XFER = 32000  # record size of the binary transfer files of loaders / unloaders
_UNBOUNDED_LIMIT = 9999  # entries of an OCCURS ... UNBOUNDED table declared by a driver


def _cap(text: str) -> str:
    if len(text) <= _LOG_CAP:
        return text
    return text[: _LOG_CAP // 2] + "\n... (truncated) ...\n" + text[-_LOG_CAP // 2:]


def _killpg(p: subprocess.Popen) -> None:
    """kill the process group of a child started with start_new_session."""
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(p.pid, signal.SIGKILL)


def _xfsz(status: int | None) -> bool:
    """the process was killed by SIGXFSZ (file size limit)."""
    sig = getattr(signal, "SIGXFSZ", None)
    return sig is not None and status in (-int(sig), 128 + int(sig))


def parse_verdicts(out: str) -> tuple[dict[str, CaseResult], list[str]]:
    """TEST <id> PASS|FAIL lines (the last per id wins) and the ids that printed RUN."""
    seen: dict[str, CaseResult] = {}
    started: list[str] = []
    for line in out.splitlines():
        m = _TEST_LINE.search(line)
        if m:
            status = "pass" if m.group(2).upper() == "PASS" else "fail"
            seen[m.group(1)] = CaseResult(id=m.group(1), status=status,
                                          detail=m.group(3).strip()[:500])
            continue
        r = _RUN_LINE.search(line)
        if r and r.group(1) not in started:
            started.append(r.group(1))
    return seen, started


def parse_trace(path: Path, rel_of: Callable[[str], str | None], files: dict[str, str],
                heads: dict[str, set[int]] | None = None
                ) -> tuple[dict[str, dict[int, int]], dict[str, set[str]]]:
    """(rel -> line -> statement hits, rel -> paragraphs entered) of a GnuCOBOL trace file.

    reads the pinned format (`%I|%L|%S`) and the default one, line by line. `Source:` headers
    give the current file (rel_of maps it, None when unknown); a record counts for that file
    when its program id is one of the file's programs (files: id -> rel) or unknown. Records
    of unknown files are kept under "#<ID>" (paragraphs only). *heads*, when given, collects
    the header lines of the paragraphs and sections entered per known file: names repeat
    across SECTIONs and programs, header lines do not."""
    lines: dict[str, dict[int, int]] = {}
    paras: dict[str, set[str]] = {}
    cur: str | None = None

    def record(pid: str, num: int, text: str) -> None:
        owner = files.get(pid.upper())
        rel = cur if cur is not None and (owner is None or owner == cur) else None
        m = _TRACE_MARKER.match(text)
        if m:
            if m.group(1) in ("Paragraph", "Section"):
                paras.setdefault(rel or f"#{pid.upper()}", set()).add(m.group(2).upper())
                if heads is not None and rel is not None:
                    heads.setdefault(rel, set()).add(num)
            return
        if rel is not None:
            hits = lines.setdefault(rel, {})
            hits[num] = hits.get(num, 0) + 1

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return lines, paras
    with fh:
        for raw in fh:
            ln = raw.strip()
            if not ln:
                continue
            m = _TRACE_SOURCE.match(ln)
            if m:
                cur = rel_of(m.group(1))
                continue
            if "|" in ln and not ln.startswith(("Program-Id:", "Function-Id:")):
                parts = ln.split("|", 2)
                num = parts[1].strip() if len(parts) == 3 else ""
                if num.isdigit():
                    record(parts[0].strip(), int(num), parts[2].strip())
                continue
            d = _TRACE_DEFAULT.match(ln)
            if d:
                pid, kind, name, verb, num = d.groups()
                record(pid, int(num), f"{kind}: {name}" if kind else verb)
    return lines, paras


def _safe_rel(path: str) -> str:
    parts = [p for p in PurePosixPath(path.replace("\\", "/")).parts
             if p not in ("/", "..", ".", "")]
    return "/".join(parts) or "file.dat"


def _assign_key(name: str) -> str:
    """normalized ASSIGN name: unquoted, upper, IBM 'UT-S-' style prefixes removed."""
    name = name.strip().strip("'\"").upper()
    return re.sub(r"^(UT|DA)-[A-Z]-|^S-", "", name)


def _run_cobc(cobc: str, args: list[str], cwd: Path | None) -> tuple[bool, str]:
    try:
        p = subprocess.run([cobc, *args], cwd=cwd, capture_output=True, text=True,
                           errors="replace", timeout=_COMPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"cobc timed out after {_COMPILE_TIMEOUT}s"
    except OSError as e:
        return False, f"cobc failed to start: {e}"
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def _first_error(log: str) -> str:
    return next((ln.strip() for ln in log.splitlines() if "error" in ln), log[:200])


def choose_dialect(cobc: str, src: Path, fmt: str, inc_args: list[str],
                   cwd: Path | None = None) -> tuple[str, str]:
    """(dialect name, note) for one source file, probed with `cobc -fsyntax-only`.

    Candidates follow DIALECT_ORDER[fmt]: the first that compiles without dialect
    warnings wins, else the first that compiles at all, else the preferred one.
    """
    order = DIALECT_ORDER.get(fmt, DIALECT_ORDER["fixed"])
    first_ok = ""
    tried: list[str] = []
    for name in order:
        args = ["-fsyntax-only", *DIALECTS[name], f"-fformat={fmt}", *inc_args, str(src)]
        ok, log = _run_cobc(cobc, args, cwd)
        if ok and _DIALECT_WARNING not in log:
            note = f"dialect {name} ({' '.join(DIALECTS[name])})"
            return name, note + (f"; tried before: {'; '.join(tried)}" if tried else "")
        if ok:
            first_ok = first_ok or name
            tried.append(f"{name}: {log.count(_DIALECT_WARNING)} dialect warning(s)")
        else:
            tried.append(f"{name}: {_first_error(log)}")
    if first_ok:
        return first_ok, (f"dialect {first_ok} ({' '.join(DIALECTS[first_ok])}), the first "
                          f"that compiles; {'; '.join(tried)}")
    return order[0], f"no dialect compiles the source ({'; '.join(tried)})"


def repo_include_dirs(index, pid: str) -> list[Path]:
    """copybook dirs this program resolved, all repo copybook dirs, then its own dir."""
    dirs: list[Path] = []
    used = getattr(index, "_copy_used", {}).get(pid, {})
    for rel in used.values():
        dirs.append((index.root / rel).parent.resolve())
    dirs.extend(index.copybook_dirs())
    # own dir and ancestors last, for COPY "sub/dir/x.cpy" (never ahead of the
    # copybook dirs: COPY X must not find the program file X.cbl)
    root = Path(index.root).resolve()
    here = (index.root / index.programs[pid].path).parent.resolve()
    for d in [here, *here.parents]:
        dirs.append(d)
        if d == root:
            break
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def _inc_args(dirs: list[Path]) -> list[str]:
    out: list[str] = []
    for d in dirs:
        out += ["-I", str(d)]
    return out


def link_closure(index, pid: str, known: list[str] | None = None,
                 exclude: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """repo programs linked with *pid*: *known* (a plan's callees) plus everything
    reachable through CALL and user-defined FUNCTION edges. Programs in *exclude*
    (replaced by stubs) are neither linked nor traversed; *known* is then ignored, since
    some of it may be reachable only through them."""
    seed = [] if exclude else (known or [])
    out = {c for c in seed if c in index.programs and c != pid}
    todo = [pid, *out]
    while todo:
        cur = todo.pop()
        for c in index.callees(cur, kinds=LINK_KINDS):
            if c != pid and c not in out and c not in exclude and c in index.programs:
                out.add(c)
                todo.append(c)
    return sorted(out)


@dataclass
class StubPlan:
    """which LLM stubs are linked and which repo callees they replace."""

    chunks: list[tuple[str, list[str]]] = field(default_factory=list)  # (source, PROGRAM-IDs)
    replaced: list[str] = field(default_factory=list)  # repo programs replaced by stubs
    linked: list[str] = field(default_factory=list)  # repo programs linked for real
    skipped: list[str] = field(default_factory=list)  # "stub X skipped: why"


def _call_owner(index, call) -> str | None:
    """repo program a CALL lands in (the owner of an ENTRY name, see CallSite.owner)."""
    return getattr(call, "owner", "") or index.program_of(call.target.upper())


# CALL STATIC targets are C routines: left to the linker / no-op stubs (see _auto_stubs)
_CALL_LIT_RE = re.compile(r"\bCALL\s+(['\"])([^'\"]+)\1", re.I)


def call_spellings(index, pids: list[str]) -> dict[str, str]:
    """upper-cased CALL literal -> its spelling in the sources of *pids* (CALL STATIC
    excluded)."""
    out: dict[str, str] = {}
    for p in pids:
        try:
            text = index.program_source(p)
        except (KeyError, OSError):
            continue
        for m in _CALL_LIT_RE.finditer(text):
            out.setdefault(m.group(2).upper(), m.group(2))
    return out


def plan_stubs(index, pid: str, stubs_source: str, known: list[str] | None = None,
               drop: frozenset[str] = frozenset()) -> StubPlan:
    """decide, for the LLM's stub programs, which to link and what they replace.

    A stub named after a repo program P (its PROGRAM-ID or one of its ENTRYs) replaces P
    when every name of P that the SUT and the remaining linked programs CALL is stubbed;
    otherwise P stays linked and the stubs for its names are skipped. Stubs for the SUT
    (or its ENTRYs / nested programs) and for nested programs of linked programs are
    always skipped, and so are stubs naming any of *drop* (upper-cased names of stubs that
    failed to compile: the repo program they would replace is linked for real)."""
    plan = StubPlan()
    base = link_closure(index, pid, known)
    chunks = [(c, program_ids(c)) for c in split_programs(reflow_fixed(stubs_source))] \
        if stubs_source.strip() else []
    sut = index.programs[pid]
    sut_names = {pid.upper(), *(e.upper() for e in sut.entry_points),
                 *(n.upper() for n in sut.nested_programs)}
    nested = {n.upper(): p for p in base for n in index.programs[p].nested_programs}
    cands: list[tuple[str, list[str], dict[str, str | None]]] = []
    for chunk, names in chunks:
        label = ",".join(names) or "(no PROGRAM-ID)"
        bad = [n for n in names if n.upper() in sut_names]
        if bad:
            plan.skipped.append(f"stub {label} skipped: the program under test defines "
                                f"{','.join(bad)}")
            continue
        broken = [n for n in names if n.upper() in drop]
        if broken:
            owner = next((index.program_of(n.upper()) for n in broken
                          if index.program_of(n.upper())), None)
            plan.skipped.append(f"stub {label} skipped: it does not compile"
                                + (f" ({owner} linked for real)" if owner else ""))
            continue
        nest = [n for n in names if n.upper() in nested]
        if nest:
            plan.skipped.append(f"stub {label} skipped: {','.join(nest)} is a nested program "
                                f"of {nested[nest[0].upper()]}")
            continue
        owners = {n.upper(): index.program_of(n.upper()) for n in names}
        cands.append((chunk, names, owners))
    replaced = {o for _, _, ow in cands for o in ow.values() if o and o != pid}
    kept: dict[str, list[str]] = {}  # repo program -> names still called for real
    while True:
        stubbed = {n for _, _, ow in cands for n, o in ow.items() if o is None or o in replaced}
        linked = link_closure(index, pid, known, frozenset(replaced))
        users = [pid, *linked]
        newly: dict[str, list[str]] = {}
        for p in sorted(replaced):
            called = {c.target.upper() for q in users for c in index.programs[q].calls
                      if c.kind in ("call", "call-dynamic", "function")
                      and _call_owner(index, c) == p}
            missing = sorted(called - stubbed)
            if missing:
                newly[p] = missing
        if not newly:
            break
        kept.update(newly)
        replaced -= set(newly)
    linked = link_closure(index, pid, known, frozenset(replaced))
    spelled = call_spellings(index, [pid, *linked])
    for chunk, names, owners in cands:
        hold = sorted({o for o in owners.values() if o and o in kept})
        if hold:
            p = hold[0]
            plan.skipped.append(
                f"stub {','.join(names)} skipped: ENTRY/PROGRAM-ID of {p}, which is still "
                f"needed for {', '.join(kept[p])} (linked for real)")
            continue
        # CALL literals resolve case-sensitively: a stub 'GET-VARIABLE' must answer
        # CALL 'get-variable'
        fixed = []
        for n in names:
            exact = spelled.get(n.upper(), n)
            if exact != n:
                chunk = rename_program(chunk, n, exact)
            fixed.append(exact)
        plan.chunks.append((chunk, fixed))
    plan.replaced = sorted(replaced)
    plan.linked = linked
    return plan


def _case_names(rel: str) -> list[str]:
    """file names a copybook is mirrored under: the original, then its upper- and lower-case
    member names (extension kept as written), as cobc on Linux does not fold COPY names."""
    name = PurePosixPath(rel).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    suffix = f".{ext}" if dot else ""
    out = [name]
    for variant in (stem.upper() + suffix, stem.lower() + suffix):
        if variant not in out:
            out.append(variant)
    return out


class _Mirror:
    """sanitized copies of the repository's COBOL programs and copybooks under *root*
    (same relative layout): fixed-format tabs expanded the way the parser reads them
    (code kept inside col 72), CR / EOF bytes dropped, original encoding kept. Files are
    read from *source* (the sandbox mirror; default: the index root). Copybooks are also
    written under their upper- and lower-case member names; every name written is listed
    in <root>/.names.json (dir rel -> sorted file names)."""

    def __init__(self, index, root: Path, source: Path | None = None):
        self.index = index
        self.repo = Path(index.root).resolve()
        self.source = Path(source).resolve() if source is not None else self.repo
        self.root = root
        self._texts: dict[str, str] = {}  # rel path -> sanitized text
        self._enc: dict[str, str] = {}  # rel path -> encoding
        self._names: dict[str, set[str]] = {}  # dir rel -> file names written
        self._lock = threading.Lock()
        self._ready = False

    def ensure(self) -> None:
        with self._lock:
            if self._ready:
                return
            for f in self.index.files:
                if f.kind in ("program", "copybook"):
                    self._mirror(f.path, f.format, copybook=f.kind == "copybook")
            self._write_names()
            self._ready = True

    def _write_names(self) -> None:
        data = {d: sorted(names) for d, names in sorted(self._names.items())}
        path = self.root / ".names.json"
        text = json.dumps(data, indent=1, sort_keys=True)
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")

    def _mirror(self, rel: str, fmt: str, copybook: bool = False) -> None:
        try:
            data = (self.source / rel).read_bytes()
        except OSError:
            try:
                data = (self.repo / rel).read_bytes()
            except OSError:
                return
        text, enc = decode_bytes(data)
        clean = expand_tabs(text, fmt)
        self._texts[rel] = clean
        self._enc[rel] = enc
        out = clean.encode(enc, errors="replace")
        parent = PurePosixPath(rel).parent
        names = _case_names(rel) if copybook else [PurePosixPath(rel).name]
        drel = "" if str(parent) == "." else str(parent)
        for name in names:
            target = self.root / drel / name if drel else self.root / name
            try:
                if target.exists() and target.read_bytes() == out:
                    self._names.setdefault(drel, set()).add(name)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                tmp.write_bytes(out)
                tmp.replace(target)
                self._names.setdefault(drel, set()).add(name)
            except OSError:
                if name == names[0]:
                    self._texts.pop(rel, None)
                    return

    def has(self, rel: str) -> bool:
        self.ensure()
        return rel in self._texts

    def text(self, rel: str) -> str | None:
        self.ensure()
        return self._texts.get(rel)

    def encoding(self, rel: str) -> str:
        self.ensure()
        return self._enc.get(rel, "utf-8")

    def path(self, rel: str) -> Path:
        """the mirrored file (the original when it could not be mirrored)."""
        return self.root / rel if self.has(rel) else self.repo / rel

    def rel_of(self, path: str | Path) -> str | None:
        """repo rel of a mirrored file path (None outside the mirror)."""
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def dirs(self, dirs: list[Path]) -> list[Path]:
        """mirrored counterparts of repo dirs first, then the originals (for members the
        index did not classify as copybooks)."""
        self.ensure()
        mirrored: list[Path] = []
        for d in dirs:
            try:
                rel = d.resolve().relative_to(self.repo)
            except ValueError:
                continue
            m = self.root / rel
            if m.is_dir() and m not in mirrored:
                mirrored.append(m)
        return mirrored + [d for d in dirs if d not in mirrored]


class _Build:
    """state of one test execution."""

    def __init__(self, work: Path, overlay: dict[str, str] | None = None,
                 trace: str = "paragraph"):
        self.work = work
        self.compile_log: list[str] = []
        self.run_log: list[str] = []
        self.env: dict[str, str] = {}
        self.stub_names: set[str] = set()  # PROGRAM-IDs of the linked stubs, as written
        self.linked: list[str] = []  # repo programs linked for real (see plan_stubs)
        self.overlay: dict[str, str] = dict(overlay or {})  # rel -> replacement text
        self.overlay_paths: dict[str, Path] = {}  # rel -> <work>/ov/<rel>
        self.trace = trace  # none | paragraph | statement
        self.sources: dict[str, str] = {}  # compiled source path -> repo rel (coverage)
        self.trace_callees = trace == "statement"  # callees built with tracing too
        self.timeout: int | None = None  # per-call run timeout (else the runner's)
        self.returncode: int | None = None  # process status of the last run
        self.crashed = False
        self.signal_detail = ""


class CobolTestRunner:
    """compiles and runs GeneratedTests with GnuCOBOL."""

    __test__ = False  # not a pytest class

    def __init__(
        self,
        index,
        work_dir: Path,
        cobc: str = "cobc",
        timeout: int = 30,
        cobc_flags: list[str] | None = None,
        layout_index=None,
        *,
        source_root: Path | None = None,
        fsize_limit: int = FSIZE_LIMIT,
        trace_fsize_limit: int = TRACE_FSIZE_LIMIT,
        mirror_dir: Path | None = None,
        tmp_dir: Path | None = None,
    ):
        """*source_root*: where the mirror reads the files (sandbox; default the index
        root); *mirror_dir*: the mirror (default work_dir/_src); *tmp_dir*: parent of the
        per-call run dirs b<random> of run/run_program (default: work_dir/<program>)."""
        self.index = index
        self.work_dir = Path(work_dir).resolve()
        self.cobc = cobc
        self.timeout = timeout
        self.cobc_flags = cobc_flags
        self.fsize_limit = int(fsize_limit)
        self.trace_fsize_limit = int(trace_fsize_limit)
        self.mirror = _Mirror(index, Path(mirror_dir).resolve() if mirror_dir is not None
                              else self.work_dir / "_src", source_root)
        self.tmp_dir = Path(tmp_dir).resolve() if tmp_dir is not None else None
        self._dialects: dict[str, tuple[str, str]] = {}  # pid -> (dialect, note)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._info: str | None = None  # `cobc --info` output
        self._lines: dict[str, set[int]] = {}  # content key -> statement universe
        self._checks: dict[str, tuple[bool, list[str], str]] = {}  # compile_check results
        # data files are environment: when *layout_index* is given (SpecForge phi scoring,
        # where the SUT is a program regenerated from its doc), fixture records are encoded and loaded/unloaded
        # through the record layouts of the programs in that index, not of the SUT
        self.layout = (CobolTestRunner(layout_index, self.work_dir / "_layout", cobc, timeout,
                                       cobc_flags)
                       if layout_index is not None else None)

    # -- environment -------------------------------------------------------

    def available(self) -> bool:
        if getattr(self, "_available", False):
            return True
        if shutil.which(self.cobc) is None:
            return False
        try:
            p = subprocess.run([self.cobc, "--version"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        self._available = p.returncode == 0
        return self._available

    def dialect_flags(self, fmt: str) -> list[str]:
        """the preferred flag set for a source format (or the cobc_flags override)."""
        if self.cobc_flags is not None:
            return list(self.cobc_flags)
        return list(DIALECT_FLAGS.get(fmt, DIALECT_FLAGS["fixed"]))

    def layout_dialect(self, pid: str) -> str:
        """record-layout dialect (prompts.record_fields) of the program's build dialect."""
        name = self.program_dialect(pid)[0]
        if name in ("default", "mf"):
            return "default"
        return "ibm" if name == "ibm" else dialect_of(self.index.programs[pid].format)

    def program_dialect(self, pid: str) -> tuple[str, str]:
        """(dialect name, note) the program compiles with, probed once and cached."""
        if self.cobc_flags is not None:
            return "custom", f"dialect custom ({' '.join(self.cobc_flags)})"
        with self._lock(f"dialect:{pid}"):
            if pid not in self._dialects:
                prog = self.index.programs[pid]
                self.work_dir.mkdir(parents=True, exist_ok=True)
                self._dialects[pid] = choose_dialect(
                    self.cobc, self.source_path(pid), prog.format,
                    _inc_args(self.include_dirs(pid)), self.work_dir)
            return self._dialects[pid]

    def _flags(self, dialect: str, source_fmt: str) -> list[str]:
        """cobc flags for a dialect name (see DIALECTS) and the format of the file."""
        if self.cobc_flags is not None:
            flags = list(self.cobc_flags)
        else:
            flags = list(DIALECTS.get(dialect, DIALECTS["default"]))
        if not any(f.startswith("-fformat") or f in ("-free", "-F", "--free", "--fixed")
                   for f in flags):
            flags.append(f"-fformat={source_fmt}")
        return flags

    def source_path(self, pid: str) -> Path:
        return self.mirror.path(self.index.programs[pid].path)

    def include_dirs(self, pid: str) -> list[Path]:
        """mirrored copybook dirs of the program (see repo_include_dirs), then originals."""
        return self.mirror.dirs(repo_include_dirs(self.index, pid))

    def _copybook_text(self, member: str, pid: str) -> str | None:
        """the (sanitized) text of the copybook a COPY in *pid* resolves to."""
        rel = getattr(self.index, "_copy_used", {}).get(pid, {}).get(member.upper())
        if rel is None:
            cb = self.index.copybooks.get(member.upper())
            rel = cb.path if cb else None
        text = self.mirror.text(rel) if rel else None
        return text if text is not None else self.index.copybook_source(member, pid)

    def _copybook_is_free(self, pid: str) -> Callable[[str], bool]:
        prog_fmt = self.index.programs[pid].format

        def is_free(member: str) -> bool:
            text = self.index.copybook_source(member, pid)
            fmt = detect_format(text) if text is not None else prog_fmt
            return fmt == "free"

        return is_free

    def _prepare_source(self, source: str, pid: str, b: _Build) -> str:
        """an LLM-written fixed-format program (driver, stub) made ready to compile:
        re-wrapped, UNBOUNDED tables outside LINKAGE bounded, free copybooks wrapped."""
        text = reflow_fixed(source)
        text, notes = bound_unbounded(text, lambda m: self._copybook_text(m, pid),
                                      _UNBOUNDED_LIMIT)
        b.compile_log.extend(f"harness: {n}" for n in notes)
        return wrap_free_copies(text, self._copybook_is_free(pid))

    # -- compiling ---------------------------------------------------------------

    def _cobc(self, args: list[str], cwd: Path) -> tuple[bool, str]:
        return _run_cobc(self.cobc, args, cwd)

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def overlay_source(self, rel: str, text: str) -> Path:
        """the replacement text of *rel* written to a content-addressed path under
        _modules/ov (stable across calls, so cached objects and trace paths stay valid),
        encoded like the original file."""
        fmt = self._file_format(rel)
        clean = expand_tabs(text.replace("\r\n", "\n").replace("\r", "\n"), fmt)
        key = content_hash(rel + "\0" + clean)
        path = self.work_dir / "_modules" / "ov" / key / rel
        with self._lock(f"ov:{path}"):
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
                tmp.write_bytes(clean.encode(self.mirror.encoding(rel), errors="replace"))
                tmp.replace(path)
        return path

    def _file_format(self, rel: str) -> str:
        for f in self.index.files:
            if f.path == rel:
                return f.format
        return "fixed"

    def rel_of_source(self, path: str | Path) -> str | None:
        """repo rel of a compiled source path: a mirror file or an overlay copy."""
        rel = self.mirror.rel_of(path)
        if rel is not None:
            return rel
        try:
            sub = Path(path).resolve().relative_to((self.work_dir / "_modules" / "ov").resolve())
        except (ValueError, OSError):
            return None
        parts = sub.parts
        return "/".join(parts[1:]) if len(parts) > 1 else None

    def _program_source(self, pid: str, b: _Build | None) -> tuple[Path, str]:
        """(path to compile, text) of a program: its overlay in this call, else the mirror."""
        prog = self.index.programs[pid]
        if b is not None and prog.path in b.overlay:
            path = b.overlay_paths.get(prog.path)
            if path is None:
                path = b.overlay_paths[prog.path] = self.overlay_source(
                    prog.path, b.overlay[prog.path])
            return path, path.read_text(encoding=self.mirror.encoding(prog.path),
                                        errors="replace")
        return self.source_path(pid), self.mirror.text(prog.path) or \
            self.index.program_source(pid)

    @staticmethod
    def _trace_flags(trace: bool | str) -> list[str]:
        if trace is True or trace == "paragraph":
            return ["-ftrace"]
        if trace == "statement":
            return ["-ftraceall"]
        return []

    def build_object(self, pid: str, trace: bool | str = False, b: _Build | None = None
                     ) -> tuple[Path | None, str]:
        """object file of the program's (mirrored or overlaid) source, cached by content +
        flags. trace: False/"none", True/"paragraph" (-ftrace) or "statement" (-ftraceall).

        The dialect is the program's own (program_dialect); the log starts with it."""
        prog = self.index.programs[pid]
        dialect, note = self.program_dialect(pid)
        src, text = self._program_source(pid, b)
        flags = self._flags(dialect, prog.format) + self._trace_flags(trace)
        incs = self.include_dirs(pid)
        copies = "\0".join(self._copybook_text(m, pid) or "" for m in prog.copybooks)
        key = content_hash("\0".join([text, copies, " ".join(flags),
                                      " ".join(map(str, incs)), str(src)]))
        mod_dir = self.work_dir / "_modules"
        mod_dir.mkdir(parents=True, exist_ok=True)
        if b is not None:
            b.sources[str(src)] = prog.path
        obj = mod_dir / f"{src.stem}-{key}.o"
        with self._lock(str(obj)):
            if obj.exists():
                return obj, note
            tmp = mod_dir / f"{src.stem}-{key}.{os.getpid()}.{threading.get_ident()}.o"
            args = ["-c", *flags, *_inc_args(incs), "-o", str(tmp), str(src)]
            ok, log = self._cobc(args, mod_dir)
            if not ok:
                tmp.unlink(missing_ok=True)
                return None, f"{note}\n$ cobc {' '.join(args)}\n{log}"
            tmp.replace(obj)
        return obj, f"{note}\n{log}".strip()

    @staticmethod
    def _inc_args(dirs: list[Path]) -> list[str]:
        return _inc_args(dirs)

    def _build_stubs(self, test: GeneratedTest, b: _Build, plan: StubPlan,
                     taken: set[str], failed: set[str] | None = None) -> list[Path]:
        """compile the planned stubs; the names of stubs that fail go into *failed*."""
        objs: list[Path] = []
        pid = test.program_id
        dialect = self.program_dialect(pid)[0]
        b.compile_log.extend(x for x in plan.skipped if x not in b.compile_log)
        for i, (chunk, names) in enumerate(plan.chunks, 1):
            clash = [n for n in names if n.upper() in taken]
            if clash:  # safety net: never a duplicate symbol
                b.compile_log.append(f"stub {','.join(names)} skipped: repo program or "
                                     f"ENTRY of that name ({','.join(clash)})")
                continue
            path = b.work / f"STUB{i:02d}.cbl"
            path.write_text(self._prepare_source(chunk, pid, b))
            obj = b.work / f"STUB{i:02d}.o"
            args = ["-c", *self._flags(dialect, "fixed"), *_inc_args(self.include_dirs(pid)),
                    "-o", str(obj), str(path)]
            ok, log = self._cobc(args, b.work)
            if ok:
                objs.append(obj)
                b.stub_names.update(names)
            else:
                b.compile_log.append(f"stub {','.join(names) or path.name} failed:\n{log}")
                if failed is not None:
                    failed.update(n.upper() for n in names)
        if plan.replaced:
            b.compile_log.append("harness: repo callees replaced by LLM stubs: "
                                 + ", ".join(plan.replaced))
        return objs

    def _plan(self, test: GeneratedTest, b: _Build, res: TestResult,
              drop: frozenset[str] = frozenset()) -> StubPlan:
        known = test.testability.callees if test.testability else None
        plan = plan_stubs(self.index, test.program_id, test.stubs_source, known, drop)
        b.linked = plan.linked
        res.stubbed_callees = list(plan.replaced)
        res.real_callees = list(plan.linked)
        res.skipped_stubs = list(plan.skipped)
        return plan

    def _callee_objects(self, test: GeneratedTest, b: _Build) -> tuple[list[Path], bool]:
        objs: list[Path] = []
        sut_path = self.index.programs[test.program_id].path
        seen = {sut_path}
        ok_all = True
        trace = b.trace if b.trace_callees and b.trace != "none" else False
        for c in b.linked:
            prog = self.index.programs.get(c)
            if prog is None or prog.path in seen:
                continue
            seen.add(prog.path)
            obj, log = self.build_object(c, trace, b)
            if obj is None:
                ok_all = False
                b.compile_log.append(f"callee {c} failed to compile:\n{log}")
                if prog.path in b.overlay:
                    b.compile_log.append(f"{OVERLAY_FAILED} {prog.path}")
            else:
                objs.append(obj)
        return objs, ok_all

    def _static_externals(self, pids: list[str]) -> list[str]:
        """CALL STATIC literal targets of *pids* outside the repository, as written."""
        out: list[str] = []
        for pid in pids:
            prog = self.index.programs.get(pid)
            if prog is None:
                continue
            for c in prog.calls:
                if c.kind != "call" or not getattr(c, "static", False) or \
                        self.index.program_of(c.target) is not None or \
                        is_runtime_routine(c.target):
                    continue
                m = re.search(r"(['\"])(" + re.escape(c.target) + r")\1",
                              self.index.program_source(pid), re.I)
                name = m.group(2) if m else c.target
                if name not in out:
                    out.append(name)
        return out

    def _auto_stubs(self, test: GeneratedTest, b: _Build, taken: set[str]) -> list[Path]:
        """no-op programs for CALL STATIC targets no repo program or stub provides: the
        linker resolves static calls, so without them the executable does not link."""
        objs: list[Path] = []
        dialect = self.program_dialect(test.program_id)[0]
        mod_dir = self.work_dir / "_modules"
        mod_dir.mkdir(parents=True, exist_ok=True)
        for name in self._static_externals([test.program_id, *b.linked]):
            # the linker matches symbols case-sensitively: a stub 'CURS_SET' does not
            # provide 'curs_set'
            if name in b.stub_names or name.upper() in taken:
                continue
            src = ("       IDENTIFICATION DIVISION.\n"
                   f"       PROGRAM-ID. \"{name}\".\n"
                   "       PROCEDURE DIVISION.\n"
                   "           MOVE 0 TO RETURN-CODE\n"
                   "           GOBACK.\n")
            flags = self._flags(dialect, "fixed")
            key = content_hash(src + " ".join(flags))
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", name)
            obj = mod_dir / f"_static_{stem}-{key}.o"
            with self._lock(str(obj)):
                if not obj.exists():
                    path = mod_dir / f"_static_{stem}-{key}.cbl"
                    path.write_text(src)
                    ok, log = self._cobc(["-c", *flags, "-o", str(obj), str(path)], mod_dir)
                    if not ok:
                        b.compile_log.append(f"no-op for CALL STATIC '{name}' failed:\n{log}")
                        continue
            b.compile_log.append(f"harness: CALL STATIC '{name}' (outside the repository, "
                                 f"no stub) linked to a no-op")
            objs.append(obj)
            b.stub_names.add(name)
        return objs

    # -- files ----------------------------------------------------------------------

    def _file_uses(self, test: GeneratedTest) -> list[tuple[str, FileUse]]:
        """(owner program, file) for native files of the SUT and its callees."""
        out = []
        pids = [test.program_id] + (test.testability.callees if test.testability else [])
        for pid in pids:
            prog = self.index.programs.get(pid)
            if prog is None:
                continue
            out.extend((pid, f) for f in prog.files if f.via == "native")
        return out

    def _match_file(self, fx: FixtureFile, uses: list[tuple[str, FileUse]]):
        key = _assign_key(fx.dd)
        for owner, f in uses:
            if key and key in (_assign_key(f.assign), f.name.upper()):
                return owner, f
        for owner, f in uses:  # a literal ASSIGN given by its path
            if f.assign and f.assign.strip("'\"") == fx.path:
                return owner, f
        return None, None

    def _map_env(self, b: _Build, name: str, path: Path) -> None:
        name = name.strip().strip("'\"")
        if not name or "=" in name or "\0" in name:
            return
        for var in (f"DD_{name}", f"dd_{name}", name):
            b.env[var] = str(path)

    def _layout_of(self, fx: FixtureFile, luses: list[tuple[str, FileUse]], owner, f):
        """(runner, owner, file) whose record layout encodes the fixture: the layout
        runner's matching file when there is one, else the SUT's own."""
        if self.layout is not None:
            lowner, lf = self.layout._match_file(fx, luses)
            if lf is not None:
                return self.layout, lowner, lf
        return self, owner, f

    def _layouts(self, owner: str | None, f: FileUse | None) -> list[list[LayoutField]]:
        if owner is None or f is None:
            return []
        prog = self.index.programs[owner]
        dialect = self.layout_dialect(owner)
        return [record_fields(it, dialect)[1] for it in file_records(prog, f, dialect)]

    def _prepare_files(self, test: GeneratedTest, b: _Build) -> list[tuple]:
        """write fixtures, map DD names, load non line-sequential inputs.

        returns the expected-file checks: (fixture, owner, FileUse | None, actual path).
        """
        uses = self._file_uses(test)
        lr = self.layout or self
        luses = lr._file_uses(test) if lr is not self else uses
        # every native file of the SUT gets its own name in the run dir by default, so a
        # stray environment variable can never redirect it
        for _, f in uses:
            if f.assign:
                self._map_env(b, f.assign, b.work / _safe_rel(f.assign.strip("'\"")))
        for n, fx in enumerate(test.inputs, 1):
            owner, f = self._match_file(fx, uses)
            lrun, lowner, lf = self._layout_of(fx, luses, owner, f)
            target = b.work / _safe_rel(fx.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            org = (lf.organization or "SEQUENTIAL") if lf is not None else _LINE_SEQ
            fx.organization = org
            self._map_env(b, f.assign if f is not None else fx.dd, target)
            if lf is not None and lf is not f and lf.assign:
                self._map_env(b, lf.assign, target)  # the data lives under its real DD name
            layouts = lrun._layouts(lowner, lf)
            recs, warns = [], []
            for r in fixture_records(fx):
                data, w = encode_record(r, layouts)
                recs.append(data)
                warns += w
            if warns:
                b.run_log.append(f"fixture {fx.path}: " + "; ".join(sorted(set(warns))[:20]))
            if org == _LINE_SEQ or lf is None:
                if any(b"\n" in r for r in recs):
                    b.run_log.append(f"fixture {fx.path}: a record holds a newline byte")
                target.write_bytes(b"".join(r + b"\n" for r in recs))
                continue
            seq = b.work / f"_fixture{n:02d}.bin"
            seq.write_bytes(b"".join(r[:_XFER].ljust(_XFER, b" ") for r in recs))
            ok, log = lrun._convert(lowner, lf, seq, target, b, load=True, n=n)
            b.run_log.append(f"loader for {lf.name} ({org}): {'ok' if ok else 'FAILED'}\n{log}")
        checks = []
        for fx in test.expected_files:
            owner, f = self._match_file(fx, uses)
            lrun, lowner, lf = self._layout_of(fx, luses, owner, f)
            actual = b.work / _safe_rel(fx.path)
            if lf is not None:
                fx.organization = lf.organization or "SEQUENTIAL"
            self._map_env(b, f.assign if f is not None else fx.dd, actual)
            if lf is not None and lf is not f and lf.assign:
                self._map_env(b, lf.assign, actual)
            checks.append((fx, (lrun, lowner), lf, actual))
        return checks

    def _convert(self, owner: str, f: FileUse, seq: Path, target: Path, b: _Build,
                 load: bool, n: int) -> tuple[bool, str]:
        """generate, compile and run a loader (text -> file) or unloader (file -> text)."""
        prog = self.index.programs[owner]
        texts = [self.index.program_source(owner)] + [
            t for t in (self.index.copybook_source(c, owner) for c in prog.copybooks) if t
        ]
        src = conversion_program(f, texts, prog.format, seq.name, _rel(target, b.work), load,
                                 name=f"CDL{'L' if load else 'U'}{n:03d}",
                                 copybook=lambda m: self.index.copybook_source(m, owner))
        if src is None:
            return False, f"SELECT/FD for {f.name} not found in {owner}"
        tag = f"_{'load' if load else 'unload'}{n:02d}"
        path = b.work / f"{tag}.cbl"
        path.write_text(wrap_free_copies(src, self._copybook_is_free(owner)))
        exe = b.work / tag
        dialect = self.program_dialect(owner)[0]
        args = ["-x", *self._flags(dialect, "fixed"), *_inc_args(self.include_dirs(owner)),
                "-o", str(exe), str(path)]
        ok, log = self._cobc(args, b.work)
        if not ok:
            return False, f"$ cobc {' '.join(args)}\n{log}"
        rc, out, _ = self._exec([str(exe)], b, {}, "", record=False)
        return rc == 0, out.strip()

    # -- running ----------------------------------------------------------------------

    def _exec(self, cmd: list[str], b: _Build, extra_env: dict[str, str], stdin: str,
              timeout: int | None = None, record: bool = True
              ) -> tuple[int | None, str, bool]:
        """run a program in the work dir: (return code | None on timeout, output, crashed).

        own session (the process group is killed on timeout), COB_* variables removed,
        TMPDIR in the run dir, RLIMIT_FSIZE (lower when traced) and no core dumps. A crash is
        a signal (negative status or 128 + signal) or a libcob runtime error."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("COB_")}
        env.update(b.env)
        env.update(extra_env)
        tmp = b.work / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp)
        traced = "COB_SET_TRACE" in extra_env
        fsize = self.trace_fsize_limit if traced else self.fsize_limit

        def limits() -> None:  # in the child, before exec
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except (ImportError, ValueError, OSError):
                pass

        data = stdin.encode("utf-8", "replace") if stdin else None
        try:
            p = subprocess.Popen(cmd, cwd=b.work, env=env,
                                 stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True, preexec_fn=limits)
        except OSError as e:
            if record:
                b.returncode, b.crashed = 127, True
            return 127, f"cannot start {cmd[0]}: {e}", True
        try:
            out_b, err_b = p.communicate(data, timeout=timeout or b.timeout or self.timeout)
        except subprocess.TimeoutExpired:
            _killpg(p)
            try:
                out_b, err_b = p.communicate(timeout=10)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                out_b, err_b = b"", b""
            out = out_b.decode("utf-8", "replace") + (
                "\n" + err_b.decode("utf-8", "replace") if err_b else "")
            if record:
                b.returncode, b.crashed = -1, True
            return None, out, True
        _killpg(p)  # grandchildren left behind
        out = out_b.decode("utf-8", "replace") + (
            "\n" + err_b.decode("utf-8", "replace") if err_b else "")
        rc = p.returncode
        sig = -rc if rc < 0 else (rc - 128 if 128 < rc < 160 else 0)
        crashed = sig > 0 or bool(_LIBCOB_ERROR.search(out))
        if not record:
            return rc, out, crashed
        m = _SIGNAL_MESSAGE.search(out)
        b.signal_detail = m.group(0) if m else ""
        if rc < 0:
            try:
                name = signal.Signals(-rc).name
            except ValueError:
                name = str(-rc)
            out += f"\n(killed by signal {name})"
            b.signal_detail = b.signal_detail or f"signal {name}"
        elif sig and not b.signal_detail:
            try:
                b.signal_detail = f"signal {signal.Signals(sig).name}"
            except ValueError:
                b.signal_detail = f"exit status {rc}"
        b.returncode, b.crashed = rc, crashed
        return rc, out, crashed

    def _run_env(self, test: GeneratedTest, b: _Build) -> dict[str, str]:
        env = {k: v.replace("${WORK_DIR}", str(b.work)) for k, v in test.env.items()}
        env.update(self._trace_env(b))
        env["COB_LIBRARY_PATH"] = str(self.work_dir / "_modules")
        return env

    @staticmethod
    def _trace_env(b: _Build) -> dict[str, str]:
        if b.trace == "none":
            return {}
        return {"COB_SET_TRACE": "Y", "COB_TRACE_FILE": str(b.work / "trace.txt"),
                "COB_TRACE_FORMAT": TRACE_FORMAT}

    def _coverage(self, pid: str, b: _Build, res: TestResult) -> None:
        """paragraph coverage of the SUT (paragraphs_hit / paragraphs_total), plus per-file
        line and paragraph hits of every traced program (streamed: the trace file can be
        large)."""
        prog = self.index.programs[pid]
        names = {p.name.upper() for p in prog.paragraphs if p.name != "(entry)"}
        res.paragraphs_total = len(names)
        trace = b.work / "trace.txt"
        if not trace.exists():
            return
        heads: dict[str, set[int]] = {}
        lines, paras = parse_trace(trace, self._trace_rel(b), self._trace_files(), heads)
        res.line_hits = {rel: dict(sorted(hits.items())) for rel, hits in sorted(lines.items())}
        res.paragraph_hits = {rel: sorted(hit) for rel, hit in sorted(paras.items())}
        res.paragraph_lines = {rel: sorted(ns) for rel, ns in sorted(heads.items())}
        if not names:
            return
        hit = set(paras.get(prog.path, set()))
        # the default trace format of an unknown source path: by program id only
        hit |= paras.get(f"#{pid.upper()}", set())
        res.paragraphs_hit = len(names & {h.upper() for h in hit})

    def _trace_rel(self, b: _Build) -> Callable[[str], str | None]:
        cache: dict[str, str | None] = {}

        def rel(path: str) -> str | None:
            if path not in cache:
                cache[path] = b.sources.get(path) or self.rel_of_source(path)
            return cache[path]

        return rel

    def _trace_files(self) -> dict[str, str]:
        """upper-cased program / function id -> its file rel."""
        return {pid.upper(): p.path for pid, p in self.index.programs.items()}

    def run(self, test: GeneratedTest, *, overlay: dict[str, str] | None = None,
            trace: str = "paragraph", work: Path | None = None,
            timeout: int | None = None) -> TestResult:
        """build and run one GeneratedTest.

        overlay: rel -> text replacing program files for this run; trace: none | paragraph
        (SUT with -ftrace) | statement (every linked repo program with -ftraceall);
        work: the run dir (default: work_dir/<program>, cleared first). A statement-traced
        run killed by the trace file size limit is run again with paragraph tracing."""
        res = self._run(test, overlay, trace, work, timeout)
        if trace == "statement" and _xfsz(res.exit_status):
            again = self._run(test, overlay, "paragraph", work, timeout)
            again.run_log = (f"harness: {XFSZ_NOTE}\n\n" + again.run_log).strip()
            return again
        if _xfsz(res.exit_status):
            res.run_log = (res.run_log + "\n\nharness: trace file limit reached").strip()
        return res

    def _work(self, name: str, work: Path | None) -> Path:
        if work is not None:
            path = Path(work)
        elif self.tmp_dir is not None:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="b", dir=self.tmp_dir)).resolve()
        else:
            path = self.work_dir / _safe_rel(name)
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def _run(self, test: GeneratedTest, overlay: dict[str, str] | None, trace: str,
             work: Path | None, timeout: int | None = None) -> TestResult:
        if trace not in TRACE_MODES:
            raise ValueError(f"trace must be one of {TRACE_MODES}, not {trace!r}")
        t0 = time.time()
        res = TestResult(program_id=test.program_id, mode=test.mode, status="skipped")
        if test.program_id not in self.index.programs:
            res.run_log = "program not in index"
        elif test.error and not (test.driver_source or test.inputs or test.expected_files
                                 or test.expected_stdout or test.cases and test.mode == "spec"):
            res.status = "generation-error"
            res.run_log = test.error
        elif test.mode == "spec":
            res.status = "not-executable"
            res.cases = [CaseResult(id=c.id, status="not-run") for c in test.cases]
        elif not self.available():
            res.run_log = f"{self.cobc} not available"
        else:
            b = _Build(self._work(test.program_id, work), overlay, trace)
            b.timeout = timeout
            try:
                res.dialect = self.program_dialect(test.program_id)[0]
                if test.mode == "unit":
                    self._run_unit(test, b, res)
                else:
                    self._run_batch(test, b, res)
            except Exception as e:  # keep one broken test from killing the batch
                res.status = "runtime-error"
                b.run_log.append(f"harness error: {e!r}")
            if res.status not in ("compile-error",) and trace != "none":
                self._coverage(test.program_id, b, res)
            elif res.status not in ("compile-error",):
                res.paragraphs_total = len({p.name.upper() for p in
                                            self.index.programs[test.program_id].paragraphs
                                            if p.name != "(entry)"})
            res.crashed = b.crashed
            res.exit_status = b.returncode
            res.compile_log = _cap("\n\n".join(x for x in b.compile_log if x))
            res.run_log = _cap("\n\n".join(x for x in b.run_log if x))
            if b.signal_detail:
                res.run_log = (res.run_log + f"\n\nharness: crash detail: {b.signal_detail}"
                               ).strip()
        res.passed = sum(c.status == "pass" for c in res.cases)
        res.failed = sum(c.status == "fail" for c in res.cases)
        res.weak = sum(c.status == "weak" for c in res.cases)
        res.duration_s = round(time.time() - t0, 2)
        return res

    def _taken_names(self, test: GeneratedTest, b: _Build) -> set[str]:
        """names the linked repo programs define: PROGRAM-/FUNCTION-IDs and ENTRYs (a stub
        of the same name would be a duplicate symbol)."""
        names: set[str] = set()
        for pid in [test.program_id, *b.linked]:
            names.add(pid.upper())
            prog = self.index.programs.get(pid)
            if prog is not None:
                names |= {e.upper() for e in prog.entry_points}
                names |= {n.upper() for n in prog.nested_programs}
        return names

    def _link_parts(self, test: GeneratedTest, b: _Build, res: TestResult) -> list[Path]:
        """callee objects (minus stubbed ones), LLM stubs and CALL STATIC no-ops."""
        plan = self._plan(test, b, res)
        taken = self._taken_names(test, b)
        failed: set[str] = set()
        stub_objs = self._build_stubs(test, b, plan, taken, failed)
        if failed and any(self.index.program_of(n) in plan.replaced for n in failed):
            # a stub that replaced a repo program does not compile: link the real program
            # instead of leaving its CALLs unresolved (deterministic, no LLM call)
            plan = self._plan(test, b, res, frozenset(failed))
            b.stub_names.clear()
            taken = self._taken_names(test, b)
            stub_objs = self._build_stubs(test, b, plan, taken)
        callee_objs, _ = self._callee_objects(test, b)
        stub_objs += self._auto_stubs(test, b, taken)
        return callee_objs + stub_objs

    @staticmethod
    def _sut_trace(b: _Build) -> bool | str:
        return False if b.trace == "none" else b.trace

    def _overlay_failed(self, pid: str, b: _Build) -> None:
        rel = self.index.programs[pid].path
        if rel in b.overlay:
            b.compile_log.append(f"{OVERLAY_FAILED} {rel}")

    def cobc_info(self) -> str:
        """`cobc --info` output (probed once; "" when cobc does not answer)."""
        if self._info is None:
            try:
                p = subprocess.run([self.cobc, "--info"], capture_output=True, text=True,
                                   errors="replace", timeout=60)
                self._info = p.stdout if p.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                self._info = ""
        return self._info

    def screen_library(self) -> str:
        """the curses library libcob uses for extended screen I/O ("" when none)."""
        m = re.search(r"extended screen I/O\s*:\s*(\S+)", self.cobc_info())
        lib = m.group(1).lower() if m else ""
        return "" if lib in ("", "disabled", "no", "none") else lib

    def _link_libs(self, pids: list[str], b: _Build) -> list[str]:
        """-l<curses> when a linked program uses screen I/O or CALLs a curses routine."""
        need = []
        for pid in pids:
            prog = self.index.programs.get(pid)
            if prog is None:
                continue
            if prog.uses_screen_io or any(c.target.lower() in _SCREEN_SYMBOLS
                                          for c in prog.calls if c.kind == "call"):
                need.append(pid)
        if not need:
            return []
        lib = self.screen_library()
        if not lib:
            b.compile_log.append("harness: no screen library reported by cobc --info for "
                                 + ", ".join(need))
            return []
        b.compile_log.append(f"harness: linked with -l{lib} (screen I/O: {', '.join(need)})")
        return [f"-l{lib}"]

    def _run_unit(self, test: GeneratedTest, b: _Build, res: TestResult) -> None:
        pid = test.program_id
        sut_obj, log = self.build_object(pid, self._sut_trace(b), b)
        b.compile_log.append(log)
        if sut_obj is None:
            res.status = "compile-error"
            b.compile_log.append("program under test failed to compile")
            self._overlay_failed(pid, b)
            return
        objs = self._link_parts(test, b, res)
        name = test.driver_name or "TDRIVER"
        drv = b.work / f"{_safe_rel(name)}.cbl"
        drv.write_text(self._prepare_source(test.driver_source, pid, b))
        exe = b.work / "driver"
        dialect = self.program_dialect(pid)[0]
        args = ["-x", *self._flags(dialect, "fixed"), *_inc_args(self.include_dirs(pid)),
                "-o", str(exe), str(drv), str(sut_obj), *map(str, objs),
                *self._link_libs([pid, *b.linked], b)]
        ok, log = self._cobc(args, b.work)
        b.compile_log.append(f"$ cobc {' '.join(args)}\n{log}".strip())
        if not ok:
            res.status = "compile-error"
            return
        res.compiled = True
        checks = self._prepare_files(test, b)
        rc, out, crashed = self._exec([str(exe)], b, self._run_env(test, b),
                                      "\n".join(test.stdin) + ("\n" if test.stdin else ""))
        res.return_code = rc
        b.run_log.append(out)
        seen, res.started = parse_verdicts(out)
        cases = [seen.pop(c.id, None) or CaseResult(id=c.id, status="not-run")
                 for c in test.cases]
        cases.extend(seen.values())
        # a PASS printed unconditionally checks nothing
        weak = dict(test.weak_cases)
        weak.update(weak_cases(test.driver_source, [c.id for c in test.cases]))
        for c in cases:
            if c.status == "pass" and c.id in weak:
                c.status, c.detail = "weak", weak[c.id]
        run_checks = self._output_checks(test, rc, out)
        self._grade_ending_cases(test, cases, run_checks, rc, crashed)
        cases.extend(run_checks)
        cases.extend(self._file_checks(checks, b))
        res.cases = cases
        n_fail = sum(c.status == "fail" for c in cases)
        n_pass = sum(c.status == "pass" for c in cases)
        n_weak = sum(c.status == "weak" for c in cases)
        n_skip = sum(c.status == "not-run" for c in cases)
        if rc is None:
            res.status = "timeout"
        elif crashed or n_pass + n_fail + n_weak == 0:
            res.status = "runtime-error"
        elif n_fail or n_skip:
            res.status = "failed"
        elif n_pass == 0:
            res.status = "weak"
        else:
            res.status = "passed"

    def _output_checks(self, test: GeneratedTest, rc: int | None, out: str
                       ) -> list[CaseResult]:
        """expected_return_code and the ordered expected_stdout checks of a run. In unit
        mode, a stdout expectation that only the driver prints (its own TEST lines or
        literals) duplicates the driver's TEST verdicts: dropped, not graded."""
        cases: list[CaseResult] = []
        if rc is None:
            return cases
        if test.expected_return_code is not None:
            ok = rc == test.expected_return_code
            cases.append(CaseResult(id="return-code", status="pass" if ok else "fail",
                                    detail="" if ok else f"expected {test.expected_return_code}"
                                                         f" got {rc}"))
        expected = list(test.expected_stdout)
        if test.mode == "unit" and test.driver_source:
            # the model often copies the driver's own TEST lines / literals into
            # expected_stdout; they only mirror verdicts already graded per case
            sut = self.index.program_source(test.program_id)
            expected = [item for item in expected
                        if not driver_echo(_stdout_item(item)[0], test.driver_source, sut)]
        cases.extend(match_stdout(expected, out))
        return cases

    @staticmethod
    def _grade_ending_cases(test: GeneratedTest, cases: list[CaseResult],
                            run_checks: list[CaseResult], rc: int | None,
                            crashed: bool) -> None:
        """a case whose SUT path ends the run (STOP RUN) prints no TEST line: grade it by
        the stdout / return-code checks (flagged ends_run, or the last case)."""
        real = [c for c in run_checks if c.status != "weak"]
        if rc is None or crashed or not real or not test.cases:
            return
        by_id = {c.id: c for c in cases}
        last = test.cases[-1].id
        for spec in test.cases:
            r = by_id.get(spec.id)
            if r is None or r.status != "not-run" or not (spec.ends_run or spec.id == last):
                continue
            bad = [c.id for c in real if c.status != "pass"]
            r.status = "fail" if bad else "pass"
            r.detail = ("run ended in the SUT; graded by " + ", ".join(c.id for c in real)
                        + (f" (failed: {', '.join(bad)})" if bad else ""))

    def _parm_wrapper(self, test: GeneratedTest, b: _Build) -> tuple[Path, str]:
        """a main program that CALLs the SUT with the JCL PARM structure."""
        pid = test.program_id
        t = test.testability
        text = test.parm if test.parm is not None else (t.jcl_parms[0] if t and t.jcl_parms
                                                        else "")
        text = text[:100]
        name, n = "CDPARMWR", 0
        while name in self.index.programs or name in self.index.entry_owner:
            n += 1
            name = f"CDPARM{n:02d}"
        sut = exact_program_name(self.index.program_source(pid), pid)
        lit = "'" + text.replace("'", "''") + "'" if text else "SPACES"
        src = reflow_fixed(f"""IDENTIFICATION DIVISION.
PROGRAM-ID. {name}.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 CDP-PARM.
    05 CDP-LEN PIC S9(4) COMP.
    05 CDP-TEXT PIC X(100).
PROCEDURE DIVISION.
    MOVE {len(text)} TO CDP-LEN
    MOVE {lit} TO CDP-TEXT
    CALL '{sut}' USING CDP-PARM
    STOP RUN.
""")
        path = b.work / f"{name}.cbl"
        path.write_text(src)
        b.compile_log.append(f"harness: PARM wrapper {name} CALLs '{sut}' with PARM "
                             f"{text!r} (halfword length {len(text)})")
        return path, name

    def _run_batch(self, test: GeneratedTest, b: _Build, res: TestResult) -> None:
        pid = test.program_id
        prog = self.index.programs[pid]
        dialect, note = self.program_dialect(pid)
        b.compile_log.append(note)
        exe = b.work / "program"
        if test.testability and test.testability.parm_param:
            sut_obj, log = self.build_object(pid, self._sut_trace(b), b)
            b.compile_log.append(log)
            if sut_obj is None:
                res.status = "compile-error"
                b.compile_log.append("program under test failed to compile")
                self._overlay_failed(pid, b)
                return
            objs = self._link_parts(test, b, res)
            wrapper, _ = self._parm_wrapper(test, b)
            args = ["-x", *self._flags(dialect, "fixed"), *_inc_args(self.include_dirs(pid)),
                    "-o", str(exe), str(wrapper), str(sut_obj), *map(str, objs),
                    *self._link_libs([pid, *b.linked], b)]
        else:
            objs = self._link_parts(test, b, res)
            src, _ = self._program_source(pid, b)
            b.sources[str(src)] = prog.path
            args = ["-x", *self._trace_flags(self._sut_trace(b)),
                    *self._flags(dialect, prog.format),
                    *_inc_args(self.include_dirs(pid)), "-o", str(exe),
                    str(src), *map(str, objs), *self._link_libs([pid, *b.linked], b)]
        ok, log = self._cobc(args, b.work)
        b.compile_log.append(f"$ cobc {' '.join(args)}\n{log}".strip())
        if not ok:
            res.status = "compile-error"
            if not (test.testability and test.testability.parm_param):
                self._overlay_failed(pid, b)
            return
        res.compiled = True
        checks = self._prepare_files(test, b)
        rc, out, crashed = self._exec([str(exe)], b, self._run_env(test, b),
                                      "\n".join(test.stdin) + ("\n" if test.stdin else ""))
        res.return_code = rc
        b.run_log.append(out)
        if rc is None:
            res.status = "timeout"
            return
        cases = self._output_checks(test, rc, out)
        cases.extend(self._file_checks(checks, b))
        if not cases:
            cases.append(CaseResult(id="completes", status="fail" if crashed else "pass",
                                    detail="runtime error" if crashed else ""))
        res.cases = cases
        if any(c.status == "fail" for c in cases):
            res.status = "runtime-error" if crashed else "failed"
        else:
            res.status = "passed"

    def _file_checks(self, checks: list[tuple], b: _Build) -> list[CaseResult]:
        out: list[CaseResult] = []
        for n, (fx, (lrun, owner), f, actual) in enumerate(checks, 1):
            cid = f"file-{fx.dd or fx.path}"
            if not actual.exists():
                out.append(CaseResult(id=cid, status="fail", detail=f"{fx.path} not written"))
                continue
            if f is not None and (f.organization or "SEQUENTIAL") != _LINE_SEQ:
                dump = b.work / f"_unload{n:02d}.bin"
                ok, log = lrun._convert(owner, f, dump, actual, b, load=False, n=n)
                b.run_log.append(f"unloader for {f.name}: {'ok' if ok else 'FAILED'}\n{log}")
                if not ok or not dump.exists():
                    out.append(CaseResult(id=cid, status="fail", detail="could not unload"))
                    continue
                data = dump.read_bytes()
                got = [data[i:i + _XFER] for i in range(0, len(data), _XFER)]
            else:
                got = [ln.rstrip(b"\r") for ln in actual.read_bytes().split(b"\n")]
                while got and not got[-1].strip():
                    got.pop()
            problem = compare_records(fixture_records(fx), got, lrun._layouts(owner, f))
            out.append(CaseResult(id=cid, status="fail" if problem else "pass", detail=problem))
        return out

    def run_all(
        self,
        tests: dict[str, GeneratedTest],
        out_dir: Path | None = None,
        workers: int = 4,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, TestResult]:
        """run every test (in parallel); with *out_dir*, also write the test artifacts."""
        results: dict[str, TestResult] = {}
        done = 0
        lock = threading.Lock()

        def one(pid: str) -> None:
            nonlocal done
            r = self.run(tests[pid])
            with lock:
                results[pid] = r
                done += 1
                if on_progress:
                    on_progress(f"[{done}/{len(tests)}] run {pid}: {r.status} "
                                f"({r.passed} pass, {r.failed} fail)")

        with ThreadPoolExecutor(max(1, workers)) as ex:
            list(ex.map(one, sorted(tests)))
        results = {k: results[k] for k in sorted(results)}
        if out_dir is not None:
            write_run_artifacts(tests, results, Path(out_dir))
        return results

    # -- existing test programs, compile checks, statement universes -------------------

    def program_of_file(self, rel: str) -> str | None:
        """the primary (first top-level) program of a program file."""
        pids = getattr(self.index, "_file_programs", {}).get(rel) or []
        if pids:
            return pids[0]
        return next((pid for pid, p in self.index.programs.items() if p.path == rel), None)

    def run_program(self, rel: str, *, overlay: dict[str, str] | None = None,
                    trace: str = "none", work: Path | None = None,
                    timeout: int | None = None) -> TestResult:
        """an existing test program run as a main program: `cobc -x <program> <linked repo
        callees> <CALL STATIC no-ops>` in the run dir. Its cases are its `TEST <id> PASS|FAIL`
        lines; overlay replaces repo program files (the target under test) for this run. A
        statement-traced run killed by the trace file size limit is run again with paragraph
        tracing."""
        res = self._run_program(rel, overlay=overlay, trace=trace, work=work, timeout=timeout)
        if trace == "statement" and _xfsz(res.exit_status):
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            again = self._run_program(rel, overlay=overlay, trace="paragraph", work=work,
                                      timeout=timeout)
            again.run_log = (f"harness: {XFSZ_NOTE}\n\n" + again.run_log).strip()
            return again
        if _xfsz(res.exit_status):
            res.run_log = (res.run_log + "\n\nharness: trace file limit reached").strip()
        return res

    def _run_program(self, rel: str, *, overlay: dict[str, str] | None = None,
                    trace: str = "none", work: Path | None = None,
                    timeout: int | None = None) -> TestResult:
        """an existing test program run as a main program: `cobc -x <program> <linked repo
        callees> <CALL STATIC no-ops>` in the run dir. Its cases are its `TEST <id> PASS|FAIL`
        lines; overlay replaces repo program files (the target under test) for this run."""
        if trace not in TRACE_MODES:
            raise ValueError(f"trace must be one of {TRACE_MODES}, not {trace!r}")
        t0 = time.time()
        pid = self.program_of_file(rel)
        res = TestResult(program_id=pid or rel, mode="program", status="skipped")
        if pid is None:
            res.run_log = f"{rel}: no program in the index"
            return res
        if not self.available():
            res.run_log = f"{self.cobc} not available"
            return res
        b = _Build(self._work(pid, work), overlay, trace)
        b.trace_callees = trace != "none"
        b.timeout = timeout
        prog = self.index.programs[pid]
        try:
            dialect, note = self.program_dialect(pid)
            res.dialect = dialect
            b.compile_log.append(note)
            test = GeneratedTest(program_id=pid, mode="batch")
            objs = self._link_parts(test, b, res)
            src, _ = self._program_source(pid, b)
            b.sources[str(src)] = prog.path
            exe = b.work / "program"
            args = ["-x", *self._trace_flags(self._sut_trace(b)),
                    *self._flags(dialect, prog.format), *_inc_args(self.include_dirs(pid)),
                    "-o", str(exe), str(src), *map(str, objs),
                    *self._link_libs([pid, *b.linked], b)]
            ok, log = self._cobc(args, b.work)
            b.compile_log.append(f"$ cobc {' '.join(args)}\n{log}".strip())
            if not ok:
                res.status = "compile-error"
            else:
                res.compiled = True
                for owner in [pid, *b.linked]:
                    p = self.index.programs.get(owner)
                    for f in (p.files if p is not None else []):
                        if f.via == "native" and f.assign:
                            self._map_env(b, f.assign,
                                          b.work / _safe_rel(f.assign.strip("'\"")))
                rc, out, crashed = self._exec([str(exe)], b, self._trace_env(b), "")
                res.return_code = rc
                b.run_log.append(out)
                seen, res.started = parse_verdicts(out)
                res.cases = list(seen.values())
                if rc is None:
                    res.status = "timeout"
                elif crashed:
                    res.status = "runtime-error"
                elif any(c.status == "fail" for c in res.cases):
                    res.status = "failed"
                elif not res.cases and rc != 0:
                    res.status = "failed"
                else:
                    res.status = "passed"
        except Exception as e:  # keep one broken program from killing the batch
            res.status = "runtime-error"
            b.run_log.append(f"harness error: {e!r}")
        if res.compiled and trace != "none":
            self._coverage(pid, b, res)
        res.crashed = b.crashed
        res.exit_status = b.returncode
        res.compile_log = _cap("\n\n".join(x for x in b.compile_log if x))
        res.run_log = _cap("\n\n".join(x for x in b.run_log if x))
        if b.signal_detail:
            res.run_log = (res.run_log + f"\n\nharness: crash detail: {b.signal_detail}").strip()
        res.passed = sum(c.status == "pass" for c in res.cases)
        res.failed = sum(c.status == "fail" for c in res.cases)
        res.duration_s = round(time.time() - t0, 2)
        return res

    def _dcl_flags(self, pid: str | None) -> list[str]:
        """-ext dcl / -ext dclgen when the program's resolved members are DCLGEN files."""
        used = getattr(self.index, "_copy_used", {}).get(pid or "", {})
        exts = {PurePosixPath(p).suffix.lower() for p in used.values()}
        out: list[str] = []
        for ext in (".dcl", ".dclgen"):
            if ext in exts:
                out += ["-ext", ext[1:]]
        return out

    def check_dirs(self, rel: str) -> list[Path]:
        pid = self.program_of_file(rel)
        if pid is not None:
            return self.include_dirs(pid)
        return self.mirror.dirs(self.index.copybook_dirs())

    def compile_check(self, rel: str, text: str | None = None, *, neutralize: bool = False
                      ) -> tuple[bool, list[str], str]:
        """(ok, mapped errors, log) of `cobc -fsyntax-only` on a program file (its mirror
        text, or *text*) in the program's dialect; neutralize: EXEC blocks neutralised
        first (codocs.cobol.precompile), errors mapped back to the original lines."""
        if not self.available():
            return False, [f"{self.cobc} not available"], ""
        pid = self.program_of_file(rel)
        fmt = self._file_format(rel)
        if text is None:
            text = self.mirror.text(rel)
            if text is None:
                return False, [f"{rel}: not a mirrored program file"], ""
        text = expand_tabs(text.replace("\r\n", "\n"), fmt)
        incs = self.check_dirs(rel)
        if pid is not None:
            dialect = self.program_dialect(pid)[0]
        else:
            dialect = "ibm" if fmt == "fixed" else "default"
        flags = self._flags(dialect, fmt)
        key = content_hash("\0".join([rel, text, " ".join(flags), str(neutralize)]))
        with self._lock(f"check:{key}"):
            hit = self._checks.get(key)
            if hit is not None:
                return hit
            d = self.work_dir / "_check" / key
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            line_map: list[int] | None = None
            extra: list[Path] = []
            if neutralize:
                from codocs.cobol.precompile import neutralize as neutral

                pre = neutral(text, fmt, incs, index=self.index, gen_dir=d / "gen")
                text, line_map, extra = pre.text, pre.line_map, list(pre.include_dirs)
                incs = list(pre.override_dirs) + incs
            src = d / PurePosixPath(rel).name
            src.write_bytes(text.encode(self.mirror.encoding(rel), errors="replace"))
            args = ["-fsyntax-only", *flags, *self._dcl_flags(pid),
                    *_inc_args(incs + extra), str(src)]
            ok, log = self._cobc(args, d)
            errors = self.map_errors(log, {str(src): rel}, {rel: line_map} if line_map else {})
            if not ok and not errors:
                errors = [f"{rel}:1: error: {_first_error(log)}"]
            out = (ok, errors, f"$ cobc {' '.join(args)}\n{log}".strip())
            self._checks[key] = out
            shutil.rmtree(d, ignore_errors=True)
            return out

    def map_errors(self, log: str, paths: dict[str, str] | None = None,
                   line_maps: dict[str, list[int]] | None = None) -> list[str]:
        """`path:line: error: message` lines of a cobc log as `rel:line: error: message`
        (mirror and overlay paths mapped to repo rels; warnings dropped)."""
        out: list[str] = []
        paths = paths or {}
        line_maps = line_maps or {}
        for raw in log.splitlines():
            m = _ERROR_LINE.match(raw.strip())
            if not m:
                continue
            path, num, msg = m.group(1), int(m.group(2)), m.group(3).strip()
            rel = paths.get(path) or self.rel_of_source(path)
            if rel is None:
                rel = PurePosixPath(path).name
            lm = line_maps.get(rel)
            if lm and 0 < num <= len(lm):
                num = lm[num - 1]
            line = f"{rel}:{num}: error: {msg}"
            if line not in out:
                out.append(line)
        return out[:MAX_BUILD_ERRORS]

    def statement_lines(self, rel: str, text: str | None = None) -> set[int]:
        """the executable-statement universe of a program file: the lines of the statement
        markers of `cobc -C -ftraceall` (cached by content), else the statement-verb lines
        the parser finds (files that do not translate)."""
        fmt = self._file_format(rel)
        base = self.mirror.text(rel) if text is None else expand_tabs(text, fmt)
        if base is None:
            return set()
        pid = self.program_of_file(rel)
        dialect = self.program_dialect(pid)[0] if pid is not None and self.available() else \
            ("ibm" if fmt == "fixed" else "default")
        flags = self._flags(dialect, fmt)
        key = content_hash("\0".join([rel, base, " ".join(flags)]))
        if key in self._lines:
            return set(self._lines[key])
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", PurePosixPath(rel).stem)
        cache = self.work_dir / "_modules" / f"{stem}-{key}.lines.json"
        try:
            lines = set(json.loads(cache.read_text(encoding="utf-8")))
            self._lines[key] = lines
            return set(lines)
        except (OSError, ValueError, TypeError):
            pass
        lines: set[int] | None = None
        if self.available():
            src = self.mirror.path(rel) if text is None else self.overlay_source(rel, text)
            with tempfile.TemporaryDirectory(dir=self._tmp_base()) as tmp:
                out_c = Path(tmp) / "x.c"
                args = ["-C", "-ftraceall", *flags, *self._dcl_flags(pid),
                        *_inc_args(self.check_dirs(rel)), "-o", str(out_c), str(src)]
                ok, _log = self._cobc(args, Path(tmp))
                if ok and out_c.exists():
                    lines = c_marker_lines(out_c, str(src))
        if lines is None:
            from codocs.cobol.mutate import statement_verb_lines

            lines = statement_verb_lines(base, fmt)
        else:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                cache.write_text(json.dumps(sorted(lines)), encoding="utf-8")
        self._lines[key] = set(lines)
        return set(lines)

    def _tmp_base(self) -> Path:
        d = self.work_dir / "_tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d


MAX_BUILD_ERRORS = 50


def c_marker_lines(c_file: Path, source: str) -> set[int]:
    """statement lines of one source file among the `/* Line: n : VERB : file */` markers of
    cobc -C output (Entry, Paragraph, Section, Exit and last-source-line markers excluded)."""
    out: set[int] = set()
    want = {source, str(Path(source).resolve())}
    files = [c_file, *sorted(c_file.parent.glob(c_file.name + "*.h"))]
    for f in files:
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for raw in fh:
                if "/* Line:" not in raw:
                    continue
                m = _C_MARKER.search(raw)
                if not m or m.group(3) not in want:
                    continue
                kind = m.group(2).strip()
                if kind.startswith(_NOT_STATEMENTS):
                    continue
                out.add(int(m.group(1)))
    return out


def write_run_artifacts(tests: dict[str, GeneratedTest], results: dict[str, TestResult],
                        out_dir: Path) -> None:
    """<out>/<PROG>/{plan.json, result.json} and <out>/summary.json of a run_all batch."""
    out_dir.mkdir(parents=True, exist_ok=True)
    statuses: dict[str, int] = {}
    for pid, test in sorted(tests.items()):
        d = out_dir / _safe_rel(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan.json").write_text(test.model_dump_json(indent=2), encoding="utf-8")
        res = results.get(pid)
        if res is not None:
            (d / "result.json").write_text(res.model_dump_json(indent=2), encoding="utf-8")
            statuses[res.status] = statuses.get(res.status, 0) + 1
    summary = {"programs": len(tests), "statuses": dict(sorted(statuses.items())),
               "passed": sum(r.passed for r in results.values()),
               "failed": sum(r.failed for r in results.values())}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


_NUL = "\x00"  # data-item placeholder in oracle.display_texts
_DRIVER_TEST_RE = re.compile(r"\bTEST\s+\S+\s+(PASS|FAIL)\b", re.I)


def driver_echo(text: str, driver_source: str, sut_source: str) -> bool:
    """an expected output line that the driver prints itself: a TEST line, or text inside
    one of the driver's DISPLAY literals that the SUT's source does not contain."""
    if not text.strip():
        return False
    if _DRIVER_TEST_RE.search(text):
        return True
    if text in sut_source:
        return False
    return any(text in part for d in display_texts(driver_source) for part in d.split(_NUL))


def _stdout_item(item: str | dict[str, str]) -> tuple[str, bool]:
    """(text, whole line?) of one expected_stdout entry."""
    if isinstance(item, dict):
        if "line" in item:
            return str(item["line"]).rstrip(), True
        return str(item.get("text", "")).strip(), False
    return str(item).strip(), False


def match_stdout(expected: list[str | dict[str, str]], out: str) -> list[CaseResult]:
    """ordered-subsequence check of the DISPLAY output: each expectation must occur after
    the previous match (a substring, or a whole line with trailing spaces ignored), and
    each occurrence is consumed once, so order and repeated lines are enforced."""
    lines = out.splitlines()
    li, col = 0, 0  # cursor: line index, column in that line
    results: list[CaseResult] = []
    for k, item in enumerate(expected, 1):
        text, whole = _stdout_item(item)
        hit: tuple[int, int] | None = None
        for j in range(li, len(lines)):
            if whole:
                if j == li and col > 0:
                    continue  # partially consumed: a whole line must be a later one
                if lines[j].rstrip() == text:
                    hit = (j + 1, 0)
                    break
            else:
                pos = lines[j].find(text, col if j == li else 0)
                if pos >= 0:
                    hit = (j, pos + len(text))
                    break
        cid = f"stdout-{k}"
        if hit is not None:
            li, col = hit
            results.append(CaseResult(id=cid, status="pass"))
            continue
        shown = text[:200]
        earlier = any((ln.rstrip() == text) if whole else (text in ln) for ln in lines)
        kind = "line" if whole else "text"
        detail = (f"{kind} {shown!r} only found before the previous match: out of order, or "
                  f"listed more times than printed" if earlier else f"missing {kind}: {shown!r}")
        results.append(CaseResult(id=cid, status="fail", detail=detail))
    return results


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# -- loader / unloader generation ---------------------------------------------------

_SECTION_END = re.compile(
    r"\b(?:FD|SD|RD)\s+[A-Za-z0-9-]+|"
    r"\b(?:WORKING-STORAGE|LOCAL-STORAGE|LINKAGE|REPORT|SCREEN|COMMUNICATION)\s+SECTION\b|"
    r"\bPROCEDURE\s+DIVISION\b",
    re.I,
)


def _sentence_end(text: str, start: int) -> int:
    """index just past the period that ends the sentence starting at *start*."""
    quote = ""
    i = start
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "." and (i + 1 == len(text) or text[i + 1].isspace()):
            return i + 1
        i += 1
    return len(text)


def _code_text(source: str, fmt: str | None = None) -> str:
    return "\n".join(ln.text for ln in logical_lines(source, fmt))


def select_and_fd(texts: list[str], file_name: str) -> tuple[str, str] | None:
    """the SELECT sentence and the FD entry (with its records) of *file_name*."""
    name = re.escape(file_name)
    select = fd = ""
    for raw in texts:
        text = _code_text(raw)
        if not select:
            m = re.search(rf"\bSELECT\s+(?:OPTIONAL\s+)?{name}(?![A-Za-z0-9-])", text, re.I)
            if m:
                select = text[m.start():_sentence_end(text, m.start())]
        if not fd:
            m = re.search(rf"(?<![A-Za-z0-9-])(?:FD|SD)\s+{name}(?![A-Za-z0-9-])", text, re.I)
            if m:
                nxt = _SECTION_END.search(text, m.end())
                fd = text[m.start():nxt.start() if nxt else len(text)].strip()
    if not select or not fd:
        return None
    return select, fd


_LEVEL01 = re.compile(r"(?:^|\s)0?1\s+([A-Za-z0-9][A-Za-z0-9-]*)")


def _first_record(fd: str, copybook: Callable[[str], str | None] | None = None) -> str:
    """name of the FD's first record, looking into a COPYd member (with REPLACING)."""
    m = _LEVEL01.search(fd)
    if m and m.group(1).upper() != "FILLER":
        return m.group(1)
    c = re.search(r"\bCOPY\s+['\"]?([A-Za-z0-9_$#@-]+)['\"]?", fd, re.I)
    if not c or copybook is None:
        return ""
    text = copybook(c.group(1).upper())
    m = _LEVEL01.search(_code_text(text)) if text else None
    if not m:
        return ""
    name = m.group(1)
    rest = fd[c.end():]
    operand = r"(==.*?==|[A-Za-z0-9-]+)"
    for old, new in re.findall(operand + r"\s+BY\s+" + operand, rest, re.I | re.S):
        old, new = old.strip("=").strip(), new.strip("=").split()
        if old.upper() == name.upper() and new:
            return new[0].rstrip(".")
    return name


def conversion_program(f: FileUse, texts: list[str], fmt: str, seq_path: str,
                       target: str, load: bool, name: str = "CDLOAD",
                       copybook: Callable[[str], str | None] | None = None) -> str | None:
    """fixed-format program that copies fixed 32000-byte transfer records into *f*'s
    file (load) or *f*'s records into a transfer file (unload), with the SUT's own
    SELECT / FD; binary-safe, so COMP / COMP-3 fields survive."""
    found = select_and_fd(texts, f.name)
    if found is None:
        return None
    select, fd = found
    org = (f.organization or "SEQUENTIAL").upper()
    record = f.fd_record if f.fd_record not in ("", "FILLER") else _first_record(fd, copybook)
    if not record:
        return None
    sel = re.sub(r"\bASSIGN\s+(?:TO\s+)?(?:EXTERNAL\s+|DYNAMIC\s+|USING\s+)?"
                 r"(?:'[^']*'|\"[^\"]*\"|[A-Za-z0-9_$#@.:/-]+)",
                 f"ASSIGN TO '{target}'", select, count=1, flags=re.I)
    sel = re.sub(r"\b(?:FILE\s+)?STATUS\s+(?:IS\s+)?[A-Za-z0-9-]+", "", sel, flags=re.I)
    sel = re.sub(r"\bRELATIVE\s+(?:KEY\s+)?(?:IS\s+)?[A-Za-z0-9-]+", "", sel, flags=re.I)
    access = "SEQUENTIAL" if not load or org != "INDEXED" else "DYNAMIC"
    sel, n = re.subn(r"\bACCESS\s+(?:MODE\s+)?(?:IS\s+)?(?:SEQUENTIAL|RANDOM|DYNAMIC)",
                     f"ACCESS MODE IS {access}", sel, flags=re.I)
    sel = sel.rstrip().rstrip(".")
    if not n:
        sel += f"\n    ACCESS MODE IS {access}"
    sel += "\n    FILE STATUS IS CDL-FS."
    fd = re.sub(r"\bDEPENDING\s+(?:ON\s+)?[A-Za-z0-9-]+", "", fd, flags=re.I)
    fd = re.sub(r"\bVALUE\s+OF\s+FILE-ID\s+(?:IS\s+)?\S+", "", fd, flags=re.I)
    if not fd.rstrip().endswith("."):
        fd = fd.rstrip() + "."
    fname = f.name
    if load:
        open_stmt = f"OPEN INPUT CDL-SEQ\n    OPEN OUTPUT {fname}"
        body = (
            "    READ CDL-SEQ\n"
            "        AT END MOVE 'Y' TO CDL-EOF\n"
            "        NOT AT END\n"
            f"            MOVE CDL-SEQ-REC TO {record}\n"
            f"            WRITE {record}\n"
            "            IF CDL-FS(1:1) NOT = '0'\n"
            "                DISPLAY 'CDL WRITE STATUS ' CDL-FS ' RECORD ' CDL-N\n"
            "            END-IF\n"
            "            ADD 1 TO CDL-N\n"
            "    END-READ\n"
        )
    else:
        open_stmt = f"OPEN INPUT {fname}\n    OPEN OUTPUT CDL-SEQ"
        body = (
            f"    READ {fname}\n"
            "        AT END MOVE 'Y' TO CDL-EOF\n"
            "        NOT AT END\n"
            f"            MOVE {record} TO CDL-SEQ-REC\n"
            "            WRITE CDL-SEQ-REC\n"
            "            ADD 1 TO CDL-N\n"
            f"    END-READ\n"
        )
    src = f"""IDENTIFICATION DIVISION.
PROGRAM-ID. {name}.
ENVIRONMENT DIVISION.
INPUT-OUTPUT SECTION.
FILE-CONTROL.
    SELECT CDL-SEQ ASSIGN TO '{seq_path}'
        ORGANIZATION IS SEQUENTIAL.
    {sel}
DATA DIVISION.
FILE SECTION.
FD CDL-SEQ.
01 CDL-SEQ-REC PIC X({_XFER}).
{fd}
WORKING-STORAGE SECTION.
01 CDL-FS PIC XX.
01 CDL-EOF PIC X VALUE 'N'.
01 CDL-N PIC 9(9) VALUE 0.
PROCEDURE DIVISION.
    {open_stmt}
    IF CDL-FS(1:1) NOT = '0'
        DISPLAY 'CDL OPEN STATUS ' CDL-FS
        MOVE 8 TO RETURN-CODE
        STOP RUN
    END-IF
    PERFORM UNTIL CDL-EOF = 'Y'
{body}    END-PERFORM
    CLOSE CDL-SEQ {fname}
    DISPLAY 'CDL RECORDS ' CDL-N
    MOVE 0 TO RETURN-CODE
    STOP RUN.
"""
    return reflow_fixed(src)



# -- record codec: field maps <-> record bytes, via the computed layout ------------------

_BINARY_USAGE = {"COMP", "COMPUTATIONAL", "COMP-4", "COMPUTATIONAL-4", "BINARY"}
_NATIVE_USAGE = {"COMP-5", "COMPUTATIONAL-5", "COMP-X"}
_PACKED_USAGE = {"COMP-3", "COMPUTATIONAL-3", "PACKED-DECIMAL"}
_OVERPUNCH_NEG = {chr(0x70 + d): d for d in range(10)} | {"}": 0} | \
    {chr(ord("J") + d - 1): d for d in range(1, 10)}
_OVERPUNCH_POS = {"{": 0} | {chr(ord("A") + d - 1): d for d in range(1, 10)}
_REF_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*(?:\(([^)]*)\))?")


def fixture_records(fx: FixtureFile) -> list[str | dict[str, str]]:
    """the records of a fixture: structured ones, else the content lines."""
    if fx.records:
        return list(fx.records)
    if not fx.content:
        return []
    lines = fx.content.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _expand_pic(pic: str) -> str:
    return re.sub(r"(.)\((\d+)\)", lambda m: m.group(1) * int(m.group(2)), pic.upper())


def _kind(f: LayoutField) -> tuple[str, int, int, bool]:
    """(kind, digits, scale, signed); kind: text | display | packed | binary | native."""
    pic = _expand_pic(f.pic)
    usage = f.usage.upper()
    digits = pic.count("9")
    scale = pic.split("V", 1)[1].count("9") if "V" in pic else 0
    signed = pic.startswith("S")
    if f.group:
        return "text", 0, 0, False
    if usage in _PACKED_USAGE:
        return "packed", digits, scale, signed
    if usage in _BINARY_USAGE:
        return "binary", digits, scale, signed
    if usage in _NATIVE_USAGE:
        return "native", digits, scale, signed
    if pic and digits and set(pic) <= set("S9VP"):
        return "display", digits, scale, signed
    return "text", 0, 0, False


def _decimal(value: str) -> Decimal | None:
    v = value.strip().replace(",", "").replace("+", "")
    if v.endswith("-"):
        v = "-" + v[:-1]
    try:
        return Decimal(v) if v else Decimal(0)
    except InvalidOperation:
        return None


def _encode_field(f: LayoutField, value: str) -> bytes | None:
    kind, digits, scale, signed = _kind(f)
    if kind == "text":
        return value.encode("latin-1", "replace")[:f.size].ljust(f.size, b" ")
    num = _decimal(value)
    if num is None:
        return None
    scaled = int((num * (10 ** scale)).to_integral_value())
    neg = scaled < 0 and signed
    mag = abs(scaled) % (10 ** max(digits, 1))
    if kind == "display":
        text = str(mag).rjust(digits, "0")[-digits:] if digits else ""
        if neg and text:
            text = text[:-1] + chr(0x70 + int(text[-1]))
        return text.encode("ascii").ljust(f.size, b"0")[:f.size]
    if kind == "packed":
        nd = f.size * 2 - 1
        nibbles = str(mag).rjust(nd, "0")[-nd:]
        sign = (0xD if neg else 0xC) if signed else 0xF
        vals = [int(c) for c in nibbles] + [sign]
        return bytes(vals[i] * 16 + vals[i + 1] for i in range(0, len(vals), 2))
    val = -mag if neg else mag
    order = "big" if kind == "binary" else "little"
    try:
        return val.to_bytes(f.size, order, signed=signed)
    except OverflowError:
        return (val % (1 << (8 * f.size))).to_bytes(f.size, order)


def _decode_field(f: LayoutField, data: bytes):
    """field value: Decimal for numeric kinds (None when invalid), str for text."""
    kind, digits, scale, signed = _kind(f)
    if kind == "text":
        # trailing NULs: unwritten bytes of a field filled by STRING / reference
        # modification (the same leniency as whole-record comparison)
        return data.decode("latin-1").rstrip(" \0")
    if kind == "display":
        text = data.decode("latin-1")
        neg = False
        if text and text[-1] in _OVERPUNCH_NEG:
            text, neg = text[:-1] + str(_OVERPUNCH_NEG[text[-1]]), True
        elif text and text[-1] in _OVERPUNCH_POS:
            text = text[:-1] + str(_OVERPUNCH_POS[text[-1]])
        if not text.strip().isdigit():
            return None
        mag = int(text.strip())
    elif kind == "packed":
        nib = "".join(f"{b >> 4:x}{b & 15:x}" for b in data)
        if not nib[:-1].isdigit():
            return None
        mag, neg = int(nib[:-1]), nib[-1] in "bd"
    else:
        val = int.from_bytes(data, "big" if kind == "binary" else "little", signed=signed)
        mag, neg = abs(val), val < 0
    return Decimal(-mag if neg else mag).scaleb(-scale)


def _locate(name: str, layouts: list[list[LayoutField]]) -> tuple[LayoutField, int] | None:
    """field and absolute offset for 'NAME' / 'NAME(i)' / 'NAME(i, j)' / 'NAME OF GRP'."""
    m = _REF_RE.match(name)
    if not m:
        return None
    key = m.group(1).upper()
    subs = [int(x) for x in re.findall(r"\d+", m.group(2) or "")]
    for fields in layouts:
        for f in fields:
            if f.name.upper() != key:
                continue
            off = f.offset
            for k, (count, stride) in enumerate(f.dims):
                i = subs[k] if k < len(subs) else 1
                off += (min(max(i, 1), count) - 1) * stride
            return f, off
    return None


def _defaults(fields: list[LayoutField], size: int) -> bytearray:
    buf = bytearray(b" " * size)
    for f in fields:
        if f.group or f.shadow or f.level == 1 and len(fields) > 1:
            continue
        kind = _kind(f)[0]
        if kind == "text":
            continue
        blank = _encode_field(f, "0") or b""
        offsets = [f.offset]
        for count, stride in f.dims:
            offsets = [o + i * stride for o in offsets for i in range(count)]
        for o in offsets:
            if o + len(blank) <= size:
                buf[o:o + len(blank)] = blank
    return buf


def _extent(f: LayoutField) -> int:
    return f.offset + f.size + sum((count - 1) * stride for count, stride in f.dims)


def _best_layout(names: list[str], layouts: list[list[LayoutField]]) -> list[LayoutField]:
    def score(fields: list[LayoutField]) -> int:
        have = {f.name.upper() for f in fields}
        return sum(1 for n in names if (m := _REF_RE.match(n)) and m.group(1).upper() in have)

    return max(layouts, key=score) if layouts else []


def encode_record(rec: str | dict[str, str], layouts: list[list[LayoutField]]
                  ) -> tuple[bytes, list[str]]:
    """record bytes for a fixture record, plus warnings (unknown fields, bad numbers)."""
    if isinstance(rec, str):
        return rec.encode("latin-1", "replace"), []
    if not layouts:
        return _kv_line(rec), ["no record layout known: field map written as text"]
    base = _best_layout(list(rec), layouts)
    size = max(_extent(f) for fl in layouts for f in fl)
    buf = _defaults(base, size)
    warns: list[str] = []
    for name, value in rec.items():
        hit = _locate(name, [base] + [fl for fl in layouts if fl is not base])
        if hit is None:
            warns.append(f"unknown field {name}")
            continue
        f, off = hit
        data = _encode_field(f, str(value))
        if data is None:
            warns.append(f"bad number {value!r} for {name}")
            continue
        buf[off:off + len(data)] = data
    return bytes(buf), warns


def _kv_line(rec: dict[str, str]) -> bytes:
    return " ".join(f"{k}={v}" for k, v in rec.items()).encode("latin-1", "replace")


def compare_records(want: list[str | dict[str, str]], got: list[bytes],
                    layouts: list[list[LayoutField]]) -> str:
    """'' when *got* matches *want*, else a short description of the first difference.

    string records compare as whole lines (trailing spaces ignored); field maps compare
    only the listed fields, decoded through the record layout.
    """
    if len(want) != len(got):
        return f"expected {len(want)} records, got {len(got)}" + (
            f"; first: {got[0][:120].decode('latin-1').rstrip()!r}" if got else "")
    for i, (w, g) in enumerate(zip(want, got), 1):
        if isinstance(w, str):
            # unused tail of a shorter variable-length record may hold NULs
            text = g.decode("latin-1").rstrip(" \0")
            if text != w.rstrip():
                return f"record {i}: expected {w.rstrip()[:160]!r} got {text[:160]!r}"
            continue
        if not layouts:
            return "field-map expectations need a record layout (none found)"
        for name, value in w.items():
            hit = _locate(name, layouts)
            if hit is None:
                return f"record {i}: unknown field {name}"
            f, off = hit
            actual = _decode_field(f, g[off:off + f.size].ljust(f.size, b" "))
            if isinstance(actual, str):
                ok = actual == str(value).rstrip()
            else:
                exp = _decimal(str(value))
                ok = actual is not None and exp is not None and actual == exp
            if not ok:
                return f"record {i} {name}: expected {value!r} got {actual!s}"
    return ""
