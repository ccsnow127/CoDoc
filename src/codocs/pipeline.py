"""stage orchestration: analyze -> baseline -> testgen -> docgen -> judge -> wiki -> report.

Go-only mode (the selected languages are exactly ["go"]) runs the single-module Go path.
multi mode runs every stage once per language in ALL_LANGS order, each with its own runner,
and the wiki and the report once over all of them. the judge stage judges the COBOL docs of
OUT/docs and exists only in multi mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codocs.config import Settings
from codocs.golang import gotool
from codocs.golang.gotool import GoRunner
from codocs.golang.package import PackageInfo, Target, discover, select_targets
from codocs.lang.base import ALL_LANGS, DISPLAY, NO_TS_LANGS
from codocs.lang.registry import Detection, parse_requested, resolve_languages

log = logging.getLogger(__name__)

STAGES = ("analyze", "baseline", "testgen", "docgen", "judge", "wiki", "report")
SKIPPABLE = ("baseline", "testgen", "docgen", "judge", "wiki")
# removed --skip stage names, with their replacement
LEGACY_SKIPS = {
    "leafdoc": "docgen (the bounded docgen loop replaces one-shot leaf docs)",
    "testrun": "testgen (tests are measured inside the loop; re-run them with codocs testrun)",
    "phi": "docgen (phi is the docgen loop's score)",
}
PROCESSED = ("ok", "no-funcs")
# key of run()'s returned meta (never written to run_meta.json): the stages this run recorded
RAN_KEY = "stages_run"


class GoUnavailable(RuntimeError):
    """the configured go binary cannot be run."""


@dataclass
class Selection:
    subtree: str | None = None
    packages: list[str] | None = None
    files: str | None = None
    max_files: int | None = None
    programs: list[str] | None = None       # COBOL program ids (--programs)

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("programs") is None:
            d.pop("programs", None)
        return d


@dataclass
class Analysis:
    module_root: Path
    module: str
    go_version: str
    pkgs: dict[str, PackageInfo]
    targets: list[Target]
    selection: Selection = field(default_factory=Selection)

    @property
    def narrowed(self) -> bool:
        sel = self.selection
        return bool(sel.files or sel.max_files is not None or sel.programs)

    def active_packages(self) -> dict[str, PackageInfo]:
        """packages to baseline: all of them, or those with a selected target when narrowed."""
        if not self.narrowed:
            return dict(self.pkgs)
        wanted = {t.pkg for t in self.targets if t.status in PROCESSED}
        return {k: v for k, v in self.pkgs.items() if k in wanted}

    def totals(self) -> dict:
        statuses = Counter(t.status for t in self.targets)
        return {
            "packages": len(self.pkgs),
            "packages_with_tests": sum(1 for p in self.pkgs.values() if p.all_test_files),
            "files": len(self.targets),
            "lines": sum(t.lines for t in self.targets),
            "test_files": sum(len(p.all_test_files) for p in self.pkgs.values()),
            "statuses": dict(sorted(statuses.items())),
            "docgen_targets": statuses.get("ok", 0) + statuses.get("no-funcs", 0),
            "testgen_targets": statuses.get("ok", 0),
        }

    def to_dict(self) -> dict:
        per_pkg: dict[str, Counter] = {k: Counter() for k in self.pkgs}
        for t in self.targets:
            per_pkg.setdefault(t.pkg, Counter())[t.status] += 1
        packages = [
            {
                "rel": p.rel, "import_path": p.import_path, "name": p.name,
                "go_files": len(p.go_files), "test_files": len(p.test_files),
                "xtest_files": len(p.xtest_files),
                "targets": dict(sorted(per_pkg.get(rel, Counter()).items())),
                "error": p.error,
            }
            for rel, p in self.pkgs.items()
        ]
        return {
            "module_root": os.fspath(self.module_root),
            "module": self.module,
            "go_version": self.go_version,
            "selection": self.selection.to_dict(),
            "totals": self.totals(),
            "packages": packages,
            "targets": [t.to_dict() for t in self.targets],
            "skipped": [t.to_dict() for t in self.targets if t.status not in PROCESSED],
        }


class Hooks:
    """progress callbacks; the cli overrides them with rich output."""

    def stage_start(self, name: str, total: int | None = None) -> None:
        pass

    def note(self, text: str) -> None:
        """a one-line notice (multi mode only: detection notes, per-language target counts)."""

    def progress(self, name: str, *args) -> None:
        pass

    def stage_end(self, name: str, info: dict) -> None:
        pass

    def callback(self, name: str):
        return lambda *args, **kwargs: self.progress(name, *args, *kwargs.values())


# -- helpers --------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _tokens(llm) -> dict:
    t = getattr(llm, "tokens", None) or {}
    return {"input": int(t.get("input", 0)), "output": int(t.get("output", 0))}


def _accumulate(meta: dict, tokens: dict | None, llm_stats: dict | None) -> None:
    acc = meta.setdefault("tokens", {"input": 0, "output": 0})
    for k, v in (tokens or {}).items():
        acc[k] = int(acc.get(k, 0)) + int(v)
    if llm_stats:
        acc = meta.setdefault("llm", {"calls": 0, "cache_hits": 0})
        for k, v in llm_stats.items():
            acc[k] = int(acc.get(k, 0)) + int(v)


def record_stage(out_dir: Path, settings: Settings, name: str, info: dict,
                 repo: Path | None = None, llm_stats: dict | None = None,
                 accumulate: bool = True) -> dict:
    """merge one stage's info into OUT/run_meta.json; tokens and llm counters accumulate
    (unless accumulate=False: a TokenMeter already added them file by file)."""
    path = Path(out_dir) / "run_meta.json"
    meta = _read_json(path)
    meta["settings"] = settings.to_dict()
    if repo is not None:
        meta["repo"] = os.fspath(repo)
    meta.setdefault("created", _now())
    meta["updated"] = _now()
    meta.setdefault("stages", {})[name] = info
    meta.setdefault("tokens", {"input": 0, "output": 0})
    if accumulate:
        _accumulate(meta, info.get("tokens"), llm_stats)
    _write_json(path, meta)
    return meta


class TokenMeter:
    """adds llm usage to OUT/run_meta.json as it happens, so an interrupted run keeps it."""

    def __init__(self, out_dir: Path, llm):
        self.path = Path(out_dir) / "run_meta.json"
        self.llm = llm
        self._last = self._now()
        self.start = dict(self._last)

    def _now(self) -> dict:
        t = _tokens(self.llm)
        return {**t, "calls": int(getattr(self.llm, "calls", 0)),
                "cache_hits": int(getattr(self.llm, "cache_hits", 0))}

    def flush(self) -> None:
        now = self._now()
        delta = {k: now[k] - self._last.get(k, 0) for k in now}
        if not any(delta.values()):
            return
        meta = _read_json(self.path)
        meta.setdefault("created", _now())
        meta["updated"] = _now()
        _accumulate(meta, {k: delta[k] for k in ("input", "output")},
                    {k: delta[k] for k in ("calls", "cache_hits")})
        _write_json(self.path, meta)
        self._last = now

    def stage(self) -> tuple[dict, dict]:
        """(tokens, llm counters) since this meter started."""
        now = self._now()
        return ({k: now[k] - self.start[k] for k in ("input", "output")},
                {k: now[k] - self.start[k] for k in ("calls", "cache_hits")})

    def wrap(self, callback):
        def cb(*args, **kwargs):
            out = callback(*args, **kwargs) if callback else None
            self.flush()
            return out
        return cb


def _outside(pkgs: dict[str, PackageInfo], module_root: Path, out_dir: Path
             ) -> dict[str, PackageInfo]:
    """packages not inside OUT (round artifacts there hold .go files when OUT is in the module)."""
    try:
        rel = out_dir.resolve().relative_to(Path(module_root).resolve()).as_posix()
    except ValueError:
        return pkgs
    if rel in ("", "."):
        return pkgs
    return {k: v for k, v in pkgs.items() if k != rel and not k.startswith(rel + "/")}


def _sandbox_dir(root: Path, parent: Path) -> Path:
    """mirror location <parent>/<root.name>, with root's siblings symlinked next to it.

    cgo flags and go.mod replaces often use relative paths that climb out of the module
    (e.g. -I../../common/include); keeping the module's own directory name and
    its siblings makes those resolve in the mirror exactly as in the original tree.
    """
    parent.mkdir(parents=True, exist_ok=True)
    for sib in root.parent.iterdir():
        if sib.name == root.name:
            continue
        link = parent / sib.name
        if link.is_symlink() or link.exists():
            continue
        try:
            link.symlink_to(sib, target_is_directory=sib.is_dir())
        except OSError:
            continue
    return parent / root.name


def _go_runner(root: Path, settings: Settings, out_dir: Path | None) -> GoRunner:
    """the GoRunner make_runner returns, without the availability check."""
    go = os.path.expanduser(settings.go)
    kw: dict = {}
    if out_dir is not None:
        work = Path(out_dir).resolve() / "work"
        kw["work_dir"] = work / "tmp"
        if settings.sandbox:
            kw.update(sandbox=_sandbox_dir(root, work / "sandbox"),
                      sandbox_exclude=[Path(out_dir).resolve()])
    return GoRunner(root, go=go, workers=settings.workers, timeout=settings.go_timeout,
                    env=settings.go_env_map(), flags=settings.go_flag_list(), **kw)


def make_runner(repo: str | Path, settings: Settings, out_dir: Path | None = None) -> GoRunner:
    """GoRunner for the module at repo; raises when there is no go.mod or no usable go.

    with out_dir (and settings.sandbox), go runs in a mirror of the module under
    OUT/work/sandbox/<repo dir name>, so files the tests write never land in the repository.
    """
    root = Path(repo).expanduser().resolve()
    if not (root / "go.mod").is_file():
        raise ValueError(f"{root} is not a Go module (no go.mod)")
    go = os.path.expanduser(settings.go)
    runner = _go_runner(root, settings, out_dir)
    if not runner.available():
        raise GoUnavailable(
            f"cannot run the go binary {go!r}; install Go or pass --go PATH / set CODOCS_GO"
        )
    return runner


# -- multi-language runners -------------------------------------------------------------------

MULTILANG_DEPS = ("multi-language support needs tree-sitter and tree-sitter-language-pack: "
                  "pip install tree-sitter tree-sitter-language-pack")
SYNC_SKIP_DIRS = frozenset({".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                            ".mypy_cache", ".tox"})
_SYNC_KEEP = (".py", ".java", ".jar", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")


def _skip_sync_dir(d: Path, name: str, has_pom: bool) -> bool:
    """SYNC_SKIP_DIRS, any dir holding pyvenv.cfg, and `target` next to a pom.xml."""
    if name in SYNC_SKIP_DIRS or (name == "target" and has_pom):
        return True
    return (d / name / "pyvenv.cfg").is_file()


def sync_tree(src: Path, dst: Path, exclude: set[Path], *, skip_dirs: bool = True) -> None:
    """gotool._sync_tree's mirror, also skipping virtualenvs, caches and Maven `target/` dirs.

    skipped dirs are neither copied nor deleted in dst (mvn builds its own target/ there).
    """
    dst.mkdir(parents=True, exist_ok=True)
    keep = gotool._SYNC_KEEP_SUFFIXES + _SYNC_KEEP
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        out = dst / d.relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        wanted: set[str] = set()
        subdirs = []
        has_pom = "pom.xml" in filenames
        for name in dirnames:
            p = d / name
            if name in gotool._SKIP_SYNC or p in exclude:
                continue
            if skip_dirs and _skip_sync_dir(d, name, has_pom):
                wanted.add(name)            # left alone in dst
                continue
            wanted.add(name)
            if p.is_symlink():
                filenames.append(name)
            else:
                subdirs.append(name)
        dirnames[:] = subdirs
        for name in filenames:
            p, q = d / name, out / name
            wanted.add(name)
            try:
                if p.is_symlink():
                    link = os.readlink(p)
                    if not (q.is_symlink() and os.readlink(q) == link):
                        gotool._remove(q)
                        os.symlink(link, q)
                    continue
                st = p.stat()
                if st.st_size > gotool.SYNC_MAX_BYTES and not name.endswith(keep) \
                        and ".so." not in name:
                    wanted.discard(name)
                    continue
                if q.is_symlink() or q.is_dir():
                    gotool._remove(q)
                elif q.exists():
                    qs = q.stat()
                    if qs.st_size == st.st_size and qs.st_mtime_ns == st.st_mtime_ns:
                        continue
                shutil.copy2(p, q)
            except OSError:
                continue
        for extra in os.listdir(out):
            if extra not in wanted:
                gotool._remove(out / extra)


class RunContext:
    """what every runner of one run shares: paths, settings, the sandbox and its sync.

    with a GoRunner in the run, its own sandbox sync is the only one; otherwise
    the context mirrors the repo itself (sync_tree, virtualenvs and build outputs skipped).
    """

    def __init__(self, repo_root: Path, settings: Settings, out_dir: Path | None = None,
                 repo_ctx=None):
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.settings = settings
        self.out_dir = Path(out_dir).resolve() if out_dir is not None else None
        self.repo_ctx = repo_ctx
        self.workers = max(1, int(settings.workers))
        self.timeout = int(getattr(settings, "test_timeout", 0) or settings.go_timeout)
        if self.out_dir is not None:
            work = self.out_dir / "work"
        else:
            work = Path(tempfile.gettempdir()).resolve() / f"codocs-{os.getpid()}" / "work"
        self.work_dir = work / "tmp"
        self.tools_dir = work / "tools"
        self.sandbox: Path | None = None
        if self.out_dir is not None and settings.sandbox:
            self.sandbox = _sandbox_dir(self.repo_root, work / "sandbox").resolve()
        self.exec_root = self.sandbox or self.repo_root
        self.sandbox_exclude = [p for p in (self.out_dir,) if p is not None]
        self.go_runner: GoRunner | None = None
        self.sync_generation = 0
        self.cache: dict = {}
        self._synced = self.sandbox is None
        self._lock: asyncio.Lock | None = None
        self._lock_loop = None

    async def ensure_synced(self) -> None:
        """mirror the repo into the sandbox once (through the GoRunner when there is one)."""
        go = self.go_runner
        if go is not None and go.sandbox is not None:
            if not go._synced or not self.sync_generation:
                await go._prepare()
                self.sync_generation = max(self.sync_generation, 1)
            return
        if self._synced:
            return
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        async with self._lock:
            if self._synced:
                return
            exclude = {p.resolve() for p in self.sandbox_exclude}
            exclude |= {self.sandbox, self.work_dir.resolve() if self.work_dir.exists()
                        else self.work_dir}
            await asyncio.to_thread(sync_tree, self.repo_root, self.sandbox, exclude)
            self._synced = True
            self.sync_generation += 1


class Runners(dict):
    """lang -> runner (ALL_LANGS order), with the run context and the unusable languages."""

    def __init__(self, run_ctx: RunContext, detection: Detection):
        super().__init__()
        self.run_ctx = run_ctx
        self.repo_ctx = run_ctx.repo_ctx
        self.detection = detection
        self.unavailable: dict[str, str] = {}
        self.missing_deps: set[str] = set()      # not even analysable (no tree-sitter)

    @property
    def languages(self) -> list[str]:
        return [lang for lang in ALL_LANGS if lang in self]

    def usable(self) -> list[tuple[str, object]]:
        """(lang, runner) of the languages whose toolchain works, in ALL_LANGS order."""
        return [(lang, self[lang]) for lang in self.languages
                if self[lang] is not None and lang not in self.unavailable]


def _required(det: Detection, settings: Settings) -> set[str]:
    """languages whose missing toolchain is an error: named explicitly, or the only one."""
    raw = (getattr(settings, "source_lang", "auto") or "auto").strip().lower()
    if parse_requested(raw) is not None and raw != "all":
        return set(det.languages)
    return set(det.languages) if len(det.languages) == 1 else set()


def _has_multilang_deps() -> bool:
    """tree-sitter and its grammar pack are importable."""
    import importlib.util

    return all(importlib.util.find_spec(m) is not None
               for m in ("tree_sitter", "tree_sitter_language_pack"))


def _reason(runner, lang: str) -> str:
    fn = getattr(runner, "unavailable_reason", None)
    if fn is not None:
        with contextlib.suppress(Exception):
            text = fn()
            if text:
                return text
    if lang == "python":
        return "no Python interpreter with pytest and coverage (pass --py)"
    if lang in ("c", "cpp"):
        return f"cannot run the {DISPLAY[lang]} compiler {getattr(runner, 'compiler', '')!r}"
    return f"the {DISPLAY[lang]} toolchain is unavailable"


def make_runners(repo: str | Path, settings: Settings, out_dir: Path | None = None,
                 languages: Detection | list[str] | None = None, *,
                 subtree: str | None = None) -> Runners:
    """one runner per selected language around one RunContext (Go via make_runner).

    a language whose toolchain is unusable is an error when it was named or is the only one;
    otherwise a WARNING, and its targets get status no-toolchain.
    """
    from codocs.lang.base import RepoContext
    from codocs.lang.registry import adapter

    root = Path(repo).expanduser().resolve()
    if isinstance(languages, Detection):
        det = languages
    elif languages is None:
        det = resolve_languages(root, settings, subtree, out_dir=out_dir)
    else:
        langs = [lang for lang in ALL_LANGS if lang in set(languages)]
        det = Detection(languages=langs, legacy=langs == ["go"])
    repo_ctx = RepoContext.build(root, det.languages, settings, out_dir)
    run_ctx = RunContext(root, settings, out_dir, repo_ctx)
    runners = Runners(run_ctx, det)
    required = _required(det, settings)
    for lang in det.languages:
        if lang == "go":
            try:
                runner = make_runner(root, settings, out_dir)
            except GoUnavailable as e:
                if "go" in required:
                    raise
                log.warning("WARNING: %s; Go targets get status no-toolchain", e)
                runner = _go_runner(root, settings, out_dir)
                runners.unavailable["go"] = str(e)
            run_ctx.go_runner = runner
            runners["go"] = runner
            continue
        runner, reason = None, MULTILANG_DEPS
        if lang in NO_TS_LANGS or _has_multilang_deps():
            try:
                runner = adapter(lang, repo_ctx).make_runner(run_ctx)
                reason = "" if runner.available() else _reason(runner, lang)
            except ImportError:
                runner, reason = None, MULTILANG_DEPS
        if reason:
            if reason == MULTILANG_DEPS:
                runners.missing_deps.add(lang)
            if lang in required:
                raise ValueError(f"{DISPLAY[lang]}: {reason}")
            log.warning("WARNING: %s: %s; its targets get status no-toolchain",
                        DISPLAY[lang], reason)
            repo_ctx.mark_no_toolchain(lang, reason)
            runners.unavailable[lang] = reason
        runners[lang] = runner
    return runners


# -- stages ---------------------------------------------------------------------


async def analyze(runner: GoRunner, settings: Settings, out_dir: Path | None = None,
                  selection: Selection | None = None) -> Analysis:
    """packages and targets of the selection; writes OUT/analysis.json when out_dir is given."""
    sel = selection or Selection()
    pkgs = await discover(runner, sel.subtree, sel.packages)
    if out_dir is not None:
        pkgs = _outside(pkgs, runner.module_root, Path(out_dir))
    if not pkgs and (sel.packages or sel.subtree):
        what = ", ".join(sel.packages or []) or f"--subtree {sel.subtree}"
        raise ValueError(f"the selection matched no Go package: {what}")
    targets = select_targets(
        runner.module_root, pkgs, files=sel.files, max_files=sel.max_files,
        max_file_lines=settings.max_file_lines,
    )
    analysis = Analysis(
        module_root=runner.module_root, module=runner.module, go_version=runner.version(),
        pkgs=pkgs, targets=targets, selection=sel,
    )
    if out_dir is not None:
        _write_json(Path(out_dir) / "analysis.json", analysis.to_dict())
    log.info("analyze: %d packages, %d files", len(pkgs), len(targets))
    return analysis


async def baseline(runner: GoRunner, analysis: Analysis, out_dir: Path, force: bool = False,
                   on_progress=None) -> dict:
    """original-code test verdicts of the active packages (cached in OUT/baseline/)."""
    from codocs.baseline import package_baseline

    async def one(pkg: PackageInfo):
        base = await package_baseline(runner, pkg, Path(out_dir), force=force)
        if on_progress:
            on_progress(pkg.rel, base)
        return pkg.rel, base

    pairs = await asyncio.gather(*(one(p) for p in analysis.active_packages().values()))
    return dict(pairs)


async def testgen(runner: GoRunner, llm, settings: Settings, analysis: Analysis, out_dir: Path,
                  force: bool = False, on_progress=None) -> list:
    """TeSearch over the selected targets."""
    from codocs.tesearch.loop import run_testgen

    return await run_testgen(
        analysis.targets, analysis.pkgs, runner, llm, settings, Path(out_dir),
        analysis.module_root, force=force, on_progress=on_progress,
    )


async def docgen(runner: GoRunner, llm, settings: Settings, analysis: Analysis, out_dir: Path,
                 force: bool = False, use_generated_tests: bool = True,
                 on_progress=None) -> list:
    """SpecForge over the selected targets."""
    from codocs.specforge.loop import run_docgen

    return await run_docgen(
        analysis.targets, analysis.pkgs, runner, llm, settings, Path(out_dir),
        analysis.module_root, force=force, on_progress=on_progress,
        use_generated_tests=use_generated_tests,
    )


async def wiki(module_root: Path, out_dir: Path, settings: Settings, subtree: str | None = None,
               use_tree_docs: bool = True, fmt: str = "both", plan_only: bool = False,
               on_progress=None, packages: list[str] | None = None, refine: bool = True) -> dict:
    """RepoWiki deepdoc fed with OUT/docs (refine=False: no plan-refinement call)."""
    from codocs.wiki import run_wiki

    kw = {} if refine else {"refine": False}
    return await run_wiki(
        Path(module_root), Path(out_dir), settings, subtree=subtree, use_tree_docs=use_tree_docs,
        fmt=fmt, plan_only=plan_only, on_progress=on_progress, packages=packages, **kw,
    )


def wiki_packages(analysis: Analysis) -> list[str] | None:
    """the packages the wiki covers in a run: those of the selection (None = whole subtree)."""
    sel = analysis.selection
    if analysis.narrowed:
        return sorted(analysis.active_packages()) or None
    return list(sel.packages) if sel.packages else None


# -- multi mode: analysis and per-language stages ---------------------------------------------


@dataclass
class MultiAnalysis:
    """one Analysis per language (go_version = that runner's version), plus the union."""

    module_root: Path
    module: str
    parts: dict[str, Analysis]
    selection: Selection = field(default_factory=Selection)
    notes: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)

    cobol: dict | None = None       # summary, modes and scope of the COBOL index

    @property
    def languages(self) -> list[str]:
        return [lang for lang in ALL_LANGS if lang in self.parts]

    @property
    def go_version(self) -> str:
        part = self.parts.get("go")
        return part.go_version if part is not None else ""

    def versions(self) -> dict[str, str]:
        return {lang: self.parts[lang].go_version for lang in self.languages}

    @property
    def targets(self) -> list[Target]:
        return [t for lang in self.languages for t in self.parts[lang].targets]

    @property
    def narrowed(self) -> bool:
        sel = self.selection
        return bool(sel.files or sel.max_files is not None or sel.programs)

    def active_packages(self) -> dict:
        """unit rels to baseline, over every language (rels shared by c and cpp merge)."""
        out: dict = {}
        for lang in self.languages:
            out.update(self.parts[lang].active_packages())
        return dict(sorted(out.items()))

    def work_targets(self, lang: str) -> list[Target]:
        """targets handed to docgen/testgen: Go's all, others' processed only."""
        targets = self.parts[lang].targets
        return list(targets) if lang == "go" else [t for t in targets if t.status in PROCESSED]

    def part_for_loops(self, lang: str) -> Analysis:
        return dataclasses.replace(self.parts[lang], targets=self.work_targets(lang))

    def totals_by_language(self) -> dict[str, dict]:
        return {lang: self.parts[lang].totals() for lang in self.languages}

    def totals(self) -> dict:
        by = self.totals_by_language()
        out: dict = {}
        statuses: Counter = Counter()
        for tot in by.values():
            for k, v in tot.items():
                if k == "statuses":
                    statuses.update(v)
                else:
                    out[k] = out.get(k, 0) + v
        if not out:
            out = Analysis(self.module_root, self.module, "", {}, []).totals()
        out["statuses"] = dict(sorted(statuses.items()))
        return {k: out[k] for k in ("packages", "packages_with_tests", "files", "lines",
                                    "test_files", "statuses", "docgen_targets",
                                    "testgen_targets")}

    def cost_lines(self) -> list[str]:
        """"<lang>: <n> targets (<m> skipped: generated <g>, header <h>, ...)" per language."""
        lines = []
        for lang in self.languages:
            targets = self.parts[lang].targets
            work = [t for t in targets if t.status in PROCESSED]
            skipped = Counter(t.status for t in targets if t.status not in PROCESSED)
            text = f"{lang}: {len(work)} targets"
            if skipped:
                text += (f" ({sum(skipped.values())} skipped: "
                         + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())) + ")")
            if lang in self.unavailable:
                text += f" [no toolchain: {self.unavailable[lang]}]"
            lines.append(text)
        return lines

    def _packages(self) -> list[dict]:
        out: list[dict] = []
        for lang in self.languages:
            part = self.parts[lang]
            if lang == "go":
                out += [{"lang": "go", **p} for p in part.to_dict()["packages"]]
                continue
            per: dict[str, Counter] = {k: Counter() for k in part.pkgs}
            for t in part.targets:
                per.setdefault(t.pkg, Counter())[t.status] += 1
            for rel, u in part.pkgs.items():
                out.append({"lang": lang, "rel": u.rel, "import_path": u.import_path,
                            "name": u.name, "files": len(u.files),
                            "test_files": len(u.test_files),
                            "targets": dict(sorted(per.get(rel, Counter()).items())),
                            "error": u.error})
        return out

    def to_dict(self) -> dict:
        targets = []
        for lang in self.languages:
            for t in self.parts[lang].targets:
                d = t.to_dict()
                targets.append(d if "lang" in d else {**d, "lang": lang})
        cobol = {"cobol": self.cobol} if self.cobol is not None else {}
        return {
            "module_root": os.fspath(self.module_root),
            "module": self.module,
            "go_version": self.go_version,
            "languages": self.languages,
            "versions": self.versions(),
            "selection": self.selection.to_dict(),
            "totals": self.totals(),
            "totals_by_language": self.totals_by_language(),
            "packages": self._packages(),
            "targets": targets,
            "skipped": [t for t in targets if t["status"] not in PROCESSED],
            "notes": list(self.notes),
            "unavailable": dict(self.unavailable),
            **cobol,
        }


