"""a fake judge for the COBOL judge tests: answers judge prompts through the
codocs.llm.LLM.complete(system, user, ...) signature (no network)."""

from __future__ import annotations

import json
import re

JUDGE_SYSTEM_MARK = "auditing documentation of a COBOL program"


def _rules(doc: str) -> str:
    """the `### Business rules` body of the doc under review ("" when absent)."""
    m = re.search(r"^### Business rules[ \t]*\n(.*?)(?=^#{2,3} |\Z)", doc, re.M | re.S | re.I)
    return m.group(1) if m else ""


def fake_judgement(user: str) -> str:
    """a judge answer: one claim per line of the doc's `### Business rules`; a rule is
    contradicted when it says NOT = or names AUDIT-TRAIL-FILE (what calibration injects);
    every checklist item is answered yes."""
    doc = user.split("<<<DOC", 1)[1].split("DOC>>>", 1)[0]
    claims = []
    for n, line in enumerate(x.strip() for x in _rules(doc).splitlines() if x.strip()):
        bad = "NOT =" in line or "AUDIT-TRAIL-FILE" in line
        claims.append({
            "id": f"C{n + 1:02d}", "section": "Business rules", "text": line,
            "explanation": "checked against the source",
            "verdict": "contradicted" if bad else "supported",
            "error_type": "value" if bad else "", "severity": "high" if bad else "",
            "evidence_lines": [8], "evidence_quote": "GOBACK",
        })
    items = re.findall(r"^- (K\d+) \[", user, re.M)
    return json.dumps({
        "claims": claims,
        "checklist": [{"id": k, "evidence": "doc", "answer": "yes"} for k in items],
        "actionability": {"reasoning": "ok", "score": 4},
        "clarity": {"reasoning": "ok", "score": 5},
    })


class _Settings:
    def __init__(self, model: str):
        self.model = model


class FakeJudgeLLM:
    """stands in for codocs.llm.LLM: judge prompts get fake_judgement(); records each call
    (kinds, tags, models) and counts 100 input / 10 output tokens per call."""

    def __init__(self, model: str = "fake-model"):
        self.model = model
        self.settings = _Settings(model)
        self.kinds: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.tags: list[str] = []
        self.models: list[str | None] = []
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def tokens(self) -> dict:
        return {"input": self.input_tokens, "output": self.output_tokens}

    async def complete(self, system: str, user: str, *, tag: str = "",
                       temperature: float | None = None, max_tokens: int | None = None,
                       validate=None, model: str | None = None) -> str:
        self.input_tokens += 100
        self.output_tokens += 10
        self.calls.append((system, user))
        self.tags.append(tag)
        self.models.append(model)
        if JUDGE_SYSTEM_MARK in system:
            self.kinds.append("judge")
            return self.answer(system, user)
        self.kinds.append("other")
        return ""

    def answer(self, system: str, user: str) -> str:
        return fake_judgement(user)
