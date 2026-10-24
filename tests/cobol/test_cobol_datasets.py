"""the three COBOL datasets through the CLI, without an LLM: `analyze`, `wiki --plan-only`
and `bizlogic --plan-only` with the manifest scope; the static facts against the index goldens
(index parity through the adapter and the CLI); the bizlogic program set against the analysis;
the neutralised-compile rate of EXEC-using programs.

datasets are read-only and found through the `datasets` fixture ($CODOCS_DATASETS); a missing
dataset skips its tests.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from pipeline_fixtures import invoke, isolate, load

REPO = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden" / "index"
NAMES = ("carddemo", "genapp", "cbi")
# neutralised-compile floors measured on the pinned dataset commits:
# (compiling, EXEC-using programs)
COMPILE_FLOORS = {"carddemo": (30, 30), "genapp": (30, 31), "cbi": (0, 0)}


def _mg():
    spec = importlib.util.spec_from_file_location(
        "codocs_make_goldens_ds", REPO / "scripts" / "cobol" / "make_goldens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MG = _mg()
ENTRIES = {e["name"]: e for e in MG.manifest()}


def _scope_args(name: str) -> list[str]:
    subtree, excludes = MG.dataset_scope(ENTRIES[name])
    args = ["--source-lang", "cobol"]
    if subtree:
        args += ["--subtree", subtree]
    for ex in excludes:
        args += ["--exclude-path", ex]
    return args


@pytest.fixture(scope="module", params=NAMES)
def analysed(request, tmp_path_factory, datasets):
    name = request.param
    root = datasets(name)
    if root is None:
        pytest.skip(f"dataset {name} not present")
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp(name)
    try:
        isolate(mp, tmp)
        mp.delenv("OPENAI_API_KEY")
        out = tmp / "out"
        res = invoke("analyze", root, "-o", out, *_scope_args(name))
        yield {"name": name, "root": root, "out": out, "res": res, "mp": mp}
    finally:
        mp.undo()


def test_analyze_matches_the_legacy_facts(analysed):
    res, out, name = analysed["res"], analysed["out"], analysed["name"]
    assert res.exit_code == 0, res.output
    assert "Most depended-upon programs" in res.output
    golden = json.loads((GOLDEN / f"dataset-{name}.json").read_text(encoding="utf-8"))
    index = load(out / "cobol" / "index.json")
    index["root"] = index["summary"]["root"] = "<ROOT>"
    digest = MG.dataset_digest(_Wrapped(index))
    for key in ("summary", "entrypoints", "groups", "edge_counts", "transactions", "records",
                "edges"):
        assert digest[key] == golden[key], (name, key)
    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["cobol"]
    summary = dict(analysis["cobol"]["summary"], root="<ROOT>")
    assert summary == golden["summary"]
    subtree, excludes = MG.dataset_scope(ENTRIES[name])
    assert analysis["cobol"]["scope"] == {"subtree": subtree, "exclude_paths": excludes}
    progs = {t["file"] for t in analysis["targets"] if t["status"] != "copybook"}
    assert progs == {p["path"] for p in index["programs"].values()}


def test_wiki_and_bizlogic_plan_only(analysed):
    name, root, out = analysed["name"], analysed["root"], analysed["out"]
    res = invoke("wiki", root, "-o", out, "--plan-only", *_scope_args(name))
    assert res.exit_code == 0, res.output
    assert "Plan:" in res.output
    res = invoke("bizlogic", root, "-o", out, "--plan-only", "-k", "5", *_scope_args(name))
    assert res.exit_code == 0, res.output
    graph = load(out / "bizlogic" / "graph.json")
    subtree, excludes = MG.dataset_scope(ENTRIES[name])
    assert graph.get("exclude_paths", []) == excludes
    assert graph["stats"]["languages"]["cobol"]["functions"] > 0

    from codocs.bizlogic.graph import build_graph

    fg = build_graph(root, subtree=subtree, languages="cobol", out_dir=out,
                     exclude_paths=tuple(excludes))
    entries = {n.name for n in fg.nodes.values()
               if n.lang == "cobol" and n.kind == "func" and "." not in n.name}
    index = load(out / "cobol" / "index.json")
    assert entries == set(index["programs"]), (name, sorted(entries ^ set(index["programs"])))


@pytest.mark.needs_cobc
def test_neutralised_compile_rate(analysed, record_property):
    """programs using EXEC CICS/SQL/DLI whose original passes the neutralised compile."""
    from codocs.cobol.harness import CobolTestRunner
    from codocs.cobol.index import CobolRepoIndex

    name, root, out = analysed["name"], analysed["root"], analysed["out"]
    subtree, excludes = MG.dataset_scope(ENTRIES[name])
    index = CobolRepoIndex.build(root, subtree, excludes)
    h = CobolTestRunner(index, out / "work" / "cobol", source_root=root)
    files: dict[str, str] = {}
    for pid, p in index.programs.items():
        if p.has_cics or p.has_sql or p.has_dli:
            files.setdefault(p.path, pid)
    ok = sorted(rel for rel in files if h.compile_check(rel, neutralize=True)[0])
    record_property(f"{name}_neutralised_compile", f"{len(ok)}/{len(files)}")
    print(f"{name}: neutralised compile {len(ok)}/{len(files)}")
    floor_ok, floor_n = COMPILE_FLOORS[name]
    assert len(files) == floor_n
    assert len(ok) >= floor_ok, sorted(set(files) - set(ok))
    if name == "carddemo":
        base = [r for r, pid in files.items()
                if r.startswith("app/cbl/") and index.programs[pid].has_cics]
        assert len(base) == 17 and set(base) <= set(ok)


class _Wrapped:
    """an index.json dict seen through the to_dict() the golden digest reads."""

    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return self._data
