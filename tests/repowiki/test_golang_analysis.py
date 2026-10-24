"""tests for the Go module static analyzer."""

import pytest

from codocs.repowiki.analysis.golang import GoModuleIndex, _classify_import


@pytest.fixture
def go_module(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n")

    cmd = tmp_path / "cmd" / "app"
    cmd.mkdir(parents=True)
    (cmd / "main.go").write_text(
        'package main\n\n'
        'import (\n'
        '\t"fmt"\n'
        '\t"example.com/demo/core"\n'
        '\t"github.com/gin-gonic/gin"\n'
        ')\n\n'
        'func main() {\n\tfmt.Println(core.New(), gin.New())\n}\n'
    )

    core = tmp_path / "core"
    core.mkdir()
    (core / "core.go").write_text(
        '// Package core implements the demo engine.\n'
        'package core\n\n'
        'import "example.com/demo/util"\n\n'
        'type Engine struct{}\n\n'
        'func New() *Engine { return &Engine{} }\n\n'
        'func (e *Engine) Run() string { return util.Helper() }\n\n'
        'func internalOnly() {}\n'
    )
    (core / "core_test.go").write_text('package core\n\nimport "testing"\n\nfunc TestNew(t *testing.T) {}\n')

    util = tmp_path / "util"
    util.mkdir()
    (util / "util.go").write_text(
        'package util\n\nimport "strings"\n\nfunc Helper() string { return strings.ToUpper("x") }\n'
    )
    return tmp_path


def test_package_discovery_and_module_name(go_module):
    idx = GoModuleIndex.build(go_module)
    assert idx.module == "example.com/demo"
    assert set(idx.packages) == {"cmd/app", "core", "util"}


def test_import_classification(go_module):
    idx = GoModuleIndex.build(go_module)
    app = idx.packages["cmd/app"]
    assert app.imports_internal == ["core"]
    assert app.imports_external == ["github.com/gin-gonic/gin"]
    assert app.imports_stdlib == ["fmt"]


def test_classify_import_edge_cases():
    assert _classify_import("example.com/demo/x/y", "example.com/demo") == ("internal", "x/y")
    assert _classify_import("os/exec", "example.com/demo") == ("stdlib", "os/exec")
    assert _classify_import("golang.org/x/net", "example.com/demo") == (
        "external",
        "golang.org/x/net",
    )


def test_dependency_graph_and_rank(go_module):
    idx = GoModuleIndex.build(go_module)
    assert idx.graph.has_edge("cmd/app", "core")
    assert idx.graph.has_edge("core", "util")
    # util is the most transitively depended-upon leaf
    ranked = [p for p, _ in idx.rank_packages()]
    assert ranked[0] in ("util", "core")
    assert ranked.index("util") < ranked.index("cmd/app")


def test_symbols_and_main_detection(go_module):
    idx = GoModuleIndex.build(go_module)
    core = idx.packages["core"]
    names = {(s.name, s.kind) for s in core.symbols}
    assert ("Engine", "type") in names
    assert ("New", "func") in names
    assert ("Run", "method") in names
    run = next(s for s in core.symbols if s.name == "Run")
    assert run.receiver == "Engine"
    assert not next(s for s in core.symbols if s.name == "internalOnly").exported
    assert core.test_files == 1
    assert core.doc_comment == "Package core implements the demo engine."
    assert idx.packages["cmd/app"].is_main
    assert idx.entrypoints() == ["cmd/app"]


def test_group_and_edge_queries(go_module):
    idx = GoModuleIndex.build(go_module)
    assert idx.group_edges() == {("cmd", "core"): 1, ("core", "util"): 1}
    assert idx.subgraph_edges(["core", "util"]) == [("core", "util")]
    assert idx.outbound_edges(["cmd/app"]) == {"cmd/app": ["core"]}
    stack = idx.tech_stack()
    assert stack and stack[0][0] == "Gin"


def test_subtree_restriction(go_module):
    idx = GoModuleIndex.build(go_module, subtree="core")
    assert set(idx.packages) == {"core"}
