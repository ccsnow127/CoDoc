"""bizlogic graph over Python, Java, C, C++ and mixed (cgo) repositories."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codocs.bizlogic.docs import DocIndex
from codocs.bizlogic.graph import build_graph
from codocs.bizlogic.run import BizParams, render_from_json, run_bizlogic
from codocs.config import Settings

pytest.importorskip("tree_sitter_language_pack")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MIXED = FIXTURES / "mixedmod"
GO_STATS_KEYS = ["files", "functions", "methods", "edges", "call_edges", "ref_edges",
                 "table_edges", "handler_tables", "function_aliases", "utilities",
                 "utility_fanin", "infra_packages", "documented", "excluded_test_functions",
                 "test_only_functions", "vendored_dirs"]


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _edges(fg) -> dict[tuple[str, str], tuple[str, float]]:
    return {(u, v): (d["kind"], round(d["weight"], 3)) for u, v, d in fg.graph.edges(data=True)}


# -- python ------------------------------------------------------------------------------------


def test_python_fixture_nodes_and_test_only():
    fg = build_graph(FIXTURES / "pymod", utility_fanin=40)
    nodes = set(fg.nodes)
    assert {"src/pymod::shapes.classify", "src/pymod::shapes.largest",
            "src/pymod::shapes.Rect.area", "src/pymod::shapes.Rect.name"} <= nodes
    # functions only the tests call are test support (in-degree 0): removed as for Go
    assert "src/pymod::calc.clamp" not in nodes and "src/pymod::stats.Stats.add" not in nodes
    assert fg.stats["test_only_functions"] >= 4
    e = _edges(fg)
    assert e[("src/pymod::shapes.classify", "src/pymod::shapes.Rect.area")] == ("call", 1.0)
    assert ("src/pymod::shapes.largest", "src/pymod::shapes.Rect.area") in e
    assert fg.stats["languages"]["python"]["files"] == 4
    assert all(n.lang == "python" for n in fg.nodes.values())
    assert fg.module == "pymod"


PY_APP = {
    "pyproject.toml": '[project]\nname = "app"\n',
    "src/app/__init__.py": '"""the app."""\nfrom app.core import run\n',
    "src/app/core.py": '''\
from app import util
from app.util import helper as h
from app.models import Base, Child


def log_event(msg):
    return msg


def run(x):
    c = Child(x)
    c.work()
    util.shared(x)
    log_event("run")
    return h(x)


def table_user(k):
    return HANDLERS[k](k)


def on_a(k):
    return k


def on_b(k):
    return k


HANDLERS = {"a": on_a, "b": on_b}
''',
    "src/app/util.py": '''\
def shared(x):
    return helper(x) + 1


def helper(x):
    return x * 2


def only_tests(x):
    return x
''',
    "src/app/models.py": '''\
class Base:
    def __init__(self, v):
        self.v = v

    def step(self):
        return self.v

    def __repr__(self):
        return "Base"


class Child(Base):
    def __init__(self, v):
        super().__init__(v)

    def work(self):
        return self.step() + 1
''',
    "src/app/cli.py": '''\
from app import core


def main():
    return core.run(1)


if __name__ == "__main__":
    main()
''',
    "tests/test_util.py": "from app.util import only_tests\n\n\ndef test_x():\n"
                          "    assert only_tests(1) == 1\n",
    "tests/conftest.py": "from app.util import helper\n\n\ndef fx():\n    return helper(1)\n",
}


def test_python_resolution(tmp_path: Path):
    fg = build_graph(_write(tmp_path, PY_APP), utility_fanin=40)
    e = _edges(fg)
    run = "src/app::core.run"
    assert e[(run, "src/app::models.Child.__init__")] == ("call", 1.0)     # Cls(...)
    assert (run, "src/app::models.Child.work") in e                         # typed local
    assert (run, "src/app::util.shared") in e                               # module alias
    assert (run, "src/app::util.helper") in e                               # `from x import f as h`
    assert (run, "src/app::core.log_event") in e
    assert ("src/app::util.shared", "src/app::util.helper") in e            # same module
    assert ("src/app::models.Child.work", "src/app::models.Base.step") in e  # self -> MRO
    assert ("src/app::models.Child.__init__", "src/app::models.Base.__init__") in e   # super()
    assert ("src/app::cli.main", run) in e
    # test-only: referenced only by tests, nothing in production uses it
    assert "src/app::util.only_tests" not in fg.nodes
    # dispatch table entries are table-held, never test-only, and stay nodes
    assert "src/app::core.on_a" in fg.nodes and "src/app::core.on_b" in fg.nodes
    assert fg.stats["handler_tables"] == 1
    # python plumbing: loggers and dunders are utilities
    assert "src/app::core.log_event" in fg.utilities
    assert "src/app::models.Base.__repr__" in fg.utilities
    assert "src/app::models.Base.__init__" not in fg.utilities
    # conftest.py is test support: no node of its own
    assert not any(n.file.endswith("conftest.py") for n in fg.nodes.values())


def test_python_docs_attach(tmp_path: Path):
    root = _write(tmp_path / "repo", PY_APP)
    docs_root = tmp_path / "out" / "docs"
    _write(docs_root, {"src/app/markdown_docs/util.py.md":
                       "# src/app/util.py\n\n## FunctionDef shared\n"
                       "**shared**: adds one to the doubled value.\n\n"
                       "## FunctionDef helper\n**helper**: doubles x.\n"})
    fg = build_graph(root, docs=DocIndex.build([docs_root]), utility_fanin=40)
    assert fg.nodes["src/app::util.shared"].summary.startswith("adds one")
    assert fg.stats["documented"] == 2


# -- java --------------------------------------------------------------------------------------


def test_java_fixture():
    fg = build_graph(FIXTURES / "javamod", utility_fanin=40)
    e = _edges(fg)
    base = "src/main/java/com/example/shapes::"
    assert e[(base + "Shapes.classify", base + "Rect.area")] == ("call", 1.0)
    assert (base + "Shapes.largest", base + "Rect.area") in e
    # Calc/Stats are only called from the tests: test support, removed
    assert not any(n.startswith("src/main/java/com/example/calc::Calc.clamp") for n in fg.nodes)
    assert all(n.lang == "java" for n in fg.nodes.values())
    assert not any("/src/test/" in "/" + n.file for n in fg.nodes.values())


JAVA_APP = {
    "pom.xml": "<project><modelVersion>4.0.0</modelVersion><groupId>x</groupId>"
               "<artifactId>app</artifactId><version>1</version></project>\n",
    "src/main/java/com/x/api/Store.java": """\
