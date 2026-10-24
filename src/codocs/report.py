"""OUT/summary.json + OUT/report.md from the per-file result.json files.

rows carry `lang` when the result has one (non-Go). in multi mode (analysis.json names more
than Go, or a row is not Go) the summary gains per-language aggregates and the report a
per-language table and a `lang` column; with only Go rows neither is added.

COBOL rows also carry the program's test mode; spec and
no-toolchain suites are counted apart from `ran` and every mean; COBOL phi is reported per
docgen signal, with the facts class table; OUT/judge/summary.json adds a "Doc judge" section.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _results(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.rglob("result.json")):
        d = _load_json(path)
        if d is None:
            log.warning("unreadable result: %s", path)
            continue
        out.append(d)
    return out


def _rounds(d: dict) -> list[dict]:
    return [r for r in (d.get("rounds") or []) if isinstance(r, dict)]


def _seconds(rounds: list[dict]) -> float:
    return round(sum(_num(r.get("seconds")) or 0.0 for r in rounds), 3)


def _round_phi(r: dict) -> float:
    ev = r.get("eval") if isinstance(r.get("eval"), dict) else {}
    return _num(ev.get("phi")) or 0.0


def _round_score(r: dict) -> float:
    m = r.get("measurement") if isinstance(r.get("measurement"), dict) else {}
    score = _num(m.get("score"))
    if score is not None:
        return score
    if not m.get("build_ok") or not m.get("green_size"):
        return 0.0
    cov = _num(m.get("cov")) or 0.0
    mut = _num(m.get("mut"))
    return cov * mut if mut is not None else cov


def _best_eval(d: dict, rounds: list[dict]) -> dict:
    best = d.get("best_round")
    for r in rounds:
        if r.get("round") == best and isinstance(r.get("eval"), dict):
            return r["eval"]
    return {}


def doc_row(d: dict) -> dict:
    """one docgen result.json -> one report row."""
    rounds = _rounds(d)
    traj = d.get("phi_trajectory")
    if not isinstance(traj, list):
        traj = [_round_phi(r) for r in rounds]
    signal = d.get("signal") or ""
    if not signal and rounds:
        ev = rounds[0].get("eval")
        signal = ev.get("signal", "") if isinstance(ev, dict) else ""
    best = _num(d.get("best_phi"))
    if best is None:
        best = max(traj) if traj else 0.0
    return {
        "pkg": d.get("pkg", ""),
        "file": d.get("file", ""),
        "status": d.get("status", ""),
        "signal": signal,
        "rounds": len(rounds),
        "max_rounds": d.get("max_rounds"),
        "phi_trajectory": [round(float(x), 4) for x in traj],
        "best_phi": round(best, 4),
        "best_round": d.get("best_round", 0),
        "early_stop": bool(d.get("early_stop", False)),
        "threshold": d.get("threshold"),
        "seconds": _seconds(rounds),
        "error": d.get("error", "") or "",
        **_lang(d),
        **_facts(d, rounds),
    }


def _facts(d: dict, rounds: list[dict]) -> dict:
    """COBOL facts signal: the best round's facts class table."""
    if d.get("lang") != "cobol":
        return {}
    by = _best_eval(d, rounds).get("facts_by_class")
    return {"facts_by_class": by} if isinstance(by, dict) and by else {}


def _lang(d: dict) -> dict:
    lang = d.get("lang")
    return {"lang": lang} if isinstance(lang, str) and lang else {}


def lang_of_row(r: dict) -> str:
    return r.get("lang") or "go"


def suite_row(d: dict) -> dict:
    """one testgen result.json -> one report row."""
    rounds = _rounds(d)
    traj = d.get("score_trajectory")
    if not isinstance(traj, list):
        traj = [_round_score(r) for r in rounds]
    best = _num(d.get("best_score"))
    if best is None:
        best = max(traj) if traj else 0.0
    mut = _num(d.get("best_mut"))
    return {
        "pkg": d.get("pkg", ""),
        "file": d.get("file", ""),
        "status": d.get("status", ""),
        "rounds": len(rounds),
        "max_rounds": d.get("max_rounds"),
        "score_trajectory": [round(float(x), 4) for x in traj],
        "best_score": round(best, 4),
        "best_round": d.get("best_round", 0),
        "best_cov": round(_num(d.get("best_cov")) or 0.0, 4),
        "best_mut": None if mut is None else round(mut, 4),
        "best_size": int(_num(d.get("best_size")) or 0),
        "early_stop": bool(d.get("early_stop", False)),
        "threshold": d.get("threshold"),
        "seconds": _seconds(rounds),
        "error": d.get("error", "") or "",
        **_lang(d),
        **({"cov_note": d["cov_note"]} if d.get("cov_note") else {}),
    }


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


