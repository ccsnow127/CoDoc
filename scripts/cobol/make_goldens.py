"""COBOL goldens, written under tests/cobol/golden/.

  index      COBOL index goldens; regenerate them when an index change is intended:
             python scripts/cobol/make_goldens.py index
             -> golden/index/<repo>.json (full to_dict() of each fixture repo, root
             normalised) and golden/index/dataset-<name>.json (digests of the datasets
             under $CODOCS_DATASETS; skipped when it is unset)
  snapshots  regression snapshots taken with this repo's venv:
             .venv/bin/python scripts/cobol/make_goldens.py snapshots
             -> golden/prompts/<lang>.<name>.txt, golden/analysis/<fixture>.<auto|all>.json

The datasets directory is read-only: nothing is written there. `index` never imports from
tests/ except the fixture repo builders of tests/cobol/_helpers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "cobol" / "golden"
HELPERS = REPO / "tests" / "cobol" / "_helpers"
FIXTURES = REPO / "tests" / "fixtures"
COBOL_FIXTURES = ("cobolmod", "mixedcobol")
BUILDERS = ("flat_repo", "dir_repo", "chain_repo")
SNAPSHOT_FIXTURES = ("gomod", "pymod", "javamod", "cmod", "cppmod", "mixedmod")
PROMPT_LANGS = ("python", "java", "c", "cpp")
# analysis.json keys that depend on the host (paths, toolchain versions)
_HOST_KEYS = ("module_root", "go_version", "versions")


def datasets_dir() -> Path | None:
    """$CODOCS_DATASETS, or None when it is unset."""
    env = os.environ.get("CODOCS_DATASETS")
    return Path(env).expanduser() if env else None


def _sha(data) -> str:
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


# -- index parity -----------------------------------------------------------------------


def normalised_index(index) -> dict:
    """CobolRepoIndex.to_dict() with the absolute root replaced (parity golden format)."""
    data = index.to_dict()
    data["root"] = "<ROOT>"
    data["summary"]["root"] = "<ROOT>"
    return json.loads(json.dumps(data, default=str))


def dataset_digest(index) -> dict:
    """summary, entry points, groups, edge counts, transactions and record hashes."""
    data = normalised_index(index)
    return {
        "summary": data["summary"],
        "entrypoints": data["entrypoints"],
        "groups": data["groups"],
        "edge_counts": data["edge_counts"],
        "transactions": data["transactions"],
        "records": {kind: {k: _sha(v) for k, v in data[kind].items()}
                    for kind in ("programs", "copybooks", "jobs", "datastores", "mapsets")},
        "edges": _sha(data["edges"]),
    }


def manifest() -> list[dict]:
    path = REPO / "datasets" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))["datasets"]


def dataset_scope(entry: dict) -> tuple[str | None, list[str]]:
    """(subtree, exclude globs) of a manifest entry, as run_dataset.sh passes them."""
    root = entry.get("root") or None
    excl = [f"{root}/{e}" if root else e for e in entry.get("exclude") or []]
    return root, excl


def make_index_goldens() -> None:
    sys.path.insert(0, str(HELPERS))
    import grounding_fixtures
    import wiki_fixtures

    from codocs.cobol.index import CobolRepoIndex

    out = GOLDEN / "index"
    with tempfile.TemporaryDirectory() as tmp:
        for name in BUILDERS:
            mod = grounding_fixtures if name == "chain_repo" else wiki_fixtures
            root = getattr(mod, name)(Path(tmp) / name / "repo")
            _dump(out / f"{name}.json", normalised_index(CobolRepoIndex.build(root)))
            print(f"index golden: {name}")
    for name in COBOL_FIXTURES:
        _dump(out / f"{name}.json", normalised_index(CobolRepoIndex.build(FIXTURES / name)))
        print(f"index golden: {name}")
    base = datasets_dir()
    for entry in manifest():
        root = base / entry["name"] if base is not None else None
        if root is None or not root.is_dir():
            print(f"dataset {entry['name']}: not present, skipped")
            continue
        subtree, exclude = dataset_scope(entry)
        idx = CobolRepoIndex.build(root, subtree=subtree, exclude=exclude)
        digest = dataset_digest(idx)
        digest["scope"] = {"subtree": subtree, "exclude": exclude}
        _dump(out / f"dataset-{entry['name']}.json", digest)
        print(f"index golden: dataset {entry['name']} ({digest['summary']['programs']} programs)")


# -- regression snapshots (this repo) -----------------------------------------------------


def prompt_snapshots() -> dict[str, str]:
    from codocs.specforge.prompts import TEMPLATES_DIR, render_template

    out = {}
    for lang in PROMPT_LANGS:
        for t in sorted(TEMPLATES_DIR.glob("*.txt")):
            out[f"{lang}.{t.stem}.txt"] = render_template(t.stem, lang)
    return out


def _normalise(value, root: str):
    if isinstance(value, dict):
        return {k: _normalise(v, root) for k, v in value.items() if k not in _HOST_KEYS}
    if isinstance(value, list):
        return [_normalise(v, root) for v in value]
    if isinstance(value, str):
        return value.replace(root, "<REPO>")
    return value


def analysis_snapshot(fixture: str, mode: str, tmp: Path) -> dict:
    """OUT/analysis.json of `codocs analyze --source-lang <mode>` on a copy of the fixture,
    without host-dependent keys."""
    repo = tmp / mode / fixture
    shutil.copytree(FIXTURES / fixture, repo,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    out = tmp / mode / f"out-{fixture}"
    cmd = [sys.executable, "-m", "codocs", "analyze", str(repo), "-o", str(out),
           "--source-lang", mode]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp)
    if proc.returncode != 0:
        raise SystemExit(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    return _normalise(data, str(repo.resolve()))


def make_snapshots() -> None:
    (GOLDEN).mkdir(parents=True, exist_ok=True)
    for name, text in prompt_snapshots().items():
        path = GOLDEN / "prompts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print("prompt snapshots written")
    with tempfile.TemporaryDirectory() as tmp:
        for fixture in SNAPSHOT_FIXTURES:
            for mode in ("auto", "all"):
                _dump(GOLDEN / "analysis" / f"{fixture}.{mode}.json",
                      analysis_snapshot(fixture, mode, Path(tmp)))
                print(f"analysis snapshot: {fixture} ({mode})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("what", choices=("index", "snapshots"))
    args = ap.parse_args()
    if args.what == "index":
        make_index_goldens()
    else:
        make_snapshots()


if __name__ == "__main__":
    main()
