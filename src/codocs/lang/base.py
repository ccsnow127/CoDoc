"""language-adapter contract: shared dataclasses, protocols and the non-Go adapter base.

Go keeps its own types (Decl, Target, PackageInfo, GoTestResult, Mutant); they are re-exported
here so every language speaks the same vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from codocs.golang.gotool import CoverBlock, GoTestResult, file_coverage
from codocs.golang.mutate import Mutant, apply, sample
from codocs.golang.package import PackageInfo, Target
from codocs.golang.source import Decl, Import

if TYPE_CHECKING:
    from codocs.config import Settings

__all__ = [
    "LANG_ORDER", "ALL_LANGS", "NO_TS_LANGS", "DISPLAY", "Decl", "Import", "Target", "PackageInfo", "TestResult",
    "GoTestResult", "CoverBlock", "file_coverage", "Mutant", "apply", "sample", "LangTarget",
    "lang_of", "Unit", "ImportSpec", "LangTestResult", "PromptSpec", "prompt_spec",
    "parse_fragments", "omit_defaults", "RepoContext", "StaticContext", "WikiFileFacts",
    "FuncFact", "CallFact", "TypeFact", "GraphFileFacts", "GraphPart", "LanguageAdapter",
    "LangRunner", "LangAdapterBase", "PROCESSED", "TARGET_STATUSES",
]

LANG_ORDER = ("go", "c", "cpp", "java", "python")        # cobol is only in ALL_LANGS
ALL_LANGS = LANG_ORDER + ("cobol",)                     # validation and iteration
DISPLAY = {"go": "Go", "python": "Python", "java": "Java", "c": "C", "cpp": "C++",
           "cobol": "COBOL"}
NO_TS_LANGS = frozenset({"go", "cobol"})                # never need tree-sitter
PROCESSED = ("ok", "no-funcs")
TARGET_STATUSES = ("ok", "no-funcs", "generated", "too-large", "no-decls", "header",
                   "test-support", "unsupported-build", "no-toolchain", "name-clash",
                   "copybook", "duplicate-id")
MESSAGE_CHARS = 2000

TestResult = GoTestResult


def lang_of(obj: Any) -> str:
    """language of a target, unit, runner or result ("go" when it carries none)."""
    return getattr(obj, "lang", "go") or "go"


def omit_defaults(d: dict, defaults: dict) -> dict:
    """d without the keys whose value equals the given default (keeps Go output minimal)."""
    return {k: v for k, v in d.items() if not (k in defaults and v == defaults[k])}


# -- targets, units, imports ------------------------------------------------------------


@dataclass
class LangTarget(Target):
    """a non-Go target (Go keeps plain Target); to_dict() includes "lang"."""

    lang: str = "python"


@dataclass
class Unit:
    """a non-Go unit (Go keeps PackageInfo): python package dir, java package, c/c++ dir."""

    lang: str
    rel: str
    name: str
    import_path: str = ""
    files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    build_root: str = ""
    error: str = ""
    extra: dict = field(default_factory=dict)

    # duck-compat with PackageInfo (read by shared code)
    @property
    def all_test_files(self) -> list[str]:
        return self.test_files

    @property
    def dir_name(self) -> str:
        return self.rel or "_root"

    @property
    def go_files(self) -> list[str]:
        return self.files

    @property
    def xtest_files(self) -> list[str]:
        return []

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Unit:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class ImportSpec:
    """a non-Go import (Go keeps golang.source.Import); opaque to the loops."""

    module: str
    name: str = ""
    alias: str | None = None
    line: int = 0
    text: str = ""
    kind: str = ""        # python import|from; java single|wildcard|static|simple; c quote|angle

    @property
    def bound(self) -> str:
        """identifier the import binds: python name, java simple class name, c/c++ ""."""
        if self.kind in ("quote", "angle", "wildcard"):
            return ""
        if self.alias:
            return self.alias
        if self.kind == "from":
            return self.name
        if self.kind == "import":
            return self.module.split(".", 1)[0]
        if self.kind == "static" and self.name:
            return self.name
        if self.kind in ("single", "static", "simple"):
            return self.module.rsplit(".", 1)[-1]
        return self.name or ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ImportSpec:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class LangTestResult(GoTestResult):
    """result of every non-Go runner; coverage_error explains coverage=None under cover=True."""

    coverage_error: str = ""

    def to_dict(self) -> dict:
        d = super().to_dict()
        if self.coverage_error:
            d["coverage_error"] = self.coverage_error
        return d

    @classmethod
    def from_dict(cls, d: dict) -> LangTestResult:
        base = GoTestResult.from_dict(d)
        return cls(**{k: getattr(base, k) for k in GoTestResult.__dataclass_fields__},
                   coverage_error=d.get("coverage_error", ""))


# -- prompts --------------------------------------------------------------------------


PROMPTS_LANG_DIR = Path(__file__).resolve().parent.parent / "prompts" / "lang"
FRAGMENT_SLOTS = ("LANG", "FENCE", "FILE_NOUN", "GENERATOR_INPUTS", "DOC_HEADER",
                  "SECTION_KINDS", "TYPE_RULE", "VALUE_RULE", "FUNC_RULE", "CONTEXT_RULE",
                  "CODEGEN_RULES", "TEST_INTRO", "TEST_RULES", "TEST_SHAPE", "MARK")
_SLOT_RE = re.compile(r"^=== ([A-Z][A-Z0-9_]*) ===[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class PromptSpec:
    """per-language prompt facts."""

    lang: str
    display: str
    fence: str
    comment: str
    file_noun: str
    header_label: str
    code_helper_prefix: str
    test_helper_prefix: str
    fragments: dict[str, str] = field(default_factory=dict, compare=False, hash=False)


_PROMPT_FACTS = {
    #          fence     comment file_noun            header     code     test
    "go": ("go", "//", "Go file", "Package", "cdg", "cdx"),
    "python": ("python", "#", "Python module", "Module", "_cdg", "_cdx"),
    "java": ("java", "//", "Java source file", "Package", "cdg", "cdx"),
    "c": ("c", "//", "C source file", "Unit", "cdg_", "cdx_"),
    "cpp": ("cpp", "//", "C++ source file", "Unit", "cdg_", "cdx_"),
    "cobol": ("cobol", "*>", "COBOL source file", "Program", "CDG-", "TD-"),
}


def parse_fragments(text: str) -> dict[str, str]:
    """slot -> text of a prompts/lang/<lang>.txt file ("=== SLOT ===" sections)."""
    out: dict[str, str] = {}
    marks = list(_SLOT_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
        out[m.group(1)] = body
    return out


@lru_cache(maxsize=None)
def prompt_spec(lang: str) -> PromptSpec:
    """PromptSpec of a language; non-Go fragments come from prompts/lang/<lang>.txt."""
    fence, comment, noun, label, cdg, cdx = _PROMPT_FACTS[lang]
    fragments: dict[str, str] = {}
    if lang != "go":
        path = PROMPTS_LANG_DIR / f"{lang}.txt"
        try:
            fragments = parse_fragments(path.read_text(encoding="utf-8"))
        except OSError:
            fragments = {}
    return PromptSpec(lang=lang, display=DISPLAY[lang], fence=fence, comment=comment,
                      file_noun=noun, header_label=label, code_helper_prefix=cdg,
                      test_helper_prefix=cdx, fragments=fragments)


# -- static repo context --------------------------------------------------------------


@dataclass(eq=False)
class RepoContext:
    """per-run static context non-Go adapters are bound to.

    `files` and `dirs` come from one walk with the detection skip rules (OUT excluded) over
    the whole repo; every other index is filled lazily by the adapters that need it.
    """

    root: Path
    settings: Any = None
    out_dir: Path | None = None
    languages: list[str] = field(default_factory=list)
    files: dict[str, list[str]] = field(default_factory=dict)
    dirs: dict[str, list[str]] = field(default_factory=dict)
    codecs: dict[str, str] = field(default_factory=dict)
    header_index: dict[str, list[str]] = field(default_factory=dict)
    symbol_index: dict[str, list[str]] = field(default_factory=dict)
    py_projects: dict[str, dict] = field(default_factory=dict)
    py_modules: dict[str, str] = field(default_factory=dict)
    java_modules: dict[str, dict] = field(default_factory=dict)
    java_classes: dict[str, str] = field(default_factory=dict)
    cgo_flags: dict[str, dict] = field(default_factory=dict)
    cache: dict = field(default_factory=dict)

    @classmethod
    def build(cls, root: str | Path, languages: list[str] | tuple[str, ...],
              settings: Settings | None = None, out_dir: str | Path | None = None
              ) -> RepoContext:
        """scan the repo once: owned files of the selected languages, dir listings, headers."""
        from codocs.lang.registry import is_header, scan_files

        root = Path(root).expanduser().resolve()
        out = Path(out_dir).resolve() if out_dir is not None else None
        langs = [lang for lang in ALL_LANGS if lang in set(languages)]
        exclude = _exclude_paths(settings)
        files, dirs = scan_files(root, None, out_dir=out, exclude=exclude)
        ctx = cls(root=root, settings=settings, out_dir=out, languages=langs,
                  files={lang: files.get(lang, []) for lang in langs}, dirs=dirs)
        for lang in ("c", "cpp"):
            for rel in ctx.files.get(lang, []):
                if is_header(rel):
                    ctx.header_index.setdefault(PurePosixPath(rel).name, []).append(rel)
        return ctx

    def setting(self, name: str, default: Any = None) -> Any:
        return getattr(self.settings, name, default) if self.settings is not None else default

    @property
    def include_generated(self) -> bool:
        return bool(self.setting("include_generated", False))

    def read(self, rel: str) -> str:
        """decoded text of a repo file (codec remembered in self.codecs); "" when unreadable."""
        from codocs.lang.treesitter import decode

        try:
            data = (self.root / rel).read_bytes()
        except OSError:
            return ""
        text, codec = decode(data)
        self.codecs[rel] = codec
        return text

    def codec(self, rel: str) -> str:
        if rel not in self.codecs:
            self.read(rel)
        return self.codecs.get(rel, "utf-8")

    def lang_files(self, lang: str) -> list[str]:
        return self.files.get(lang, [])

    def mark_no_toolchain(self, lang: str, reason: str) -> None:
        """the language's toolchain is unusable this run: its targets get status no-toolchain."""
        self.cache[("no-toolchain", lang)] = reason or "toolchain unavailable"

    def no_toolchain(self, lang: str) -> str:
        """reason the language has no toolchain this run ("" = usable)."""
        return self.cache.get(("no-toolchain", lang), "")


