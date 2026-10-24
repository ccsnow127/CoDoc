"""extension tables, language detection, the C/C++ header rule and adapter lookup."""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from codocs.lang.base import ALL_LANGS, DISPLAY

SOURCE_EXTS: dict[str, tuple[str, ...]] = {
    "go": (".go",),
    "python": (".py",),
    "java": (".java",),
    "c": (".c",),
    "cpp": (".cc", ".cpp", ".cxx", ".c++", ".C"),
}
CPP_HEADER_EXTS = (".hh", ".hpp", ".hxx", ".h++")
HEADER_EXTS = (".h",) + CPP_HEADER_EXTS
# COBOL (matched case-insensitively, only after the case-sensitive table): program
# candidates count for detection; members (copybooks, JCL, BMS, CSD) are owned, never count
COBOL_PROGRAM_EXTS = (".cbl", ".cob", ".cobol", ".ccp", ".pco", ".sqb", ".cblle", ".sqlcblle",
                      ".scbl", ".cicscbl", ".cbx")
COBOL_COPYBOOK_EXTS = (".cpy", ".copy", ".cpb", ".cblcpy", ".dcl", ".dclgen", ".cpybk",
                       ".copybook")
COBOL_MEMBER_EXTS = COBOL_COPYBOOK_EXTS + (".jcl", ".prc", ".proc", ".job", ".bms", ".csd")
ALIASES = {"golang": "go", "py": "python", "c++": "cpp", "cxx": "cpp", "cbl": "cobol",
           "cob": "cobol"}
SKIP_DIRS = frozenset({"vendor", "node_modules", "testdata", "markdown_docs", "__pycache__",
                       "site-packages"})
# build trees: `build`/`dist` beside one of BUILD_FILES (setuptools build/lib copies, CMake
# in-source builds) unless they are a Python package, CMake's own dirs, egg-info and the like
BUILD_FILES = frozenset({"pyproject.toml", "setup.py", "setup.cfg", "CMakeLists.txt",
                         "build.gradle", "build.gradle.kts"})
BUILD_OUTPUT_DIRS = frozenset({"build", "dist"})
BUILD_OUTPUT_GLOBS = ("CMakeFiles", "*.egg-info", "cmake-build-*", "bazel-*")
PY_TEST_PATTERNS = ("test_*.py", "*_test.py")
JAVA_TEST_PATTERNS = ("Test*.java", "*Test.java", "*Tests.java", "*TestCase.java")
NO_FILES = ("has no supported source files (no go.mod; no .py, .java, .c, C++ or COBOL "
            "files)")

_EXT_LANG = {ext: lang for lang, exts in SOURCE_EXTS.items() for ext in exts}
_CPP_SNIFF = [
    re.compile(r"\bclass\s+\w+\s*[:{]"),
    re.compile(r"\bnamespace\s+\w*\s*\{"),
    re.compile(r"\btemplate\s*<"),
    re.compile(r"^\s*(?:public|private|protected)\s*:", re.MULTILINE),
    re.compile(r"\w::\w"),
]
_CPLUSPLUS_IF_RE = re.compile(r"^\s*#\s*(?:ifdef\s+__cplusplus\b|if\s+.*\b__cplusplus\b)")
_PP_IF_RE = re.compile(r"^\s*#\s*if")
_PP_ELSE_RE = re.compile(r"^\s*#\s*(?:else|elif)\b")
_PP_ENDIF_RE = re.compile(r"^\s*#\s*endif\b")


# -- extensions -----------------------------------------------------------------


