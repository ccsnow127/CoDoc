"""LLM-as-a-judge for COBOL program docs: one judge call per doc plus deterministic checks.

The judge sees the same ground truth the doc generator saw (numbered source, copybooks,
FACTS), the program's doc slice, and a checklist built from the index. Its answer is parsed
into a DocJudgement; then two checks that need no LLM are added: whether each evidence quote
really occurs in the source (the judge's own grounding) and the identifier check (names.py).
Calls go through codocs.llm.LLM; parsed judgements are cached under OUT/judge/cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from codocs.cobol.docs import LeafDoc
from codocs.cobol.facts import (
    build_facts,
    fit_context,
    program_copybooks,
    program_text,
    render_source,
)
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.judge import prompts
from codocs.cobol.judge.checklist import build_checklist
from codocs.cobol.judge.models import (
    ERROR_TYPES,
    SEVERITIES,
    VERDICTS,
    ChecklistItem,
    Claim,
    DocJudgement,
    Scale,
)
from codocs.cobol.judge.names import check_names
from codocs.llm import LLMError
from codocs.repowiki.llm.prompts import extract_json

logger = logging.getLogger(__name__)

_BUDGET = 600_000  # chars of source + copybooks shown to the judge
_MAX_TOKENS = 32_000
_LINE_NO_RE = re.compile(r"^\s*\d{1,6}\s+", re.M)


def _norm(text: str) -> str:
    return " ".join(text.upper().split())


def quote_in(quote: str, haystack_norm: str) -> bool | None:
    """does *quote* occur in the (normalized) haystack? '...' splits it into parts."""
    quote = _LINE_NO_RE.sub("", quote or "").strip().strip("`")
    if not quote:
        return None
    parts = [p for p in (_norm(x).strip(" .") for x in re.split(r"\.\.\.|…", quote))
             if len(p) >= 3]
    if not parts:
        return None
    return all(p in haystack_norm for p in parts)


def _as_int_list(values) -> list[int]:
    out = []
    for v in values or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _scale(data) -> Scale:
    if not isinstance(data, dict):
        return Scale()
    try:
        score = int(data.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return Scale(reasoning=str(data.get("reasoning", ""))[:2000],
                 score=score if 1 <= score <= 5 else 0)


def parse_judgement(
    raw: str, program_id: str, checklist: list[ChecklistItem]
) -> DocJudgement | None:
    """DocJudgement from the judge's JSON answer, or None when it is not usable."""
    data = extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return None
    claims: list[Claim] = []
    for n, c in enumerate(data["claims"], 1):
        if not isinstance(c, dict) or not str(c.get("text", "")).strip():
            continue
        verdict = str(c.get("verdict", "")).lower().strip()
        verdict = verdict if verdict in VERDICTS else "unverifiable"
        etype = str(c.get("error_type") or "").lower().strip()
        sev = str(c.get("severity") or "").lower().strip()
        if verdict == "contradicted":
            etype = etype if etype in ERROR_TYPES else "other"
            sev = sev if sev in SEVERITIES else "medium"
        else:
            etype, sev = "", ""
        claims.append(Claim(
            id=str(c.get("id") or f"C{n:02d}"), section=str(c.get("section", "")),
            text=str(c["text"]).strip(), verdict=verdict, error_type=etype, severity=sev,
            evidence_lines=_as_int_list(c.get("evidence_lines")),
            evidence_quote=str(c.get("evidence_quote") or ""),
            explanation=str(c.get("explanation") or ""),
        ))
    answers = {}
    for a in data.get("checklist") or []:
        if isinstance(a, dict) and a.get("id"):
            answers[str(a["id"]).strip()] = a
    items = []
    for item in checklist:
        a = answers.get(item.id, {})
        ans = str(a.get("answer", "")).lower().strip()
        items.append(item.model_copy(update={
            "answer": ans if ans in ("yes", "partial", "no") else "",
            "evidence": str(a.get("evidence") or "")[:500],
        }))
    return DocJudgement(
        program_id=program_id, claims=claims, checklist=items,
        actionability=_scale(data.get("actionability")), clarity=_scale(data.get("clarity")),
    )


