"""python adapter (tree-sitter parsing, projects, context, relevance) and the pytest runner."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codocs.lang import adapter as lang_adapter
from codocs.lang import treesitter as ts
from codocs.lang.base import (
    FRAGMENT_SLOTS,
    ImportSpec,
    LangTestResult,
    RepoContext,
    apply,
    file_coverage,
    prompt_spec,
)

pytestmark = pytest.mark.skipif(not ts.available(), reason="tree-sitter not installed")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pymod"


def _toolchain() -> bool:
    try:
        import coverage  # noqa: F401
        import pytest as _pt  # noqa: F401
    except ImportError:
        return False
    return True


needs_py = pytest.mark.skipif(not _toolchain(), reason="pytest/coverage not importable")


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _bound(root: Path):
    from codocs.lang.python import PythonAdapter

    ctx = RepoContext.build(root, ["python"])
    return PythonAdapter(ctx)


def _copy(tmp_path: Path) -> Path:
    dst = tmp_path / "pymod"
    shutil.copytree(FIXTURE, dst)
    return dst


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _runner(repo: Path, tmp_path: Path, timeout: int = 60):
    from codocs.lang.pyrun import PythonRunner

    ad = _bound(repo)
    run = PythonRunner(repo, adapter=ad, work_dir=tmp_path / "out" / "work" / "tmp",
                       timeout=timeout)
    return ad, run


CALC = (FIXTURE / "src/pymod/calc.py").read_text(encoding="utf-8")
STATS = (FIXTURE / "src/pymod/stats.py").read_text(encoding="utf-8")
SHAPES = (FIXTURE / "src/pymod/shapes.py").read_text(encoding="utf-8")
TEST_CALC = (FIXTURE / "tests/test_calc.py").read_text(encoding="utf-8")

SAMPLE = '''#!/usr/bin/env python
"""module doc."""
from __future__ import annotations

import os
import sys as system
from .a import (b as c, d)
from . import x
from ..m import *

X: int = 1
a, b = 1, 2
Y = Z = "s"
obj.attr = 3


# helper doc
@dec
@dec2(1)
async def f(x, y=1, *args, k=2, **kw) -> int:
    def g():
        return x + 1
    return x == y


class A(Base, metaclass=M):
    """doc."""

    z = 3

    @property
    def p(self):
        return self.z

    @p.setter
    def p(self, v):
        self.z = v

    class Inner:
        def m(self):
            return 1


if __name__ == "__main__":
    f(1)
'''


# -- registry, prompts --------------------------------------------------------------------


def test_registry_returns_bound_python_adapter():
    from codocs.lang.python import PythonAdapter

    ctx = RepoContext.build(FIXTURE, ["python"])
    a = lang_adapter("python", ctx)
    assert isinstance(a, PythonAdapter) and a.ctx is ctx
    assert lang_adapter("python", ctx) is a
    assert isinstance(lang_adapter("python"), PythonAdapter)
    assert a.display == "Python" and a.prompts.fence == "python" and a.prompts.comment == "#"


def test_prompt_fragments_define_every_slot():
    spec = prompt_spec("python")
    assert set(FRAGMENT_SLOTS) <= set(spec.fragments)
    assert spec.fragments["MARK"] == "#"
    assert spec.fragments["FENCE"] == "python"
    assert "**Module**" in spec.fragments["DOC_HEADER"]


# -- declarations, imports, identifiers -------------------------------------------------------


def test_decls_kinds_names_and_signatures():
    a = lang_adapter("python")
    got = [(d.kind, d.qualname) for d in a.decls(SAMPLE)]
    assert got == [
        ("var", "X"), ("var", "a"), ("var", "b"), ("var", "Y"), ("var", "Z"), ("func", "f"),
        ("type", "A"), ("method", "A.p"), ("method", "A.p"), ("type", "A.Inner"),
        ("method", "A.Inner.m"),
    ]
    by = {d.qualname: d for d in a.decls(SAMPLE)}
    f = by["f"]
    assert f.signature == ("@dec\n@dec2(1)\n"
                           "async def f(x, y=1, *args, k=2, **kw) -> int:")
    # start includes the leading comment, line is the def line
    assert SAMPLE[f.start:].startswith("# helper doc")
    assert SAMPLE.splitlines()[f.line - 1].startswith("async def f")
    assert by["A"].signature == "class A(Base, metaclass=M):"
    assert by["A.Inner"].kind == "type" and by["A.Inner.m"].receiver == "A.Inner"
    assert by["X"].signature == "X: int = 1"
    assert "g" not in by          # nested functions are not decls
    assert a.decls("") == []


def test_decls_of_fixture():
    a = lang_adapter("python")
    assert [(d.kind, d.qualname) for d in a.decls(CALC)] == [
        ("var", "SCALE"), ("func", "clamp"), ("func", "total"), ("func", "normalize")]
    assert [(d.kind, d.qualname) for d in a.decls(STATS)] == [
        ("type", "Stats"), ("method", "Stats.add"), ("method", "Stats.mean")]
    st = a.decls(STATS)[0]
    assert st.signature == "@dataclass\nclass Stats:"
    assert [(d.kind, d.qualname) for d in a.decls(SHAPES)] == [
        ("type", "Rect"), ("method", "Rect.__init__"), ("method", "Rect.area"),
        ("method", "Rect.name"), ("func", "classify"), ("func", "largest")]


def test_imports_and_bound_names():
    a = lang_adapter("python")
    imps = a.imports(SAMPLE)
    got = [(i.kind, i.module, i.name, i.alias, i.bound) for i in imps]
    assert got == [
        ("from", "__future__", "annotations", None, "annotations"),
        ("import", "os", "", None, "os"),
        ("import", "sys", "", "system", "system"),
        ("from", ".a", "b", "c", "c"),
        ("from", ".a", "d", None, "d"),
        ("from", ".", "x", None, "x"),
        ("from", "..m", "*", None, "*"),
    ]
    assert imps[3].text == "from .a import (b as c, d)"
    assert a.format_import(imps[2]) == "import sys as system"
    assert a.format_import(imps[3]) == "from .a import b as c"
    nested = a.imports("def f():\n    import json\n    s = 'import os'\n")
    assert [i.module for i in nested] == ["json"]


def test_identifiers_skip_strings_comments_and_keywords():
    a = lang_adapter("python")
    ids = a.identifiers('def f(x):\n    # clamp\n    return "total" + x if x else None\n')
    assert {"f", "x"} <= ids
    assert not {"clamp", "total", "def", "return", "if", "else", "None"} & ids


def test_extract_code_prefers_python_fence():
    a = lang_adapter("python")
    reply = "text\n```python\nx = 1\n```\n```java\nclass A {}\n```\n"
    assert a.extract_code(reply) == "x = 1\n"


def test_is_generated():
    a = lang_adapter("python")
    assert a.is_generated("# Code generated by protoc. DO NOT EDIT.\nx = 1\n")
    assert a.is_generated("# -*- coding: utf-8 -*-\n# Generated by the protocol buffer "
                          "compiler.  DO NOT EDIT!\n")
    assert a.is_generated("x = 1\n", "api/svc_pb2.py")
    assert a.is_generated("x = 1\n", "api/svc_pb2_grpc.py")
    assert not a.is_generated(CALC, "src/pymod/calc.py")


def test_preamble_round_trip():
    a = lang_adapter("python")
    src = '#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n"""doc."""\nfrom __future__ import ' \
          'annotations\n\nimport os\n'
    pre = a.preamble(src)
    assert pre == ("#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n"
                   "from __future__ import annotations\n")
    code = '"""doc."""\n\nfrom __future__ import annotations\nimport os\n\nX = 1\n'
    out = a.apply_preamble(code, pre)
    assert out.startswith("#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n\"\"\"doc.\"\"\"\n\n"
                          "from __future__ import annotations\n\nimport os\n")
    assert out.count("from __future__") == 1 and "X = 1" in out
    compile(out, "x.py", "exec")
    assert a.preamble("import os\n") == ""
    assert a.apply_preamble("import os\n", "") == "import os\n"
    # no docstring: futures go first
    out = a.apply_preamble("import os\n", "from __future__ import annotations\n")
    assert out.startswith("from __future__ import annotations\n") and "import os" in out


# -- tests in source ------------------------------------------------------------------------


TESTS_SRC = '''import unittest

import pytest


# a comment for test_a
@pytest.mark.parametrize("v", [1, 2])
def test_a(v):
    assert v


def helper():
    return 1


async def test_async():
    pass


class TestGroup:
    def test_x(self):
        pass

    def test_y(self):
        pass


class TestWithInit:
    def __init__(self):
        pass

    def test_never(self):
        pass


class Checks(unittest.TestCase):
    def test_u(self):
        pass

    def helper(self):
        pass


class TestMixed:
    def _setup(self):
        return 1

    def test_m(self):
        pass
'''


def test_test_names_and_lines():
    a = lang_adapter("python")
    names = a.test_names(TESTS_SRC, "tests/test_x.py")
    assert names == [
        "tests/test_x.py::test_a", "tests/test_x.py::test_async",
        "tests/test_x.py::TestGroup::test_x", "tests/test_x.py::TestGroup::test_y",
        "tests/test_x.py::Checks::test_u", "tests/test_x.py::TestMixed::test_m",
    ]
    lines = a.test_lines(TESTS_SRC, "tests/test_x.py")
    assert TESTS_SRC.splitlines()[lines["tests/test_x.py::test_a"] - 1].startswith("def test_a")
    assert TESTS_SRC.splitlines()[lines["tests/test_x.py::Checks::test_u"] - 1].strip() \
        .startswith("def test_u")


def test_test_names_are_project_relative(tmp_path):
    _write(tmp_path, "proj/pyproject.toml",
           '[tool.pytest.ini_options]\npython_functions = ["check_*"]\n')
    _write(tmp_path, "proj/pkg/__init__.py", "")
    _write(tmp_path, "proj/pkg/m.py", "def f():\n    return 1\n")
    src = "def check_one():\n    pass\n\ndef test_two():\n    pass\n"
    _write(tmp_path, "proj/tests/test_m.py", src)
    a = _bound(tmp_path)
    assert a.test_names(src, "proj/tests/test_m.py") == ["tests/test_m.py::check_one"]


def test_remove_tests_drops_decorators_comments_and_empty_classes():
    a = lang_adapter("python")
    out = a.remove_tests(TESTS_SRC, {"tests/test_x.py::test_a[1]", "tests/test_x.py::test_a",
                                     "TestGroup::test_x", "tests/test_x.py::TestGroup::test_y",
                                     "tests/test_x.py::TestMixed::test_m"})
    compile(out, "t.py", "exec")
    assert "a comment for test_a" not in out and "parametrize" not in out
    assert "def test_a(" not in out and "class TestGroup" not in out
    assert "class TestMixed" in out and "def _setup" in out and "def test_m" not in out
    assert "def helper" in out and "def test_async" in out and "import pytest" in out
    assert "\n\n\n\n" not in out
    assert a.remove_tests(TESTS_SRC, set()) == TESTS_SRC


def test_remove_tests_class_with_only_docstring_left():
    a = lang_adapter("python")
    src = 'class TestA:\n    """doc."""\n\n    def test_x(self):\n        pass\n\n\nX = 1\n'
    out = a.remove_tests(src, {"t.py::TestA::test_x"})
    assert "TestA" not in out and "X = 1" in out


def test_strip_non_tests_removes_main_guard():
    a = lang_adapter("python")
    src = "def test_a():\n    pass\n\n\nif __name__ == '__main__':\n    test_a()\n"
    out = a.strip_non_tests(src)
    assert "__main__" not in out and "def test_a" in out
    assert a.normalize_suite(src, None, "x.py") == src


# -- mutation sites ---------------------------------------------------------------------------


MUT_SRC = '''LIMIT = 3 + 4
NAMES = ["a"]


@deco(1 + 2)
def f(a, b=1 + 1, s: str = "x"):
    """a + b is documented."""
    # a - b in a comment
    t = "p" + s
    u = s + "q"
    v = f"{a + b}"
    n = a + b
    n += 1
    acc = ""
    acc += "z"
    lst = NAMES + ["b"]
    ok = a < b and a is not None or b not in (1, 2)
    flag = True
    g = lambda q=1 + 1: q * 2
    return a // b - a % b


class C:
    X = 1 + 1

    def m(self, x):
        return x != 0 and False
'''


def test_mutation_sites_only_in_bodies():
    a = lang_adapter("python")
    sites = a.mutation_sites(MUT_SRC)
    ops = [(MUT_SRC.splitlines()[m.line - 1].strip()[:12], m.operator) for m in sites]
    got = [m.operator for m in sites]
    assert got == [
        "arith: + -> -",           # n = a + b
        "assign: += -> -=",        # n += 1
        "cmp: < -> <=",
        "logic: and -> or",
        "cmp: is not -> is",
        "logic: or -> and",
        "cmp: not in -> in",
        "bool: True -> False",
        "arith: * -> /",           # lambda body
        "arith: // -> /",
        "arith: - -> +",
        "arith: % -> *",
        "cmp: != -> ==",
        "logic: and -> or",
        "bool: False -> True",
    ], ops
    for m in sites:
        assert MUT_SRC[m.offset:m.offset + len(m.original)] == m.original
        assert m.id == f"m{m.offset}"
        compile(apply(MUT_SRC, m), "m.py", "exec")


def test_mutation_sites_on_fixture_are_valid_and_in_functions():
    a = lang_adapter("python")
    for src in (CALC, STATS, SHAPES):
        decls = [d for d in a.decls(src) if d.kind in ("func", "method")]
        for m in a.mutation_sites(src):
            assert any(d.start <= m.offset < d.end for d in decls)
            compile(apply(src, m), "m.py", "exec")
    ops = [m.operator for m in a.mutation_sites(CALC)]
    assert "cmp: == -> !=" in ops and "assign: += -> -=" in ops


def test_mutation_offsets_are_char_offsets_with_non_ascii():
    a = lang_adapter("python")
    src = 'def f(a, b):\n    s = "日本語の説明"\n    return a < b\n'
    (m,) = a.mutation_sites(src)
    assert src[m.offset] == "<" and m.line == 3


# -- projects and units ---------------------------------------------------------------------


async def test_discover_fixture_units_and_targets():
    a = _bound(FIXTURE)
    units = await a.discover(None)
    assert list(units) == ["src/pymod"]
    u = units["src/pymod"]
    assert u.lang == "python" and u.name == u.import_path == "pymod"
    assert u.files == ["src/pymod/__init__.py", "src/pymod/calc.py", "src/pymod/shapes.py",
                       "src/pymod/stats.py"]
    assert u.test_files == ["tests/test_calc.py", "tests/test_stats.py"]
    assert u.build_root == "" and u.dir_name == "src/pymod"
    assert u.extra == {"import_roots": ["src", ""], "project_root": "",
                       "pytest_config": "pyproject.toml", "package": True}
    targets = a.select_targets(FIXTURE, units)
    assert [(t.file, t.stem, t.status, t.lang) for t in targets] == [
        ("src/pymod/__init__.py", "__init__.py", "no-decls", "python"),
        ("src/pymod/calc.py", "calc.py", "ok", "python"),
        ("src/pymod/shapes.py", "shapes.py", "ok", "python"),
        ("src/pymod/stats.py", "stats.py", "ok", "python"),
    ]
    assert a.module_name("src/pymod/calc.py") == "pymod.calc"
    assert a.ctx.py_modules["pymod.calc"] == "src/pymod/calc.py"
    assert a.ctx.py_modules["basename:calc"] == "src/pymod/calc.py"
    assert await a.discover(None, subtree="tests") == {}
    assert list(await a.discover(None, packages=["src"])) == ["src/pymod"]
    assert a.is_test_file("tests/test_calc.py") and not a.is_test_file("src/pymod/calc.py")
    assert a.is_test_file("src/pymod/test_calc_codocs.py")
    assert a.stem("src/pymod/calc.py") == "calc.py"


def test_select_targets_statuses(tmp_path):
    _write(tmp_path, "setup.py", "")
    _write(tmp_path, "app/__init__.py", "")
    _write(tmp_path, "app/conftest.py", "import pytest\n")
    _write(tmp_path, "app/core.py", "def f():\n    return 1\n")
    _write(tmp_path, "app/consts.py", "X = 1\n")
    _write(tmp_path, "app/api_pb2.py", "def f():\n    return 1\n")
    _write(tmp_path, "app/big.py", "x = 1\n" * 30)
    _write(tmp_path, "tests/helpers.py", "def h():\n    return 1\n")
    _write(tmp_path, "tests/test_core.py", "from app.core import f\n\ndef test_f():\n"
           "    assert f() == 1\n")
    a = _bound(tmp_path)
    units = asyncio.run(a.discover(None))
    assert list(units) == ["app"]        # tests/helpers.py is test support, not a unit
    st = {t.file: t.status for t in a.select_targets(tmp_path, units, max_file_lines=20)}
    assert st == {"app/__init__.py": "no-decls", "app/conftest.py": "test-support",
                  "app/core.py": "ok", "app/consts.py": "no-funcs",
                  "app/api_pb2.py": "generated", "app/big.py": "too-large"}
    assert units["app"].test_files == ["tests/test_core.py"]
    kept = a.select_targets(tmp_path, units, max_files=1)
    assert [t.file for t in kept if t.status in ("ok", "no-funcs")] == ["app/big.py"]
    a.ctx.mark_no_toolchain("python", "no pytest")
    assert {t.status for t in a.select_targets(tmp_path, units)} >= {"no-toolchain"}


def test_loose_layout_basename_fallback(tmp_path):
    """no config marker, a sys.path hack in the test, project = common ancestor."""
    _write(tmp_path, "app/launcher/usr/local/lib64/app/environment.py",
           "def env_name():\n    return 'prod'\n")
    _write(tmp_path, "app/launcher/tests/test_environment.py",
           "import sys\nfrom pathlib import Path\n"
           "sys.path.insert(0, str(Path(__file__).parent.joinpath('../usr/local/lib64/app')"
           ".resolve()))\nimport environment\nimport netifaces\n\n"
           "def test_env():\n    assert environment.env_name() == 'prod'\n")
    _write(tmp_path, "app/launcher/tests/requirements.txt",
           "coverage==5.1\nNet-Ifaces>=0.1  # comment\n-r other.txt\n")
    _write(tmp_path, "tools/report.py", "def y():\n    return 2\n")
    a = _bound(tmp_path)
    units = asyncio.run(a.discover(None))
    u = units["app/launcher/usr/local/lib64/app"]
    assert u.name == u.import_path == "app" and u.extra["package"] is False
    assert u.test_files == ["app/launcher/tests/test_environment.py"]
    assert u.build_root == u.extra["project_root"] == "app/launcher"
    assert "app/launcher/usr/local/lib64/app" in u.extra["import_roots"]
    assert a.model.fallback_dirs["app/launcher/tests/test_environment.py"] == [
        "app/launcher/usr/local/lib64/app"]
    assert units["tools"].build_root == "tools" and units["tools"].test_files == []
    assert a.allowed_third_party(tmp_path, u) == ["coverage", "net_ifaces"]
    src = (tmp_path / "app/launcher/tests/test_environment.py").read_text()
    assert a.test_names(src, "app/launcher/tests/test_environment.py") == [
        "tests/test_environment.py::test_env"]
    assert a.resolve_imports("app/launcher/tests/test_environment.py", a.imports(src),
                             a.ctx) == ["app/launcher/usr/local/lib64/app"]


def test_nested_loose_projects_merge_into_the_outer(tmp_path):
    _write(tmp_path, "a/pkg/__init__.py", "")
    _write(tmp_path, "a/pkg/m.py", "def f():\n    return 1\n")
    _write(tmp_path, "a/pkg/sub/__init__.py", "")
    _write(tmp_path, "a/pkg/sub/n.py", "def g():\n    return 1\n")
    _write(tmp_path, "a/tests/test_m.py", "from pkg.m import f\n\ndef test_f():\n    pass\n")
    a = _bound(tmp_path)
    units = asyncio.run(a.discover(None))
    assert units["a/pkg"].import_path == "pkg"
    assert units["a/pkg/sub"].import_path == "pkg.sub"
    assert units["a/pkg"].build_root == "a" and units["a/pkg/sub"].build_root == "a"
    assert units["a/pkg"].test_files == ["a/tests/test_m.py"]


def test_src_layout_config_and_relative_imports(tmp_path):
    _write(tmp_path, "pyproject.toml",
           '[project]\nname = "x"\ndependencies = ["Requests>=2", "pydantic"]\n'
           '[project.optional-dependencies]\ntest = ["pytest-asyncio"]\n'
           '[tool.setuptools.packages.find]\nwhere = ["lib"]\n')
    _write(tmp_path, "lib/pkg/__init__.py", '"""the pkg."""\n')
    _write(tmp_path, "lib/pkg/a.py", "from . import b\nfrom .b import g\nfrom ..x import y\n")
    _write(tmp_path, "lib/pkg/b.py", "def g():\n    return 1\n")
    _write(tmp_path, "lib/pkg/sub/__init__.py", "")
    _write(tmp_path, "lib/pkg/sub/c.py", "from .. import a\nfrom pkg.b import g\n")
    a = _bound(tmp_path)
    units = asyncio.run(a.discover(None))
    assert units["lib/pkg"].extra["import_roots"] == ["lib", ""]
    assert units["lib/pkg/sub"].import_path == "pkg.sub"
    src = (tmp_path / "lib/pkg/a.py").read_text()
    assert a.resolve_imports("lib/pkg/a.py", a.imports(src), a.ctx) == ["lib/pkg"]
    src = (tmp_path / "lib/pkg/sub/c.py").read_text()
    assert a.resolve_imports("lib/pkg/sub/c.py", a.imports(src), a.ctx) == ["lib/pkg"]
    assert a.allowed_third_party(tmp_path, units["lib/pkg"]) == [
        "pydantic", "pytest_asyncio", "requests"]
    facts = a.wiki_facts(tmp_path, "lib/pkg/__init__.py",
                         (tmp_path / "lib/pkg/__init__.py").read_text(), a.ctx)
    assert facts.doc_comment == "the pkg."


# -- context, relevance -------------------------------------------------------------------


def test_package_context_shows_imported_signatures_first():
    a = _bound(FIXTURE)
    u = a.unit("src/pymod")
    ctx = a.package_context(FIXTURE, u, "src/pymod/stats.py")
    assert ctx.startswith("# src/pymod/calc.py\nSCALE = 10\n")
    assert "def clamp(v: int, lo: int, hi: int) -> int:" in ctx
    assert "# src/pymod/shapes.py" in ctx and "class Rect:" in ctx
    ctx = a.package_context(FIXTURE, u, "src/pymod/calc.py")
    assert "@dataclass\nclass Stats:\n    count: int = 0" not in ctx   # not used by calc
    assert "class Stats:" in ctx
    small = a.package_context(FIXTURE, u, "src/pymod/stats.py", max_chars=60)
    assert small.rstrip().endswith("# ... (truncated)") and len(small) < 120
    assert a.package_identifiers(FIXTURE, u, "src/pymod/stats.py") == {
        "SCALE", "clamp", "total", "normalize", "Rect", "classify", "largest"}


def test_package_context_full_class_block(tmp_path):
    _write(tmp_path, "setup.cfg", "")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/model.py", "from dataclasses import dataclass\n\n\n@dataclass\n"
           "class Item:\n    name: str\n    qty: int = 0\n\n    def total(self, price: float)"
           " -> float:\n        return self.qty * price\n\n\ndef _private():\n    pass\n")
    _write(tmp_path, "pkg/use.py", "from pkg.model import Item\n\n\ndef make():\n"
           "    return Item('a')\n")
    a = _bound(tmp_path)
    u = a.unit("pkg")
    ctx = a.package_context(tmp_path, u, "pkg/use.py")
    assert ("# pkg/model.py\n@dataclass\nclass Item:\n    name: str\n    qty: int = 0\n"
            "    def total(self, price: float) -> float: ...\n") in ctx
    assert "_private" not in ctx


def test_relevant_tests_companions_and_bodies():
    a = _bound(FIXTURE)
    u = a.unit("src/pymod")
    cands = a.declared_tests(FIXTURE, u)
    assert cands == {"tests/test_calc.py::test_clamp", "tests/test_calc.py::test_total",
                     "tests/test_calc.py::test_normalize",
                     "tests/test_calc.py::test_known_broken",
                     "tests/test_stats.py::test_stats_add", "tests/test_stats.py::test_mean"}
    passing = cands - {"tests/test_calc.py::test_known_broken"}
    assert a.relevant_tests(FIXTURE, u, "src/pymod/calc.py", passing) == [
        "tests/test_calc.py::test_clamp", "tests/test_calc.py::test_normalize",
        "tests/test_calc.py::test_total"]
    assert a.relevant_tests(FIXTURE, u, "src/pymod/stats.py", passing) == [
        "tests/test_stats.py::test_mean", "tests/test_stats.py::test_stats_add"]
    assert a.relevant_tests(FIXTURE, u, "src/pymod/shapes.py", passing) == []
    idx = a.test_index(FIXTURE, u)
    assert idx["tests/test_stats.py::test_mean"]["file"] == "tests/test_stats.py"
    assert "Stats" in idx["tests/test_stats.py::test_mean"]["idents"]
    assert "mean" in idx["tests/test_stats.py::test_mean"]["selectors"]


def test_relevant_tests_by_module_attribute_and_import(tmp_path):
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/calc.py", "def clamp(v):\n    return v\n\n\nclass Box:\n"
           "    def open(self):\n        return 1\n")
    _write(tmp_path, "pkg/other.py", "def unrelated():\n    return 2\n")
    _write(tmp_path, "tests/test_misc.py",
           "import pkg.calc\nfrom pkg import calc as c\nfrom pkg.calc import Box\n"
           "from pkg.other import unrelated\n\n"
           "def test_attr():\n    assert pkg.calc.clamp(1) == 1\n\n"
           "def test_alias():\n    assert c.clamp(2) == 2\n\n"
           "class TestBox:\n    def test_open(self):\n        assert Box().open() == 1\n\n"
           "def test_other():\n    assert unrelated() == 2\n")
    a = _bound(tmp_path)
    u = a.unit("pkg")
    cands = a.declared_tests(tmp_path, u)
    assert a.relevant_tests(tmp_path, u, "pkg/calc.py", cands) == [
        "tests/test_misc.py::TestBox::test_open", "tests/test_misc.py::test_alias",
        "tests/test_misc.py::test_attr"]
    assert a.relevant_tests(tmp_path, u, "pkg/other.py", cands) == [
        "tests/test_misc.py::test_other"]
    # a runner-collected id the static index does not know: only companions count
    assert a.relevant_tests(tmp_path, u, "pkg/calc.py",
                            {"tests/test_misc.py::Generated::test_z"}) == []


def test_test_reference_has_conftest_helpers_and_example(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/calc.py", "def clamp(v):\n    return v\n")
    _write(tmp_path, "tests/conftest.py", "import pytest\n\n@pytest.fixture\ndef calc_value():\n"
           "    return 3\n")
    _write(tmp_path, "tests/test_calc.py",
           "from pkg.calc import clamp\n\n\ndef make(v):\n    return clamp(v)\n\n\n"
           "def test_clamp():\n    assert clamp(1) == 1\n\n\ndef test_long():\n"
           "    x = clamp(2)\n    y = clamp(3)\n    assert (x, y) == (2, 3)\n")
    a = _bound(tmp_path)
    ref = a.test_reference(tmp_path, a.unit("pkg"), "pkg/calc.py")
    assert ref.startswith("# from tests/conftest.py\nimport pytest")
    assert "def make(v):" in ref
    assert "# example test of the target from the existing suite\ndef test_clamp():" in ref
    assert "def test_long" not in ref


# -- repair, allowed imports, messages ----------------------------------------------------------


def test_fix_imports_adds_and_drops():
    a = lang_adapter("python")
    src = '"""suite."""\nimport os\nfrom pymod.missing import x\nfrom pymod.calc import clamp, nope\n\n\n' \
          "def test_a():\n    assert clamp(1, 0, 2) == 1\n"
    allowed = [ImportSpec(module="pytest", kind="import"),
               ImportSpec(module="pymod.calc", name="total", kind="from")]
    errs = ["src/pymod/test_calc_codocs.py:3: ModuleNotFoundError: No module named "
            "'pymod.missing'",
            "src/pymod/test_calc_codocs.py:4: ImportError: cannot import name 'nope' from "
            "'pymod.calc' (src/pymod/calc.py)",
            "src/pymod/test_calc_codocs.py:7: NameError: name 'pytest' is not defined",
            "src/pymod/test_calc_codocs.py:8: NameError: name 'total' is not defined",
            "src/pymod/other.py:1: NameError: name 'json' is not defined"]
    out = a.fix_imports(src, errs, "test_calc_codocs.py", allowed)
    assert out is not None
    compile(out, "s.py", "exec")
    assert "pymod.missing" not in out and "nope" not in out
    assert "from pymod.calc import clamp\n" in out
    assert "import pytest\nfrom pymod.calc import total\n" in out
    assert out.index("import pytest") > out.index("import os")
    assert a.fix_imports(src, errs, "unrelated.py", allowed) is None
    assert a.fix_imports(src, ["x.py:1: NameError: name 'os' is not defined"], "x.py",
                         [ImportSpec(module="os", kind="import")]) is None


def test_fix_allowed_and_default_allowed():
    a = _bound(FIXTURE)
    base = a.imports(STATS)
    allowed = a.fix_allowed(base)
    assert allowed[: len(base)] == base
    assert {i.module for i in allowed} >= {"json", "os", "re", "datetime", "pathlib"}
    runner = SimpleNamespace(module_root=FIXTURE)
    dflt = [a.format_import(i) for i in a.default_allowed(runner, a.unit("src/pymod"))]
    assert "import pytest" in dflt and "from pymod import calc" in dflt
    assert "from pymod.calc import clamp" in dflt and "from pymod.stats import Stats" in dflt
    assert "from __future__ import annotations" not in dflt


def test_default_allowed_makes_relative_imports_absolute(tmp_path):
    _write(tmp_path, "setup.py", "")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "from .b import g\nfrom . import b\n")
    _write(tmp_path, "pkg/b.py", "def g():\n    return 1\n")
    a = _bound(tmp_path)
    dflt = [a.format_import(i) for i in
            a.default_allowed(SimpleNamespace(module_root=tmp_path), a.unit("pkg"))]
    assert "from pkg.b import g" in dflt and "from pkg import b" in dflt


def test_suite_placement():
    a = _bound(FIXTURE)
    u = a.unit("src/pymod")
    assert a.suite_rel(u, "calc.py") == "src/pymod/test_calc_codocs.py"
    assert a.suite_basename("calc.py") == "test_calc_codocs.py"
    assert a.baseline_path(Path("/o"), u) == Path("/o/baseline/python/src__pymod.json")


def test_failure_and_crash_messages():
    a = lang_adapter("python")
    out = ("def test_x():\n>       assert f() == 2\nE       AssertionError: boom\n"
           "E       assert 1 == 2\n\ntests/test_x.py:3: AssertionError\n")
    msg = a.failure_message(out)
    assert msg.splitlines()[0] == "AssertionError: boom" and "assert f() == 2" in msg
    assert len(a.failure_message("x" * 5000)) <= 2010
    res = LangTestResult(build_ok=True, tail="...\nTimeout (0:00:05)!\nThread 0x1\n")
    assert "timed out" in a.crash_message(res)
    assert a.crash_message(LangTestResult(build_ok=True)).startswith("did not run")


# -- static facts --------------------------------------------------------------------------------


def test_wiki_facts_main_and_scripts(tmp_path):
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n[project.scripts]\n'
           'tool = "pkg.cli:main"\n')
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/cli.py", "def main():\n    pass\n")
    _write(tmp_path, "pkg/run.py", "def f():\n    pass\n\nif __name__ == '__main__':\n    f()\n")
    _write(tmp_path, "pkg/lib.py", "def f():\n    pass\n")
    a = _bound(tmp_path)
    facts = {r: a.wiki_facts(tmp_path, r, (tmp_path / r).read_text(), a.ctx)
             for r in ("pkg/cli.py", "pkg/run.py", "pkg/lib.py")}
    assert facts["pkg/cli.py"].is_main and facts["pkg/run.py"].is_main
    assert not facts["pkg/lib.py"].is_main
    assert facts["pkg/run.py"].loc == 5 and [d.name for d in facts["pkg/run.py"].decls] == ["f"]


def test_graph_facts_calls_types_and_tables():
    a = lang_adapter("python")
    src = '''from pymod import calc
from pymod.calc import clamp
from pymod.store import Store


def helper(x):
    return x


HANDLERS = {"a": helper, "b": clamp}


class Base:
    def run(self):
        return 1


class Svc(Base):
    limit: int = 3

    def __init__(self):
        self.store = Store()

    def go(self, s: Store):
        local = Store()
        self.run()
        super().run()
        self.store.put(1)
        s.get()
        local.close()
        calc.total([1])
        clamp(1, 2, 3)
        cb = helper
        return calc.SCALE
'''
    f = a.graph_facts(Path("."), "pkg/svc.py", src, None)
    assert [(x.name, x.kind, x.cls) for x in f.funcs] == [
        ("helper", "func", ""), ("Base.run", "method", "Base"),
        ("Svc.__init__", "method", "Svc"), ("Svc.go", "method", "Svc")]
    calls = [(c.kind, c.qual, c.name, c.recv_type) for c in f.calls["Svc.go"]]
    assert ("call", "self", "run", "Svc") in calls
    assert ("call", "super", "run", "Svc") in calls
    assert ("call", "self.store", "put", "Store") in calls
    assert ("call", "s", "get", "Store") in calls
    assert ("call", "local", "close", "Store") in calls
    assert ("call", "calc", "total", None) in calls
    assert ("call", "", "clamp", None) in calls
    assert ("ref", "", "helper", None) in calls
    assert ("ref", "calc", "SCALE", None) in calls
    assert f.types["Svc"].bases == ["Base"] and f.types["Svc"].methods == ["__init__", "go"]
    assert f.types["Svc"].fields == {"limit": "int", "store": "Store"}
    assert f.tables == {"HANDLERS": ["helper", "clamp"]}
    assert not f.is_test and [i.module for i in f.imports][:1] == ["pymod"]


# -- the runner (real pytest + coverage.py) ---------------------------------------------------------


@needs_py
async def test_runner_baseline_excludes_known_broken(tmp_path):
    repo = _copy(tmp_path)
    before = _snapshot(repo)
    ad, run = _runner(repo, tmp_path)
    assert run.available() and run.version().startswith("Python ")
    assert run.lang == "python" and run.adapter is ad and run.module == "pymod"
    res = await run.test("src/pymod")
    assert res.build_ok and not res.timed_out
    assert res.tests == {
        "tests/test_calc.py::test_clamp": "pass", "tests/test_calc.py::test_total": "pass",
        "tests/test_calc.py::test_normalize": "pass",
        "tests/test_calc.py::test_known_broken": "fail",
        "tests/test_stats.py::test_stats_add": "pass", "tests/test_stats.py::test_mean": "pass",
    }
    assert "AssertionError: known broken" in res.output["tests/test_calc.py::test_known_broken"]
    msg = ad.failure_message(res.output["tests/test_calc.py::test_known_broken"])
    assert msg.startswith("AssertionError: known broken")
    assert await run.list_tests("src/pymod") == sorted(res.tests)
    assert _snapshot(repo) == before      # nothing written into the repository
    assert not list((tmp_path / "out" / "work" / "tmp").iterdir())   # call dirs removed


@needs_py
async def test_runner_on_the_fixture_in_place_writes_nothing(tmp_path):
    before = _snapshot(FIXTURE)
    _, run = _runner(FIXTURE, tmp_path)
    res = await run.test("src/pymod", run=["tests/test_stats.py::test_mean"])
    assert res.tests == {"tests/test_stats.py::test_mean": "pass"}
    assert _snapshot(FIXTURE) == before


@needs_py
async def test_runner_replaced_broken_file_reports_original_path(tmp_path):
    repo = _copy(tmp_path)
    _, run = _runner(repo, tmp_path)
    broken = CALC.replace("def clamp(v: int, lo: int, hi: int) -> int:",
                          "def clamp(v: int, lo: int, hi: int) -> int")
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": broken},
                         run=["tests/test_calc.py::test_clamp"])
    assert not res.build_ok
    assert len(res.build_errors) == 1
    assert res.build_errors[0].startswith("src/pymod/calc.py:9:")
    assert "SyntaxError" in res.build_errors[0]
    assert "/root/" not in res.build_errors[0] and str(tmp_path) not in res.build_errors[0]
    # an import-time error in the replaced module maps back to its line too
    bad = CALC.replace("SCALE = 10", "SCALE = undefined_name")
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": bad},
                         run=["tests/test_calc.py::test_clamp"])
    assert not res.build_ok
    assert res.build_errors == [
        "src/pymod/calc.py:6: NameError: name 'undefined_name' is not defined"]
    # compile-only reports the same file
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": broken}, compile_only=True)
    assert not res.build_ok and res.build_errors[0].startswith("src/pymod/calc.py:9:")
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": bad}, compile_only=True)
    assert res.build_errors == [
        "src/pymod/calc.py:6: NameError: name 'undefined_name' is not defined"]
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": CALC}, compile_only=True)
    assert res.build_ok and res.tests == {}


@needs_py
async def test_runner_hidden_tests_added_suite_with_coverage(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    suite = ("from pymod.calc import clamp\n\n\ndef test_low():\n    assert clamp(-1, 0, 10)"
             " == 0\n\n\ndef test_mid():\n    assert clamp(5, 0, 10) == 5\n")
    suite_rel = ad.suite_rel(unit, "calc.py")
    ov = {f: None for f in unit.test_files}
    ov[suite_rel] = suite
    res = await run.test("src/pymod", overlay=ov, cover=True)
    assert res.build_ok
    assert res.tests == {"src/pymod/test_calc_codocs.py::test_low": "pass",
                         "src/pymod/test_calc_codocs.py::test_mid": "pass"}
    assert set(ad.test_names(suite, suite_rel)) == set(res.tests)
    cov, uncovered = file_coverage(res.coverage, "src/pymod/calc.py", CALC)
    assert 0 < cov < 1 and 14 in uncovered and 11 not in uncovered
    # every unit file has blocks: the never-imported ones are 0%, the suite is not reported
    files = {b.file for b in res.coverage}
    # (the docstring-only __init__.py has no executable line, so no block)
    assert files == {"src/pymod/calc.py", "src/pymod/stats.py", "src/pymod/shapes.py"}
    assert file_coverage(res.coverage, "src/pymod/shapes.py")[0] == 0.0
    assert all(b.start_line == b.end_line and b.stmts == 1 for b in res.coverage)


@needs_py
async def test_runner_suite_not_using_target_gives_zero_coverage(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    ov = {f: None for f in unit.test_files}
    ov[ad.suite_rel(unit, "shapes.py")] = "def test_nothing():\n    assert 1 + 1 == 2\n"
    res = await run.test("src/pymod", overlay=ov, cover=True)
    assert res.build_ok and res.coverage is not None
    assert file_coverage(res.coverage, "src/pymod/shapes.py")[0] == 0.0


@needs_py
async def test_runner_suite_build_error_and_import_repair(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    suite_rel = ad.suite_rel(unit, "calc.py")
    suite = ("from pymod.calc import clamp\nfrom pymod.nothere import x\n\n\n"
             "@pytest.mark.parametrize('v', [1])\ndef test_a(v):\n    assert clamp(v, 0, 2) == v\n")
    ov = {f: None for f in unit.test_files}
    ov[suite_rel] = suite
    res = await run.test("src/pymod", overlay=ov, cover=True)
    assert not res.build_ok
    assert res.build_errors == [f"{suite_rel}:2: ModuleNotFoundError: No module named "
                                "'pymod.nothere'"]
    allowed = ad.default_allowed(run, unit)
    fixed = ad.fix_imports(suite, res.build_errors, "test_calc_codocs.py", allowed)
    ov[suite_rel] = fixed
    res = await run.test("src/pymod", overlay=ov, cover=True)
    assert not res.build_ok
    assert res.build_errors == [f"{suite_rel}:4: NameError: name 'pytest' is not defined"]
    fixed = ad.fix_imports(fixed, res.build_errors, "test_calc_codocs.py", allowed)
    ov[suite_rel] = fixed
    res = await run.test("src/pymod", overlay=ov, cover=True)
    assert res.build_ok and res.tests == {f"{suite_rel}::test_a": "pass"}


@needs_py
async def test_runner_mutants_killed_and_survived(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    tests = ["tests/test_calc.py::test_clamp", "tests/test_calc.py::test_total",
             "tests/test_calc.py::test_normalize"]
    sites = {m.operator + "@" + str(m.line): m for m in ad.mutation_sites(CALC)}
    killed = sites["cmp: == -> !=@20"]           # len(values) == 0
    survived = sites["cmp: < -> <=@11"]          # v < lo: no test has v == lo
    for m in (killed, survived):
        compile(apply(CALC, m), "calc.py", "exec")
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": apply(CALC, killed)},
                         run=tests, failfast=True, timeout=30)
    assert res.build_ok and res.tests.get("tests/test_calc.py::test_total") == "fail"
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": apply(CALC, survived)},
                         run=tests, failfast=True, timeout=30)
    assert res.build_ok and all(res.tests[t] == "pass" for t in tests)
    res = await run.test("src/pymod", overlay={"src/pymod/calc.py": apply(CALC, survived)},
                         compile_only=True)
    assert res.build_ok


@needs_py
async def test_runner_run_selection_unknown_ids_and_failfast(tmp_path):
    repo = _copy(tmp_path)
    _, run = _runner(repo, tmp_path)
    res = await run.test("src/pymod", run=["tests/test_calc.py::test_clamp",
                                           "tests/test_calc.py::test_missing",
                                           "tests/nofile.py::test_x"])
    assert res.build_ok and res.tests == {"tests/test_calc.py::test_clamp": "pass"}
    res = await run.test("src/pymod", run=[])
    assert res.build_ok and res.tests == {}
    res = await run.test("src/pymod", run=["tests/test_calc.py::test_known_broken",
                                           "tests/test_calc.py::test_normalize",
                                           "tests/test_stats.py::test_mean"], failfast=True)
    assert res.tests["tests/test_calc.py::test_known_broken"] == "fail"
    assert "tests/test_stats.py::test_mean" not in res.tests      # never ran, no verdict
    with pytest.raises(ValueError, match="outside the project root"):
        await run.test("src/pymod", overlay={"../elsewhere.py": "x = 1\n"})


@needs_py
async def test_runner_parametrized_ids_fold(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    suite = ("import pytest\nfrom pymod.calc import clamp\n\n\n"
             "@pytest.mark.parametrize('v,want', [(1, 1), (5, 2), (0, 0)])\n"
             "def test_p(v, want):\n    assert clamp(v, 0, 2) == want\n\n\n"
             "@pytest.mark.parametrize('v', [0, 1])\ndef test_q(v):\n    assert v in (0, 1)\n\n\n"
             "@pytest.mark.skip(reason='x')\ndef test_s():\n    pass\n\n\n"
             "@pytest.mark.xfail(reason='x')\ndef test_x():\n    assert False\n")
    ov = {f: None for f in unit.test_files}
    ov[ad.suite_rel(unit, "calc.py")] = suite
    res = await run.test("src/pymod", overlay=ov)
    s = "src/pymod/test_calc_codocs.py"
    assert res.tests == {f"{s}::test_p": "pass", f"{s}::test_q": "pass", f"{s}::test_s": "skip",
                         f"{s}::test_x": "skip"}
    res = await run.test("src/pymod", overlay=ov, run=[f"{s}::test_q"])
    assert res.tests == {f"{s}::test_q": "pass"}


@needs_py
async def test_runner_inner_timeout(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    suite = ("import time\n\n\ndef test_a():\n    pass\n\n\ndef test_hang():\n"
             "    time.sleep(60)\n\n\ndef test_c():\n    pass\n")
    ov = {f: None for f in unit.test_files}
    ov[ad.suite_rel(unit, "calc.py")] = suite
    t0 = time.monotonic()
    res = await run.test("src/pymod", overlay=ov, timeout=2)
    assert time.monotonic() - t0 < 2 + 5
    s = "src/pymod/test_calc_codocs.py"
    assert res.build_ok and res.timed_out
    assert res.tests == {f"{s}::test_a": "pass", f"{s}::test_hang": "fail",
                         f"{s}::test_c": "interrupted"}
    assert "Timeout (" in res.output[f"{s}::test_hang"]
    assert "timed out" in ad.crash_message(res)


@needs_py
async def test_runner_outer_kill_and_hang_before_tests(tmp_path):
    from codocs.lang import pyrun

    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    s = "src/pymod/test_calc_codocs.py"
    suite = ("import faulthandler\nimport time\n\n\ndef test_a():\n    pass\n\n\n"
             "def test_block():\n    faulthandler.cancel_dump_traceback_later()\n"
             "    time.sleep(60)\n")
    ov = {f: None for f in unit.test_files}
    ov[s] = suite
    t0 = time.monotonic()
    res = await run.test("src/pymod", overlay=ov, timeout=1)
    assert time.monotonic() - t0 < 1 + pyrun.OUTER_SLACK + 15   # loose: shared CI runners
    assert res.build_ok and res.timed_out
    assert res.tests == {f"{s}::test_a": "pass", f"{s}::test_block": "fail"}
    # a module that hangs at import time: killed before any test started
    ov[s] = "import time\ntime.sleep(60)\n\n\ndef test_a():\n    pass\n"
    res = await run.test("src/pymod", overlay=ov, timeout=1)
    assert not res.build_ok and res.timed_out and res.build_errors == [pyrun.NOT_STARTED]


@needs_py
async def test_runner_crash_blames_the_running_test(tmp_path):
    repo = _copy(tmp_path)
    ad, run = _runner(repo, tmp_path)
    unit = ad.unit("src/pymod")
    s = "src/pymod/test_calc_codocs.py"
    ov = {f: None for f in unit.test_files}
    ov[s] = ("import os\n\n\ndef test_a():\n    pass\n\n\ndef test_exit():\n    os._exit(3)\n\n\n"
             "def test_c():\n    pass\n")
    res = await run.test("src/pymod", overlay=ov)
    assert res.build_ok and res.returncode == 3 and not res.timed_out
    assert res.tests == {f"{s}::test_a": "pass", f"{s}::test_exit": "fail",
                         f"{s}::test_c": "interrupted"}
    res = await run.test("src/pymod", overlay=ov, run=[f"{s}::test_c", f"{s}::test_a"])
    assert res.tests == {f"{s}::test_a": "pass", f"{s}::test_c": "pass"}


@needs_py
async def test_runner_other_collection_error_is_a_note(tmp_path):
    repo = _copy(tmp_path)
    _write(repo, "tests/test_extra.py", "from pymod import calc\nimport netifaces_not_installed"
           "\n\n\ndef test_z():\n    assert calc.SCALE\n")
    ad, run = _runner(repo, tmp_path)
    assert "tests/test_extra.py" in ad.unit("src/pymod").test_files
    res = await run.test("src/pymod")
    assert res.build_ok
    assert res.tests["tests/test_calc.py::test_clamp"] == "pass"
    notes = run.unit_notes("src/pymod")
    assert any("collection error in tests/test_extra.py" in n and
               "ModuleNotFoundError" in n for n in notes), notes


@needs_py
async def test_runner_conftest_fixture_and_setup_error(tmp_path):
    repo = _copy(tmp_path)
    _write(repo, "tests/conftest.py", "import pytest\n\n\n@pytest.fixture\ndef three():\n"
           "    return 3\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('x')\n")
    _write(repo, "tests/test_fix.py", "from pymod.calc import clamp\n\n\ndef test_three(three):\n"
           "    assert clamp(three, 0, 10) == 3\n\n\ndef test_broken(broken):\n    pass\n")
    ad, run = _runner(repo, tmp_path)
    res = await run.test("src/pymod", run=["tests/test_fix.py::test_three",
                                           "tests/test_fix.py::test_broken"])
    assert res.tests == {"tests/test_fix.py::test_three": "pass",
                         "tests/test_fix.py::test_broken": "fail"}
    assert "RuntimeError" in res.output["tests/test_fix.py::test_broken"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS /tmp symlink case")
@needs_py
async def test_runner_out_under_tmp_symlink(tmp_path):
    import tempfile

    from codocs.lang.pyrun import PythonRunner

    repo = _copy(tmp_path)
    out = Path(tempfile.mkdtemp(prefix="codocs-py-", dir="/tmp"))
    try:
        ad = _bound(repo)
        run = PythonRunner(repo, adapter=ad, work_dir=out / "work" / "tmp", timeout=60)
        res = await run.test("src/pymod", run=["tests/test_calc.py::test_clamp",
                                               "tests/test_calc.py::test_total"])
        assert res.tests == {"tests/test_calc.py::test_clamp": "pass",
                             "tests/test_calc.py::test_total": "pass"}
    finally:
        shutil.rmtree(out, ignore_errors=True)


@needs_py
async def test_runner_from_context_and_sandbox(tmp_path):
    from codocs.lang.pyrun import PythonRunner

    repo = _copy(tmp_path)
    sandbox = tmp_path / "sandbox"
    shutil.copytree(repo, sandbox)
    (sandbox / "tests/test_calc.py").write_text(TEST_CALC.replace(
        'assert clamp(1, 2, 3) == 1, "known broken"', "assert clamp(1, 2, 3) == 2"))
    synced = []

    async def ensure_synced():
        synced.append(1)

    ad = _bound(repo)
    rc = SimpleNamespace(repo_root=repo, exec_root=sandbox, work_dir=tmp_path / "o/work/tmp",
                         tools_dir=tmp_path / "o/work/tools", settings=None, workers=2,
                         timeout=60, ensure_synced=ensure_synced, sync_generation=1)
    run = ad.make_runner(rc)
    assert isinstance(run, PythonRunner) and run.exec_root == sandbox.resolve()
    res = await run.test("src/pymod", run=["tests/test_calc.py::test_known_broken"])
    assert synced and res.tests == {"tests/test_calc.py::test_known_broken": "pass"}
    assert (tmp_path / "o/work/tools/python/codocs_pytest_report.py").is_file()
    assert (tmp_path / "o/work/py/_root/base/src/pymod/calc.py").is_file()


@needs_py
def test_interpreter_choice_and_notes(tmp_path):
    from codocs.lang.pyrun import PythonRunner

    repo = _copy(tmp_path)
    ad = _bound(repo)
    run = PythonRunner(repo, adapter=ad, work_dir=tmp_path / "o/work/tmp",
                       py=str(tmp_path / "nope" / "python"))
    assert not run.available() and run.version() == ""
    res = asyncio.run(run.test("src/pymod"))
    assert not res.build_ok and "no Python interpreter" in res.build_errors[0]
    venv_py = repo / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\nexit 1\n")
    venv_py.chmod(0o755)
    run = PythonRunner(repo, adapter=ad, work_dir=tmp_path / "o/work/tmp", py="")
    assert run.available() and run.interpreter().path == sys.executable
    assert any("project dependencies may be missing" in n for n in run.unit_notes("src/pymod"))


def test_import_safe():
    from codocs.lang.pyrun import import_safe

    assert import_safe(CALC) and import_safe(STATS)
    assert import_safe("import os\nX = 1\n\nif __name__ == '__main__':\n    main()\n")
    assert not import_safe("import os\nos.system('x')\n")
    assert not import_safe("for i in range(3):\n    pass\n")


def test_fold_verdicts_crash_rules():
    from codocs.lang.pyrun import fold_verdicts

    def ev(kind, uid, phase=None, outcome=None):
        d = {"e": kind, "uid": uid, "id": uid.split("[")[0]}
        if phase:
            d.update(phase=phase, outcome=outcome)
        return d

    events = [ev("run", "t.py::a[1]"), ev("result", "t.py::a[1]", "setup", "passed"),
              ev("result", "t.py::a[1]", "call", "passed"),
              ev("result", "t.py::a[1]", "teardown", "passed"),
              ev("run", "t.py::a[2]"), ev("result", "t.py::a[2]", "setup", "passed"),
              ev("result", "t.py::a[2]", "call", "failed"),
              ev("result", "t.py::a[2]", "teardown", "passed"),
              ev("run", "t.py::b"), ev("result", "t.py::b", "setup", "passed")]
    tests, _ = fold_verdicts(events, requested=["t.py::a", "t.py::b", "t.py::c"], died=True,
                             timed_out=False)
    # b was running but a failed: b is interrupted, never-started c too
    assert tests == {"t.py::a": "fail", "t.py::b": "interrupted", "t.py::c": "interrupted"}
    tests, _ = fold_verdicts(events[:4] + events[8:], requested=None, died=True,
                             timed_out=False)
    assert tests == {"t.py::a": "pass", "t.py::b": "fail"}
    skip = [ev("run", "t.py::s"), ev("result", "t.py::s", "setup", "skipped"),
            ev("result", "t.py::s", "teardown", "passed")]
    assert fold_verdicts(skip, requested=None, died=False, timed_out=False)[0] == {
        "t.py::s": "skip"}


@needs_py
async def test_runner_loose_layout_with_stdlib_shadowing_package(tmp_path):
    """a script dir holding a `platform` package must not shadow the stdlib for tools."""
    repo = tmp_path / "repo"
    _write(repo, "launcher/lib/app/environment.py", "def env_name():\n    return 'prod'\n\n\n"
           "def unused():\n    return 1\n")
    _write(repo, "launcher/lib/app/platform/__init__.py", "NAME = 'mine'\n")
    _write(repo, "launcher/tests/test_environment.py",
           "import environment\n\n\ndef test_env():\n    assert environment.env_name() == 'prod'\n")
    ad, run = _runner(repo, tmp_path)
    u = ad.unit("launcher/lib/app")
    assert u.build_root == "launcher" and u.test_files == ["launcher/tests/test_environment.py"]
    res = await run.test(u.rel, cover=True)
    assert res.build_ok, res.build_errors
    assert res.tests == {"tests/test_environment.py::test_env": "pass"}
    cov, uncovered = file_coverage(res.coverage, "launcher/lib/app/environment.py")
    assert 0 < cov < 1 and uncovered == [6]


async def test_unbound_adapter_does_not_discover():
    with pytest.raises(ValueError, match="not bound"):
        await lang_adapter("python").discover(SimpleNamespace(module_root=FIXTURE))
    bound = _bound(FIXTURE)
    units = await lang_adapter("python").discover(SimpleNamespace(adapter=bound))
    assert list(units) == ["src/pymod"] and lang_adapter("python").ctx is None


# -- pytest collection, farms, repair, mutants, relevance ----------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        _write(root, rel, text)
    return root


RX_SHOP = {
    "src/shop/__init__.py": "from .core.models import Item, total\n",
    "src/shop/core/__init__.py": "",
    "src/shop/core/models.py": (
        "class Item:\n    def __init__(self, n):\n        self.n = n\n\n\n"
        "def total(items):\n    return sum(i.n for i in items)\n"),
}


# -- addopts --import-mode is kept ----------------------------------------------------------


def test_carried_addopts_keeps_import_mode_and_plugins():
    from codocs.lang.pyrun import carried_addopts

    got = carried_addopts("--import-mode=importlib -p no:warnings -p xdist -n 4 --cov=x "
                          "--strict-markers --import-mode prepend -pfoo")
    assert got == ["--import-mode=importlib", "-p", "no:warnings", "--strict-markers",
                   "--import-mode=prepend", "-p", "foo"]
    assert carried_addopts(["--import-mode=importlib", "-q"]) == ["--import-mode=importlib"]
    assert carried_addopts(None) == []


@needs_py
def test_duplicate_test_basenames_with_importlib_mode(tmp_path):
    repo = _tree(tmp_path / "dup", dict(RX_SHOP, **{
        "pyproject.toml": ('[tool.pytest.ini_options]\naddopts = "--import-mode=importlib"\n'
                           'pythonpath = ["src"]\n'),
        "tests/unit/test_models.py": ("from shop.core.models import Item, total\n\n\n"
                                      "def test_total():\n    assert total([Item(2)]) == 2\n"),
        "tests/integration/test_models.py": ("from shop.core.models import total\n\n\n"
                                             "def test_empty():\n    assert total([]) == 0\n"),
    }))
    ad, run = _runner(repo, tmp_path)
    res = asyncio.run(run.test("src/shop/core"))
    assert res.build_ok
    assert res.tests == {"tests/integration/test_models.py::test_empty": "pass",
                         "tests/unit/test_models.py::test_total": "pass"}


# -- packages named build/dist/target stay in the farm -------------------------------------


@needs_py
def test_package_named_build_is_importable_in_the_farm(tmp_path):
    repo = _tree(tmp_path / "bld", {
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
        "src/build/__init__.py": "def version():\n    return 3\n",
        "src/deploy/__init__.py": "",
        "src/deploy/target/__init__.py": "def resolve(x):\n    return x + 1\n",
        "tests/test_build.py": ("from build import version\n\n\n"
                                "def test_version():\n    assert version() == 3\n"),
        "tests/test_target.py": ("from deploy.target import resolve\n\n\n"
                                 "def test_resolve():\n    assert resolve(1) == 2\n"),
        "build/lib/junk.py": "raise SystemExit('a build output dir')\n",
    })
    ad, run = _runner(repo, tmp_path)
    assert asyncio.run(run.test("src/build")).tests == {"tests/test_build.py::test_version":
                                                        "pass"}
    assert asyncio.run(run.test("src/deploy/target")).tests == {
        "tests/test_target.py::test_resolve": "pass"}


def test_base_farm_skips_build_dirs_without_model_files(tmp_path):
    from codocs.lang.pyrun import build_base_farm

    repo = _tree(tmp_path / "r", {"src/build/__init__.py": "", "build/lib/x.py": "",
                                   "dist/y.py": ""})
    build_base_farm(repo, tmp_path / "farm", set(), {"src", "src/build"})
    assert (tmp_path / "farm/src/build/__init__.py").is_file()
    assert not (tmp_path / "farm/build").exists() and not (tmp_path / "farm/dist").exists()


def test_base_farm_keeps_regular_packages_and_drops_build_output_beside_a_build_file(tmp_path):
    from codocs.lang.pyrun import build_base_farm

    repo = _tree(tmp_path / "r", {
        "pyproject.toml": "", "src/build/__init__.py": "", "src/dist/mod.py": "",
        "build/lib/shop/__init__.py": "", "pkg/target/__init__.py": "",
    })
    model = {"src", "src/build", "src/dist", "build", "build/lib", "build/lib/shop", "pkg",
             "pkg/target"}
    build_base_farm(repo, tmp_path / "farm", set(), model)
    farm = tmp_path / "farm"
    assert (farm / "src/build/__init__.py").is_file() and (farm / "src/dist/mod.py").is_file()
    assert (farm / "pkg/target/__init__.py").is_file()
    assert not (farm / "build").exists()
    # without a model only regular packages survive
    build_base_farm(repo, tmp_path / "farm2", set(), None)
    assert (tmp_path / "farm2/src/build/__init__.py").is_file()
    assert not (tmp_path / "farm2/src/dist").exists() and not (tmp_path / "farm2/build").exists()


# -- static test discovery follows pytest's collection -------------------------------------

RX_SUITE = '''import pytest


@pytest.fixture
def test_values():
    return [1, 2]


def test_table(test_values):
    assert test_values


class TestBase:
    def test_inherited(self):
        assert 1


class TestChild(TestBase):
    pass


try:
    def test_in_try():
        assert 1
except Exception:
    pass


def test_x():
    assert 1


test_alias = test_x


class TestOuter:
    def test_a(self):
        pass

    class TestNested:
        def test_nested(self):
            assert 1 == 2
'''


def test_test_names_follow_pytest_collection():
    from codocs.lang.python import PythonAdapter

    names = PythonAdapter().test_names(RX_SUITE, "t_codocs.py")
    assert [n.split("::", 1)[1] for n in names] == [
        "test_table", "TestBase::test_inherited", "TestChild::test_inherited", "test_in_try",
        "test_x", "test_alias", "TestOuter::test_a", "TestOuter::TestNested::test_nested"]
    assert not any("test_values" in n for n in names)


def test_remove_tests_nested_conditional_and_aliases():
    from codocs.lang.python import PythonAdapter

    ad = PythonAdapter()
    out = ad.remove_tests(RX_SUITE, {"t.py::TestOuter::TestNested::test_nested",
                                  "t.py::test_in_try", "t.py::test_x"})
    compile(out, "t.py", "exec")
    names = [n.split("::", 1)[1] for n in ad.test_names(out, "t.py")]
    assert names == ["test_table", "TestBase::test_inherited", "TestChild::test_inherited",
                     "TestOuter::test_a"]
    assert "test_alias" not in out and "@pytest.fixture" in out
    assert "try:\n    pass\nexcept" in out


def test_unlisted_failures_block_green():
    from codocs.lang.python import PythonAdapter

    ad = PythonAdapter()
    res = LangTestResult(build_ok=True, tests={"p/t.py::test_x": "pass",
                                               "p/t.py::test_x[1]": "fail",
                                               "p/t.py::Hidden::test_y": "fail",
                                               "p/other.py::test_z": "fail"})
    assert ad.unlisted_failures(res, "def test_x():\n    pass\n", "p/t.py") == [
        "p/t.py::Hidden::test_y"]


RX_PYPROJ = {
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    "src/inv/__init__.py": "",
    "src/inv/util.py": ("def clamp(x, lo, hi):\n    if x < lo:\n        return lo\n"
                        "    if x > hi:\n        return hi\n    return x\n"),
    "src/inv/pricing.py": ("from .util import clamp\n\n\ndef bulk(n):\n"
                           "    return clamp(n * 2, 0, 10)\n"),
    "tests/test_pricing.py": ("from inv.pricing import bulk\n\n\n"
                              "def test_bulk():\n    assert bulk(3) == 6\n\n\n"
                              "def test_cap():\n    assert bulk(9) == 10\n"),
}


@needs_py
def test_measure_prunes_nested_failure_and_keeps_fixture_tests(tmp_path):
    from codocs.tesearch.measure import measure

    repo = _tree(tmp_path / "p", RX_PYPROJ)
    ad, run = _runner(repo, tmp_path)
    units = asyncio.run(ad.discover(run))
    suite = '''import pytest
from inv.util import clamp


@pytest.fixture
def test_data():
    return [(5, 0, 3, 3), (-1, 0, 3, 0)]


def test_table(test_data):
    for x, lo, hi, want in test_data:
        assert clamp(x, lo, hi) == want


class TestClamp:
    def test_inside(self):
        assert clamp(1, 0, 3) == 1

    class TestNested:
        def test_wrong(self):
            assert clamp(1, 0, 3) == 2
'''
    m = asyncio.run(measure(run, units["src/inv"], "src/inv/util.py", suite, []))
    assert m.build_ok and m.green_src
    local = sorted(n.split("::", 1)[1] for n in m.passing)
    assert local == ["TestClamp::test_inside", "test_table"]
    assert list(m.failing) == ["src/inv/test_util_codocs.py::TestClamp::TestNested::test_wrong"]
    assert "def test_data" in m.green_src and "test_wrong" not in m.green_src


# -- a NameError at run time is repaired like an import error ------------------------------


def test_runtime_import_errors_from_tracebacks():
    from codocs.lang.python import PythonAdapter

    out = ("    def test_bulk():\n>       assert bulk(3) == 6\n\ntests/test_pricing.py:5: \n"
           "_ _ _\n\n    def bulk(n):\n>       return clamp(n * 2, 0, 10)\n"
           "E       NameError: name 'clamp' is not defined\n\nsrc/inv/pricing.py:5: NameError\n")
    res = LangTestResult(build_ok=True, tests={"t::a": "fail", "t::b": "pass"},
                         output={"t::a": out, "t::b": out})
    ad = PythonAdapter()
    assert ad.runtime_import_errors(res, "src/inv/pricing.py") == [
        "src/inv/pricing.py:5: NameError: name 'clamp' is not defined"]
    assert ad.runtime_import_errors(res, "src/inv/util.py") == []


@needs_py
def test_specforge_repairs_missing_import_used_in_a_function(tmp_path):
    from codocs.baseline import package_baseline
    from codocs.config import Settings
    from codocs.specforge.loop import SpecForgeLoop

    repo = _tree(tmp_path / "p", RX_PYPROJ)
    ad, run = _runner(repo, tmp_path)
    units = asyncio.run(ad.discover(run))
    unit = units["src/inv"]
    work = tmp_path / "w"
    b = asyncio.run(package_baseline(run, unit, work, force=True))
    loop = SpecForgeLoop(run, None, Settings(), work, repo)
    t = next(x for x in ad.select_targets(repo, units) if x.file == "src/inv/pricing.py")
    sig = loop._signal(t, unit, b, None)
    assert sig.name == "tests"
    code = "def bulk(n):\n    return clamp(n * 2, 0, 10)\n"
    allowed = ad.fix_allowed(ad.imports(RX_PYPROJ["src/inv/pricing.py"]))
    ev, final = asyncio.run(loop.evaluate(unit, t, sig, code, allowed))
    assert ev.phi == 1.0 and ev.import_fixes == 1
    assert "from .util import clamp" in final


@needs_py
def test_tesearch_repairs_missing_pytest_import(tmp_path):
    from codocs.tesearch.measure import measure

    repo = _tree(tmp_path / "p", RX_PYPROJ)
    ad, run = _runner(repo, tmp_path)
    units = asyncio.run(ad.discover(run))
    suite = ("from inv.util import clamp\n\n\ndef test_raises():\n"
             "    with pytest.raises(TypeError):\n        clamp(None, 0, 1)\n\n\n"
             "def test_inside():\n    assert clamp(1, 0, 3) == 1\n")
    m = asyncio.run(measure(run, units["src/inv"], "src/inv/util.py", suite, []))
    assert m.green_size == 2 and not m.failing
    assert "import pytest" in m.green_src


# -- a mutant that breaks the import is killed ---------------------------------------------


@needs_py
def test_import_breaking_mutant_is_killed(tmp_path):
    from codocs.tesearch.measure import measure

    repo = _tree(tmp_path / "p", {
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "codes/__init__.py": "",
        "codes/table.py": ("def _build(n):\n    out = []\n    for i in range(n):\n"
                           "        if i > 4:\n            raise IndexError(i)\n"
                           "        out.append(i * 2)\n    return out\n\n\nTABLE = _build(5)\n\n\n"
                           "def lookup(i):\n    return TABLE[i]\n"),
    })
    ad, run = _runner(repo, tmp_path)
    units = asyncio.run(ad.discover(run))
    src = (repo / "codes/table.py").read_text()
    muts = [m for m in ad.mutation_sites(src) if m.operator == "cmp: > -> >="]
    assert muts
    suite = ("from codes.table import lookup\n\n\ndef test_lookup():\n"
             "    assert lookup(2) == 4\n")
    m = asyncio.run(measure(run, units["codes"], "codes/table.py", suite, muts))
    assert m.mutants_valid == len(muts) and m.killed == len(muts)


def test_broken_mutant_classification():
    from codocs.lang.python import PythonAdapter

    ad = PythonAdapter()
    ok = "def f(x):\n    return x >= 1\n"
    assert ad.broken_mutant("def f(:\n", None) == "invalid"
    assert ad.broken_mutant(ok, LangTestResult(
        build_ok=False, build_errors=["codes/table.py:6: IndexError: 4"])) == "killed"
    assert ad.broken_mutant(ok, LangTestResult(
        build_ok=False, build_errors=["codes/t.py:2: SyntaxError: invalid syntax"])) == "invalid"
    assert ad.broken_mutant(ok, LangTestResult(
        build_ok=False, build_errors=["no Python interpreter with pytest and coverage: x"])) \
        == "invalid"


# -- conftest fixtures and package re-exports ----------------------------------------------


@needs_py
def test_fixture_only_and_reexport_tests_are_associated_and_relevant(tmp_path):
    repo = _tree(tmp_path / "p", dict(RX_SHOP, **{
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
        "tests/conftest.py": ("import pytest\nfrom shop.core.models import Item\n\n\n"
                              "@pytest.fixture\ndef item():\n    return Item(3)\n"),
        "tests/test_flow.py": "def test_flow(item):\n    assert item.n == 3\n",
        "tests/test_api.py": ("from shop import Item, total\n\n\n"
                              "def test_api():\n    assert total([Item(1)]) == 1\n"),
    }))
    ad, run = _runner(repo, tmp_path)
    units = asyncio.run(ad.discover(run))
    core = units["src/shop/core"]
    assert "tests/test_flow.py" in core.test_files and "tests/test_api.py" in core.test_files
    res = asyncio.run(run.test("src/shop/core"))
    passing = {k for k, v in res.tests.items() if v == "pass"}
    assert passing == {"tests/test_flow.py::test_flow", "tests/test_api.py::test_api"}
    assert ad.relevant_tests(repo, core, "src/shop/core/models.py", passing) == sorted(passing)
    pkg = units["src/shop"]
    assert ad.relevant_tests(repo, pkg, "src/shop/__init__.py",
                             {"tests/test_api.py::test_api"}) == ["tests/test_api.py::test_api"]


# -- definitions under module-level if/try -------------------------------------------------

RX_COMPAT = '''import sys

try:
    from os import scandir as _scan

    def list_names(p):
        return [e.name for e in _scan(p)]
except ImportError:
    def list_names(p):
        return []

if sys.platform == "win32":
    def normalize(n):
        return n.lower()

    class Lock:
        pass
else:
    def normalize(n):
        return n

    class Lock:
        def acquire(self):
            return True

    HAVE_FCNTL = True


def walk(p):
    return [normalize(n) for n in list_names(p)]


if __name__ == "__main__":
    def script_only():
        pass
'''


def test_conditional_definitions_are_decls_and_graph_nodes(tmp_path):
    from codocs.lang.python import PythonAdapter, py_decls
    from codocs.specforge.docfmt import expected_sections

    names = [(d.kind, d.qualname) for d in py_decls(RX_COMPAT)]
    assert names == [("func", "list_names"), ("func", "normalize"), ("type", "Lock"),
                     ("method", "Lock.acquire"), ("var", "HAVE_FCNTL"), ("func", "walk")]
    assert any("list_names" in str(s) for s in expected_sections(RX_COMPAT, "python"))
    repo = _tree(tmp_path / "c", {"compat/__init__.py": "", "compat/fs.py": RX_COMPAT})
    ad = PythonAdapter(RepoContext.build(repo, ["python"]))
    assert ad.target_status(RX_COMPAT, "compat/fs.py", 1500)[1] == "ok"
    facts = ad.graph_facts(repo, "compat/fs.py", RX_COMPAT, ad.ctx)
    assert [f.name for f in facts.funcs if f.kind == "func"] == ["list_names", "normalize",
                                                                "walk"]
    assert {c.name for c in facts.calls["walk"]} >= {"normalize", "list_names"}


# -- per-test output carries repo paths, not the call farm ---------------------------------


@needs_py
def test_failure_output_has_no_farm_paths(tmp_path):
    repo = _tree(tmp_path / "p", RX_PYPROJ)
    ad, run = _runner(repo, tmp_path)
    suite = ("def test_missing_fixture(nope):\n    pass\n\n\n"
             "def test_imp():\n    from inv.util import nothere  # noqa\n")
    res = asyncio.run(run.test("src/inv", overlay={"src/inv/test_x_codocs.py": suite,
                                                   "tests/test_pricing.py": None}))
    assert set(res.tests.values()) == {"fail"}
    work = str(tmp_path / "out" / "work")
    for text in res.output.values():
        assert work not in text and "/root/" not in text
    assert "src/inv/test_x_codocs.py" in res.output["src/inv/test_x_codocs.py::"
                                                    "test_missing_fixture"]
