"""multi-language wiki: MultiLangIndex, run_wiki modes and the multi-language generator."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codocs.config import Settings
from codocs.wiki import build_digests, multilang_generator, run_wiki, wiki_languages

pytest.importorskip("tree_sitter_language_pack")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MIXED = FIXTURES / "mixedmod"


def _settings(source_lang: str = "auto", **kw) -> Settings:
    s = Settings(**kw)
    s.source_lang = source_lang          # set as an attribute: older Settings lack the field
    return s


def _index(root: Path, languages, subtree=None, out_dir=None):
    from codocs.lang.wikiindex import MultiLangIndex

    return MultiLangIndex.build(root, subtree, languages, None, out_dir=out_dir)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# -- MultiLangIndex on the language fixtures ---------------------------------------------------


def test_index_python():
    idx = _index(FIXTURES / "pymod", ["python"])
    assert list(idx.packages) == ["src/pymod"]
    pkg = idx.packages["src/pymod"]
    assert pkg.name == "pymod" and pkg.test_files == 2 and not pkg.is_main
    assert sorted(pkg.files) == ["src/pymod/__init__.py", "src/pymod/calc.py",
                                 "src/pymod/shapes.py", "src/pymod/stats.py"]
    syms = {(s.name, s.kind, s.receiver) for s in pkg.symbols}
    assert {("clamp", "func", ""), ("Stats", "type", ""), ("area", "method", "Rect")} <= syms
    assert pkg.doc_comment                       # the __init__ docstring
    assert idx.languages["python"]["packages"] == 1 and idx.module == "pymod"
    assert "dataclasses" in pkg.imports_stdlib


def test_index_java():
    idx = _index(FIXTURES / "javamod", ["java"])
    calc, shapes = "src/main/java/com/example/calc", "src/main/java/com/example/shapes"
    assert sorted(idx.packages) == [calc, shapes]          # test-source dirs are no packages
    assert idx.packages[calc].name == "com.example.calc"
    assert idx.packages[calc].test_files == 2 and idx.packages[shapes].test_files == 0
    syms = {(s.name, s.kind): s.exported for s in idx.packages[calc].symbols}
    assert syms[("clamp", "method")] and syms[("Calc", "type")] is True
    assert not any(s.name == "Calc" and s.kind == "method" and s.exported
                   for s in idx.packages[calc].symbols)      # private constructor
    assert "java.util" in idx.packages[calc].imports_stdlib


def test_index_c_and_cpp():
    idx = _index(FIXTURES / "cmod", ["c"])
    assert sorted(idx.packages) == ["calc", "shapes", "sjis"]
    calc = idx.packages["calc"]
    assert "calc/calc.h" in calc.files and "calc/test_calc.c" not in calc.files
    assert calc.test_files == 3
    syms = {s.name: s.exported for s in calc.symbols}
    assert syms["clamp"] and syms["is_space"] is False         # static
    assert "string.h" in calc.imports_stdlib
    cpp = _index(FIXTURES / "cppmod", ["cpp"])
    assert sorted(cpp.packages) == ["geom", "util"]
    assert any(s.name == "Vec2" and s.kind == "type" for s in cpp.packages["geom"].symbols)
    assert cpp.languages["cpp"]["packages"] == 2
    assert "vector" in cpp.packages["util"].imports_stdlib


def test_index_mixed_merges_and_cgo_edges():
    from codocs.repowiki.analysis.golang import GoModuleIndex

    go = GoModuleIndex.build(MIXED)
    idx = _index(MIXED, ["go", "c", "python"])
    assert idx.module == "example.com/mixed"
    assert set(idx.packages) == {"ring", "clib/include", "clib/src", "gen/asn/src", "tools"}
    ring, gring = idx.packages["ring"], go.packages["ring"]
    # the Go record keeps its values, extended with the directory's C file
    assert ring.files[:len(gring.files)] == gring.files and "ring/cring_build.c" in ring.files
    assert ring.name == "ring" and ring.imports_stdlib == sorted(set(gring.imports_stdlib))
    assert idx.package_languages["ring"] == ["go", "c"]
    # cgo: Go package -> the dirs of the headers its preamble includes
    assert idx.graph.has_edge("ring", "clib/include")
    assert idx.graph.has_edge("clib/src", "clib/include")       # quoted include
    assert idx.packages["tools"].is_main                        # __main__ guard
    assert "tools" in idx.entrypoints()
    # generated C stays a package for the wiki (symbols and LOC, statically)
    assert len(idx.packages["gen/asn/src"].files) == 4
    assert idx.language_counts() == {"go": 1, "c": 4, "python": 1}
    assert set(idx.languages) == {"go", "c", "python"}


def test_index_cgo_edges_with_cpp_only(tmp_path: Path):
    """a Go package whose cgo preamble includes a C++ library header (no .c anywhere)."""
    _write(tmp_path, {
        "go.mod": "module example.com/cpp\n\ngo 1.21\n",
        "engine/engine.go": 'package engine\n\n// #cgo CFLAGS: -I${SRCDIR}/../core\n'
                            '// #include "core.h"\nimport "C"\n\n'
                            "func Score() int { return int(C.core_score()) }\n",
        "core/core.h": '#ifdef __cplusplus\nextern "C" {\n#endif\nint core_score(void);\n'
                               "#ifdef __cplusplus\n}\n#endif\n",
        "core/core.cpp": '#include "core.h"\nextern "C" int core_score(void) { return 1; }\n',
    })
    idx = _index(tmp_path, ["go", "cpp"])
    assert {"engine", "core"} <= set(idx.packages)
    assert idx.graph.has_edge("engine", "core")


def test_index_subtree_and_tech_stack(tmp_path: Path):
    _write(tmp_path, {
        "svc/app.py": "import requests\nimport numpy as np\n\n\ndef get(u):\n"
                      "    return requests.get(u)\n",
        "svc/net.c": "#include <openssl/ssl.h>\n#include <pthread.h>\nint f(void) { return 0; }\n",
        "other/x.py": "def x():\n    return 1\n",
    })
    idx = _index(tmp_path, ["c", "python"], subtree="svc")
    assert list(idx.packages) == ["svc"]
    assert idx.package_languages["svc"] == ["c", "python"]
    stack = {name for name, _cat, _n in idx.tech_stack()}
    assert {"requests", "NumPy", "OpenSSL", "pthreads"} <= stack
    assert "requests" in idx.packages["svc"].imports_external
    assert "openssl/ssl.h" in idx.packages["svc"].imports_external


# -- run_wiki ---------------------------------------------------------------------------------


async def test_plan_only_per_fixture(tmp_path: Path):
    for name, lang in (("pymod", "python"), ("javamod", "java"), ("cmod", "c"),
                       ("cppmod", "cpp")):
        res = await run_wiki(FIXTURES / name, tmp_path / name, _settings(), plan_only=True)
        assert res["packages"] >= 1 and set(res["languages"]) == {lang}, name
        assert res["static"] == res["packages"] and res["pages"] >= 1
        assert not (tmp_path / name / "wiki").exists()


async def test_plan_only_mixed_modes(tmp_path: Path):
    legacy = await run_wiki(MIXED, tmp_path / "a", _settings(), plan_only=True)
    assert "languages" not in legacy and legacy["packages"] == 1          # auto: Go only
    go = await run_wiki(MIXED, tmp_path / "b", _settings("go"), plan_only=True)
    assert go == legacy
    multi = await run_wiki(MIXED, tmp_path / "c", _settings("all"), plan_only=True)
    assert multi["packages"] == 5 and set(multi["languages"]) == {"go", "c", "python"}
    titles = {s["title"] for s in multi["plan"]}
    assert {"ring", "clib", "tools"} <= titles
    narrowed = await run_wiki(MIXED, tmp_path / "d", _settings("all"), plan_only=True,
                              packages=["clib"])
    assert narrowed["packages"] == 2
    with pytest.raises(ValueError, match="no packages found under"):
        await run_wiki(MIXED, tmp_path / "e", _settings("all"), plan_only=True,
                       packages=["nothing"])


async def test_gomod_with_non_go_out_inside_stays_legacy(tmp_path: Path):
    mod = tmp_path / "gomod"
    shutil.copytree(FIXTURES / "gomod", mod)
    out = mod / "out"
    _write(out, {"docgen/calc/calc.c/round_1/code.c": "int ghost(void) { return 1; }\n",
                 "docgen/calc/x.py/round_1/code.py": "def ghost():\n    return 1\n",
                 "tests/_support/codocs_main.c": "int main(void) { return 0; }\n"})
    assert wiki_languages(mod, None, _settings(), out) == ["go"]
    res = await run_wiki(mod, out, _settings(), plan_only=True)
    assert "languages" not in res and res["packages"] == 2


async def test_errors(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        await run_wiki(tmp_path / "empty", tmp_path / "o1", _settings(), plan_only=True)
    with pytest.raises(ValueError, match="python"):
        await run_wiki(FIXTURES / "cmod", tmp_path / "o2", _settings("python"), plan_only=True)


def test_digests_merge_go_and_c_docs(tmp_path: Path):
    out = tmp_path / "out"
    _write(out / "docs", {
        "clib/src/markdown_docs/cring.c.md":
            "# clib/src/cring.c\n\n**Unit**: `src` (C)\n\n## FunctionDef cring_push\n"
            "**cring_push**: appends a value, dropping the oldest when full.\n",
        "ring/markdown_docs/ring.md":
            "# ring/ring.go\n\n## FunctionDef New\n**New**: allocates a ring.\n",
        "ring/markdown_docs/cring_build.c.md":
            "# ring/cring_build.c\n\n## FunctionDef cring_build\n**cring_build**: none.\n",
    })
    idx = _index(MIXED, ["go", "c", "python"])
    digests, counts = build_digests(idx, MIXED, out, use_tree_docs=False)
    assert digests["clib/src"].source == "leafdocs"
    names = [e.name for e in digests["clib/src"].entries]
    assert names[0] == "cring_push" and "cring_new" in names        # static entries kept
    ring = [e.name for e in digests["ring"].entries]
    assert "New" in ring and "cring_build" in ring                  # x.md and x.c.md merge
    assert counts["leaf_out"] == 2


# -- the multi-language generator ---------------------------------------------------------------


def test_multilang_generator_facts(tmp_path: Path):
    from codocs.repowiki.deepdoc.generator import DeepDocGenerator

    idx = _index(MIXED, ["go", "c", "python"])
    digests, _ = build_digests(idx, MIXED, tmp_path)
    cls = multilang_generator(DeepDocGenerator)
    assert issubclass(cls, DeepDocGenerator) and cls.__name__ == "MultiLangDeepDocGenerator"
    gen = cls(idx, digests, llm=None, cache=None)
    plan = gen._plan_facts()
    assert plan.startswith("Repository `example.com/mixed`: 5 packages (Go 1, C 4, Python 1).")
    assert "Entry points (Python `__main__`): tools" in plan
    system = gen._system_facts()
    assert "Lines of code: Go 74, C 91, Python 37." in system
    assert "Most depended-upon packages (PageRank):" in system


async def test_full_flow_multi_with_fake_generator(tmp_path: Path, monkeypatch):
    import codocs.repowiki.core.cache as cache_mod
    import codocs.repowiki.deepdoc.generator as gen_mod
    import codocs.repowiki.llm.client as client_mod
    from codocs.repowiki.deepdoc.models import PageDoc

    seen: dict = {}

    class FakeCache:
        def __init__(self, db_path=None):
            pass

        async def init(self):
            pass

        async def close(self):
            pass

    class FakeClient:
        def __init__(self, model, api_key="", api_base=""):
            self.total_input_tokens = 1
            self.total_output_tokens = 2

    class FakeGenerator:
        def __init__(self, index, digests, llm, cache, language="en", concurrency=4,
                     on_progress=None):
            seen["packages"] = sorted(index.packages)
            self.index, self.digests = index, digests

        async def refine_plan(self, skeleton):
            return skeleton

        async def generate(self, plan):
            seen["facts"] = self._plan_facts()
            docs = [PageDoc(id="index", title="Overview", markdown="# Overview\n"),
                    PageDoc(id="architecture", title="Architecture", markdown="# Arch\n")]
            return docs + [PageDoc(id=p.id, title=p.title, markdown=f"# {p.title}\n")
                           for p in plan.all_pages()]

    monkeypatch.setattr(cache_mod, "Cache", FakeCache)
    monkeypatch.setattr(client_mod, "LLMClient", FakeClient)
    monkeypatch.setattr(gen_mod, "DeepDocGenerator", FakeGenerator)
    out = tmp_path / "out"
    res = await run_wiki(MIXED, out, _settings("all", cache_path=str(tmp_path / "c.db")),
                         fmt="markdown")
    assert seen["packages"] == ["clib/include", "clib/src", "gen/asn/src", "ring", "tools"]
    assert seen["facts"].startswith("Repository `example.com/mixed`")
    assert (out / "wiki" / "index.md").is_file() and (out / "wiki" / "package-index.md").is_file()
    assert res["tokens"] == {"input": 1, "output": 2} and res["html"] is None
