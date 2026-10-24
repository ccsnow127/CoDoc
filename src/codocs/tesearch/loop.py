"""tesearch bounded loop: seed, refine, measure, deliver the best suite.

the language comes from the runner (adapter_for): Go calls the codocs.golang functions; other
languages call the same-named members of their adapter.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from codocs.config import Settings
from codocs.golang.gotool import GoRunner
from codocs.golang.mutate import mutation_sites, sample
from codocs.golang.package import (
    PackageInfo,
    Target,
    package_context,
    package_identifiers,
    test_reference,
)
from codocs.golang.source import extract_go_code
from codocs.lang.registry import adapter_for
from codocs.llm import LLMError
from codocs.tesearch.measure import Measurement, measure
from codocs.tesearch.prompts import refine_messages, seed_messages

log = logging.getLogger(__name__)

_REQUIRE_LINE_RE = re.compile(r"^\s*require\s+([^\s(]+)\s+\S+(.*)$")
_BLOCK_START_RE = re.compile(r"^\s*require\s*\(\s*$")


@dataclass
class SuiteRound:
    round: int
    suite: str
    measurement: Measurement
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"round": self.round, "suite": self.suite,
                "measurement": self.measurement.to_dict(), "seconds": self.seconds}

    @classmethod
    def from_dict(cls, d: dict) -> SuiteRound:
        return cls(round=int(d.get("round", 0)), suite=d.get("suite", ""),
                   measurement=Measurement.from_dict(d.get("measurement") or {}),
                   seconds=float(d.get("seconds", 0.0)))


@dataclass
class SuiteResult:
    pkg: str
    file: str
    stem: str
    status: str
    rounds: list[SuiteRound] = field(default_factory=list)
    best_round: int = 0
    best_score: float = 0.0
    best_cov: float = 0.0
    best_mut: float | None = None
    best_size: int = 0
    early_stop: bool = False
    threshold: float = 0.0
    max_rounds: int = 0
    error: str = ""
    lang: str = "go"
    cov_note: str = ""

    @property
    def score_trajectory(self) -> list[float]:
        return [r.measurement.score for r in self.rounds]

    def to_dict(self) -> dict:
        lang = {} if self.lang == "go" else {"lang": self.lang}
        note = {"cov_note": self.cov_note} if self.cov_note else {}
        return {
            "pkg": self.pkg, "file": self.file, "stem": self.stem, **lang, "status": self.status,
            "rounds": [r.to_dict() for r in self.rounds],
            "best_round": self.best_round, "best_score": self.best_score,
            "best_cov": self.best_cov, "best_mut": self.best_mut, "best_size": self.best_size,
            **note, "early_stop": self.early_stop, "threshold": self.threshold,
            "max_rounds": self.max_rounds, "error": self.error,
            "score_trajectory": self.score_trajectory,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SuiteResult:
        mut = d.get("best_mut")
        return cls(
            pkg=d.get("pkg", ""), file=d.get("file", ""), stem=d.get("stem", ""),
            status=d.get("status", ""),
            rounds=[SuiteRound.from_dict(r) for r in d.get("rounds") or []],
            best_round=int(d.get("best_round", 0)),
            best_score=float(d.get("best_score", 0.0)),
            best_cov=float(d.get("best_cov", 0.0)),
            best_mut=None if mut is None else float(mut),
            best_size=int(d.get("best_size", 0)),
            early_stop=bool(d.get("early_stop", False)),
            threshold=float(d.get("threshold", 0.0)),
            max_rounds=int(d.get("max_rounds", 0)),
            error=d.get("error", ""),
            lang=d.get("lang", "go") or "go",
            cov_note=d.get("cov_note", "") or "",
        )


def go_requires(module_root: Path) -> list[str]:
    """direct (non-indirect) module paths required by go.mod."""
    try:
        text = (Path(module_root) / "go.mod").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        if in_block:
            if line.strip().startswith(")"):
                in_block = False
                continue
            parts = line.split("//", 1)
            fields = parts[0].split()
            if len(fields) >= 2 and not (len(parts) > 1 and "indirect" in parts[1]):
                out.append(fields[0].strip('"'))
            continue
        if _BLOCK_START_RE.match(line):
            in_block = True
            continue
        m = _REQUIRE_LINE_RE.match(line)
        if m and "indirect" not in m.group(2):
            out.append(m.group(1).strip('"'))
    return list(dict.fromkeys(out))


def _rank(r: SuiteRound) -> tuple[float, bool, float, int, int]:
    m = r.measurement
    return (m.score, m.green_size > 0, m.cov, -m.green_size, -r.round)


def pick_best(rounds: list[SuiteRound], measured_only: bool = False) -> SuiteRound | None:
    """best round: score desc, then a non-empty green suite, higher coverage, smaller green
    suite, earlier round."""
    pool = [r for r in rounds if r.measurement.build_ok] if measured_only else list(rounds)
    return max(pool, key=_rank) if pool else None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class TeSearchLoop:
    """one target file -> the best of up to n measured suites."""

    def __init__(self, runner: GoRunner, llm, settings: Settings, out_dir: Path,
                 module_root: Path):
        self.runner = runner
        self.llm = llm
        self.settings = settings
        self.out_dir = Path(out_dir)
        self.module_root = Path(module_root)
        self._requires: list[str] | None = None
        self.lang = adapter_for(runner)

    @property
    def is_go(self) -> bool:
        return self.lang.name == "go"

    @property
    def requires(self) -> list[str]:
        if self._requires is None:
            self._requires = go_requires(self.module_root)
        return self._requires

    def work_dir(self, pkg: PackageInfo, stem: str) -> Path:
        return self.out_dir / "testgen" / pkg.dir_name / stem

    def result_path(self, pkg: PackageInfo, stem: str) -> Path:
        return self.work_dir(pkg, stem) / "result.json"

    def suite_basename(self, stem: str) -> str:
        return f"{stem}_codocs_test.go" if self.is_go else self.lang.suite_basename(stem)

    def tests_path(self, pkg: PackageInfo, stem: str) -> Path:
        return self.out_dir / "tests" / pkg.dir_name / self.suite_basename(stem)

    def load_result(self, pkg: PackageInfo, stem: str) -> SuiteResult | None:
        try:
            d = json.loads(self.result_path(pkg, stem).read_text(encoding="utf-8"))
            return SuiteResult.from_dict(d)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def write_result(self, pkg: PackageInfo, result: SuiteResult) -> None:
        _write(self.result_path(pkg, result.stem),
               json.dumps(result.to_dict(), indent=2, ensure_ascii=False))

    def _write_round(self, pkg: PackageInfo, stem: str, rnd: SuiteRound, system: str,
                     user: str, reply: str) -> None:
        d = self.work_dir(pkg, stem) / f"round_{rnd.round}"
        m = rnd.measurement
        _write(d / "prompt.txt", f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}\n")
        _write(d / "reply.txt", reply)
        ext = PurePosixPath(self.suite_basename(stem)).suffix or ".go"
        _write(d / f"suite{ext}", rnd.suite)
        _write(d / f"green{ext}", m.green_src)
        _write(d / "measure.json", json.dumps(m.to_dict(), indent=2, ensure_ascii=False))

    async def run_file(self, target: Target, pkg: PackageInfo) -> SuiteResult:
        s = self.settings
        n = max(1, int(s.test_rounds))
        threshold = float(s.test_threshold)
        lang = self.lang
        go = self.is_go
        oneshot = None if go else getattr(lang, "testgen_oneshot", None)
        if oneshot is not None:
            done = await oneshot(self, target, pkg)
            if done is not None:        # finished by the language itself: only persisted
                self.write_result(pkg, done)
                return done
        result = SuiteResult(pkg=target.pkg, file=target.file, stem=target.stem, status="ok",
                             threshold=threshold, max_rounds=n, lang=self.lang.name)
        extra_kw: dict = {}
        budget = None if go else getattr(lang, "test_max_tokens", None)
        if budget is not None:
            extra_kw["max_tokens"] = max(int(budget), int(s.max_tokens))
        if go:
            source = (self.module_root / target.file).read_text(encoding="utf-8",
                                                                errors="replace")
            mutants = sample(mutation_sites(source), s.mutants, s.mutant_seed)
            context = package_context(self.module_root, pkg, target.file)
            reference = test_reference(self.module_root, pkg, target.file)
            taken = package_identifiers(self.module_root, pkg)
            requires = self.requires
        else:
            source = lang.read(target.file, self.module_root)
            mutants = sample(lang.mutation_sites(source), s.mutants, s.mutant_seed)
            context = lang.package_context(self.module_root, pkg, target.file)
            reference = lang.test_reference(self.module_root, pkg, target.file)
            taken = lang.package_identifiers(self.module_root, pkg)
            requires = lang.allowed_third_party(self.module_root, pkg)
        tag = f"testgen {target.file}"
        for old in self.work_dir(pkg, target.stem).glob("round_*"):
            shutil.rmtree(old, ignore_errors=True)

        for r in range(1, n + 1):
            t0 = time.monotonic()
            if r == 1:
                seed = seed_messages if go else lang.test_seed_messages
                system, user = seed(target.file, pkg, source, context, taken, requires,
                                    reference)
            else:
                best = pick_best(result.rounds, measured_only=True)
                refine = refine_messages if go else lang.test_refine_messages
                system, user = refine(
                    target.file, pkg, source, context, taken, requires,
                    best.measurement if best else None, result.rounds[-1].measurement,
                    attempt=r, last_round=result.rounds[-1].round, reference=reference)
            try:
                reply = await self.llm.complete(system, user, tag=f"{tag} r{r}", **extra_kw)
            except LLMError as e:
                result.status, result.error = "error", str(e)
                log.warning("%s round %d: %s", tag, r, e)
                break
            if go:
                suite = extract_go_code(reply)
            else:
                suite = lang.normalize_suite(lang.extract_code(reply), pkg, target.file)
            m = await measure(self.runner, pkg, target.file, suite, mutants)
            rnd = SuiteRound(round=r, suite=suite, measurement=m,
                             seconds=round(time.monotonic() - t0, 3))
            result.rounds.append(rnd)
            self._write_round(pkg, target.stem, rnd, system, user, reply)
            log.info("%s round %d: build=%s size=%d green=%d cov=%.3f mut=%s score=%.3f", tag, r,
                     m.build_ok, m.size, m.green_size, m.cov,
                     "-" if m.mut is None else f"{m.mut:.3f}", m.score)
            if _toolchain_failed(self.runner, m.build_ok, m.build_errors):
                # not the suite's fault: no further rounds, and not resumed as finished
                result.status, result.error = "no-toolchain", m.build_errors[0][:500]
                log.warning("%s: %s", tag, result.error)
                break
            if m.score >= threshold:
                result.early_stop = r < n
                break

        self._deliver(pkg, result)
        self.write_result(pkg, result)
        return result

    def _deliver(self, pkg: PackageInfo, result: SuiteResult) -> None:
        best = pick_best(result.rounds)
        path = self.tests_path(pkg, result.stem)
        if best is None:
            path.unlink(missing_ok=True)
            return
        m = best.measurement
        result.best_round = best.round
        result.best_score = m.score
        result.best_cov = m.cov
        result.best_mut = m.mut
        result.best_size = m.green_size
        result.cov_note = m.cov_note
        if m.green_size > 0:
            _write(path, m.green_src)
            if self.lang.name in ("c", "cpp"):
                self._deliver_support()
            extra = None if self.is_go else getattr(self.lang, "deliver_extra", None)
            if extra is not None:
                extra(path, m.green_src)
        else:
            path.unlink(missing_ok=True)

    def _deliver_support(self) -> None:
        """C/C++ suites need the embedded test header and runner: OUT/tests/_support/."""
        from codocs.lang.cc import support_files

        dest = self.out_dir / "tests" / "_support"
        for name, text in support_files().items():
            p = dest / name
            if not p.is_file() or p.read_text(encoding="utf-8", errors="replace") != text:
                _write(p, text)


def _toolchain_failed(runner, build_ok: bool, errors: list[str]) -> bool:
    """a failed build caused by the toolchain (no JDK/mvn, ...), per the runner's own rule."""
    fn = getattr(runner, "toolchain_failure", None)
    return bool(fn is not None and not build_ok and errors and fn(errors))


