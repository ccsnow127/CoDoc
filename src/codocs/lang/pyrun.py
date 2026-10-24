"""PythonRunner: pytest + coverage.py in hardlink farms.

every call runs in a sparse call farm built from a per-project base farm (hardlinks of the
project's .py files, symlinks for everything else), so nothing is written into the repository or
the sandbox: overlays are real files in the call farm, hidden files are absent, byte-code writing
is off and pytest's cache plugin is disabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from codocs.golang.gotool import (
    MAX_BUILD_ERRORS,
    OUTPUT_CAP,
    OUTPUT_LIMIT,
    TAIL_CAP,
    _Capture,
    _drain,
    _killpg,
    _Proc,
)
from codocs.lang.base import CoverBlock, LangTestResult

log = logging.getLogger(__name__)

OUTER_SLACK = 5
PLUGIN = "codocs_pytest_report"
PLUGIN_SRC = Path(__file__).resolve().parent / "tools" / f"{PLUGIN}.py"
NOT_STARTED = ("pytest timed out before any test started (collection or module imports did "
               "not finish)")
FARM_SKIP_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
                            ".venv", "venv", ".tox", "node_modules", "target", "build", "dist"})
# build-output names that are also legal package names (pypa/build ships src/build): kept when
# they are a regular package, or hold model files and do not sit next to a build file
SOFT_SKIP_DIRS = frozenset({"target", "build", "dist"})
BUILD_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "pom.xml", "build.gradle",
               "build.gradle.kts", "CMakeLists.txt", "Makefile", "makefile", "GNUmakefile")
# addopts flags that change what pytest imports or collects; the rest of addopts is dropped
# (`-o addopts=`) because it may name plugins codocs disables (--cov, -n, -p xdist, ...)
_DISABLED_PLUGINS = frozenset({"cacheprovider", "randomly", "xdist", "cov", "pytest_cov",
                               "no:cacheprovider"})
LINKED_NAMES = ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", ".pytest.ini",
                "tox.ini", "conftest.py")
PY_SUFFIXES = (".py", ".pyi")
_PROBE = ("import sys, pytest, coverage; "
          "print(sys.version.split()[0], pytest.__version__, coverage.__version__)")
_COMPILE = r"""
import json, sys
errs = []
for f in sys.argv[1:]:
    try:
        compile(open(f, "rb").read(), f, "exec")
    except SyntaxError as e:
        errs.append({"path": e.filename or f, "line": e.lineno or 0, "col": e.offset or 0,
                     "type": type(e).__name__, "msg": e.msg or str(e)})
    except (ValueError, TypeError) as e:
        errs.append({"path": f, "line": 0, "col": 0, "type": type(e).__name__, "msg": str(e)})
print("@@CODOCS " + json.dumps(errs))
"""
_IMPORT = r"""
import importlib, json, os, sys, traceback
for p in os.environ.get("CODOCS_PYTEST_PATH_APPEND", "").split(os.pathsep):
    if p and p not in sys.path:
        sys.path.append(p)
roots = [r.rstrip(os.sep) + os.sep for r in os.environ["CODOCS_PYTEST_ROOTS"].split(os.pathsep) if r]
errs = []
def project(p):
    return p and not p.startswith("<") and "site-packages" not in p and any(
        p.startswith(r) or os.path.realpath(p).startswith(r) for r in roots)
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except BaseException as e:
        if isinstance(e, SyntaxError) and e.filename:
            errs.append({"path": e.filename, "line": e.lineno or 0, "col": e.offset or 0,
                         "type": type(e).__name__, "msg": e.msg or str(e)})
            continue
        pick = None
        for fr in traceback.extract_tb(e.__traceback__):
            if project(fr.filename):
                pick = fr
        msg = str(e).splitlines()[0] if str(e) else ""
        errs.append({"path": pick.filename if pick else "", "line": pick.lineno if pick else 0,
                     "col": 0, "type": type(e).__name__, "msg": msg, "module": name})
print("@@CODOCS " + json.dumps(errs))
"""
_ANALYSIS = r"""
import json, sys
import coverage
cov = coverage.Coverage(config_file=sys.argv[1])
cov.load()
out = {}
for f in sys.argv[2:]:
    try:
        _, stmts, _, missing, _ = cov.analysis2(f)
    except Exception:
        continue
    out[f] = [sorted(stmts), sorted(missing)]