def _suffix(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def _dir_flags(names: list[str]) -> tuple[bool, bool]:
    """(has .c sources, has C++ sources) among a directory's file names."""
    sufs = {_suffix(n) for n in names}
    return ".c" in sufs, any(s in sufs for s in SOURCE_EXTS["cpp"])


def source_language(rel: str) -> str | None:
    """language of a source file by its extension (headers and other files: None).

    the case-sensitive table first (`.C` stays C++), then COBOL program extensions in any case.
    """
    suf = _suffix(rel)
    lang = _EXT_LANG.get(suf)
    if lang is None and suf.lower() in COBOL_PROGRAM_EXTS:
        return "cobol"
    return lang


def is_cobol_member(rel: str) -> bool:
    """a COBOL copybook, JCL, PROC, BMS or CSD file (owned by cobol, never a detection count)."""
    return _suffix(rel).lower() in COBOL_MEMBER_EXTS


def _cobol_excluded(rel: str, exclude: tuple[str, ...]) -> bool:
    """--exclude-path semantics: rel equals the glob, fnmatches it, or lies below it."""
    from fnmatch import fnmatch

    return any(rel == g or fnmatch(rel, g) or rel.startswith(g.rstrip("/") + "/")
               for g in exclude if g)


def is_header(rel: str) -> bool:
    return _suffix(rel) in HEADER_EXTS


def _strip_cplusplus_blocks(text: str) -> str:
    """drop `#ifdef __cplusplus ... #endif` regions (the extern "C" guards of C headers)."""
    out: list[str] = []
    depth = 0          # nesting inside a __cplusplus region (0 = outside)
    for line in text.splitlines():
        if depth:
            if _PP_IF_RE.match(line):
                depth += 1
            elif _PP_ENDIF_RE.match(line):
                depth -= 1
            elif depth == 1 and _PP_ELSE_RE.match(line):
                depth = 0
            continue
        if _CPLUSPLUS_IF_RE.match(line):
            depth = 1
            continue
        out.append(line)
    return "\n".join(out)


def sniff_cpp(text: str) -> bool:
    """whether header text uses C++ syntax outside its extern "C" guards."""
    from codocs.lang.treesitter import mask_c_like

    body = _strip_cplusplus_blocks(mask_c_like(text))
    return any(r.search(body) for r in _CPP_SNIFF)


def header_language(rel: str, siblings: list[str] | None = None,
                    text: str | None = None, *, flags: tuple[bool, bool] | None = None) -> str:
    """"c" or "cpp" for a header; siblings = basenames in its dir.

    flags = _dir_flags(siblings), when the caller already has it.
    """
    suf = _suffix(rel)
    if suf in CPP_HEADER_EXTS:
        return "cpp"
    has_c, has_cpp = flags if flags is not None else _dir_flags(siblings or [])
    if has_cpp and not has_c:
        return "cpp"
    if has_c:
        return "c"
    return "cpp" if text and sniff_cpp(text) else "c"


def _read_text(path: Path) -> str:
    try:
        from codocs.lang.treesitter import decode

        return decode(path.read_bytes())[0]
    except OSError:
        return ""


def file_language(root: Path, rel: str, siblings: list[str] | None = None, *,
                  flags: tuple[bool, bool] | None = None) -> str | None:
    """language owning a file (headers by header_language), None for files of no language."""
    lang = source_language(rel)
    if lang is not None:
        return lang
    if is_cobol_member(rel):
        return "cobol"
    if not is_header(rel):
        return None
    if flags is None:
        if siblings is None:
            d = (Path(root) / rel).parent
            try:
                siblings = [p.name for p in d.iterdir() if p.is_file()]
            except OSError:
                siblings = []
        flags = _dir_flags(siblings)
    text = None
    if _suffix(rel) == ".h" and not any(flags):
        text = _read_text(Path(root) / rel)
    return header_language(rel, None, text, flags=flags)


def is_test_name(lang: str, rel: str) -> bool:
    """static test-file rule used by detection counts (adapters own the full rule)."""
    p = PurePosixPath(rel)
    name = p.name
    if lang == "go":
        return name.endswith("_test.go")
    if lang == "python":
        return name == "conftest.py" or any(fnmatchcase(name, g) for g in PY_TEST_PATTERNS)
    if lang == "java":
        return "/src/test/" in "/" + rel
    return False


# -- walking --------------------------------------------------------------------


def _resolved(p: Path | None) -> Path | None:
    if p is None:
        return None
    try:
        return Path(p).expanduser().resolve()
    except OSError:
        return None


def is_build_output(path: Path, beside_build: bool) -> bool:
    """a build tree: CMakeFiles, *.egg-info, cmake-build-*, bazel-*, or `build`/`dist` next to
    a build file (pyproject.toml, setup.py, CMakeLists.txt, build.gradle, ...) that is not a
    Python package itself (no __init__.py)."""
    name = path.name
    if any(fnmatchcase(name, g) for g in BUILD_OUTPUT_GLOBS):
        return True
    return name in BUILD_OUTPUT_DIRS and beside_build and not (path / "__init__.py").is_file()


def walk_repo(root: Path, subtree: str | None = None, *, out_dir: Path | None = None
              ) -> Iterator[tuple[str, list[str]]]:
    """(dir rel, sorted file names) of every scanned directory, with the detection skip rules.

    skipped: dot-dirs, vendor, node_modules, testdata, markdown_docs, __pycache__,
    site-packages, any dir holding pyvenv.cfg, `target` next to a pom.xml, OUT, and build
    output (is_build_output).
    """
    root = Path(root).resolve()
    sub = (subtree or "").strip().strip("/")
    while sub.startswith("./"):
        sub = sub[2:]
    start = root / sub if sub and sub != "." else root
    out = _resolved(out_dir)
    if not start.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(start):
        d = Path(dirpath)
        if d != root and ("pyvenv.cfg" in filenames or "CMakeCache.txt" in filenames):
            dirnames[:] = []
            continue
        has_pom = "pom.xml" in filenames
        beside_build = not BUILD_FILES.isdisjoint(filenames)
        keep = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            if name == "target" and has_pom:
                continue
            if is_build_output(d / name, beside_build):
                continue
            if out is not None and _resolved(d / name) == out:
                continue
            keep.append(name)
        dirnames[:] = keep
        rel = d.relative_to(root).as_posix()
        yield ("" if rel == "." else rel), sorted(filenames)


def _join(d: str, name: str) -> str:
    return f"{d}/{name}" if d else name


def scan_files(root: Path, subtree: str | None = None, *, out_dir: Path | None = None,
               headers: bool = True, exclude: tuple[str, ...] = ()
               ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(lang -> owned rels incl. headers and tests, dir rel -> file basenames) of one walk.

    headers=False leaves headers (and COBOL members) out: they never enable a language.
    exclude drops matching rels only for files assigned to cobol (COBOL scope option).
    """
    files: dict[str, list[str]] = {}
    dirs: dict[str, list[str]] = {}
    root = Path(root).resolve()
    for d, names in walk_repo(root, subtree, out_dir=out_dir):
        dirs[d] = names
        flags = None
        for name in names:
            rel = _join(d, name)
            lang = source_language(name)
            if lang is None and headers and is_header(name):
                if flags is None:
                    flags = _dir_flags(names)
                lang = file_language(root, rel, flags=flags)
            elif lang is None and headers and is_cobol_member(name):
                lang = "cobol"
            if lang == "cobol" and exclude and _cobol_excluded(rel, exclude):
                continue
            if lang is not None:
                files.setdefault(lang, []).append(rel)
    return {k: sorted(v) for k, v in files.items()}, dirs


# -- detection ------------------------------------------------------------------


@dataclass
class Detection:
    languages: list[str]
    counts: dict[str, int] = field(default_factory=dict)
    found: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    legacy: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def parse_requested(requested: str | None) -> list[str] | None:
    """None for auto, ALL_LANGS for all, else the validated languages in ALL_LANGS order."""
    req = (requested or "auto").strip().lower()
    if req in ("", "auto"):
        return None
    if req == "all":
        return list(ALL_LANGS)
    names = []
    for part in req.split(","):
        part = part.strip()
        if not part:
            continue
        name = ALIASES.get(part, part)
        if name not in ALL_LANGS:
            raise ValueError(f"--source-lang: unknown language {part!r} "
                             f"(choose auto, all or a list of {', '.join(ALL_LANGS)})")
        names.append(name)
    if not names:
        return None
    return [lang for lang in ALL_LANGS if lang in names]


def _count_sources(files: dict[str, list[str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lang, rels in files.items():
        n = sum(1 for r in rels if not is_header(r) and not is_test_name(lang, r))
        if n:
            out[lang] = n
    return out


def _also_found(found: dict[str, int], selected: list[str]) -> str:
    rest = [f"{lang} {found[lang]}" for lang in ALL_LANGS
            if lang in found and lang not in selected]
    if not rest:
        return ""
    return (f"also found: {', '.join(rest)} source files (not processed; pass --source-lang all "
            "or e.g. go,c to include them)")


def _has_go_source(root: Path, *, out_dir: Path | None) -> bool:
    """whether the repo holds at least one non-test .go file (stops at the first)."""
    for d, names in walk_repo(root, None, out_dir=out_dir):
        if any(n.endswith(".go") and not n.endswith("_test.go") for n in names):
            return True
    return False


def _sniff_cobol(root: Path, sub: str | None, out_dir: Path | None,
                 exclude: tuple[str, ...]) -> int:
    """COBOL programs among files of no known extension (PDS exports), by content sniffing."""
    from codocs.cobol.index import CobolRepoIndex

    probe = CobolRepoIndex(root, root.name)
    n = 0
    for d, names in walk_repo(root, sub, out_dir=out_dir):
        for name in names:
            if name.startswith(".") or source_language(name) is not None or is_header(name) \
                    or is_cobol_member(name):
                continue
            rel = _join(d, name)
            if exclude and _cobol_excluded(rel, exclude):
                continue
            try:
                if probe._classify(root / rel, rel) == "program":
                    n += 1
            except OSError:
                continue
    return n


def detect_languages(root: str | Path, subtree: str | None = None, requested: str = "auto", *,
                     out_dir: str | Path | None, exclude: tuple[str, ...] = ()) -> Detection:
    """languages to process under root/subtree.

    exclude: COBOL-scope globs (dropped only from the COBOL counts).
    """
    root = Path(root).expanduser().resolve()
    sub = (subtree or "").strip().strip("/")
    where = root / sub if sub else root
    has_mod = (root / "go.mod").is_file()
    files, _ = scan_files(root, sub or None, out_dir=Path(out_dir) if out_dir else None,
                          headers=False, exclude=tuple(exclude))
    counted = _count_sources(files)
    notes: list[str] = []
    if "go" in counted and not has_mod:
        counted.pop("go")
        notes.append("Go files ignored: no go.mod at the repository root")
    found = {lang: counted[lang] for lang in ALL_LANGS if lang in counted}
    wanted = parse_requested(requested)
    explicit = (requested or "").strip().lower() not in ("", "auto", "all")
    if explicit and wanted and "cobol" in wanted and "cobol" not in found:
        n = _sniff_cobol(root, sub or None, Path(out_dir) if out_dir else None, tuple(exclude))
        if n:
            found["cobol"] = n
    explicit_all = (requested or "").strip().lower() == "all"
    # a Go module repo is decided by the whole repo, a subtree only narrows it
    go_repo = "go" in found or (has_mod and bool(sub) and wanted is None and _has_go_source(
        root, out_dir=Path(out_dir) if out_dir else None))

    if wanted is None or explicit_all:
        if go_repo and (wanted is None):
            langs = ["go"]
            also = _also_found(found, langs)
            if also:
                notes.append(also)
        elif found:
            langs = list(found)
            if has_mod and "go" not in found:
                notes.append("go.mod found but no Go packages")
        elif has_mod:
            if sub:
                # the Go-only pipeline reports an empty selection itself
                return Detection(languages=["go"], counts={}, found={}, notes=notes, legacy=True)
            raise ValueError(f"no Go packages found under {where}")
        else:
            raise ValueError(f"{root} {NO_FILES}")
    else:
        langs = []
        for lang in wanted:
            if lang == "go":
                if not has_mod:
                    raise ValueError(f"{root} is not a Go module (no go.mod)")
                if "go" not in found and not sub:
                    raise ValueError(f"no Go packages found under {where}")
                langs.append("go")
                continue
            if lang not in found:
                # COBOL keeps the "unknown language" wording its CLI tests match
                pre = "unknown language for this repository: " if lang == "cobol" else ""
                raise ValueError(f"--source-lang {lang}: {pre}no {DISPLAY[lang]} files under "
                                 f"{where}")
            langs.append(lang)
    counts = {lang: found.get(lang, 0) for lang in langs}
    return Detection(languages=langs, counts=counts, found=found, notes=notes,
                     legacy=langs == ["go"])


def resolve_languages(repo: str | Path, settings: Any, subtree: str | None = None, *,
                      out_dir: str | Path | None = None) -> Detection:
    """detect_languages with settings.source_lang (default auto) and its COBOL excludes."""
    from codocs.lang.base import _exclude_paths

    return detect_languages(repo, subtree, getattr(settings, "source_lang", "auto") or "auto",
                            out_dir=out_dir, exclude=_exclude_paths(settings))


# -- adapters -------------------------------------------------------------------


ADAPTERS: dict[str, tuple[str, str]] = {
    "go": ("codocs.lang.go", "GO"),
    "python": ("codocs.lang.python", "PythonAdapter"),
    "java": ("codocs.lang.java", "JavaAdapter"),
    "c": ("codocs.lang.cc", "CAdapter"),
    "cpp": ("codocs.lang.cc", "CppAdapter"),
    "cobol": ("codocs.lang.cobol", "CobolAdapter"),
}
_UNBOUND: dict[str, Any] = {}


def _factory(name: str):
    if name not in ADAPTERS:
        raise ValueError(f"unknown language {name!r}")
    module, attr = ADAPTERS[name]
    return getattr(importlib.import_module(module), attr)


def adapter(name: str, ctx: Any = None) -> Any:
    """the adapter for a language: Go's context-free singleton, else one bound to ctx.

    a bound adapter is created once per (language, ctx) and cached in ctx.cache; without a
    ctx an unbound adapter is returned (enough for parsing members such as decls).
    """
    name = ALIASES.get(name, name)
    if name == "go":
        from codocs.lang.go import GO

        return GO
    if ctx is None:
        hit = _UNBOUND.get(name)
        if hit is None:
            hit = _UNBOUND[name] = _factory(name)(None)
        return hit
    key = ("adapter", name)
    hit = ctx.cache.get(key)
    if hit is None:
        hit = ctx.cache[key] = _factory(name)(ctx)
    return hit


def adapter_for(runner: Any) -> Any:
    """the adapter a runner carries, else the one of its language (GoRunner: Go)."""
    return getattr(runner, "adapter", None) or adapter(getattr(runner, "lang", "go"))