def _mark_no_toolchain(targets: list[Target]) -> list[Target]:
    return [dataclasses.replace(t, status="no-toolchain") if t.status in PROCESSED else t
            for t in targets]


def _program_files(runners: Runners, sel: Selection) -> set[str] | None:
    """--programs: the COBOL program files defining the requested programs (None = all)."""
    if not sel.programs:
        return None
    if "cobol" not in runners.languages:
        raise ValueError("--programs selects COBOL programs, but COBOL is not among the "
                         "selected languages (pass --source-lang cobol)")
    from codocs.cobol.select import program_files, select_programs
    from codocs.lang.registry import adapter

    index = adapter("cobol", runners.repo_ctx).index(sel.subtree)
    return set(program_files(index, select_programs(index, sel.programs) or []))


def cobol_block(ad, settings: Settings, sel: Selection, targets: list[Target]) -> dict:
    """analysis.json `cobol`: the index summary, the mode per program file and the scope."""
    from codocs.cobol.classify import spec_kind

    index = ad.index(sel.subtree)
    modes: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for t in targets:
        if t.status in ("copybook", "test-support", "duplicate-id", "no-decls"):
            continue
        try:
            modes[t.file] = ad.mode(t.file)
            if modes[t.file] == "spec":
                kinds[t.file] = spec_kind(ad.testability(t.file)) or "unknown"
        except (KeyError, ValueError, AttributeError):
            continue
    return {"summary": index.summary().model_dump(mode="json"),
            "modes": dict(sorted(modes.items())),
            "spec_kinds": dict(sorted(kinds.items())),
            "scope": {"subtree": sel.subtree or None,
                      "exclude_paths": list(settings.exclude_list())}}


