"""cross-language integration: missing toolchains, cgo imports, detection with --subtree,
markup in messages, missing tree-sitter, build output, OUT inside the repo, C++ doc headers
and the adapter hooks."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from test_loops_lang import PY_SUITES, ScriptedLLM, copy_fixture, prepare, settings

from codocs import pipeline as pl
from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.graph import build_graph
from codocs.cli import _settings, cli
from codocs.lang import treesitter as ts
from codocs.lang.base import LangTestResult, RepoContext
from codocs.lang.registry import adapter, detect_languages, scan_files
from codocs.specforge.loop import load_result, run_docgen
from codocs.tesearch.loop import run_testgen

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    for var in ("CODOCS_GO", "CODOCS_SOURCE_LANG", "CODOCS_PY", "CODOCS_CC", "CODOCS_MVN"):
        monkeypatch.setenv(var, "")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def _flat(text: str) -> str:
    return " ".join(text.split())


# -- java without mvn (a toolchain failure is not cached as finished) -----------------------


def _jdk() -> bool:
    from codocs.lang.javarun import select_jdk

    return select_jdk("", shutil.which("mvn"))[0] is not None


@pytest.mark.skipif(not _jdk(), reason="no usable JDK")
def test_java_maven_module_needs_mvn(tmp_path: Path):
    repo = copy_fixture("javamod", tmp_path)
    s = _settings(mvn="/nonexistent/mvn", source_lang="java")
    with pytest.raises(ValueError, match="mvn not found"):
        pl.make_runners(repo, s, tmp_path / "out")
    from codocs.lang.javarun import JavaRunner

    run = JavaRunner(repo, settings=SimpleNamespace(mvn="/nonexistent/mvn", test_timeout=60,
                                                    go_timeout=300), out_dir=tmp_path / "out")
    assert "mvn not found" in run.unavailable_reason() and not run.available()
    assert run.toolchain_failure(["mvn not found (set --mvn or CODOCS_MVN)"])
    assert not run.toolchain_failure(["A.java:3: error: cannot find symbol"])
    assert "JUnit Platform per module" in run.version()


def _toolchain_broken(runner) -> None:
    """the runner's builds now fail the way a vanished toolchain fails."""
    async def test(*_a, **_kw):
        return LangTestResult(build_ok=False, build_errors=["TOOLCHAIN gone"])

    runner.test = test
    runner.toolchain_failure = lambda errors: any(e.startswith("TOOLCHAIN") for e in errors)


def test_toolchain_failures_are_not_finished_results(tmp_path: Path):
    repo = copy_fixture("pymod", tmp_path)
    out = tmp_path / "out"
    s = settings(test_rounds=3, doc_rounds=3)
    runner, part = asyncio.run(prepare(repo, out, s, "python"))
    target = next(t for t in part.targets if t.file == "src/pymod/calc.py")
    real_test = runner.test
    _toolchain_broken(runner)
    llm = ScriptedLLM(repo, PY_SUITES)
    [suite] = asyncio.run(run_testgen([target], part.pkgs, runner, llm, s, out, repo))
    assert suite.status == "no-toolchain" and "TOOLCHAIN" in suite.error
    assert llm.calls == 1                         # no further rounds
    [doc] = asyncio.run(run_docgen([target], part.pkgs, runner, llm, s, out, repo))
    assert doc.status == "no-toolchain" and llm.calls == 1      # no LLM call at all
    assert load_result(out, target.pkg, target.stem).status == "no-toolchain"
    assert not any((out / "baseline").rglob("*.json"))          # nothing cached

    runner.test = real_test                       # the toolchain is back: both are redone
    del runner.toolchain_failure
    llm2 = ScriptedLLM(repo, PY_SUITES)
    [suite] = asyncio.run(run_testgen([target], part.pkgs, runner, llm2, s, out, repo))
    [doc] = asyncio.run(run_docgen([target], part.pkgs, runner, llm2, s, out, repo))
    assert suite.status == "ok" and doc.status == "ok" and doc.best_phi == 1.0
    assert llm2.calls > 0


# -- grouped cgo import ------------------------------------------------------------------


