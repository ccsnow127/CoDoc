"""specforge Go message builders; the system prompts live in codocs/prompts/*.txt."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from codocs.golang.package import PackageInfo
from codocs.golang.source import Import, cgo_header
from codocs.specforge.docfmt import expected_sections

if TYPE_CHECKING:
    from codocs.specforge.loop import DocEval

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SEED = "specforge_seed_system"
CODEGEN = "specforge_codegen_system"
REFINE = "specforge_refine_system"

MAX_BUILD_ERROR_LINES = 40
MAX_FAILURE_CHARS = 6000
MAX_TAKEN_CHARS = 3000
MAX_LAST_CHARS = 3000
_PER_TEST_CHARS = 2000
_NOISE_RE = re.compile(r"^\s*=== (?:RUN|PAUSE|CONT|NAME)\b")
_MSG_LINE_RE = re.compile(r"\w+\.go:\d+:|panic:|Error Trace:|expected|got|want", re.I)
_SIGNALS = {
    "tests": "the package's existing tests that exercise this file",
    "generated-tests": "a generated unit-test suite for this file (the package's own tests hidden)",
    "compile": "compilation only (the package has no tests for this file); phi is 1 when it builds",
}


TEMPLATES_DIR = PROMPTS_DIR / "templates"


@lru_cache(maxsize=None)
def render_template(name: str, lang: str) -> str:
    """codocs/prompts/templates/<name>.txt with the slots of prompts/lang/<lang>.txt filled in;
    a language's full override prompts/lang/<lang>/<name>.txt takes the template's place.

    string.Template.substitute: a slot the fragment file lacks is an error (KeyError).
    """
    from string import Template

    from codocs.lang.base import prompt_spec

    override = PROMPTS_DIR / "lang" / lang / f"{name}.txt"
    path = override if override.is_file() else TEMPLATES_DIR / f"{name}.txt"
    text = path.read_text(encoding="utf-8")
    return Template(text).substitute(prompt_spec(lang).fragments)


@lru_cache(maxsize=None)
def load_prompt(name: str, lang: str = "go") -> str:
    """system prompt text: codocs/prompts/<name>.txt for Go, else the rendered template."""
    if lang != "go":
        return render_template(name, lang)
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def _fence(text: str, lang: str = "go") -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}{lang}\n{text.rstrip()}\n{ticks}"


def _what(target_rel: str, pkg: PackageInfo) -> str:
    return f"the Go file `{target_rel}` of package `{pkg.name}` (import path `{pkg.import_path}`)"


def _header(target_rel: str, pkg: PackageInfo) -> str:
    return f"# {target_rel}\n\n**Package**: `{pkg.name}` (`{pkg.import_path}`)"


def _import_block(allowed: list[Import]) -> str:
    if not allowed:
        return "(none: the original file imports nothing)"
    specs = [f'{i.alias} "{i.path}"' if i.alias else f'"{i.path}"' for i in allowed]
    return _fence("import (\n" + "".join(f"\t{s}\n" for s in specs) + ")")


def _context_block(context: str) -> str:
    if not context.strip():
        return "(no declarations in other files of this package)"
    return _fence(context)


_CGO_DOC_NOTE = (
    "\nThis file uses cgo. Its build constraints, C preamble and `import \"C\"` are handed to "
    "the code generator verbatim: do not transcribe the C code, but document how the Go code "
    "uses each `C.` symbol (names, argument conversions, ownership and freeing of C memory).\n"
)


def seed_messages(target_rel: str, pkg: PackageInfo, source: str, context: str
                  ) -> tuple[str, str]:
    """(system, user) asking for the first documentation of a file."""
    sections = "\n".join(f"- `## {k} {n}`" for k, n in expected_sections(source))
    cgo = _CGO_DOC_NOTE if cgo_header(source) else ""
    user = f"""Document {_what(target_rel, pkg)}.
{cgo}
Start the documentation with exactly these lines:

{_fence(_header(target_rel, pkg), "markdown")}

Required sections, in this order:
{sections or "- (none: describe the file in the opening paragraph)"}

## Package context (declared in OTHER files of the package; do not document as sections)
{_context_block(context)}

## Source of `{target_rel}`
{_fence(source)}
"""
    return load_prompt(SEED), user


def _taken_block(taken: set[str], context: str) -> str:
    """unexported package names not already shown in the context, within a char budget."""
    shown = set(re.findall(r"\w+", context))
    names = sorted(n for n in taken if not n[:1].isupper() and n not in shown)
    out: list[str] = []
    size = 0
    for n in names:
        if size + len(n) + 2 > MAX_TAKEN_CHARS:
            out.append("...")
            break
        out.append(n)
        size += len(n) + 2
    return ", ".join(out)


def codegen_messages(target_rel: str, pkg: PackageInfo, doc: str, context: str,
                     allowed_imports: list[Import], *, taken: set[str] | None = None,
                     cgo: bool = False) -> tuple[str, str]:
    """(system, user) asking to rebuild the file from its documentation alone.

    taken: package-level names of the other files (unexported ones not in the context are
    listed so helpers do not collide); cgo: the original cgo header is spliced in afterwards.
    """
    name = PurePosixPath(target_rel).name
    if cgo:
        allowed_imports = [i for i in allowed_imports if i.path != "C"]
    extra = ""
    names = _taken_block(taken or set(), context)
    if names:
        extra += ("\n## Other names already declared in this package (never declare these)\n"
                  f"{names}\n")
    if cgo:
        extra += ("\n## cgo\nThis file uses cgo. Its build constraints, C preamble and "
                  "`import \"C\"` are inserted verbatim above your imports: do not write them "
                  "and do not import \"C\" yourself; use the `C.` symbols as documented.\n")
    user = f"""Write the Go file `{name}` of package `{pkg.name}` (import path `{pkg.import_path}`).

