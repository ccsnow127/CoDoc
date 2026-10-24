"""COBOL repository index: discovery, copybook resolution, the repo graph and queries.

`CobolRepoIndex.build(root)` walks a repository, classifies files (programs, copybooks,
JCL, PROCs, BMS, CICS CSD), parses them, resolves copybooks / calls / transactions, links
programs to data stores (JCL DSNs, CICS files, DB2 tables, TS/TD queues) and builds a
networkx graph with nodes prog:X copy:Y job:Z data:W txn:T map:M.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections import Counter
from pathlib import Path, PurePosixPath

import networkx as nx

from codocs.cobol.bms import is_bms_text, parse_bms
from codocs.cobol.jcl import define_statements, is_jcl_text, parse_jcl, vsam_definitions
from codocs.cobol.models import (
    BmsMapset,
    CobolProgram,
    Copybook,
    DataStore,
    JclJob,
    RepoSummary,
    SourceFile,
)
from codocs.cobol.parser import (
    copy_members,
    has_procedure_code,
    is_copybook_text,
    parse_program_file,
    sql_declared_tables,
    top_level_items,
)
from codocs.cobol.source import decode_bytes, detect_format

PROGRAM_EXTS = {
    ".cbl", ".cob", ".cobol", ".ccp", ".pco", ".sqb", ".cblle", ".sqlcblle", ".scbl",
    ".cicscbl", ".cbx",
}
COPYBOOK_EXTS = {".cpy", ".copy", ".cpb", ".cblcpy", ".dcl", ".dclgen", ".cpybk", ".copybook"}
JCL_EXTS = {".jcl", ".prc", ".proc", ".job"}
BMS_EXTS = {".bms"}
CSD_EXTS = {".csd"}
# never sniffed: documentation, data, build and binary formats
NO_SNIFF_EXTS = {
    ".md", ".rst", ".adoc", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".py", ".sh", ".bat", ".cmd", ".ps1", ".c", ".h", ".cpp", ".hpp", ".java", ".go",
    ".rs", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".tar", ".jar", ".class", ".o", ".so", ".dylib", ".dll", ".exe", ".a", ".csv", ".tsv",
    ".toml", ".cfg", ".ini", ".properties", ".lock", ".gitignore", ".gitattributes", ".ps",
    ".dat", ".rexx", ".asm", ".mac", ".j2", ".log", ".bin", ".xls", ".xlsx", ".doc", ".docx",
    ".ppt", ".pptx", ".dockerignore", ".env", ".sql", ".ddl", ".ctl",
}
SKIP_DIRS = {
    "node_modules", "build", "dist", "target", "out", "__pycache__", "venv", "obj", "bin",
}
SYSTEM_PROGRAMS_RE = re.compile(
    r"^(CBL_|C\$|CEE|ILBO|IGZ|DSNTIAR$|DSNTIAC$|DSNHLI|MQ[A-Z]|CBLTDLI$|AIBTDLI$|CEETDLI$|"
    r"PLITDLI$|SYSTEM$|COBDATFT$|DFH|EZA|BPX1|IEFBR14$|CSQ)"
)
PROGRAM_EDGE_KINDS = ("call", "cics-link", "cics-xctl", "cics-start", "function")
_GENERIC_DIRS = {
    "cbl", "cob", "cobol", "src", "source", "sources", "program", "programs", "prog", "progs",
    "pgm", "pgms", "app", "base", "main", "code", "copybooks", "copybook", "cpy", "copy",
    "copylib", "lib",
}
_SYSTEM_DD = {
    "STEPLIB", "JOBLIB", "SYSPRINT", "SYSOUT", "SYSUDUMP", "SYSABEND", "SYSMDUMP", "SYSDBOUT",
    "CEEDUMP", "SYSIN", "SYSTSIN", "SYSTSPRT", "SYSLIB", "SYSLMOD", "SYSLIN", "SYSPUNCH",
    "SYSTERM", "SYMNAMES", "DFSRESLB", "IMS", "IEFRDER", "SYSOUD", "CEEOPTS", "SYSEXEC",
    "SYSPROC", "SYSUT3", "SYSUT4", "SYSUT5", "SYSUT6", "SYSUT7",
}
_READ_MODES = {"INPUT", "I-O", "READ", "READNEXT", "READPREV", "STARTBR", "RESETBR"}
_WRITE_MODES = {"OUTPUT", "I-O", "EXTEND", "WRITE", "REWRITE", "DELETE"}
_SQL_READ = {"SELECT", "DECLARE-CURSOR", "OPEN", "FETCH"}
_SQL_WRITE = {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}
_MAX_SNIFF_BYTES = 2_000_000
_PROGRAM_ID_RE = re.compile(r"^.{0,12}\b(?:PROGRAM|FUNCTION)-ID\b", re.IGNORECASE | re.MULTILINE)
_CSD_RE = re.compile(r"^\s*DEFINE\s+(TRANSACTION|PROGRAM|FILE|MAPSET)\s*\(", re.I | re.M)


def _ext(path: Path) -> str:
    return path.suffix.lower()


def _dd_of(assign: str) -> str:
    """ASSIGN target -> DD name (UT-S-INFILE -> INFILE); '' for paths / devices."""
    a = assign.strip().upper()
    if not a or "/" in a or "\\" in a or "." in a:
        return ""
    if "-" in a:
        a = a.rsplit("-", 1)[-1]
    if a in ("KEYBOARD", "DISPLAY", "PRINTER", "DISK"):
        return ""
    return a


def _dsn_key(dsn: str) -> str:
    """dataset name used as a data-store key: GDG relative generation stripped."""
    d = dsn.strip().strip("'").upper()
    return re.sub(r"\(\s*[+-]?\d+\s*\)$", "", d)


def _is_data_dsn(dsn: str) -> bool:
    d = dsn.strip()
    if not d or d in ("*", "DUMMY", "SYSOUT", "NULLFILE") or d.startswith("&&"):
        return False
    return not d.startswith("/")


def _pagerank(g: nx.DiGraph, alpha: float = 0.85, iters: int = 100) -> dict[str, float]:
    """plain power-iteration PageRank (no scipy dependency)."""
    nodes = list(g.nodes)
    n = len(nodes)
    if not n:
        return {}
    pr = dict.fromkeys(nodes, 1.0 / n)
    out_deg = {v: g.out_degree(v) for v in nodes}
    for _ in range(iters):
        dangling = sum(pr[v] for v in nodes if out_deg[v] == 0)
        base = (1 - alpha) / n + alpha * dangling / n
        new = dict.fromkeys(nodes, base)
        for v in nodes:
            if out_deg[v]:
                share = alpha * pr[v] / out_deg[v]
                for w in g.successors(v):
                    new[w] += share
        err = sum(abs(new[v] - pr[v]) for v in nodes)
        pr = new
        if err < 1e-10 * n:
            break
    return pr


class CobolRepoIndex:
    """everything static analysis knows about one COBOL repository."""

    def __init__(self, root: Path, name: str):
        self.root = root
        self.name = name
        self.files: list[SourceFile] = []
        self.programs: dict[str, CobolProgram] = {}
        self.copybooks: dict[str, Copybook] = {}
        self.jobs: dict[str, JclJob] = {}
        self.mapsets: dict[str, BmsMapset] = {}
        self.datastores: dict[str, DataStore] = {}
        self.graph = nx.DiGraph()
        self.transactions: dict[str, str] = {}  # transid -> program id
        self.csd_transactions: set[str] = set()  # transids defined in CSD / RDO
        self.csd_files: dict[str, str] = {}  # CICS file -> DSN
        self.csd_file_attrs: dict[str, dict[str, str]] = {}  # CICS file -> DEFINE FILE attrs
        self.vsam_orgs: dict[str, str] = {}  # DSN -> organization from IDCAMS DEFINE
        self.warnings: list[str] = []
        self._texts: dict[str, str] = {}  # rel path -> text
        self._copy_files: dict[str, list[str]] = {}  # member -> candidate rel paths
        self._copy_used: dict[str, dict[str, str]] = {}  # pid -> member -> rel path
        self._missing: dict[str, set[str]] = {}  # member -> referencing programs
        self._groups: dict[str, str] = {}
        self._file_programs: dict[str, list[str]] = {}  # rel path -> program ids
        self._enc: dict[str, str] = {}  # rel path -> encoding
        self.entry_owner: dict[str, str] = {}  # ENTRY name -> program id

    # -- construction -----------------------------------------------------

    @classmethod
    def build(
        cls,
        root: str | Path,
        subtree: str | None = None,
        exclude: list[str] | None = None,
        *,
        skip: tuple[str | Path, ...] | list[str | Path] = (),
    ) -> CobolRepoIndex:
        """scan *root* (optionally only *subtree*), skipping paths matching *exclude* globs
        and the directories in *skip* (e.g. the run's output directory)."""
        root = Path(root).resolve()
        index = cls(root, root.name)
        found = index._discover(root / subtree if subtree else root, exclude or [],
                                skip=skip)
        index._load(found)
        index._resolve()
        index._build_graph()
        index._compute_groups()
        return index

    def _discover(self, scan_root: Path, exclude: list[str],
                  skip: tuple[str | Path, ...] | list[str | Path] = ()
                  ) -> list[tuple[str, str]]:
        """(rel path, kind) for every COBOL-relevant file."""
        out: list[tuple[str, str]] = []
        pruned = {Path(p).resolve() for p in skip}
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS
                and not (pruned and (Path(dirpath) / d).resolve() in pruned)
            )
            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                path = Path(dirpath) / fname
                rel = path.relative_to(self.root).as_posix()
                if any(fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("/") + "/")
                       for pat in exclude):
                    continue
                kind = self._classify(path, rel)
                if kind:
                    out.append((rel, kind))
        return out

    def _read(self, path: Path, rel: str) -> str | None:
        if rel in self._texts:
            return self._texts[rel]
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:8192]:
            return None
        text, enc = decode_bytes(data)
        self._texts[rel] = text
        self._enc[rel] = enc
        return text

    def _classify(self, path: Path, rel: str) -> str:
        ext = _ext(path)
        if ext in PROGRAM_EXTS:
            text = self._read(path, rel)
            if text is None:
                return ""
            if _PROGRAM_ID_RE.search(text):
                return "program"
            return "copybook" if is_copybook_text(text) else ""
        if ext in COPYBOOK_EXTS:
            return "copybook" if self._read(path, rel) is not None else ""
        if ext in JCL_EXTS:
            return "jcl" if self._read(path, rel) is not None else ""
        if ext in BMS_EXTS:
            return "bms" if self._read(path, rel) is not None else ""
        if ext in CSD_EXTS:
            return "csd" if self._read(path, rel) is not None else ""
        if ext in NO_SNIFF_EXTS:
            return ""
        try:
            if path.stat().st_size > _MAX_SNIFF_BYTES:
                return ""
        except OSError:
            return ""
        text = self._read(path, rel)
        if text is None:
            return ""
        kind = ""
        if is_jcl_text(text):
            kind = "jcl"
        elif is_bms_text(text):
            kind = "bms"
        elif _CSD_RE.search(text[:20000]):
            kind = "csd"
        elif _PROGRAM_ID_RE.search(text) and re.search(r"\bPROCEDURE\s+DIVISION\b", text, re.I):
            kind = "program"
        elif ext != ".txt" and is_copybook_text(text) and _strong_copybook(text):
            kind = "copybook"
        if not kind:
            self._texts.pop(rel, None)
        return kind

    def _load(self, found: list[tuple[str, str]]) -> None:
        enc = self._enc
        jcl_files: list[str] = []
        program_files: list[str] = []
        for rel, kind in found:
            text = self._texts[rel]
            loc = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
            fmt = detect_format(text) if kind in ("program", "copybook") else "fixed"
            sf = SourceFile(path=rel, kind=kind if kind != "csd" else "other", format=fmt,
                            encoding=enc.get(rel, "utf-8"), loc=loc)
            self.files.append(sf)
            if kind == "copybook":
                member = PurePosixPath(rel).stem.upper()
                self._copy_files.setdefault(member, []).append(rel)
            elif kind == "program":
                program_files.append(rel)
            elif kind == "jcl":
                jcl_files.append(rel)
            elif kind == "bms":
                self._load_bms(rel, text)
            elif kind == "csd":
                self._load_defines(text)

        self._load_jcl(jcl_files)
        self._load_copybooks()
        for rel in program_files:
            self._load_program(rel)

    def _load_bms(self, rel: str, text: str) -> None:
        try:
            ms = parse_bms(rel, text)
        except Exception as e:  # keep going on malformed input
            self.warnings.append(f"{rel}: BMS parse failed: {e}")
            return
        if ms.name in self.mapsets:
            self.warnings.append(f"{rel}: duplicate mapset {ms.name}")
            return
        self.mapsets[ms.name] = ms

    def _load_defines(self, text: str) -> None:
        for kind, attrs in define_statements(text):
            if kind == "TRANSACTION" and attrs.get("TRANSACTION") and attrs.get("PROGRAM"):
                t = attrs["TRANSACTION"]
                self.transactions.setdefault(t, attrs["PROGRAM"])
                self.csd_transactions.add(t)
            elif kind == "FILE" and attrs.get("FILE") and attrs.get("DSNAME"):
                self.csd_files.setdefault(attrs["FILE"], attrs["DSNAME"])
                self.csd_file_attrs.setdefault(attrs["FILE"], attrs)

    def _load_jcl(self, jcl_files: list[str]) -> None:
        parsed: dict[str, JclJob] = {}
        for rel in jcl_files:
            text = self._texts[rel]
            try:
                parsed[rel] = parse_jcl(rel, text)
            except Exception as e:
                self.warnings.append(f"{rel}: JCL parse failed: {e}")
                continue
            if re.search(r"DEFINE\s+(?:TRANSACTION|FILE)\s*\(", text, re.IGNORECASE):
                self._load_defines(text)
            for dsn, org in vsam_definitions(text):
                self.vsam_orgs.setdefault(_dsn_key(dsn), org)
            if parsed[rel].kind == "proc":
                for f in self.files:
                    if f.path == rel:
                        f.kind = "proc"
        procs = {j.name: j for j in parsed.values() if j.kind == "proc"}
        for rel, job in parsed.items():
            if job.kind == "job" and any(s.proc in procs for s in job.steps if s.proc):
                job = parse_jcl(rel, self._texts[rel], procs)
            stem = PurePosixPath(rel).stem.upper()
            key = job.name
            if key in self.jobs:
                key = f"{stem}-PROC" if job.kind == "proc" else stem
                n = 2
                while key in self.jobs:
                    key = f"{stem}_{n}"
                    n += 1
                self.warnings.append(f"{rel}: JCL name {job.name} already used, keyed as {key}")
                job.name = key
            self.jobs[key] = job

    def _load_copybooks(self) -> None:
        for member, paths in sorted(self._copy_files.items()):
            paths.sort(key=lambda p: (p.count("/"), p))
            rel = paths[0]
            text = self._texts[rel]
            cb = Copybook(name=member, path=rel, loc=text.count("\n") + 1)
            cb.kind = self._copybook_kind(member, rel, text)
            try:
                cb.top_items = top_level_items(text)
            except Exception as e:
                self.warnings.append(f"{rel}: copybook parse failed: {e}")
            self.copybooks[member] = cb

    def _copybook_kind(self, member: str, rel: str, text: str) -> str:
        if sql_declared_tables(text):
            return "sql-include"
        parts = {p.lower() for p in PurePosixPath(rel).parent.parts}
        if member in self.mapsets or any("bms" in p for p in parts):
            return "bms-symbolic"
        if not top_level_items(text) and has_procedure_code(text):
            return "procedure"
        return "data"

    def resolve_copybook(self, member: str, from_rel: str) -> str | None:
        """rel path of the copybook a COPY in *from_rel* gets: same dir, then nearest dir."""
        member = member.upper()
        cands = self._copy_files.get(member)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        src = PurePosixPath(from_rel).parent.parts

        def score(c: str) -> tuple[int, int, str]:
            parts = PurePosixPath(c).parent.parts
            common = 0
            for a, b in zip(parts, src):
                if a != b:
                    break
                common += 1
            same = 1 if parts == src else 0
            return (-same, -common, c)

        return min(cands, key=score)

    def _load_program(self, rel: str) -> None:
        text = self._texts[rel]
        used: dict[str, str] = {}

        def lookup(member: str) -> str | None:
            path = self.resolve_copybook(member, rel)
            if path is None:
                return None
            used[member] = path
            return self._texts.get(path)

        try:
            progs = parse_program_file(rel, text, lookup)
        except Exception as e:
            self.warnings.append(f"{rel}: parse failed: {type(e).__name__}: {e}")
            return
        if not progs:
            self.warnings.append(f"{rel}: no PROGRAM-ID found in code")
        for prog in progs:
            pid = prog.program_id
            if pid in self.programs:
                keep = self.programs[pid]
                if _prefer(prog.path, keep.path):
                    self.warnings.append(
                        f"duplicate PROGRAM-ID {pid}: {prog.path} preferred over {keep.path}"
                    )
                    self._file_programs[keep.path].remove(pid)
                else:
                    self.warnings.append(
                        f"duplicate PROGRAM-ID {pid}: {prog.path} ignored (kept {keep.path})"
                    )
                    continue
            self.programs[pid] = prog
            self._file_programs.setdefault(rel, []).append(pid)
            resolved = {}
            for member in prog.copybooks:
                path = used.get(member) or self.resolve_copybook(member, rel)
                if path:
                    resolved[member] = path
            self._copy_used[pid] = resolved

    # -- resolution -------------------------------------------------------

    def _resolve(self) -> None:
        # copybooks: users, nested includes, missing members
        for pid, prog in self.programs.items():
            for member in prog.copybooks:
                cb = self.copybooks.get(member)
                if cb is not None:
                    if pid not in cb.used_by:
                        cb.used_by.append(pid)
                    if any(s.op == "INCLUDE" and s.member == member for s in prog.sql) and \
                            cb.kind == "data":
                        cb.kind = "sql-include"
                elif not _system_copybook(member) and member not in self.mapsets:
                    self._missing.setdefault(member, set()).add(pid)  # BMS ones are generated
        for member, cb in self.copybooks.items():
            for nested in copy_members(self._texts.get(cb.path, "")):
                if nested not in self.copybooks and not _system_copybook(nested):
                    self._missing.setdefault(nested, set()).add(f"copy:{member}")

        # transactions: CSD first, then RETURN TRANSID heuristics
        returns: dict[str, set[str]] = {}
        for pid, prog in self.programs.items():
            for use in prog.cics:
                if use.command == "RETURN":
                    for t in use.resolved.get("TRANSID", []):
                        returns.setdefault(t, set()).add(pid)
        for t, pids in returns.items():
            if t not in self.transactions and len(pids) == 1:
                self.transactions[t] = next(iter(pids))

        # call sites (a CALL may name a program or an ENTRY of one)
        for pid, prog in self.programs.items():
            for e in prog.entry_points:
                if e not in self.programs:
                    self.entry_owner.setdefault(e, pid)
        for prog in self.programs.values():
            for c in prog.calls:
                if c.kind == "cics-start":
                    c.resolved = self.transactions.get(c.target) in self.programs
                elif self.program_of(c.target):
                    c.resolved = True
                    if c.target not in self.programs:  # an ENTRY of another program
                        c.owner = self.entry_owner[c.target]

        self._link_datastores()

    def _store(self, key: str, kind: str) -> DataStore:
        ds = self.datastores.get(key)
        if ds is None:
            ds = DataStore(name=key, kind=kind)
            self.datastores[key] = ds
        return ds

    @staticmethod
    def _touch(ds: DataStore, pid: str, read: bool, write: bool) -> None:
        if read and pid not in ds.readers:
            ds.readers.append(pid)
        if write and pid not in ds.writers:
            ds.writers.append(pid)

    def _link_datastores(self) -> None:
        for pid, prog in self.programs.items():
            self._link_online(pid, prog)
        linked = self._link_jcl()
        self._link_unallocated(linked)
        self._store_organizations()

    def _store_organizations(self) -> None:
        """VSAM organization of dataset / CICS file stores that no SELECT told: IDCAMS
        DEFINE CLUSTER, a CSD DEFINE FILE with a KEYLENGTH, or a KSDS qualifier in the DSN."""
        keyed = {
            _dsn_key(self.csd_files[f]) for f, a in self.csd_file_attrs.items()
            if a.get("KEYLENGTH", "0").strip("0 ")
        }
        for name, ds in self.datastores.items():
            if ds.kind not in ("dataset", "cics-file") or ds.organization:
                continue
            org = self.vsam_orgs.get(name, "")
            if not org and (name in keyed or re.search(r"(?:^|\.)KSDS(?:\.|$)", name)):
                org = "INDEXED"
            ds.organization = org

    def _link_unallocated(self, linked: set[tuple[str, str]]) -> None:
        for pid, prog in self.programs.items():
            # native files no JCL step covers: keyed by their ASSIGN target
            for fu in prog.files:
                if fu.via != "native" or (pid, fu.name) in linked or not fu.assign:
                    continue
                key = fu.assign.strip()
                if key.upper() in ("KEYBOARD", "DISPLAY", "PRINTER"):
                    continue
                modes = set(fu.modes)
                ds = self._store(key, "dataset")
                if fu.name not in ds.aliases:
                    ds.aliases.append(fu.name)
                ds.organization = ds.organization or fu.organization
                self._touch(ds, pid, bool(modes & _READ_MODES) or not modes,
                            bool(modes & _WRITE_MODES))

    def _link_online(self, pid: str, prog: CobolProgram) -> None:
        """CICS files (merged with their CSD DSNAME), DB2 tables, TS/TD queues."""
        for fu in prog.files:
            if fu.via != "cics":
                continue
            modes = set(fu.modes)
            dsn = self.csd_files.get(fu.name)
            ds = self._store(_dsn_key(dsn) if dsn else fu.name, "cics-file")
            ds.kind = "cics-file"  # a file CICS owns, even when batch jobs also allocate it
            if fu.name not in ds.aliases:
                ds.aliases.append(fu.name)
            self._touch(ds, pid, bool(modes & _READ_MODES) or not modes,
                        bool(modes & _WRITE_MODES))
        for s in prog.sql:
            if s.op not in _SQL_READ and s.op not in _SQL_WRITE:
                continue
            for t in s.tables:
                name = t.rsplit(".", 1)[-1]
                if t.startswith("SYSIBM.") or not name:
                    continue
                ds = self._store(name, "db2-table")
                self._touch(ds, pid, s.op in _SQL_READ, s.op in _SQL_WRITE)
        for use in prog.cics:
            verb = use.command.split()[0]
            if verb not in ("WRITEQ", "READQ", "DELETEQ"):
                continue
            for q in use.resolved.get("QUEUE", []) + use.resolved.get("QNAME", []):
                ds = self._store(q, "queue")
                self._touch(ds, pid, verb == "READQ", verb != "READQ")

    def _link_jcl(self) -> set[tuple[str, str]]:
        """datasets of JCL steps. Steps running a repo program create stores and link the
        program's SELECTs by DD name; utility steps only add jobs to stores already known."""
        linked: set[tuple[str, str]] = set()  # (pid, SELECT name) covered by a JCL DD
        later: list[tuple[str, str, str]] = []  # (job, dd, dsn) of non-application steps
        for jname, job in self.jobs.items():
            if job.kind != "job":
                continue
            for step in job.steps:
                prog = self.programs.get(step.app_program) if step.app_program else None
                # the step's DDs also serve the subprograms its program CALLs (same task)
                progs = [prog] + [self.programs[c] for c in self._call_closure(prog.program_id)] \
                    if prog is not None else []
                for ddname, value in step.dd.items():
                    if ddname in _SYSTEM_DD or "." in ddname:
                        continue
                    for dsn in value.split(","):
                        if not _is_data_dsn(dsn):
                            continue
                        if prog is None:
                            later.append((jname, ddname, dsn))
                            continue
                        ds = self._store(_dsn_key(dsn), "dataset")
                        _add(ds.jobs, jname)
                        _add(ds.aliases, ddname)
                        for p in progs:
                            for fu in p.files:
                                if fu.via == "native" and _dd_of(fu.assign) == ddname:
                                    modes = set(fu.modes)
                                    self._touch(ds, p.program_id,
                                                bool(modes & _READ_MODES) or not modes,
                                                bool(modes & _WRITE_MODES))
                                    ds.organization = ds.organization or fu.organization
                                    linked.add((p.program_id, fu.name))
        for jname, ddname, dsn in later:
            ds = self.datastores.get(_dsn_key(dsn))
            if ds is not None and ds.kind in ("dataset", "cics-file"):
                _add(ds.jobs, jname)
        return linked

    def _call_closure(self, pid: str) -> list[str]:
        """repo programs *pid* reaches through CALL (static or resolved dynamic), not CICS."""
        seen: list[str] = []
        todo = [pid]
        while todo:
            cur = self.programs.get(todo.pop())
            if cur is None:
                continue
            for c in cur.calls:
                if c.kind not in ("call", "call-dynamic") or not c.resolved:
                    continue
                target = self.program_of(c.target)
                if target and target != pid and target not in seen:
                    seen.append(target)
                    todo.append(target)
        return seen

    # -- graph ------------------------------------------------------------

    def _build_graph(self) -> None:
        g = self.graph
        for pid, prog in self.programs.items():
            g.add_node(f"prog:{pid}", kind="program", path=prog.path)
        for name, cb in self.copybooks.items():
            g.add_node(f"copy:{name}", kind="copybook", path=cb.path, copy_kind=cb.kind)
        for name, job in self.jobs.items():
            g.add_node(f"job:{name}", kind="job", path=job.path, jcl_kind=job.kind)
        for name, ds in self.datastores.items():
            g.add_node(f"data:{name}", kind="datastore", store_kind=ds.kind)
        for name, ms in self.mapsets.items():
            g.add_node(f"map:{name}", kind="map", path=ms.path)

        def edge(u: str, v: str, kind: str, **attrs) -> None:
            if g.has_edge(u, v):
                d = g.edges[u, v]
                d["count"] = d.get("count", 1) + 1
                if kind not in d["kinds"]:
                    d["kinds"].append(kind)
                return
            g.add_edge(u, v, kind=kind, kinds=[kind], count=1, **attrs)

        for pid, prog in self.programs.items():
            src = f"prog:{pid}"
            for c in prog.calls:
                if c.kind == "cics-start":
                    target = self.transactions.get(c.target)
                    if target in self.programs and target != pid:
                        edge(src, f"prog:{target}", "cics-start", transid=c.target)
                    continue
                target = self.program_of(c.target)
                if target and target != pid:
                    kind = "call" if c.kind in ("call", "call-dynamic") else c.kind
                    edge(src, f"prog:{target}", kind, dynamic=c.kind == "call-dynamic")
            for member in prog.copybooks:
                if member in self.copybooks:
                    edge(src, f"copy:{member}", "copy")
            for m in prog.maps:
                if f"map:{m}" not in g:
                    g.add_node(f"map:{m}", kind="map", path="")
                edge(src, f"map:{m}", "sends-map")
        for name, job in self.jobs.items():
            for step in job.steps:
                if step.app_program in self.programs:
                    edge(f"job:{name}", f"prog:{step.app_program}", "runs", step=step.name)
        for name, ds in self.datastores.items():
            for pid in ds.readers:
                edge(f"prog:{pid}", f"data:{name}", "reads")
            for pid in ds.writers:
                edge(f"prog:{pid}", f"data:{name}", "writes")
        for t, pid in sorted(self.transactions.items()):
            if pid in self.programs:
                g.add_node(f"txn:{t}", kind="transaction", program=pid,
                           csd=t in self.csd_transactions)
                edge(f"txn:{t}", f"prog:{pid}", "starts")

    def _compute_groups(self) -> None:
        if not self.programs:
            return
        dirs = {pid: PurePosixPath(p.path).parent.parts for pid, p in self.programs.items()}
        common = list(next(iter(dirs.values())))
        for parts in dirs.values():
            k = 0
            while k < len(common) and k < len(parts) and common[k] == parts[k]:
                k += 1
            common = common[:k]
        rel = {
            pid: [p for p in parts[len(common):] if p.lower() not in _GENERIC_DIRS]
            for pid, parts in dirs.items()
        }
        groups = {pid: (r[0] if r else "(root)") for pid, r in rel.items()}
        counts = Counter(groups.values())
        total = len(groups)
        for g, cnt in counts.items():
            if g == "(root)" or cnt <= 0.6 * total:
                continue
            subs = {tuple(rel[pid][:2]) for pid in groups if groups[pid] == g}
            if len({s[1] for s in subs if len(s) > 1}) >= 2:
                for pid in groups:
                    if groups[pid] == g and len(rel[pid]) > 1:
                        groups[pid] = f"{g}/{rel[pid][1]}"
        self._groups = groups

    # -- queries ----------------------------------------------------------

    def program_of(self, name: str) -> str | None:
        """program id a CALL / LINK / XCTL target names (a PROGRAM-ID or an ENTRY)."""
        if name in self.programs:
            return name
        return self.entry_owner.get(name)

    def program_source(self, pid: str) -> str:
        prog = self.programs[pid]
        if prog.path not in self._texts:
            self._texts[prog.path] = decode_bytes((self.root / prog.path).read_bytes())[0]
        return self._texts[prog.path]

    def copybook_source(self, name: str, pid: str | None = None) -> str | None:
        """copybook text; with *pid*, the file that program's COPY actually resolves to."""
        name = name.upper()
        path = None
        if pid is not None:
            path = self._copy_used.get(pid, {}).get(name)
        if path is None:
            cb = self.copybooks.get(name)
            path = cb.path if cb else None
        if path is None:
            return None
        if path not in self._texts:
            self._texts[path] = decode_bytes((self.root / path).read_bytes())[0]
        return self._texts[path]

    def copybook_dirs(self) -> list[Path]:
        """absolute dirs of resolved copybooks (cobc -I), most used first."""
        uses: Counter[str] = Counter()
        for resolved in self._copy_used.values():
            for path in resolved.values():
                uses[str(PurePosixPath(path).parent)] += 1
        for cb in self.copybooks.values():
            uses.setdefault(str(PurePosixPath(cb.path).parent), 0)
        ordered = sorted(uses, key=lambda d: (-uses[d], d))
        return [(self.root / d).resolve() for d in ordered]

    def _program_succ(self, pid: str, kinds: tuple[str, ...] | list[str] | None = None):
        node = f"prog:{pid}"
        if node not in self.graph:
            return []
        wanted = set(kinds or PROGRAM_EDGE_KINDS)
        out = []
        for v in self.graph.successors(node):
            d = self.graph.edges[node, v]
            if v.startswith("prog:") and wanted & set(d.get("kinds", [d["kind"]])):
                out.append(v[5:])
        return sorted(out)

    def callees(self, pid: str, kinds: list[str] | None = None) -> list[str]:
        """repo programs *pid* transfers control to (CALL / LINK / XCTL / START)."""
        return self._program_succ(pid, kinds)

    def callers(self, pid: str) -> list[str]:
        node = f"prog:{pid}"
        if node not in self.graph:
            return []
        return sorted(
            u[5:] for u in self.graph.predecessors(node)
            if u.startswith("prog:") and self.graph.edges[u, node]["kind"] in PROGRAM_EDGE_KINDS
        )

    def transitive_callees(self, pid: str) -> list[str]:
        seen: set[str] = set()
        todo = [pid]
        while todo:
            cur = todo.pop()
            for c in self.callees(cur):
                if c not in seen and c != pid:
                    seen.add(c)
                    todo.append(c)
        return sorted(seen)

    def jobs_running(self, pid: str) -> list[str]:
        node = f"prog:{pid}"
        if node not in self.graph:
            return []
        return sorted(
            u[4:] for u in self.graph.predecessors(node)
            if u.startswith("job:") and self.graph.edges[u, node]["kind"] == "runs"
        )

    def transactions_of(self, pid: str) -> list[str]:
        """CICS transids that start *pid* (CSD / RDO definitions, else RETURN TRANSID)."""
        return sorted(t for t, p in self.transactions.items() if p == pid)

    def entrypoints(self) -> dict[str, list[str]]:
        jcl = sorted(p for p in self.programs if self.jobs_running(p))
        # CICS programs: started by a transaction, or never transferred to by repo code
        cics = sorted(
            p for p, prog in self.programs.items()
            if self.transactions_of(p) or (prog.has_cics and not self.callers(p))
        )
        taken = set(jcl) | set(cics)
        standalone = sorted(
            p for p, prog in self.programs.items()
            if p not in taken and prog.is_batch_main and not self.callers(p)
        )
        return {"jcl": jcl, "cics": cics, "standalone": standalone}

    def program_graph(self) -> nx.DiGraph:
        """program-only view: nodes are program ids (no prefix), edges carry `kind`."""
        pg = nx.DiGraph()
        for pid, prog in self.programs.items():
            pg.add_node(pid, path=prog.path)
        for u, v, d in self.graph.edges(data=True):
            if u.startswith("prog:") and v.startswith("prog:") and d["kind"] in PROGRAM_EDGE_KINDS:
                pg.add_edge(u[5:], v[5:], kind=d["kind"], kinds=list(d["kinds"]),
                            count=d.get("count", 1))
        return pg

    def rank_programs(self) -> list[tuple[str, float]]:
        """PageRank over the program graph (most depended-upon first)."""
        scores = _pagerank(self.program_graph())
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))

    def group_of(self, pid: str) -> str:
        """directory group below the common program root ("(root)" when flat)."""
        return self._groups.get(pid, "(root)")

    def summary(self) -> RepoSummary:
        seen_paths: set[str] = set()
        loc = 0
        for f in self.files:
            if f.kind in ("program", "copybook") and f.path not in seen_paths:
                seen_paths.add(f.path)
                loc += f.loc
        unresolved: set[str] = set()
        dynamic: set[str] = set()
        for pid, prog in self.programs.items():
            for c in prog.calls:
                if c.resolved:
                    continue
                if c.kind == "cics-start":
                    unresolved.add(f"TRANSID {c.target}")
                elif c.via and c.via == c.target:
                    dynamic.add(f"{pid}:{c.target}")
                elif not SYSTEM_PROGRAMS_RE.match(c.target):
                    unresolved.add(c.target)
        pg = self.program_graph()
        return RepoSummary(
            name=self.name,
            root=str(self.root),
            programs=len(self.programs),
            copybooks=len(self.copybooks),
            jcl_jobs=sum(1 for j in self.jobs.values() if j.kind == "job"),
            bms_mapsets=len(self.mapsets),
            cobol_loc=loc,
            call_edges=pg.number_of_edges(),
            unresolved_calls=sorted(unresolved),
            missing_copybooks=sorted(self._missing),
            unresolved_dynamic=sorted(dynamic),
            warnings=list(self.warnings),
        )

    def edge_counts(self) -> dict[str, int]:
        return dict(Counter(d["kind"] for _, _, d in self.graph.edges(data=True)))

    def to_dict(self) -> dict:
        """JSON-serializable dump (OUT/analysis.json)."""
        return {
            "name": self.name,
            "root": str(self.root),
            "summary": self.summary().model_dump(),
            "files": [f.model_dump() for f in self.files],
            "programs": {k: v.model_dump() for k, v in sorted(self.programs.items())},
            "copybooks": {k: v.model_dump() for k, v in sorted(self.copybooks.items())},
            "jobs": {k: v.model_dump() for k, v in sorted(self.jobs.items())},
            "mapsets": {k: v.model_dump() for k, v in sorted(self.mapsets.items())},
            "datastores": {k: v.model_dump() for k, v in sorted(self.datastores.items())},
            "transactions": dict(sorted(self.transactions.items())),
            "edges": [
                {"source": u, "target": v, "kind": d["kind"], "kinds": d.get("kinds", []),
                 "count": d.get("count", 1)}
                for u, v, d in sorted(self.graph.edges(data=True), key=lambda e: (e[0], e[1]))
            ],
            "edge_counts": self.edge_counts(),
            "entrypoints": self.entrypoints(),
            "groups": {pid: self.group_of(pid) for pid in sorted(self.programs)},
            "ranking": [[p, round(s, 6)] for p, s in self.rank_programs()],
        }


