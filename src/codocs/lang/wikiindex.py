"""MultiLangIndex: repowiki's GoModuleIndex duck-typed over Go, Python, Java, C, C++, COBOL.

deepdoc (the third-party code in src/repowiki) only reads `root`, `module`,
`packages` (GoPackage records), `graph` and the query methods, so every unit directory of
every language becomes one GoPackage. the Go part is
`GoModuleIndex.build` itself; a directory holding Go and other files is one record, the Go
values extended with the other languages' files, LOC, symbols and imports.

COBOL is the exception: one package per top-level program,
keyed by its file rel (`<rel>#<PID>` for later programs of a file), from the run's COBOL
index (`cobol_index`, with `cobol_programs` key -> PID and `package_of_program`).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from codocs.lang.base import LANG_ORDER, RepoContext

try:
    from codocs.lang.base import ALL_LANGS
except ImportError:  # a base module without ALL_LANGS
    ALL_LANGS = LANG_ORDER + ("cobol",)
from codocs.repowiki.analysis.golang import GoModuleIndex
from codocs.repowiki.deepdoc.models import GoPackage, GoSymbol

log = logging.getLogger(__name__)

# codocs-side tech stack for non-Go imports: (match, display name, category); a match
# ending in "/" or "." is a prefix, else the exact module / header (or its first segment)
TECH_STACK_EXTRA: dict[str, list[tuple[str, str, str]]] = {
    "python": [
        ("django", "Django", "web framework"), ("flask", "Flask", "web framework"),
        ("fastapi", "FastAPI", "web framework"), ("requests", "requests", "HTTP client"),
        ("numpy", "NumPy", "numerics"), ("pandas", "pandas", "data analysis"),
        ("sqlalchemy", "SQLAlchemy", "database"), ("grpc", "gRPC (Python)", "RPC framework"),
        ("pydantic", "pydantic", "data validation"),
    ],
    "java": [
        ("org.springframework.", "Spring", "application framework"),
        ("io.grpc.", "gRPC (Java)", "RPC framework"),
        ("com.fasterxml.jackson.", "Jackson", "serialization"),
        ("org.apache.kafka.", "Kafka client", "messaging"),
        ("io.netty.", "Netty", "networking"),
    ],
    "c": [
        ("openssl/", "OpenSSL", "cryptography"), ("pthread.h", "pthreads", "threading"),
        ("sctp", "SCTP", "transport"), ("zlib.h", "zlib", "compression"),
        ("curl/", "libcurl", "HTTP client"),
    ],
}
TECH_STACK_EXTRA["cpp"] = TECH_STACK_EXTRA["c"]
_JAVA_STD = ("java.", "javax.", "jdk.", "sun.", "com.sun.")
_CPP_STD_HEADERS = frozenset(
    "algorithm any array atomic bitset cassert cctype cerrno cfloat chrono cinttypes climits "
    "cmath complex condition_variable cstdarg cstddef cstdint cstdio cstdlib cstring ctime "
    "deque exception filesystem fstream functional future initializer_list iomanip ios "
    "iosfwd iostream istream iterator limits list locale map memory mutex new numeric "
    "optional ostream queue random ratio regex set shared_mutex sstream stack stdexcept "
    "streambuf string string_view system_error thread tuple type_traits typeinfo "
    "unordered_map unordered_set utility valarray variant vector".split())
_POSIX_PREFIXES = ("sys/", "netinet/", "arpa/", "linux/", "asm/", "bits/", "net/")


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _under(rel: str, d: str) -> bool:
    return not d or rel == d or rel.startswith(d + "/")


def _match(entry: str, item: str) -> bool:
    if entry.endswith(("/", ".")):
        return item.startswith(entry)
    return item == entry or item.split(".", 1)[0] == entry or item.split("/", 1)[0] == entry \
        or (entry == "sctp" and "sctp" in item)


class MultiLangIndex(GoModuleIndex):
    """GoModuleIndex over every selected language; `languages` gives per-language totals."""

    def __init__(self, root: Path, module: str):
        super().__init__(root, module)
        self.languages: dict[str, dict] = {}
        self.package_languages: dict[str, list[str]] = {}
        self.main_kinds: dict[str, set[str]] = {}       # package -> languages with a main
        self.cobol_index: Any = None                    # the run's CobolRepoIndex
        self.cobol_programs: dict[str, str] = {}        # COBOL package key -> PROGRAM-ID
        self.package_of_program: dict[str, str] = {}    # PROGRAM-ID -> package key
        self.cobol_page_of: dict[str, str] = {}         # COBOL key -> wiki page (merge_into)
        self.cobol_digests: dict[str, Any] = {}         # PROGRAM-ID -> ProgramDigest
        self.cobol_docs: dict[str, Any] = {}            # PROGRAM-ID -> LeafDoc
        self.cobol_out_docs: set[str] = set()           # programs documented in OUT/docs

    # -- construction ---------------------------------------------------------------------

    @classmethod
    def build(cls, root: str | Path, subtree: str | None = None,
              languages: list[str] | tuple[str, ...] = ("go",), settings: Any = None, *,
              out_dir: str | Path | None = None) -> MultiLangIndex:
        """index the units of `languages` under root (optionally one subtree)."""
        root = Path(root).resolve()
        langs = [lang for lang in ALL_LANGS if lang in set(languages)]
        sub = (subtree or "").strip().strip("/")
        if sub == ".":
            sub = ""
        go_index = GoModuleIndex.build(root, subtree=sub or None) if "go" in langs else None
        module = go_index.module if go_index is not None else root.name
        index = cls(root, module)
        if go_index is not None:
            for path, pkg in go_index.packages.items():
                index.packages[path] = pkg
                index._note(path, "go", pkg.is_main)
            index.languages["go"] = {"packages": len(go_index.packages),
                                     "files": sum(len(p.files) for p in go_index.packages.values()),
                                     "loc": sum(p.loc for p in go_index.packages.values())}
        others = [lang for lang in langs if lang != "go"]
        cgo_edges: list[tuple[str, str]] = []
        if others:
            ctx = RepoContext.build(root, others, settings, out_dir)
            for lang in others:
                try:
                    if lang == "cobol":
                        index._add_cobol(ctx, sub)
                    else:
                        index._add_language(ctx, lang, sub)
                except Exception as e:  # noqa: BLE001 - one language must not sink the wiki
                    log.warning("wiki: %s analysis failed: %s", lang, e)
            if go_index is not None and ("c" in others or "cpp" in others):
                cgo_edges = index._cgo_edges(ctx, go_index)
        index._build_graph()
        for s, d in cgo_edges:
            if s in index.packages and d in index.packages and s != d:
                index.graph.add_edge(s, d)
        return index

    def _note(self, path: str, lang: str, is_main: bool) -> None:
        langs = self.package_languages.setdefault(path, [])
        if lang not in langs:
            langs.append(lang)
        if is_main:
            self.main_kinds.setdefault(path, set()).add(lang)

    def _units(self, ctx: RepoContext, lang: str, ad: Any) -> dict[str, dict]:
        """unit dir -> {"name", "files", "tests", "doc_files"} for one language."""
        out: dict[str, dict] = {}
        if lang == "python":
            m = ad.model
            for d, u in (m.units.items() if m is not None else ()):
                out[d] = {"name": u.name, "files": list(u.files), "tests": len(u.test_files)}
        elif lang == "java":
            lay = ad.layout()
            for d, u in lay.units.items():
                out[d] = {"name": u.name, "files": list(u.files), "tests": len(u.test_files)}
        else:
            from codocs.lang.registry import is_header

            units = ad.units()
            by_dir: dict[str, list[str]] = {}
            for rel in ctx.files.get(lang, []):
                by_dir.setdefault(_parent(rel), []).append(rel)
            for d, rels in sorted(by_dir.items()):
                u = units.get(d)
                if u is not None:
                    out[d] = {"name": u.name, "files": list(u.files),
                              "tests": len(u.test_files)}
                    continue
                headers = [r for r in rels if is_header(r)]
                if headers:                   # header-only directories are include targets
                    name = PurePosixPath(d).name if d else ctx.root.name
                    out[d] = {"name": name, "files": headers, "tests": 0}
        return out

    def _add_language(self, ctx: RepoContext, lang: str, sub: str) -> None:
        from codocs.lang.registry import adapter

        ad = adapter(lang, ctx)
        units = {d: u for d, u in self._units(ctx, lang, ad).items() if _under(d, sub)}
        totals = {"packages": 0, "files": 0, "loc": 0}
        for d, info in sorted(units.items()):
            if not info["files"]:
                continue
            pkg = self.packages.get(d)
            if pkg is None:
                pkg = GoPackage(path=d, name=info["name"])
            internal = set(pkg.imports_internal)
            external = set(pkg.imports_external)
            stdlib = set(pkg.imports_stdlib)
            main = False
            doc = ""
            loc = 0
            for rel in info["files"]:
                src = ctx.read(rel)
                try:
                    facts = ad.wiki_facts(ctx.root, rel, src, ctx)
                except Exception as e:  # noqa: BLE001 - an unparsable file keeps its LOC only
                    log.debug("wiki facts of %s failed: %s", rel, e)
                    from codocs.lang.treesitter import line_count

                    facts = None
                    loc += line_count(src)
                if rel not in pkg.files:
                    pkg.files.append(rel)
                if facts is None:
                    continue
                loc += facts.loc
                main = main or facts.is_main
                doc = doc or facts.doc_comment
                for dcl in facts.decls:
                    if dcl.kind not in ("func", "method", "type"):
                        continue
                    pkg.symbols.append(GoSymbol(
                        name=dcl.name, kind=dcl.kind, receiver=dcl.receiver, file=rel,
                        exported=_exported(lang, dcl)))
                try:
                    resolved = ad.resolve_imports(rel, facts.imports, ctx)
                except Exception:  # noqa: BLE001
                    resolved = []
                internal.update(r for r in resolved if r != d)
                for kind, item in _classify(lang, ad, ctx, rel, facts.imports, bool(resolved)):
                    if kind == "external":
                        external.add(item)
                    else:
                        stdlib.add(item)
            pkg.loc += loc
            pkg.test_files += info["tests"]
            pkg.is_main = pkg.is_main or main
            if not pkg.doc_comment and doc:
                pkg.doc_comment = doc
            if not pkg.name:
                pkg.name = info["name"]
            pkg.imports_internal = sorted(internal - {d})
            pkg.imports_external = sorted(external)
            pkg.imports_stdlib = sorted(stdlib)
            self.packages[d] = pkg
            self._note(d, lang, main)
            totals["packages"] += 1
            totals["files"] += len(info["files"])
            totals["loc"] += loc
        if totals["packages"]:
            self.languages[lang] = totals

    def _add_cobol(self, ctx: RepoContext, sub: str) -> None:
        """one package per COBOL program of the run's COBOL index (subtree `sub`)."""
        from codocs.cobol import graphfacts

        cidx = graphfacts.cobol_index(ctx, sub)
        self.cobol_index = cidx
        files: set[str] = set()
        loc = 0
        for wp in graphfacts.wiki_programs(cidx, ctx):
            if wp.key in self.packages:         # never shadow another language's package
                log.warning("wiki: COBOL package %s clashes with a package; skipped", wp.key)
                continue
            self.packages[wp.key] = wp.package
            self.cobol_programs[wp.key] = wp.pid
            self.package_of_program[wp.pid] = wp.key
            self._note(wp.key, "cobol", wp.package.is_main)
            files.update(wp.package.files)
            loc += wp.package.loc
        if self.cobol_programs:
            self.languages["cobol"] = {"packages": len(self.cobol_programs),
                                       "files": len(files), "loc": loc}

    def _cgo_edges(self, ctx: RepoContext, go_index: GoModuleIndex) -> list[tuple[str, str]]:
        """Go package dir -> dir of every repo header its cgo preambles include."""
        from codocs.bizlogic.xlang import _INCLUDE_RE, cgo_preamble
        from codocs.lang.registry import adapter

        c_ad = adapter("c", ctx)
        out: list[tuple[str, str]] = []
        for path, pkg in go_index.packages.items():
            for rel in pkg.files:
                src = ctx.read(rel)
                if '"C"' not in src:
                    continue
                pre = cgo_preamble(src)
                for m in _INCLUDE_RE.finditer(pre):
                    spec = f'"{m.group(2).strip()}"' if m.group(1) == '"' else \
                        f"<{m.group(2).strip()}>"
                    try:
                        got = c_ad.resolve_include(rel, spec, ctx)
                    except Exception:  # noqa: BLE001
                        got = None
                    if got is not None and _parent(got) != path:
                        edge = (path, _parent(got))
                        if edge not in out:
                            out.append(edge)
        return out

    # -- queries ------------------------------------------------------------------------

    def tech_stack(self) -> list[tuple[str, str, int]]:
        """Go's TECH_STACK_MAP, plus known Python/Java/C/C++ libraries."""
        rows: dict[tuple[str, str], int] = {}
        for name, cat, n in super().tech_stack():
            rows[(name, cat)] = rows.get((name, cat), 0) + n
        for lang, table in TECH_STACK_EXTRA.items():
            if lang not in self.languages:
                continue
            for path, pkg in self.packages.items():
                if lang not in self.package_languages.get(path, []):
                    continue
                items = set(pkg.imports_external) | set(pkg.imports_stdlib)
                for entry, name, cat in table:
                    if any(_match(entry, it) for it in items):
                        rows[(name, cat)] = rows.get((name, cat), 0) + 1
        if self.cobol_index is not None and self.cobol_programs:
            from codocs.cobol import graphfacts

            pkgs = {k: self.packages[k] for k in self.cobol_programs if k in self.packages}
            for name, cat, n in graphfacts.tech_stack(self.cobol_index, pkgs):
                rows[(name, cat)] = rows.get((name, cat), 0) + n
        return sorted([(name, cat, n) for (name, cat), n in rows.items()], key=lambda r: -r[2])

    def language_counts(self) -> dict[str, int]:
        """packages per language (a package of two languages counts for both)."""
        out: dict[str, int] = {}
        for langs in self.package_languages.values():
            for lang in langs:
                out[lang] = out.get(lang, 0) + 1
        return {lang: out[lang] for lang in ALL_LANGS if lang in out}