## Package clause
{_fence(f"package {pkg.name}")}

## Allowed imports (use only these; omit unused ones)
{_import_block(allowed_imports)}

## Package context (already declared in other files of this package: do not redeclare, you may use)
{_context_block(context)}
{extra}
## Documentation of `{name}`
{doc.strip()}

Output the complete `{name}` in one ```go block.
"""
    return load_prompt(CODEGEN), user


def _clean_output(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not _NOISE_RE.match(ln)]
    return "\n".join(lines).strip()


def _informative(item: tuple[str, str]) -> tuple[int, int, str]:
    name, out = item
    hits = sum(1 for ln in out.splitlines() if _MSG_LINE_RE.search(ln))
    return (0 if hits else 1 if out else 2, len(out), name)


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: max(n - 20, 0)].rstrip() + "\n... (truncated)"


def render_evaluation(ev: DocEval) -> str:
    """phi, then build errors or failing tests with their output (most informative first)."""
    lines = [f"Signal: {ev.signal}: {_SIGNALS.get(ev.signal, ev.signal)}."]
    if ev.signal == "compile":
        lines.append(f"phi = {ev.phi:.2f} ({'builds' if ev.build_ok else 'does not build'})")
    else:
        lines.append(f"phi = {ev.phi:.2f} ({len(ev.passed)}/{len(ev.relevant)} tests passed)")
    if not ev.build_ok:
        errs = ev.build_errors[:MAX_BUILD_ERROR_LINES]
        lines += ["", "### Build failed: compiler errors", _fence("\n".join(errs) or "(no output)",
                                                                   "text")]
        if len(ev.build_errors) > len(errs):
            lines.append(f"({len(ev.build_errors) - len(errs)} more lines omitted)")
        return "\n".join(lines)
    if ev.passed:
        lines.append("Passed: " + ", ".join(ev.passed))
    if not ev.failing:
        return "\n".join(lines)
    lines += ["", "### Failing tests"]
    budget = MAX_FAILURE_CHARS
    omitted: list[str] = []
    items = sorted(((n, _clean_output(o)) for n, o in ev.failing.items()), key=_informative)
    for name, out in items:
        if budget < 200:
            omitted.append(name)
            continue
        body = _clip(out or "(no output)", min(_PER_TEST_CHARS, budget))
        budget -= len(body)
        lines += [f"#### {name}", _fence(body, "text")]
    if omitted:
        lines.append("Also failing (output omitted): " + ", ".join(omitted))
    return "\n".join(lines)


def _last_attempt(last: DocEval | None, last_round: int) -> str:
    if last is None:
        return ""
    which = f"round {last_round}" if last_round else "the latest attempt"
    return f"""
## Last attempt ({which}): its documentation did NOT improve on the current one
{_clip(render_evaluation(last), MAX_LAST_CHARS)}

Do not repeat that attempt: refine the current documentation above differently.
"""


def refine_messages(target_rel: str, pkg: PackageInfo, source: str, doc: str, code: str,
                    evaluation: DocEval, missing: list[str], *, last: DocEval | None = None,
                    last_round: int = 0, attempt: int = 0) -> tuple[str, str]:
    """(system, user) asking for one diagnose-and-rewrite pass over the documentation.

    `attempt` and the evaluation of the last round when it was not the best make each round's
    prompt distinct, so a round that did not improve is never replayed from the llm cache.
    """
    if missing:
        gaps = "These required sections are missing; add them:\n" + "\n".join(
            f"- `## {m}`" for m in missing)
    else:
        gaps = "(none)"
    head = f"Round {attempt}. " if attempt else ""
    cgo = _CGO_DOC_NOTE if cgo_header(source) else ""
    user = f"""{head}Refine the documentation of {_what(target_rel, pkg)}.
{cgo}
Keep the documentation starting with exactly these lines:

{_fence(_header(target_rel, pkg), "markdown")}

## Reference implementation (original `{target_rel}`)
{_fence(source)}

## Current documentation
{_fence(doc, "markdown")}

## Generated code (written from the current documentation only)
{_fence(code)}

## Evaluation
{render_evaluation(evaluation)}

## Missing sections
{gaps}
{_last_attempt(last, last_round)}
Reply with the complete revised documentation inside <documentation>...</documentation>.
"""
    return load_prompt(REFINE), user
