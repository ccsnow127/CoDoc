"""testability classification: how (and whether) a program can run outside the mainframe.

Modes, assigned in order:
  - spec:  EXEC CICS / SQL / DLI, screen I/O, or a (transitive) callee that needs one;
           terminal (curses) ACCEPT / DISPLAY in the program itself
  - batch: a JCL step program that receives the PARM through PROCEDURE DIVISION USING
           and that no repo program calls -> a generated wrapper main CALLs it with the
           PARM structure (halfword length + text)
  - unit:  PROCEDURE DIVISION USING -> a fixed-format driver CALLs the program;
           a user-defined FUNCTION -> the driver invokes it as FUNCTION name(...)
  - batch: a main program with native file I/O only -> fixtures + expected outputs

Spec programs carry a spec_kind: "CICS/DB2" (CICS, SQL, DL/I in the program or a linked
callee) or "terminal" (curses ACCEPT/DISPLAY, SCREEN SECTION / BMS without CICS).
"""

from __future__ import annotations

from codocs.cobol.models import CobolProgram
from codocs.cobol.testmodels import Testability

# CALL targets GnuCOBOL provides itself (no stub needed)
_RUNTIME_PREFIXES = ("CBL_", "C$", "CBL-")
_RUNTIME_NAMES = {"SYSTEM", "CBL_OC_DUMP"}


def is_runtime_routine(name: str) -> bool:
    up = name.upper()
    return up in _RUNTIME_NAMES or up.startswith(_RUNTIME_PREFIXES)


_BLOCKERS = ("EXEC CICS", "EXEC SQL (DB2)", "IMS DL/I")


def _blocker_named(reason: str) -> bool:
    return any(b in reason for b in _BLOCKERS)


def _blocker(prog: CobolProgram) -> str:
    """why a program cannot run under GnuCOBOL, or ''."""
    if prog.has_cics:
        return "EXEC CICS"
    if prog.has_sql:
        return "EXEC SQL (DB2)"
    if prog.has_dli:
        return "IMS DL/I"
    return ""


SPEC_MAINFRAME = "CICS/DB2"
SPEC_TERMINAL = "terminal"


def spec_kind(t: Testability | None) -> str:
    """"CICS/DB2" | "terminal" | "" for a spec Testability (derived from the reasons when
    the field is empty, e.g. in older plan files)."""
    if t is None or t.mode != "spec":
        return ""
    if t.spec_kind:
        return t.spec_kind
    if any(_blocker_named(r) for r in t.reasons):
        return SPEC_MAINFRAME
    return SPEC_TERMINAL if t.reasons else ""


def _jcl_parms(pid: str, index) -> list[str]:
    """PARM values JCL steps pass to *pid* (EXEC PGM=pid,PARM=...)."""
    out: list[str] = []
    for job in getattr(index, "jobs", {}).values():
        for step in job.steps:
            if pid in (step.program.upper(), (step.app_program or "").upper()) and step.parm:
                parm = step.parm.strip()
                if len(parm) >= 2 and parm[0] == parm[-1] and parm[0] in "'\"":
                    parm = parm[1:-1]
                if parm not in out:
                    out.append(parm)
    return out


def _parm_entry(program: CobolProgram, index) -> bool:
    """a JCL step program receiving the PARM: one USING item, run by a job, no callers."""
    if getattr(program, "unit_kind", "program") != "program" or len(program.using_params) != 1:
        return False
    jobs = getattr(index, "jobs_running", None)
    if jobs is None or not jobs(program.program_id):
        return False
    return not index.callers(program.program_id)


LINK_KINDS = ["call", "function"]  # edges whose targets are linked into the test


def _call_closure(prog: CobolProgram, index) -> list[str]:
    """repo programs reachable through plain CALLs and user-defined FUNCTION references
    (CICS transfers excluded): everything the test executable links."""
    seen: set[str] = set()
    todo = [prog.program_id]
    while todo:
        cur = todo.pop()
        for c in index.callees(cur, kinds=LINK_KINDS):
            if c not in seen and c != prog.program_id and c in index.programs:
                seen.add(c)
                todo.append(c)
    return sorted(seen)


