"""tesearch measurement: prune failing tests, coverage and mutation on the passing subset."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from codocs.golang.gotool import GoRunner, GoTestResult, file_coverage
from codocs.golang.mutate import Mutant, apply
from codocs.golang.package import PackageInfo
from codocs.golang.source import (
    Import,
    function_spans,
    identifiers,
    imports,
    remove_functions,
    remove_imports,
)
from codocs.lang.registry import adapter_for
from codocs.tesearch.prompts import suite_path

MAX_IMPORT_FIXES = 2
MAX_GREEN_RUNS = 8
MESSAGE_CHARS = 2000
MUTANT_MIN_TIMEOUT = 30
_NON_TESTS = ("Example", "Fuzz", "Benchmark")

_NOISE = ("=== RUN", "=== PAUSE", "=== CONT", "=== NAME", "--- PASS", "--- SKIP", "PASS")

STD_ALLOWED = [
    Import(p) for p in (
        "testing", "fmt", "strings", "errors", "reflect", "sort", "strconv", "math", "bytes",
        "time", "os", "io", "context", "sync", "regexp", "encoding/json", "path/filepath",
        "math/rand", "net/http", "net/http/httptest", "unicode", "slices", "maps",
    )
]


@dataclass
class Measurement:
    build_ok: bool
    build_errors: list[str] = field(default_factory=list)
    size: int = 0
    passing: list[str] = field(default_factory=list)
    failing: dict[str, str] = field(default_factory=dict)
    green_src: str = ""
    green_size: int = 0
    cov: float = 0.0
    uncovered: list[int] = field(default_factory=list)
    mut: float | None = None
    mutants_total: int = 0
    mutants_valid: int = 0
    killed: int = 0
    survivors: list[dict] = field(default_factory=list)
    suite_src: str = ""
    cov_note: str = ""            # non-Go: "coverage unavailable: <reason>" (cov is then 1.0)

    @property
    def score(self) -> float:
        if not self.build_ok or self.green_size == 0:
            return 0.0
        return self.cov * self.mut if self.mut is not None else self.cov

    def to_dict(self) -> dict:
        d = asdict(self)
        if not self.cov_note:
            d.pop("cov_note")
        d["score"] = self.score
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Measurement:
        mut = d.get("mut")
        return cls(
            build_ok=bool(d.get("build_ok", False)),
            build_errors=list(d.get("build_errors") or []),
            size=int(d.get("size", 0)),
            passing=list(d.get("passing") or []),
            failing=dict(d.get("failing") or {}),
            green_src=d.get("green_src", ""),
            green_size=int(d.get("green_size", 0)),
            cov=float(d.get("cov", 0.0)),
            uncovered=[int(n) for n in d.get("uncovered") or []],
            mut=None if mut is None else float(mut),
            mutants_total=int(d.get("mutants_total", 0)),
            mutants_valid=int(d.get("mutants_valid", 0)),
            killed=int(d.get("killed", 0)),
            survivors=[dict(s) for s in d.get("survivors") or []],
            suite_src=d.get("suite_src", ""),
            cov_note=d.get("cov_note", "") or "",
        )


_LOG_LINE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}")
_ASSERT_LINE = re.compile(r"^\s*[\w.-]+_test\.go:\d+:")
_FRAME_SKIP = ("testing.", "runtime.", "panic(", "sync.", "reflect.", "created by ")


def _func_name(frame: str) -> str:
    """'pkg.(*T).M(0x0, {..})' -> 'pkg.(*T).M': drop the trailing argument list."""
    if not frame.endswith(")"):
        return frame
    depth = 0
    for i in range(len(frame) - 1, -1, -1):
        if frame[i] == ")":
            depth += 1
        elif frame[i] == "(":
            depth -= 1
            if depth == 0:
                return frame[:i]
    return frame


def failure_summary(output: str) -> str:
    """one line saying why a test failed: the panic and its first frames in module code,
    else its t.Error/t.Fatal lines; log noise (timestamped lines) is skipped."""
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("panic:"):
            head = ln.strip().replace(" [recovered]", "")
            frames: list[str] = []
            j = i + 1
            while j < len(lines) and len(frames) < 3:
                fn = lines[j].strip()
                loc = lines[j + 1].strip() if j + 1 < len(lines) else ""
                if loc.startswith("/") and ".go:" in loc:
                    if not fn.startswith(_FRAME_SKIP) and not fn.startswith("panic:"):
                        where = loc.split(" +0x")[0].rsplit("/", 1)[-1]
                        frames.append(f"{_func_name(fn)} ({where})")
                    j += 2
                else:
                    j += 1
            return head + (" at " + " <- ".join(frames) if frames else "")
    asserts = [ln.strip() for ln in lines if _ASSERT_LINE.match(ln)]
    if asserts:
        return " | ".join(asserts[:3])
    rest = [ln.strip() for ln in lines if not _LOG_LINE.match(ln.strip())
            and not ln.lstrip().startswith(_NOISE) and not ln.lstrip().startswith("--- FAIL")
            and ln.strip() != "FAIL"]
    return " | ".join(rest[:2]) or "test failed"


def failure_message(output: str) -> str:
    """the failure summary line, then the test's output without the runner's status lines."""
    lines = [ln.rstrip() for ln in output.splitlines()]
    kept = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith(_NOISE)
            and not ln.lstrip().startswith("--- FAIL") and ln.strip() != "FAIL"]
    body = "\n".join(kept).strip()
    summary = failure_summary(output)
    first = body.splitlines()[0].strip() if body else ""
    text = body if summary == first else summary + ("\n" + body if body else "")
    return text if len(text) <= MESSAGE_CHARS else text[:MESSAGE_CHARS] + "\n..."


def _crash_message(res: GoTestResult) -> str:
    for ln in reversed(res.tail.splitlines()):
        if "panic:" in ln or "timed out" in ln:
            return f"did not run: the test binary exited early ({ln.strip()[:200]})"
    return "did not run: the test binary exited early"


def prune(src: str, names: set[str]) -> str:
    """remove those test functions and the imports only they used."""
    spans = function_spans(src)
    removed = "".join(src[a:b] for n, (a, b) in spans.items() if n in names)
    rest = remove_functions(src, names)
    present = imports(rest)
    body = identifiers(remove_imports(rest, {i.path for i in present}))
    used_before = identifiers(removed)
    drop = {
        i.path for i in present
        if i.alias not in ("_", ".") and i.name in used_before and i.name not in body
    }
    return remove_imports(rest, drop) if drop else rest


def _go_name(name: str, prefix: str) -> bool:
    rest = name[len(prefix):] if name.startswith(prefix) else None
    return rest is not None and (not rest or not rest[0].islower())


def strip_non_tests(src: str) -> str:
    """drop Example/Fuzz/Benchmark functions and TestMain, and the imports only they used."""
    names = {n for n in function_spans(src)
             if n == "TestMain" or any(_go_name(n, p) for p in _NON_TESTS)}
    return prune(src, names) if names else src


def _verdict_ok(v: str | None) -> bool:
    return v in ("pass", "skip")


class _Runs:
    def __init__(self, runner: GoRunner, pkg: PackageInfo, target_rel: str,
                 allowed: list[Import], lang=None):
        self.runner = runner
        self.pkg = pkg
        self.target_rel = target_rel
        self.allowed = allowed
        self.lang = lang if lang is not None else adapter_for(runner)
        if self.lang.name == "go":
            stem = PurePosixPath(target_rel).name.removesuffix(".go")
            self.suite_rel = suite_path(pkg, stem)
        else:
            self.suite_rel = self.lang.suite_rel(pkg, self.lang.stem(target_rel))
        self.basename = PurePosixPath(self.suite_rel).name
        self.seconds = 0.0

    def overlay(self, suite: str, target: str | None = None) -> dict[str, str | None]:
        ov: dict[str, str | None] = {f: None for f in self.pkg.all_test_files}
        ov[self.suite_rel] = suite
        if target is not None:
            ov[self.target_rel] = target
        return ov

    async def _test(self, src: str, run: list[str] | None) -> GoTestResult:
        t0 = time.monotonic()
        if run is None:
            res = await self.runner.test(self.pkg.rel, overlay=self.overlay(src), cover=True)
        else:
            res = await self.runner.test(self.pkg.rel, overlay=self.overlay(src), run=run)
        self.seconds = time.monotonic() - t0
        return res

    async def suite(self, src: str, run: list[str] | None = None) -> tuple[str, GoTestResult]:
        """run (all tests with coverage, or only `run`), repairing the suite's imports."""
        res = await self._test(src, run)
        runtime = getattr(self.lang, "runtime_import_errors", None)
        for _ in range(MAX_IMPORT_FIXES):
            if not res.build_ok:
                errors = list(res.build_errors)
            elif runtime is not None:
                errors = runtime(res, self.suite_rel)    # a NameError at run time (Python)
            else:
                break
            if not errors:
                break
            fixed = self.lang.fix_imports(src, errors, self.basename, self.allowed)
            if fixed is None:
                break
            src = fixed
            res = await self._test(src, run)
        return src, res

    def compile_overlay(self, target: str) -> dict[str, str | None]:
        ov: dict[str, str | None] = {f: None for f in self.pkg.all_test_files}
        ov[self.target_rel] = target
        return ov


