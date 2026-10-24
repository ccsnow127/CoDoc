"""CcRunner: builds and runs C/C++ tests with cc/c++ and measures line coverage with gcov.

one class, instantiated per language. every call works in its own temp dir under OUT/work/tmp
(objects, binaries, overlay copies, gcov output), so nothing is ever written into the
repository; programs run with cwd = the unit dir of the sandbox mirror (or of the repo with
--no-sandbox).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from codocs.golang.gotool import (
    MAX_BUILD_ERRORS,
    OUTPUT_CAP,
    OUTPUT_LIMIT,
    TAIL_CAP,
    CoverBlock,
    _Capture,
    _drain,
    _killpg,
    _Proc,
)
from codocs.lang import treesitter as ts
from codocs.lang.base import LangTestResult, Unit
from codocs.lang.cc import (
    CMD_BUILD,
    CMD_TEST,
    CcAdapter,
    case_names,
    defines_main,
    nearest,
    write_support,
)
from codocs.lang.registry import is_header, source_language

OUTER_SLACK = 5
BASE_FLAGS = ("-O0", "-g", "-fno-inline", "-w")
NOT_STARTED = "the C/C++ build timed out before any test started"
BUILD_SLACK = 60
MAX_CLOSURE_ROUNDS = 3
MAX_CLOSURE_FILES = 64
FARM_SKIP = frozenset({".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
                       ".venv", "venv", ".tox", "node_modules"})
# a verdict can follow a case's partial output line (printf("%d ", x)): searched, not matched
_PROTO_RE = re.compile(r"@@CODOCS (RUN|PASS|FAIL|SKIP|TIMEOUT)(?: (\S+))?(?: (.*))?$")
_MAC_UNDEF_RE = re.compile(r'^\s*"([^"]+)", referenced from:')
_GNU_UNDEF_RE = re.compile(r"undefined reference to [`'‘]([^'’]+)['’]")
_ERROR_LINE_RE = re.compile(r"^\S[^:]*:\d+(?::\d+)?: (?:fatal )?error")
_GCOV_LINE_RE = re.compile(r"^\s*([^:]+):\s*(\d+):")


def _parent(rel: str) -> str:
    p = PurePosixPath(rel).parent.as_posix()
    return "" if p == "." else p


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[-n:]


def _settings_list(settings: Any, name: str) -> list[str]:
    raw = getattr(settings, name, "") or ""
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


class PathMap:
    """maps tool paths (overlay copies, sandbox, repo, tools) back to repo rels."""

    def __init__(self, prefixes: dict[str, str]):
        pairs: dict[str, str] = {}
        for pre, rel in prefixes.items():
            for form in {str(pre), str(Path(pre).resolve())}:
                pairs[form.rstrip("/") + "/"] = (rel.rstrip("/") + "/") if rel else ""
        self._pairs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))
        self._re = re.compile("|".join(re.escape(k) for k, _ in self._pairs)) \
            if self._pairs else None
        self._lookup = dict(self._pairs)

    def __call__(self, text: str) -> str:
        if not text or self._re is None:
            return text
        return self._re.sub(lambda m: self._lookup[m.group(0)], text)

    def rel(self, path: str) -> str | None:
        """repo rel of an absolute tool path, None when it is under no registered prefix."""
        for pre, rel in self._pairs:
            if path.startswith(pre):
                return rel + path[len(pre):]
        return None


@dataclass
class _Obj:
    path: Path
    src: Path
    rel: str
    cpp: bool
    cover: bool = False


@dataclass
class _Run:
    tests: dict[str, str] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)
    tail: list[str] = field(default_factory=list)
    timed_out: bool = False
    returncode: int = 0
    build_errors: list[str] = field(default_factory=list)


class _BuildFailed(Exception):
    def __init__(self, errors: list[str], output: str, timed_out: bool = False):
        super().__init__("build failed")
        self.errors = errors
        self.output = output
        self.timed_out = timed_out


class CcRunner:
    """runs one unit's C/C++ test programs and generated suites (LangRunner semantics)."""

    def __init__(self, adapter: CcAdapter, *, repo_root: str | Path,
                 exec_root: str | Path | None = None, work_dir: str | Path | None = None,
                 tools_dir: str | Path | None = None, settings: Any = None, workers: int = 4,
                 timeout: int = 300, ensure_synced: Any = None):
        self.adapter = adapter
        self.lang = adapter.name
        self.settings = settings
        self.module_root = Path(repo_root).resolve()
        self.exec_root = Path(exec_root).resolve() if exec_root else self.module_root
        self.module = self.module_root.name
        base = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "codocs-cc"
        self.work_dir = base.resolve() if base.exists() else base
        tools = Path(tools_dir) if tools_dir else self.work_dir.parent / "tools"
        self.tools_dir = tools
        self.workers = max(1, int(workers))
        self.timeout = int(timeout)
        self._ensure_synced = ensure_synced
        self.cc = getattr(settings, "cc", "") or "cc"
        self.cxx = getattr(settings, "cxx", "") or "c++"
        self.gcov = getattr(settings, "gcov", "") or "gcov"
        self.cc_flags = _settings_list(settings, "cc_flags")
        self.ld_flags = _settings_list(settings, "ld_flags")
        self.test_cmd = getattr(settings, "test_cmd", "") or ""
        self.ar = getattr(settings, "ar", "") or shutil.which("ar") or ""
        self.build_cmd = getattr(settings, "build_cmd", "") or ""
        self._version: str | None = None
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop: Any = None
        self._main_lock: asyncio.Lock | None = None
        self._main_obj: Path | None = None

    @classmethod
    def from_context(cls, adapter: CcAdapter, ctx: Any) -> CcRunner:
        """a runner from a pipeline.RunContext (read duck-typed)."""
        settings = getattr(ctx, "settings", None)
        repo_root = getattr(ctx, "repo_root", None) or getattr(
            getattr(ctx, "repo_ctx", None), "root", None) or adapter.ctx.root
        work_dir = getattr(ctx, "work_dir", None)
        out_dir = getattr(ctx, "out_dir", None)
        if work_dir is None and out_dir is not None:
            work_dir = Path(out_dir) / "work" / "tmp"
        tools = getattr(ctx, "tools_dir", None)
        if tools is None and work_dir is not None:
            tools = Path(work_dir).parent / "tools"
        timeout = getattr(ctx, "timeout", None) or getattr(settings, "test_timeout", 0) \
            or getattr(settings, "go_timeout", 300)
        workers = getattr(ctx, "workers", None) or getattr(settings, "workers", 4)
        return cls(adapter, repo_root=repo_root, exec_root=getattr(ctx, "exec_root", None),
                   work_dir=work_dir, tools_dir=tools, settings=settings, workers=workers,
                   timeout=timeout, ensure_synced=getattr(ctx, "ensure_synced", None))

    # -- toolchain --------------------------------------------------------------------------

    @property
    def compiler(self) -> str:
        return self.cc if self.lang == "c" else self.cxx

    def _probe(self, args: list[str]) -> str:
        try:
            cp = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        if cp.returncode != 0:
            return ""
        out = (cp.stdout or cp.stderr).strip().splitlines()
        return out[0].strip() if out else ""

    def version(self) -> str:
        if self._version is None:
            cc = self._probe([self.compiler, "--version"])
            if not cc:
                self._version = ""
            else:
                gcov = self._probe([self.gcov, "--version"])
                self._version = cc + (f", gcov: {gcov}" if gcov else ", no gcov")
        return self._version

    def available(self) -> bool:
        return bool(self.version())

    # -- process execution ---------------------------------------------------------------------

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.workers)
            self._sem_loop = loop
            self._main_lock = asyncio.Lock()
        return self._sem

    async def _exec(self, args: list[str], *, cwd: Path, timeout: float,
                    env: dict[str, str] | None = None) -> _Proc:
        """run one command (stderr merged into stdout) in its own session; kill on timeout."""
        async with self._semaphore():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=str(cwd), env=env or self._tool_env(),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                return _Proc(127, f"{args[0]}: {e}\n", "", False)
            out = _Capture(OUTPUT_LIMIT, lambda: _killpg(proc))
            reader = asyncio.ensure_future(_drain(proc.stdout, out))
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
                reader.cancel()
                raise
            try:
                await asyncio.wait_for(reader, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                _killpg(proc)
                reader.cancel()
            rc = proc.returncode if proc.returncode is not None else -1
            return _Proc(rc, out.text(), "", timed_out, out.overflow)

    @staticmethod
    def _tool_env() -> dict[str, str]:
        """compilers, linker and gcov in the C locale: ASCII quotes in diagnostics (GCC with
        NLS prints ‘x’ under UTF-8 locales) and untranslated messages for the parsers."""
        env = dict(os.environ)
        env.update({"LC_ALL": "C", "LC_MESSAGES": "C", "LANG": "C"})
        return env

    def _env(self, tmp: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["TMPDIR"] = str(tmp)
        env.pop("CODOCS_TIMEOUT", None)
        env.pop("CODOCS_FAILFAST", None)
        env.pop("CODOCS_NOFORK", None)
        env.update(extra or {})
        return env

    async def _sync(self) -> None:
        fn = self._ensure_synced
        if fn is None:
            return
        res = fn()
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            await res

    def _call_dir(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="c", dir=self.work_dir)).resolve()

    def support_dir(self) -> Path:
        """OUT/work/tools/cc with codocs_test.h and codocs_main.c/.cpp (written on demand)."""
        d = write_support(Path(self.tools_dir) / "cc")
        return d.resolve()

    async def _main_object(self, deadline: float) -> Path:
        """codocs_main compiled once per runner (C or C++ build)."""
        self._semaphore()
        assert self._main_lock is not None
        async with self._main_lock:
            if self._main_obj is not None and self._main_obj.exists():
                return self._main_obj
            sup = self.support_dir()
            src = sup / ("codocs_main.c" if self.lang == "c" else "codocs_main.cpp")
            out_dir = sup / f"obj-{self.lang}"
            out_dir.mkdir(parents=True, exist_ok=True)
            final = out_dir / "codocs_main.o"
            tmp = out_dir / f".codocs_main.{os.getpid()}.o"
            p = await self._exec([self.compiler, "-c", str(src), "-o", str(tmp), *BASE_FLAGS,
                                  f"-I{sup}"], cwd=out_dir, timeout=self._left(deadline))
            if p.returncode != 0 or p.timed_out:
                raise _BuildFailed([ln for ln in p.stdout.splitlines() if ln.strip()][:20]
                                   or ["cannot compile codocs_main"], p.stdout, p.timed_out)
            os.replace(tmp, final)
            self._main_obj = final
            return final

    @staticmethod
    def _left(deadline: float) -> float:
        return max(deadline - asyncio.get_running_loop().time(), 0.5)

    # -- public ---------------------------------------------------------------------------------

    async def list_tests(self, unit_rel: str) -> list[str] | None:
        """the unit's existing tests: detected test programs (or cmd:test)."""
        try:
            unit = self.adapter.unit(unit_rel)
        except KeyError:
            return None
        if self.adapter.test_cmd_applies(unit):
            return [CMD_TEST]
        return sorted(unit.test_files)

    async def test(self, unit_rel: str, *, overlay: dict[str, str | None] | None = None,
                   run: list[str] | None = None, cover: bool = False, failfast: bool = False,
                   compile_only: bool = False, timeout: int | None = None) -> LangTestResult:
        if failfast and cover:
            raise ValueError("failfast is never combined with cover")
        t = int(timeout if timeout is not None else self.timeout)
        await self._sync()
        try:
            unit = self.adapter.unit(unit_rel)
        except KeyError:
            return LangTestResult(build_ok=False, build_errors=[
                f"{unit_rel or '.'}: no {self.adapter.display} unit"])
        call = _Call(self, unit, self._call_dir(), overlay or {})
        try:
            return await call.execute(run=run, cover=cover, failfast=failfast,
                                      compile_only=compile_only, t=t)
        finally:
            shutil.rmtree(call.dir, ignore_errors=True)


class _Call:
    """the state of one runner.test() call."""

    def __init__(self, runner: CcRunner, unit: Unit, call_dir: Path,
                 overlay: dict[str, str | None]):
        self.r = runner
        self.a = runner.adapter
        self.unit = unit
        self.dir = call_dir
        self.obj_dir = call_dir / "obj"
        self.bin_dir = call_dir / "bin"
        self.tmp = call_dir / "tmp"
        for d in (self.obj_dir, self.bin_dir, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        self.ov_root = call_dir / "ov"
        self.overlay: dict[str, str] = {}
        self.hidden: set[str] = set()
        for rel, content in overlay.items():
            rel = rel.strip("/")
            if content is None:
                self.hidden.add(rel)
            else:
                self.overlay[rel] = content
                self._write_overlay(rel, content)
        self.map = PathMap({str(self.ov_root): "", str(runner.exec_root): "",
                            str(runner.module_root): "", str(runner.tools_dir / "cc"): "_support",
                            str(self.obj_dir): "", str(self.bin_dir): ""})
        self.notes: list[str] = []
        self.skipped: dict[str, str] = {}        # library file -> its first compile error
        self.archived: set[str] = set()           # library files in the unit archive
        self._n = 0

    # -- files -------------------------------------------------------------------------

    def _write_overlay(self, rel: str, content: str) -> None:
        dst = self.ov_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        codec = "utf-8"
        if self.a.ctx is not None and (self.r.module_root / rel).exists():
            codec = self.a.ctx.codec(rel)
        dst.write_bytes(ts.encode(content, codec))

    def path(self, rel: str) -> Path:
        if rel in self.overlay:
            return self.ov_root / rel
        return self.r.exec_root / rel

    def text(self, rel: str) -> str:
        if rel in self.overlay:
            return self.overlay[rel]
        return self.a.read(rel)

    def unit_dir(self) -> Path:
        d = self.r.exec_root / self.unit.rel
        return d if d.is_dir() else self.tmp

    def _cpp(self, rel: str) -> bool:
        return source_language(rel) == "cpp"

    def _counter(self) -> int:
        self._n += 1
        return self._n

    def file_flags(self, rel: str) -> list[str]:
        flags, _ = self.a.file_flags(rel)
        out = []
        root = str(self.r.module_root)
        for f in flags:
            if root in f and str(self.r.exec_root) != root:
                f = f.replace(root, str(self.r.exec_root))
            out.append(f)
        return out

    # -- composition -------------------------------------------------------------------

    def sources(self) -> list[str]:
        """non-test sources of the unit after the overlay (headers excluded)."""
        out = [f for f in self.unit.files if not is_header(f) and f not in self.hidden]
        for rel, content in self.overlay.items():
            if rel in out or is_header(rel) or source_language(rel) not in ("c", "cpp"):
                continue
            if _parent(rel) != self.unit.rel:
                continue
            if not self.a.is_test_file(rel, content):
                out.append(rel)
        return sorted(out)

    def mains(self, sources: list[str]) -> set[str]:
        out = set()
        for f in sources:
            if f in self.overlay:
                if defines_main(self.overlay[f], self.a.lang_of(f)):
                    out.add(f)
            elif f in self.unit.extra.get("main_files", []):
                out.add(f)
        return out

    def programs(self) -> list[str]:
        out = [f for f in self.unit.test_files if f not in self.hidden]
        for rel, content in self.overlay.items():
            if rel not in out and self.a.is_program(rel, content):
                out.append(rel)
        return sorted(out)

    def suites(self) -> list[str]:
        return sorted(rel for rel, content in self.overlay.items()
                      if not is_header(rel) and self.a.is_suite(rel, content)
                      and not self.a.is_program(rel, content))

    # -- execution ---------------------------------------------------------------------

    async def execute(self, *, run: list[str] | None, cover: bool, failfast: bool,
                      compile_only: bool, t: int) -> LangTestResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + t + BUILD_SLACK
        try:
            if compile_only:
                return await self._compile_only(deadline)
            if self.a.test_cmd_applies(self.unit) and self.r.test_cmd and (
                    run is None and not self.suites() or run is not None and CMD_TEST in run):
                return await self._command(self.r.test_cmd, CMD_TEST, t)
            return await self._tests(run, cover, failfast, t, deadline)
        except _BuildFailed as e:
            errors = e.errors or [ln for ln in e.output.splitlines() if ln.strip()]
            if e.timed_out and not errors:
                errors = [NOT_STARTED]
            res = LangTestResult(build_ok=False, build_errors=errors[:MAX_BUILD_ERRORS],
                                 timed_out=e.timed_out,
                                 tail=_cap(self.map(e.output), TAIL_CAP))
            if e.timed_out:
                res.build_errors = [NOT_STARTED]
            return res

    def _errors(self, output: str) -> list[str]:
        lines = [self.map(ln.rstrip()) for ln in output.splitlines() if ln.strip()]
        errs = [ln for ln in lines if _ERROR_LINE_RE.match(ln)]
        if not errs:
            errs = [ln for ln in lines if not ln.startswith(("In file included", "      "))]
        return errs[:MAX_BUILD_ERRORS]

    async def compile(self, rel: str, out: Path, deadline: float, *, cover: bool,
                      extra: list[str] | None = None, src: Path | None = None) -> _Obj:
        src = src or self.path(rel)
        cpp = self._cpp(rel)
        args = [self.r.cxx if cpp else self.r.cc, "-c", str(src), "-o", str(out), *BASE_FLAGS,
                *self.file_flags(rel)]
        if rel in self.overlay:
            args += ["-iquote", str(self.r.exec_root / PurePosixPath(rel).parent)]
        args += list(extra or []) + self.r.cc_flags
        if cover:
            args.append("--coverage")
        p = await self.r._exec(args, cwd=self.obj_dir, timeout=self.r._left(deadline))
        if p.timed_out or p.returncode != 0:
            raise _BuildFailed([] if p.timed_out else self._errors(p.stdout), p.stdout,
                               p.timed_out)
        return _Obj(path=out, src=src, rel=rel, cpp=cpp, cover=cover)

    async def _compile_only(self, deadline: float) -> LangTestResult:
        if self.a.test_cmd_applies(self.unit) and self.r.build_cmd:
            res = await self._command(self.r.build_cmd, CMD_BUILD,
                                      int(self.r._left(deadline)))
            ok = res.tests.get(CMD_BUILD) == "pass"
            return LangTestResult(build_ok=ok, build_errors=[] if ok else self._errors(
                res.output.get(CMD_BUILD, "")) or ["build command failed"], tail=res.tail,
                timed_out=res.timed_out)
        jobs = []
        for rel in sorted(self.overlay):
            if is_header(rel) or source_language(rel) not in ("c", "cpp"):
                continue
            n = self._counter()
            out = self.obj_dir / f"co_{n}_{PurePosixPath(rel).name}.o"
            if self.a.is_suite(rel, self.overlay[rel]):
                jobs.append(self._compile_suite(rel, out, deadline, cover=False))
            else:
                jobs.append(self.compile(rel, out, deadline, cover=False))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        errors: list[str] = []
        timed_out = False
        tail = []
        for r in results:
            if isinstance(r, _BuildFailed):
                errors += r.errors or ["compile failed"]
                timed_out = timed_out or r.timed_out
                tail.append(r.output)
            elif isinstance(r, BaseException):
                raise r
        if errors or timed_out:
            return LangTestResult(build_ok=False, timed_out=timed_out,
                                  build_errors=[NOT_STARTED] if timed_out and not errors
                                  else errors[:MAX_BUILD_ERRORS],
                                  tail=_cap(self.map("".join(tail)), TAIL_CAP))
        return LangTestResult(build_ok=True)

    def suite_target(self, suite: str) -> str | None:
        return self.a.target_of_suite(self.unit, suite)

    async def _compile_suite(self, suite: str, out: Path, deadline: float, *,
                             cover: bool) -> _Obj:
        target = self.suite_target(suite)
        if target is not None and target not in self.hidden:
            tpath = self.path(target)
        else:
            tpath = self.dir / "codocs_no_target.h"
            tpath.write_text("/* no target */\n", encoding="utf-8")
        sup = self.r.support_dir()
        extra = [f"-I{sup}", "-iquote", str(self.r.exec_root / self.unit.rel),
                 f'-DCODOCS_TARGET="{tpath}"', "-Dmain=codocs_target_main"]
        if target is not None:
            extra += [f for f in self.file_flags(target) if f not in extra]
        obj = await self.compile(suite, out, deadline, cover=cover, extra=extra)
        obj.rel = suite
        return obj

    def _main_flag(self, rel: str) -> list[str]:
        """a library file that also defines main (a self-test under #ifdef, a tool) keeps its
        other definitions linkable: its main is renamed."""
        n = self._counter()
        return [f"-Dmain=codocs_unit_main_{n}", f"-Dwmain=codocs_unit_wmain_{n}"]

    async def _lib(self, rels: list[str], deadline: float, cover: bool,
                   mains: set[str] | None = None) -> dict[str, _Obj]:
        """the unit's library objects, best effort: a sibling that does not compile (a
        platform file, a header missing from the repo) is left out with a note; an overlaid
        file that does not compile is a build failure."""
        jobs = {}
        for rel in rels:
            n = self._counter()
            extra = self._main_flag(rel) if rel in (mains or set()) else []
            jobs[rel] = self.compile(rel, self.obj_dir / f"lib_{n}_{PurePosixPath(rel).name}.o",
                                     deadline, cover=cover, extra=extra)
        done = await asyncio.gather(*jobs.values(), return_exceptions=True)
        out: dict[str, _Obj] = {}
        errors: list[str] = []
        texts = []
        timed_out = False
        for rel, r in zip(jobs, done):
            if isinstance(r, _BuildFailed):
                timed_out = timed_out or r.timed_out
                if rel in self.overlay or r.timed_out:
                    errors += r.errors
                    texts.append(r.output)
                else:
                    first = (r.errors or ["does not compile"])[0]
                    self.skipped[rel] = first
                    note = f"{rel}: left out of the link (does not compile: {first[:200]})"
                    if note not in self.notes:
                        self.notes.append(note)
            elif isinstance(r, BaseException):
                raise r
            else:
                out[rel] = r
        if errors or timed_out:
            raise _BuildFailed(errors, "".join(texts), timed_out)
        return out

    async def _archive(self, objs: dict[str, _Obj], deadline: float) -> _Obj | None:
        """the library objects as a static archive: the linker takes only the members a program
        needs, and a program's own definitions (stubs, an #include of a .c) win."""
        if not objs or not self.r.ar:
            return None
        out = self.obj_dir / f"unit_{self._counter()}.a"
        p = await self.r._exec([self.r.ar, "rcs", str(out), *(str(o.path) for o in
                                                              objs.values())],
                               cwd=self.obj_dir, timeout=self.r._left(deadline))
        if p.returncode != 0 or p.timed_out or not out.is_file():
            return None
        return _Obj(path=out, src=out, rel="_unit.a", cpp=any(o.cpp for o in objs.values()))

    async def _tests(self, run: list[str] | None, cover: bool, failfast: bool, t: int,
                     deadline: float) -> LangTestResult:
        progs = self.programs()
        suites = self.suites()
        if run is not None:
            wanted = set(run)
            progs = [p for p in progs if p in wanted]
            cases = {s: [n.split("::", 1)[1] for n in run if n.startswith(s + "::")]
                     for s in suites}
            suites = [s for s in suites if cases[s]]
        else:
            cases = {s: [] for s in suites}
        if not progs and not suites:
            return LangTestResult(build_ok=True)
        sources = self.sources()
        mains = self.mains(sources)
        lib = list(sources)
        need: set[str] = set(lib) if progs else set()
        for s in suites:
            need |= set(lib) - {self.suite_target(s)}
        objs = await self._lib(sorted(need), deadline, cover, mains)
        self.archived = set(objs)
        cov_objs: list[_Obj] = list(objs.values())
        res = _Run()
        stop = False
        archives: dict[str | None, _Obj | None] = {}

        async def linkable(skip: str | None) -> dict[str, _Obj]:
            """the library for a program/suite: one archive (else the loose objects)."""
            if skip not in archives:
                part = {k: v for k, v in objs.items() if k != skip}
                archives[skip] = await self._archive(part, deadline)
            a = archives[skip]
            return {"_unit.a": a} if a is not None else \
                {k: v for k, v in objs.items() if k != skip}

        prog_lib = await linkable(None) if progs else {}
        for prog in progs:
            await self._program(prog, prog_lib, res, cover, t, deadline)
            if failfast and res.tests.get(prog) == "fail":
                stop = True
                break
        for s in suites if not stop else []:
            target = self.suite_target(s)
            link = await linkable(target)
            sobj = await self._suite(s, cases[s], link, res, cover, failfast, t, deadline)
            if sobj is not None:
                cov_objs.append(sobj)
            if failfast and any(v == "fail" for k, v in res.tests.items()
                                if k.startswith(s + "::")):
                break
        if res.build_errors:
            return LangTestResult(build_ok=False, build_errors=res.build_errors[:MAX_BUILD_ERRORS],
                                  timed_out=res.timed_out,
                                  tail=_cap("".join(res.tail), TAIL_CAP))
        out = LangTestResult(build_ok=True, tests=res.tests, output=res.output,
                             timed_out=res.timed_out, returncode=res.returncode,
                             tail=_cap("".join(res.tail) + "".join(
                                 n + "\n" for n in self.notes), TAIL_CAP))
        if cover:
            out.coverage, out.coverage_error = await self._coverage(cov_objs, sources)
        return out

    # -- linking ---------------------------------------------------------------------------

    @staticmethod
    def _undefined(output: str) -> list[str]:
        names: list[str] = []
        for line in output.splitlines():
            m = _MAC_UNDEF_RE.match(line)
            name = None
            if m:
                name = m.group(1)
                if "::" not in name and "(" not in name and name.startswith("_"):
                    name = name[1:]
            else:
                g = _GNU_UNDEF_RE.search(line)
                if g:
                    name = g.group(1)
            if name and name not in names:
                names.append(name)
        return names

    @staticmethod
    def _keys(symbol: str) -> list[str]:
        s = symbol.split("(", 1)[0].strip()
        s = re.sub(r"<[^<>]*>", "", s)
        parts = [x for x in s.split("::") if x]
        if not parts:
            return []
        return ["::".join(parts[i:]) for i in range(len(parts))]

    async def link(self, name: str, objs: list[_Obj], near: str, deadline: float, *,
                   cover: bool) -> tuple[Path | None, str]:
        """link objs (+ the symbol closure); (binary, output) or (None, errors output)."""
        out = self.bin_dir / f"{self._counter()}_{PurePosixPath(name).name}.bin"
        used = {o.rel for o in objs} | self.archived
        extra: list[_Obj] = []
        index = None
        output = ""

        def ordered(xs: list[_Obj]) -> list[_Obj]:
            return [o for o in xs if o.path.suffix != ".a"] + \
                [o for o in xs if o.path.suffix == ".a"]      # archives after the objects

        for round_ in range(MAX_CLOSURE_ROUNDS + 1):
            allobjs = ordered(objs + extra)
            cpp = any(o.cpp for o in allobjs)
            args = [self.r.cxx if cpp else self.r.cc, *(str(o.path) for o in allobjs), "-o",
                    str(out), *self.r.ld_flags, "-lm"]
            if cover:
                args.append("--coverage")
            p = await self.r._exec(args, cwd=self.bin_dir, timeout=self.r._left(deadline))
            if p.timed_out:
                raise _BuildFailed([], p.stdout, True)
            if p.returncode == 0:
                return out, p.stdout
            output = p.stdout
            undef = self._undefined(output)
            if not undef or round_ == MAX_CLOSURE_ROUNDS or len(extra) >= MAX_CLOSURE_FILES:
                break
            if index is None:
                index = self.a.symbols()
            added = False
            for sym in undef:
                for key in self._keys(sym):
                    cands = [r for r in index.get(key, []) if r not in used
                             and r not in self.hidden and r not in self.skipped]
                    if not cands:
                        continue
                    pick = nearest(cands, near)[0]
                    used.add(pick)
                    n = self._counter()
                    flags = self._main_flag(pick) if self._is_main_file(pick) else []
                    with contextlib.suppress(_BuildFailed):
                        extra.append(await self.compile(
                            pick, self.obj_dir / f"ext_{n}_{PurePosixPath(pick).name}.o",
                            deadline, cover=False, extra=flags))
                        added = True
                    break
                if len(extra) >= MAX_CLOSURE_FILES:
                    break
            if not added:
                break
        undef = self._undefined(output)
        if undef:
            allobjs = ordered(objs + extra)
            cpp = any(o.cpp for o in allobjs)
            flag = "-Wl,-undefined,dynamic_lookup" if os.uname().sysname == "Darwin" \
                else "-Wl,--unresolved-symbols=ignore-all"
            args = [self.r.cxx if cpp else self.r.cc, *(str(o.path) for o in allobjs), "-o",
                    str(out), *self.r.ld_flags, "-lm", flag]
            if cover:
                args.append("--coverage")
            p = await self.r._exec(args, cwd=self.bin_dir, timeout=self.r._left(deadline))
            if p.returncode == 0 and not p.timed_out:
                note = f"{name}: unresolved: {', '.join(undef[:20])}"
                if note not in self.notes:
                    self.notes.append(note)
                return out, p.stdout
            output = p.stdout
        return None, output

    def _is_main_file(self, rel: str) -> bool:
        try:
            u = self.a.unit(_parent(rel))
        except KeyError:
            return defines_main(self.a.read(rel), self.a.lang_of(rel))
        return rel in u.extra.get("main_files", [])

    def _build_failed(self, res: _Run, name: str, output: str) -> None:
        """a program/suite that does not build: a build failure with an overlay, else a fail."""
        errs = self._errors(output) or [f"{name}: build failed"]
        if self.overlay:
            res.build_errors += [e for e in errs if e not in res.build_errors]
            res.tail.append(self.map(output))
        else:
            res.tests[name] = "fail"
            res.output[name] = _cap(self.map(output), OUTPUT_CAP)
            res.returncode = res.returncode or 1

    # -- programs and suites ---------------------------------------------------------------

    async def _program(self, prog: str, objs: dict[str, _Obj], res: _Run, cover: bool, t: int,
                       deadline: float) -> None:
        n = self._counter()
        try:
            pobj = await self.compile(prog, self.obj_dir / f"prog_{n}_{PurePosixPath(prog).name}.o",
                                      deadline, cover=False)
        except _BuildFailed as e:
            if e.timed_out:
                raise
            self._build_failed(res, prog, e.output)
            return
        binary, output = await self.link(prog, [pobj, *objs.values()], prog, deadline,
                                         cover=cover)
        if binary is None:
            self._build_failed(res, prog, output)
            return
        p = await self.r._exec([str(binary)], cwd=self.unit_dir(), timeout=max(t, 1),
                               env=self.r._env(self.tmp))
        text = self.map(p.stdout)
        if p.timed_out:
            res.timed_out = True
            text += f"\n{prog}: timed out after {t}s\n"
        verdict = "pass" if p.returncode == 0 and not p.timed_out else "fail"
        if verdict == "fail" and not p.timed_out and p.returncode < 0:
            text += f"\n{prog}: killed by signal {-p.returncode}\n"
        elif verdict == "fail" and not p.timed_out:
            text += f"\n{prog}: exit status {p.returncode}\n"
        res.tests[prog] = verdict
        res.output[prog] = _cap(text, OUTPUT_CAP)
        res.tail.append(text)
        if verdict == "fail":
            res.returncode = res.returncode or (p.returncode if p.returncode > 0 else 1)

    async def _suite(self, suite: str, cases: list[str], objs: dict[str, _Obj], res: _Run,
                     cover: bool, failfast: bool, t: int, deadline: float) -> _Obj | None:
        n = self._counter()
        try:
            sobj = await self._compile_suite(
                suite, self.obj_dir / f"suite_{n}_{PurePosixPath(suite).name}.o", deadline,
                cover=cover)
            main = await self.r._main_object(deadline)
        except _BuildFailed as e:
            if e.timed_out:
                raise
            res.build_errors += [x for x in (e.errors or self._errors(e.output))
                                 if x not in res.build_errors]
            res.tail.append(self.map(e.output))
            return None
        mobj = _Obj(path=main, src=main, rel="_support/codocs_main", cpp=self.r.lang == "cpp")
        binary, output = await self.link(suite, [sobj, mobj, *objs.values()], suite, deadline,
                                         cover=cover)
        if binary is None:
            res.build_errors += [x for x in self._errors(output) if x not in res.build_errors]
            res.tail.append(self.map(output))
            return None
        env = {"CODOCS_TIMEOUT": str(max(t, 1))}
        if failfast:
            env["CODOCS_FAILFAST"] = "1"
        p = await self.r._exec([str(binary), *cases], cwd=self.unit_dir(),
                               timeout=max(t, 1) + OUTER_SLACK, env=self.r._env(self.tmp, env))
        requested = cases or [name for name, _ in case_names(self.overlay.get(suite, ""))]
        self._parse(suite, p, requested, res)
        return sobj

    def _parse(self, suite: str, p: _Proc, requested: list[str], res: _Run) -> None:
        """the @@CODOCS protocol of one suite run -> folded verdicts with the crash rules."""
        verdicts: dict[str, str] = {}
        started: list[str] = []
        outputs: dict[str, list[str]] = {}
        loose: list[str] = []
        running: str | None = None
        timed_out = p.timed_out
        for raw in p.stdout.splitlines(keepends=True):
            line = raw.rstrip("\n")
            m = _PROTO_RE.search(line)
            if m and m.start():
                head = line[:m.start()]
                (outputs.setdefault(running, []) if running else loose).append(head + "\n")
            if not m:
                (outputs.setdefault(running, []) if running else loose).append(raw)
                continue
            kind, name, msg = m.group(1), m.group(2) or "", m.group(3) or ""
            if kind == "TIMEOUT":
                timed_out = True
                continue
            if kind == "RUN":
                if name not in started:
                    started.append(name)
                outputs.setdefault(name, [])
                running = name
                continue
            verdicts[name] = kind.lower()
            if name not in started:
                started.append(name)
            if kind != "PASS" and msg and msg not in "".join(outputs.get(name, [])):
                outputs.setdefault(name, []).append(msg + "\n")
            running = None
        died = timed_out or p.returncode not in (0, 1)
        explicit_fail = any(v == "fail" for v in verdicts.values())
        prefix = suite + "::"
        for name in started:
            body = "".join(outputs.get(name, []))
            if name in verdicts:
                v = verdicts[name]
            elif name == running and not explicit_fail:
                v = "fail"
                body += "".join(loose) + ("\ntimed out\n" if timed_out else "")
            else:
                v = "interrupted"
            res.tests[prefix + name] = v
            res.output[prefix + name] = _cap(self.map(body), OUTPUT_CAP)
        if died:
            for name in requested:
                if prefix + name not in res.tests:
                    res.tests[prefix + name] = "interrupted"
                    res.output.setdefault(prefix + name, "")
        res.timed_out = res.timed_out or timed_out
        if p.returncode != 0:
            res.returncode = res.returncode or (p.returncode if p.returncode > 0 else 1)
        res.tail.append(self.map(p.stdout))

    # -- configured commands -------------------------------------------------------------------

    def _farm(self, root_rel: str) -> Path:
        """a symlink farm of the --cc-root tree with the overlay applied."""
        src = self.r.exec_root / root_rel if root_rel else self.r.exec_root
        dst = self.dir / "farm"
        for dirpath, dirnames, filenames in os.walk(src):
            d = Path(dirpath)
            rel = d.relative_to(src)
            dirnames[:] = [n for n in dirnames if n not in FARM_SKIP
                           and (d / n).resolve() != self.r.work_dir.parent.parent.resolve()]
            (dst / rel).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                with contextlib.suppress(OSError):
                    os.symlink(d / name, dst / rel / name)
        base = root_rel + "/" if root_rel else ""
        for rel in self.hidden:
            if rel.startswith(base):
                with contextlib.suppress(OSError):
                    (dst / rel[len(base):]).unlink()
        for rel, content in self.overlay.items():
            if rel.startswith(base):
                p = dst / rel[len(base):]
                p.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    p.unlink()
                p.write_bytes((self.ov_root / rel).read_bytes())
        return dst

    async def _command(self, cmd: str, name: str, t: int) -> LangTestResult:
        from codocs.lang.cc import _cc_root_rel

        root_rel = _cc_root_rel(self.a.ctx) if self.a.ctx is not None else ""
        farm = self._farm(root_rel or "")
        text = cmd.replace("{root}", str(farm))
        p = await self.r._exec(["sh", "-c", text], cwd=farm, timeout=max(t, 1),
                               env=self.r._env(self.tmp))
        out = PathMap({str(farm): root_rel or ""})(self.map(p.stdout))
        verdict = "pass" if p.returncode == 0 and not p.timed_out else "fail"
        return LangTestResult(build_ok=True, tests={name: verdict},
                              output={name: _cap(out, OUTPUT_CAP)}, timed_out=p.timed_out,
                              returncode=p.returncode, tail=_cap(out, TAIL_CAP))

    # -- coverage ----------------------------------------------------------------------------

    async def _coverage(self, objs: list[_Obj], sources: list[str]
                        ) -> tuple[list[CoverBlock] | None, str]:
        if shutil.which(self.r.gcov) is None and not Path(self.r.gcov).is_file():
            return None, f"gcov not found: {self.r.gcov}"
        wanted = set(sources)
        counts: dict[tuple[str, int], int] = {}
        files_seen: set[str] = set()
        errors: list[str] = []
        root = self.dir / "gcov"
        for i, o in enumerate(o for o in objs if o.cover):
            d = root / str(i)
            d.mkdir(parents=True, exist_ok=True)
            # no -p: it encodes the whole path into one file name, which overflows NAME_MAX
            # for deep work dirs; every object has its own output dir anyway
            p = await self.r._exec([self.r.gcov, "-o", str(o.path), str(o.src)], cwd=d,
                                   timeout=120)
            if p.returncode != 0:
                errors.append(p.stdout.strip().splitlines()[-1] if p.stdout.strip()
                              else f"gcov exit {p.returncode}")
            outs = sorted(d.glob("*.gcov"))
            if not outs:
                why = [ln.strip() for ln in p.stdout.splitlines()
                       if re.search(r"(?i)cannot|error|too long|failed", ln)]
                errors.append(f"gcov wrote no report for {o.rel}" +
                              (f" ({why[0][:160]})" if why else ""))
            for g in outs:
                rel, lines = self._parse_gcov(g)
                if rel is None or rel not in wanted:
                    continue
                files_seen.add(rel)
                for ln, c in lines.items():
                    counts[(rel, ln)] = counts.get((rel, ln), 0) + c
        if not counts and any(o.cover for o in objs) and errors:
            return None, "gcov failed: " + errors[0][:200]
        blocks = [CoverBlock(file=rel, start_line=ln, start_col=1, end_line=ln, end_col=1,
                             stmts=1, count=c) for (rel, ln), c in sorted(counts.items())]
        return blocks, ""

    def _parse_gcov(self, path: Path) -> tuple[str | None, dict[int, int]]:
        """(rel of the Source: header, line -> count) of a plain-text .gcov file."""
        rel = None
        lines: dict[int, int] = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, lines
        for raw in text.splitlines():
            m = _GCOV_LINE_RE.match(raw)
            if not m:
                continue
            count, ln = m.group(1).strip(), int(m.group(2))
            if ln == 0:
                rest = raw[m.end():]
                if rest.startswith("Source:"):
                    src = rest[len("Source:"):].strip()
                    rel = self.map.rel(src) if os.path.isabs(src) else self.map.rel(
                        str((self.obj_dir / src).resolve()))
                continue
            if count == "-":
                continue
            if count.startswith(("#####", "=====")):
                c = 0
            else:
                c = _count(count)
                if c is None:
                    continue
            lines[ln] = max(lines.get(ln, 0), c) if ln in lines else c
        return rel, lines


def _count(text: str) -> int | None:
    t = text.rstrip("*").strip()
    mult = 1
    if t and t[-1] in "kKMG":
        mult = {"k": 1000, "K": 1000, "M": 1000000, "G": 1000000000}[t[-1]]
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


__all__ = ["CcRunner", "PathMap", "OUTER_SLACK", "NOT_STARTED"]
