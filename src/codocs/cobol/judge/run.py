"""the judge stage: judge the COBOL docs of OUT/docs into OUT/judge (optional calibration)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from codocs.cobol.docs import load_program_docs
from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.judge.calibrate import calibrate as run_calibration
from codocs.cobol.judge.judge import DocJudge
from codocs.cobol.judge.report import write_judgements

CACHE_DIRNAME = "cache"  # OUT/judge/cache/<sha256>.json: parsed judgements


def judge_dir(out_dir: str | Path) -> Path:
    return Path(out_dir) / "judge"


def calibration_sample(index: CobolRepoIndex, pids: list[str], sample: int) -> list[str]:
    """*sample* programs spread over program sizes, smallest to largest."""
    ok = sorted(pids, key=lambda p: (index.programs[p].loc, p))
    k = min(sample, len(ok))
    picked = [ok[round(i * (len(ok) - 1) / max(k - 1, 1))] for i in range(k)] if k else []
    return list(dict.fromkeys(picked))


async def run_judge(
    index: CobolRepoIndex,
    out_dir: str | Path,
    llm,
    settings,
    programs: list[str] | None = None,
    *,
    calibrate: bool = False,
    sample: int = 5,
    repeats: int = 3,
    model: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """judge every COBOL doc of OUT/docs (only *programs* when given) and write OUT/judge.

    *llm* is the judge's codocs.llm.LLM (built for the judge model); *model*, when given,
    is passed to each call instead. Returns the summary (OUT/judge/summary.json), which
    also carries `notes` for the stage info. ValueError when there is no doc to judge.
    """
    out_dir = Path(out_dir)
    docs = load_program_docs(out_dir, index)
    if programs:
        wanted = set(programs)
        docs = {k: v for k, v in docs.items() if k in wanted}
    if not docs:
        raise ValueError(f"no COBOL docs to judge in {out_dir / 'docs'} "
                         "(run `codocs docgen` first)")
    concurrency = getattr(settings, "concurrency", 4) if settings is not None else 4
    judge = DocJudge(index, llm, judge_dir(out_dir) / CACHE_DIRNAME,
                     concurrency=concurrency, on_progress=on_progress, model=model)
    judgements = await judge.judge(docs)
    cal = None
    if calibrate and judgements:
        ok = [p for p, j in judgements.items() if not j.error]
        picked = calibration_sample(index, ok, sample)
        cal = await run_calibration(judge, index, docs, judgements, picked,
                                    repeats=repeats, on_progress=on_progress)
    summary = write_judgements(judgements, out_dir, cal)
    notes = [
        f"judge {judge.model}: accuracy {summary.get('accuracy_micro')}, "
        f"coverage {summary.get('coverage_macro')}, high-severity errors "
        f"{summary.get('high_errors')}, gate {summary.get('gate')}",
    ]
    if cal is not None:
        c = summary["calibration"]
        notes.append(f"calibration: sensitivity {c['sensitivity']} over "
                     f"{c['mutations']} injected errors")
    return {**summary, "judged": sorted(judgements), "notes": notes}
