from __future__ import annotations

import json
from pathlib import Path

import pytest

from codocs.report import build_report, doc_row, suite_row


def _doc_round(r: int, phi: float, signal: str = "tests") -> dict:
    return {
        "round": r, "doc": "# doc", "code": "package calc\n", "missing": [], "seconds": 1.5,
        "eval": {"signal": signal, "build_ok": phi > 0, "phi": phi, "relevant": ["TestA"],
                 "passed": [], "failing": {}, "build_errors": [], "import_fixes": 0},
    }


def _doc_result(pkg, file, phis, status="ok", signal="tests", early=False, **extra) -> dict:
    d = {
        "pkg": pkg, "file": file, "stem": Path(file).stem, "status": status, "signal": signal,
        "rounds": [_doc_round(i + 1, p, signal) for i, p in enumerate(phis)],
        "best_round": (phis.index(max(phis)) + 1) if phis else 0,
        "best_phi": max(phis) if phis else 0.0,
        "early_stop": early, "threshold": 0.9, "max_rounds": 3, "error": "",
    }
    d.update(extra)
    return d


def _measurement(cov: float, mut: float | None, size: int = 3, build_ok: bool = True) -> dict:
    return {
        "build_ok": build_ok, "build_errors": [], "size": size, "passing": [], "failing": {},
        "green_src": "", "green_size": size if build_ok else 0, "cov": cov, "uncovered": [],
        "mut": mut, "mutants_total": 10, "mutants_valid": 8, "killed": 4, "survivors": [],
    }


def _suite_result(pkg, file, ms, status="ok", early=False, **extra) -> dict:
    rounds = [{"round": i + 1, "suite": "package x\n", "measurement": m, "seconds": 2.0}
              for i, m in enumerate(ms)]
    d = {
        "pkg": pkg, "file": file, "stem": Path(file).stem, "status": status, "rounds": rounds,
        "best_round": 0, "best_score": 0.0, "best_cov": 0.0, "best_mut": None, "best_size": 0,
        "early_stop": early, "threshold": 0.85, "max_rounds": 3, "error": "",
    }
    d.update(extra)
    return d


def _put(out: Path, stage: str, pkg: str, stem: str, data: dict) -> None:
    d = out / stage / (pkg or "_root") / stem
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(data), encoding="utf-8")
    (d / "round_1").mkdir(exist_ok=True)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    _put(out, "docgen", "calc", "stats", _doc_result("calc", "calc/stats.go", [0.5, 1.0],
                                                     early=True))
    _put(out, "docgen", "calc", "calc", _doc_result("calc", "calc/calc.go", [0.0, 0.25, 0.5],
                                                    signal="compile"))
    _put(out, "docgen", "shapes", "gen", _doc_result("shapes", "shapes/gen.go", [],
                                                     status="generated", signal=""))
    _put(out, "docgen", "a/b", "x", _doc_result("a/b", "a/b/x.go", [], status="error",
                                                error="LLMError: boom | bad"))
    _put(out, "testgen", "calc", "stats", _suite_result(
        "calc", "calc/stats.go", [_measurement(0.5, 0.5), _measurement(1.0, 0.9)],
        early=True, best_round=2, best_score=0.9, best_cov=1.0, best_mut=0.9, best_size=3))
    # no explicit best_score: derived from the measurements (mut None -> cov alone)
    m1 = _measurement(0.0, None, build_ok=False)
    m2 = _measurement(0.6, None, size=2)
    data = _suite_result("calc", "calc/calc.go", [m1, m2], best_round=2, best_cov=0.6,
                         best_size=2)
    del data["best_score"]
    _put(out, "testgen", "calc", "calc", data)
    (out / "run_meta.json").write_text(json.dumps({
        "settings": {"model": "gpt-5.4"},
        "stages": {"testgen": {"seconds": 12.5}, "wiki": {"seconds": 1, "error": "boom"}},
        "tokens": {"input": 1234, "output": 56},
    }))
    (out / "analysis.json").write_text(json.dumps({
        "module": "example.com/fixture", "go_version": "go1.23.12",
        "totals": {"packages": 2, "files": 3},
    }))
    return out