class DocJudge:
    """judges program docs against the source with one LLM call per doc.

    *llm* is a codocs.llm.LLM (or anything with its `complete(system, user, *, tag,
    max_tokens, ...)`); *model*, when given, is passed to every call as `model=`, else the
    judge model is the LLM's own. Parsed judgements are cached as JSON files in *cache_dir*
    (OUT/judge/cache), keyed by sha256(judge model, salt, system, user); None disables it.
    """

    def __init__(
        self,
        index: CobolRepoIndex,
        llm,
        cache_dir: str | Path | None = None,
        concurrency: int = 5,
        on_progress: Callable[[str], None] | None = None,
        budget: int = _BUDGET,
        *,
        model: str | None = None,
    ):
        self.index = index
        self.llm = llm
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.budget = budget
        self._model = model or None
        self._sem = asyncio.Semaphore(concurrency)
        self._on_progress = on_progress

    @property
    def model(self) -> str:
        if self._model:
            return self._model
        own = getattr(self.llm, "model", "")
        if isinstance(own, str) and own:
            return own
        return getattr(getattr(self.llm, "settings", None), "model", "") or ""

    def _progress(self, msg: str) -> None:
        if self._on_progress:
            self._on_progress(msg)

    def context(self, pid: str) -> tuple[str, dict[str, str], str, str, str]:
        """(numbered source, copybooks, facts, context note, haystack for quote checks)."""
        prog = self.index.programs[pid]
        text, first = program_text(self.index, pid)
        full = render_source(text, prog.format, start=first)
        cbs = program_copybooks(self.index, pid)
        source, copybooks = fit_context(full, cbs, self.budget)
        note = ""
        if source != full or copybooks != cbs:
            note = ("The source and/or copybooks were abridged to fit the context (see the "
                    "'lines omitted' markers). Claims that depend on omitted code are "
                    "'unverifiable'.")
        facts = build_facts(self.index, pid)
        hay = "\n".join([render_source(text, prog.format, numbered=False), *cbs.values(),
                         facts, text])
        return source, copybooks, facts, note, _norm(hay)

    def prepare(self, doc: LeafDoc, salt: str = "") -> tuple[str, str, str, list[ChecklistItem]]:
        """(system, user, cache key, checklist); *salt* forces a fresh sample (stability
        runs): it is appended to the user message as `(sample <salt>)`."""
        source, copybooks, facts, note, _ = self.context(doc.program_id)
        checklist = build_checklist(self.index, doc.program_id)
        messages = prompts.build_judge_prompt(doc.program_id, source, copybooks, facts,
                                              doc.markdown, checklist, note)
        system, user = messages[0]["content"], messages[-1]["content"]
        if salt:
            user += f"\n\n(sample {salt})"
        blob = json.dumps([self.model, salt, system, user], ensure_ascii=False)
        return system, user, hashlib.sha256(blob.encode()).hexdigest(), checklist

    def _cached(self, key: str) -> DocJudgement | None:
        if self.cache_dir is None:
            return None
        path = self.cache_dir / f"{key}.json"
        if not path.is_file():
            return None
        try:
            return DocJudgement(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def _store(self, key: str, result: DocJudgement) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result.model_dump(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    async def _ask(self, pid: str, system: str, user: str,
                   checklist: list[ChecklistItem]) -> tuple[DocJudgement | None, str]:
        """(parsed judgement or None, last raw answer): one retry on an unusable answer."""
        kw: dict = {"tag": f"judge {pid}", "max_tokens": _MAX_TOKENS,
                    "validate": lambda raw: parse_judgement(raw, pid, checklist) is not None}
        if self._model:
            kw["model"] = self._model
        raw = ""
        for attempt in (1, 2):  # evaluation, not generation: one retry on a bad answer
            try:
                raw = await self.llm.complete(system, user, **kw)
            except LLMError as e:  # the client already asked again; nothing usable came
                logger.warning("judge answer for %s unusable: %s", pid, e)
                return None, str(e)
            result = parse_judgement(raw, pid, checklist)
            if result is not None:
                return result, raw
            logger.warning("judge answer for %s unusable (attempt %d): %s",
                           pid, attempt, raw[:200])
        return None, raw

    async def judge_one(self, doc: LeafDoc, salt: str = "") -> DocJudgement:
        pid = doc.program_id
        system, user, key, checklist = self.prepare(doc, salt)
        result = self._cached(key)
        if result is None:
            result, raw = await self._ask(pid, system, user, checklist)
            if result is None:
                return DocJudgement(program_id=pid, judge_model=self.model,
                                    checklist=checklist, error="judge answer unusable",
                                    raw_response=raw[-4000:])
            result.raw_response = ""
            self._store(key, result)
        # deterministic layers, recomputed every time (cheap, and independent of the cache)
        _, copybooks, facts, note, hay = self.context(pid)
        for c in result.claims:
            c.quote_found = quote_in(c.evidence_quote, hay)
        text, _ = program_text(self.index, pid)
        result.names = check_names(self.index, pid, doc.markdown,
                                   [text, *copybooks.values(), facts])
        result.judge_model = self.model
        result.context_note = note
        return result

    async def judge(self, docs: dict[str, LeafDoc]) -> dict[str, DocJudgement]:
        todo = [d for pid, d in sorted(docs.items())
                if pid in self.index.programs and not d.error]
        self._progress(f"Judging {len(todo)} program docs with {self.model}...")
        done = 0

        async def _one(d: LeafDoc) -> DocJudgement:
            nonlocal done
            async with self._sem:
                j = await self.judge_one(d)
            done += 1
            acc = j.accuracy
            self._progress(f"Judged {done}/{len(todo)} {d.program_id}"
                           + (f" (accuracy {acc:.0%})" if acc is not None else ""))
            return j

        out = await asyncio.gather(*(_one(d) for d in todo))
        return {j.program_id: j for j in out}
