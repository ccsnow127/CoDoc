"""COBOL in the language registry: extensions, ALL_LANGS, detection,
content sniffing only on request, COBOL-scope excludes, no tree-sitter needed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import codocs.lang as lang_pkg
from codocs.config import Settings
from codocs.lang import treesitter
from codocs.lang.base import ALL_LANGS, DISPLAY, LANG_ORDER, NO_TS_LANGS, RepoContext
from codocs.lang.registry import (
    ADAPTERS,
    ALIASES,
    NO_FILES,
    adapter,
    detect_languages,
    file_language,
    is_cobol_member,
    parse_requested,
    resolve_languages,
    scan_files,
    source_language,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
COBOLMOD = FIXTURES / "cobolmod"
PROGRAM = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PROG1.\n"
           "       PROCEDURE DIVISION.\n           STOP RUN.\n")


def test_languages_and_tables():
    assert LANG_ORDER == ("go", "c", "cpp", "java", "python")
    assert ALL_LANGS == LANG_ORDER + ("cobol",)
    assert lang_pkg.ALL_LANGS == ALL_LANGS and "ALL_LANGS" in lang_pkg.__all__
    assert DISPLAY["cobol"] == "COBOL" and NO_TS_LANGS == {"go", "cobol"}
    assert ALIASES["cbl"] == "cobol" and ALIASES["cob"] == "cobol"
    assert ADAPTERS["cobol"] == ("codocs.lang.cobol", "CobolAdapter")
    assert NO_FILES.startswith("has no supported source files (no go.mod;")
    assert parse_requested("all")[-1] == "cobol"
    assert parse_requested("cbl,python") == ["python", "cobol"]
    with pytest.raises(ValueError, match="unknown language 'rpg'"):
        parse_requested("rpg")


def test_extensions_case_insensitive_only_for_cobol():
    for rel in ("a/X.cbl", "X.CBL", "x.Cob", "p.pco", "q.sqb", "r.cobol"):
        assert source_language(rel) == "cobol", rel
    assert source_language("x.C") == "cpp" and source_language("x.c") == "c"
    assert source_language("x.cpy") is None and is_cobol_member("x.CPY")
    for rel in ("j.jcl", "p.PROC", "m.bms", "d.csd", "t.dcl"):
        assert is_cobol_member(rel) and file_language(Path("."), rel) == "cobol"
    assert not is_cobol_member("x.txt")


def test_detection_of_cobolmod_and_member_ownership():
    det = detect_languages(COBOLMOD, out_dir=None)
    assert det.languages == ["cobol"] and det.counts == {"cobol": 5}
    files, _ = scan_files(COBOLMOD)
    owned = set(files["cobol"])
    assert {"cpy/DATEPARM.cpy", "jcl/ACCTJOB.jcl", "bms/ACCTSET.bms", "csd/ACCT.csd",
            "test/TDATEUTL.cbl"} <= owned
    detection_only, _ = scan_files(COBOLMOD, headers=False)
    assert all(source_language(r) == "cobol" for r in detection_only["cobol"])
    assert adapter("cobol").owns("cpy/DATEPARM.cpy") and adapter("cobol").owns("X.CBL")
    assert not adapter("cobol").owns("x.py")


def test_sniffing_only_when_cobol_is_requested(tmp_path: Path):
    repo = tmp_path / "pds"
    repo.mkdir()
    (repo / "PROG1").write_text(PROGRAM)
    with pytest.raises(ValueError, match="has no supported source files"):
        detect_languages(repo, out_dir=None)
    det = detect_languages(repo, requested="cobol", out_dir=None)
    assert det.languages == ["cobol"] and det.counts == {"cobol": 1}
    with pytest.raises(ValueError, match="no COBOL files"):
        detect_languages(FIXTURES / "pymod", requested="cobol", out_dir=None)


def test_exclude_drops_only_cobol_files(tmp_path: Path):
    repo = tmp_path / "mixed"
    shutil.copytree(FIXTURES / "pymod", repo)
    (repo / "legacy").mkdir()
    (repo / "legacy" / "OLD.cbl").write_text(PROGRAM)
    (repo / "legacy" / "tool.py").write_text("x = 1\n")
    (repo / "cbl").mkdir()
    (repo / "cbl" / "NEW.cbl").write_text(PROGRAM.replace("PROG1", "NEW"))
    files, _ = scan_files(repo, exclude=("legacy",))
    assert files["cobol"] == ["cbl/NEW.cbl"] and "legacy/tool.py" in files["python"]
    det = detect_languages(repo, requested="all", out_dir=None, exclude=("legacy", "cbl"))
    assert "cobol" not in det.languages and "python" in det.languages
    st = Settings()
    st.exclude_paths = "legacy"
    assert resolve_languages(repo, st).counts["cobol"] == 1
    st.source_lang = "auto"
    ctx = RepoContext.build(repo, ["python", "cobol"], st, None)
    assert ctx.languages == ["python", "cobol"] and ctx.files["cobol"] == ["cbl/NEW.cbl"]
    assert "legacy/tool.py" in ctx.files["python"]
    # the default () changes nothing
    assert scan_files(repo)[0]["cobol"] == ["cbl/NEW.cbl", "legacy/OLD.cbl"]


def test_cobol_needs_no_tree_sitter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(treesitter, "available", lambda: False)
    assert treesitter.with_deps(["cobol"], "wiki") == ["cobol"]
    assert treesitter.with_deps(["go", "cobol", "python"], "wiki") == ["go", "cobol"]
    with pytest.raises(treesitter.MissingDependency):
        treesitter.with_deps(["python"], "wiki")
    import codocs.lang.cobol as cobol_mod

    assert cobol_mod.CobolAdapter.name == "cobol"
