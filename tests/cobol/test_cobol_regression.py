"""COBOL support leaves the other languages alone.

- the rendered system prompts of python/java/c/cpp equal the golden snapshot;
- detection, units, targets and statuses of the non-COBOL fixtures equal the snapshot;
- the Go-only analysis has no COBOL key; the judge stage never appears in Go runs;
- a copy of pymod with one .cbl file processes COBOL under auto;
- CODOCS_CODEGEN_MODEL never reaches other languages' fakes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pipeline_fixtures import FIXTURES, invoke, isolate, load

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "cobol" / "golden"


def _goldens():
    spec = importlib.util.spec_from_file_location(
        "codocs_make_goldens", REPO / "scripts" / "cobol" / "make_goldens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MG = _goldens()
# toolchains `codocs analyze` needs for each snapshot fixture (the goldens were recorded
# with all of them present)
_SNAPSHOT_TOOLS = {"gomod": ("go",), "mixedmod": ("go", "cc"), "javamod": ("mvn",),
                   "cmod": ("cc",), "cppmod": ("c++",)}


def test_rendered_prompts_of_the_other_languages_unchanged():
    got = MG.prompt_snapshots()
    assert sorted(got) == sorted(p.name for p in (GOLDEN / "prompts").glob("*.txt"))
    for name, text in got.items():
        assert text == (GOLDEN / "prompts" / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("fixture", MG.SNAPSHOT_FIXTURES)
@pytest.mark.parametrize("mode", ["auto", "all"])
def test_analysis_of_pre_merge_fixtures_unchanged(fixture, mode, tmp_path):
    for tool in _SNAPSHOT_TOOLS.get(fixture, ()):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not installed")
    want = json.loads((GOLDEN / "analysis" / f"{fixture}.{mode}.json").read_text("utf-8"))
    got = json.loads(json.dumps(MG.analysis_snapshot(fixture, mode, tmp_path),
                                sort_keys=True))
    assert got == want


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_go_legacy_analysis_has_no_cobol_keys(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    repo = tmp_path / "gomod"
    shutil.copytree(FIXTURES / "gomod", repo)
    out = tmp_path / "out"
    res = invoke("analyze", repo, "-o", out)
    assert res.exit_code == 0, res.output
    data = load(out / "analysis.json")
    assert "cobol" not in data and "languages" not in data
    assert "programs" not in data["selection"]
    assert not (out / "cobol").exists()


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_go_module_with_cobol_files_notes_them(monkeypatch, tmp_path):
    """the "also found" note of a Go repo that holds COBOL files lists cobol N."""
    isolate(monkeypatch, tmp_path)
    repo = tmp_path / "gomod"
    shutil.copytree(FIXTURES / "gomod", repo)
    (repo / "legacy").mkdir()
    shutil.copy(FIXTURES / "cobolmod" / "cbl" / "DAYSINM.cbl", repo / "legacy" / "DAYSINM.cbl")
    res = invoke("analyze", repo)
    assert res.exit_code == 0, res.output
    assert "also found: cobol 1" in " ".join(res.output.split())


def test_pymod_with_a_cbl_file_processes_cobol(monkeypatch, tmp_path):
    """a Python repo that also holds a .cbl program processes COBOL under auto; the Python
    part of the analysis matches the snapshot."""
    isolate(monkeypatch, tmp_path)
    repo = tmp_path / "pymod"
    shutil.copytree(FIXTURES / "pymod", repo, ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "cobol").mkdir()
    shutil.copy(FIXTURES / "cobolmod" / "cbl" / "DAYSINM.cbl", repo / "cobol" / "DAYSINM.cbl")
    out = tmp_path / "out"
    res = invoke("analyze", repo, "-o", out)
    assert res.exit_code == 0, res.output
    data = load(out / "analysis.json")
    assert data["languages"] == ["python", "cobol"]
    assert data["cobol"]["modes"] == {"cobol/DAYSINM.cbl": "unit"}
    want = json.loads((GOLDEN / "analysis" / "pymod.auto.json").read_text("utf-8"))
    py = [t for t in data["targets"] if t["lang"] == "python"]
    assert [(t["file"], t["status"]) for t in py] == \
        [(t["file"], t["status"]) for t in want["targets"]]
    assert [t for t in data["targets"] if t["lang"] == "cobol"] == [
        {"file": "cobol/DAYSINM.cbl", "lang": "cobol", "lines": data["targets"][-1]["lines"],
         "pkg": "cobol", "status": "ok", "stem": "DAYSINM.cbl"}]


def test_codegen_model_never_reaches_python_fakes(monkeypatch, tmp_path):
    """a fake LLM without a `model` parameter keeps working with CODOCS_CODEGEN_MODEL set."""
    sys.path.insert(0, str(REPO / "tests" / "codocs"))
    try:
        from _e2e_lang import install, run_cli
        from test_e2e_python import SUITES
    finally:
        sys.path.remove(str(REPO / "tests" / "codocs"))
    repo, out, fake = install(monkeypatch, tmp_path, "pymod", SUITES)
    monkeypatch.setenv("CODOCS_CODEGEN_MODEL", "test/codegen-model")
    monkeypatch.setenv("CODOCS_PY", sys.executable)
    res = run_cli(repo, out, "--skip", "wiki,testgen", "--doc-rounds", "1")
    assert res.exit_code == 0, res.output
    assert any(kind == "code" for kind, _ in fake.calls)
    assert "judge" not in load(out / "run_meta.json")["stages"]


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_go_golden_with_codegen_model_set():
    env = {**os.environ, "CODOCS_CODEGEN_MODEL": "test/codegen-model", "CODOCS_JUDGE_MODEL": "judge"}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "tests/codocs/test_go_golden.py"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
