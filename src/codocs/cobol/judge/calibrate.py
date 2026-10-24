"""calibration of the judge itself: is it sensitive, stable, and consistent with the
deterministic checks?

- sensitivity ("mutation testing of the judge"): known errors are injected into a doc
  (flipped comparison, swapped file access, wrong paragraph attribution, changed value,
  fabricated rule) and the mutated doc is judged; a mutation is detected when a
  contradicted claim mentions the injected text;
- stability: the same doc is judged several times (fresh samples); spread of accuracy and
  coverage, and agreement of the checklist answers;
- agreement with the name check: identifiers that exist nowhere (names.py) should show up
  in the judge's contradicted claims.
No randomness: which site each operator mutates is a function of the doc text.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from collections.abc import Callable

from pydantic import BaseModel, Field

from codocs.cobol.docs import LeafDoc
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.judge.judge import DocJudge
from codocs.cobol.judge.models import DocJudgement

OPERATORS = ("flip-comparison", "swap-access", "swap-paragraph", "change-value", "fabricate")
FABRICATED_FILE = "AUDIT-TRAIL-FILE"
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_CMP = [(" NOT = ", " = "), (" >= ", " < "), (" <= ", " > "), (" > ", " <= "),
        (" < ", " >= "), (" = ", " NOT = ")]


class Mutation(BaseModel):
    program_id: str
    operator: str
    section: str = ""
    original: str = ""  # the line before mutation
    mutated: str = ""  # the line after mutation
    needle: str = ""  # text a detecting claim must mention
    detected: bool | None = None
    verdict_found: str = ""  # contradicted | unverifiable | missed
    claim: str = ""  # the claim that caught it


class Stability(BaseModel):
    program_id: str
    runs: int = 0
    accuracy: list[float | None] = Field(default_factory=list)
    coverage: list[float | None] = Field(default_factory=list)
    gates: list[str] = Field(default_factory=list)
    checklist_agreement: float | None = None  # share of items answered identically in all runs


class Calibration(BaseModel):
    judge_model: str = ""
    mutations: list[Mutation] = Field(default_factory=list)
    stability: list[Stability] = Field(default_factory=list)
    name_findings: int = 0  # unknown identifiers found by the deterministic check
    name_findings_flagged: int = 0  # ... of which the judge flagged in a contradicted claim

    def sensitivity(self, operator: str | None = None) -> float | None:
        ms = [m for m in self.mutations if m.detected is not None
              and (operator is None or m.operator == operator)]
        return sum(m.detected for m in ms) / len(ms) if ms else None


# -- mutation operators -----------------------------------------------------------------


def _sections(md: str) -> list[tuple[str, int, int]]:
    """(heading, first body line, end line) of each `## ` and `### ` section: the ClassDef
    subsections (`### Business rules`, ...) and the VariableDef / MethodDef sections."""
    lines = md.split("\n")
    heads = []
    in_fence = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and ln.startswith(("## ", "### ")):
            heads.append((i, ln.lstrip("#").strip()))
    return [(h, i + 1, heads[k + 1][0] if k + 1 < len(heads) else len(lines))
            for k, (i, h) in enumerate(heads)]


def _lines_in(md: str, names: tuple[str, ...]) -> list[tuple[int, str]]:
    """(line index, heading) of the prose lines of the sections whose heading starts with
    one of *names* (case-insensitive); fenced blocks are skipped."""
    lines = md.split("\n")
    out = []
    in_fence = False
    for head, a, b in _sections(md):
        if not head.lower().startswith(tuple(n.lower() for n in names)):
            continue
        for i in range(a, b):
            if lines[i].strip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence and lines[i].strip():
                out.append((i, head))
    return out


# the program doc sections each operator looks in
_RULES = ("Business rules",)
_COMPARE_IN = ("Business rules", "Processing logic", "MethodDef")
_IO_IN = ("Inputs and outputs",)
_VALUE_IN = ("Business rules", "Interface", "Error handling")


def _replace_line(md: str, i: int, new: str) -> str:
    lines = md.split("\n")
    lines[i] = new
    return "\n".join(lines)


def _pick(cands: list, seed: str):
    """deterministic choice that still spreads over the candidates across programs."""
    if not cands:
        return None
    return cands[sum(map(ord, seed)) % len(cands)]


def mutate(doc: LeafDoc, operator: str, paragraphs: list[str]) -> tuple[str, Mutation] | None:
    """the doc with one injected error of kind *operator*, or None if no site fits."""
    md = doc.markdown
    lines = md.split("\n")
    m = Mutation(program_id=doc.program_id, operator=operator)
    seed = doc.program_id + operator

    if operator == "flip-comparison":
        cands = []
        for i, head in _lines_in(md, _COMPARE_IN):
            for span in _SPAN_RE.findall(lines[i]):
                padded = f" {span} "
                for old, new in _CMP:
                    if old in padded:
                        flipped = padded.replace(old, new, 1).strip()
                        cands.append((i, head, span, flipped))
                        break
        pick = _pick(cands, seed)
        if not pick:
            return None
        i, head, span, flipped = pick
        new_line = lines[i].replace(f"`{span}`", f"`{flipped}`", 1)
        m.needle = flipped
    elif operator == "swap-access":
        cands = []
        for i, head in _lines_in(md, _IO_IN):
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            if len(cells) >= 3 and not set(cells[0]) <= set("-: "):
                for k, c in enumerate(cells[1:], 1):
                    swap = {"read": "write", "write": "read", "input": "output",
                            "output": "input"}.get(c.lower())
                    if swap:
                        cands.append((i, head, cells, k, swap))
                        break
        pick = _pick(cands, seed)
        if not pick:
            return None
        i, head, cells, k, swap = pick
        cells[k] = swap
        new_line = "| " + " | ".join(cells) + " |"
        m.needle = re.sub(r"[`*]", "", cells[0]).split("/")[0].strip()
    elif operator == "swap-paragraph":
        names = {p.upper() for p in paragraphs}
        cands = []
        for i, head in _lines_in(md, _RULES):
            for span in _SPAN_RE.findall(lines[i]):
                if span.upper() in names:
                    cands.append((i, head, span))
        pick = _pick(cands, seed)
        if not pick or len(names) < 2:
            return None
        i, head, span = pick
        others = [p for p in paragraphs if p.upper() != span.upper()]
        other = others[len(others) // 2]
        new_line = lines[i].replace(f"`{span}`", f"`{other}`")
        m.needle = other
    elif operator == "change-value":
        cands = []
        for i, head in _lines_in(md, _VALUE_IN):
            for span in _SPAN_RE.findall(lines[i]):
                if re.fullmatch(r"'?-?\d+(\.\d+)?'?", span.strip()):
                    cands.append((i, head, span))
        pick = _pick(cands, seed)
        if not pick:
            return None
        i, head, span = pick
        digits = [k for k, ch in enumerate(span) if ch.isdigit()]
        k = digits[-1] if span.count(".") == 0 else digits[0]
        changed = span[:k] + str((int(span[k]) + 3) % 10) + span[k + 1:]
        new_line = lines[i].replace(f"`{span}`", f"`{changed}`", 1)
        m.needle = changed
    elif operator == "fabricate":
        rules = _lines_in(md, _RULES)
        if not rules:
            return None
        i, head = rules[-1]
        n = len([1 for j, _ in rules if re.match(r"\s*\d+\.", lines[j])]) + 1
        extra = (f"{n}. If the input record is entirely blank, the program writes it unchanged "
                 f"to `{FABRICATED_FILE}` and skips all further validation.")
        new_line = lines[i] + "\n" + extra
        m.needle = FABRICATED_FILE
    else:
        raise ValueError(f"unknown mutation operator {operator}")
    m.section, m.original, m.mutated = head, lines[i], new_line
    return _replace_line(md, i, new_line), m


_TOK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_STOP = frozenset("THE AND FOR WITH FROM INTO THAT THIS WHEN THEN ELSE NOT ARE IS".split())


def _toks(text: str, min_len: int = 2) -> set[str]:
    return {t.upper() for t in _TOK_RE.findall(text.replace("`", " "))
            if len(t) >= min_len and t.upper() not in _STOP}


def _about(claim, key: set[str], line: set[str]) -> bool:
    """the claim mentions the mutated fact: a key token plus 2+ tokens of the line
    (key tokens of any length: an injected value may be a single digit)."""
    text = f"{claim.text} {claim.explanation}"
    return bool(_toks(text, 1) & key) and len(_toks(text) & line) >= 2


def detect(m: Mutation, j: DocJudgement, base: DocJudgement | None = None) -> Mutation:
    """mark *m* detected when a contradicted claim of *j* is about the injected fact.

    A claim is about it when it shares a key token (the needle's tokens) and at least two
    tokens with the mutated line; judges paraphrase, so exact text is not required. When
    the judgement of the unmutated doc already contradicted that same line, the result is
    ambiguous (detected=None).
    """
    if j.error:
        m.detected, m.verdict_found = None, "judge error"
        return m
    key = _toks(m.needle, 1) or {m.needle.upper()}
    line = _toks(m.mutated)
    hits = [c for c in j.claims if _about(c, key, line)]
    bad = [c for c in hits if c.verdict == "contradicted"]
    if bad:
        orig = _toks(m.original)
        if base and any(c.verdict == "contradicted" and _about(c, key, orig)
                        for c in base.claims):
            m.detected, m.verdict_found = None, "ambiguous (already contradicted)"
        else:
            m.detected, m.verdict_found = True, "contradicted"
        m.claim = bad[0].text
    elif any(c.verdict == "unverifiable" for c in hits):
        m.detected, m.verdict_found = False, "unverifiable"
        m.claim = next(c.text for c in hits if c.verdict == "unverifiable")
    else:
        m.detected, m.verdict_found = False, "supported" if hits else "missed"
        m.claim = hits[0].text if hits else ""
    return m


# -- runner ---------------------------------------------------------------------------------


def _agreement(runs: list[DocJudgement]) -> float | None:
    if len(runs) < 2 or not runs[0].checklist:
        return None
    ids = [i.id for i in runs[0].checklist]
    same = 0
    for k in ids:
        answers = {next((i.answer for i in r.checklist if i.id == k), "") for r in runs}
        same += len(answers) == 1
    return same / len(ids)


async def calibrate(
    judge: DocJudge,
    index: CobolRepoIndex,
    docs: dict[str, LeafDoc],
    base: dict[str, DocJudgement],
    sample: list[str],
    repeats: int = 3,
    operators: tuple[str, ...] = OPERATORS,
    on_progress: Callable[[str], None] | None = None,
) -> Calibration:
    """mutation sensitivity + stability on the *sample* programs; name agreement on all."""
    cal = Calibration(judge_model=judge.model)
    jobs: list = []
    for pid in sample:
        doc = docs[pid]
        paras = [p.name for p in index.programs[pid].paragraphs if p.name != "(entry)"]
        for op in operators:
            res = mutate(doc, op, paras)
            if res is None:
                continue
            md, mut = res
            jobs.append(("mut", mut, doc.model_copy(update={"markdown": md})))
        for r in range(1, repeats):
            jobs.append(("rep", pid, r))
    done = 0

    async def _run(job):
        nonlocal done
        kind = job[0]
        if kind == "mut":
            _, mut, mdoc = job
            j = await judge.judge_one(mdoc, salt=f"mutation:{mut.operator}")
            out = detect(mut, j, base.get(mut.program_id))
        else:
            _, pid, r = job
            out = (pid, await judge.judge_one(docs[pid], salt=f"repeat:{r}"))
        done += 1
        if on_progress:
            on_progress(f"Calibration: {done}/{len(jobs)}")
        return kind, out

    results = await asyncio.gather(*(_run(j) for j in jobs))
    cal.mutations = [o for k, o in results if k == "mut"]
    reps: dict[str, list[DocJudgement]] = {p: [base[p]] for p in sample if p in base}
    for k, o in results:
        if k == "rep" and o[0] in reps:
            reps[o[0]].append(o[1])
    for pid, runs in reps.items():
        runs = [r for r in runs if not r.error]
        cal.stability.append(Stability(
            program_id=pid, runs=len(runs), accuracy=[r.accuracy for r in runs],
            coverage=[r.coverage for r in runs], gates=[r.gate() for r in runs],
            checklist_agreement=_agreement(runs)))
    for j in base.values():
        for name in j.names.unknown:
            cal.name_findings += 1
            flagged = any(c.verdict == "contradicted" and name in c.text.upper().replace("`", "")
                          for c in j.claims)
            cal.name_findings_flagged += flagged
    return cal


def spread(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.pstdev(vals), 4) if len(vals) >= 2 else None
