"""go toolchain driver: go list, go test -json -overlay, build-error and coverage parsing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

TIMEOUT_SLACK = 60
OUTPUT_CAP = 4000
TAIL_CAP = 3000
MAX_BUILD_ERRORS = 50
OUTPUT_LIMIT = 64 * 1024 * 1024
_KEEP_TAIL = 64 * 1024
NOT_STARTED = "go test timed out before any test started (the build or package init did not finish)"
OUTPUT_EXCEEDED = "go test output exceeded the {} MB limit; the run was killed"
_SKIP_SYNC = frozenset({".git", ".hg", ".svn"})
# mirroring skips files above this size (prebuilt service binaries, archives) unless they are
# libraries or sources a build may need
SYNC_MAX_BYTES = 32 * 1024 * 1024
_SYNC_KEEP_SUFFIXES = (".go", ".so", ".a", ".h", ".c", ".s", ".syso", ".mod", ".sum")

_MODULE_RE = re.compile(r"^module\s+\"?([^\s\"]+)\"?", re.MULTILINE)
_BUILD_FAIL_RE = re.compile(r"\[(?:build|setup) failed\]")
_COVER_RE = re.compile(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+) (\d+) (\d+)$")
_COMPILER_RE = re.compile(r"^\S+\.(?:go|c|h|s):\d+(?::\d+)?: ")


@dataclass
class CoverBlock:
    file: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    stmts: int
    count: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CoverBlock:
        return cls(**{k: int(d[k]) if k != "file" else d[k] for k in cls.__dataclass_fields__})


@dataclass
class GoTestResult:
    build_ok: bool
    build_errors: list[str] = field(default_factory=list)
    tests: dict[str, str] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)
    coverage: list[CoverBlock] | None = None
    timed_out: bool = False
    returncode: int = 0
    tail: str = ""

    @property
    def passed(self) -> set[str]:
        return {t for t, v in self.tests.items() if v == "pass"}

    @property
    def failed(self) -> set[str]:
        return {t for t, v in self.tests.items() if v == "fail"}

    @property
    def interrupted(self) -> set[str]:
        """tests cut short by another test crashing the binary (no verdict of their own)."""
        return {t for t, v in self.tests.items() if v == "interrupted"}

    def to_dict(self) -> dict:
        return {
            "build_ok": self.build_ok,
            "build_errors": list(self.build_errors),
            "tests": dict(self.tests),
            "output": dict(self.output),
            "coverage": None if self.coverage is None else [b.to_dict() for b in self.coverage],
            "timed_out": self.timed_out,
            "returncode": self.returncode,
            "tail": self.tail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GoTestResult:
        cov = d.get("coverage")
        return cls(
            build_ok=bool(d.get("build_ok", False)),
            build_errors=list(d.get("build_errors") or []),
            tests=dict(d.get("tests") or {}),
            output=dict(d.get("output") or {}),
            coverage=None if cov is None else [CoverBlock.from_dict(b) for b in cov],
            timed_out=bool(d.get("timed_out", False)),
            returncode=int(d.get("returncode", 0)),
            tail=d.get("tail", ""),
        )


@dataclass
class GoListPackage:
    rel: str
    import_path: str
    name: str
    go_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    xtest_files: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> GoListPackage:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# -- pure parsers -------------------------------------------------------------


def _json_stream(text: str) -> list[dict]:
    """decode a stream of concatenated json objects (go list -json)."""
    dec = json.JSONDecoder()
    out: list[dict] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            nl = text.find("\n", i)
            if nl < 0:
                break
            i = nl + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _path_mapper(overlay_dirs: dict[str, str]):
    subs = []
    for tmp, rel in overlay_dirs.items():
        name = os.path.basename(os.path.normpath(tmp))
        if not name:
            continue
        pat = re.compile(r"(?:[^\s:\"'()]*/)?" + re.escape(name) + "/")
        subs.append((pat, f"{rel}/" if rel else ""))

    def remap(s: str) -> str:
        for pat, repl in subs:
            s = pat.sub(lambda _m, r=repl: r, s)
        return s

    return remap


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[-n:]


def _error_lines(lines: list[str], remap) -> list[str]:
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.startswith("# ") or line.startswith("go: downloading"):
            continue
        line = remap(line)
        if line.startswith("./"):
            line = line[2:]
        out.append(line)
    return out[:MAX_BUILD_ERRORS]


def parse_test_json(stdout: str, stderr: str, overlay_dirs: dict[str, str]) -> GoTestResult:
    """parse `go test -json` output (go 1.23 and >=1.24 build-failure styles)."""
    return _parse_test_json(stdout, stderr, overlay_dirs)[0]


def _parse_test_json(stdout: str, stderr: str, overlay_dirs: dict[str, str]
                     ) -> tuple[GoTestResult, bool]:
    """the result, and whether the test run started (any event past the build)."""
    remap = _path_mapper(overlay_dirs)
    build_failed = False
    build_lines: list[str] = []
    verdicts: dict[str, str] = {}
    started: list[str] = []
    state: dict[str, str] = {}
    outputs: dict[str, list[str]] = {}
    pkg_output: list[str] = []
    text: list[str] = []
    saw_event = False

    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            ev = None
        if not isinstance(ev, dict):
            pkg_output.append(line + "\n")
            text.append(line + "\n")
            continue
        action = ev.get("Action", "")
        out = ev.get("Output") or ""
        if out:
            text.append(out)
        if action == "build-output":
            build_lines.extend(out.splitlines())
            continue
        if action == "build-fail":
            build_failed = True
            continue
        saw_event = True
        test = ev.get("Test") or ""
        top = test.split("/", 1)[0]
        if action == "output":
            if top:
                outputs.setdefault(top, []).append(out)
            else:
                pkg_output.append(out)
                if _BUILD_FAIL_RE.search(out):
                    build_failed = True
        elif action == "run" and top:
            if top not in started:
                started.append(top)
            outputs.setdefault(top, [])
            if test == top:
                state[top] = "running"
        elif action in ("pause", "cont") and top and test == top:
            state[top] = "paused" if action == "pause" else "running"
        elif action in ("pass", "fail", "skip"):
            if test and test == top:
                verdicts[top] = action
                if top not in started:
                    started.append(top)
            elif not test and action == "fail" and ev.get("FailedBuild"):
                build_failed = True

    if stderr.strip():
        text.append(stderr if stderr.endswith("\n") else stderr + "\n")
    if not saw_event and stderr.strip() and not verdicts:
        build_failed = True
    tail = remap(_cap("".join(text), TAIL_CAP))

    if build_failed:
        errs = _error_lines(build_lines, remap) or _error_lines(stderr.splitlines(), remap)
        if not errs:
            errs = _error_lines([ln for ln in pkg_output if not _BUILD_FAIL_RE.search(ln)], remap)
        return GoTestResult(build_ok=False, build_errors=errs, tail=tail), saw_event

    # a test without a verdict either crashed the binary itself (it was running and no other
    # test reported a failure: log.Fatal, os.Exit, -timeout) or was cut short by a test that
    # did fail (paused or running in parallel): the latter are "interrupted", not failures
    explicit_fail = any(v == "fail" for v in verdicts.values())
    pkg_text = "".join(pkg_output)
    tests: dict[str, str] = {}
    output: dict[str, str] = {}
    for name in started:
        body = "".join(outputs.get(name, []))
        if name in verdicts:
            verdict = verdicts[name]
        elif state.get(name, "running") == "running" and not explicit_fail:
            verdict = "fail"
            body += pkg_text
        else:
            verdict = "interrupted"
        tests[name] = verdict
        output[name] = remap(_cap(body, OUTPUT_CAP))
    return GoTestResult(build_ok=True, tests=tests, output=output, tail=tail), saw_event


def parse_cover_profile(text: str, module: str) -> list[CoverBlock]:
    """parse a go cover profile; file names become module-relative paths."""
    prefix = module.rstrip("/") + "/" if module else ""
    blocks: list[CoverBlock] = []
    for line in text.splitlines():
        m = _COVER_RE.match(line.strip())
        if not m:
            continue
        name = m.group(1)
        if prefix and name.startswith(prefix):
            name = name[len(prefix):]
        sl, sc, el, ec, st, ct = (int(g) for g in m.groups()[1:])
        blocks.append(CoverBlock(name, sl, sc, el, ec, st, ct))
    return blocks


def file_coverage(blocks: list[CoverBlock], rel_file: str,
                  src: str | None = None) -> tuple[float | None, list[int]]:
    """statement coverage of one file and its sorted uncovered lines.

    with src, a block that starts at the end of a line or on its trailing '{' (go 1.23 opens a function's entry
    block right after the '{' of its header) does not claim that line, so uncovered lines
    match across go versions.
    """
    src_lines = src.splitlines() if src is not None else None
    merged: dict[tuple[int, int, int, int], list[int]] = {}
    for b in blocks:
        if b.file != rel_file:
            continue
        key = (b.start_line, b.start_col, b.end_line, b.end_col)
        if key in merged:
            merged[key][1] = max(merged[key][1], b.count)
        else:
            merged[key] = [b.stmts, b.count]
    total = sum(st for st, _ in merged.values())
    if total == 0:
        return None, []
    covered = sum(st for st, ct in merged.values() if ct > 0)
    hit: set[int] = set()
    miss: set[int] = set()
    for (sl, sc, el, _), (st, ct) in merged.items():
        if (src_lines is not None and el > sl and 0 < sl <= len(src_lines)
                and src_lines[sl - 1][sc - 1:].strip() in ("", "{")):
            sl += 1
        lines = range(sl, el + 1)
        if ct > 0:
            hit.update(lines)
        elif st > 0:
            miss.update(lines)
    return covered / total, sorted(miss - hit)


# -- runner -------------------------------------------------------------------


def _read_module(root: Path) -> str:
    try:
        m = _MODULE_RE.search((root / "go.mod").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def _killpg(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


class _Capture:
    """bounded output buffer: the first `limit` bytes plus a rolling tail."""

    def __init__(self, limit: int, on_overflow=None):
        self.limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.overflow = False
        self.on_overflow = on_overflow

    def add(self, chunk: bytes) -> None:
        if not self.overflow:
            room = self.limit - len(self.head)
            if len(chunk) <= room:
                self.head.extend(chunk)
                return
            self.head.extend(chunk[:max(room, 0)])
            chunk = chunk[max(room, 0):]
            self.overflow = True
            if self.on_overflow is not None:
                self.on_overflow()
        self.tail.extend(chunk)
        if len(self.tail) > 2 * _KEEP_TAIL:
            del self.tail[:-_KEEP_TAIL]

    def text(self) -> str:
        data = bytes(self.head)
        if self.overflow:
            data += b"\n" + bytes(self.tail[-_KEEP_TAIL:])
        return data.decode("utf-8", errors="replace")


async def _drain(stream: asyncio.StreamReader | None, buf: _Capture) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        buf.add(chunk)


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    overflow: bool = False


def _sync_tree(src: Path, dst: Path, exclude: set[Path]) -> None:
    """mirror src into dst: copy files whose size or mtime differ, delete extras."""
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        out = dst / d.relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        wanted: set[str] = set()
        subdirs = []
        for name in dirnames:
            p = d / name
            if name in _SKIP_SYNC or p in exclude:
                continue
            wanted.add(name)
            if p.is_symlink():
                filenames.append(name)
            else:
                subdirs.append(name)
        dirnames[:] = subdirs
        for name in filenames:
            p, q = d / name, out / name
            wanted.add(name)
            try:
                if p.is_symlink():
                    link = os.readlink(p)
                    if not (q.is_symlink() and os.readlink(q) == link):
                        _remove(q)
                        os.symlink(link, q)
                    continue
                st = p.stat()
                if st.st_size > SYNC_MAX_BYTES and not name.endswith(_SYNC_KEEP_SUFFIXES) \
                        and ".so." not in name:
                    wanted.discard(name)
                    continue
                if q.is_symlink() or q.is_dir():
                    _remove(q)
                elif q.exists():
                    qs = q.stat()
                    if qs.st_size == st.st_size and qs.st_mtime_ns == st.st_mtime_ns:
                        continue
                shutil.copy2(p, q)
            except OSError:
                continue
        for extra in os.listdir(out):
            if extra not in wanted:
                _remove(out / extra)


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            p.unlink()


class GoRunner:
    """runs the go tool against one module, never writing into it."""

    def __init__(self, module_root: str | Path, go: str = "go", workers: int = 8,
                 timeout: int = 300, work_dir: str | Path | None = None,
                 sandbox: str | Path | None = None,
                 sandbox_exclude: list[str | Path] | None = None,
                 env: dict[str, str] | None = None, flags: list[str] | None = None):
        self.extra_env = dict(env or {})
        self.flags = list(flags or [])
        self.module_root = Path(module_root).resolve()
        self.go = go
        self.workers = max(1, int(workers))
        self.timeout = int(timeout)
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "codocs"
        self.sandbox = Path(sandbox).resolve() if sandbox else None
        self.exec_root = self.sandbox or self.module_root
        self._exclude = {Path(p).resolve() for p in sandbox_exclude or []}
        if self.sandbox is not None:
            self._exclude |= {self.sandbox, self.work_dir.resolve()}
        self.module = _read_module(self.module_root)
        self._version: str | None = None
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop = None
        self._synced = self.sandbox is None
        self._sync_lock: asyncio.Lock | None = None

    # -- helpers ----------------------------------------------------------

    def _env(self, tmp: Path | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["GOTOOLCHAIN"] = "local"
        root = str(self.exec_root)
        for key, val in self.extra_env.items():
            env[key] = val.replace("{root}", root)
        env["PWD"] = root
        if tmp is not None:
            env["GOTMPDIR"] = str(tmp)
            env["TMPDIR"] = str(tmp)
        return env

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.workers)
            self._sem_loop = loop
            self._sync_lock = asyncio.Lock()
        return self._sem

    def sync_sandbox(self) -> None:
        """mirror the module into the sandbox (go runs there, so tests never write into it)."""
        if self.sandbox is not None:
            _sync_tree(self.module_root, self.sandbox, self._exclude)
        self._synced = True

    async def _prepare(self) -> None:
        if self._synced:
            return
        self._semaphore()
        assert self._sync_lock is not None
        async with self._sync_lock:
            if not self._synced:
                await asyncio.to_thread(self.sync_sandbox)

    async def _exec(self, args: list[str], timeout: float,
                    env: dict[str, str] | None = None) -> _Proc:
        """run one go command in its own process group; kill the group on timeout."""
        await self._prepare()
        async with self._semaphore():
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(self.exec_root), env=env or self._env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
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

    # -- public -----------------------------------------------------------

    def available(self) -> bool:
        return bool(self.version())

    def version(self) -> str:
        if self._version is None:
            try:
                env = self._env()
                env["PWD"] = str(self.module_root)
                cp = subprocess.run(
                    [self.go, "version"], cwd=str(self.module_root), env=env,
                    capture_output=True, text=True, timeout=60,
                )
                self._version = cp.stdout.strip() if cp.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                self._version = ""
        return self._version

    def import_path(self, pkg_rel: str) -> str:
        rel = pkg_rel.strip("/")
        if not rel or rel == ".":
            return self.module
        return f"{self.module}/{rel}" if self.module else rel

    def _rel_dir(self, d: dict) -> str | None:
        dir_ = d.get("Dir")
        if dir_:
            p = Path(dir_).resolve()
            for root in (self.exec_root, self.module_root):
                try:
                    s = p.relative_to(root).as_posix()
                except ValueError:
                    continue
                return "" if s == "." else s
            return None
        ip = d.get("ImportPath", "")
        if self.module and ip == self.module:
            return ""
        if self.module and ip.startswith(self.module + "/"):
            return ip[len(self.module) + 1:]
        return None

    def _call_dir(self, prefix: str) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=self.work_dir))

    async def list_packages(self, pattern: str = "./...") -> dict[str, GoListPackage]:
        """packages of the pattern; raises RuntimeError when go list fails or times out."""
        args = [self.go, "list", "-e", *self.flags,
                "-json=ImportPath,Dir,Name,GoFiles,CgoFiles,TestGoFiles,XTestGoFiles,Error",
                pattern]
        call_dir = self._call_dir("l")
        try:
            p = await self._exec(args, self.timeout + TIMEOUT_SLACK, self._env(call_dir))
        finally:
            shutil.rmtree(call_dir, ignore_errors=True)
        if p.timed_out or p.overflow:
            raise RuntimeError(f"go list {pattern} timed out after "
                               f"{self.timeout + TIMEOUT_SLACK}s")
        pkgs: dict[str, GoListPackage] = {}
        for d in _json_stream(p.stdout):
            rel = self._rel_dir(d)
            if rel is None:
                continue

            def paths(key: str, rel=rel, d=d) -> list[str]:
                return [f"{rel}/{f}" if rel else f for f in d.get(key) or []]

            err = d.get("Error") or ""
            if isinstance(err, dict):
                err = err.get("Err", "") or json.dumps(err)
            pkgs[rel] = GoListPackage(
                rel=rel, import_path=d.get("ImportPath", ""), name=d.get("Name", ""),
                go_files=paths("GoFiles") + paths("CgoFiles"),
                test_files=paths("TestGoFiles"), xtest_files=paths("XTestGoFiles"),
                error=str(err),
            )
        if not pkgs and p.returncode != 0:
            msg = " ".join(p.stderr.strip().splitlines()[-5:]) or f"exit status {p.returncode}"
            raise RuntimeError(f"go list {pattern}: {msg[-1000:]}")
        return pkgs

    def _write_overlay(self, call_dir: Path, overlay: dict[str, str | None]
                       ) -> tuple[Path, dict[str, str]]:
        replace: dict[str, str] = {}
        dirs: dict[str, Path] = {}
        overlay_dirs: dict[str, str] = {}
        for rel, content in overlay.items():
            rel = rel.strip("/")
            target = self.exec_root / rel
            if content is None:
                replace[str(target)] = ""
                continue
            parent = Path(rel).parent.as_posix()
            parent = "" if parent == "." else parent
            if parent not in dirs:
                d = Path(tempfile.mkdtemp(prefix="ov", dir=call_dir))
                dirs[parent] = d
                overlay_dirs[str(d)] = parent
            tmp = dirs[parent] / target.name
            tmp.write_text(content, encoding="utf-8")
            replace[str(target)] = str(tmp)
        path = call_dir / "overlay.json"
        path.write_text(json.dumps({"Replace": replace}), encoding="utf-8")
        return path, overlay_dirs

    async def test(self, pkg_rel: str, *, overlay: dict[str, str | None] | None = None,
                   run: list[str] | None = None, cover: bool = False, failfast: bool = False,
                   compile_only: bool = False, timeout: int | None = None) -> GoTestResult:
        t = int(timeout if timeout is not None else self.timeout)
        rel = pkg_rel.strip("/")
        call_dir = self._call_dir("t")
        try:
            args = [self.go, "test", "-json", "-vet=off", "-count=1", *self.flags]
            overlay_dirs: dict[str, str] = {}
            if overlay:
                ov_path, overlay_dirs = self._write_overlay(call_dir, overlay)
                args += ["-overlay", str(ov_path)]
            if compile_only:
                args += ["-run", "^$"]
            elif run is not None:
                args += ["-run", "^(" + "|".join(run) + ")$" if run else "^$"]
            if failfast:
                args.append("-failfast")
            profile = call_dir / "cover.out"
            if cover:
                args += ["-cover", "-covermode=set", f"-coverprofile={profile}"]
            args += ["-timeout", f"{t}s", f"./{rel}" if rel else "."]
            tmp = call_dir / "tmp"
            tmp.mkdir()

            p = await self._exec(args, t + TIMEOUT_SLACK, self._env(tmp))
            res, started = _parse_test_json(p.stdout, p.stderr, overlay_dirs)
            res.returncode = p.returncode
            if p.timed_out or p.overflow:
                res.timed_out = True
                if p.overflow:
                    note = OUTPUT_EXCEEDED.format(OUTPUT_LIMIT // (1024 * 1024))
                    res.tail = _cap(res.tail + "\n" + note + "\n", TAIL_CAP)
                compiler = any(_COMPILER_RE.match(e) for e in res.build_errors)
                if not started and (res.build_ok or not compiler):
                    res.build_ok = False
                    res.build_errors = [NOT_STARTED]
            if res.build_ok:
                requested = [] if compile_only else (run or [])
                died = res.timed_out or p.returncode != 0
                for name in requested:
                    if name not in res.tests and died:
                        res.tests[name] = "interrupted"
                        res.output.setdefault(name, "")
                if cover and profile.exists():
                    with contextlib.suppress(OSError):
                        res.coverage = parse_cover_profile(
                            profile.read_text(encoding="utf-8", errors="replace"), self.module)
            return res
        finally:
            shutil.rmtree(call_dir, ignore_errors=True)