# testgen statuses of COBOL suites that are not measured runs
UNMEASURED = ("spec", "no-toolchain")


def _measured(r: dict) -> bool:
    return r["rounds"] > 0 and not (lang_of_row(r) == "cobol" and r["status"] in UNMEASURED)


def _common(rows: list[dict], best_key: str) -> dict:
    ran = [r for r in rows if _measured(r)]
    hist = Counter(r["rounds"] for r in ran)
    reached = [
        r for r in ran
        if _num(r.get("threshold")) is not None and r[best_key] >= float(r["threshold"])
    ]
    return {
        "files": len(rows),
        "ran": len(ran),
        "statuses": dict(sorted(Counter(r["status"] or "?" for r in rows).items())),
        "errors": sum(1 for r in rows if r["status"] == "error" or r["error"]),
        f"mean_{best_key}": _mean([r[best_key] for r in ran]),
        "early_stop_rate": round(sum(r["early_stop"] for r in ran) / len(ran), 4) if ran else None,
        "reached_threshold": len(reached),
        "rounds_histogram": {str(k): hist[k] for k in sorted(hist)},
        "total_rounds": sum(r["rounds"] for r in ran),
        "seconds": round(sum(r["seconds"] for r in rows), 3),
    }


TEST_SIGNALS = ("tests", "generated-tests")
PHI_SIGNALS = ("tests", "generated-tests", "facts", "compile")


def doc_aggregate(rows: list[dict]) -> dict:
    agg = _common(rows, "best_phi")
    ran = [r for r in rows if r["rounds"] > 0]
    agg["signals"] = dict(sorted(Counter(r["signal"] or "?" for r in ran).items()))
    cobol = [r for r in ran if lang_of_row(r) == "cobol"]
    if cobol:
        # COBOL phi only from executed tests in the overall mean; per signal below
        kept = [r for r in ran if lang_of_row(r) != "cobol" or r["signal"] in TEST_SIGNALS]
        agg["mean_best_phi"] = _mean([r["best_phi"] for r in kept])
        by_sig: dict = {}
        for sig in PHI_SIGNALS + tuple(sorted({r["signal"] for r in cobol} - set(PHI_SIGNALS))):
            xs = [r["best_phi"] for r in cobol if r["signal"] == sig]
            if xs:
                by_sig[sig or "?"] = _mean(xs)
        agg["cobol_mean_best_phi_by_signal"] = by_sig
        table = _facts_table(cobol)
        if table:
            agg["cobol_facts_by_class"] = table
    return agg


def _facts_table(rows: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        for cls, c in (r.get("facts_by_class") or {}).items():
            if not isinstance(c, dict):
                continue
            acc = out.setdefault(cls, {"original": 0, "matched": 0, "invented": 0})
            for k in acc:
                acc[k] += int(_num(c.get(k)) or 0)
    return dict(sorted(out.items()))


def suite_aggregate(rows: list[dict]) -> dict:
    agg = _common(rows, "best_score")
    ran = [r for r in rows if _measured(r)]
    agg["mean_best_cov"] = _mean([r["best_cov"] for r in ran if not r.get("cov_note")])
    agg["mean_best_mut"] = _mean([r["best_mut"] for r in ran if r["best_mut"] is not None])
    agg["delivered_suites"] = sum(1 for r in ran if r["best_size"] > 0)
    agg["delivered_tests"] = sum(r["best_size"] for r in ran)
    cobol = [r for r in rows if lang_of_row(r) == "cobol"]
    if cobol:
        agg["spec"] = sum(1 for r in cobol if r["status"] == "spec")
        agg["no_toolchain"] = sum(1 for r in cobol if r["status"] == "no-toolchain")
    return agg


# -- markdown -------------------------------------------------------------------


def _cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _traj(xs: list[float]) -> str:
    return " → ".join(f"{x:.2f}" for x in xs) if xs else "-"


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def _kv(d: dict) -> list[str]:
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            v = ", ".join(f"{a}: {b}" for a, b in v.items()) or "-"
        out.append(f"- **{k}**: {_cell(v) if not isinstance(v, str) else v}")
    return out


def _cov_cell(r: dict):
    return "n/a" if r.get("cov_note") else r["best_cov"]


def _language_table(summary: dict) -> list[str]:
    """one row per language: targets, skips per status, docs and suites."""
    analysis = summary.get("analysis") or {}
    by_tot = analysis.get("totals_by_language") or {}
    doc_by = summary["docgen"].get("by_language") or {}
    tst_by = summary["testgen"].get("by_language") or {}
    rows = []
    for lang in summary.get("languages") or []:
        tot = by_tot.get(lang) or {}
        statuses = tot.get("statuses") or {}
        skipped = ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())
                            if k not in ("ok", "no-funcs")) or "-"
        d = doc_by.get(lang) or {}
        t = tst_by.get(lang) or {}
        rows.append([lang, tot.get("packages"), tot.get("docgen_targets"), skipped,
                     d.get("ran"), d.get("mean_best_phi"), t.get("ran"), t.get("mean_best_score"),
                     t.get("mean_best_cov"), t.get("mean_best_mut")])
    return _table(["language", "packages", "targets", "skipped", "docs run", "mean φ",
                   "suites run", "mean score", "mean cov", "mean mut"], rows)