package com.x.api;

public interface Store {
    void put(String k);
}
""",
    "src/main/java/com/x/api/MemStore.java": """\
package com.x.api;

public class MemStore implements Store {
    public void put(String k) { record(k); }
    private void record(String k) { }
}
""",
    "src/main/java/com/x/api/DiskStore.java": """\
package com.x.api;

public class DiskStore implements Store {
    public void put(String k) { }
}
""",
    "src/main/java/com/x/svc/Service.java": """\
package com.x.svc;

import com.x.api.Store;
import com.x.api.MemStore;
import static com.x.svc.Util.trim;

public class Service extends BaseService {
    private final Store store;

    public Service() {
        super();
        this.store = new MemStore();
    }

    public void handle(String k) {
        store.put(trim(k));
        Util.check(k);
        this.audit(k);
        prepare();
        java.util.function.Consumer<String> f = Util::check;
    }

    public String getName() { return "svc"; }
}
""",
    "src/main/java/com/x/svc/BaseService.java": """\
package com.x.svc;

public class BaseService {
    public BaseService() { }
    protected void prepare() { }
    protected void audit(String k) { }
}
""",
    "src/main/java/com/x/svc/Util.java": """\
package com.x.svc;

public final class Util {
    public static String trim(String s) { return s.trim(); }
    public static void check(String s) { }
}
""",
}


def test_java_resolution(tmp_path: Path):
    fg = build_graph(_write(tmp_path, JAVA_APP), utility_fanin=40)
    e = _edges(fg)
    svc, api = "src/main/java/com/x/svc::", "src/main/java/com/x/api::"
    h = svc + "Service.handle"
    # interface receiver (a field): both implementors, split weight
    assert e[(h, api + "MemStore.put")] == ("call", 0.5)
    assert e[(h, api + "DiskStore.put")] == ("call", 0.5)
    assert e[(h, svc + "Util.trim")] == ("call", 1.0)            # static import
    assert (h, svc + "Util.check") in e                           # Cls.m() and Cls::m
    assert (h, svc + "BaseService.audit") in e                    # this.m() -> superclass
    assert (h, svc + "BaseService.prepare") in e                  # bare m() -> superclass
    ctor = svc + "Service.Service"
    assert (ctor, api + "MemStore.MemStore") not in e             # no declared constructor
    assert (ctor, svc + "BaseService.BaseService") in e           # super(...)
    assert (api + "MemStore.put", api + "MemStore.record") in e
    # java accessors are folded like Go's Get/Set
    assert svc + "Service.getName" in fg.utilities


# -- c / c++ -----------------------------------------------------------------------------------


def test_c_fixture():
    fg = build_graph(FIXTURES / "cmod", utility_fanin=40)
    e = _edges(fg)
    assert ("shapes::classify", "shapes::rect_area") in e
    # the static helper of a test-only function stays (no test names it)
    assert "calc::is_space@calc.c" in fg.nodes and "calc::normalize" not in fg.nodes
    # clamp/sum/stats_* are only used by the test programs: test-only
    assert "calc::clamp" not in fg.nodes and "calc::stats_add" not in fg.nodes
    assert not any(n.file.startswith("calc/test_") for n in fg.nodes.values())
    assert {n.lang for n in fg.nodes.values()} == {"c"}
    assert fg.stats["languages"]["c"]["files"] >= 5


C_APP = {
    "core/core.c": """\
