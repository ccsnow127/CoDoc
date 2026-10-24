"""tesearch Go message builders: inline gap marks, seed and refine prompts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from codocs.golang.package import PackageInfo
from codocs.golang.source import top_level_decls

if TYPE_CHECKING:
    from codocs.tesearch.measure import Measurement

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
MAX_BUILD_ERRORS = 40
FAIL_MSG_CHARS = 300

_SOURCE_HEADER_MARKED = (
    "[TARGET SOURCE - annotated with what the CURRENT BEST suite below still misses: "
    "`// <-- NOT COVERED YET` marks lines none of its passing tests execute; "
    "`// <-- SURVIVING MUTANT: <op>` marks lines where that mutation survives its assertions. "
    "Cover those lines, and assert exact values/branches around each surviving mutant so the "
    "mutation would fail]"
)


def _load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").rstrip("\n")


def _system(name: str, lang: str) -> str:
    if lang == "go":
        return _load(f"{name}.txt")
    from codocs.specforge.prompts import render_template

    return render_template(name, lang).rstrip("\n")


def seed_system(lang: str = "go") -> str:
    return _system("tesearch_seed_system", lang)


def refine_system(lang: str = "go") -> str:
    return _system("tesearch_refine_system", lang)


def suite_path(pkg: PackageInfo, stem: str) -> str:
    """rel path of the generated suite next to the target."""
    name = f"{stem}_codocs_test.go"
    return f"{pkg.rel}/{name}" if pkg.rel else name


def _join_lines(lines: list[str], src: str) -> str:
    out = "\n".join(lines)
    return out + "\n" if src.endswith("\n") else out


def mark_source(src: str, uncovered: list[int], survivors: list[dict],
                comment: str = "//") -> str:
    """append gap marks to the lines they refer to (bare brace lines are never marked uncovered)."""
    marks: dict[int, list[str]] = {}

    def add(line: int, text: str) -> None:
        bucket = marks.setdefault(int(line), [])
        if text not in bucket:
            bucket.append(text)

    lines = src.splitlines()
    for n in uncovered or ():
        if 0 < n <= len(lines) and lines[n - 1].strip(" \t{}()") == "":
            continue
        add(n, "NOT COVERED YET")
    for s in survivors or ():
        if s.get("line"):
            add(s["line"], f"SURVIVING MUTANT: {s.get('operator') or 'mutation'}")
    for i, ln in enumerate(lines, start=1):
        if i in marks:
            lines[i - 1] = f"{ln}  {comment} <-- {'; '.join(marks[i])}"
    return _join_lines(lines, src)


def _first_line(msg: str) -> str:
    line = next((ln.strip() for ln in (msg or "").splitlines() if ln.strip()), "test failed")
    if len(line) > FAIL_MSG_CHARS:
        line = line[: FAIL_MSG_CHARS - 3] + "..."
    return line


def mark_failing(suite_src: str, failing: dict[str, str], comment: str = "//", *,
                 decls=None, lines: dict[str, int] | None = None) -> str:
    """append a FAILING mark with the first message line to each failing test's func line.

    test lines come from `lines` (test name -> line, non-Go: adapter.test_lines), else from
    `decls` (default: Go's top_level_decls of the suite).
    """
    if not failing:
        return suite_src
    if lines is not None:
        marks = {ln: _first_line(failing[n]) for n, ln in lines.items() if n in failing}
    else:
        marks = {
            d.line: _first_line(failing[d.name])
            for d in (top_level_decls(suite_src) if decls is None else decls)
            if d.kind == "func" and d.name in failing
        }
    out = suite_src.splitlines()
    for i, ln in enumerate(out, start=1):
        if i in marks:
            out[i - 1] = f"{ln}  {comment} <-- FAILING: {marks[i]}"
    return _join_lines(out, suite_src)


def module_path(pkg: PackageInfo) -> str:
    """the module path, from the package's import path and its dir."""
    rel = pkg.rel.strip("/")
    if not rel:
        return pkg.import_path
    suffix = "/" + rel
    return pkg.import_path[: -len(suffix)] if pkg.import_path.endswith(suffix) else ""


def _taken(names: set[str]) -> list[str]:
    """taken names a suite following the `cdx` rule could still collide with."""
    return sorted(n for n in names if n.startswith(("cdx", "Test")))


def _preamble(target_rel: str, pkg: PackageInfo, context: str, taken_names: set[str],
              requires: list[str], reference: str = "") -> list[str]:
    stem = target_rel.rsplit("/", 1)[-1].removesuffix(".go")
    module = module_path(pkg)
    own = f"; the packages of this module (`{module}/...`)" if module else ""
    allowed = ("; and these third-party modules: " + ", ".join(requires) if requires
               else "; no third-party modules")
    taken = ", ".join(_taken(taken_names)) or "(none)"
    parts = [
        f"[TARGET FILE] {target_rel}",
        f"[PACKAGE] `package {pkg.name}` ({pkg.import_path}); the suite is compiled as "
        f"{suite_path(pkg, stem)} with no other _test.go file of the package",
        f"[ALLOWED IMPORTS] the Go standard library{own}{allowed}",
        "[TAKEN NAMES - package-level identifiers starting with `cdx` or `Test` that the suite "
        f"must not redeclare]\n{taken}",
    ]
    if context.strip():
        parts.append(f"[PACKAGE CONTEXT - other declarations of the package]\n{context.rstrip()}")
    if reference.strip():
        parts.append("[EXISTING TEST CODE OF THE PACKAGE - reference only: these files are hidden "
                     "while your suite runs. They show how this codebase stubs its collaborators "
                     "and sets up loggers, memory and globals; copy what you need into your file, "
                     "renaming every package-level name you copy with the `cdx` prefix]\n"
                     f"{reference.rstrip()}")
    return parts


def seed_messages(target_rel: str, pkg: PackageInfo, source: str, context: str,
                  taken_names: set[str], requires: list[str],
                  reference: str = "") -> tuple[str, str]:
    """(system, user) asking for a first suite."""
    parts = _preamble(target_rel, pkg, context, taken_names, requires, reference)
    parts.append(f"[TARGET SOURCE]\n{source.rstrip()}")
    parts.append(f"Write the complete test file now, starting with `package {pkg.name}`. "
                 "Complete file, no prose, no fences.")
    return seed_system(), "\n\n".join(parts)


def _fmt_mut(m: Measurement) -> str:
    return "n/a" if m.mut is None else f"{m.mut:.3f}"


def _build_errors(errors: list[str]) -> str:
    lines = list(errors[:MAX_BUILD_ERRORS]) or ["(no compiler message captured)"]
    if len(errors) > MAX_BUILD_ERRORS:
        lines.append(f"... ({len(errors) - MAX_BUILD_ERRORS} more)")
    return "\n".join(lines)


def _best_block(m: Measurement) -> str:
    header = (f"[CURRENT BEST SUITE - size={m.size} green={m.green_size} coverage={m.cov:.3f} "
              f"mutation={_fmt_mut(m)} (killed {m.killed} of {m.mutants_valid} valid mutants) "
              f"score={m.score:.3f}]")
    if m.failing:
        header += (f"\n({len(m.failing)} of its {m.size} tests FAIL - each marked inline below; "
                   f"the numbers above come from the {m.green_size} that pass)")
    src = m.suite_src or m.green_src
    return f"{header}\n{mark_failing(src, m.failing).rstrip()}"


def _last_block(m: Measurement, last_round: int) -> str:
    which = f"round {last_round}" if last_round else "the latest attempt"
    lines = [f"[LAST ATTEMPT ({which}) - it built but did NOT beat the current best: "
             f"size={m.size} green={m.green_size} coverage={m.cov:.3f} mutation={_fmt_mut(m)} "
             f"score={m.score:.3f}. Do not repeat it; take a different route to the gaps]"]
    for name, msg in list(m.failing.items())[:10]:
        lines.append(f"- {name} FAILED: {_first_line(msg)}")
    if len(m.failing) > 10:
        lines.append(f"- ... ({len(m.failing) - 10} more failing tests)")
    return "\n".join(lines)


def refine_messages(target_rel: str, pkg: PackageInfo, source: str, context: str,
                    taken_names: set[str], requires: list[str], best: Measurement | None,
                    last: Measurement | None, *, attempt: int = 0,
                    last_round: int = 0, reference: str = "") -> tuple[str, str]:
    """(system, user) asking to improve the best suite, or to fix the last one's build.

    `attempt` (this round's number) and the last non-best attempt make each round's prompt
    distinct, so a round that did not improve is never replayed from the llm cache.
    """
    parts = _preamble(target_rel, pkg, context, taken_names, requires, reference)
    if attempt:
        parts.insert(0, f"[ROUND {attempt}]")
    if best is not None and (best.uncovered or best.survivors):
        parts.append(f"{_SOURCE_HEADER_MARKED}\n"
                     f"{mark_source(source, best.uncovered, best.survivors).rstrip()}")
    else:
        parts.append(f"[TARGET SOURCE]\n{source.rstrip()}")
    if best is not None:
        parts.append(_best_block(best))
        if last is not None and last is not best and not last.build_ok:
            parts.append("[LAST ATTEMPT - did NOT COMPILE; avoid these errors]\n"
                         f"{_build_errors(last.build_errors)}")
        elif last is not None and last is not best:
            parts.append(_last_block(last, last_round))
        ask = ("Write the improved complete test file now: keep the passing tests, repair the "
               "failing ones, close the marked gaps.")
    elif last is not None:
        parts.append("[LAST ATTEMPT - did NOT COMPILE; build errors]\n"
                     f"{_build_errors(last.build_errors)}")
        parts.append(f"[LAST ATTEMPT SOURCE]\n{(last.suite_src or '').rstrip()}")
        ask = "Write the corrected complete test file now: fix every build error."
    else:
        parts.append("(no earlier attempt)")
        ask = "Write the complete test file now."
    parts.append(f"{ask} Start with `package {pkg.name}`. Complete file, no prose, no fences.")
    return refine_system(), "\n\n".join(parts)