def write_cobol_modes(out_dir: Path, block: dict) -> Path:
    """merge this analysis' modes and spec kinds into OUT/cobol/modes.json, which keeps the
    programs of earlier runs (a --programs run analyses only its programs; the report needs
    the mode of every row in OUT)."""
    path = Path(out_dir) / "cobol" / "modes.json"
    old = _read_json(path)
    modes = {**(old.get("modes") or {}), **block["modes"]}
    kinds = {k: v for k, v in (old.get("spec_kinds") or {}).items()
             if modes.get(k) == "spec" and k not in block["modes"]}
    kinds.update(block["spec_kinds"])
    _write_json(path, {"modes": dict(sorted(modes.items())),
                       "spec_kinds": dict(sorted(kinds.items()))})
    return path


def write_cobol_index(ad, out_dir: Path, subtree: str | None) -> Path:
    """OUT/cobol/index.json = CobolRepoIndex.to_dict() of the analysis scope."""
    path = Path(out_dir) / "cobol" / "index.json"
    _write_json(path, ad.index(subtree).to_dict())
    return path


async def analyze_all(runners: Runners, settings: Settings, out_dir: Path | None = None,
                      selection: Selection | None = None, *, write: bool = True
                      ) -> MultiAnalysis:
    """analyze every selected language; the selection applies to each by rel and --max-files
    is one global cap consumed in ALL_LANGS order. writes OUT/analysis.json (multi layout)
    and, with COBOL, OUT/cobol/index.json, unless write=False.

    --programs keeps only the COBOL files defining those programs; other languages' targets
    are then excluded."""
    from codocs.lang.registry import adapter

    sel = selection or Selection()
    det = runners.detection
    root = runners.run_ctx.repo_root
    parts: dict[str, Analysis] = {}
    notes = list(det.notes)
    remaining = sel.max_files
    module = root.name
    wanted_files = _program_files(runners, sel)
    cobol = None
    for lang in runners.languages:
        runner = runners[lang]
        pkgs: dict = {}
        if lang == "go":
            module = runner.module or module
            ad = None
            if det.counts.get("go", 1) or not sel.subtree:
                pkgs = await discover(runner, sel.subtree, sel.packages)
        else:
            if lang in runners.missing_deps:
                notes.append(f"{lang}: not analysed: {MULTILANG_DEPS}")
                continue
            ad = adapter(lang, runners.repo_ctx)
            if det.counts.get(lang, 1) or not sel.subtree:
                pkgs = await ad.discover(runner, sel.subtree, sel.packages)
        if out_dir is not None:
            pkgs = _outside(pkgs, root, Path(out_dir))
        if wanted_files is not None:
            if lang != "cobol":
                pkgs = {}
            else:
                pkgs = {k: u for k, u in pkgs.items() if set(u.files) & wanted_files}
        kw = dict(files=sel.files, max_files=remaining, max_file_lines=settings.max_file_lines)
        if ad is None:
            targets = select_targets(root, pkgs, **kw)
        else:
            targets = ad.select_targets(root, pkgs, **kw)
        if wanted_files is not None:
            targets = [t for t in targets if t.file in wanted_files]
        if lang == "cobol" and ad is not None:
            cobol = cobol_block(ad, settings, sel, targets)
            if out_dir is not None and write:
                write_cobol_index(ad, Path(out_dir), sel.subtree)
                write_cobol_modes(Path(out_dir), cobol)
        if lang in runners.unavailable:
            targets = _mark_no_toolchain(targets)
        if remaining is not None:
            remaining = max(0, remaining - sum(t.status in PROCESSED for t in targets))
        version = ""
        if runner is not None and lang not in runners.unavailable:
            version = runner.version()
        parts[lang] = Analysis(module_root=root, module=module, go_version=version, pkgs=pkgs,
                               targets=targets, selection=sel)
    if not any(p.pkgs for p in parts.values()) and (sel.packages or sel.subtree):
        what = ", ".join(sel.packages or []) or f"--subtree {sel.subtree}"
        langs = ", ".join(runners.languages)
        raise ValueError(f"the selection matched no package ({langs}): {what}")
    analysis = MultiAnalysis(module_root=root, module=module, parts=parts, selection=sel,
                             notes=notes, unavailable=dict(runners.unavailable), cobol=cobol)
    if out_dir is not None and write:
        _write_json(Path(out_dir) / "analysis.json", analysis.to_dict())
    log.info("analyze: %s", "; ".join(analysis.cost_lines()))
    return analysis