#include "../lib/lib.h"

static int twice(int x) { return 2 * x; }

int core_run(int x) {
    return lib_add(twice(x), 1) + pick(x);
}

static int on_a(int x) { return x; }
static int on_b(int x) { return -x; }

static int (*handlers[])(int) = { on_a, on_b };

int dispatch(int k) {
    return run_table(handlers, k);
}

int pick(int x) { return x; }
""",
    "lib/lib.h": "int lib_add(int a, int b);\n",
    "lib/lib.c": '#include "lib.h"\n\nint lib_add(int a, int b) { return a + b; }\n',
    "a/x/dup.c": "int dupe(int v) { return v; }\n",
    "a/y/dup.c": "int dupe(int v) { return v + 1; }\n",
    "a/user.c": "int dupe(int v);\nint use_dupe(int v) { return dupe(v); }\n",
    "gen/asn.c": "/* Generated by asn1c-0.9.29 */\nint asn_decode(int x) { return x; }\n",
    "core/test_core.c": '#include <stdio.h>\nint pick(int x);\nint ut_only(int x);\n'
                        'int main(void) { return pick(1) + ut_only(0) == 1 ? 0 : 1; }\n',
    "app/main.c": "int core_run(int x);\nint dispatch(int k);\nint use_dupe(int v);\n"
                  "int main(void) { return core_run(1) + dispatch(0) + use_dupe(0); }\n",
    "ut/helper.c": "int ut_only(int x) { return x; }\n",
}


def test_c_resolution(tmp_path: Path):
    fg = build_graph(_write(tmp_path, C_APP), utility_fanin=40)
    e = _edges(fg)
    assert e[("core::core_run", "core::twice@core.c")] == ("call", 1.0)
    assert e[("core::core_run", "lib::lib_add")] == ("call", 1.0)      # repo symbol index
    assert ("core::core_run", "core::pick") in e                         # same unit
    # a function pointer table: users reach every entry, split
    assert e[("core::dispatch", "core::on_a@core.c")] == ("table", 0.5)
    assert e[("core::dispatch", "core::on_b@core.c")] == ("table", 0.5)
    # two definitions at the same path distance: split weight
    assert e[("a::use_dupe", "a/x::dupe")] == ("call", 0.5)
    assert e[("a::use_dupe", "a/y::dupe")] == ("call", 0.5)
    # generated and ut/ code is never a node; a test program is not either
    assert not any(n.file.startswith(("gen/", "ut/", "core/test_")) for n in fg.nodes.values())
    assert e[("app::main", "core::core_run")] == ("call", 1.0)
    # pick is named by the test program but also called from core_run: not test-only
    assert "core::pick" in fg.nodes and fg.stats["test_only_functions"] == 0


def test_cpp_fixture():
    fg = build_graph(FIXTURES / "cppmod", utility_fanin=40)
    e = _edges(fg)
    assert ("geom::Vec2.norm", "geom::Vec2.dot") in e                   # bare call in method
    assert ("geom::Polygon.area", "geom::Vec2.x") in e                  # typed local receiver
    assert ("geom::Polygon.perimeter", "geom::Vec2.Vec2") in e
    assert {n.lang for n in fg.nodes.values()} == {"cpp"}
    assert "geom::Vec2.operator==" in fg.utilities or "geom::Vec2.operator==" not in fg.nodes


# -- mixed: one graph, cgo edges ---------------------------------------------------------------


def test_mixed_fixture_cgo_edges():
    fg = build_graph(MIXED, languages="all", utility_fanin=40)
    e = _edges(fg)
    assert e[("ring::New", "clib/src::cring_new")] == ("call", 1.0)       # prototype in include
    assert e[("ring::New", "ring::ring_cap_hint@ring.go")] == ("call", 1.0)   # preamble
    assert ("ring::Ring.Push", "clib/src::cring_push") in e
    assert ("ring::Ring.Len", "clib/src::cring_len") in e
    assert e[("clib/src::cring_push", "ring::ring_on_full")] == ("call", 1.0)   # //export
    assert fg.graph["ring::New"]["clib/src::cring_new"]["xlang"] == "cgo"
    assert "xlang" not in fg.graph["ring::Collect"]["ring::New"]
    pre = fg.nodes["ring::ring_cap_hint@ring.go"]
    assert (pre.lang, pre.file, pre.line) == ("c", "ring/ring.go", 9)
    cross = fg.stats["cross_edges"]
    assert cross["cgo_calls"] == 5 and cross["cgo_exports"] == 1 and cross["cgo_refs"] == 0
    assert set(fg.stats["languages"]) == {"go", "c", "python"}
    assert fg.stats["languages"]["c"]["edges"] == 2
    # the generated asn directory (marker files plus the directory rule) is left out
    assert not any(n.file.startswith("gen/") for n in fg.nodes.values())
    assert {"tools::report.main", "tools::report.summarize"} <= set(fg.nodes)
    assert fg.stats["id_renames"] == 0
    assert fg.module == "example.com/mixed"


def test_mixed_go_only_is_the_old_graph(tmp_path: Path):
    """auto and ["go"] on a Go module repo are the Go-only graph: no new stats keys, and
    its nodes/edges equal those of the same tree without any non-Go file."""
    auto = build_graph(MIXED, utility_fanin=40)
    only = build_graph(MIXED, languages=("go",), utility_fanin=40)
    assert list(auto.stats) == GO_STATS_KEYS and auto.stats == only.stats
    assert set(auto.nodes) == set(only.nodes)
    copy = tmp_path / "m"
    shutil.copytree(MIXED, copy, ignore=shutil.ignore_patterns("*.c", "*.h", "*.py"))
    bare = build_graph(copy, utility_fanin=40)
    assert _edges(bare) == _edges(auto) and list(bare.graph.nodes) == list(auto.graph.nodes)
    assert all(n.lang == "go" for n in auto.nodes.values())
    # multi mode keeps every Go node and Go -> Go edge
    multi = build_graph(MIXED, languages="all", utility_fanin=40)
    go_edges = {k: v for k, v in _edges(multi).items()
                if multi.nodes[k[0]].lang == "go" and multi.nodes[k[1]].lang == "go"}
    assert go_edges == _edges(auto)
    assert {n for n in multi.nodes if multi.nodes[n].lang == "go"} == set(auto.nodes)


CGO_APP = {
    "go.mod": "module example.com/cg\n\ngo 1.21\n",
    "pkg/a/a.go": """\