def _default_allowed(runner: GoRunner, pkg: PackageInfo) -> list[Import]:
    out: list[Import] = []
    seen: set[str] = set()
    for f in pkg.go_files:
        try:
            src = (runner.module_root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for imp in imports(src):
            if imp.alias not in ("_", ".") and imp.path not in seen:
                seen.add(imp.path)
                out.append(Import(imp.path, imp.alias))
    out += [i for i in STD_ALLOWED if i.path not in seen]
    return out


async def _green(runs: _Runs, src: str, res: GoTestResult
                 ) -> tuple[str, GoTestResult, dict[str, str], bool, list[str], float]:
    """prune failing tests until one full run of the rest passes.

    a crash (panic, log.Fatal, -timeout) only blames the test that caused it: tests cut short
    by it, or never started, are re-run on their own (only those, merging verdicts) and the
    surviving suite is verified by a final full run with coverage.
    """
    lang = runs.lang
    failing: dict[str, str] = {}
    green = src
    known: dict[str, str] = {}
    full = True
    for attempt in range(MAX_GREEN_RUNS + 1):
        names = lang.test_names(green, runs.suite_rel)
        if full:
            known = {}
        before = len(known)
        for n in names:
            v = res.tests.get(n)
            if v in ("pass", "skip", "fail"):
                known[n] = v
        bad = {n: lang.failure_message(res.output.get(n, ""))
               for n in names if known.get(n) == "fail"}
        unlisted = getattr(lang, "unlisted_failures", None)
        extra = unlisted(res, green, runs.suite_rel) if unlisted is not None and full else []
        for n in extra:
            bad[n] = lang.failure_message(res.output.get(n, ""))
        if full and not bad and len(known) == len(names):
            return green, res, failing, bool(names), [], runs.seconds
        pending = [n for n in names if n not in known]
        if pending and (not bad and len(known) == before or attempt >= MAX_GREEN_RUNS - 1):
            bad.update({n: lang.crash_message(res) for n in pending})
            pending = []
        failing.update(bad)
        if bad:
            before_src = green
            green = lang.remove_tests(green, set(bad))
            for n in bad:
                known.pop(n, None)
            if extra and green == before_src and not pending:
                break       # failing tests static discovery cannot prune: never green

        if not lang.test_names(green, runs.suite_rel) or attempt == MAX_GREEN_RUNS:
            break
        full = not pending
        green, res = await runs.suite(green, None if full else pending)
        if not res.build_ok:
            return green, res, failing, False, list(res.build_errors), runs.seconds
    return green, res, failing, False, [], runs.seconds


async def measure(runner: GoRunner, pkg: PackageInfo, target_rel: str, suite_src: str,
                  mutants: list[Mutant], allowed: list[Import] | None = None) -> Measurement:
    """run the suite with the package's own tests hidden, prune, cover, mutate."""
    lang = adapter_for(runner)
    runs = _Runs(runner, pkg, target_rel,
                 allowed if allowed is not None else lang.default_allowed(runner, pkg), lang)
    size = len(lang.test_names(suite_src, runs.suite_rel))
    src, res = await runs.suite(lang.strip_non_tests(suite_src))
    if not res.build_ok:
        return Measurement(build_ok=False, build_errors=list(res.build_errors), size=size,
                           suite_src=src)

    green, res, failing, green_ok, build_errors, green_seconds = await _green(runs, src, res)
    names = lang.test_names(green, runs.suite_rel) if green_ok else []
    base = Measurement(build_ok=True, build_errors=build_errors, size=size,
                       passing=sorted(n for n in names if _verdict_ok(res.tests.get(n))),
                       failing=failing, suite_src=src,
                       mutants_total=len(mutants))
    if not green_ok:
        return base
    base.green_src = green
    base.green_size = len(names)

    if lang.name == "go":
        try:
            target = (runner.module_root / target_rel).read_text(encoding="utf-8")
        except OSError:
            target = ""
    else:
        target = lang.read(target_rel, runner.module_root)
    cov, uncovered = file_coverage(res.coverage or [], target_rel, target or None)
    if cov is None and lang.name == "go":
        cov = 1.0 if res.coverage else 0.0
    elif cov is None and res.coverage is None:
        # coverage could not be measured: neutral, so the score is the mutation score
        reason = getattr(res, "coverage_error", "") or "no coverage data"
        cov, uncovered = 1.0, []
        base.cov_note = f"coverage unavailable: {reason}"
    elif cov is None:
        cov = 0.0           # coverage measured, but the target was never loaded
    base.cov = cov
    base.uncovered = uncovered
    if not mutants or not target:
        return base
    supported = getattr(runner, "mutants_supported", None)
    if supported is not None and not supported(pkg.rel):
        return base

    gap = set(uncovered)
    timeout = min(runner.timeout, max(MUTANT_MIN_TIMEOUT, int(3 * green_seconds) + 10))

    classify = getattr(lang, "broken_mutant", None)

    def broken(mutated: str, r: GoTestResult) -> str:
        """a mutant that does not build: invalid (compiled languages), unless the adapter can
        tell a run-time failure at import from a syntax error (Python)."""
        return classify(mutated, r) if classify is not None else "invalid"

    async def run_mutant(m: Mutant) -> str:
        try:
            mutated = apply(target, m)
        except ValueError:
            return "invalid"
        if m.line in gap:
            r = await runner.test(pkg.rel, overlay=runs.compile_overlay(mutated),
                                  compile_only=True, timeout=timeout)
            return "survived" if r.build_ok else broken(mutated, r)
        r = await runner.test(pkg.rel, overlay=runs.overlay(green, mutated), run=names,
                              failfast=True, timeout=timeout)
        if not r.build_ok:
            return broken(mutated, r)
        if r.timed_out or not all(_verdict_ok(r.tests.get(n)) for n in names):
            return "killed"
        return "survived"

    outcomes = await asyncio.gather(*(run_mutant(m) for m in mutants))
    base.mutants_valid = sum(o != "invalid" for o in outcomes)
    base.killed = sum(o == "killed" for o in outcomes)
    base.survivors = [
        {"id": m.id, "line": m.line, "operator": m.operator}
        for m, o in zip(mutants, outcomes) if o == "survived"
    ]
    base.mut = base.killed / base.mutants_valid if base.mutants_valid else None
    return base