StaticContext = RepoContext


def _exclude_paths(settings: Any) -> tuple[str, ...]:
    """settings.exclude_paths as globs (COBOL scope); () without settings."""
    if settings is None:
        return ()
    fn = getattr(settings, "exclude_list", None)
    if fn is not None:
        try:
            return tuple(fn())
        except Exception:
            return ()
    raw = getattr(settings, "exclude_paths", "") or ""
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\n]", raw)
    return tuple(x.strip() for x in items if x and x.strip())


# -- facts for wiki and bizlogic ------------------------------------------------------------


@dataclass
class WikiFileFacts:
    decls: list[Decl] = field(default_factory=list)
    imports: list[ImportSpec] = field(default_factory=list)
    is_main: bool = False
    doc_comment: str = ""
    loc: int = 0


@dataclass
class FuncFact:
    name: str                 # "f", "Cls.m", static C: "f@file.c"
    kind: str = "func"        # func | method
    line: int = 0
    end_line: int = 0
    static: bool = False
    cls: str = ""
    start: int = 0            # char offsets of the definition (body included)
    end: int = 0


@dataclass
class CallFact:
    kind: str                 # call | ref
    qual: str                 # "" | self | cls | this | super | <module alias> | <Class> | <var> | C
    name: str
    recv_type: str | None = None
    line: int = 0