package a

/*
#cgo CFLAGS: -I../../inc
#include "api.h"
#define MAXV 10
*/
import "C"

func Run() int {
	n := int(C.near_fn(C.int(1)))
	n += int(C.api_call(C.MAXV))
	n += int(C.gen_decode(1))
	n += int(C.many(1))
	p := C.CString("x")
	_ = p
	f := C.ref_target
	_ = f
	return n
}

func Use() int { return Run() + Foo() }

func Foo() int { return 1 }
""",
    "pkg/a/foo.c": "int Foo(void) { return 2; }\n",
    "inc/api.h": "int api_call(int v);\n",
    "lib/api/api.c": "int api_call(int v) { return v; }\n",
    "pkg/b1/near.c": "int near_fn(int v) { return v; }\n",
    "pkg/b2/near.c": "int near_fn(int v) { return v + 1; }\n",
    "lib/gen/codec.c": "/* Generated by asn1c */\nint gen_decode(int v) { return v; }\n",
    "m1/m.c": "int many(int v) { return v; }\n",
    "m2/m.c": "int many(int v) { return v; }\n",
    "m3/m.c": "int many(int v) { return v; }\n",
    "m4/m.c": "int many(int v) { return v; }\n",
    "lib/ref.c": "int ref_target(int v) { return v; }\n",
    "pkg/a/caller.c": "int caller(void) { return Foo(); }\n",
}


def test_cgo_ranks_split_generated_and_renames(tmp_path: Path):
    fg = build_graph(_write(tmp_path, CGO_APP), languages="all", utility_fanin=40)
    e = _edges(fg)
    run = "pkg/a::Run"
    assert e[(run, "lib/api::api_call")] == ("call", 1.0)
    assert e[(run, "pkg/b1::near_fn")] == ("call", 0.5)        # two at one distance: split
    assert e[(run, "pkg/b2::near_fn")] == ("call", 0.5)
    assert e[(run, "lib::ref_target")] == ("ref", 1.0)         # C.f without a call
    assert not any(v.startswith("m") and v.endswith("::many") for (u, v) in e if u == run)
    # Go `Foo` and C `Foo` in one directory: the C node is renamed, Go keeps its id
    assert fg.nodes["pkg/a::Foo"].lang == "go"
    assert fg.nodes["pkg/a::Foo@foo.c"].lang == "c"
    assert fg.stats["id_renames"] == 1
    assert ("pkg/a::caller", "pkg/a::Foo@foo.c") in e
    cross = fg.stats["cross_edges"]
    assert cross["cgo_to_generated"] == 1                    # gen_decode is only in gen code
    assert cross["cgo_unresolved_names"] == 1                # many: four definitions
    assert cross["cgo_calls"] == 3 and cross["cgo_refs"] == 1
    # CString/MAXV/int are never C functions: no edge, not counted as unresolved
    assert not any("CString" in v or "MAXV" in v for (_u, v) in e)


CGO_RANK3 = {
    "go.mod": "module example.com/r3\n\ngo 1.21\n",
    "app/svc/encode.go": """\
