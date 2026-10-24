"""data models for the LLM-as-a-judge evaluation of COBOL program docs.

Three layers, reported separately (never folded into one number):
  A. accuracy: atomic claims of the doc, each verified against the source (FActScore /
     ReFEree style), with an error taxonomy for the contradicted ones;
  B. completeness: a yes/no checklist derived deterministically from the static index
     (CheckEval / TICK style, the items come from facts, not from the judge);
  C. usability: two 1-5 scales with anchored levels (Prometheus style).
Plus a deterministic layer the judge cannot influence: identifiers named in the doc that
do not exist in the program, its copybooks or the repo index.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# contradicted-claim taxonomy (ReFEree categories adapted to COBOL)
ERROR_TYPES = {
    "name": "identifier does not match the code or refers to a different entity "
            "(e.g. an ENTRY name presented as a program)",
    "value": "wrong literal, PIC size, 88-level condition, return / status / abend code",
    "control-flow": "misread control flow: NEXT SENTENCE, GO TO out of a PERFORM range, "
                    "PERFORM THRU, fall-through, loop exit, EVALUATE order",
    "io": "wrong file / table / queue / map, or wrong access direction or mode",
    "fabricated": "describes a program, file, field, paragraph or behaviour that does not exist",
    "attribution": "behaviour attributed to the wrong paragraph or program",
    "other": "any other factual error",
}
VERDICTS = ("supported", "contradicted", "unverifiable")
SEVERITIES = ("high", "medium", "low")


class Claim(BaseModel):
    """one atomic factual statement of the doc and its verification."""

    id: str
    section: str = ""
    text: str
    verdict: str = "unverifiable"  # supported | contradicted | unverifiable
    error_type: str = ""  # ERROR_TYPES key when contradicted
    severity: str = ""  # high | medium | low when contradicted
    evidence_lines: list[int] = Field(default_factory=list)  # source line numbers cited
    evidence_quote: str = ""  # verbatim source text the verdict rests on
    explanation: str = ""
    quote_found: bool | None = None  # deterministic: evidence_quote occurs in the source


class ChecklistItem(BaseModel):
    """one yes/no completeness question, generated from the static index."""

    id: str
    category: str  # interface | io | sql | cics | transfer | invocation | paragraph | hazard | error
    question: str
    fact: str = ""  # the ground-truth fact the question is about
    answer: str = ""  # yes | partial | no (filled by the judge)
    evidence: str = ""  # doc text the answer rests on


class Scale(BaseModel):
    reasoning: str = ""
    score: int = 0  # 1-5, 0 = not given


class NameCheck(BaseModel):
    """deterministic check of identifiers named in the doc (no LLM)."""

    total: int = 0
    known: int = 0
    unknown: list[str] = Field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return self.known / self.total if self.total else None


class DocJudgement(BaseModel):
    """the full evaluation of one program doc."""

    program_id: str
    judge_model: str = ""
    claims: list[Claim] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    actionability: Scale = Field(default_factory=Scale)
    clarity: Scale = Field(default_factory=Scale)
    names: NameCheck = Field(default_factory=NameCheck)
    context_note: str = ""  # what was trimmed from the judge's view of the source
    error: str = ""  # judge call failed / unparseable
    raw_response: str = ""

    # -- metrics ------------------------------------------------------------------

    def count(self, verdict: str) -> int:
        return sum(1 for c in self.claims if c.verdict == verdict)

    @property
    def accuracy(self) -> float | None:
        """supported / (supported + contradicted): unverifiable claims are left out."""
        s, c = self.count("supported"), self.count("contradicted")
        return s / (s + c) if s + c else None

    @property
    def high_errors(self) -> int:
        return sum(1 for c in self.claims if c.verdict == "contradicted" and c.severity == "high")

    @property
    def coverage(self) -> float | None:
        """checklist recall: yes = 1, partial = 0.5, no = 0."""
        answered = [i for i in self.checklist if i.answer in ("yes", "partial", "no")]
        if not answered:
            return None
        return sum(1.0 if i.answer == "yes" else 0.5 if i.answer == "partial" else 0.0
                   for i in answered) / len(answered)

    @property
    def grounded_quotes(self) -> float | None:
        """share of the judge's evidence quotes found verbatim in the source (judge sanity)."""
        checked = [c for c in self.claims if c.quote_found is not None]
        return sum(c.quote_found for c in checked) / len(checked) if checked else None

    def gate(self, min_accuracy: float = 0.9, min_coverage: float = 0.7) -> str:
        """pass | fail | error: accuracy gates first (a high-severity error always fails)."""
        if self.error:
            return "error"
        acc, cov = self.accuracy, self.coverage
        if self.high_errors or (acc is not None and acc < min_accuracy):
            return "fail"
        if cov is not None and cov < min_coverage:
            return "fail"
        return "pass"

    def metrics(self) -> dict:
        return {
            "program_id": self.program_id,
            "claims": len(self.claims),
            "supported": self.count("supported"),
            "contradicted": self.count("contradicted"),
            "unverifiable": self.count("unverifiable"),
            "accuracy": self.accuracy,
            "high_errors": self.high_errors,
            "checklist": len(self.checklist),
            "coverage": self.coverage,
            "actionability": self.actionability.score or None,
            "clarity": self.clarity.score or None,
            "name_precision": self.names.precision,
            "unknown_names": self.names.unknown,
            "grounded_quotes": self.grounded_quotes,
            "gate": self.gate(),
            "error": self.error,
        }
