"""prompt for judging one program doc against the program source.

Rubric design:
- accuracy is judged per atomic claim with verbatim evidence (FActScore, ReFEree), never as
  one holistic score;
- completeness is a yes/no checklist whose questions come from the static index
  (CheckEval, TICK, CodeWikiBench);
- the two subjective scales have an anchor description per level (Prometheus) and the
  reasoning is written before the score (G-Eval);
- length is never rewarded (verbosity bias, MT-Bench).
"""

from __future__ import annotations

import json

from codocs.cobol.judge.models import ERROR_TYPES, ChecklistItem

PROMPT_VERSION = "judge-v3"
MAX_CLAIMS = 60

_SYSTEM = """\
You are a senior mainframe engineer auditing documentation of a COBOL program for factual \
accuracy. You check the document against the program source, which is the only ground \
truth, plus the static-analysis FACTS (computed by a parser from the repository: callers, \
JCL jobs, CICS transactions, files, tables; treat them as correct).

Rules:
- Judge only what the source and FACTS show. If the source shown to you is abridged and \
the claim depends on the omitted part, or the claim is about something outside this \
program (another program's behaviour, the runtime), the verdict is "unverifiable", not \
"supported" and not "contradicted".
- Every verdict must rest on evidence: cite source line numbers and copy the decisive \
source text VERBATIM into evidence_quote (a short exact excerpt, no line-number prefix). \
For a claim backed by FACTS, quote the FACTS line.
- Read COBOL semantics precisely: NEXT SENTENCE jumps past the next separator period \
(possibly out of an inline PERFORM or IF nest); GO TO does not return; PERFORM A THRU B \
runs every paragraph in between; a paragraph without GO TO / GOBACK falls into the next \
one; MOVE truncates/pads per the receiving PIC; DISPLAY of a group prints raw bytes; \
an alphanumeric comparison of unequal lengths pads the shorter operand with spaces (so \
"LOOP" = "LOOP " is true); PERFORM UNTIL tests before each iteration unless WITH TEST \
AFTER; READ ... INTO leaves the INTO item unchanged at end of file; CICS XCTL and RETURN \
never come back. Before marking a claim contradicted, re-check it against these rules.
- Write the reasoning (explanation) before deciding; do not reward length, confident \
tone or formatting. A short correct document scores as well as a long correct one.
- Output ONLY one JSON object, no markdown fences, no commentary."""


def _error_types() -> str:
    return "\n".join(f'  - "{k}": {v}' for k, v in ERROR_TYPES.items())


def _checklist(items: list[ChecklistItem]) -> str:
    if not items:
        return "(no checklist items for this program)"
    return "\n".join(f"- {i.id} [{i.category}] {i.question}" for i in items)


_TASKS = """\
## Task 1: claims (accuracy)
Split the DOCUMENT into atomic claims: each claim states ONE checkable fact (a condition, a \
value, a file access, a control transfer, a paragraph's action, a business rule, an \
error-handling step). Take them from the summary lines, the ClassDef subsections \
(Invocation, Interface, Inputs and outputs, Processing logic, Business rules, Error \
handling, Dependencies) and the VariableDef and MethodDef sections; ignore the copied \
COBOL blocks and mermaid diagrams. Cover the document evenly; when there are more than {max_claims} \
candidate claims, keep the {max_claims} most consequential ones (business rules, control \
flow, outputs written, error paths first). Paraphrase each claim faithfully in "text"; \
do not fix it.
For each claim give:
- "verdict": "supported" | "contradicted" | "unverifiable"
- when contradicted, "error_type" (one of):
{error_types}
  and "severity":
  - "high": a maintainer relying on it would get the program's behaviour wrong: wrong \
business rule or condition, wrong control flow / loop exit, wrong data written or wrong \
file direction, invented behaviour;
  - "medium": wrong detail on a secondary path or in the interface description;
  - "low": cosmetic: naming, ordering, an imprecise but harmless paraphrase.
  Edge cases: a claim that holds in normal operation but not in a corner case the \
document does not claim to cover (empty input file, uninitialized storage, a record area \
left undefined at end of file) is "contradicted" with severity "low" at most, and the \
explanation names the corner case. Reserve "high" for errors that hold in the normal path.
- "evidence_lines": source line numbers; "evidence_quote": verbatim excerpt; \
"explanation": one or two sentences.

## Task 2: checklist (completeness)
For each item answer "yes" (the document states it correctly), "partial" (mentioned but \
incomplete or vague) or "no" (absent or stated wrongly), with "evidence": the document \
text you relied on (empty for "no").
{checklist}

## Task 3: usability (1-5, reasoning first)
"actionability": a maintainer must change one business rule or fix one error path. Using \
ONLY this document, can they find where and under which condition?
  1 = no paragraph-level pointers, generic description;
  2 = names some paragraphs, but behaviour -> paragraph mapping is mostly missing;
  3 = main flows mapped to paragraphs/sections, rules lack their governing conditions;
  4 = most rules and error paths mapped to paragraphs with their conditions/fields;
  5 = every business rule and error path tied to a paragraph and the exact condition or \
field.
"clarity": 1 = confusing or self-contradictory; 2 = understandable but disorganised or \
repetitive; 3 = clear with noticeable redundancy or vague passages; 4 = clear and well \
organised, minor redundancy; 5 = precise and well organised, every sentence earns its \
place. Length never raises the score; padding lowers it.

## Output (JSON only)
{{"claims": [{{"id": "C01", "section": "...", "text": "...", "explanation": "...", \
"verdict": "...", "error_type": "", "severity": "", "evidence_lines": [123], \
"evidence_quote": "..."}}],
 "checklist": [{{"id": "K01", "evidence": "...", "answer": "yes"}}],
 "actionability": {{"reasoning": "...", "score": 4}},
 "clarity": {{"reasoning": "...", "score": 4}}}}"""


def build_judge_prompt(
    program_id: str,
    source: str,
    copybooks: dict[str, str],
    facts: str,
    doc_markdown: str,
    checklist: list[ChecklistItem],
    context_note: str = "",
) -> list[dict]:
    cb = "\n\n".join(f"### COPYBOOK {name}\n{text}" for name, text in copybooks.items())
    user = "\n\n".join(p for p in [
        f"# Program {program_id}",
        "## SOURCE (physical line numbers on the left; sequence area removed)\n" + source,
        ("## COPYBOOKS\n" + cb) if cb else "",
        ("## NOTE ON THE SOURCE SHOWN\n" + context_note) if context_note else "",
        "## FACTS (static analysis, authoritative)\n" + facts,
        "## DOCUMENT UNDER REVIEW\n<<<DOC\n" + doc_markdown.strip() + "\nDOC>>>",
        _TASKS.format(max_claims=MAX_CLAIMS, error_types=_error_types(),
                      checklist=_checklist(checklist)),
    ] if p)
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def checklist_json(items: list[ChecklistItem]) -> str:
    """stable text of the checklist, for cache keys."""
    return json.dumps([[i.id, i.question] for i in items])