@dataclass
class TypeFact:
    bases: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    methods: list[str] = field(default_factory=list)
    is_interface: bool = False
    line: int = 0


@dataclass
class GraphFileFacts:
    funcs: list[FuncFact] = field(default_factory=list)
    calls: dict[str, list[CallFact]] = field(default_factory=dict)
    types: dict[str, TypeFact] = field(default_factory=dict)
    imports: list[ImportSpec] = field(default_factory=list)
    tables: dict[str, list[str]] = field(default_factory=dict)
    is_test: bool = False


@dataclass
class GraphPart:
    """one language's share of the bizlogic graph, before the languages are combined.

    nodes/edges keep the bizlogic graph module's own types; spans maps node id ->
    (file rel, start, end) char offsets of the function (used by cross-language linkers).
    """

    lang: str
    nodes: dict[str, Any] = field(default_factory=dict)
    edges: dict[tuple[str, str], Any] = field(default_factory=dict)
    spans: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    test_refs: set[str] = field(default_factory=set)
    var_refd: set[str] = field(default_factory=set)
    facts: dict[str, GraphFileFacts] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


# -- protocols --------------------------------------------------------------------------


class LanguageAdapter(Protocol):
    """static half of a language; Go: GoAdapter, others: LangAdapterBase."""

    name: str
    display: str
    prompts: PromptSpec
    ctx: RepoContext | None

    def owns(self, rel: str) -> bool: ...
    def is_test_file(self, rel: str, src: str | None = None) -> bool: ...
    def is_generated(self, src: str, rel: str = "") -> bool: ...
    def decls(self, src: str) -> list[Decl]: ...
    def imports(self, src: str) -> list: ...
    def identifiers(self, src: str) -> set[str]: ...
    def extract_code(self, reply: str) -> str: ...
    def preamble(self, src: str) -> str: ...
    def apply_preamble(self, code: str, preamble: str) -> str: ...
    def test_names(self, src: str, rel: str) -> list[str]: ...
    def test_lines(self, src: str, rel: str) -> dict[str, int]: ...
    def remove_tests(self, src: str, names: set[str]) -> str: ...
    def strip_non_tests(self, src: str) -> str: ...
    def normalize_suite(self, src: str, unit: Any, target_rel: str) -> str: ...
    def mutation_sites(self, src: str) -> list[Mutant]: ...
    async def discover(self, runner: Any, subtree: str | None = None,
                       packages: list[str] | None = None) -> dict[str, Any]: ...
    def select_targets(self, root: Path, units: dict[str, Any], *, files: str | None = None,
                       max_files: int | None = None,
                       max_file_lines: int = 1500) -> list[Target]: ...
    def stem(self, rel: str) -> str: ...
    def package_context(self, root: Path, unit: Any, target_rel: str,
                        max_chars: int = 12000) -> str: ...
    def package_identifiers(self, root: Path, unit: Any,
                            exclude_rel: str | None = None) -> set[str]: ...
    def test_reference(self, root: Path, unit: Any, target_rel: str,
                       max_chars: int = 8000) -> str: ...
    def allowed_third_party(self, root: Path, unit: Any) -> list[str]: ...
    def test_index(self, root: Path, unit: Any) -> dict[str, dict]: ...
    def declared_tests(self, root: Path, unit: Any) -> set[str]: ...
    def relevant_tests(self, root: Path, unit: Any, target_rel: str,
                       candidates: set[str]) -> list[str]: ...
    def suite_rel(self, unit: Any, stem: str) -> str: ...
    def suite_basename(self, stem: str) -> str: ...
    def fix_imports(self, src: str, build_errors: list[str], file_basename: str,
                    allowed: list) -> str | None: ...
    def fix_allowed(self, source_imports: list) -> list: ...
    def default_allowed(self, runner: Any, unit: Any) -> list: ...
    def failure_message(self, output: str) -> str: ...
    def crash_message(self, res: TestResult) -> str: ...
    def doc_seed_messages(self, target_rel: str, unit: Any, source: str,
                          context: str) -> tuple[str, str]: ...
    def doc_codegen_messages(self, target_rel: str, unit: Any, doc: str, context: str,
                             allowed: list, *, taken: set[str] | None = None,
                             has_preamble: bool = False) -> tuple[str, str]: ...
    def doc_refine_messages(self, target_rel: str, unit: Any, source: str, doc: str, code: str,
                            evaluation: Any, missing: list[str], *, last: Any = None,
                            last_round: int = 0, attempt: int = 0) -> tuple[str, str]: ...
    def test_seed_messages(self, target_rel: str, unit: Any, source: str, context: str,
                           taken: set[str], third_party: list[str],
                           reference: str = "") -> tuple[str, str]: ...
    def test_refine_messages(self, target_rel: str, unit: Any, source: str, context: str,
                             taken: set[str], third_party: list[str], best: Any, last: Any, *,
                             attempt: int = 0, last_round: int = 0,
                             reference: str = "") -> tuple[str, str]: ...
    def resolve_imports(self, rel: str, imports: list, ctx: RepoContext) -> list[str]: ...
    def resolve_include(self, rel: str, header: str, ctx: RepoContext) -> str | None: ...
    def baseline_path(self, out_dir: Path, unit: Any) -> Path: ...
    def make_runner(self, ctx: Any) -> Any: ...
    def wiki_facts(self, root: Path, rel: str, src: str, ctx: RepoContext) -> WikiFileFacts: ...
    def graph_facts(self, root: Path, rel: str, src: str,
                    ctx: RepoContext) -> GraphFileFacts: ...


