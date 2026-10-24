"""specforge message builders for non-Go languages (python, java, c, cpp).

the structure mirrors specforge/prompts.py (the Go builders); every language fact
comes from the adapter: its PromptSpec (fence, file noun, header label), its decls (required
sections), its preamble and format_import. the system prompts are the rendered templates
(load_prompt(name, lang)).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from codocs.specforge.docfmt import expected_sections
from codocs.specforge.prompts import (
    CODEGEN,
    MAX_TAKEN_CHARS,
    REFINE,
    SEED,
    _clip,
    load_prompt,
    render_evaluation,
)

if TYPE_CHECKING:
    from codocs.specforge.loop import DocEval

MAX_LAST_CHARS = 3000


def _fence(text: str, tag: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}{tag}\n{text.rstrip()}\n{ticks}"


def module_name(adapter: Any, target_rel: str, unit: Any) -> str:
    """python: the dotted module of the target (`pymod.calc`; a package's `__init__`: `pymod`)."""
    fn = getattr(adapter, "module_name", None)
    if fn is not None and getattr(adapter, "ctx", None) is not None:
        try:
            name = fn(target_rel)
        except Exception:
            name = ""
        if name:
            return name
    stem = PurePosixPath(target_rel).stem
    base = getattr(unit, "import_path", "") or ""
    if stem == "__init__":
        return base or PurePosixPath(target_rel).parent.name
    return f"{base}.{stem}" if base else stem


def java_module(unit: Any) -> str:
    """the Maven artifactId (or module dir name) shown in a Java doc header."""
    extra = getattr(unit, "extra", None) or {}
    return extra.get("artifact") or extra.get("module") or getattr(unit, "build_root", "") \
        or "-"


def unit_name(unit: Any) -> str:
    name = getattr(unit, "name", "") or ""
    if name:
        return name
    rel = getattr(unit, "rel", "") or ""
    return PurePosixPath(rel).name or "_root"


def header_lines(adapter: Any, target_rel: str, unit: Any) -> str:
    """the two lines every doc starts with; an adapter may give its own
    through the optional hook header_lines(rel, unit) -> [line, ...]."""
    hook = getattr(adapter, "header_lines", None)
    if hook is not None:
        return "\n\n".join(hook(target_rel, unit))
    lang = adapter.name
    if lang == "python":
        second = f"**Module**: `{module_name(adapter, target_rel, unit)}`"
    elif lang == "java":
        second = f"**Package**: `{unit.import_path or unit_name(unit)}` (module `{java_module(unit)}`)"
    else:
        second = f"**{adapter.prompts.header_label}**: `{unit_name(unit)}` ({adapter.display})"
    return f"# {target_rel}\n\n{second}"


def what(adapter: Any, target_rel: str, unit: Any) -> str:
    """"the <file noun> `<rel>` of <unit description>" (or the adapter's unit_description)."""
    hook = getattr(adapter, "unit_description", None)
    if hook is not None:
        return hook(target_rel, unit)
    noun = adapter.prompts.file_noun
    lang = adapter.name
    if lang == "python":
        mod = module_name(adapter, target_rel, unit)
        pkg = getattr(unit, "import_path", "") or unit_name(unit)
        desc = f"module `{mod}` (package `{pkg}`)"
    elif lang == "java":
        desc = f"package `{unit.import_path or unit_name(unit)}` (module `{java_module(unit)}`)"
    else:
        desc = f"directory unit `{unit_name(unit)}`"
    return f"the {noun} `{target_rel}` of {desc}"


def _context_block(adapter: Any, context: str) -> str:
    if not context.strip():
        return "(no declarations in other files of this unit)"
    return _fence(context, adapter.prompts.fence)


def _sections(src: str, adapter: Any) -> str:
    return "\n".join(f"- `## {k} {n}`"
                     for k, n in expected_sections(src, adapter.name, adapter))


def _shown(adapter: Any, source: str) -> str:
    """the source as prompts show it (the adapter's optional prompt_source hook)."""
    hook = getattr(adapter, "prompt_source", None)
    return hook(source) if hook is not None else source


def _facts_block(adapter: Any, target_rel: str) -> str:
    """the optional static-analysis FACTS block (adapter hook doc_facts)."""
    hook = getattr(adapter, "doc_facts", None)
    if hook is None:
        return ""
    try:
        text = hook(target_rel)
    except Exception:
        text = ""
    if not text or not text.strip():
        return ""
    return ("## Static analysis facts (authoritative for names and relationships)\n"
            f"{text.rstrip()}\n\n")


def seed_messages(adapter: Any, target_rel: str, unit: Any, source: str, context: str
                  ) -> tuple[str, str]:
    """(system, user) asking for the first documentation of a non-Go file."""
    spec = adapter.prompts
    sections = _sections(source, adapter)
    user = f"""Document {what(adapter, target_rel, unit)}.

Start the documentation with exactly these lines:

{_fence(header_lines(adapter, target_rel, unit), "markdown")}

Required sections, in this order:
{sections or "- (none: describe the file in the opening paragraph)"}

{_facts_block(adapter, target_rel)}## Context (declared in OTHER files; do not document as sections)
{_context_block(adapter, context)}

## Source of `{target_rel}`
{_fence(_shown(adapter, source), spec.fence)}
"""
    return load_prompt(SEED, adapter.name), user


def _import_block(adapter: Any, allowed: list) -> str:
    if not allowed:
        return "(none: the original file imports nothing)"
    lines = list(dict.fromkeys(adapter.format_import(i) for i in allowed))
    return _fence("\n".join(lines), adapter.prompts.fence)


def _taken_block(taken: set[str], context: str) -> str:
    """names of the unit's other files not already shown in the context, within a char budget."""
    shown = set(re.findall(r"\w+", context))
    out: list[str] = []
    size = 0
    for n in sorted(n for n in taken if n not in shown):
        if size + len(n) + 2 > MAX_TAKEN_CHARS:
            out.append("...")
            break
        out.append(n)
        size += len(n) + 2
    return ", ".join(out)


_PREAMBLE_NOTE = {
    "python": ("## Inserted verbatim (do not write these lines)\n"
               "The original module's shebang, encoding cookie and `from __future__` imports "
               "are put back at the top of your module: do not write them yourself."),
    "java": ("## Package declaration (inserted verbatim as the first line; do not write it)"),
    "c": ("## Include preamble (inserted verbatim above your code; do not write it)\n"
          "The original file's leading comments, `#include` and `#pragma` lines are put back "
          "above your code: write only what follows them."),
    "cpp": ("## Include preamble (inserted verbatim above your code; do not write it)\n"
            "The original file's leading comments, `#include` and `#pragma` lines are put back "
            "above your code: write only what follows them."),
}


def _preamble_block(adapter: Any, target_rel: str) -> str:
    note = getattr(adapter, "preamble_note", None)
    if note is None:
        note = _PREAMBLE_NOTE.get(adapter.name, "## Preamble (inserted verbatim; do not write it)")
    text = ""
    try:
        if getattr(adapter, "ctx", None) is not None:
            text = adapter.preamble(adapter.read(target_rel))
    except Exception:
        text = ""
    if text.strip():
        return f"{note}\n{_fence(text, adapter.prompts.fence)}\n"
    return f"{note}\n"


def codegen_messages(adapter: Any, target_rel: str, unit: Any, doc: str, context: str,
                     allowed: list, *, taken: set[str] | None = None,
                     has_preamble: bool = False) -> tuple[str, str]:
    """(system, user) asking to rebuild a non-Go file from its documentation alone."""
    spec = adapter.prompts
    name = PurePosixPath(target_rel).name
    extra = ""
    if adapter.name != "python":
        names = _taken_block(taken or set(), context)
        if names:
            extra += ("\n## Other names already declared in this unit (never declare these)\n"
                      f"{names}\n")
    pre = f"\n{_preamble_block(adapter, target_rel)}" if has_preamble else ""
    heading = getattr(adapter, "import_heading",
                      "Allowed imports (use only these; omit unused ones)")
    user = f"""Write the {spec.file_noun} `{name}` ({what(adapter, target_rel, unit)}).
{pre}
## {heading}
{_import_block(adapter, allowed)}

## Context (already declared in other files: do not redeclare, you may use)
{_context_block(adapter, context)}
{extra}
## Documentation of `{name}`
{doc.strip()}

Output the complete file `{name}` in one ```{spec.fence} block.
"""
    return load_prompt(CODEGEN, adapter.name), user


def _last_attempt(last: DocEval | None, last_round: int) -> str:
    if last is None:
        return ""
    which = f"round {last_round}" if last_round else "the latest attempt"
    return f"""
## Last attempt ({which}): its documentation did NOT improve on the current one
{_clip(render_evaluation(last), MAX_LAST_CHARS)}

Do not repeat that attempt: refine the current documentation above differently.
"""


def refine_messages(adapter: Any, target_rel: str, unit: Any, source: str, doc: str, code: str,
                    evaluation: DocEval, missing: list[str], *, last: DocEval | None = None,
                    last_round: int = 0, attempt: int = 0) -> tuple[str, str]:
    """(system, user) asking for one diagnose-and-rewrite pass over a non-Go doc."""
    fence = adapter.prompts.fence
    if missing:
        gaps = "These required sections are missing; add them:\n" + "\n".join(
            f"- `## {m}`" for m in missing)
    else:
        gaps = "(none)"
    head = f"Round {attempt}. " if attempt else ""
    user = f"""{head}Refine the documentation of {what(adapter, target_rel, unit)}.

Keep the documentation starting with exactly these lines:

{_fence(header_lines(adapter, target_rel, unit), "markdown")}

{_facts_block(adapter, target_rel)}## Reference implementation (original `{target_rel}`)
{_fence(_shown(adapter, source), fence)}

## Current documentation
{_fence(doc, "markdown")}

## Generated code (written from the current documentation only)
{_fence(code, fence)}

## Evaluation
{render_evaluation(evaluation)}

## Missing sections
{gaps}
{_last_attempt(last, last_round)}
Reply with the complete revised documentation inside <documentation>...</documentation>.
"""
    return load_prompt(REFINE, adapter.name), user
