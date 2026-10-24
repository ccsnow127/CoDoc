"""the COBOL runner: LangRunner semantics over the GnuCOBOL harness.

`test(unit, overlay=, run=, cover=, compile_only=)` runs the unit's existing test programs
(`harness.run_program`) and the bundles of the overlay (`harness.run`), compiling every
overlaid program file from a content-addressed copy; verdicts follow the harness crash rules,
coverage is statement coverage from -ftraceall (or paragraph coverage), build errors are
mapped back to repo rels and bundle lines. Calls run in threads under a semaphore; each works
in OUT/work/tmp/b<random>/, removed afterwards (kept with CODOCS_KEEP_WORK=1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from codocs.golang.gotool import CoverBlock
from codocs.lang.base import LangTestResult

COBC_MISSING = "cobc not available"
COBOL_TIMEOUT = 60
MAX_BUILD_ERRORS = 50
OUTPUT_CHARS = 4000
TAIL_CHARS = 2000
NO_TRACE = "no GnuCOBOL trace was written"


log = logging.getLogger(__name__)
NO_COBC = ("cobc not found or not runnable ({cobc}): COBOL unit and batch tests are not "
           "generated or run, and COBOL docs use the facts signal")
_WARNED: set[str] = set()                 # cobc commands the missing-cobc WARNING named


def warn_no_cobc(cobc: str) -> bool:
    """log the missing-cobc WARNING once per process and cobc command; True
    when it was logged now."""
    cobc = cobc or "cobc"
    if cobc in _WARNED:
        return False
    _WARNED.add(cobc)
    log.warning("WARNING: %s", NO_COBC.format(cobc=cobc))
    return True


def _cap_tail(text: str, n: int) -> str:
    return text if len(text) <= n else text[-n:]


class _Outcome:
    """what one test program or bundle run contributed."""

    def __init__(self) -> None:
        self.tests: dict[str, str] = {}
        self.output: dict[str, str] = {}
        self.build_ok = True
        self.build_errors: list[str] = []
        self.timed_out = False
        self.returncode = 0
        self.tail = ""
        self.line_hits: dict[str, dict[int, int]] = {}
        self.paragraph_hits: dict[str, set[str]] = {}
        self.paragraph_lines: dict[str, set[int]] = {}
        self.traced = False
        self.paragraph_fallback = False


class CobolRunner:
    """runs one unit's COBOL test programs and generated bundles (LangRunner semantics)."""

    lang = "cobol"

    def __init__(self, adapter: Any, *, repo_root: str | Path, exec_root: str | Path | None = None,
                 work_dir: str | Path | None = None, tmp_dir: str | Path | None = None,
                 settings: Any = None, workers: int = 4, timeout: int = COBOL_TIMEOUT,
                 ensure_synced: Any = None):
        self.adapter = adapter
        self.settings = settings
        self.module_root = Path(repo_root).resolve()
        self.exec_root = Path(exec_root).resolve() if exec_root else self.module_root
        self.module = self.module_root.name
        base = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "codocs-cobol"
        self.work_dir = base.resolve() if base.exists() else base
        self.tmp_dir = Path(tmp_dir) if tmp_dir else self.work_dir.parent / "tmp"
        self.workers = max(1, int(workers))
        self.timeout = int(timeout)
        self._ensure_synced = ensure_synced
        self.cobc = getattr(settings, "cobc", "") or "cobc"
        flags = getattr(settings, "cobc_flags", "") or ""
        self.cobc_flags = shlex.split(flags) if flags else None
        mode = (getattr(settings, "cobol_coverage", "") or "statement").lower()
        self.coverage_mode = "paragraph" if mode == "paragraph" else "statement"
        self._cobc_ok: bool | None = None
        self._version: str | None = None
        self._harness = None
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop: Any = None
        self._dialects: dict[str, str] | None = None

    @classmethod
    def from_context(cls, adapter: Any, ctx: Any) -> CobolRunner:
        """a runner from a pipeline RunContext (read duck-typed)."""
        settings = getattr(ctx, "settings", None)
        repo_root = getattr(ctx, "repo_root", None) or adapter.ctx.root
        out_dir = getattr(ctx, "out_dir", None)
        work = getattr(ctx, "work_dir", None)          # OUT/work/tmp
        if work is not None:
            tmp_dir = Path(work)
            work_dir = Path(work).parent / "cobol"
        elif out_dir is not None:
            tmp_dir = Path(out_dir) / "work" / "tmp"
            work_dir = Path(out_dir) / "work" / "cobol"
        else:
            tmp_dir = work_dir = None
        timeout = int(getattr(settings, "test_timeout", 0) or COBOL_TIMEOUT)
        workers = getattr(ctx, "workers", None) or getattr(settings, "workers", 4)
        return cls(adapter, repo_root=repo_root, exec_root=getattr(ctx, "exec_root", None),
                   work_dir=work_dir, tmp_dir=tmp_dir, settings=settings, workers=workers,
                   timeout=timeout, ensure_synced=getattr(ctx, "ensure_synced", None))

    # -- toolchain -------------------------------------------------------------------------

    def available(self) -> bool:
        """always True: the static signals work without cobc."""
        return True

    def cobc_ok(self) -> bool:
        """`cobc --version` exits 0 (cached)."""
        if self._cobc_ok is None:
            self._cobc_ok = bool(self._probe())
            if not self._cobc_ok:
                warn_no_cobc(self.cobc)
        return self._cobc_ok

    def _probe(self) -> str:
        if shutil.which(self.cobc) is None:
            return ""
        try:
            cp = subprocess.run([self.cobc, "--version"], capture_output=True, text=True,
                                timeout=60)
        except (OSError, subprocess.SubprocessError):
            return ""
        if cp.returncode != 0:
            return ""
        first = (cp.stdout or "").strip().splitlines()
        return first[0].strip() if first else "cobc"

    def version(self) -> str:
        if self._version is None:
            text = self._probe()
            if text.startswith("cobc (GnuCOBOL)"):
                text = "GnuCOBOL " + text.split(")", 1)[1].strip()
            self._version = text or "no cobc: static signals only"
        return self._version

    def toolchain_failure(self, errors: list[str]) -> bool:
        return any(str(e).startswith(COBC_MISSING) for e in errors)

    def mutants_supported(self, unit_rel: str) -> bool:
        return self.cobc_ok()

    def cobc_flags_text(self) -> str:
        return " ".join(self.cobc_flags or [])

    @property
    def harness(self):
        """the GnuCOBOL harness over the adapter's COBOL index (created on first use)."""
        if self._harness is None:
            from codocs.cobol.harness import CobolTestRunner

            self._harness = CobolTestRunner(
                self.adapter.index(), self.work_dir, cobc=self.cobc, timeout=self.timeout,
                cobc_flags=self.cobc_flags, source_root=self.exec_root,
                mirror_dir=self.work_dir / "src", tmp_dir=self.tmp_dir)
        return self._harness

    def dialect(self, pid: str) -> str:
        """the dialect name a program builds with (probed once; OUT/work/cobol/dialects.json)."""
        if self._dialects is None:
            self._dialects = {}
            try:
                self._dialects = json.loads((self.work_dir / "dialects.json").read_text())
            except (OSError, ValueError):
                self._dialects = {}
        if pid not in self._dialects:
            name = self.harness.program_dialect(pid)[0]
            self._dialects[pid] = name
            try:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                (self.work_dir / "dialects.json").write_text(
                    json.dumps(dict(sorted(self._dialects.items())), indent=1))
            except OSError:
                pass
        return self._dialects[pid]

    # -- execution plumbing ----------------------------------------------------------------

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.workers)
            self._sem_loop = loop
        return self._sem

    async def _sync(self) -> None:
        fn = self._ensure_synced
        if fn is None:
            return
        res = fn()
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            await res

    def _call_dir(self) -> Path:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="b", dir=self.tmp_dir)).resolve()

    @staticmethod
    def _cleanup(d: Path) -> None:
        if os.environ.get("CODOCS_KEEP_WORK") != "1":
            shutil.rmtree(d, ignore_errors=True)

    # -- public ------------------------------------------------------------------------------

    async def list_tests(self, unit_rel: str) -> list[str] | None:
        try:
            unit = self.adapter.unit(unit_rel)
        except KeyError:
            return None
        names: list[str] = []
        for f in unit.test_files:
            names += self.adapter.test_names(self.adapter.read(f), f)
        return sorted(names)

    def unit_notes(self, unit_rel: str) -> list[str]:
        """cobol-check suites are reported, never run."""
        root = self.module_root / unit_rel if unit_rel else self.module_root
        try:
            n = sum(1 for p in root.rglob("*.cut") if p.is_file())
        except OSError:
            n = 0
        return [f"cobol-check suites are not supported: {n} .cut files"] if n else []

    async def test(self, unit_rel: str, *, overlay: dict[str, str | None] | None = None,
                   run: list[str] | None = None, cover: bool = False, failfast: bool = False,
                   compile_only: bool = False, timeout: int | None = None) -> LangTestResult:
        if failfast and cover:
            raise ValueError("failfast is never combined with cover")
        await self._sync()
        try:
            unit = self.adapter.unit(unit_rel)
        except KeyError:
            return LangTestResult(build_ok=False,
                                  build_errors=[f"{unit_rel or '.'}: no COBOL unit"])
        if not self.cobc_ok():
            return LangTestResult(build_ok=False, build_errors=[
                f"{COBC_MISSING}: {self.cobc} (install GnuCOBOL 3.2 or pass --cobc)"])
        t = int(timeout if timeout is not None else self.timeout)
        overlay = dict(overlay or {})
        programs = {rel: text for rel, text in overlay.items()
                    if text is not None and not self.adapter.is_test_file(rel)}
        bundles = {rel: text for rel, text in overlay.items()
                   if text is not None and rel.endswith("_codocs_test.cbt")}
        hidden = {rel for rel, text in overlay.items() if text is None}
        tests = [f for f in unit.test_files if f not in hidden]
        if compile_only:
            return await self._compile_only(unit, programs)
        if run is None:
            owners = tests + sorted(bundles)
            wanted: set[str] | None = None
        else:
            wanted = set(run)
            want_owners = {n.split("::", 1)[0] for n in run}
            owners = [o for o in tests + sorted(bundles) if o in want_owners]
        trace = self.coverage_mode if cover else "none"
        jobs = [self._one(o, bundles.get(o), programs, trace, t, wanted) for o in owners]
        outcomes = await asyncio.gather(*jobs)
        return self._merge(unit, outcomes, cover)

    # -- compile-only ----------------------------------------------------------------------

    async def _compile_only(self, unit: Any, programs: dict[str, str]) -> LangTestResult:
        errors: list[str] = []
        logs: list[str] = []
        ok_all = True
        for rel, text in sorted(programs.items()):
            neutral = bool(_has_exec(text))
            async with self._semaphore():
                ok, errs, log = await asyncio.to_thread(self.harness.compile_check, rel, text,
                                                        neutralize=neutral)
            ok_all = ok_all and ok
            errors += errs
            logs.append(log)
        return LangTestResult(build_ok=ok_all, build_errors=errors[:MAX_BUILD_ERRORS],
                              tail=_cap_tail("\n".join(logs), TAIL_CHARS))

    # -- one test program or bundle -------------------------------------------------------------

    async def _one(self, owner: str, bundle_text: str | None, programs: dict[str, str],
                   trace: str, t: int, wanted: set[str] | None) -> _Outcome:
        async with self._semaphore():
            d = self._call_dir()
            try:
                if bundle_text is not None:
                    return await asyncio.to_thread(self._bundle, owner, bundle_text, programs,
                                                   trace, t, wanted, d)
                return await asyncio.to_thread(self._program, owner, programs, trace, t,
                                               wanted, d)
            finally:
                self._cleanup(d)

    def _keep(self, name: str, wanted: set[str] | None) -> bool:
        return wanted is None or name in wanted

    def _bundle(self, suite: str, text: str, programs: dict[str, str], trace: str, t: int,
                wanted: set[str] | None, d: Path) -> _Outcome:
        from codocs.cobol import bundle

        out = _Outcome()
        b = bundle.parse_bundle(text)
        if b is None or b.error:
            reason = (b.error if b is not None else "not a COBOL test bundle")
            out.build_ok = False
            out.build_errors = [f"{suite}:1: the reply is not a JSON test plan: {reason}"]
            return out
        target = b.target
        testability = self.adapter.testability(target) if target else None
        test = bundle.to_generated(b, testability)
        if test.error:
            out.build_ok = False
            out.build_errors = [f"{suite}:1: the reply is not a JSON test plan: {test.error}"]
            return out
        if test.program_id not in self.harness.index.programs:
            out.build_ok = False
            out.build_errors = [f"{suite}:1: program {test.program_id} is not in the index"]
            return out
        res = self.harness.run(test, overlay=programs, trace=trace, work=d, timeout=t)
        out.tail = self._tail(res)
        self._trace(res, d, trace, out)
        failed = self._overlay_failures(res)
        if res.status == "compile-error" or failed:
            out.build_ok = False
            out.build_errors = self._build_errors(res, suite, b, d) or \
                [f"{suite}:1: the test plan does not build"]
            return out
        names = bundle.test_ids(b)
        case_ids = set(bundle.case_ids(b))
        stdout_ids = b.stdout_ids
        died = res.crashed or res.status == "timeout"
        out.timed_out = res.status == "timeout"
        out.returncode = self._returncode(res)
        log = res.run_log or ""
        seen: set[str] = set()
        pending: list[str] = []
        for c in res.cases:
            cid = c.id
            if cid.startswith("stdout-") and cid[7:].isdigit():
                k = int(cid[7:]) - 1
                cid = stdout_ids[k] if 0 <= k < len(stdout_ids) else cid
            if cid not in names:
                continue
            name = f"{suite}::{cid}"
            seen.add(cid)
            if c.status == "not-run":
                if cid in case_ids:
                    pending.append(cid)
                elif died:
                    self._put(out, name, "fail", "the run ended early: " + (c.detail or ""), log,
                              wanted)
                continue
            if c.status == "pass":
                self._put(out, name, "pass", c.detail, log, wanted)
            elif c.status == "weak":
                self._put(out, name, "fail", f"weak: {c.detail} (a PASS that checks nothing is "
                                             "not a passing test)", log, wanted)
            else:
                msg = f"TEST {cid} FAIL {c.detail}".rstrip() if cid in case_ids else \
                    (c.detail or f"{cid} failed")
                self._put(out, name, "fail", msg, log, wanted)
        for cid in names:
            if cid not in seen and cid not in case_ids and died:
                self._put(out, f"{suite}::{cid}", "fail", "the run ended early", log, wanted)
        if died and pending:
            blamed = next((s for s in reversed(res.started) if s in pending), None)
            if blamed is None:
                blamed = pending[0]
            for cid in pending:
                name = f"{suite}::{cid}"
                if cid == blamed:
                    detail = self._crash_detail(res)
                    self._put(out, name, "fail", f"did not run: the driver ended early "
                                                 f"({detail})", log, wanted)
                else:
                    self._put(out, name, "interrupted", "", log, wanted)
        return out

    def _program(self, rel: str, programs: dict[str, str], trace: str, t: int,
                 wanted: set[str] | None, d: Path) -> _Outcome:
        out = _Outcome()
        res = self.harness.run_program(rel, overlay=programs, trace=trace, work=d, timeout=t)
        out.tail = self._tail(res)
        self._trace(res, d, trace, out)
        failed = self._overlay_failures(res)
        if res.status == "compile-error" or failed:
            out.build_ok = False
            out.build_errors = self._build_errors(res, rel, None, d) or \
                [f"{rel}:1: error: the test program does not build"]
            return out
        src = self.adapter.read(rel)
        names = self.adapter.test_names(src, rel)
        died = res.crashed or res.status == "timeout"
        out.timed_out = res.status == "timeout"
        out.returncode = self._returncode(res)
        log = res.run_log or ""
        by_id = {c.id: c for c in res.cases}
        if names == [rel]:
            ok = res.status == "passed" and not died
            self._put(out, rel, "pass" if ok else "fail",
                      "" if ok else (self._crash_detail(res) if died else
                                     f"exit status {res.exit_status}"), log, wanted)
            return out
        blamed = False
        for name in names:
            cid = name.split("::", 1)[1]
            c = by_id.get(cid)
            if c is not None:
                st = "pass" if c.status == "pass" else "fail"
                msg = "" if st == "pass" else f"TEST {cid} FAIL {c.detail}".rstrip()
                self._put(out, name, st, msg, log, wanted)
            elif died:
                started = [s for s in res.started if s not in by_id]
                if not blamed and (not started or cid == started[-1]):
                    blamed = True
                    self._put(out, name, "fail", "did not run: the test program ended early "
                                                 f"({self._crash_detail(res)})", log, wanted)
                else:
                    self._put(out, name, "interrupted", "", log, wanted)
        return out

    # -- result details --------------------------------------------------------------------

    def _put(self, out: _Outcome, name: str, verdict: str, detail: str, log: str,
             wanted: set[str] | None) -> None:
        if not self._keep(name, wanted):
            return
        out.tests[name] = verdict
        if verdict != "pass":
            text = (detail or "").strip()
            if log:
                text = (text + "\n" + log).strip() if text else log
            out.output[name] = _cap_tail(text, OUTPUT_CHARS) if len(text) > OUTPUT_CHARS \
                else text

    @staticmethod
    def _tail(res: Any) -> str:
        """the last characters of the compile and run logs, with a non-zero exit status."""
        text = "\n\n".join(x for x in (res.compile_log, res.run_log) if x)
        status = res.exit_status
        if status and status > 0 and not res.crashed:
            text += f"\nexit status {status}"
        return _cap_tail(text, TAIL_CHARS)

    @staticmethod
    def _returncode(res: Any) -> int:
        import signal as _signal

        status = res.exit_status
        if res.status == "timeout" or status is None:
            return -1 if res.status == "timeout" else 0
        if status < 0:
            return 128 - status
        if 128 < status < 160:
            try:
                _signal.Signals(status - 128)
                return status
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _crash_detail(res: Any) -> str:
        from codocs.cobol.harness import _SIGNAL_MESSAGE

        log = res.run_log or ""
        m = _SIGNAL_MESSAGE.search(log)
        if m:
            return m.group(0)
        if res.status == "timeout":
            return "timed out"
        for ln in log.splitlines():
            if ln.startswith("libcob:"):
                return ln.strip()[:200]
        status = res.exit_status
        if status is not None and status < 0:
            return f"signal {-status}"
        if status:
            return f"exit status {status}"
        last = next((ln.strip() for ln in reversed(log.splitlines()) if ln.strip()), "")
        return last[:200] or "no verdict"

    @staticmethod
    def _overlay_failures(res: Any) -> list[str]:
        from codocs.cobol.harness import OVERLAY_FAILED

        return [ln.split(OVERLAY_FAILED, 1)[1].strip()
                for ln in (res.compile_log or "").splitlines() if OVERLAY_FAILED in ln]

    def _build_errors(self, res: Any, suite: str, b: Any, d: Path) -> list[str]:
        """cobc errors of a failed build: driver and stub lines as bundle lines, generated
        harness programs as <harness>/<name>, repo files as rels."""
        from codocs.cobol.harness import _ERROR_LINE

        out: list[str] = []
        stub_offsets = self._stub_offsets(b, d) if b is not None else {}
        driver = None
        if b is not None:
            name = (b.plan.get("driver_name") or "TDRIVER")
            from codocs.cobol.harness import _safe_rel

            driver = str((d / f"{_safe_rel(name)}.cbl").resolve())
        for raw in (res.compile_log or "").splitlines():
            m = _ERROR_LINE.match(raw.strip())
            if not m:
                continue
            path, num, msg = m.group(1), int(m.group(2)), m.group(3).strip()
            try:
                full = str(Path(path).resolve())
            except OSError:
                full = path
            if driver is not None and full == driver:
                line = f"{suite}:{b.driver_line + num}: error: {msg}"
            elif full in stub_offsets:
                line = f"{suite}:{stub_offsets[full] + num}: error: {msg}"
            else:
                rel = self.harness.rel_of_source(path)
                if rel is None and full.startswith(str(d)):
                    rel = f"<harness>/{PurePosixPath(full).name}"
                line = f"{rel or PurePosixPath(path).name}:{num}: error: {msg}"
            if line not in out:
                out.append(line)
        return out[:MAX_BUILD_ERRORS]

    @staticmethod
    def _stub_offsets(b: Any, d: Path) -> dict[str, int]:
        """STUBnn.cbl path -> bundle line before its first line."""
        out: dict[str, int] = {}
        stub_lines = b.stubs.split("\n") if b.stubs else []
        used = 0
        for p in sorted(d.glob("STUB*.cbl")):
            try:
                first = next(ln for ln in p.read_text(errors="replace").split("\n")
                             if ln.strip())
            except (OSError, StopIteration):
                continue
            k = next((i for i in range(used, len(stub_lines))
                      if stub_lines[i].rstrip() == first.rstrip()), None)
            if k is None:
                continue
            used = k + 1
            out[str(p.resolve())] = b.stubs_line + k
        return out

    def _trace(self, res: Any, d: Path, trace: str, out: _Outcome) -> None:
        from codocs.cobol.harness import XFSZ_NOTE

        if trace == "none":
            return
        out.traced = (d / "trace.txt").exists() or bool(res.line_hits or res.paragraph_hits)
        out.paragraph_fallback = XFSZ_NOTE in (res.run_log or "")
        for rel, hits in (res.line_hits or {}).items():
            cur = out.line_hits.setdefault(rel, {})
            for line, n in hits.items():
                cur[int(line)] = cur.get(int(line), 0) + int(n)
        for rel, paras in (res.paragraph_hits or {}).items():
            out.paragraph_hits.setdefault(rel, set()).update(paras)
        for rel, heads in (getattr(res, "paragraph_lines", None) or {}).items():
            out.paragraph_lines.setdefault(rel, set()).update(int(n) for n in heads)

    # -- merging ---------------------------------------------------------------------------

    def _merge(self, unit: Any, outcomes: list[_Outcome], cover: bool) -> LangTestResult:
        res = LangTestResult(build_ok=True)
        tails: list[str] = []
        for o in outcomes:
            res.tests.update(o.tests)
            res.output.update(o.output)
            if not o.build_ok:
                res.build_ok = False
                res.build_errors += [e for e in o.build_errors if e not in res.build_errors]
            res.timed_out = res.timed_out or o.timed_out
            if o.returncode and not res.returncode:
                res.returncode = o.returncode
            if o.tail:
                tails.append(o.tail)
        res.build_errors = res.build_errors[:MAX_BUILD_ERRORS]
        res.tail = _cap_tail("\n\n".join(tails), TAIL_CHARS)
        if cover and res.build_ok:
            traced = [o for o in outcomes if o.traced]
            if not traced:
                res.coverage = None
                res.coverage_error = NO_TRACE
            else:
                fallback = any(o.paragraph_fallback for o in traced)
                mode = "paragraph" if fallback or self.coverage_mode == "paragraph" \
                    else "statement"
                res.coverage = self._blocks(unit, traced, mode)
                if fallback:
                    from codocs.cobol.harness import XFSZ_NOTE

                    res.coverage_error = XFSZ_NOTE
        return res

    def _blocks(self, unit: Any, outcomes: list[_Outcome], mode: str) -> list[CoverBlock]:
        """statement blocks per universe line (or paragraph blocks) of every non-test
        program file of the unit; files not linked into a run get count-0 blocks."""
        lines: dict[str, dict[int, int]] = {}
        paras: dict[str, set[str]] = {}
        entered: dict[str, set[int]] = {}
        for o in outcomes:
            for rel, hits in o.line_hits.items():
                cur = lines.setdefault(rel, {})
                for n, c in hits.items():
                    cur[n] = cur.get(n, 0) + c
            for rel, ps in o.paragraph_hits.items():
                paras.setdefault(rel, set()).update(ps)
            for rel, ns in o.paragraph_lines.items():
                entered.setdefault(rel, set()).update(ns)
        blocks: list[CoverBlock] = []
        index = self.harness.index
        for rel in unit.files:
            pids = self.adapter.programs_of(rel)
            if not pids or self.adapter.is_test_file(rel):
                continue
            if mode == "statement":
                hits = lines.get(rel, {})
                for n in sorted(self.harness.statement_lines(rel)):
                    blocks.append(CoverBlock(file=rel, start_line=n, start_col=1, end_line=n,
                                             end_col=1, stmts=1, count=hits.get(n, 0)))
                continue
            hit = {p.upper() for p in paras.get(rel, set())}
            text = self.adapter.read(rel)
            total = text.count("\n") + 1
            heads: list[tuple[int, str]] = []
            for pid in pids:
                heads += [(p.line, p.name) for p in index.programs[pid].paragraphs
                          if p.name != "(entry)"]
            heads.sort()
            names = {d.name for d in self.adapter.decls(text) if d.kind == "method"}
            # a paragraph is covered when its own header line was entered; by name only
            # when the trace gave no lines for the file (S1.P1 must not cover S2.P1)
            at = entered.get(rel)
            for i, (ln, name) in enumerate(heads):
                if name.upper() not in names:
                    continue                    # trivial paragraphs (only EXIT): no block
                end = heads[i + 1][0] - 1 if i + 1 < len(heads) else total
                ran = ln in at if at is not None else name.upper() in hit
                blocks.append(CoverBlock(file=rel, start_line=ln, start_col=1,
                                         end_line=max(ln, end), end_col=1, stmts=1,
                                         count=1 if ran else 0))
        return blocks


def _has_exec(text: str) -> bool:
    import re

    return bool(re.search(r"\bEXEC\s+(?:CICS|SQL|DLI)\b", text, re.I))
