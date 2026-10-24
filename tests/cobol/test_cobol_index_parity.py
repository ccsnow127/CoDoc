"""the COBOL index reproduces the reference facts under tests/cobol/golden/index exactly.

The goldens are reference outputs of the index; regenerate them with
`scripts/cobol/make_goldens.py index`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import grounding_fixtures
import pytest
import wiki_fixtures

from codocs.cobol.index import CobolRepoIndex

REPO = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden" / "index"
FIXTURES = REPO / "tests" / "fixtures"


def _make_goldens():
    spec = importlib.util.spec_from_file_location(
        "make_goldens", REPO / "scripts" / "cobol" / "make_goldens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MG = _make_goldens()


def _golden(name: str) -> dict:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("builder", ["flat_repo", "dir_repo", "chain_repo"])
def test_fixture_builders_match_legacy(builder: str, tmp_path: Path):
    mod = grounding_fixtures if builder == "chain_repo" else wiki_fixtures
    root = getattr(mod, builder)(tmp_path / builder / "repo")
    assert MG.normalised_index(CobolRepoIndex.build(root)) == _golden(builder)


@pytest.mark.parametrize("fixture", ["cobolmod", "mixedcobol"])
def test_fixture_repos_match_legacy(fixture: str):
    assert MG.normalised_index(CobolRepoIndex.build(FIXTURES / fixture)) == _golden(fixture)


def test_skip_prunes_the_output_dir(cobolmod_copy: Path):
    """OUT inside the repo never contributes programs (docgen code.cbl copies)."""
    out = cobolmod_copy / "codocs-run"  # "out" itself is in SKIP_DIRS
    code = out / "docgen" / "cbl" / "DATEUTIL.cbl" / "round_1"
    code.mkdir(parents=True)
    shutil.copy(cobolmod_copy / "cbl" / "DATEUTIL.cbl", code / "code.cbl")
    polluted = CobolRepoIndex.build(cobolmod_copy)
    assert any(f.path.startswith("codocs-run/") for f in polluted.files)
    clean = CobolRepoIndex.build(cobolmod_copy, skip=(out,))
    assert MG.normalised_index(clean) == _golden("cobolmod")
    assert MG.normalised_index(CobolRepoIndex.build(cobolmod_copy, skip=(str(out),))) == \
        _golden("cobolmod")


@pytest.mark.parametrize("name", ["carddemo", "genapp", "cbi"])
def test_datasets_match_legacy(name: str, datasets):
    root = datasets(name)
    if root is None:
        pytest.skip(f"dataset {name} not present")
    entry = next(e for e in MG.manifest() if e["name"] == name)
    subtree, exclude = MG.dataset_scope(entry)
    golden = _golden(f"dataset-{name}")
    assert golden["scope"] == {"subtree": subtree, "exclude": exclude}
    digest = MG.dataset_digest(CobolRepoIndex.build(root, subtree=subtree, exclude=exclude))
    digest["scope"] = golden["scope"]
    assert digest == golden
