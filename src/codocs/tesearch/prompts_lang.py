"""tesearch message builders for non-Go languages (python, java, c, cpp).

the structure mirrors tesearch/prompts.py (the Go builders): the same [TARGET FILE],
[PACKAGE], [ALLOWED IMPORTS], [TAKEN NAMES], [PACKAGE CONTEXT], [EXISTING TEST CODE ...],
[TARGET SOURCE] / marked source and best/last blocks, with the language's comment token in
the inline marks and its fence around the code.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from codocs.specforge.prompts_lang import module_name, unit_name
from codocs.tesearch.prompts import (
    _build_errors,
    _first_line,
    _fmt_mut,
    mark_failing,
    mark_source,
    refine_system,
    seed_system,
)

if TYPE_CHECKING:
    from codocs.tesearch.measure import Measurement


def _suite_rel(adapter: Any, unit: Any, target_rel: str) -> str:
    return adapter.suite_rel(unit, adapter.stem(target_rel))


def _suite_class(adapter: Any, target_rel: str) -> str:
    return PurePosixPath(adapter.suite_basename(adapter.stem(target_rel))).stem


def package_line(adapter: Any, target_rel: str, unit: Any) -> str:
    """the [PACKAGE] block: what the suite is and how it is built."""
    suite = _suite_rel(adapter, unit, target_rel)
    lang = adapter.name
    if lang == "python":
        mod = module_name(adapter, target_rel, unit)
        return (f"module `{mod}`; import it as `from {mod} import ...`; the suite runs as "
                f"{suite} with no other test file of the project")
    if lang == "java":
        pkg = getattr(unit, "import_path", "") or unit_name(unit)
        return (f"`package {pkg};` class `{_suite_class(adapter, target_rel)}`, compiled as "
                f"{suite} with no other test class of the module")
    return (f"directory unit `{unit_name(unit)}`; the suite is one program compiled with the "
            f"unit's other sources as {suite}; the target is included through "
            "`#include CODOCS_TARGET`, so its static functions are reachable")


def _allowed(adapter: Any, third_party: list[str]) -> str:
    lang = adapter.name
    if lang == "python":
        base = "the Python standard library, `pytest`, and this project's modules"
        what = "third-party packages"
    elif lang == "java":
        base = ("the JDK, JUnit 5 Jupiter (`org.junit.jupiter.api`, `org.junit.jupiter.params`) "
                "and this project's classes")
        what = "third-party libraries"
    else:
        base = (f"the {adapter.display} standard library headers and the repository headers "
                "the target or its unit already include")
        what = "libraries"
    extra = (f"; and these {what}: " + ", ".join(third_party) if third_party
             else f"; no other {what}")
    return base + extra


def first_lines(adapter: Any, target_rel: str, unit: Any) -> str:
    """the required start of the suite, named in the final instruction."""
    lang = adapter.name
    if lang == "python":
        return f"the imports (the target as `from {module_name(adapter, target_rel, unit)} import ...`)"
    if lang == "java":
        pkg = getattr(unit, "import_path", "") or unit_name(unit)
        return f"`package {pkg};`"
    return '`#include "codocs_test.h"` and `#include CODOCS_TARGET`'


def _taken(adapter: Any, names: set[str]) -> list[str]:
    """taken names a suite following the helper-prefix rule could still collide with."""
    spec = adapter.prompts
    prefixes = {spec.test_helper_prefix, spec.test_helper_prefix.lstrip("_"), "Cdx", "cdx"}
    prefixes |= {"test", "Test"} if adapter.name == "python" else set()
    return sorted(n for n in names if n.startswith(tuple(p for p in prefixes if p)))


def _preamble(adapter: Any, target_rel: str, unit: Any, context: str, taken_names: set[str],
              third_party: list[str], reference: str = "") -> list[str]:
    spec = adapter.prompts
    taken = ", ".join(_taken(adapter, taken_names)) or "(none)"
    pre = spec.test_helper_prefix
    parts = [
        f"[TARGET FILE] {target_rel}",
        f"[PACKAGE] {package_line(adapter, target_rel, unit)}",
        f"[ALLOWED IMPORTS] {_allowed(adapter, third_party)}",
        f"[TAKEN NAMES - top-level identifiers starting with `{pre}` that the suite must not "
        f"redeclare]\n{taken}",
    ]
    if context.strip():
        parts.append(f"[PACKAGE CONTEXT - other declarations of the unit]\n{context.rstrip()}")
    if reference.strip():
        parts.append("[EXISTING TEST CODE OF THE PROJECT - reference only: these files are hidden "
                     "while your suite runs. They show how this codebase sets up its fixtures, "
                     "stubs and data; copy what you need into your file, renaming every top-level "
                     f"name you copy with the `{pre}` prefix]\n{reference.rstrip()}")
    return parts


def _ask(adapter: Any, target_rel: str, unit: Any, text: str) -> str:
    return (f"{text} Start with {first_lines(adapter, target_rel, unit)}. Complete file, in one "
            f"```{adapter.prompts.fence} block, no prose.")


def seed_messages(adapter: Any, target_rel: str, unit: Any, source: str, context: str,
                  taken_names: set[str], third_party: list[str],
                  reference: str = "") -> tuple[str, str]:
    """(system, user) asking for a first suite for a non-Go target."""
    parts = _preamble(adapter, target_rel, unit, context, taken_names, third_party, reference)
    parts.append(f"[TARGET SOURCE]\n{source.rstrip()}")
    parts.append(_ask(adapter, target_rel, unit, "Write the complete test file now."))
    return seed_system(adapter.name), "\n\n".join(parts)


def _cov(m: Measurement) -> str:
    note = getattr(m, "cov_note", "")
    return f"n/a ({note})" if note else f"{m.cov:.3f}"


def _source_header(mark: str) -> str:
    return ("[TARGET SOURCE - annotated with what the CURRENT BEST suite below still misses: "
            f"`{mark} <-- NOT COVERED YET` marks lines none of its passing tests execute; "
            f"`{mark} <-- SURVIVING MUTANT: <op>` marks lines where that mutation survives its "
            "assertions. Cover those lines, and assert exact values/branches around each "
            "surviving mutant so the mutation would fail]")


def _best_block(adapter: Any, unit: Any, target_rel: str, m: Measurement) -> str:
    header = (f"[CURRENT BEST SUITE - size={m.size} green={m.green_size} coverage={_cov(m)} "
              f"mutation={_fmt_mut(m)} (killed {m.killed} of {m.mutants_valid} valid mutants) "
              f"score={m.score:.3f}]")
    if m.failing:
        header += (f"\n({len(m.failing)} of its {m.size} tests FAIL - each marked inline below; "
                   f"the numbers above come from the {m.green_size} that pass)")
    src = m.suite_src or m.green_src
    lines = None
    if m.failing:
        try:
            lines = adapter.test_lines(src, _suite_rel(adapter, unit, target_rel))
        except Exception:
            lines = {}
    marked = mark_failing(src, m.failing, adapter.prompts.comment, lines=lines)
    return f"{header}\n{marked.rstrip()}"


def _last_block(m: Measurement, last_round: int) -> str:
    which = f"round {last_round}" if last_round else "the latest attempt"
    lines = [f"[LAST ATTEMPT ({which}) - it built but did NOT beat the current best: "
             f"size={m.size} green={m.green_size} coverage={_cov(m)} mutation={_fmt_mut(m)} "
             f"score={m.score:.3f}. Do not repeat it; take a different route to the gaps]"]
    for name, msg in list(m.failing.items())[:10]:
        lines.append(f"- {name} FAILED: {_first_line(msg)}")
    if len(m.failing) > 10:
        lines.append(f"- ... ({len(m.failing) - 10} more failing tests)")
    return "\n".join(lines)


def refine_messages(adapter: Any, target_rel: str, unit: Any, source: str, context: str,
                    taken_names: set[str], third_party: list[str], best: Measurement | None,
                    last: Measurement | None, *, attempt: int = 0, last_round: int = 0,
                    reference: str = "") -> tuple[str, str]:
    """(system, user) asking to improve the best suite, or to fix the last one's build."""
    mark = adapter.prompts.comment
    parts = _preamble(adapter, target_rel, unit, context, taken_names, third_party, reference)
    if attempt:
        parts.insert(0, f"[ROUND {attempt}]")
    if best is not None and (best.uncovered or best.survivors):
        parts.append(f"{_source_header(mark)}\n"
                     f"{mark_source(source, best.uncovered, best.survivors, mark).rstrip()}")
    else:
        parts.append(f"[TARGET SOURCE]\n{source.rstrip()}")
    if best is not None:
        parts.append(_best_block(adapter, unit, target_rel, best))
        if last is not None and last is not best and not last.build_ok:
            parts.append("[LAST ATTEMPT - did NOT BUILD; avoid these errors]\n"
                         f"{_build_errors(last.build_errors)}")
        elif last is not None and last is not best:
            parts.append(_last_block(last, last_round))
        ask = ("Write the improved complete test file now: keep the passing tests, repair the "
               "failing ones, close the marked gaps.")
    elif last is not None:
        parts.append("[LAST ATTEMPT - did NOT BUILD; build errors]\n"
                     f"{_build_errors(last.build_errors)}")
        parts.append(f"[LAST ATTEMPT SOURCE]\n{(last.suite_src or '').rstrip()}")
        ask = "Write the corrected complete test file now: fix every build error."
    else:
        parts.append("(no earlier attempt)")
        ask = "Write the complete test file now."
    parts.append(_ask(adapter, target_rel, unit, ask))
    return refine_system(adapter.name), "\n\n".join(parts)