def _external_callees(progs: list[CobolProgram], index) -> tuple[list[str], list[str]]:
    """(unresolved CALL literal targets, unresolved dynamic data names) of *progs*.

    CALL STATIC targets are left out: those are C routines (e.g. ncurses' curs_set)
    that the runner links to a generated no-op, since the linker needs a symbol."""
    ext: list[str] = []
    dyn: list[str] = []
    for p in progs:
        for c in p.calls:
            if c.kind not in ("call", "call-dynamic") or c.resolved:
                continue
            if index.program_of(c.target) is not None or getattr(c, "static", False):
                continue
            if c.kind == "call-dynamic":
                if c.target not in dyn:
                    dyn.append(c.target)
                continue
            if is_runtime_routine(c.target) or c.target in ext:
                continue
            ext.append(c.target)
    return ext, dyn


def _screen_ui(program: CobolProgram) -> bool:
    """screen I/O that blocks a caller too: a SCREEN SECTION or BMS maps. The parser also
    sets uses_screen_io for positioned / colored ACCEPT / DISPLAY, which always comes with
    uses_terminal_io; like other terminal I/O that only matters on the paths that reach it,
    so it does not make the callers spec."""
    if not program.uses_screen_io:
        return False
    return bool(program.maps) or not getattr(program, "uses_terminal_io", False)


def classify(program: CobolProgram, index) -> Testability:
    """assign unit / batch / spec plus the programs to build and the stubs needed."""
    t = Testability(program_id=program.program_id, mode="spec")
    block = _blocker(program)
    if block:
        t.reasons.append(f"uses {block}")
    if getattr(program, "uses_terminal_io", False):
        # callees are not checked for this: it only matters on the paths that reach it
        t.reasons.append("uses terminal I/O (ACCEPT/DISPLAY AT, NO-ECHO, TIME-OUT, colors "
                         "or screen routines): needs a curses terminal")
    if program.uses_screen_io:
        t.reasons.append("uses screen I/O (SCREEN SECTION / BMS maps)")

    callees = _call_closure(program, index)
    for c in callees:
        cb = _blocker(index.programs[c])
        if cb:
            t.reasons.append(f"calls {c}, which uses {cb}")
        elif _screen_ui(index.programs[c]):
            t.reasons.append(f"calls {c}, which uses screen I/O")
    t.callees = callees
    ext, dyn = _external_callees([program] + [index.programs[c] for c in callees], index)
    t.external_callees = ext
    if t.reasons:
        mainframe = any(_blocker_named(r) for r in t.reasons)
        t.spec_kind = SPEC_MAINFRAME if mainframe else SPEC_TERMINAL
        return t

    if ext:
        t.reasons.append("needs stubs for " + ", ".join(ext))
    if dyn:
        t.reasons.append("dynamic CALL through unresolved data names: " + ", ".join(dyn))
    native = [f for f in program.files if f.via == "native"]
    if getattr(program, "unit_kind", "program") == "function":
        t.mode = "unit"
        t.reasons.insert(0, "user-defined FUNCTION (FUNCTION-ID)" + (
            ": USING " + " ".join(program.using_params) if program.using_params else ""))
    elif _parm_entry(program, index):
        t.mode = "batch"
        t.parm_param = program.using_params[0]
        t.jcl_parms = _jcl_parms(program.program_id, index)
        t.reasons.insert(0, f"JCL step program receiving the PARM via PROCEDURE DIVISION USING "
                            f"{t.parm_param} (no callers): run through a generated PARM wrapper")
        if native:
            t.reasons.append("opens files: " + ", ".join(f.name for f in native))
    elif program.using_params:
        t.mode = "unit"
        t.reasons.insert(0, "PROCEDURE DIVISION USING " + " ".join(program.using_params))
        if native:
            t.reasons.append("opens files: " + ", ".join(f.name for f in native))
    elif program.is_batch_main or native or not index.callers(program.program_id):
        t.mode = "batch"
        t.reasons.insert(0, "main program" + (" with native file I/O" if native else ""))
        if any(f.organization == "INDEXED" for f in native):
            t.reasons.append("INDEXED inputs are loaded by a generated loader")
    else:
        # a parameterless subprogram that other programs CALL
        t.mode = "unit"
        t.reasons.insert(0, "subprogram without parameters")
    return t