def render_markdown(summary: dict) -> str:
    lines = ["# CoDoc report", ""]
    meta = summary.get("run") or {}
    analysis = summary.get("analysis") or {}
    multi = bool(summary.get("languages"))
    if analysis:
        lines += ["## Analysis", ""]
        lines += _kv({k: analysis[k] for k in ("module", "go_version") if analysis.get(k)})
        if multi:
            lines.append(f"- **languages**: {', '.join(summary['languages'])}")
            for lang, ver in (analysis.get("versions") or {}).items():
                lines.append(f"- **{lang}**: {ver or 'no toolchain'}")
        lines += _kv(analysis.get("totals") or {})
        lines.append("")
    if meta:
        lines += ["## Run", ""]
        if meta.get("model"):
            lines.append(f"- **model**: {meta['model']}")
        if summary.get("tokens"):
            t = summary["tokens"]
            lines.append(f"- **tokens** (cumulative over every run into this OUT): "
                         f"{t.get('input', 0):,} in / {t.get('output', 0):,} out")
        for name, st in (meta.get("stages") or {}).items():
            if isinstance(st, dict):
                extra = f" (error: {st['error']})" if st.get("error") else ""
                lines.append(f"- **{name}**: {_cell(_num(st.get('seconds')))} s{extra}")
        lines.append("")

    if multi:
        lines += ["## Languages", ""]
        lines += _language_table(summary)
        lines.append("")

    doc = summary["docgen"]
    lines += ["## Module docs (SpecForge-go)", ""]
    if doc["rows"]:
        lines += _kv(doc["aggregate"])
        lines.append("")
        lead = ["lang"] if multi else []
        lines += _table(
            lead + ["file", "status", "signal", "rounds", "φ trajectory", "best φ",
                    "early stop"],
            [([lang_of_row(r)] if multi else [])
             + [r["file"], r["status"], r["signal"] or "-",
                f"{r['rounds']}/{_cell(r['max_rounds'])}", _traj(r["phi_trajectory"]),
                r["best_phi"], r["early_stop"]] for r in doc["rows"]],
        )
    else:
        lines.append("_no docgen results_")
    lines.append("")

    tst = summary["testgen"]
    lines += ["## Unit tests (TeSearch-go)", ""]
    if tst["rows"]:
        lines += _kv(tst["aggregate"])
        lines.append("")
        lead = ["lang"] if multi else []
        lines += _table(
            lead + ["file", "status", "rounds", "score trajectory", "best", "cov", "mut", "size",
                    "early stop"],
            [([lang_of_row(r)] if multi else [])
             + [r["file"], r["status"], f"{r['rounds']}/{_cell(r['max_rounds'])}",
                _traj(r["score_trajectory"]), r["best_score"], _cov_cell(r), r["best_mut"],
                r["best_size"], r["early_stop"]] for r in tst["rows"]],
        )
        notes = [r for r in tst["rows"] if r.get("cov_note")]
        if notes:
            lines.append("")
            lines += [f"- `{r['file']}`: {_cell(r['cov_note'][:300])}" for r in notes]
    else:
        lines.append("_no testgen results_")
    lines.append("")

    if summary.get("cobol"):
        lines += _cobol_sections(summary)

    errors = [r for r in doc["rows"] + tst["rows"] if r["error"]]
    if errors:
        lines += ["## Errors", ""]
        lines += [f"- `{r['file']}`: {_cell(r['error'][:300])}" for r in errors]
        lines.append("")
    return "\n".join(lines)