async def baseline_all(runners: Runners, analysis: MultiAnalysis, out_dir: Path,
                       force: bool = False, on_progress=None) -> dict[str, dict]:
    """per-language baselines (languages in ALL_LANGS order, one after the other)."""
    out: dict[str, dict] = {}
    for lang, runner in runners.usable():
        if lang in analysis.parts:
            out[lang] = await baseline(runner, analysis.parts[lang], out_dir, force=force,
                                       on_progress=on_progress)
    return out


async def testgen_all(runners: Runners, llm, settings: Settings, analysis: MultiAnalysis,
                      out_dir: Path, force: bool = False, on_progress=None) -> dict[str, list]:
    """TeSearch per language with that language's targets, units and runner."""
    out: dict[str, list] = {}
    for lang, runner in runners.usable():
        if lang in analysis.parts and analysis.work_targets(lang):
            out[lang] = await testgen(runner, llm, settings, analysis.part_for_loops(lang),
                                      out_dir, force=force, on_progress=on_progress)
    return out


async def docgen_all(runners: Runners, llm, settings: Settings, analysis: MultiAnalysis,
                     out_dir: Path, force: bool = False, use_generated_tests: bool = True,
                     on_progress=None) -> dict[str, list]:
    """SpecForge per language with that language's targets, units and runner."""
    out: dict[str, list] = {}
    for lang, runner in runners.usable():
        if lang in analysis.parts and analysis.work_targets(lang):
            out[lang] = await docgen(runner, llm, settings, analysis.part_for_loops(lang),
                                     out_dir, force=force,
                                     use_generated_tests=use_generated_tests,
                                     on_progress=on_progress)
    return out