GROUPED_CGO = {
    "go.mod": "module example.com/grp\n\ngo 1.21\n",
    "nas/sec.go": ('package nas\n\nimport (\n\t/*\n\t   #cgo CFLAGS: -I./head\n'
                   '\t   #include <stdlib.h>\n\t   #include "sec.h"\n\t*/\n\t"C"\n\t"fmt"\n)\n\n'
                   "// Secoff removes the protection.\nfunc Secoff(n int) int {\n"
                   "\tr := int(C.sec_off(C.int(n)))\n\tfmt.Println(r)\n\treturn r\n}\n"),
    "nas/head/sec.h": "int sec_off(int n);\n",
    "nas/lib/src/sec.c": '#include "sec.h"\nint sec_off(int n) { return n + 1; }\n',
}


def test_grouped_cgo_import_is_linked(tmp_path: Path):
    from codocs.bizlogic.xlang import cgo_preamble, has_cgo
    from codocs.lang.wikiindex import MultiLangIndex

    repo = _write(tmp_path / "grp", GROUPED_CGO)
    src = GROUPED_CGO["nas/sec.go"]
    pre = cgo_preamble(src)
    assert has_cgo(src) and '#include "sec.h"' in pre and "fmt" not in pre
    assert pre.count("\n") == src[:len(pre)].count("\n")          # lines kept
    fg = build_graph(repo, languages="all")
    assert fg.stats["cross_edges"]["cgo_calls"] == 1
    assert fg.graph.has_edge("nas::Secoff", "nas/lib/src::sec_off")
    ix = MultiLangIndex.build(repo, languages=["go", "c"])
    assert ix.graph.has_edge("nas", "nas/head")
    # a `"C"` spec without a comment in a block is cgo without a preamble
    assert cgo_preamble('package x\n\nimport (\n\t"C"\n\t"fmt"\n)\n') == ""


# -- auto with --subtree in a Go module repo ----------------------------------------------


def test_auto_subtree_keeps_a_go_repo_go_only(tmp_path: Path):
    repo = _write(tmp_path / "r", {"go.mod": "module example.com/r\n\ngo 1.21\n",
                                   "pkg/a.go": "package pkg\n\nfunc A() {}\n",
                                   "csub/add.c": "int add(int a, int b) { return a + b; }\n"})
    det = detect_languages(repo, "csub", "auto", out_dir=None)
    assert det.languages == ["go"] and det.legacy
    assert any("also found: c 1" in n for n in det.notes)
    assert not any("no Go packages" in n for n in det.notes)
    assert detect_languages(repo, "csub", "c", out_dir=None).languages == ["c"]
    # a go.mod with no Go source at all is not a Go module repo
    (repo / "pkg" / "a.go").unlink()
    assert detect_languages(repo, "csub", "auto", out_dir=None).languages == ["c"]


# -- rich markup in messages ----------------------------------------------------------------


def _mixed(tmp_path: Path) -> Path:
    return _write(tmp_path / "mx", {
        "go.mod": "module example.com/mx\n\ngo 1.21\n",
        "svc/svc.go": "package svc\n\nfunc Run() int { return 1 }\n",
        "clib/add.c": "int add(int a, int b) { return a + b; }\n",
        "tools/pkg/__init__.py": "",
        "tools/pkg/core.py": "def f():\n    return 1\n",
    })


def test_bracketed_text_survives_rich(tmp_path: Path, monkeypatch):
    repo = _mixed(tmp_path)
    res = _invoke("analyze", str(repo), "--source-lang", "all", "--cc", "/nonexistent/cc")
    assert "[no toolchain: cannot run the C compiler" in _flat(res.output)
    monkeypatch.setattr(pl, "_has_multilang_deps", lambda: False)
    res = _invoke("analyze", str(repo), "--source-lang", "python")
    assert res.exit_code == 1
    assert "pip install tree-sitter tree-sitter-language-pack" in _flat(res.output)


# -- missing tree-sitter in bizlogic and wiki -----------------------------------------------


def test_bizlogic_and_wiki_without_tree_sitter(tmp_path: Path, monkeypatch, caplog):
    py = _write(tmp_path / "pyl", {"pkg/__init__.py": "", "pkg/core.py": "def f():\n    return 1\n",
                                   "pkg/app.py": "from .core import f\n\n\ndef g():\n"
                                                 "    return f()\n"})
    monkeypatch.setattr(ts, "available", lambda: False)
    for cmd in ("bizlogic", "wiki"):
        res = _invoke(cmd, str(py), "-o", str(tmp_path / f"out_{cmd}"), "--plan-only")
        assert res.exit_code == 1, res.output
        flat = _flat(res.output)
        assert "pip install tree-sitter tree-sitter-language-pack" in flat
        assert "Traceback" not in flat and "no packages found" not in flat
    # with Go selected too, the other languages are left out with a WARNING
    fg = build_graph(_mixed(tmp_path), languages="all")
    assert {n.lang for n in fg.nodes.values()} == {"go"}
    assert "left out: c, python" in caplog.text


