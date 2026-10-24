"""`codocs testrun`: re-run every delivered test suite of OUT without an LLM.

the analysis (languages, selection, COBOL scope) is restored from OUT/analysis.json and the
toolchain options from OUT/run_meta.json. COBOL bundles run through the GnuCOBOL harness and
report the COBOL summary metrics (summarize / summary_md); every other language re-runs its
suite with the unit's own tests hidden, as measure() does.

outputs: OUT/testrun/<lang>/<pkg>/<stem>.json and OUT/testrun/summary.{json,md}.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from codocs.cobol.bundle import mode_label
from codocs.cobol.classify import spec_kind
from codocs.cobol.testmodels import GeneratedTest, TestResult

log = logging.getLogger(__name__)

_EXECUTABLE = ("unit", "batch")
# run_meta.json settings restored for a re-run (toolchain options only)
TOOLCHAIN_KEYS = ("go", "go_env", "go_flags", "go_timeout", "test_timeout", "py", "pytest_args",
                  "java_home", "mvn", "java_runner", "jacoco_version", "cc", "cxx", "cc_flags",
                  "ld_flags", "cc_db", "cc_root", "build_cmd", "test_cmd", "gcov", "cobc",
                  "cobc_flags", "cobol_coverage", "include_generated", "sandbox")


# -- the COBOL summary --------------------------------------------------------------------


def summarize(tests: dict[str, GeneratedTest], results: dict[str, TestResult] | None) -> dict:
    """counts by mode/status, compile rate, pass rate over executed cases, mean coverage.

    weak cases (a PASS printed without checking anything) are counted apart: they are
    neither passed nor failed checks. results=None: nothing was executed."""
    results = results or {}
    modes = Counter(t.mode for t in tests.values())
    statuses = Counter(r.status for r in results.values())
    executable = [results[p] for p, t in tests.items()
                  if p in results and t.mode in _EXECUTABLE
                  and results[p].status not in ("generation-error", "skipped")]
    compiled = [r for r in executable if r.compiled]
    passed = sum(r.passed for r in compiled)
    failed = sum(r.failed for r in compiled)
    covered = [r for r in compiled if r.paragraphs_total]
    cov = [r.paragraphs_hit / r.paragraphs_total for r in covered]
    by_mode = {}
    for mode in sorted(modes):
        rs = [results[p] for p, t in tests.items() if t.mode == mode and p in results]
        by_mode[mode] = {
            "programs": modes[mode],
            "statuses": dict(Counter(r.status for r in rs)),
            "compiled": sum(r.compiled for r in rs),
            "cases_passed": sum(r.passed for r in rs),
            "cases_failed": sum(r.failed for r in rs),
            "cases_weak": sum(r.weak for r in rs),
        }
    weak = [r for r in compiled if r.weak]
    spec_kinds = Counter(spec_kind(t.testability) or "unknown"
                         for t in tests.values() if t.mode == "spec")
    return {
        "programs": len(tests),
        "modes": dict(modes),
        "spec_kinds": dict(spec_kinds),
        "statuses": dict(statuses),
        "generation_errors": sum(1 for t in tests.values() if t.error),
        "spec_cases": sum(len(t.cases) for t in tests.values() if t.mode == "spec"),
        "executable": len(executable),
        "compiled": len(compiled),
        "compile_rate": round(len(compiled) / len(executable), 3) if executable else None,
        "programs_passed": sum(r.status == "passed" for r in executable),
        "cases_passed": passed,
        "cases_failed": failed,
        "case_pass_rate": round(passed / (passed + failed), 3) if passed + failed else None,
        "cases_weak": sum(r.weak for r in compiled),
        "programs_with_weak_cases": len(weak),
        "programs_only_weak": sum(r.status == "weak" for r in executable),
        "stubbed_callees": sum(len(r.stubbed_callees) for r in compiled),
        "mean_paragraph_coverage": round(sum(cov) / len(cov), 3) if cov else None,
        "by_mode": by_mode,
    }


def summary_md(tests: dict[str, GeneratedTest], results: dict[str, TestResult] | None,
               summary: dict, no_toolchain: set[str] | frozenset[str] = frozenset()) -> str:
    """the COBOL summary.md table (one row per program); *no_toolchain*: programs not
    run because cobc is missing (status no-toolchain instead of not run)."""
    results = results or {}

    def pct(v) -> str:
        return "-" if v is None else f"{v * 100:.1f}%"

    labels = Counter(mode_label(t) for t in tests.values())
    modes = ", ".join(f"{k} {v}" for k, v in sorted(labels.items()))
    lines = [
        "# Generated tests",
        "",
        f"- programs: {summary['programs']} ({modes})",
        f"- executable: {summary['executable']}, compiled: {summary['compiled']} "
        f"({pct(summary['compile_rate'])})",
        f"- programs passing all checks: {summary['programs_passed']}",
        f"- executed cases: {summary['cases_passed']} passed, {summary['cases_failed']} failed "
        f"({pct(summary['case_pass_rate'])})",
        f"- weak cases (PASS printed without a check; not counted as passed): "
        f"{summary['cases_weak']} in {summary['programs_with_weak_cases']} programs",
        f"- mean paragraph coverage: {pct(summary['mean_paragraph_coverage'])}",
    ]
    if summary.get("mean_statement_coverage") is not None:
        lines.append(f"- mean statement coverage: {pct(summary['mean_statement_coverage'])}")
    if summary.get("no_toolchain"):
        lines.append(f"- not run, cobc not available ({summary.get('cobc') or 'cobc'}): "
                     f"{summary['no_toolchain']} unit/batch programs")
    lines += [
        f"- spec cases (not executable): {summary['spec_cases']}",
        "",
        "| program | mode | status | cases (pass/fail/total) | paragraphs hit | note |",
        "|---|---|---|---|---|---|",
    ]
    for pid in sorted(tests):
        t = tests[pid]
        r = results.get(pid)
        if r is None:
            status = "generation-error" if t.error else \
                ("not executable" if t.mode == "spec" else
                 "no-toolchain" if pid in no_toolchain else "not run")
            cases, cov = f"-/-/{len(t.cases)}", "-"
        else:
            status = r.status
            cases = f"{r.passed}/{r.failed}/{len(r.cases)}"
            cov = f"{r.paragraphs_hit}/{r.paragraphs_total}" if r.paragraphs_total else "-"
        note = (t.error or (t.testability.reasons[0] if t.testability and
                            t.testability.reasons else "")).replace("|", "/")[:80]
        extra = []
        if r is not None and r.weak:
            extra.append(f"{r.weak} weak")
        if r is not None and r.stubbed_callees:
            extra.append("stubbed: " + ", ".join(r.stubbed_callees))
        if extra:
            note = "; ".join(extra) + (f"; {note}" if note else "")
        lines.append(f"| {pid} | {mode_label(t)} | {status} | {cases} | {cov} | {note} |")
    return "\n".join(lines) + "\n"


# -- the re-run ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def restore_settings(settings, out_dir: Path, *, keep: tuple[str, ...] = ()):
    """settings for a re-run: the languages, selection-independent COBOL scope of
    OUT/analysis.json and the toolchain options of OUT/run_meta.json (names in *keep*, set
    on the command line, win)."""
    from codocs.config import Settings

    analysis = _read_json(Path(out_dir) / "analysis.json")
    meta = _read_json(Path(out_dir) / "run_meta.json").get("settings") or {}
    defaults = Settings()
    over: dict = {}
    for key in TOOLCHAIN_KEYS:
        if key in keep or key not in meta or not hasattr(defaults, key):
            continue
        if getattr(settings, key) == getattr(defaults, key):
            over[key] = meta[key]
    langs = analysis.get("languages")
    if isinstance(langs, list) and langs:
        over["source_lang"] = ",".join(str(x) for x in langs)
    scope = (analysis.get("cobol") or {}).get("scope") or {}
    if isinstance(scope, dict) and "exclude_paths" in scope:
        over["exclude_paths"] = "\n".join(scope.get("exclude_paths") or [])
    return dataclasses.replace(settings, **over)


def _selection(analysis: dict):
    from codocs.pipeline import Selection

    sel = analysis.get("selection") or {}
    scope = (analysis.get("cobol") or {}).get("scope") or {}
    subtree = scope.get("subtree") if "subtree" in scope else sel.get("subtree")
    return Selection(subtree=subtree or None, packages=sel.get("packages") or None,
                     files=sel.get("files"), max_files=sel.get("max_files"),
                     programs=sel.get("programs") or None)


def _suite_rows(out_dir: Path) -> list[dict]:
    rows = []
    base = out_dir / "testgen"
    if not base.is_dir():
        return rows
    for path in sorted(base.rglob("result.json")):
        d = _read_json(path)
        if d.get("file") and d.get("stem"):
            rows.append(d)
    return rows


def _statement_coverage(harness, rel: str, res: TestResult) -> float | None:
    hits = res.line_hits.get(rel) or {}
    if not hits:
        return None
    try:
        universe = harness.statement_lines(rel)
    except Exception:          # a file that does not translate has no reliable universe
        return None
    if not universe:
        return None
    seen = {int(k) for k, v in hits.items() if v}
    return round(len(universe & seen) / len(universe), 4)


async def _run_cobol(runner, ad, row: dict, text: str, work: Path, sem: asyncio.Semaphore
                     ) -> tuple[str, GeneratedTest, TestResult | None, dict]:
    from codocs.cobol import bundle

    target = row["file"]
    b = bundle.parse_bundle(text)
    try:
        testability = ad.testability(target)
    except (KeyError, ValueError):
        testability = None
    test = bundle.to_generated(b if b is not None else text, testability)
    pid = test.program_id or target
    info: dict = {"lang": "cobol", "pkg": row.get("pkg", ""), "file": target,
                  "stem": row["stem"], "program": pid, "mode": test.mode}
    if test.mode == "spec" or not runner.cobc_ok():
        info["status"] = "not executable" if test.mode == "spec" else "no-toolchain"
        return pid, test, None, info
    trace = getattr(runner, "coverage_mode", "statement")
    async with sem:
        d = Path(tempfile.mkdtemp(prefix="r", dir=work))
        try:
            res = await asyncio.to_thread(runner.harness.run, test, trace=trace, work=d)
        finally:
            if os.environ.get("CODOCS_KEEP_WORK") != "1":
                shutil.rmtree(d, ignore_errors=True)
    stmt = _statement_coverage(runner.harness, target, res) if trace == "statement" else None
    info.update(status=res.status, compiled=res.compiled, passed=res.passed, failed=res.failed,
                weak=res.weak, cases=[c.model_dump() for c in res.cases],
                paragraphs_hit=res.paragraphs_hit, paragraphs_total=res.paragraphs_total,
                statement_coverage=stmt, return_code=res.return_code,
                stubbed_callees=list(res.stubbed_callees),
                run_log=res.run_log[-2000:], compile_log=res.compile_log[-2000:])
    return pid, test, res, info


async def _run_generic(runner, lang: str, pkg, row: dict, text: str, sem: asyncio.Semaphore
                       ) -> dict:
    from codocs.lang.registry import adapter_for
    from codocs.tesearch.measure import _Runs, file_coverage

    ad = adapter_for(runner)
    target = row["file"]
    runs = _Runs(runner, pkg, target, ad.default_allowed(runner, pkg) if lang != "go" else [],
                 ad)
    async with sem:
        res = await runner.test(pkg.rel, overlay=runs.overlay(text), cover=True)
    try:
        src = ad.read(target, runner.module_root) if lang != "go" else \
            (Path(runner.module_root) / target).read_text(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        src = ""
    cov, _ = file_coverage(res.coverage or [], target, src or None)
    verdicts = {k: getattr(v, "status", v) if not isinstance(v, str) else v
                for k, v in (res.tests or {}).items()}
    passed = sum(1 for v in verdicts.values() if str(v) in ("pass", "passed", "ok"))
    return {"lang": lang, "pkg": row.get("pkg", ""), "file": target, "stem": row["stem"],
            "suite": runs.suite_rel,
            "build_ok": bool(res.build_ok), "build_errors": list(res.build_errors)[:20],
            "tests": {k: str(v) for k, v in sorted(verdicts.items())}, "passed": passed,
            "failed": len(verdicts) - passed, "coverage": None if cov is None else round(cov, 4),
            "timed_out": bool(getattr(res, "timed_out", False))}


def _lang_summary(infos: list[dict]) -> dict:
    covs = [i["coverage"] for i in infos if i.get("coverage") is not None]
    return {"suites": len(infos), "build_ok": sum(1 for i in infos if i.get("build_ok")),
            "tests_passed": sum(i.get("passed", 0) for i in infos),
            "tests_failed": sum(i.get("failed", 0) for i in infos),
            "mean_coverage": round(sum(covs) / len(covs), 4) if covs else None}


def _overall_md(summary: dict) -> str:
    lines = ["# Test re-run", ""]
    by = summary.get("by_language") or {}
    if by:
        lines += ["| language | suites | built | tests passed | tests failed | mean coverage |",
                  "|---|---|---|---|---|---|"]
        for lang, s in by.items():
            cov = "-" if s.get("mean_coverage") is None else f"{s['mean_coverage'] * 100:.1f}%"
            lines.append(f"| {lang} | {s.get('suites')} | {s.get('build_ok')} | "
                         f"{s.get('tests_passed')} | {s.get('tests_failed')} | {cov} |")
        lines.append("")
    return "\n".join(lines)


async def run_testrun(out_dir: str | Path, settings, repo: str | Path | None = None,
                      programs: list[str] | None = None, *,
                      on_progress: Callable[..., None] | None = None) -> dict:
    """re-run every delivered suite in OUT/tests; returns the summary (summary.json).

    raises FileNotFoundError without OUT/analysis.json, ValueError without delivered suites."""
    from codocs import pipeline as pl
    from codocs.lang.registry import adapter

    out_dir = Path(out_dir).resolve()
    analysis = _read_json(out_dir / "analysis.json")
    if not analysis:
        raise FileNotFoundError(f"{out_dir / 'analysis.json'} not found "
                                "(run codocs analyze, testgen or run into this OUT first)")
    root = Path(repo).expanduser().resolve() if repo else Path(analysis["module_root"])
    if not root.is_dir():
        raise FileNotFoundError(f"repository {root} not found (pass --repo)")
    rows = _suite_rows(out_dir)
    langs = analysis.get("languages") or ["go"]
    runners = pl.make_runners(root, settings, out_dir, list(langs))
    sel = _selection(analysis)
    ma = await pl.analyze_all(runners, settings, out_dir, sel, write=False)
    wanted_files: set[str] | None = None
    if programs:
        from codocs.cobol.select import program_files, select_programs

        if "cobol" not in ma.languages:
            raise ValueError("--programs selects COBOL programs, but OUT has no COBOL analysis")
        index = adapter("cobol", runners.repo_ctx).index(sel.subtree)
        wanted_files = set(program_files(index, select_programs(index, programs) or []))
    usable = dict(runners.usable())
    jobs: list = []
    sem = asyncio.Semaphore(max(1, int(getattr(settings, "workers", 4) or 4)))
    work = out_dir / "work" / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    await runners.run_ctx.ensure_synced()
    for row in rows:
        lang = row.get("lang") or "go"
        if lang not in usable or lang not in ma.parts:
            continue
        if wanted_files is not None and (lang != "cobol" or row["file"] not in wanted_files):
            continue
        part = ma.parts[lang]
        pkg = part.pkgs.get(row.get("pkg", ""))
        if pkg is None:
            continue
        runner = usable[lang]
        if lang == "go":
            suite = out_dir / "tests" / pkg.dir_name / f"{row['stem']}_codocs_test.go"
        else:
            ad = adapter(lang, runners.repo_ctx)
            suite = out_dir / "tests" / pkg.dir_name / ad.suite_basename(row["stem"])
        if not suite.is_file() and lang == "cobol" and row.get("status") == "spec":
            # spec plans are not delivered as suites: their bundle stays in round_1
            suite = out_dir / "testgen" / pkg.dir_name / row["stem"] / "round_1" / "suite.cbt"
        if not suite.is_file():
            continue
        text = suite.read_text(encoding="utf-8", errors="replace")
        if lang == "cobol":
            jobs.append(_run_cobol(runner, adapter("cobol", runners.repo_ctx), row, text, work,
                                   sem))
        else:
            jobs.append(_run_generic(runner, lang, pkg, row, text, sem))
    if not jobs:
        raise ValueError(f"no delivered test suites to run in {out_dir / 'tests'}")
    done = await asyncio.gather(*jobs)
    tests: dict[str, GeneratedTest] = {}
    results: dict[str, TestResult] = {}
    infos: dict[str, list[dict]] = {}
    for item in done:
        if isinstance(item, tuple):
            pid, test, res, info = item
            tests[pid] = test
            if res is not None:
                results[pid] = res
        else:
            info = item
        infos.setdefault(info["lang"], []).append(info)
        pkg_dir = info.get("pkg") or "_root"
        _write(out_dir / "testrun" / info["lang"] / pkg_dir / f"{info['stem']}.json",
               json.dumps(info, indent=2, ensure_ascii=False) + "\n")
        if on_progress:
            on_progress(info["file"])
    summary: dict = {"suites": sum(len(v) for v in infos.values()),
                     "by_language": {}}
    for lang in [x for x in pl.ALL_LANGS if x in infos]:
        if lang == "cobol":
            covs = [i["statement_coverage"] for i in infos[lang]
                    if i.get("statement_coverage") is not None]
            cob = summarize(tests, results)
            cob["mean_statement_coverage"] = round(sum(covs) / len(covs), 3) if covs else None
            nt = {i["program"] for i in infos[lang] if i.get("status") == "no-toolchain"}
            if nt:
                cob["no_toolchain"] = len(nt)
                cob["cobc"] = getattr(settings, "cobc", "") or "cobc"
            summary["cobol"] = cob
            summary["by_language"]["cobol"] = {
                "suites": len(infos[lang]), "build_ok": cob["compiled"],
                "tests_passed": cob["cases_passed"], "tests_failed": cob["cases_failed"],
                "mean_coverage": cob["mean_statement_coverage"]}
        else:
            summary["by_language"][lang] = _lang_summary(infos[lang])
    md = _overall_md(summary)
    if "cobol" in summary:
        nt = {i["program"] for i in infos["cobol"] if i.get("status") == "no-toolchain"}
        md += "\n" + summary_md(tests, results, summary["cobol"], nt)
    _write(out_dir / "testrun" / "summary.json",
           json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    _write(out_dir / "testrun" / "summary.md", md)
    return summary