async def _notify(on_progress, result: SuiteResult) -> None:
    if on_progress is None:
        return
    out = on_progress(result)
    if inspect.isawaitable(out):
        await out


async def run_testgen(targets: list[Target], pkgs: dict[str, PackageInfo], runner: GoRunner,
                      llm, settings: Settings, out_dir: Path, module_root: Path,
                      force: bool = False, on_progress=None) -> list[SuiteResult]:
    """tesearch over the targets, files in parallel, resuming finished ones."""
    loop = TeSearchLoop(runner, llm, settings, out_dir, module_root)
    sem = asyncio.Semaphore(max(1, int(settings.concurrency)))

    def skipped(t: Target, status: str, error: str = "") -> SuiteResult:
        return SuiteResult(pkg=t.pkg, file=t.file, stem=t.stem, status=status,
                           threshold=settings.test_threshold, max_rounds=settings.test_rounds,
                           error=error, lang=loop.lang.name)

    async def one(t: Target) -> SuiteResult:
        pkg = pkgs.get(t.pkg)
        if pkg is None:
            res = skipped(t, "error", f"package {t.pkg!r} not found")
        elif t.status != "ok":
            res = skipped(t, t.status)
            loop.write_result(pkg, res)
        else:
            prev = None if force else loop.load_result(pkg, t.stem)
            finished = getattr(loop.lang, "final_statuses", ("ok",)) if not loop.is_go \
                else ("ok",)
            if prev is not None and prev.status in finished:
                res = prev
            else:
                async with sem:
                    try:
                        res = await loop.run_file(t, pkg)
                    except Exception as e:
                        log.exception("testgen %s failed", t.file)
                        res = skipped(t, "error", f"{type(e).__name__}: {e}")
                        loop.write_result(pkg, res)
        await _notify(on_progress, res)
        return res

    return list(await asyncio.gather(*(one(t) for t in targets)))