def _flat(by_lang: dict[str, list]) -> list:
    return [r for lang in ALL_LANGS for r in by_lang.get(lang, [])]


def summarize_all(kind: str, by_lang: dict) -> dict:
    """the stage summary over every language, plus "by_language"."""
    if kind == "baseline":
        merged = {f"{lang}:{rel}": b for lang, bases in by_lang.items()
                  for rel, b in bases.items()}
        info = summarize_baselines(merged)
        info["by_language"] = {lang: summarize_baselines(b) for lang, b in by_lang.items()}
        return info
    fn = summarize_suites if kind == "testgen" else summarize_docs
    info = fn(_flat(by_lang))
    info["by_language"] = {lang: fn(res) for lang, res in by_lang.items()}
    return info


def _rows(results: list) -> list[dict]:
    out = []
    for r in results or []:
        if hasattr(r, "to_dict"):
            out.append(r.to_dict())
        elif isinstance(r, dict):
            out.append(r)
    return out


def _mean_of(rows: list[dict], key: str) -> float | None:
    return round(sum(float(r.get(key) or 0) for r in rows) / len(rows), 4) if rows else None


# COBOL docgen signals that execute tests (the only ones in the overall mean_best_phi)
TEST_SIGNALS = ("tests", "generated-tests")
# COBOL testgen statuses that are not measured runs (outside `ran` and the means)
UNMEASURED = ("spec", "no-toolchain")