def _exported(lang: str, d: Any) -> bool:
    name = d.name
    sig = d.signature or ""
    if lang == "python":
        return not name.startswith("_")
    if lang == "java":
        words = sig.split("(", 1)[0].split("{", 1)[0].split()
        return "public" in words or "protected" in words
    if lang in ("c", "cpp"):
        return not sig.lstrip().startswith("static") and " static " not in \
            f" {sig.split('(', 1)[0]} "
    return True


def _classify(lang: str, ad: Any, ctx: RepoContext, rel: str, imports: list,
              any_resolved: bool) -> list[tuple[str, str]]:
    """(external | stdlib, item) for the imports of a file that are not repo-internal."""
    out: list[tuple[str, str]] = []
    if lang == "python":
        m = ad.model
        for imp in imports:
            if imp.kind not in ("import", "from") or imp.module.startswith("."):
                continue
            top = imp.module.split(".", 1)[0]
            if not top or top == "__future__":
                continue
            try:
                hits = m.resolve_spec(rel, imp) if m is not None else []
            except Exception:  # noqa: BLE001
                hits = []
            if hits:
                continue
            std = top in getattr(sys, "stdlib_module_names", ())
            out.append(("stdlib" if std else "external", top))
    elif lang == "java":
        try:
            lay = ad.layout()
        except Exception:  # noqa: BLE001
            lay = None
        for imp in imports:
            mod = imp.module
            if not mod:
                continue
            pkg = mod if imp.kind == "wildcard" else mod.rsplit(".", 1)[0]
            if imp.kind == "static":
                pkg = mod.rsplit(".", 1)[0]
            if lay is not None and (mod in lay.classes or any(
                    u.import_path == pkg for u in lay.units.values())):
                continue
            out.append(("stdlib" if mod.startswith(_JAVA_STD) else "external", pkg))
    else:
        from codocs.lang.cc import STD_C_HEADER_NAMES

        for imp in imports:
            if imp.kind != "angle":
                continue
            h = imp.module
            try:
                got = ad.resolve_include(rel, f"<{h}>", ctx)
            except Exception:  # noqa: BLE001
                got = None
            if got is not None:
                continue
            std = h in STD_C_HEADER_NAMES or h in _CPP_STD_HEADERS or h.startswith(
                _POSIX_PREFIXES)
            out.append(("stdlib" if std else "external", h))
    return out