def _pct(v) -> str:
    return "-" if _num(v) is None else f"{float(v) * 100:.0f}%"


def _cobol_sections(summary: dict) -> list[str]:
    """COBOL test modes, phi per signal, the facts class table and the doc judge."""
    cob = summary["cobol"]
    lines: list[str] = []
    modes = cob.get("modes") or {}
    if modes:
        lines += ["## COBOL test modes", ""]
        rows = [[k, v] for k, v in modes.items()]
        lines += _table(["mode", "programs"], rows)
        kinds = cob.get("spec_kinds") or {}
        if kinds:
            lines.append("")
            lines.append("- **spec kinds**: " + ", ".join(f"{k} {v}" for k, v in kinds.items()))
        lines.append("")
    agg = summary["docgen"]["aggregate"]
    by_sig = agg.get("cobol_mean_best_phi_by_signal") or {}
    if by_sig:
        lines += ["## COBOL docs per signal", ""]
        lines += _table(["signal", "mean best φ"], [[k, v] for k, v in by_sig.items()])
        lines.append("")
        lines.append("_facts is a structural floor for programs without executable tests; "
                     "the doc judge is their primary quality signal._")
        lines.append("")
    facts = agg.get("cobol_facts_by_class") or {}
    if facts:
        lines += ["### Facts signal by class", ""]
        lines += _table(["class", "original", "matched", "invented"],
                        [[k, v.get("original"), v.get("matched"), v.get("invented")]
                         for k, v in facts.items()])
        lines.append("")
    judge = cob.get("judge")
    if judge:
        gate = judge.get("gate") if isinstance(judge.get("gate"), dict) else {}
        docs = _num(judge.get("docs")) or 0
        rate = (gate.get("pass", 0) / docs) if docs else None
        lines += ["## Doc judge", ""]
        lines.append(f"- **docs judged**: {judge.get('docs', 0)}")
        lines.append(f"- **gate pass rate**: {_pct(rate)} ({gate.get('pass', 0)} pass, "
                     f"{gate.get('fail', 0)} fail)")
        lines.append(f"- **accuracy**: {_pct(judge.get('accuracy_micro'))} of "
                     f"{judge.get('claims', 0)} claims")
        lines.append(f"- **coverage**: {_pct(judge.get('coverage_macro'))}")
        lines.append(f"- **high-severity errors**: {judge.get('high_errors', 0)}")
        if judge.get("judge_model"):
            lines.append(f"- **judge model**: {judge['judge_model']}")
        lines.append("")
    return lines


def _cobol_modes(out_dir: Path, analysis: dict | None) -> tuple[dict, dict]:
    """(rel -> mode, rel -> spec kind): OUT/cobol/modes.json (every program analysed into
    this OUT) overlaid with the last analysis."""
    block = (analysis or {}).get("cobol") if isinstance((analysis or {}).get("cobol"),
                                                        dict) else {}
    store = _load_json(out_dir / "cobol" / "modes.json") or {}
    modes = {**(store.get("modes") or {}), **(block.get("modes") or {})}
    kinds = {k: v for k, v in (store.get("spec_kinds") or {}).items()
             if k not in (block.get("modes") or {})}
    kinds.update(block.get("spec_kinds") or {})
    return modes, {k: v for k, v in kinds.items() if modes.get(k) == "spec"}