print("@@CODOCS " + json.dumps(out))
"""
_IMPORT_SAFE = frozenset({"import_statement", "import_from_statement", "future_import_statement",
                          "function_definition", "class_definition", "decorated_definition",
                          "assignment", "string", "comment", "pass_statement",
                          "type_alias_statement"})


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[-n:]


def _rel_join(d: str, name: str) -> str:
    return f"{d}/{name}" if d else name


def _under(rel: str, root: str) -> bool:
    return not root or rel == root or rel.startswith(root + "/")


def _relto(rel: str, root: str) -> str:
    if not root:
        return rel
    return "" if rel == root else rel[len(root) + 1:]


def _parent(rel: str) -> str:
    p = PurePosixPath(rel).parent.as_posix()
    return "" if p == "." else p


# -- path mapping -------------------------------------------------------------------------


class PathMap:
    """tool paths (farms, sandbox, repo) back to repo rels; longest prefix wins.

    every prefix is registered in its given and its resolved form; a relative path is taken as
    relative to cwd_rel (the farm root's rel).
    """

    def __init__(self, prefixes: dict[str | Path, str], cwd_rel: str = ""):
        self.prefixes: list[tuple[str, str]] = []
        for p, rel in prefixes.items():
            for form in {str(p), os.path.realpath(str(p))}:
                self.prefixes.append((form.rstrip("/"), rel.strip("/")))
        self.prefixes.sort(key=lambda x: -len(x[0]))
        self.cwd_rel = cwd_rel.strip("/")

    def rel(self, path: str) -> str | None:
        """repo rel of a tool path, None when it is outside every registered prefix."""
        if not path:
            return None
        if not os.path.isabs(path):
            norm = os.path.normpath(_rel_join(self.cwd_rel, path))
            return None if norm.startswith("..") else ("" if norm == "." else norm)
        for pre, rel in self.prefixes:
            if path == pre:
                return rel
            if path.startswith(pre + "/"):
                rest = os.path.normpath(path[len(pre) + 1:])
                return _rel_join(rel, rest) if rest != "." else rel
        return None

    def text(self, text: str) -> str:
        """text with every registered prefix replaced by its rel."""
        for pre, rel in self.prefixes:
            if pre and pre in text:
                text = text.replace(pre + "/", f"{rel}/" if rel else "").replace(pre, rel or ".")
        return text


# -- interpreter ----------------------------------------------------------------------------


@dataclass
class Interpreter:
    path: str
    python: str
    pytest: str
    coverage: str
    own: bool            # the project's own venv


def probe(py: str) -> tuple[Interpreter | None, str]:
    """an interpreter that can import pytest and coverage (else None and the reason)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        cp = subprocess.run([py, "-c", _PROBE], capture_output=True, text=True, timeout=60,
                            env=env, cwd=tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if cp.returncode != 0:
        lines = [ln for ln in (cp.stderr or cp.stdout).strip().splitlines() if ln.strip()]
        return None, lines[-1] if lines else f"exit status {cp.returncode}"
    parts = cp.stdout.split()
    if len(parts) < 3:
        return None, f"unexpected probe output {cp.stdout.strip()!r}"
    return Interpreter(path=py, python=parts[0], pytest=parts[1], coverage=parts[2],
                       own=False), ""


# -- events -----------------------------------------------------------------------------------


def read_events(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _verdict(phases: dict[str, dict]) -> str | None:
    """one invocation: fail if any phase failed, skip if skipped/xfailed, pass; None = unfinished."""
    if any(p.get("outcome") == "failed" for p in phases.values()):
        return "fail"
    if "teardown" not in phases and not (
            "setup" in phases and phases["setup"].get("outcome") == "skipped"):
        return None
    if any(p.get("outcome") == "skipped" for p in phases.values()):
        return "skip"
    return "pass"


def fold_verdicts(events: list[dict], *, requested: list[str] | None, died: bool,
                  timed_out: bool, crash_text: str = "") -> tuple[dict[str, str], dict[str, str]]:
    """(tests, output) from reporter events: per-invocation verdicts folded per id."""
    inv: dict[str, dict] = {}
    order: list[str] = []
    for ev in events:
        kind = ev.get("e")
        if kind not in ("run", "result"):
            continue
        uid = ev.get("uid") or ev.get("id") or ""
        rec = inv.setdefault(uid, {"id": ev.get("id") or uid, "phases": {}, "run": False})
        if kind == "run":
            rec["run"] = True
            order.append(uid)
        else:
            rec["phases"][ev.get("phase") or "call"] = ev
    verdicts = {uid: _verdict(rec["phases"]) for uid, rec in inv.items()}
    explicit_fail = any(v == "fail" for v in verdicts.values())
    running = order[-1] if order else None
    ids: dict[str, list[str]] = {}
    for uid, rec in inv.items():
        ids.setdefault(rec["id"], []).append(uid)
    tests: dict[str, str] = {}
    output: dict[str, str] = {}
    for tid, uids in ids.items():
        vs = []
        texts = []
        for uid in uids:
            v = verdicts[uid]
            for ph in ("setup", "call", "teardown"):
                t = inv[uid]["phases"].get(ph, {}).get("text") or ""
                if t:
                    texts.append(t)
            if v is None:
                if uid == running and (timed_out or not explicit_fail):
                    v = "fail"
                    if crash_text:
                        texts.append(crash_text)
                else:
                    v = "interrupted"
            vs.append(v)
        if "fail" in vs:
            verdict = "fail"
        elif "interrupted" in vs:
            verdict = "interrupted"
        elif all(v == "skip" for v in vs):
            verdict = "skip"
        else:
            verdict = "pass"
        tests[tid] = verdict
        joined = "\n".join(texts)
        if crash_text and crash_text in texts and len(joined) > OUTPUT_CAP:
            rest = "\n".join(t for t in texts if t is not crash_text)
            joined = _cap(rest, max(0, OUTPUT_CAP - len(crash_text) - 1)) + "\n" + crash_text
        output[tid] = _cap(joined, OUTPUT_CAP)
    if died:
        for name in requested or []:
            if name not in tests:
                tests[name] = "interrupted"
                output.setdefault(name, "")
    return tests, output


# -- farms ------------------------------------------------------------------------------------


def _link(src: str, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def carried_addopts(value: Any) -> list[str]:
    """the flags of a project's addopts that codocs keeps: --import-mode (duplicate test
    basenames need importlib mode), -p plugin loads (except the plugins codocs disables) and
    --asyncio-mode / --strict-markers."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        words = [str(v) for v in value]
    else:
        try:
            words = shlex.split(str(value))
        except ValueError:
            words = str(value).split()
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if w.startswith(("--import-mode=", "--asyncio-mode=")) or w == "--strict-markers":
            out.append(w)
        elif w in ("--import-mode", "--asyncio-mode") and nxt is not None:
            out.append(f"{w}={nxt}")
            i += 1
        elif w == "-p" and nxt is not None:
            if nxt.removeprefix("no:") not in _DISABLED_PLUGINS:
                out += ["-p", nxt]
            i += 1
        elif w.startswith("-p") and len(w) > 2:
            if w[2:].removeprefix("no:") not in _DISABLED_PLUGINS:
                out += ["-p", w[2:]]
        i += 1
    return out


def build_base_farm(src_root: Path, dst: Path, exclude: set[Path],
                    model_dirs: set[str] | None = None) -> None:
    """hardlink farm of a project: real dirs where .py files live, symlinks for the rest.

    model_dirs: dirs (relative to src_root) holding files of the repository model. a build-output
    name (build, dist, target) is kept when it is a regular package (has __init__.py), or is one
    of model_dirs and no build file (pyproject.toml, setup.py, pom.xml, ...) sits next to it.
    """
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(dst, ignore_errors=True)
    src_root = src_root.resolve()
    py_dirs: set[str] = {""}
    skip: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(src_root):
        d = Path(dirpath)
        rel = d.relative_to(src_root).as_posix()
        rel = "" if rel == "." else rel
        keep = []
        beside_build = any(f in filenames for f in BUILD_FILES)
        for name in dirnames:
            p = d / name
            sub = _rel_join(rel, name)
            soft = name in SOFT_SKIP_DIRS and ((p / "__init__.py").is_file() or (
                model_dirs is not None and sub in model_dirs and not beside_build))
            if (name in FARM_SKIP_DIRS and not soft) or name.endswith(".egg-info") or \
                    p.is_symlink() or p.resolve() in exclude or (p / "pyvenv.cfg").exists():
                skip.add(sub)
                continue
            keep.append(name)
        dirnames[:] = keep
        if any(f.endswith(PY_SUFFIXES) for f in filenames):
            cur = rel
            while True:
                py_dirs.add(cur)
                if not cur:
                    break
                cur = _parent(cur)
    for rel in sorted(py_dirs):
        out = dst / rel if rel else dst
        out.mkdir(parents=True, exist_ok=True)
        src = src_root / rel if rel else src_root
        try:
            entries = list(os.scandir(src))
        except OSError:
            continue
        for e in entries:
            sub = _rel_join(rel, e.name)
            target = out / e.name
            try:
                if e.is_symlink():
                    if sub in skip:
                        continue
                    os.symlink(os.readlink(e.path), target)
                elif e.is_dir():
                    if sub in skip or sub in py_dirs:
                        continue
                    os.symlink(e.path, target, target_is_directory=True)
                elif e.name.endswith(PY_SUFFIXES) or e.name in LINKED_NAMES or \
                        fnmatchcase(e.name, "requirements*.txt"):
                    _link(e.path, target)
                else:
                    os.symlink(e.path, target)
            except OSError:
                continue


def build_call_farm(base: Path, dst: Path, real_dirs: set[str],
                    overlay: dict[str, bytes | None]) -> None:
    """sparse farm: real dirs (with hardlinks) where needed, dir symlinks into base elsewhere."""
    dirs = set()
    for d in list(real_dirs) + [_parent(k) for k in overlay]:
        cur = d
        while True:
            dirs.add(cur)
            if not cur:
                break
            cur = _parent(cur)
    for rel in sorted(dirs):
        out = dst / rel if rel else dst
        out.mkdir(parents=True, exist_ok=True)
        src = base / rel if rel else base
        try:
            entries = list(os.scandir(src))
        except OSError:
            entries = []
        for e in entries:
            sub = _rel_join(rel, e.name)
            if sub in overlay or sub in dirs:
                continue
            target = out / e.name
            try:
                if e.is_symlink():
                    os.symlink(os.readlink(e.path), target)
                elif e.is_dir():
                    os.symlink(e.path, target, target_is_directory=True)
                else:
                    _link(e.path, target)
            except OSError:
                continue
    for rel, data in overlay.items():
        if data is None:
            continue
        p = dst / rel
        if p.is_symlink() or p.exists():
            p.unlink()
        p.write_bytes(data)


# -- the runner ---------------------------------------------------------------------------------


class PythonRunner:
    """runs a unit's pytest tests (with coverage.py) in per-call farms; never writes the repo."""

    lang = "python"

    def __init__(self, repo_root: str | Path, *, adapter: Any, work_dir: str | Path | None = None,
                 tools_dir: str | Path | None = None, exec_root: str | Path | None = None,
                 settings: Any = None, workers: int = 4, timeout: int = 300,
                 py: str | None = None, pytest_args: str | list[str] | None = None,
                 run_ctx: Any = None, exclude: list[str | Path] | None = None):
        self.module_root = Path(repo_root).expanduser().resolve()
        self.exec_root = Path(exec_root).resolve() if exec_root else self.module_root
        self.module = self.module_root.name
        self.adapter = adapter
        self.settings = settings
        self.run_ctx = run_ctx
        self.workers = max(1, int(workers))
        self.timeout = int(timeout)
        work = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "codocs-py" / "tmp"
        self.work_dir = work.resolve() if work.exists() else work
        top = self.work_dir.parent if self.work_dir.name == "tmp" else self.work_dir
        self.py_dir = top / "py"
        self.tools_dir = Path(tools_dir) if tools_dir else top / "tools"
        self._py = py if py is not None else (getattr(settings, "py", "") or
                                              os.environ.get("CODOCS_PY", ""))
        args = pytest_args if pytest_args is not None else getattr(settings, "pytest_args", "")
        self.pytest_args = shlex.split(args) if isinstance(args, str) else list(args or [])
        self._exclude = {Path(p).resolve() for p in exclude or []}
        self._exclude.add(top.resolve() if top.exists() else top)
        self._interp: Interpreter | None = None
        self._probed = False
        self._rejected: list[str] = []
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop = None
        self._bases: dict[str, tuple[Path, int]] = {}
        self._base_locks: dict[str, asyncio.Lock] = {}
        self.notes: dict[str, list[str]] = {}

    @classmethod
    def from_context(cls, adapter: Any, ctx: Any) -> PythonRunner:
        """a runner for a RunContext (duck-typed: repo_root, exec_root, work_dir, tools_dir,
        settings, workers, timeout, ensure_synced, sync_generation)."""
        settings = getattr(ctx, "settings", None)
        timeout = getattr(ctx, "timeout", None) or getattr(settings, "test_timeout", 0) or \
            getattr(settings, "go_timeout", 300)
        work_dir = getattr(ctx, "work_dir", None)
        if work_dir is None and getattr(ctx, "out_dir", None) is not None:
            work_dir = Path(ctx.out_dir) / "work" / "tmp"
        exclude = [p for p in (getattr(ctx, "out_dir", None),) if p is not None]
        exclude += list(getattr(ctx, "sandbox_exclude", None) or [])
        return cls(ctx.repo_root, adapter=adapter, work_dir=work_dir,
                   tools_dir=getattr(ctx, "tools_dir", None),
                   exec_root=getattr(ctx, "exec_root", None), settings=settings,
                   workers=getattr(ctx, "workers", None) or getattr(settings, "workers", 4),
                   timeout=int(timeout), run_ctx=ctx, exclude=exclude)

    # -- interpreter ------------------------------------------------------------------------

    def _candidates(self) -> tuple[list[tuple[str, bool]], bool]:
        if self._py:
            return [(os.path.expanduser(self._py), False)], True
        out: list[tuple[str, bool]] = []
        for venv in (".venv", "venv"):
            p = self.module_root / venv / "bin" / "python"
            if p.exists():
                out.append((str(p), True))
        out.append((sys.executable, False))
        return out, False

    def interpreter(self) -> Interpreter | None:
        if not self._probed:
            self._probed = True
            cands, explicit = self._candidates()
            for path, own in cands:
                it, err = probe(path)
                if it is not None:
                    it.own = own
                    self._interp = it
                    break
                self._rejected.append(f"{path} ({err})")
            if self._interp is not None:
                for r in self._rejected:
                    log.warning("WARNING: %s lacks pytest/coverage; using %s", r,
                                self._interp.path)
            elif explicit:
                log.warning("WARNING: %s cannot run pytest with coverage", self._rejected[0])
        return self._interp

    def available(self) -> bool:
        return self.interpreter() is not None

    def version(self) -> str:
        it = self.interpreter()
        if it is None:
            return ""
        return f"Python {it.python}, pytest {it.pytest}, coverage {it.coverage}"

    def unit_notes(self, unit_rel: str) -> list[str]:
        """baseline notes for a unit: interpreter caveats and collection errors seen."""
        notes: list[str] = []
        it = self.interpreter()
        unit = self.adapter.unit(unit_rel)
        if it is not None and not it.own and unit is not None:
            had_venv = any(own for _, own in self._candidates()[0])
            deps = self.adapter.allowed_third_party(self.module_root, unit)
            if had_venv or deps:
                notes.append(f"tests run with {it.path}; project dependencies may be missing")
        return notes + list(dict.fromkeys(self.notes.get(unit_rel.strip("/"), [])))

    # -- helpers ------------------------------------------------------------------------------

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.workers)
            self._sem_loop = loop
            self._base_locks = {}
        return self._sem

    def _generation(self) -> int:
        return int(getattr(self.run_ctx, "sync_generation", 0) or 0)

    async def _synced(self) -> None:
        fn = getattr(self.run_ctx, "ensure_synced", None)
        if fn is not None:
            await fn()

    def _plugin_dir(self) -> Path:
        d = self.tools_dir / "python"
        dst = d / f"{PLUGIN}.py"
        data = PLUGIN_SRC.read_bytes()
        try:
            same = dst.read_bytes() == data
        except OSError:
            same = False
        if not same:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{PLUGIN}.{os.getpid()}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, dst)
        return d.resolve()

    async def _base(self, proj: str) -> Path:
        """the project's base farm (built once per sync generation)."""
        self._semaphore()
        lock = self._base_locks.setdefault(proj, asyncio.Lock())
        async with lock:
            gen = self._generation()
            hit = self._bases.get(proj)
            if hit is not None and hit[1] == gen and hit[0].is_dir():
                return hit[0]
            key = proj.replace("/", "__") or "_root"
            dst = self.py_dir / key / "base"
            dst.parent.mkdir(parents=True, exist_ok=True)
            src = self.exec_root / proj if proj else self.exec_root
            await asyncio.to_thread(build_base_farm, src, dst, self._exclude,
                                    self._model_dirs(proj))
            dst = dst.resolve()
            self._bases[proj] = (dst, gen)
            return dst

    def _model_dirs(self, proj: str) -> set[str] | None:
        """dirs (relative to proj) that hold a Python file of the repository model."""
        model = getattr(self.adapter, "model", None)
        if model is None:
            return None
        out: set[str] = set()
        for f in model.files:
            if not _under(f, proj):
                continue
            cur = _parent(_relto(f, proj))
            while cur and cur not in out:
                out.add(cur)
                cur = _parent(cur)
        return out

    def _encode(self, rel: str, text: str) -> bytes:
        from codocs.lang.treesitter import encode

        ctx = getattr(self.adapter, "ctx", None)
        codec = ctx.codec(rel) if ctx is not None and (self.module_root / rel).exists() \
            else "utf-8"
        return encode(text, codec)

    async def _exec(self, args: list[str], *, cwd: Path, env: dict[str, str],
                    timeout: float) -> _Proc:
        async with self._semaphore():
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            out = _Capture(OUTPUT_LIMIT, lambda: _killpg(proc))
            err = _Capture(OUTPUT_LIMIT, lambda: _killpg(proc))
            readers = asyncio.gather(_drain(proc.stdout, out), _drain(proc.stderr, err))
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=max(timeout, 0.01))
            except asyncio.TimeoutError:
                timed_out = True
                _killpg(proc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=10)
            except BaseException:
                _killpg(proc)
                readers.cancel()
                raise
            try:
                await asyncio.wait_for(readers, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                _killpg(proc)
                readers.cancel()
                with contextlib.suppress(BaseException):
                    await readers
            rc = proc.returncode if proc.returncode is not None else -1
            return _Proc(rc, out.text(), err.text(), timed_out, out.overflow or err.overflow)

    def _call_dir(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="p", dir=self.work_dir)).resolve()

    # -- per call -------------------------------------------------------------------------------

    def _plan(self, unit: Any, overlay: dict[str, str | None], run: list[str] | None
              ) -> tuple[str, list[str], set[str]]:
        """(project root, selected test file rels, rels of files holding requested tests)."""
        proj = unit.extra.get("project_root", unit.build_root)
        for rel in overlay:
            parts = rel.split("/")
            if os.path.isabs(rel) or ".." in parts or not _under(rel, proj):
                raise ValueError(f"overlay file {rel} is outside the project root "
                                 f"{proj or '.'} of {unit.rel or '.'}")
        hidden = {k for k, v in overlay.items() if v is None}
        model = getattr(self.adapter, "model", None)
        exists = (lambda r: r in model.fileset) if model is not None else \
            (lambda r: (self.module_root / r).is_file())
        if run is None:
            files = [f for f in unit.test_files if f not in hidden]
            files += [k for k, v in overlay.items() if v is not None and k not in files
                      and self.adapter.is_test_file(k)]
            return proj, files, set()
        files = []
        for name in run:
            head = name.split("::", 1)[0]
            rel = _rel_join(proj, head)
            ok = (overlay.get(rel) is not None) or (rel not in overlay and exists(rel))
            if ok and rel not in files:
                files.append(rel)
        return proj, files, set(files)

    def _env(self, unit: Any, proj: str, farm: Path, call: Path, files: list[str],
             base: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COVERAGE_PROCESS_START",
                            "PYTHONHOME")}
        paths = [str(self._plugin_dir())]
        model = getattr(self.adapter, "model", None)
        scripts = model.script_dirs if model is not None else set()
        # script dirs and fallback dirs go after the stdlib (appended by the plugin): they are
        # plain directories of top-level modules that may shadow stdlib names (e.g. platform/)
        late: list[str] = []
        for r in unit.extra.get("import_roots", []):
            if _under(r, proj):
                p = str(farm / _relto(r, proj))
                dst = late if r in scripts and r != proj else paths
                if p not in paths and p not in late:
                    dst.append(p)
        for f in files:
            for d in (model.fallback_dirs.get(f, []) if model is not None else []):
                if _under(d, proj):
                    p = str(farm / _relto(d, proj))
                    if p not in paths and p not in late:
                        late.append(p)
        if str(farm) not in paths:
            paths.append(str(farm))
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        tmp = call / "tmp"
        tmp.mkdir(exist_ok=True)
        env.update({
            "PYTHONPATH": os.pathsep.join(paths), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0", "CODOCS_PYTEST_EVENTS": str(call / "events.jsonl"),
            "CODOCS_PYTEST_ROOTS": os.pathsep.join([str(farm), str(base)]),
            "CODOCS_PYTEST_PATH_APPEND": os.pathsep.join(late),
            "TMPDIR": str(tmp), "COVERAGE_FILE": str(call / ".coverage"), "PWD": str(farm),
        })
        return env

    def _mapper(self, proj: str, farm: Path, base: Path, call: Path) -> PathMap:
        return PathMap({farm: proj, base: proj, self.exec_root: "", self.module_root: "",
                        call / "ov": ""}, cwd_rel=proj)

    def _pytest_cmd(self, it: Interpreter, unit: Any, proj: str, farm: Path, call: Path,
                    files: list[str], *, cover: bool, failfast: bool, collect: bool = False
                    ) -> list[str]:
        cmd = [it.path]
        if cover:
            rc = call / "covrc"
            rc.write_text("[run]\nbranch = False\n", encoding="utf-8")
            src = farm / _relto(unit.rel, proj) if unit.rel != proj else farm
            cmd += ["-m", "coverage", "run", f"--rcfile={rc}", f"--source={src}"]
        cmd += ["-m", "pytest", "-p", PLUGIN, "-p", "no:cacheprovider", "-p", "no:randomly",
                "-p", "no:xdist", "-p", "no:cov", "-o", "addopts=", "-q",
                f"--rootdir={farm}", f"--confcutdir={farm}", "--continue-on-collection-errors"]
        cfg = unit.extra.get("pytest_config", "")
        cmd += carried_addopts(unit.extra.get("pytest_addopts"))
        if cfg and _under(cfg, proj):
            cmd += ["-c", str(farm / _relto(cfg, proj))]
        else:
            ini = call / "pytest.ini"
            ini.write_text("[pytest]\n", encoding="utf-8")
            cmd += ["-c", str(ini)]
        if collect:
            cmd.append("--collect-only")
        if failfast:
            cmd.append("-x")
        cmd += self.pytest_args
        cmd += [_relto(f, proj) for f in files]
        return cmd

    def _error_lines(self, errors: list[dict], mapper: PathMap, fallback: str) -> list[str]:
        out = []
        for e in errors:
            rel = mapper.rel(e.get("path") or "") or fallback
            line = max(int(e.get("line") or 0), 1)
            col = int(e.get("col") or 0)
            loc = f"{rel}:{line}" + (f":{col}" if col else "")
            msg = mapper.text(str(e.get("msg") or ""))
            out.append(f"{loc}: {e.get('type') or 'Error'}: {msg}")
        return out

    @staticmethod
    def _crash_errors(text: str, mapper: PathMap) -> list[str]:
        """build-error lines from pytest's own output when the session never started."""
        import re

        lines = mapper.text(text).splitlines()
        frame = None
        out: list[str] = []
        for ln in lines:
            m = re.match(r"^(\S+\.pyi?):(\d+): in ", ln.strip())
            if m:
                frame = (m.group(1), m.group(2))
                continue
            m = re.match(r"^E\s+(\w[\w.]*(?:Error|Exception)\b.*)$", ln.strip())
            if m and frame is not None:
                out.append(f"{frame[0]}:{frame[1]}: {m.group(1)}")
                frame = None
        if not out:
            out = [ln.strip() for ln in lines if ln.strip()][-10:]
        return out[:MAX_BUILD_ERRORS]

    async def test(self, unit_rel: str, *, overlay: dict[str, str | None] | None = None,
                   run: list[str] | None = None, cover: bool = False, failfast: bool = False,
                   compile_only: bool = False, timeout: int | None = None) -> LangTestResult:
        assert not (failfast and cover), "failfast is never combined with cover"
        t = int(timeout if timeout is not None else self.timeout)
        unit = self.adapter.unit(unit_rel)
        if unit is None:
            return LangTestResult(build_ok=False,
                                  build_errors=[f"no Python unit {unit_rel or '.'}"])
        it = self.interpreter()
        if it is None:
            return LangTestResult(build_ok=False, build_errors=[
                "no Python interpreter with pytest and coverage: "
                + "; ".join(self._rejected)])
        ov = {k.strip("/"): v for k, v in (overlay or {}).items()}
        proj, files, req_files = self._plan(unit, ov, run)
        if compile_only:
            return await self._compile(it, unit, proj, ov, t)
        if run is not None and not run:
            return LangTestResult(build_ok=True)
        await self._synced()
        base = await self._base(proj)
        call = self._call_dir()
        try:
            farm = call / "root"
            real = {_relto(unit.rel, proj)} | {_relto(_parent(f), proj) for f in files}
            data = {_relto(k, proj): (None if v is None else self._encode(k, v))
                    for k, v in ov.items()}
            await asyncio.to_thread(build_call_farm, base, farm, real, data)
            env = self._env(unit, proj, farm, call, files, base)
            env["CODOCS_TIMEOUT"] = str(t)
            env["CODOCS_PYTEST_FAULT"] = str(call / "fault.txt")
            if run is not None:
                sel = call / "select.txt"
                sel.write_text("".join(n + "\n" for n in run), encoding="utf-8")
                env["CODOCS_PYTEST_SELECT"] = str(sel)
            mapper = self._mapper(proj, farm, base, call)
            if not files:
                return LangTestResult(build_ok=True)
            cmd = self._pytest_cmd(it, unit, proj, farm, call, files, cover=cover,
                                   failfast=failfast)
            p = await self._exec(cmd, cwd=farm, env=env, timeout=t + OUTER_SLACK)
            events = read_events(call / "events.jsonl")
            try:
                fault = (call / "fault.txt").read_text(encoding="utf-8", errors="replace")
            except OSError:
                fault = ""
            res = self._result(unit, proj, events, p, mapper, run=run, overlay=ov,
                               req_files=req_files, fault=fault)
            if cover and res.build_ok:
                await self._coverage(it, unit, proj, farm, call, env, mapper, res)
            return res
        finally:
            shutil.rmtree(call, ignore_errors=True)

    def _result(self, unit: Any, proj: str, events: list[dict], p: _Proc, mapper: PathMap, *,
                run: list[str] | None, overlay: dict[str, str | None],
                req_files: set[str], fault: str = "") -> LangTestResult:
        text = mapper.text(p.stdout + ("\n" if p.stdout and p.stderr else "") + p.stderr)
        tail = _cap(text, TAIL_CAP)
        if fault.strip():
            head = fault.strip().splitlines()[0][:200]
            tail = _cap(text, TAIL_CAP - len(head) - 1) + "\n" + head
        started = any(ev.get("e") == "start" for ev in events)
        finished = any(ev.get("e") == "finish" for ev in events)
        any_run = any(ev.get("e") == "run" for ev in events)
        inner = ("Timeout (" in fault or "Timeout (" in p.stderr) and not finished
        timed_out = p.timed_out or inner
        res = LangTestResult(build_ok=True, returncode=p.returncode, timed_out=timed_out,
                             tail=tail)
        if timed_out and not any_run:
            res.build_ok = False
            res.build_errors = [NOT_STARTED]
            return res
        if not started and p.returncode != 0:
            res.build_ok = False
            res.build_errors = self._crash_errors(p.stdout + "\n" + p.stderr, mapper)
            return res
        replaced = {k for k, v in overlay.items() if v is not None}
        build_errors: list[str] = []
        collect_errors = False
        for ev in events:
            if ev.get("e") != "collect_error":
                continue
            collect_errors = True
            path = (ev.get("path") or "").split("::", 1)[0]
            rel = _rel_join(proj, path) if path else proj
            errs = self._error_lines(ev.get("errors") or [], mapper, rel)
            err_rels = {ln.split(":", 1)[0] for ln in errs}
            if rel in replaced or rel in req_files or err_rels & replaced:
                build_errors.extend(errs or [f"{rel}:1: collection error"])
            else:
                first = errs[0] if errs else f"{rel}: collection error"
                msg = first.split(": ", 1)[1] if ": " in first else first
                self.notes.setdefault(unit.rel, []).append(
                    f"collection error in {rel}: {msg}")
        if build_errors:
            res.build_ok = False
            res.build_errors = list(dict.fromkeys(build_errors))[:MAX_BUILD_ERRORS]
            return res
        normal = finished and (p.returncode in (0, 1, 5) or (p.returncode == 2 and
                                                               collect_errors))
        died = timed_out or not normal
        crash = ""
        if died and fault.strip():
            head, _, rest = mapper.text(fault).strip().partition("\n")
            crash = head + "\n" + _cap(rest, OUTPUT_CAP // 2)
        elif died:
            crash = _cap(mapper.text(p.stderr), OUTPUT_CAP // 2)
        requested = run
        if requested is None:
            requested = [i for ev in events if ev.get("e") == "collected"
                         for i in ev.get("ids") or []]
        res.tests, res.output = fold_verdicts(events, requested=requested, died=died,
                                              timed_out=timed_out, crash_text=crash)
        # per-test texts name the random call farm: map them back to repo paths
        res.output = {k: mapper.text(v) for k, v in res.output.items()}
        if run is not None:
            wanted = set(run)
            res.tests = {k: v for k, v in res.tests.items() if k in wanted}
            res.output = {k: v for k, v in res.output.items() if k in res.tests}
        return res

    def _coverage_files(self, unit: Any) -> list[str]:
        return [f for f in unit.files if f.endswith(".py")
                and PurePosixPath(f).name != "conftest.py"
                and not self.adapter.is_test_file(f)]

    async def _coverage(self, it: Interpreter, unit: Any, proj: str, farm: Path, call: Path,
                        env: dict[str, str], mapper: PathMap, res: LangTestResult) -> None:
        wanted = self._coverage_files(unit)
        rc = call / "covrc"
        out = call / "cov.json"
        blocks: dict[str, list[CoverBlock]] = {}
        error = ""
        if (call / ".coverage").exists():
            p = await self._exec([it.path, "-m", "coverage", "json", f"--rcfile={rc}", "-q",
                                  "-o", str(out)], cwd=farm, env=env, timeout=120)
            if p.returncode == 0 and out.exists():
                try:
                    data = json.loads(out.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    data = {}
                    error = f"coverage json unreadable: {e}"
                for key, info in (data.get("files") or {}).items():
                    path = key if os.path.isabs(key) else str(farm / key)
                    rel = mapper.rel(path)
                    if rel not in wanted:
                        continue
                    blocks[rel] = [CoverBlock(rel, n, 1, n, 1, 1, 1)
                                   for n in info.get("executed_lines") or []]
                    blocks[rel] += [CoverBlock(rel, n, 1, n, 1, 1, 0)
                                    for n in info.get("missing_lines") or []]
            else:
                lines = [ln for ln in (p.stderr or p.stdout).strip().splitlines() if ln.strip()]
                error = "coverage json failed: " + (lines[-1] if lines else
                                                    f"exit status {p.returncode}")
        else:
            error = "coverage.py wrote no data file"
        missing = [f for f in wanted if f not in blocks]
        if missing:
            paths = {str(farm / _relto(f, proj)): f for f in missing}
            p = await self._exec([it.path, "-c", _ANALYSIS, str(rc), *paths], cwd=farm,
                                 env=env, timeout=120)
            got = _marked_json(p.stdout)
            if isinstance(got, dict):
                for path, (stmts, miss) in got.items():
                    rel = paths.get(path)
                    if rel is None:
                        continue
                    ms = set(miss)
                    blocks[rel] = [CoverBlock(rel, n, 1, n, 1, 1, 0 if n in ms else 1)
                                   for n in stmts]
                if error and all(f in blocks for f in wanted):
                    error = ""
            elif not error:
                error = "coverage analysis failed: " + (p.stderr.strip().splitlines() or
                                                       [f"exit status {p.returncode}"])[-1]
        if error and not blocks:
            res.coverage = None
            res.coverage_error = error
            return
        res.coverage = [b for f in sorted(blocks) for b in sorted(blocks[f],
                                                                   key=lambda b: b.start_line)]

    async def _compile(self, it: Interpreter, unit: Any, proj: str,
                       overlay: dict[str, str | None], t: int) -> LangTestResult:
        """syntax check of the overlaid files, then an import of import-safe modules."""
        sources = [k for k, v in overlay.items() if v is not None and k.endswith(".py")]
        if not sources:
            return LangTestResult(build_ok=True)
        await self._synced()
        base = await self._base(proj)
        call = self._call_dir()
        try:
            farm = call / "root"
            data = {_relto(k, proj): (None if v is None else self._encode(k, v))
                    for k, v in overlay.items()}
            await asyncio.to_thread(build_call_farm, base, farm, {_relto(unit.rel, proj)}, data)
            env = self._env(unit, proj, farm, call, [], base)
            mapper = self._mapper(proj, farm, base, call)
            paths = [str(farm / _relto(k, proj)) for k in sources]
            p = await self._exec([it.path, "-c", _COMPILE, *paths], cwd=farm, env=env,
                                 timeout=t + OUTER_SLACK)
            errs = _marked_json(p.stdout)
            if errs is None:
                return LangTestResult(build_ok=False, returncode=p.returncode,
                                      timed_out=p.timed_out,
                                      build_errors=self._crash_errors(p.stderr, mapper),
                                      tail=_cap(mapper.text(p.stderr), TAIL_CAP))
            if errs:
                return LangTestResult(build_ok=False, returncode=p.returncode,
                                      build_errors=self._error_lines(errs, mapper, sources[0]))
            modules = [self.adapter.module_name(k) for k in sources
                       if not self.adapter.is_test_file(k) and import_safe(overlay[k] or "")]
            modules = [m for m in modules if m]
            if not modules:
                return LangTestResult(build_ok=True)
            p = await self._exec([it.path, "-c", _IMPORT, *modules], cwd=farm, env=env,
                                 timeout=t + OUTER_SLACK)
            errs = _marked_json(p.stdout)
            if p.timed_out:
                return LangTestResult(build_ok=False, timed_out=True, build_errors=[NOT_STARTED])
            if errs is None:
                return LangTestResult(build_ok=False, returncode=p.returncode,
                                      build_errors=self._crash_errors(p.stderr, mapper),
                                      tail=_cap(mapper.text(p.stderr), TAIL_CAP))
            if errs:
                fallback = sources[0]
                return LangTestResult(build_ok=False, returncode=p.returncode,
                                      build_errors=self._error_lines(errs, mapper, fallback))
            return LangTestResult(build_ok=True)
        finally:
            shutil.rmtree(call, ignore_errors=True)

    async def list_tests(self, unit_rel: str) -> list[str] | None:
        """the unit's existing tests as pytest collects them (folded ids), None on failure."""
        unit = self.adapter.unit(unit_rel)
        it = self.interpreter()
        if unit is None or it is None:
            return None
        if not unit.test_files:
            return []
        proj = unit.extra.get("project_root", unit.build_root)
        files = list(unit.test_files)
        await self._synced()
        base = await self._base(proj)
        call = self._call_dir()
        try:
            farm = call / "root"
            real = {_relto(unit.rel, proj)} | {_relto(_parent(f), proj) for f in files}
            await asyncio.to_thread(build_call_farm, base, farm, real, {})
            env = self._env(unit, proj, farm, call, files, base)
            env["CODOCS_TIMEOUT"] = str(self.timeout)
            mapper = self._mapper(proj, farm, base, call)
            cmd = self._pytest_cmd(it, unit, proj, farm, call, files, cover=False,
                                   failfast=False, collect=True)
            p = await self._exec(cmd, cwd=farm, env=env, timeout=self.timeout + OUTER_SLACK)
            events = read_events(call / "events.jsonl")
            for ev in events:
                if ev.get("e") == "collect_error":
                    path = (ev.get("path") or "").split("::", 1)[0]
                    rel = _rel_join(proj, path) if path else proj
                    errs = self._error_lines(ev.get("errors") or [], mapper, rel)
                    msg = errs[0].split(": ", 1)[1] if errs and ": " in errs[0] else \
                        "collection error"
                    self.notes.setdefault(unit.rel, []).append(
                        f"collection error in {rel}: {msg}")
            got = [ev for ev in events if ev.get("e") == "collected"]
            if not got or p.timed_out:
                return None
            prels = {_relto(f, proj) for f in files}
            ids = [i for i in got[-1].get("ids") or [] if i.split("::", 1)[0] in prels]
            return sorted(dict.fromkeys(ids))
        finally:
            shutil.rmtree(call, ignore_errors=True)


def _marked_json(stdout: str) -> Any:
    for line in reversed(stdout.splitlines()):
        if line.startswith("@@CODOCS "):
            try:
                return json.loads(line[len("@@CODOCS "):])
            except ValueError:
                return None
    return None


def import_safe(src: str) -> bool:
    """whether importing a module only defines things (imports, defs, classes, assignments)."""
    if not src.strip():
        return True
    from codocs.lang.python import _is_main_guard, _parse, _unwrap

    try:
        root = _parse(src).root
    except Exception:
        return False
    for st in root.named_children:
        node = _unwrap(st)
        if node.type in _IMPORT_SAFE or _is_main_guard(node):
            continue
        return False
    return True
