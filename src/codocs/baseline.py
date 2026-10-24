"""per-package baseline: test verdicts on the original code, cached under OUT/baseline/.

Go packages are cached under OUT/baseline/; other languages go through their adapter and
runner and are cached under OUT/baseline/<lang>/ with their discovered tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from codocs.golang.gotool import GoRunner
from codocs.golang.package import PackageInfo
from codocs.golang.source import test_functions
from codocs.lang.registry import adapter_for

log = logging.getLogger(__name__)

MAX_RUNS = 25
_VERDICTS = ("pass", "fail", "skip")


@dataclass
class Baseline:
    pkg: str
    build_ok: bool
    passing: list[str] = field(default_factory=list)
    failing: list[str] = field(default_factory=list)
    build_errors: list[str] = field(default_factory=list)
    seconds: float = 0.0
    timed_out: bool = False
    incomplete: bool = False
    runs: int = 0
    lang: str = "go"
    declared: list[str] = field(default_factory=list)   # non-Go: the runner's discovered tests
    notes: list[str] = field(default_factory=list)      # non-Go: collection errors, caveats

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.lang == "go":
            for key in ("lang", "declared", "notes"):
                d.pop(key)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Baseline:
        return cls(
            pkg=d.get("pkg", ""),
            build_ok=bool(d.get("build_ok", False)),
            passing=list(d.get("passing") or []),
            failing=list(d.get("failing") or []),
            build_errors=list(d.get("build_errors") or []),
            seconds=float(d.get("seconds", 0.0)),
            timed_out=bool(d.get("timed_out", False)),
            incomplete=bool(d.get("incomplete", False)),
            runs=int(d.get("runs", 0)),
            lang=d.get("lang", "go") or "go",
            declared=list(d.get("declared") or []),
            notes=list(d.get("notes") or []),
        )


def baseline_path(out_dir: Path, pkg: PackageInfo) -> Path:
    return Path(out_dir) / "baseline" / (pkg.dir_name.replace("/", "__") + ".json")


def _load(path: Path) -> Baseline | None:
    try:
        return Baseline.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _save(path: Path, base: Baseline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(base.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _declared_tests(runner: GoRunner, pkg: PackageInfo) -> set[str]:
    names: set[str] = set()
    for f in pkg.all_test_files:
        try:
            names.update(test_functions((runner.module_root / f).read_text(
                encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return names


async def _declared(runner, pkg) -> tuple[set[str], list[str]]:
    """non-Go: the runner's discovery of the unit's existing tests, else the static list."""
    lister = getattr(runner, "list_tests", None)
    names = None
    if lister is not None:
        try:
            names = await lister(pkg.rel)
        except Exception as e:      # discovery is best-effort: fall back to the static list
            log.warning("test discovery failed for %s: %s", pkg.rel or ".", e)
            names = None
    if names is not None:
        return set(names), []
    lang = adapter_for(runner)
    return (set(lang.declared_tests(runner.module_root, pkg)),
            ["test discovery failed; using the statically declared tests"])


def _unit_notes(runner, pkg) -> list[str]:
    fn = getattr(runner, "unit_notes", None)
    if fn is None:
        return []
    try:
        return list(fn(pkg.rel) or [])
    except Exception:
        return []


async def _measure(runner: GoRunner, pkg: PackageInfo) -> Baseline:
    """run the package's tests; after a crash, re-run the tests that never reported."""
    lang = adapter_for(runner).name
    res = await runner.test(pkg.rel)
    if not res.build_ok:
        return Baseline(pkg=pkg.rel, build_ok=False, build_errors=list(res.build_errors),
                        timed_out=res.timed_out, runs=1, lang=lang)
    notes: list[str] = []
    if lang == "go":
        declared = _declared_tests(runner, pkg)
    else:
        declared, notes = await _declared(runner, pkg)
    verdicts: dict[str, str] = {}
    seen: set[str] = set()
    died = timed_out = False
    runs = 1
    while True:
        new = {n: v for n, v in res.tests.items() if v in _VERDICTS and n not in verdicts}
        verdicts.update(new)
        seen.update(res.tests)
        crashed = res.timed_out or res.returncode != 0
        died = died or crashed
        timed_out = timed_out or res.timed_out
        pending = sorted((seen | (declared if died else set())) - set(verdicts))
        if not pending or not crashed or not new or runs >= MAX_RUNS:
            break
        res = await runner.test(pkg.rel, run=pending)
        runs += 1
        if not res.build_ok:
            break
    return Baseline(
        pkg=pkg.rel, build_ok=True,
        passing=sorted(n for n, v in verdicts.items() if v == "pass"),
        failing=sorted(n for n, v in verdicts.items() if v == "fail"),
        timed_out=timed_out, incomplete=bool(pending), runs=runs,
        lang=lang, declared=sorted(declared) if lang != "go" else [], notes=notes,
    )


async def package_baseline(runner: GoRunner, pkg: PackageInfo, out_dir: Path,
                           force: bool = False) -> Baseline:
    """test verdicts of the package's own tests on the unmodified code (cached).

    a cached baseline whose run timed out is recomputed; tests cut short by a crashing
    test (panic, log.Fatal, os.Exit, -timeout) are re-run until each has its own verdict.
    """
    adapter = adapter_for(runner)
    lang = adapter.name
    path = baseline_path(out_dir, pkg) if lang == "go" else adapter.baseline_path(out_dir, pkg)
    toolchain = getattr(runner, "toolchain_failure", None) if lang != "go" else None
    if not force and path.exists():
        cached = _load(path)
        if cached is not None and not cached.timed_out and not (
                toolchain is not None and not cached.build_ok and
                toolchain(cached.build_errors)):
            return cached
    # non-Go: a configured test command (C/C++ --test-cmd) is a test even without test files
    test_cmd = getattr(runner, "test_cmd", "") if lang != "go" else ""
    has_tests = bool(pkg.all_test_files) or bool(test_cmd)
    if not has_tests:
        base = Baseline(pkg=pkg.rel, build_ok=True, lang=lang)
    else:
        t0 = time.monotonic()
        base = await _measure(runner, pkg)
        base.seconds = round(time.monotonic() - t0, 3)
        if lang != "go":
            base.notes = list(dict.fromkeys(base.notes + _unit_notes(runner, pkg)))
        note = "" if base.build_ok else " (build failed)"
        if base.timed_out:
            note += " (timed out)"
        if base.incomplete:
            note += " (incomplete)"
        level = logging.WARNING if base.timed_out or base.incomplete else logging.INFO
        where = (pkg.rel or ".") if lang == "go" else f"{lang}:{pkg.rel or '.'}"
        log.log(level, "baseline %s: %d passing, %d failing in %d run(s)%s", where,
                len(base.passing), len(base.failing), base.runs, note)
        if toolchain is not None and not base.build_ok and toolchain(base.build_errors):
            return base         # a toolchain problem, not the code's: never cached
    _save(path, base)
    return base