package svc

/*
#cgo CFLAGS: -I${SRCDIR}/../../third_party/fastcodec/include
#include <stdlib.h>
#include "fastcodec.h"
*/
import "C"

func Encode(n int) int { return int(C.codec_encode(C.int(n))) }

func Version() int { return int(C.codec_version()) }
""",
    "third_party/fastcodec/include/fastcodec.h": "int codec_encode(int n);\n"
                                                 "int codec_version(void);\n",
    "third_party/fastcodec/src/codec.c": '#include "fastcodec.h"\n'
                                         "int codec_encode(int n) { return n ^ 0x5a; }\n"
                                         "int codec_version(void) { return 3; }\n",
    # an unrelated library defining the same name, nearer the Go package by path
    "app/codec/legacy/codec.c": "int codec_encode(int n) { return n; }\n",
}


def test_cgo_rank3_prefers_the_included_headers_library(tmp_path: Path):
    fg = build_graph(_write(tmp_path, CGO_RANK3), languages="go,c", utility_fanin=40)
    e = _edges(fg)
    assert e[("app/svc::Encode", "third_party/fastcodec/src::codec_encode")] == ("call", 1.0)
    assert ("app/svc::Encode", "app/codec/legacy::codec_encode") not in e
    assert e[("app/svc::Version", "third_party/fastcodec/src::codec_version")] == ("call", 1.0)


CGO_CPP = {
    "go.mod": "module example.com/cpp\n\ngo 1.21\n",
    "engine/engine.h": '#ifdef __cplusplus\nextern "C" {\n#endif\n'
                       "int engine_score(int a, int b);\n#ifdef __cplusplus\n}\n#endif\n",
    "engine/engine.go": 'package engine\n\n// #include "engine.h"\nimport "C"\n\n'
                        "func Score(a, b int) int { return int(C.engine_score(C.int(a), "
                        "C.int(b))) }\n",
    "engine/engine.cpp": '#include "engine.h"\nnamespace {\nclass Scorer {\npublic:\n'
                         "    int score(int a, int b) const { return a * 2 + b; }\n};\n}\n"
                         'extern "C" int engine_score(int a, int b) { Scorer s; '
                         "return s.score(a, b); }\n",
}


def test_cgo_edges_into_cpp_without_any_c_file(tmp_path: Path):
    """cgo compiles a package's .cpp: Go -> extern "C" edges need no .c file in the repo."""
    fg = build_graph(_write(tmp_path, CGO_CPP), languages="all", utility_fanin=40)
    assert set(fg.stats["languages"]) == {"go", "cpp"}
    e = _edges(fg)
    assert e[("engine::Score", "engine::engine_score")] == ("call", 1.0)
    assert fg.graph["engine::Score"]["engine::engine_score"]["xlang"] == "cgo"
    assert fg.stats["cross_edges"]["cgo_calls"] == 1
    fg = build_graph(tmp_path, languages="go,cpp", utility_fanin=40)
    assert ("engine::Score", "engine::engine_score") in _edges(fg)