def summarize_docs(results: list) -> dict:
    rows = _rows(results)
    ran = [r for r in rows if r.get("rounds")]
    info = {
        "files": len(rows),
        "ran": len(ran),
        "statuses": dict(Counter(str(r.get("status", "")) for r in rows)),
        "mean_best_phi": _mean_of(ran, "best_phi"),
    }
    cobol = [r for r in ran if r.get("lang") == "cobol"]
    if cobol:
        info["mean_best_phi"] = _mean_of(
            [r for r in ran if r.get("lang") != "cobol" or r.get("signal") in TEST_SIGNALS],
            "best_phi")
        by_sig: dict[str, list[dict]] = {}
        for r in cobol:
            by_sig.setdefault(str(r.get("signal") or "?"), []).append(r)
        info["cobol_mean_best_phi_by_signal"] = {
            k: _mean_of(v, "best_phi") for k, v in sorted(by_sig.items())}
    return info


def summarize_suites(results: list) -> dict:
    rows = _rows(results)
    cobol = [r for r in rows if r.get("lang") == "cobol"]
    ran = [r for r in rows if r.get("rounds")
           and not (r.get("lang") == "cobol" and r.get("status") in UNMEASURED)]
    info = {
        "files": len(rows),
        "ran": len(ran),
        "statuses": dict(Counter(str(r.get("status", "")) for r in rows)),
        "mean_best_score": _mean_of(ran, "best_score"),
    }
    if cobol:
        info["spec"] = sum(1 for r in cobol if r.get("status") == "spec")
        info["no_toolchain"] = sum(1 for r in cobol if r.get("status") == "no-toolchain")
    return info


def summarize_baselines(bases: dict) -> dict:
    vals = list(bases.values())
    return {
        "packages": len(vals),
        "build_failed": sum(1 for b in vals if not b.build_ok),
        "timed_out": sum(1 for b in vals if getattr(b, "timed_out", False)),
        "incomplete": sum(1 for b in vals if getattr(b, "incomplete", False)),
        "passing": sum(len(b.passing) for b in vals),
        "failing": sum(len(b.failing) for b in vals),
    }


# -- the judge stage ------------------------------------------------------------------------


def cobol_requested(settings: Settings) -> bool:
    """COBOL was named explicitly (--source-lang cobol, or a list naming it; not auto/all)."""
    raw = (getattr(settings, "source_lang", "auto") or "auto").strip().lower()
    if raw in ("", "auto", "all"):
        return False
    try:
        return "cobol" in (parse_requested(raw) or [])
    except ValueError:
        return False


def cobol_docs_written(results: list, out_dir: Path) -> int:
    """COBOL docgen results of this run whose doc exists in OUT/docs."""
    from codocs.specforge.docfmt import doc_path

    n = 0
    for r in results or []:
        d = r.to_dict() if hasattr(r, "to_dict") else r
        if isinstance(d, dict) and d.get("lang") == "cobol" and d.get("rounds") \
                and doc_path(Path(out_dir), d.get("pkg", ""), d.get("stem", "")).is_file():
            n += 1
    return n