# -- build output is not source -------------------------------------------------------------


def test_build_output_dirs_are_not_scanned(tmp_path: Path):
    repo = _write(tmp_path / "r", {
        "pyproject.toml": "[project]\nname = 'inv'\n",
        "src/inv/__init__.py": "", "src/inv/core.py": "def f():\n    return 1\n",
        "build/lib/inv/__init__.py": "", "build/lib/inv/core.py": "def f():\n    return 1\n",
        "dist/inv-0.1/inv/core.py": "x = 1\n",
        "inv.egg-info/top.py": "x = 1\n",
        "CMakeLists.txt": "project(x C)\n",
        "native/m.c": "int m(void) { return 0; }\n",
        "build/CMakeFiles/3.28.1/CompilerIdC/CMakeCCompilerId.c": "int main(void) { return 0; }\n",
        "build/CMakeFiles/3.28.1/CompilerIdCXX/CMakeCXXCompilerId.cpp": "int main() {}\n",
        "out-debug/CMakeCache.txt": "", "out-debug/gen/x.c": "int x;\n",
        # kept: a regular package named build, and a `build` dir with no build file beside it
        "src/tools/build/__init__.py": "", "src/tools/build/make.py": "def mk():\n    pass\n",
        "go/build/b.go": "package build\n",
    })
    files, _ = scan_files(repo)
    flat = [r for rels in files.values() for r in rels]
    assert not [r for r in flat if r.startswith(("build/", "dist/", "inv.egg-info/",
                                                 "out-debug/"))]
    assert "src/tools/build/make.py" in flat and "go/build/b.go" in flat
    assert "cpp" not in files and files["c"] == ["native/m.c"]
    det = detect_languages(repo, None, "auto", out_dir=None)
    assert det.languages == ["c", "python"]
    ctx = RepoContext.build(repo, ["python"])
    assert not [r for r in ctx.files["python"] if r.startswith("build/")]


# -- OUT inside the repo (multi-mode Go part) -----------------------------------------------


def test_multi_graph_skips_go_copies_under_out(tmp_path: Path):
    repo = tmp_path / "mx"
    shutil.copytree(FIXTURES / "mixedmod", repo)
    clean = build_graph(repo, languages="all", out_dir=repo / "out")
    ring = (repo / "ring" / "ring.go").read_text(encoding="utf-8")
    _write(repo, {"out/docgen/ring/ring/round_1/code.go": ring,
                  "out/work/sandbox/mx/ring/ring.go": ring})
    fg = build_graph(repo, languages="all", out_dir=repo / "out")
    assert not [n for n in fg.nodes if n.startswith("out/")]
    assert fg.stats["cross_edges"] == clean.stats["cross_edges"]
    assert fg.stats["languages"]["go"] == clean.stats["languages"]["go"]
    # Go-only mode does not know OUT: its files stay
    go_only = build_graph(repo, languages=["go"])
    assert [n for n in go_only.nodes if n.startswith("out/")]


# -- C++ `Cls::m` doc headers ---------------------------------------------------------------


CPP_REPO = {
    "geo/shape.cpp": ("namespace geo {\nclass Rect {\npublic:\n"
                      "    Rect(double w, double h) : w_(w), h_(h) {}\n"
                      "    double area() const;\n    void scale(double fx, double fy);\n"
                      "private:\n    double w_, h_;\n};\n"
                      "double Rect::area() const { return w_ * h_; }\n"
                      "void Rect::scale(double fx, double fy) { w_ *= fx; h_ *= fy; }\n"
                      "double total_area(const Rect *r, int n) {\n    double t = 0;\n"
                      "    for (int i = 0; i < n; i++) t += r[i].area();\n    return t;\n}\n"
                      "}\n"),
}
CPP_DOC = ("# geo/shape.cpp\n\n## ClassDef Rect\nA rectangle with width and height.\n\n"
           "## MethodDef Rect::Rect\nConstructs a rectangle from width and height.\n\n"
           "## MethodDef Rect::area\nComputes the area.\n\n"
           "## MethodDef Rect::scale(double fx, double fy)\nScales the rectangle.\n\n"
           "## FunctionDef total_area\nSums the areas.\n")