def test_cgo_edges_reach_a_handwritten_bridge_next_to_generated_code(tmp_path: Path):
    gen = "/*\n * Generated by asn1c-1.0.18 (http://lionet.info/asn1c)\n */\n"
    files = {
        "go.mod": "module example.com/codec\n\ngo 1.21\n",
        "codec/codec.go": 'package codec\n\n/*\n#cgo CFLAGS: -I${SRCDIR}/asn/src\n'
                          '#include "codec_bridge.h"\n*/\nimport "C"\n\n'
                          "func FrameEncode(v int) int {\n"
                          "\treturn int(C.codec_bridge_msgenc(C.int(v)))\n}\n",
        "codec/asn/src/codec_bridge.h": "int codec_bridge_msgenc(int v);\n",
        "codec/asn/src/codec_bridge.c": "/**\n * @file codec_bridge.c\n * @brief encode I/F\n */\n"
                                        '#include "codec_bridge.h"\n'
                                        "int codec_bridge_msgenc(int v) { return v; }\n",
    }
    for i in range(4):
        files[f"codec/asn/src/gen{i}.c"] = gen + f"int gen{i}(void) {{ return 0; }}\n"
    fg = build_graph(_write(tmp_path, files), languages="go,c", utility_fanin=40)
    e = _edges(fg)
    assert e[("codec::FrameEncode", "codec/asn/src::codec_bridge_msgenc")] == ("call", 1.0)
    assert fg.stats["cross_edges"]["cgo_to_generated"] == 0
    assert not any(n.endswith("::gen0") for n in fg.nodes)


