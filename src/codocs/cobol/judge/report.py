"""judge artifacts: OUT/judge/<PROG>.json, summary.json, summary.md (+ calibration)."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

from codocs.cobol.judge.calibrate import OPERATORS, Calibration, spread
from codocs.cobol.judge.models import ERROR_TYPES, DocJudgement


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 4) if vals else None


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _num(v) -> str:
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def _sd(v) -> str:
    """a standard deviation of a 0-1 rate, in percentage points."""
    return "-" if v is None else f"{v * 100:.1f} pp"


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize(judgements: dict[str, DocJudgement], cal: Calibration | None = None) -> dict:
    js = [j for j in judgements.values()]
    ok = [j for j in js if not j.error]
    errors = Counter(c.error_type for j in ok for c in j.claims if c.verdict == "contradicted")
    sev = Counter(c.severity for j in ok for c in j.claims if c.verdict == "contradicted")
    cats: dict[str, list[float]] = {}
    for j in ok:
        for i in j.checklist:
            if i.answer:
                cats.setdefault(i.category, []).append(
                    1.0 if i.answer == "yes" else 0.5 if i.answer == "partial" else 0.0)
    out = {
        "judge_model": next((j.judge_model for j in js if j.judge_model), ""),
        "docs": len(js),
        "judge_errors": len(js) - len(ok),
        "gate": dict(Counter(j.gate() for j in js)),
        "claims": sum(len(j.claims) for j in ok),
        "contradicted": sum(j.count("contradicted") for j in ok),
        "unverifiable": sum(j.count("unverifiable") for j in ok),
        "high_errors": sum(j.high_errors for j in ok),
        # micro: over all claims of all docs; macro: mean of per-doc values
        "accuracy_micro": None,
        "accuracy_macro": _mean(j.accuracy for j in ok),
        "coverage_macro": _mean(j.coverage for j in ok),
        "coverage_by_category": {k: round(statistics.fmean(v), 4) for k, v in sorted(cats.items())},
        "actionability_mean": _mean(j.actionability.score or None for j in ok),
        "clarity_mean": _mean(j.clarity.score or None for j in ok),
        "name_precision_macro": _mean(j.names.precision for j in ok),
        "grounded_quotes_macro": _mean(j.grounded_quotes for j in ok),
        "error_types": dict(errors.most_common()),
        "severities": dict(sev),
        "programs": [j.metrics() for j in sorted(js, key=lambda j: j.program_id)],
    }
    s = sum(j.count("supported") for j in ok)
    c = out["contradicted"]
    out["accuracy_micro"] = round(s / (s + c), 4) if s + c else None
    if cal is not None:
        out["calibration"] = calibration_summary(cal)
    return out


def calibration_summary(cal: Calibration) -> dict:
    return {
        "judge_model": cal.judge_model,
        "mutations": len(cal.mutations),
        "sensitivity": cal.sensitivity(),
        "sensitivity_by_operator": {op: cal.sensitivity(op) for op in OPERATORS
                                    if any(m.operator == op for m in cal.mutations)},
        "ambiguous": sum(1 for m in cal.mutations if m.detected is None),
        "stability": [
            {"program_id": s.program_id, "runs": s.runs,
             "accuracy_sd": spread(s.accuracy), "coverage_sd": spread(s.coverage),
             "gate_agreement": len(set(s.gates)) <= 1, "checklist_agreement":
             s.checklist_agreement}
            for s in cal.stability
        ],
        "name_findings": cal.name_findings,
        "name_findings_flagged": cal.name_findings_flagged,
    }


def write_judgements(judgements: dict[str, DocJudgement], out_dir: Path,
                     cal: Calibration | None = None) -> dict:
    """write OUT/judge/*; *out_dir* is OUT (or OUT/judge). Merges with earlier runs."""
    out_dir = Path(out_dir)
    jdir = out_dir if out_dir.name == "judge" else out_dir / "judge"
    jdir.mkdir(parents=True, exist_ok=True)
    for pid, j in judgements.items():
        data = j.model_dump()
        data["metrics"] = j.metrics()
        (jdir / f"{pid.replace('/', '_')}.json").write_text(
            json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    allj = load_judgements(jdir)
    allj.update(judgements)
    if cal is not None:
        (jdir / "calibration.json").write_text(cal.model_dump_json(indent=1) + "\n")
    elif (jdir / "calibration.json").exists():
        try:
            cal = Calibration.model_validate_json((jdir / "calibration.json").read_text())
        except Exception:
            cal = None
    summary = summarize(allj, cal)
    (jdir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
    (jdir / "summary.md").write_text(render_summary(summary, allj, cal), encoding="utf-8")
    return summary


def load_judgements(jdir: Path) -> dict[str, DocJudgement]:
    jdir = Path(jdir)
    if jdir.name != "judge":
        jdir = jdir / "judge"
    out: dict[str, DocJudgement] = {}
    for f in sorted(jdir.glob("*.json")):
        if f.name in ("summary.json", "calibration.json"):
            continue
        try:
            data = json.loads(f.read_text())
            data.pop("metrics", None)
            j = DocJudgement(**data)
            out[j.program_id] = j
        except Exception:
            continue
    return out


def render_summary(s: dict, judgements: dict[str, DocJudgement],
                   cal: Calibration | None = None) -> str:
    lines = [
        "# Leaf doc evaluation (LLM judge)",
        "",
        f"Judge model `{s['judge_model']}`. Each doc is checked claim by claim against the "
        "program source (accuracy), against a checklist derived from static analysis "
        "(completeness), and on two anchored 1-5 scales (usability). Identifiers that exist "
        "nowhere are found without an LLM (name precision).",
        "",
        "| metric | value |",
        "|---|---|",
        f"| docs judged | {s['docs']} (judge errors {s['judge_errors']}) |",
        f"| gate pass / fail | {s['gate'].get('pass', 0)} / {s['gate'].get('fail', 0)} |",
        f"| claims checked | {s['claims']} ({s['contradicted']} contradicted, "
        f"{s['unverifiable']} unverifiable) |",
        f"| accuracy (micro / macro) | {_pct(s['accuracy_micro'])} / "
        f"{_pct(s['accuracy_macro'])} |",
        f"| high-severity errors | {s['high_errors']} |",
        f"| checklist coverage (macro) | {_pct(s['coverage_macro'])} |",
        f"| actionability / clarity (1-5) | {_num(s['actionability_mean'])} / "
        f"{_num(s['clarity_mean'])} |",
        f"| name precision (deterministic) | {_pct(s['name_precision_macro'])} |",
        f"| judge evidence quotes found in source | {_pct(s['grounded_quotes_macro'])} |",
        "",
        "Gate: fail on any high-severity error, accuracy < 90% or coverage < 70%.",
        "",
    ]
    if s["coverage_by_category"]:
        lines += ["Checklist coverage by category: " + ", ".join(
            f"{k} {_pct(v)}" for k, v in s["coverage_by_category"].items()), ""]
    if s["error_types"]:
        lines += ["Contradicted claims by type: " + ", ".join(
            f"{k} {v}" for k, v in s["error_types"].items()) + " (" + "; ".join(
            f"{k}: {ERROR_TYPES[k].split(':')[0].split('(')[0].strip()}"
            for k in s["error_types"] if k in ERROR_TYPES) + ")", ""]
    if cal is not None:
        c = s["calibration"]
        lines += ["## Judge calibration", "",
                  f"- mutation sensitivity: {_pct(c['sensitivity'])} of {c['mutations']} "
                  f"injected errors detected ({c['ambiguous']} ambiguous) — "
                  + ", ".join(f"{k} {_pct(v)}" for k, v in c["sensitivity_by_operator"].items()),
                  f"- deterministic name findings flagged by the judge: "
                  f"{c['name_findings_flagged']}/{c['name_findings']}"]
        for st in c["stability"]:
            lines.append(f"- stability {st['program_id']} ({st['runs']} runs): accuracy sd "
                         f"{_sd(st['accuracy_sd'])}, coverage sd {_sd(st['coverage_sd'])}, "
                         f"checklist agreement {_pct(st['checklist_agreement'])}, gate "
                         f"{'stable' if st['gate_agreement'] else 'UNSTABLE'}")
        missed = [m for m in cal.mutations if m.detected is False]
        if missed:
            lines += ["", "Missed injected errors:", ""]
            for m in missed:
                lines.append(f"- {m.program_id} {m.operator} ({m.verdict_found}): "
                             f"`{_cell(m.mutated, 140)}`")
        lines.append("")
    lines += ["## Per program", "",
              "| program | gate | accuracy | high | contradicted / claims | coverage | "
              "action. | clarity | names | unknown names |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for p in s["programs"]:
        lines.append(
            f"| [{p['program_id']}]({p['program_id'].replace('/', '_')}.json) | {p['gate']} | "
            f"{_pct(p['accuracy'])} | {p['high_errors']} | {p['contradicted']}/{p['claims']} | "
            f"{_pct(p['coverage'])} | {_num(p['actionability'])} | {_num(p['clarity'])} | "
            f"{_pct(p['name_precision'])} | {_cell(', '.join(p['unknown_names']), 60)} |")
    high = [(j.program_id, c) for j in sorted(judgements.values(), key=lambda j: j.program_id)
            for c in j.claims if c.verdict == "contradicted" and c.severity == "high"]
    if high:
        lines += ["", "## High-severity errors", ""]
        for pid, c in high:
            where = f"lines {', '.join(map(str, c.evidence_lines[:4]))}" if c.evidence_lines else ""
            lines.append(f"- **{pid}** [{c.error_type}] {_cell(c.text, 220)} — "
                         f"{_cell(c.explanation, 260)} ({where}; quote "
                         f"{'verified' if c.quote_found else 'NOT found in source'})")
    return "\n".join(lines) + "\n"
