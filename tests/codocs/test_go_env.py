import shutil
from pathlib import Path

import pytest

from codocs.config import Settings
from codocs.golang import gotool
from codocs.golang.gotool import GoRunner

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gomod"
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")


def test_settings_parse_go_env_and_flags(monkeypatch):
    monkeypatch.delenv("CODOCS_GO_ENV", raising=False)
    monkeypatch.delenv("CODOCS_GO_FLAGS", raising=False)
    s = Settings.load(go_env="LD_LIBRARY_PATH={root}/lib; FOO=a=b ;bad", go_flags="-tags=integration -trimpath")
    assert s.go_env_map() == {"LD_LIBRARY_PATH": "{root}/lib", "FOO": "a=b"}
    assert s.go_flag_list() == ["-tags=integration", "-trimpath"]


def test_settings_go_env_from_environment(monkeypatch):
    monkeypatch.setenv("CODOCS_GO_ENV", "A=1;B=2")
    monkeypatch.setenv("CODOCS_GO_FLAGS", "-tags=x")
    s = Settings.load()
    assert s.go_env_map() == {"A": "1", "B": "2"}
    assert s.go_flag_list() == ["-tags=x"]


def test_runner_env_expands_root(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "m")
    r = GoRunner(tmp_path / "m", env={"LD_LIBRARY_PATH": "{root}/lib"},
                 sandbox=tmp_path / "sb")
    assert r._env()["LD_LIBRARY_PATH"] == f"{(tmp_path / 'sb').resolve()}/lib"


def test_sync_skips_big_non_library_files(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "service").write_bytes(b"\0" * 2048)
    (src / "bin" / "libx.so").write_bytes(b"\0" * 2048)
    (src / "bin" / "libx.so.1").write_bytes(b"\0" * 2048)
    (src / "a.go").write_text("package a\n")
    monkeypatch.setattr(gotool, "SYNC_MAX_BYTES", 1024)
    dst = tmp_path / "dst"
    gotool._sync_tree(src, dst, set())
    assert not (dst / "bin" / "service").exists()
    assert (dst / "bin" / "libx.so").exists() and (dst / "bin" / "libx.so.1").exists()
    assert (dst / "a.go").exists()


@needs_go
async def test_flags_and_env_reach_go_test(tmp_path):
    mod = tmp_path / "m"
    shutil.copytree(FIXTURE, mod)
    (mod / "calc" / "tag_test.go").write_text(
        '//go:build codocstag\n\npackage calc\n\nimport (\n\t"os"\n\t"testing"\n)\n\n'
        'func TestTagged(t *testing.T) {\n\tif os.Getenv("CODOCS_X") != "y" {\n'
        '\t\tt.Fatal("env missing")\n\t}\n}\n')
    plain = GoRunner(mod)
    res = await plain.test("calc", run=["TestTagged"])
    assert "TestTagged" not in res.tests
    tagged = GoRunner(mod, env={"CODOCS_X": "y"}, flags=["-tags=codocstag"])
    res = await tagged.test("calc", run=["TestTagged"])
    assert res.tests.get("TestTagged") == "pass"


def test_sandbox_dir_keeps_name_and_links_siblings(tmp_path):
    from codocs.pipeline import _sandbox_dir

    (tmp_path / "proj" / "myrepo").mkdir(parents=True)
    (tmp_path / "proj" / "shared").mkdir()
    (tmp_path / "proj" / "notes.txt").write_text("x")
    sb = _sandbox_dir(tmp_path / "proj" / "myrepo", tmp_path / "work" / "sandbox")
    assert sb == tmp_path / "work" / "sandbox" / "myrepo"
    assert (tmp_path / "work" / "sandbox" / "shared").resolve() == (tmp_path / "proj" / "shared")
    assert (tmp_path / "work" / "sandbox" / "notes.txt").is_symlink()


@needs_go
async def test_sandbox_resolves_paths_that_climb_out_of_the_module(tmp_path):
    """a relative replace (like cgo -I../../..) must resolve in the mirror as in the original."""
    from codocs.pipeline import make_runner

    proj = tmp_path / "proj"
    (proj / "dep" / "util").mkdir(parents=True)
    (proj / "dep" / "go.mod").write_text("module example.com/dep\n\ngo 1.21\n")
    (proj / "dep" / "util" / "util.go").write_text(
        "package util\n\nfunc Twice(x int) int { return 2 * x }\n")
    app = proj / "app"
    (app / "core").mkdir(parents=True)
    (app / "go.mod").write_text(
        "module example.com/app\n\ngo 1.21\n\nrequire example.com/dep v0.0.0\n\n"
        "replace example.com/dep => ../dep\n")
    (app / "core" / "core.go").write_text(
        'package core\n\nimport "example.com/dep/util"\n\n'
        "func Four() int { return util.Twice(2) }\n")
    (app / "core" / "core_test.go").write_text(
        'package core\n\nimport "testing"\n\n'
        'func TestFour(t *testing.T) {\n\tif Four() != 4 {\n\t\tt.Fatal("bad")\n\t}\n}\n')
    runner = make_runner(app, Settings.load(), tmp_path / "out")
    res = await runner.test("core")
    assert res.build_ok, res.build_errors
    assert res.tests == {"TestFour": "pass"}
    assert runner.exec_root.name == "app"