def judge_settings(settings: Settings) -> Settings:
    """the run's settings with the judge model as the model."""
    return dataclasses.replace(settings, model=settings.judge_model or settings.model)


async def judge_docs(index, out_dir: Path, settings: Settings, programs: list[str] | None = None,
                     *, llm_factory=None, calibrate: bool = False, sample: int = 5,
                     repeats: int = 3, on_progress=None) -> dict:
    """judge the COBOL docs of OUT/docs with an LLM built for the judge model; returns the
    stage info (programs, gate_pass, tokens, accuracy, coverage, notes)."""
    from codocs.cobol.judge.run import run_judge

    if llm_factory is None:
        from codocs.llm import LLM as llm_factory
    js = judge_settings(settings)
    async with llm_factory(js) as llm:
        summary = await run_judge(index, out_dir, llm, js, programs, calibrate=calibrate,
                                  sample=sample, repeats=repeats, on_progress=on_progress)
        tokens = _tokens(llm)
        stats = {"calls": int(getattr(llm, "calls", 0)),
                 "cache_hits": int(getattr(llm, "cache_hits", 0))}
    gate = summary.get("gate") or {}
    info = {"programs": len(summary.get("judged") or []), "model": js.model,
            "gate_pass": gate.get("pass", 0) if isinstance(gate, dict) else gate,
            "accuracy": summary.get("accuracy_micro"), "coverage": summary.get("coverage_macro"),
            "high_errors": summary.get("high_errors"), "tokens": tokens, "llm": stats,
            "notes": list(summary.get("notes") or [])}
    if "calibration" in summary:
        info["calibration"] = summary["calibration"]
    return info


def cobol_index_from_out(out_dir: Path, settings: Settings, repo: str | Path | None = None):
    """the COBOL index of an earlier analysis: the repo and the scope (subtree, excludes)
    of OUT/analysis.json `cobol.scope`, never of run_meta.json."""
    from codocs.lang.base import RepoContext
    from codocs.lang.registry import adapter

    out_dir = Path(out_dir)
    analysis = _read_json(out_dir / "analysis.json")
    where = repo or analysis.get("module_root")
    if not where:
        raise FileNotFoundError(f"{out_dir / 'analysis.json'} not found: pass --repo, or run "
                                "codocs analyze/run -o OUT first")
    root = Path(where).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"repository {root} not found (pass --repo)")
    if analysis and "cobol" not in (analysis.get("languages") or []):
        raise ValueError(f"{out_dir / 'analysis.json'} has no COBOL analysis")
    scope = (analysis.get("cobol") or {}).get("scope") or {}
    excludes = scope.get("exclude_paths") if "exclude_paths" in scope \
        else list(settings.exclude_list())
    s = dataclasses.replace(settings, source_lang="cobol", exclude_paths="\n".join(excludes))
    ctx = RepoContext.build(root, ["cobol"], s, out_dir.resolve())
    return adapter("cobol", ctx).index(scope.get("subtree") or None)


def judge_skip_reason(analysis: MultiAnalysis, settings: Settings, skip, docs_written: int
                      ) -> str:
    """why the judge stage does not run ("" = it runs)."""
    if "cobol" not in analysis.languages:
        return "COBOL not selected"
    if "judge" in skip:
        return "skipped"
    if cobol_requested(settings) or docs_written:
        return ""
    return "no COBOL docs produced in this run"


async def run(repo: str | Path, out_dir: Path, settings: Settings,
              selection: Selection | None = None, *, skip: tuple[str, ...] | list[str] = (),
              force: bool = False, use_generated_tests: bool = True, use_tree_docs: bool = True,
              fmt: str = "both", hooks: Hooks | None = None, llm_factory=None,
              refine: bool = True) -> dict:
    """every stage in order; returns run_meta (also written to OUT/run_meta.json)."""
    from codocs.report import build_report

    legacy = sorted(set(skip) & set(LEGACY_SKIPS))
    if legacy:
        raise ValueError("; ".join(f"--skip {n} no longer exists: use {LEGACY_SKIPS[n]}"
                                   for n in legacy))
    unknown = sorted(set(skip) - set(SKIPPABLE))
    if unknown:
        raise ValueError(f"unknown stage(s) to skip: {', '.join(unknown)}")
    hooks = hooks or Hooks()
    out_dir = Path(out_dir)
    sel = selection or Selection()
    det = resolve_languages(repo, settings, sel.subtree, out_dir=out_dir)
    if not det.legacy:
        return await _run_multi(repo, out_dir, settings, sel, det, skip=skip, force=force,
                                use_generated_tests=use_generated_tests,
                                use_tree_docs=use_tree_docs, fmt=fmt, hooks=hooks,
                                llm_factory=llm_factory, refine=refine)
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = make_runner(repo, settings, out_dir)
    root = runner.module_root
    meta: dict = {}
    ran: list[str] = []

    def done(name: str, t0: float, info: dict, llm_stats: dict | None = None,
             accumulate: bool = True) -> None:
        nonlocal meta
        info = {"seconds": round(time.monotonic() - t0, 3), "finished": _now(), **info}
        meta = record_stage(out_dir, settings, name, info, repo=root, llm_stats=llm_stats,
                            accumulate=accumulate)
        ran.append(name)
        hooks.stage_end(name, info)

    t0 = time.monotonic()
    hooks.stage_start("analyze")
    analysis = await analyze(runner, settings, out_dir, sel)
    done("analyze", t0, {"totals": analysis.totals(), "selection": sel.to_dict()})

    if "baseline" not in skip:
        t0 = time.monotonic()
        active = analysis.active_packages()
        hooks.stage_start("baseline", len(active))
        bases = await baseline(runner, analysis, out_dir, force=force,
                               on_progress=hooks.callback("baseline"))
        done("baseline", t0, summarize_baselines(bases))

    llm_stages = [s for s in ("testgen", "docgen") if s not in skip]
    if llm_stages:
        if llm_factory is None:
            from codocs.llm import LLM as llm_factory
        async with llm_factory(settings) as llm:
            for name in llm_stages:
                t0 = time.monotonic()
                meter = TokenMeter(out_dir, llm)
                hooks.stage_start(name, len(analysis.targets))
                try:
                    if name == "testgen":
                        res = await testgen(runner, llm, settings, analysis, out_dir,
                                            force=force,
                                            on_progress=meter.wrap(hooks.callback(name)))
                        info = summarize_suites(res)
                    else:
                        res = await docgen(runner, llm, settings, analysis, out_dir, force=force,
                                           use_generated_tests=use_generated_tests,
                                           on_progress=meter.wrap(hooks.callback(name)))
                        info = summarize_docs(res)
                finally:
                    meter.flush()
                tokens, stats = meter.stage()
                info.update(tokens=tokens, llm=stats)
                done(name, t0, info, llm_stats=stats, accumulate=False)

    if "wiki" not in skip:
        t0 = time.monotonic()
        hooks.stage_start("wiki")
        try:
            res = await wiki(root, out_dir, settings, subtree=sel.subtree,
                             use_tree_docs=use_tree_docs, fmt=fmt,
                             on_progress=hooks.callback("wiki"),
                             packages=wiki_packages(analysis), refine=refine)
            info = {k: res.get(k) for k in ("packages", "leaf_out", "leaf_tree", "static",
                                            "pages", "html", "tokens")}
        except Exception as e:
            log.exception("wiki stage failed")
            info = {"error": f"{type(e).__name__}: {e}"}
        done("wiki", t0, info)

    hooks.stage_start("report")
    summary = build_report(out_dir)
    hooks.stage_end("report", {"docgen_files": summary["docgen"]["aggregate"]["files"],
                               "testgen_files": summary["testgen"]["aggregate"]["files"]})
    return {**meta, RAN_KEY: ran}


