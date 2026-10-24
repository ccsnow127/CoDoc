"""package discovery, target selection, package context, relevant tests, import autofix."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from codocs.golang.gotool import GoRunner
from codocs.golang.source import (
    KEYWORDS,
    Decl,
    Import,
    _lex,
    add_imports,
    function_spans,
    identifiers,
    imports,
    is_generated,
    mask,
    package_name,
    remove_imports,
    test_functions,
    top_level_decls,
    type_definitions,
)

log = logging.getLogger(__name__)

TRUNCATED = "// ... (truncated)"
_IGNORED_NAMES = frozenset({"init", "main", "_"})
_MSG_RE = re.compile(r"^\s*(?:.*[/\\])?(?P<base>[^/\\\s:]+\.go):\d+(?::\d+)?:\s*(?P<msg>.*)$")
_UNUSED_RE = re.compile(
    r'^"(?P<path>[^"]+)" imported(?: as \S+)? and not used'
    r'|^imported and not used: "(?P<path2>[^"]+)"'
)
_UNDEFINED_RE = re.compile(r"^undefined: (?P<name>[^\W\d]\w*)\s*$")


@dataclass
class PackageInfo:
    rel: str
    import_path: str
    name: str
    go_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    xtest_files: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def all_test_files(self) -> list[str]:
        return self.test_files + self.xtest_files

    @property
    def dir_name(self) -> str:
        return self.rel or "_root"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PackageInfo:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Target:
    pkg: str
    file: str
    stem: str
    lines: int
    status: str = "ok"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Target:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def _read(module_root: Path, rel: str) -> str:
    try:
        return (Path(module_root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _norm(rel: str | None) -> str:
    rel = (rel or "").strip().strip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    return "" if rel == "." else rel


def _under(rel: str, prefixes: list[str]) -> bool:
    return any(rel == p or not p or rel.startswith(p + "/") for p in prefixes)


# -- discovery ----------------------------------------------------------------


def _static_packages(module_root: Path, module: str, subtree: str | None
                     ) -> dict[str, PackageInfo]:
    """repowiki's static module scan, with test files split by their package clause."""
    from codocs.repowiki.analysis.golang import GoModuleIndex

    index = GoModuleIndex.build(module_root, subtree or None)
    out: dict[str, PackageInfo] = {}
    for rel, gp in index.packages.items():
        rel = _norm(rel)
        d = Path(module_root) / rel
        test_files: list[str] = []
        xtest_files: list[str] = []
        for p in sorted(d.glob("*_test.go")):
            try:
                name = package_name(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                name = None
            file_rel = f"{rel}/{p.name}" if rel else p.name
            if name and name.endswith("_test") and name != gp.name:
                xtest_files.append(file_rel)
            else:
                test_files.append(file_rel)
        if module:
            import_path = f"{module}/{rel}" if rel else module
        else:
            import_path = rel
        out[rel] = PackageInfo(
            rel=rel, import_path=import_path, name=gp.name, go_files=sorted(gp.files),
            test_files=test_files, xtest_files=xtest_files,
        )
    return out


async def discover(runner: GoRunner, subtree: str | None = None,
                   packages: list[str] | None = None) -> dict[str, PackageInfo]:
    """packages of the module (or of a subtree), via go list with a static fallback."""
    sub = _norm(subtree)
    pattern = f"./{sub}/..." if sub else "./..."
    listed: dict[str, PackageInfo] = {}
    try:
        for rel, gp in (await runner.list_packages(pattern)).items():
            if not gp.go_files:
                continue
            listed[rel] = PackageInfo(
                rel=rel, import_path=gp.import_path or runner.import_path(rel), name=gp.name,
                go_files=list(gp.go_files), test_files=list(gp.test_files),
                xtest_files=list(gp.xtest_files), error=gp.error,
            )
    except (OSError, RuntimeError, ValueError) as e:
        log.warning("go list failed: %s", e)
    if not listed:
        log.warning("go list found no packages; falling back to a static scan "
                    "(build constraints are not applied)")
        listed = _static_packages(runner.module_root, runner.module, sub)
    if sub:
        listed = {k: v for k, v in listed.items() if _under(k, [sub])}
    if packages:
        wanted = [_norm(p) for p in packages]
        listed = {
            k: v for k, v in listed.items()
            if any(k == p or (p and k.startswith(p + "/")) for p in wanted)
        }
    return dict(sorted(listed.items()))


def _status(src: str, max_file_lines: int) -> tuple[int, str]:
    lines = src.count("\n") + (1 if src and not src.endswith("\n") else 0)
    if is_generated(src):
        return lines, "generated"
    if lines > max_file_lines:
        return lines, "too-large"
    decls = top_level_decls(src)
    if not decls:
        return lines, "no-decls"
    if not any(d.kind in ("func", "method") for d in decls):
        return lines, "no-funcs"
    return lines, "ok"


def select_targets(module_root: Path, pkgs: dict[str, PackageInfo], *, files: str | None = None,
                   max_files: int | None = None, max_file_lines: int = 1500) -> list[Target]:
    """every non-test file of the packages, with a status saying whether it is processed."""
    out: list[Target] = []
    kept = 0
    for pkg_rel, rel in sorted((p.rel, f) for p in pkgs.values() for f in p.go_files):
        if files and not fnmatchcase(rel, files):
            continue
        lines, status = _status(_read(module_root, rel), max_file_lines)
        if status in ("ok", "no-funcs"):
            if max_files is not None and kept >= max_files:
                continue
            kept += 1
        stem = PurePosixPath(rel).name
        stem = stem[:-3] if stem.endswith(".go") else stem
        out.append(Target(pkg=pkg_rel, file=rel, stem=stem, lines=lines, status=status))
    return out


# -- context ------------------------------------------------------------------


def _other_files(pkg: PackageInfo, exclude_rel: str | None) -> list[str]:
    return [f for f in sorted(pkg.go_files) if f != exclude_rel]


def _module_of(pkg: PackageInfo) -> str:
    if pkg.rel and pkg.import_path.endswith("/" + pkg.rel):
        return pkg.import_path[: -len(pkg.rel) - 1]
    return pkg.import_path


_VAR_ALIAS_RE = re.compile(r"^var (\w+)\s*=\s*(\w+)$")


def _func_types(decls: list[Decl]) -> dict[str, str]:
    """name -> func type for funcs and for vars aliasing them (`var Lock = mutexLock`)."""
    types = {d.name: "func" + d.signature.split(d.name, 1)[1]
             for d in decls if d.kind == "func" and d.name in d.signature}
    aliases = {m.group(1): m.group(2) for d in decls if d.kind == "var"
               for m in [_VAR_ALIAS_RE.match(d.signature.strip())] if m}
    for _ in range(4):
        new = {a: types[t] for a, t in aliases.items() if t in types and a not in types}
        if not new:
            break
        types.update(new)
    return types


def _with_func_type(d: Decl, ftypes: dict[str, str]) -> Decl:
    """`var Get = getImpl` gets getImpl's func type appended, so a stub can match it."""
    if not _VAR_ALIAS_RE.match(d.signature.strip()) or d.name not in ftypes:
        return d
    sig = " ".join(d.signature.split())
    return Decl(**{**asdict(d), "signature": f"{sig}  // type: {ftypes[d.name]}"})

def imported_api(module_root: Path, pkg: PackageInfo, target_rel: str,
                 max_chars: int = 6000) -> str:
    """api of the module-internal packages the target imports.

    for each such import, the declarations the target selects (store.Get, store.ErrNotFound) come first,
    with full struct/interface definitions and the methods of selected types, then the
    package's other exported signatures while the budget lasts. without it a model has to
    guess another package's api.
    """
    src = _read(module_root, target_rel)
    module = _module_of(pkg)
    if not module:
        return ""
    masked = mask(src)
    blocks: list[str] = []
    size = 0
    for imp in imports(src):
        if not imp.path.startswith(module + "/") or imp.alias in ("_", "."):
            continue
        rel = imp.path[len(module) + 1:]
        pkg_dir = module_root / rel
        if not pkg_dir.is_dir():
            continue
        name = imp.name
        used = set(re.findall(rf"(?<![\w.]){re.escape(name)}\.([A-Za-z_]\w*)", masked))
        if not used:
            continue
        first: list[str] = []
        later: list[str] = []
        files: list[tuple[str, list[Decl], dict[str, str]]] = []
        all_decls: list[Decl] = []
        for f in sorted(pkg_dir.glob("*.go")):
            if f.name.endswith("_test.go"):
                continue
            try:
                fsrc = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if is_generated(fsrc):
                continue
            decls = top_level_decls(fsrc)
            files.append((fsrc, decls, type_definitions(fsrc)))
            all_decls.extend(decls)
        ftypes = _func_types(all_decls)
        for _fsrc, decls, types in files:
            for d in decls:
                if d.kind == "var":
                    d = _with_func_type(d, ftypes)
                if not d.signature or not d.name[:1].isupper():
                    continue
                if d.kind == "method":
                    (first if d.receiver in used else later).append(d.signature)
                elif d.name in used:
                    full = types.get(d.name, "") if d.kind == "type" else ""
                    first.append(full or d.signature)
                else:
                    later.append(d.signature)
        head = f"// package {name} ({imp.path})"
        lines = [head]
        for sig in dict.fromkeys(first + later):
            if size + sum(len(x) + 1 for x in lines) + len(sig) + 1 > max_chars:
                lines.append(TRUNCATED)
                break
            lines.append(sig)
        if len(lines) > 1:
            blocks.append("\n".join(lines))
            size += len(blocks[-1]) + 2
        if size >= max_chars:
            break
    return "\n\n".join(blocks) + "\n" if blocks else ""


def package_context(module_root: Path, pkg: PackageInfo, target_rel: str,
                    max_chars: int = 12000, imported_chars: int = 6000) -> str:
    """signatures of the package's other declarations, those the target references first.

    struct and interface types the target references are shown with their full definition
    (comments removed) while it fits the budget, so field and method names are exact.
    """
    used = identifiers(_read(module_root, target_rel))
    per_file = []
    for f in _other_files(pkg, target_rel):
        src = _read(module_root, f)
        per_file.append((f, top_level_decls(src), type_definitions(src)))
    referenced: list[tuple[str, str, str]] = []
    rest: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for f, decls, types in per_file:
        for d in decls:
            key = (f, d.signature)
            if not d.signature or key in seen or d.name not in used:
                continue
            seen.add(key)
            short = d.kind == "type" and d.signature.endswith(("struct", "interface"))
            full = types.get(d.name, "") if short else ""
            referenced.append((f, d.signature, full))
    for f, decls, _ in per_file:
        for d in decls:
            key = (f, d.signature)
            if not d.signature or key in seen or not d.name[:1].isupper():
                continue
            seen.add(key)
            rest.append((f, d.signature, ""))

    lines: list[str] = []
    size = 0
    for section in (referenced, rest):
        current = None
        for f, sig, full in section:
            head = ([""] if lines else []) + [f"// {f}"] if f != current else []
            room = max_chars - size - len(TRUNCATED) - 1
            chunk = head + [full] if full else []
            if not chunk or sum(len(c) + 1 for c in chunk) > room:
                chunk = head + [sig]
            add = sum(len(c) + 1 for c in chunk)
            if add > room:
                lines.append(TRUNCATED)
                return _with_imported("\n".join(lines) + "\n",
                                      imported_api(module_root, pkg, target_rel, imported_chars))
            lines.extend(chunk)
            size += add
            current = f
    own = "\n".join(lines) + "\n" if lines else ""
    return _with_imported(own, imported_api(module_root, pkg, target_rel, imported_chars))


def _with_imported(own: str, api: str) -> str:
    if not api:
        return own
    head = "// ---- module-internal packages this file imports (they exist; use them as declared)\n"
    return (own + "\n" if own else "") + head + api


def package_identifiers(module_root: Path, pkg: PackageInfo,
                        exclude_rel: str | None = None) -> set[str]:
    """package-scope names declared in the non-test files (methods excluded)."""
    names: set[str] = set()
    for f in _other_files(pkg, exclude_rel):
        for d in top_level_decls(_read(module_root, f)):
            if d.kind != "method" and d.name not in _IGNORED_NAMES:
                names.add(d.name)
    return names


# -- tests --------------------------------------------------------------------


def _own_qualifiers(src: str, pkg: PackageInfo) -> set[str]:
    """names under which a test file refers to the package itself (external test packages)."""
    out = set()
    for imp in imports(src):
        if imp.path == pkg.import_path and imp.alias not in ("_", "."):
            out.add(imp.alias or pkg.name or imp.name)
    return out


_NON_TEST_PREFIXES = ("Test", "Benchmark", "Example", "Fuzz")


def test_reference(module_root: Path, pkg: PackageInfo, target_rel: str,
                   max_chars: int = 8000) -> str:
    """helpers, stubs, fixtures and setup from the package's existing internal test files,
    those touching what the target uses first, plus one example test of the target.

    a generated suite runs with those files hidden, so this is reference material to copy
    from (how this codebase stubs its collaborators and bootstraps loggers and globals).
    """
    src = _read(module_root, target_rel)
    target_names = {d.name for d in top_level_decls(src) if d.name not in _IGNORED_NAMES}
    wanted = identifiers(src) | {i.name for i in imports(src)}
    stem = PurePosixPath(target_rel).name.removesuffix(".go")
    companion = str(PurePosixPath(target_rel).with_name(f"{stem}_test.go"))
    scored: list[tuple[float, str, str]] = []
    example: tuple[int, str, str] | None = None
    for f in pkg.test_files:
        tsrc = _read(module_root, f)
        if not tsrc:
            continue
        seen_spans: set[tuple[int, int]] = set()
        for d in top_level_decls(tsrc):
            span = (d.start, d.end)
            if span in seen_spans:
                continue
            seen_spans.add(span)
            text = tsrc[d.start:d.end].strip()
            if not text:
                continue
            used = identifiers(text)
            is_test = d.kind == "func" and d.name.startswith(_NON_TEST_PREFIXES) \
                and d.name != "TestMain"
            if is_test:
                if f == companion and used & target_names and (
                        example is None or len(text) < example[0]):
                    example = (len(text), f, text)
                continue
            score = 3.0 * len(used & target_names) + len(used & wanted) \
                + (2.0 if f == companion else 0.0) + (5.0 if d.name == "TestMain" else 0.0)
            if score > 0:
                scored.append((score, f, text))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [(f, t) for _, f, t in scored]
    if example is not None and len(example[2]) <= max_chars // 3:
        picked.insert(0, (example[1], "// example test of the target from the existing suite\n"
                          + example[2]))
    lines: list[str] = []
    size = 0
    current = None
    for f, text in picked:
        chunk = ([f"// from {f}"] if f != current else []) + [text, ""]
        add = sum(len(c) + 1 for c in chunk)
        if size + add > max_chars:
            lines.append(TRUNCATED)
            break
        lines.extend(chunk)
        size += add
        current = f
    return "\n".join(lines).strip() + "\n" if lines else ""


def test_index(module_root: Path, pkg: PackageInfo) -> dict[str, dict]:
    """test name -> {"file", "idents", "selectors"} for every test function of the package.

    idents are bare uses (plus `pkg.Name` in an external test package); selectors are names
    used after a dot (fields, methods, other packages' members).
    """
    index: dict[str, dict] = {}
    for f in sorted(pkg.all_test_files):
        src = _read(module_root, f)
        names = test_functions(src)
        if not names:
            continue
        own = _own_qualifiers(src, pkg)
        toks = _lex(src).toks
        spans = function_spans(src)
        for name in names:
            a, b = spans.get(name, (0, 0))
            bare: set[str] = set()
            selectors: set[str] = set()
            for k, t in enumerate(toks):
                if t.start < a or t.end > b or t.kind != "ident" or t.text in KEYWORDS:
                    continue
                if k and toks[k - 1].kind == "op" and toks[k - 1].text == ".":
                    q = toks[k - 2] if k >= 2 else None
                    if q is not None and q.kind == "ident" and q.text in own:
                        bare.add(t.text)
                    else:
                        selectors.add(t.text)
                else:
                    bare.add(t.text)
            index[name] = {"file": f, "idents": sorted(bare), "selectors": sorted(selectors)}
    return index


test_index.__test__ = False


def _target_names(src: str) -> tuple[set[str], list[Decl]]:
    plain: set[str] = set()
    methods: list[Decl] = []
    for d in top_level_decls(src):
        if d.kind == "method":
            if d.name not in _IGNORED_NAMES:
                methods.append(d)
        elif d.name not in _IGNORED_NAMES:
            plain.add(d.name)
    return plain, methods


def relevant_tests(module_root: Path, pkg: PackageInfo, target_rel: str,
                   candidates: set[str]) -> list[str]:
    """candidate tests that live in <stem>_test.go or reference the target's declarations."""
    target = PurePosixPath(target_rel)
    own_test = (target.parent / f"{target.stem}_test.go").as_posix()
    plain, methods = _target_names(_read(module_root, target_rel))
    out = []
    for name, info in test_index(module_root, pkg).items():
        if name not in candidates:
            continue
        idents = set(info["idents"])
        called = idents | set(info.get("selectors") or [])
        if (
            info["file"] == own_test
            or plain & idents
            or any(m.name in called and m.receiver in idents for m in methods)
        ):
            out.append(name)
    return sorted(out)


# -- import repair ------------------------------------------------------------


def fix_imports(src: str, build_errors: list[str], file_basename: str,
                allowed: list[Import]) -> str | None:
    """drop unused imports and add missing allowed ones, as the compiler reports them."""
    present = imports(src)
    present_paths = {i.path for i in present}
    present_names = {i.name for i in present}
    remove: set[str] = set()
    add: list[Import] = []
    for line in build_errors:
        m = _MSG_RE.match(line)
        if not m or m.group("base") != file_basename:
            continue
        msg = m.group("msg").strip()
        u = _UNUSED_RE.match(msg)
        if u:
            path = u.group("path") or u.group("path2")
            if path in present_paths:
                remove.add(path)
            continue
        u = _UNDEFINED_RE.match(msg)
        if not u:
            continue
        name = u.group("name")
        if name in present_names:
            continue
        for imp in allowed:
            if imp.alias in ("_", ".") or imp.name != name:
                continue
            if imp.path in present_paths:
                continue
            if all(a.path != imp.path for a in add):
                add.append(Import(path=imp.path, alias=imp.alias))
            present_names.add(name)
            break
    if not remove and not add:
        return None
    out = src
    if remove:
        out = remove_imports(out, remove)
    if add:
        out = add_imports(out, add)
    return out if out != src else None