def test_bizlogic_lang_tags_in_prompts():
    from codocs.bizlogic.infer import cluster_prompt, describe_clusters

    fg = build_graph(MIXED, languages="all", utility_fanin=40)
    assign = {n: 0 for n in fg.nodes}
    infos = describe_clusters(fg, assign)
    _sys, user = cluster_prompt(fg, infos[0], "en")
    lines = [ln for ln in user.splitlines() if ln.startswith("- ")]
    assert any("[C]" in ln for ln in lines) and any("[Python]" in ln for ln in lines)
    assert all("[" not in ln.split(":")[0] for ln in lines if ln.startswith("- ring.")
               and "@" not in ln)


# -- run_bizlogic ------------------------------------------------------------------------------


async def test_run_bizlogic_plan_only_multi_and_render(tmp_path: Path):
    out = tmp_path / "out"
    settings = Settings()
    params = BizParams(clusters=(2,), utility_fanin=40, min_reach=1)
    res = await run_bizlogic(MIXED, out, settings, params, plan_only=True, languages="all")
    graph = json.loads((out / "bizlogic" / "graph.json").read_text())
    assert graph["stats"]["cross_edges"]["cgo_calls"] == 5
    assert set(graph["stats"]["languages"]) == {"go", "c", "python"}
    assert res["graph"]["languages"]["python"]["functions"] == 4
    # a re-render without nodes.json rebuilds the same cross-language graph
    kdir = next((out / "bizlogic").glob("k*"))
    stored = json.loads((kdir / "nodes.json").read_text())
    (kdir / "nodes.json").unlink()
    render_from_json(kdir, int(kdir.name[1:]), repo=MIXED)
    assert json.loads((kdir / "nodes.json").read_text()) == stored

    # settings.source_lang reaches the graph when no languages are passed
    out2 = tmp_path / "out2"
    s2 = Settings()
    s2.source_lang = "go"
    await run_bizlogic(MIXED, out2, s2, params, plan_only=True)
    g2 = json.loads((out2 / "bizlogic" / "graph.json").read_text())
    assert list(g2["stats"]) == GO_STATS_KEYS


async def test_run_bizlogic_default_on_go_repo_is_legacy(tmp_path: Path):
    out = tmp_path / "out"
    await run_bizlogic(MIXED, out, Settings(), BizParams(clusters=(2,), min_reach=1),
                       plan_only=True)
    graph = json.loads((out / "bizlogic" / "graph.json").read_text())
    assert list(graph["stats"]) == GO_STATS_KEYS


def test_out_dir_inside_repo_is_ignored(tmp_path: Path):
    root = _write(tmp_path / "repo", PY_APP)
    out = root / "out"
    _write(out, {"docgen/src/app/core.py/round_1/code.py": "def ghost():\n    return 1\n",
                 "tests/_support/codocs_main.c": "int main(void) { return 0; }\n"})
    fg = build_graph(root, utility_fanin=40, out_dir=out)
    assert not any(n.file.startswith("out/") for n in fg.nodes.values())
    assert set(fg.stats["languages"]) == {"python"}


def test_explicit_language_list_and_errors(tmp_path: Path):
    fg = build_graph(MIXED, languages=["python"], utility_fanin=40)
    assert {n.lang for n in fg.nodes.values()} == {"python"}
    with pytest.raises(ValueError):
        build_graph(MIXED, languages=["cobol"])
    with pytest.raises(ValueError):
        build_graph(MIXED, languages="java")       # requested but absent