def _cobol_summary(out_dir: Path, analysis: dict | None, rows: list[dict],
                   test_rows: list[dict]) -> dict:
    """modes, spec kinds and testgen statuses of the COBOL programs, and the judge summary.
    the modes count the COBOL files the report lists (docgen or testgen rows); without rows,
    the programs of the last analysis."""
    modes_of, kinds_of = _cobol_modes(out_dir, analysis)
    files = {r["file"] for r in rows if lang_of_row(r) == "cobol" and r["file"] in modes_of}
    if not files:
        block = (analysis or {}).get("cobol") if isinstance((analysis or {}).get("cobol"),
                                                            dict) else {}
        files = set(block.get("modes") or {})
    modes = Counter(str(modes_of[f]) for f in files if f in modes_of)
    cobol_rows = [r for r in test_rows if lang_of_row(r) == "cobol"]
    nt = sum(1 for r in cobol_rows if r["status"] == "no-toolchain")
    out: dict = {}
    if modes or cobol_rows:
        ordered = {k: modes[k] for k in ("unit", "batch", "spec") if modes.get(k)}
        ordered.update({k: v for k, v in sorted(modes.items()) if k not in ordered})
        if nt:
            ordered["no-toolchain"] = nt
        out["modes"] = ordered
        kinds = [kinds_of[f] for f in files if f in kinds_of]
        if kinds:
            out["spec_kinds"] = dict(sorted(Counter(kinds).items()))
    judge = _load_json(out_dir / "judge" / "summary.json")
    if judge:
        out["judge"] = {k: judge.get(k) for k in ("docs", "gate", "accuracy_micro",
                                                  "coverage_macro", "claims", "high_errors",
                                                  "judge_model") if k in judge}
    return out


def _languages(analysis: dict | None, rows: list[dict]) -> list[str]:
    """the run's languages in multi mode ([] when everything is Go: no per-language parts)."""
    order = ("go", "c", "cpp", "java", "python", "cobol")
    found = {lang_of_row(r) for r in rows}
    listed = (analysis or {}).get("languages")
    if isinstance(listed, list):
        found |= {str(x) for x in listed}
    if found <= {"go"}:
        return []
    return [lang for lang in order if lang in found] + sorted(found - set(order))


def _by_language(rows: list[dict], langs: list[str], agg) -> dict:
    return {lang: agg([r for r in rows if lang_of_row(r) == lang]) for lang in langs}


def _skipped(analysis: dict) -> dict:
    """lang -> status -> count of the targets not processed (headers, generated, ...)."""
    out: dict[str, Counter] = {}
    for t in analysis.get("skipped") or []:
        if isinstance(t, dict):
            out.setdefault(t.get("lang") or "go", Counter())[str(t.get("status", ""))] += 1
    return {lang: dict(sorted(c.items())) for lang, c in sorted(out.items())}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_report(out_dir: Path) -> dict:
    """summary dict of every docgen/testgen result; writes summary.json and report.md."""
    out_dir = Path(out_dir)
    doc_rows = sorted((doc_row(d) for d in _results(out_dir / "docgen")),
                      key=lambda r: r["file"])
    test_rows = sorted((suite_row(d) for d in _results(out_dir / "testgen")),
                       key=lambda r: r["file"])
    summary: dict = {
        "docgen": {"aggregate": doc_aggregate(doc_rows), "rows": doc_rows},
        "testgen": {"aggregate": suite_aggregate(test_rows), "rows": test_rows},
    }
    analysis = _load_json(out_dir / "analysis.json")
    modes, _ = _cobol_modes(out_dir, analysis)
    for r in doc_rows + test_rows:
        if lang_of_row(r) == "cobol" and r["file"] in modes:
            r["mode"] = modes[r["file"]]
    if analysis:
        summary["analysis"] = {k: analysis.get(k) for k in ("module", "go_version", "totals")}
    langs = _languages(analysis, doc_rows + test_rows)
    if langs:
        summary["languages"] = langs
        summary["docgen"]["by_language"] = _by_language(doc_rows, langs, doc_aggregate)
        summary["testgen"]["by_language"] = _by_language(test_rows, langs, suite_aggregate)
        if analysis:
            for key in ("languages", "versions", "totals_by_language"):
                if key in analysis:
                    summary["analysis"][key] = analysis[key]
            summary["analysis"]["skipped_by_language"] = _skipped(analysis)
    if "cobol" in langs:
        cob = _cobol_summary(out_dir, analysis, doc_rows + test_rows, test_rows)
        if cob:
            summary["cobol"] = cob
    meta = _load_json(out_dir / "run_meta.json")
    if meta:
        settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
        summary["run"] = {"model": settings.get("model", ""), "stages": meta.get("stages") or {}}
        if isinstance(meta.get("tokens"), dict):
            summary["tokens"] = meta["tokens"]
    _write(out_dir / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    _write(out_dir / "report.md", render_markdown(summary))
    return summary