def test_cpp_colon_headers_reach_graph_nodes(tmp_path: Path):
    from codocs.specforge.docfmt import missing_sections

    repo = _write(tmp_path / "cpl", CPP_REPO)
    out = _write(tmp_path / "out", {"docs/geo/markdown_docs/shape.cpp.md": CPP_DOC})
    assert missing_sections(CPP_DOC, CPP_REPO["geo/shape.cpp"], "cpp") == []
    docs = DocIndex.build([out / "docs"])
    assert ("geo", "Rect.area") in docs.sections and ("geo", "Rect::area") not in docs.sections
    fg = build_graph(repo, docs=docs, languages="cpp")
    summary = {n.name: n.summary for n in fg.nodes.values()}
    assert summary["Rect.area"].startswith("Computes the area")
    assert summary["Rect.scale"].startswith("Scales the rectangle")
    assert summary["Rect.Rect"].startswith("Constructs a rectangle")     # not the class doc
    # an undocumented constructor does not take the class doc
    only_cls = _write(tmp_path / "out2", {"docs/geo/markdown_docs/shape.cpp.md":
                                          "## ClassDef Rect\nA rectangle.\n"})
    assert DocIndex.build([only_cls / "docs"]).get("geo", "Rect.Rect") is None
    # Go: a method still falls back to a bare section of its name
    go = DocIndex()
    go.sections[("p", "Add")] = SimpleNamespace(kind="FunctionDef")
    assert go.get("p", "Stats.Add") is go.sections[("p", "Add")]


def test_wiki_digest_merges_colon_names(tmp_path: Path):
    from codocs import wiki
    from codocs.repowiki.deepdoc.models import PackageDigest

    leaf = PackageDigest(package="geo", source="leafdocs")
    static = PackageDigest(package="geo", source="static")
    for d, name in ((leaf, "Rect::area"), (static, "Rect.area"), (static, "total_area")):
        d.entries.append(SimpleNamespace(name=name))
    index = SimpleNamespace(packages={"geo": object()})

    class Leaf:
        @staticmethod
        def build(*_a, **_kw):
            return SimpleNamespace(digests={"geo": leaf})

    import codocs.repowiki.analysis.leafdocs as leafdocs

    mp = pytest.MonkeyPatch()
    mp.setattr(leafdocs, "LeafDocIndex", Leaf)
    mp.setattr(leafdocs, "digest_from_static", lambda pkg: static)
    try:
        (tmp_path / "out" / "docs").mkdir(parents=True)
        digests, _ = wiki.build_digests(index, tmp_path, tmp_path / "out", use_tree_docs=False)
    finally:
        mp.undo()
    assert [e.name for e in digests["geo"].entries] == ["Rect::area", "total_area"]


# -- the bound adapter in docgen, optional adapter hooks -------------------------------------


def test_docgen_sections_use_the_bound_adapter(tmp_path: Path):
    repo = _write(tmp_path / "r", {
        "include/geo/vec.hpp": ("#pragma once\nnamespace geo {\nclass Vec {\npublic:\n"
                                "    int dot(const Vec &o) const;\nprivate:\n    int x_;\n};\n}\n"),
        "src/vec.cpp": ('#include "geo/vec.hpp"\n\n'
                        "int geo::Vec::dot(const geo::Vec &o) const { return x_ * o.x_; }\n"),
    })
    src = (repo / "src/vec.cpp").read_text()
    cpp = adapter("cpp", RepoContext.build(repo, ["cpp"]))
    _, user = cpp.doc_seed_messages("src/vec.cpp", cpp.unit("src"), src, "")
    assert "`## MethodDef Vec.dot`" in user
    # nothing of this repo leaks into the unbound adapter (other repos, other tests)
    assert adapter("cpp").repo_namespaces() == frozenset()


def test_optional_hooks_have_defaults():
    for lang in ("java", "c", "cpp"):
        ad = adapter(lang)
        res = LangTestResult(build_ok=False, build_errors=["x"])
        assert ad.runtime_import_errors(res, "a") == []
        assert ad.unlisted_failures(res, "", "a") == []
        assert ad.broken_mutant("", res) == "invalid"
    py = adapter("python")
    assert py.broken_mutant("def f(:\n", LangTestResult(build_ok=False)) == "invalid"
