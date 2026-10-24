"""tests for leaf-node markdown documentation ingestion."""

from codocs.repowiki.analysis.leafdocs import LeafDocIndex, digest_from_static
from codocs.repowiki.deepdoc.models import GoPackage, GoSymbol

_FUNC_DOC = """\
## FunctionDef run(cfg)
**run**: The function of `run` is to drive the worker loop. It also does more things.

**parameters**: ...

## ClassDef jobStatus
**jobStatus**: The function of jobStatus is to hold the state of one job.

**attributes**: ...
"""


def _make_docs(tmp_path):
    pkg = tmp_path / "worker"
    docs = pkg / "markdown_docs"
    docs.mkdir(parents=True)
    (docs / "run.md").write_text(_FUNC_DOC)

    # nested docs dir mirroring a subpackage
    sub = docs / "grpc"
    sub.mkdir()
    (sub / "srv.md").write_text(
        "## FunctionDef Serve\n**Serve**: The function of Serve is to start the gRPC server.\n"
    )
    (tmp_path / "worker" / "grpc").mkdir()
    return tmp_path


def test_parse_symbols_and_summaries(tmp_path):
    root = _make_docs(tmp_path)
    idx = LeafDocIndex.build(root, known_packages={"worker", "worker/grpc"})
    digest = idx.digests["worker"]
    assert digest.source == "leafdocs"
    entries = {e.name: e for e in digest.entries}
    assert entries["run"].kind == "function"
    assert entries["jobStatus"].kind == "class"
    # bold prefix stripped, sentence extracted
    assert entries["run"].summary.startswith("The function of")
    assert "**" not in entries["run"].summary


def test_nested_docs_map_to_subpackage(tmp_path):
    root = _make_docs(tmp_path)
    idx = LeafDocIndex.build(root, known_packages={"worker", "worker/grpc"})
    assert "worker/grpc" in idx.digests
    assert idx.digests["worker/grpc"].entries[0].name == "Serve"


def test_nested_docs_fall_back_to_owner_when_unknown(tmp_path):
    root = _make_docs(tmp_path)
    idx = LeafDocIndex.build(root, known_packages={"worker"})
    names = {e.name for e in idx.digests["worker"].entries}
    assert "Serve" in names


def test_coverage(tmp_path):
    root = _make_docs(tmp_path)
    idx = LeafDocIndex.build(root, known_packages={"worker", "worker/grpc"})
    assert idx.coverage(["worker", "worker/grpc", "other"]) == (2, 3)


def test_digest_from_static_fallback():
    pkg = GoPackage(
        path="util",
        doc_comment="Package util has helpers.",
        symbols=[
            GoSymbol(name="Helper", kind="func", file="util/util.go"),
            GoSymbol(name="hidden", kind="func", file="util/util.go", exported=False),
            GoSymbol(name="Thing", kind="type", file="util/thing.go"),
            GoSymbol(name="Do", kind="method", receiver="Thing", file="util/thing.go"),
        ],
    )
    digest = digest_from_static(pkg)
    assert digest.source == "static"
    names = [e.name for e in digest.entries]
    assert names[0] == "(package doc)"
    assert "Thing" in names and "Thing.Do" in names
    assert "hidden" not in names