def test_build_report(out: Path):
    s = build_report(out)
    doc = s["docgen"]
    assert [r["file"] for r in doc["rows"]] == ["a/b/x.go", "calc/calc.go", "calc/stats.go",
                                                 "shapes/gen.go"]
    by = {r["file"]: r for r in doc["rows"]}
    assert by["calc/stats.go"]["phi_trajectory"] == [0.5, 1.0]
    assert by["calc/stats.go"]["rounds"] == 2 and by["calc/stats.go"]["early_stop"]
    assert by["calc/calc.go"]["best_phi"] == 0.5 and by["calc/calc.go"]["signal"] == "compile"
    assert by["calc/stats.go"]["seconds"] == 3.0
    agg = doc["aggregate"]
    assert agg["files"] == 4 and agg["ran"] == 2
    assert agg["mean_best_phi"] == 0.75
    assert agg["early_stop_rate"] == 0.5
    assert agg["rounds_histogram"] == {"2": 1, "3": 1}
    assert agg["signals"] == {"compile": 1, "tests": 1}
    assert agg["statuses"] == {"error": 1, "generated": 1, "ok": 2}
    assert agg["errors"] == 1 and agg["reached_threshold"] == 1

    tst = s["testgen"]
    by = {r["file"]: r for r in tst["rows"]}
    assert by["calc/stats.go"]["score_trajectory"] == [0.25, 0.9]
    assert by["calc/calc.go"]["score_trajectory"] == [0.0, 0.6]
    assert by["calc/calc.go"]["best_score"] == 0.6 and by["calc/calc.go"]["best_mut"] is None
    agg = tst["aggregate"]
    assert agg["files"] == 2 and agg["ran"] == 2
    assert agg["mean_best_score"] == 0.75
    assert agg["mean_best_cov"] == 0.8 and agg["mean_best_mut"] == 0.9
    assert agg["delivered_suites"] == 2 and agg["delivered_tests"] == 5
    assert agg["early_stop_rate"] == 0.5

    assert s["tokens"] == {"input": 1234, "output": 56}
    assert s["analysis"]["module"] == "example.com/fixture"

    written = json.loads((out / "summary.json").read_text())
    assert written == json.loads(json.dumps(s))
    md = (out / "report.md").read_text()
    assert "| calc/stats.go | ok | tests | 2/3 | 0.50 → 1.00 | 1.00 | yes |" in md
    assert "| calc/stats.go | ok | 2/3 | 0.25 → 0.90 | 0.90 | 1.00 | 0.90 | 3 | yes |" in md
    assert "| shapes/gen.go | generated | - | 0/3 | - | 0.00 | no |" in md
    assert "1,234 in / 56 out" in md
    assert "boom \\| bad" in md
    assert "(error: boom)" in md


def test_rows_tolerate_sparse_results():
    r = doc_row({"file": "x.go", "rounds": [{"eval": {"phi": 0.3}}, "junk", {}]})
    assert r["phi_trajectory"] == [0.3, 0.0] and r["best_phi"] == 0.3 and r["signal"] == ""
    r = suite_row({"file": "y.go", "rounds": [{"measurement": {"score": 0.7}}],
                   "score_trajectory": [0.7]})
    assert r["best_score"] == 0.7 and r["best_size"] == 0 and r["best_mut"] is None


def test_empty_out(tmp_path: Path):
    s = build_report(tmp_path)
    assert s["docgen"]["rows"] == [] and s["docgen"]["aggregate"]["mean_best_phi"] is None
    assert s["testgen"]["aggregate"]["early_stop_rate"] is None
    assert "tokens" not in s
    md = (tmp_path / "report.md").read_text()
    assert "_no docgen results_" in md and "_no testgen results_" in md


def test_unreadable_result_skipped(out: Path):
    bad = out / "docgen" / "zz" / "broken"
    bad.mkdir(parents=True)
    (bad / "result.json").write_text("{not json")
    s = build_report(out)
    assert s["docgen"]["aggregate"]["files"] == 4