class LangRunner(Protocol):
    """execution half of a language; GoRunner satisfies it.

    GoRunner has no `lang`, `adapter` or `list_tests`: read them with getattr.
    """

    module_root: Path
    exec_root: Path
    module: str
    timeout: int
    workers: int

    def available(self) -> bool: ...
    def version(self) -> str: ...
    async def test(self, unit_rel: str, *, overlay: dict[str, str | None] | None = None,
                   run: list[str] | None = None, cover: bool = False, failfast: bool = False,
                   compile_only: bool = False, timeout: int | None = None) -> TestResult: ...


# -- the non-Go adapter base ------------------------------------------------------------


def _norm(rel: str | None) -> str:
    rel = (rel or "").strip().strip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    return "" if rel == "." else rel


class LangAdapterBase:
    """shared behaviour of the non-Go adapters, bound to a RepoContext (or None: parsing only).

    subclasses set `name` and implement the parsing members (decls, imports, test_names,
    test_lines, remove_tests, mutation_sites, is_test_file, discover, suite_rel, make_runner,
    graph_facts); everything else has a generic default they may override.

    prompt builders delegate to `codocs.specforge.prompts_lang` / `codocs.tesearch.prompts_lang`
    (imported lazily), passing the adapter first:
      specforge.prompts_lang.seed_messages(adapter, target_rel, unit, source, context)
      specforge.prompts_lang.codegen_messages(adapter, target_rel, unit, doc, context, allowed,
                                              taken=, has_preamble=)
      specforge.prompts_lang.refine_messages(adapter, target_rel, unit, source, doc, code,
                                             evaluation, missing, last=, last_round=, attempt=)
      tesearch.prompts_lang.seed_messages(adapter, target_rel, unit, source, context, taken,
                                          third_party, reference)
      tesearch.prompts_lang.refine_messages(adapter, target_rel, unit, source, context, taken,
                                            third_party, best, last, attempt=, last_round=,
                                            reference=)
    """

    name: str = ""
    test_file_patterns: tuple[str, ...] = ()

    def __init__(self, ctx: RepoContext | None = None):
        self.ctx = ctx

    # -- identity ----------------------------------------------------------------

    @property
    def display(self) -> str:
        return DISPLAY[self.name]

    @property
    def prompts(self) -> PromptSpec:
        return prompt_spec(self.name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} bound={self.ctx is not None}>"

    # -- files --------------------------------------------------------------------

    def read(self, rel: str, root: Path | None = None) -> str:
        """decoded text of a repo file, through the context when bound."""
        if self.ctx is not None and (root is None or Path(root).resolve() == self.ctx.root):
            return self.ctx.read(rel)
        from codocs.lang.treesitter import decode

        base = Path(root) if root is not None else Path(".")
        try:
            return decode((base / rel).read_bytes())[0]
        except OSError:
            return ""

    def owns(self, rel: str) -> bool:
        from codocs.lang.registry import file_language

        siblings = None
        if self.ctx is not None:
            siblings = self.ctx.dirs.get(_norm(str(PurePosixPath(rel).parent)))
        root = self.ctx.root if self.ctx is not None else Path(".")
        return file_language(root, rel, siblings) == self.name

    def is_test_file(self, rel: str, src: str | None = None) -> bool:
        name = PurePosixPath(rel).name
        return any(fnmatchcase(name, g) for g in self.test_file_patterns)

    def is_generated(self, src: str, rel: str = "") -> bool:
        return False

    @property
    def include_generated(self) -> bool:
        return self.ctx.include_generated if self.ctx is not None else False

    # -- parsing (subclasses) ---------------------------------------------------------

    def decls(self, src: str) -> list[Decl]:
        raise NotImplementedError

    def imports(self, src: str) -> list:
        raise NotImplementedError

    def identifiers(self, src: str) -> set[str]:
        from codocs.lang.treesitter import identifiers

        return identifiers(src, self.name)

    def extract_code(self, reply: str) -> str:
        from codocs.lang.treesitter import FENCE_TAGS, extract_code

        return extract_code(reply, FENCE_TAGS.get(self.name, (self.prompts.fence,)))

    def preamble(self, src: str) -> str:
        return ""

    def apply_preamble(self, code: str, preamble: str) -> str:
        return code

    def format_import(self, imp: Any) -> str:
        """one allowed import in the language's syntax (for prompts)."""
        text = getattr(imp, "text", "")
        return text or getattr(imp, "module", str(imp))

    # -- tests in source --------------------------------------------------------------

    def test_names(self, src: str, rel: str) -> list[str]:
        raise NotImplementedError

    def test_lines(self, src: str, rel: str) -> dict[str, int]:
        raise NotImplementedError

    def remove_tests(self, src: str, names: set[str]) -> str:
        raise NotImplementedError

    def strip_non_tests(self, src: str) -> str:
        return src

    def normalize_suite(self, src: str, unit: Any, target_rel: str) -> str:
        return src

    def mutation_sites(self, src: str) -> list[Mutant]:
        raise NotImplementedError

    # -- units and targets ----------------------------------------------------------

    async def discover(self, runner: Any, subtree: str | None = None,
                       packages: list[str] | None = None) -> dict[str, Unit]:
        raise NotImplementedError

    def file_status(self, rel: str, unit: Any) -> str | None:
        """a status decided before reading the file (header, test-support, ...); None = read it."""
        return None

    def target_status(self, src: str, rel: str, max_file_lines: int) -> tuple[int, str]:
        """(lines, status) of a target's source, by the Go rules generalised."""
        from codocs.lang.treesitter import line_count

        lines = line_count(src)
        if not self.include_generated and self.is_generated(src, rel):
            return lines, "generated"
        if lines > max_file_lines:
            return lines, "too-large"
        decls = self.decls(src)
        if not decls:
            return lines, "no-decls"
        if not any(d.kind in ("func", "method") for d in decls):
            return lines, "no-funcs"
        return lines, "ok"

    def select_targets(self, root: Path, units: dict[str, Any], *, files: str | None = None,
                       max_files: int | None = None, max_file_lines: int = 1500
                       ) -> list[Target]:
        """every target file of the units with its status; same max_files rule as Go."""
        out: list[Target] = []
        kept = 0
        blocked = self.ctx.no_toolchain(self.name) if self.ctx is not None else ""
        pairs = sorted((u.rel, f, u) for u in units.values() for f in u.files)
        for unit_rel, rel, unit in pairs:
            if files and not fnmatchcase(rel, files):
                continue
            status = self.file_status(rel, unit)
            if status is None:
                lines, status = self.target_status(self.read(rel, root), rel, max_file_lines)
            else:
                from codocs.lang.treesitter import line_count

                lines = line_count(self.read(rel, root))
            if status in PROCESSED and blocked:
                status = "no-toolchain"
            if status in PROCESSED:
                if max_files is not None and kept >= max_files:
                    continue
                kept += 1
            out.append(LangTarget(pkg=unit_rel, file=rel, stem=self.stem(rel), lines=lines,
                                  status=status, lang=self.name))
        return out

    def stem(self, rel: str) -> str:
        return PurePosixPath(rel).name

    # -- context for prompts ----------------------------------------------------------

    def _other_files(self, unit: Any, exclude_rel: str | None) -> list[str]:
        return [f for f in sorted(unit.files) if f != exclude_rel]

    def package_context(self, root: Path, unit: Any, target_rel: str,
                        max_chars: int = 12000) -> str:
        """signatures of the unit's other files, `<comment> <rel>` headed (generic default)."""
        c = self.prompts.comment
        blocks: list[str] = []
        size = 0
        for f in self._other_files(unit, target_rel):
            sigs = [d.signature for d in self.decls(self.read(f, root)) if d.signature]
            if not sigs:
                continue
            block = "\n".join([f"{c} {f}", *sigs])
            if size + len(block) > max_chars:
                blocks.append(f"{c} ... (truncated)")
                break
            blocks.append(block)
            size += len(block) + 2
        return "\n\n".join(blocks) + "\n" if blocks else ""

    def package_identifiers(self, root: Path, unit: Any,
                            exclude_rel: str | None = None) -> set[str]:
        names: set[str] = set()
        for f in self._other_files(unit, exclude_rel):
            for d in self.decls(self.read(f, root)):
                if d.kind != "method":
                    names.add(d.name)
        return names

    def test_reference(self, root: Path, unit: Any, target_rel: str,
                       max_chars: int = 8000) -> str:
        return ""

    def allowed_third_party(self, root: Path, unit: Any) -> list[str]:
        return []

    # -- tests of a unit --------------------------------------------------------------

    def test_index(self, root: Path, unit: Any) -> dict[str, dict]:
        """test name -> {"file", "idents", "selectors"} over the unit's test files."""
        out: dict[str, dict] = {}
        for f in unit.test_files:
            src = self.read(f, root)
            idents = sorted(self.identifiers(src)) if src else []
            for name in self.test_names(src, f):
                out[name] = {"file": f, "idents": idents, "selectors": []}
        return out

    def declared_tests(self, root: Path, unit: Any) -> set[str]:
        names: set[str] = set()
        for f in unit.test_files:
            src = self.read(f, root)
            if src:
                names.update(self.test_names(src, f))
        return names

    def relevant_tests(self, root: Path, unit: Any, target_rel: str,
                       candidates: set[str]) -> list[str]:
        """candidates whose test file uses a name the target declares (generic default)."""
        declared = {d.name for d in self.decls(self.read(target_rel, root))}
        out = [name for name, info in self.test_index(root, unit).items()
               if name in candidates and declared & set(info["idents"])]
        return sorted(out)

    # -- generated suite placement ----------------------------------------------------

    def suite_rel(self, unit: Any, stem: str) -> str:
        raise NotImplementedError

    def suite_basename(self, stem: str) -> str:
        raise NotImplementedError

    # -- repair and messages ----------------------------------------------------------

    def fix_imports(self, src: str, build_errors: list[str], file_basename: str,
                    allowed: list) -> str | None:
        return None

    # optional hooks the loops look up with getattr (GoAdapter has none); Python overrides them
    def runtime_import_errors(self, res: Any, rel: str) -> list[str]:
        """build-error-like lines for imports missing only at run time (fed to fix_imports)."""
        return []

    def unlisted_failures(self, res: Any, src: str, rel: str) -> list[str]:
        """failing test ids of the suite file that static test_names does not list."""
        return []

    def broken_mutant(self, mutated: str, res: Any) -> str:
        """verdict of a mutant whose run failed to build: "invalid" (or "killed")."""
        return "invalid"

    def fix_allowed(self, source_imports: list) -> list:
        return list(source_imports)

    def default_allowed(self, runner: Any, unit: Any) -> list:
        return []

    def failure_message(self, output: str) -> str:
        text = "\n".join(ln.rstrip() for ln in output.strip().splitlines() if ln.strip())
        return text if len(text) <= MESSAGE_CHARS else text[:MESSAGE_CHARS] + "\n..."

    def crash_message(self, res: TestResult) -> str:
        for ln in reversed((res.tail or "").splitlines()):
            if ln.strip():
                return f"did not run: the test process exited early ({ln.strip()[:200]})"
        return "did not run: the test process exited early"

    # -- prompt builders (specforge/prompts_lang.py, tesearch/prompts_lang.py) -----------------

    def doc_seed_messages(self, target_rel: str, unit: Any, source: str,
                          context: str) -> tuple[str, str]:
        from codocs.specforge import prompts_lang

        return prompts_lang.seed_messages(self, target_rel, unit, source, context)

    def doc_codegen_messages(self, target_rel: str, unit: Any, doc: str, context: str,
                             allowed: list, *, taken: set[str] | None = None,
                             has_preamble: bool = False) -> tuple[str, str]:
        from codocs.specforge import prompts_lang

        return prompts_lang.codegen_messages(self, target_rel, unit, doc, context, allowed,
                                             taken=taken, has_preamble=has_preamble)

    def doc_refine_messages(self, target_rel: str, unit: Any, source: str, doc: str, code: str,
                            evaluation: Any, missing: list[str], *, last: Any = None,
                            last_round: int = 0, attempt: int = 0) -> tuple[str, str]:
        from codocs.specforge import prompts_lang

        return prompts_lang.refine_messages(self, target_rel, unit, source, doc, code,
                                            evaluation, missing, last=last,
                                            last_round=last_round, attempt=attempt)

    def test_seed_messages(self, target_rel: str, unit: Any, source: str, context: str,
                           taken: set[str], third_party: list[str],
                           reference: str = "") -> tuple[str, str]:
        from codocs.tesearch import prompts_lang

        return prompts_lang.seed_messages(self, target_rel, unit, source, context, taken,
                                          third_party, reference)

    def test_refine_messages(self, target_rel: str, unit: Any, source: str, context: str,
                             taken: set[str], third_party: list[str], best: Any, last: Any, *,
                             attempt: int = 0, last_round: int = 0,
                             reference: str = "") -> tuple[str, str]:
        from codocs.tesearch import prompts_lang

        return prompts_lang.refine_messages(self, target_rel, unit, source, context, taken,
                                            third_party, best, last, attempt=attempt,
                                            last_round=last_round, reference=reference)

    # -- resolution -------------------------------------------------------------------

    def resolve_imports(self, rel: str, imports: list, ctx: RepoContext) -> list[str]:
        return []

    def resolve_include(self, rel: str, header: str, ctx: RepoContext) -> str | None:
        return None

    # -- persistence, runner ------------------------------------------------------------

    def baseline_path(self, out_dir: Path, unit: Any) -> Path:
        """OUT/baseline/<lang>/<dir_name with / -> __>.json."""
        return Path(out_dir) / "baseline" / self.name / (unit.dir_name.replace("/", "__")
                                                         + ".json")

    def make_runner(self, ctx: Any) -> Any:
        raise NotImplementedError

    # -- static facts -------------------------------------------------------------------

    def wiki_facts(self, root: Path, rel: str, src: str, ctx: RepoContext) -> WikiFileFacts:
        from codocs.lang.treesitter import line_count

        return WikiFileFacts(decls=self.decls(src), imports=list(self.imports(src)),
                             loc=line_count(src))

    def graph_facts(self, root: Path, rel: str, src: str, ctx: RepoContext) -> GraphFileFacts:
        raise NotImplementedError