# -- helpers ----------------------------------------------------------------


def _add(lst: list[str], v: str) -> None:
    if v and v not in lst:
        lst.append(v)


def _system_copybook(member: str) -> bool:
    """IBM-supplied members (CICS DFH*, MQ CMQ*, SQLCA / SQLDA) are never repo files."""
    return member.startswith(("DFH", "CMQ", "CEE")) or member in ("SQLCA", "SQLDA")


def _strong_copybook(text: str) -> bool:
    """stricter sniff for extensionless files: several COBOL data entries or a DCLGEN."""
    entries = re.findall(
        r"^.{0,12}\b(?:0?[1-9]|[1-4][0-9]|77|88)\s+[A-Z0-9][\w-]*\b.*?"
        r"(?:\bPIC(?:TURE)?\b|\bVALUE\b|\bREDEFINES\b|\bOCCURS\b|\.\s*$)",
        text[:200_000], re.IGNORECASE | re.MULTILINE,
    )
    return len(entries) >= 2 or bool(sql_declared_tables(text))


def _prefer(new_path: str, old_path: str) -> bool:
    """when two files define the same PROGRAM-ID, prefer the original over expanded copies."""
    def rank(p: str) -> tuple[int, int, str]:
        noisy = 1 if re.search(r"expand|backup|\bold\b|copy of|listing|bak", p, re.I) else 0
        return (noisy, p.count("/"), p)

    return rank(new_path) < rank(old_path)