def _note(hooks, text: str) -> None:
    fn = getattr(hooks, "note", None)
    if fn is not None:
        fn(text)
    log.info("%s", text)


async def _run_multi(repo: str | Path, out_dir: Path, settings: Settings, sel: Selection,
                     det: Detection, *, skip, force: bool, use_generated_tests: bool,
                     use_tree_docs: bool, fmt: str, hooks: Hooks, llm_factory,
                     refine: bool = True) -> dict:
    """run() in multi mode: every stage once per language, the wiki and the report once."""
    from codocs.report import build_report

    out_dir.mkdir(parents=True, exist_ok=True)
    runners = make_runners(repo, settings, out_dir, det)
    root = runners.run_ctx.repo_root
    meta: dict = {}
    ran: list[str] = []

    def done(name: str, t0: float, info: dict, llm_stats: dict | None = None,
             accumulate: bool = True) -> None:
        nonlocal meta
        info = {"seconds": round(time.monotonic() - t0, 3), "finished": _now(), **info}
        meta = record_stage(out_dir, settings, name, info, repo=root, llm_stats=llm_stats,
                            accumulate=accumulate)
        ran.append(name)
        hooks.stage_end(name, info)

    for text in det.notes:
        _note(hooks, text)
    t0 = time.monotonic()
    hooks.stage_start("analyze")
    analysis = await analyze_all(runners, settings, out_dir, sel)
    done("analyze", t0, {"totals": analysis.totals(), "selection": sel.to_dict(),
                         "languages": analysis.languages,
                         "by_language": analysis.totals_by_language()})

    if "baseline" not in skip:
        t0 = time.monotonic()
        total = sum(len(analysis.parts[lang].active_packages())
                    for lang, _ in runners.usable() if lang in analysis.parts)
        hooks.stage_start("baseline", total)
        bases = await baseline_all(runners, analysis, out_dir, force=force,
                                   on_progress=hooks.callback("baseline"))
        done("baseline", t0, summarize_all("baseline", bases))

    docs_written = 0
    llm_stages = [s for s in ("testgen", "docgen") if s not in skip]
    if llm_stages:
        for text in analysis.cost_lines():
            _note(hooks, text)
        if llm_factory is None:
            from codocs.llm import LLM as llm_factory
        async with llm_factory(settings) as llm:
            for name in llm_stages:
                t0 = time.monotonic()
                meter = TokenMeter(out_dir, llm)
                total = sum(len(analysis.work_targets(lang)) for lang, _ in runners.usable()
                            if lang in analysis.parts)
                hooks.stage_start(name, total)
                cb = meter.wrap(hooks.callback(name))
                try:
                    if name == "testgen":
                        res = await testgen_all(runners, llm, settings, analysis, out_dir,
                                                force=force, on_progress=cb)
                    else:
                        res = await docgen_all(runners, llm, settings, analysis, out_dir,
                                               force=force,
                                               use_generated_tests=use_generated_tests,
                                               on_progress=cb)
                    info = summarize_all(name, res)
                    if name == "docgen":
                        docs_written = cobol_docs_written(res.get("cobol", []), out_dir)
                finally:
                    meter.flush()
                tokens, stats = meter.stage()
                info.update(tokens=tokens, llm=stats)
                done(name, t0, info, llm_stats=stats, accumulate=False)

    reason = judge_skip_reason(analysis, settings, skip, docs_written)
    if "cobol" in analysis.languages and reason != "skipped":
        t0 = time.monotonic()
        hooks.stage_start("judge")
        if reason:
            info = {"skipped": reason}
        else:
            from codocs.lang.registry import adapter

            try:
                index = adapter("cobol", runners.repo_ctx).index(sel.subtree)
                pids = None
                if sel.programs:
                    from codocs.cobol.select import select_programs

                    pids = select_programs(index, sel.programs)
                info = await judge_docs(index, out_dir, settings, pids, llm_factory=llm_factory,
                                        on_progress=hooks.callback("judge"))
            except Exception as e:
                log.exception("judge stage failed")
                info = {"error": f"{type(e).__name__}: {e}"}
        stats = info.get("llm") if isinstance(info.get("llm"), dict) else None
        done("judge", t0, info, llm_stats=stats)

    if "wiki" not in skip:
        t0 = time.monotonic()
        hooks.stage_start("wiki")
        try:
            res = await wiki(root, out_dir, settings, subtree=sel.subtree,
                             use_tree_docs=use_tree_docs, fmt=fmt,
                             on_progress=hooks.callback("wiki"),
                             packages=wiki_packages(analysis), refine=refine)
            info = {k: res.get(k) for k in ("packages", "leaf_out", "leaf_tree", "static",
                                            "pages", "html", "tokens")}
            for k in ("cobol", "grounding"):
                if k in res:
                    info[k] = res[k]
        except Exception as e:
            log.exception("wiki stage failed")
            info = {"error": f"{type(e).__name__}: {e}"}
        done("wiki", t0, info)

    hooks.stage_start("report")
    summary = build_report(out_dir)
    hooks.stage_end("report", {"docgen_files": summary["docgen"]["aggregate"]["files"],
                               "testgen_files": summary["testgen"]["aggregate"]["files"]})
    return {**meta, RAN_KEY: ran}
