"""specforge bounded loop: doc -> regenerate the file from the doc -> run the tests -> refine.

the language comes from the runner (adapter_for): Go calls the codocs.golang functions; other
languages call the same-named members of their adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from codocs.baseline import Baseline, package_baseline
from codocs.config import Settings
from codocs.golang.gotool import GoRunner, GoTestResult
from codocs.golang.package import (
    PackageInfo,
    Target,
    fix_imports,
    package_context,
    package_identifiers,
    relevant_tests,
)
from codocs.golang.source import (
    Import,
    apply_cgo_header,
    cgo_header,
    extract_go_code,
    imports,
    test_functions,
)
from codocs.lang.registry import adapter_for
from codocs.llm import LLMError
from codocs.specforge.docfmt import clean_doc, doc_path, missing_sections
from codocs.specforge.prompts import codegen_messages, refine_messages, seed_messages

log = logging.getLogger(__name__)

DOC_STATUSES = ("ok", "no-funcs")
MAX_IMPORT_FIXES = 2
CHARS_PER_TOKEN = 2.5
CODEGEN_TOKEN_MARGIN = 8000
_FIX_STD = ("bytes", "context", "errors", "fmt", "io", "math", "os", "sort", "strconv",
            "strings", "sync", "time")


@dataclass
class DocEval:
    signal: str
    build_ok: bool
    phi: float
    relevant: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    failing: dict[str, str] = field(default_factory=dict)
    build_errors: list[str] = field(default_factory=list)
    import_fixes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DocEval:
        return cls(
            signal=d.get("signal", ""),
            build_ok=bool(d.get("build_ok", False)),
            phi=float(d.get("phi", 0.0)),
            relevant=list(d.get("relevant") or []),
            passed=list(d.get("passed") or []),
            failing=dict(d.get("failing") or {}),
            build_errors=list(d.get("build_errors") or []),
            import_fixes=int(d.get("import_fixes", 0)),
        )


@dataclass
class DocRound:
    round: int
    doc: str
    code: str
    eval: DocEval
    missing: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "round": self.round, "doc": self.doc, "code": self.code,
            "eval": self.eval.to_dict(), "missing": list(self.missing), "seconds": self.seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DocRound:
        return cls(
            round=int(d.get("round", 0)),
            doc=d.get("doc", ""),
            code=d.get("code", ""),
            eval=DocEval.from_dict(d.get("eval") or {}),
            missing=list(d.get("missing") or []),
            seconds=float(d.get("seconds", 0.0)),
        )


@dataclass
class DocResult:
    pkg: str
    file: str
    stem: str
    status: str
    signal: str = ""
    rounds: list[DocRound] = field(default_factory=list)
    best_round: int = 0
    best_phi: float = 0.0
    early_stop: bool = False
    threshold: float = 0.0
    max_rounds: int = 0
    error: str = ""
    lang: str = "go"

    @property
    def phi_trajectory(self) -> list[float]:
        return [r.eval.phi for r in self.rounds]

    @property
    def best(self) -> DocRound | None:
        return next((r for r in self.rounds if r.round == self.best_round), None)

    def to_dict(self) -> dict:
        lang = {} if self.lang == "go" else {"lang": self.lang}
        return {
            "pkg": self.pkg, "file": self.file, "stem": self.stem, **lang, "status": self.status,
            "signal": self.signal, "rounds": [r.to_dict() for r in self.rounds],
            "best_round": self.best_round, "best_phi": self.best_phi,
            "early_stop": self.early_stop, "threshold": self.threshold,
            "max_rounds": self.max_rounds, "error": self.error,
            "phi_trajectory": self.phi_trajectory,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DocResult:
        return cls(
            pkg=d.get("pkg", ""),
            file=d.get("file", ""),
            stem=d.get("stem", ""),
            status=d.get("status", ""),
            signal=d.get("signal", ""),
            rounds=[DocRound.from_dict(r) for r in d.get("rounds") or []],
            best_round=int(d.get("best_round", 0)),
            best_phi=float(d.get("best_phi", 0.0)),
            early_stop=bool(d.get("early_stop", False)),
            threshold=float(d.get("threshold", 0.0)),
            max_rounds=int(d.get("max_rounds", 0)),
            error=d.get("error", ""),
            lang=d.get("lang", "go") or "go",
        )


# -- paths and io ---------------------------------------------------------------


def _dir_name(pkg: str) -> str:
    return pkg.strip("/") or "_root"


def result_dir(out_dir: Path, pkg: str, stem: str) -> Path:
    """OUT/docgen/<pkg>/<stem>/ (root package: _root)."""
    return Path(out_dir) / "docgen" / _dir_name(pkg) / stem


def result_path(out_dir: Path, pkg: str, stem: str) -> Path:
    return result_dir(out_dir, pkg, stem) / "result.json"


def suite_rel(pkg: str, stem: str) -> str:
    """rel path a generated suite takes inside its package."""
    name = f"{stem}_codocs_test.go"
    return f"{pkg}/{name}" if pkg else name


def delivered_suite_path(out_dir: Path, pkg: str, stem: str, basename: str | None = None
                         ) -> Path:
    """OUT/tests/<pkg>/<stem>_codocs_test.go (root package: _root); basename overrides the
    file name (non-Go: adapter.suite_basename(stem))."""
    name = basename if basename is not None else f"{stem}_codocs_test.go"
    return Path(out_dir) / "tests" / _dir_name(pkg) / name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _prompt_text(system: str, user: str) -> str:
    return f"=== SYSTEM ===\n{system}\n=== USER ===\n{user}"


def load_result(out_dir: Path, pkg: str, stem: str) -> DocResult | None:
    try:
        d = json.loads(result_path(out_dir, pkg, stem).read_text(encoding="utf-8"))
        return DocResult.from_dict(d)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_result(out_dir: Path, result: DocResult) -> None:
    """result.json, plus the best round's doc under OUT/docs/ (a stale doc is removed)."""
    _write_json(result_path(out_dir, result.pkg, result.stem), result.to_dict())
    best = result.best
    path = doc_path(out_dir, result.pkg, result.stem)
    if best is not None and best.doc.strip():
        _write(path, best.doc)
    else:
        path.unlink(missing_ok=True)


MAX_REPLY_TOKENS = 64000


def doc_max_tokens(source: str, max_tokens: int) -> int:
    """a doc runs about as long as the code it describes (often longer): scale with the file."""
    need = int(1.5 * len(source) / CHARS_PER_TOKEN) + CODEGEN_TOKEN_MARGIN
    return min(MAX_REPLY_TOKENS, max(int(max_tokens), need))


def codegen_max_tokens(source: str, max_tokens: int) -> int:
    """completion budget for regenerating a file: enough for its size plus reasoning."""
    need = int(len(source) / CHARS_PER_TOKEN) + CODEGEN_TOKEN_MARGIN
    return min(MAX_REPLY_TOKENS, max(int(max_tokens), need))


def _fix_allowed(source_imports: list[Import]) -> list[Import]:
    names = {i.name for i in source_imports}
    paths = {i.path for i in source_imports}
    extra = [Import(path=p) for p in _FIX_STD if p not in paths and p not in names]
    return list(source_imports) + extra


# -- the loop -------------------------------------------------------------------


@dataclass
class _Signal:
    name: str
    relevant: list[str]
    overlay: dict[str, str | None]


class SpecForgeLoop:
    """per file: seed, then refine from the best round until phi reaches the threshold."""

    def __init__(self, runner: GoRunner, llm, settings: Settings, out_dir: Path,
                 module_root: Path, use_generated_tests: bool = True):
        self.runner = runner
        self.llm = llm
        self.settings = settings
        self.out_dir = Path(out_dir)
        self.module_root = Path(module_root)
        self.use_generated_tests = use_generated_tests
        self.lang = adapter_for(runner)

    @property
    def is_go(self) -> bool:
        return self.lang.name == "go"

    def _forced(self, target: Target, pkg: PackageInfo) -> str | None:
        """the signal the language imposes on a target (optional hook doc_signal), else None."""
        hook = None if self.is_go else getattr(self.lang, "doc_signal", None)
        return hook(target, pkg) if hook is not None else None

    def _custom(self) -> dict:
        """signals the language evaluates itself (optional hook custom_signals)."""
        return {} if self.is_go else dict(getattr(self.lang, "custom_signals", None) or {})

    def _signal(self, target: Target, pkg: PackageInfo, baseline: Baseline,
                generated_suite: str | None, forced: str | None = None) -> _Signal:
        if forced is None:
            forced = self._forced(target, pkg)
        custom = self._custom()
        if forced is not None:
            if forced in custom:
                return _Signal(forced, list(self.lang.signal_ids(forced, target)), {})
            hide = {} if baseline.build_ok else {f: None for f in pkg.all_test_files}
            return _Signal(forced, [], hide)
        if self.is_go:
            relevant = relevant_tests(self.module_root, pkg, target.file, set(baseline.passing))
        else:
            relevant = self.lang.relevant_tests(self.module_root, pkg, target.file,
                                                set(baseline.passing))
        if relevant:
            return _Signal("tests", relevant, {})
        if self.is_go:
            srel = suite_rel(pkg.rel, target.stem)
            delivered = delivered_suite_path(self.out_dir, pkg.rel, target.stem)
        else:
            srel = self.lang.suite_rel(pkg, target.stem)
            delivered = delivered_suite_path(self.out_dir, pkg.rel, target.stem,
                                             basename=self.lang.suite_basename(target.stem))
        if generated_suite is None and self.use_generated_tests:
            if delivered.is_file():
                generated_suite = delivered.read_text(encoding="utf-8", errors="replace")
        if not generated_suite:
            names = []
        elif self.is_go:
            names = test_functions(generated_suite)
        else:
            names = self.lang.test_names(generated_suite, srel)
        if names:
            overlay: dict[str, str | None] = {f: None for f in pkg.all_test_files}
            overlay[srel] = generated_suite
            return _Signal("generated-tests", sorted(names), overlay)
        fallback = "compile" if self.is_go else getattr(self.lang, "fallback_signal", "compile")
        if fallback in custom:
            return _Signal(fallback, list(self.lang.signal_ids(fallback, target)), {})
        hide = {} if baseline.build_ok else {f: None for f in pkg.all_test_files}
        return _Signal("compile", [], hide)

    async def _run(self, pkg: PackageInfo, target: Target, sig: _Signal, code: str
                   ) -> GoTestResult:
        overlay = dict(sig.overlay)
        overlay[target.file] = code
        if sig.name == "compile":
            return await self.runner.test(pkg.rel, overlay=overlay, compile_only=True)
        return await self.runner.test(pkg.rel, overlay=overlay, run=sig.relevant)

    async def evaluate(self, pkg: PackageInfo, target: Target, sig: _Signal, code: str,
                       allowed: list[Import]) -> tuple[DocEval, str]:
        """overlay the regenerated file and score it; returns the eval and the code as run."""
        if sig.name in self._custom():
            return await self.lang.signal_eval(sig.name, self.runner, pkg, target, code), code
        fixes = 0
        res = await self._run(pkg, target, sig, code)
        repair = fix_imports if self.is_go else self.lang.fix_imports
        runtime = None if self.is_go or sig.name == "compile" else \
            getattr(self.lang, "runtime_import_errors", None)
        while fixes < MAX_IMPORT_FIXES:
            if not res.build_ok:
                errors = list(res.build_errors)
            elif runtime is not None:
                errors = runtime(res, target.file)      # a NameError at run time (Python)
            else:
                break
            if not errors:
                break
            fixed = repair(code, errors, PurePosixPath(target.file).name, allowed)
            if fixed is None:
                break
            code = fixed
            fixes += 1
            res = await self._run(pkg, target, sig, code)
        if sig.name == "compile":
            phi = 1.0 if res.build_ok else 0.0
            ev = DocEval(signal=sig.name, build_ok=res.build_ok, phi=phi,
                         build_errors=list(res.build_errors), import_fixes=fixes)
            return ev, code
        if not res.build_ok:
            ev = DocEval(signal=sig.name, build_ok=False, phi=0.0, relevant=list(sig.relevant),
                         build_errors=list(res.build_errors), import_fixes=fixes)
            return ev, code
        passed = [t for t in sig.relevant if res.tests.get(t) == "pass"]
        failing = {
            t: res.output.get(t) or ("timed out" if res.timed_out else "did not run")
            for t in sig.relevant if res.tests.get(t) != "pass"
        }
        phi = len(passed) / len(sig.relevant) if sig.relevant else 0.0
        ev = DocEval(signal=sig.name, build_ok=True, phi=phi, relevant=list(sig.relevant),
                     passed=passed, failing=failing, import_fixes=fixes)
        return ev, code

    async def run_file(self, target: Target, pkg: PackageInfo, baseline: Baseline,
                       generated_suite: str | None = None) -> DocResult:
        """bounded doc loop for one file; writes artifacts, result.json and the final doc."""
        s = self.settings
        max_rounds = max(1, int(s.doc_rounds))
        result = DocResult(pkg=target.pkg, file=target.file, stem=target.stem,
                           status=target.status, threshold=s.doc_threshold, max_rounds=max_rounds,
                           lang=self.lang.name)
        if target.status not in DOC_STATUSES:
            write_result(self.out_dir, result)
            return result
        result.status = "ok"
        forced = self._forced(target, pkg)
        custom = self._custom()
        static = forced is not None and forced in custom and \
            not custom[forced].get("executes", True)
        if not static and _toolchain_failed(self.runner, baseline.build_ok,
                                            baseline.build_errors):
            result.status = "no-toolchain"        # no LLM calls; recomputed by a later run
            result.error = baseline.build_errors[0][:500]
            write_result(self.out_dir, result)
            return result

        rdir = result_dir(self.out_dir, target.pkg, target.stem)
        for old in rdir.glob("round_*"):
            shutil.rmtree(old, ignore_errors=True)
        lang = self.lang
        go = self.is_go
        if go:
            source = (self.module_root / target.file).read_text(encoding="utf-8",
                                                                errors="replace")
            context = package_context(self.module_root, pkg, target.file)
            taken = package_identifiers(self.module_root, pkg, exclude_rel=target.file)
            header = cgo_header(source)
            allowed = imports(source)
            fix_allowed = _fix_allowed(allowed)
        else:
            source = lang.read(target.file, self.module_root)
            context = lang.package_context(self.module_root, pkg, target.file)
            taken = lang.package_identifiers(self.module_root, pkg, exclude_rel=target.file)
            header = lang.preamble(source)
            allowed = lang.imports(source)
            fix_allowed = lang.fix_allowed(allowed)
        code_name = "code" + (PurePosixPath(target.file).suffix or ".go")
        code_tokens = codegen_max_tokens(source, s.max_tokens)
        doc_tokens = doc_max_tokens(source, s.max_tokens)
        sig = self._signal(target, pkg, baseline, generated_suite, forced)
        result.signal = sig.name
        codegen_kw: dict = {}
        model = getattr(s, "codegen_model", "") or ""
        if not go and model and getattr(lang, "uses_codegen_model", False):
            codegen_kw["model"] = model

        best: DocRound | None = None
        for r in range(1, max_rounds + 1):
            t0 = time.monotonic()
            rd = rdir / f"round_{r}"
            try:
                if best is None:
                    seed = seed_messages if go else lang.doc_seed_messages
                    system, user = seed(target.file, pkg, source, context)
                else:
                    last = result.rounds[-1]
                    refine = refine_messages if go else lang.doc_refine_messages
                    system, user = refine(
                        target.file, pkg, source, best.doc, best.code, best.eval, best.missing,
                        last=None if last is best else last.eval, last_round=last.round,
                        attempt=r)
                _write(rd / "prompt_doc.txt", _prompt_text(system, user))
                reply = await self.llm.complete(system, user, tag=f"doc {target.file} r{r}",
                                                max_tokens=doc_tokens)
                _write(rd / "reply_doc.txt", reply)
                doc = clean_doc(reply)
                if doc and not go:
                    doc = getattr(lang, "finalize_doc", _same_doc)(doc)
                doc = doc or (best.doc if best else "")
                _write(rd / "doc.md", doc)
                if go:
                    missing = missing_sections(doc, source)
                    system, user = codegen_messages(target.file, pkg, doc, context, allowed,
                                                    taken=taken, cgo=bool(header))
                else:
                    missing = missing_sections(doc, source, lang.name, lang)
                    system, user = lang.doc_codegen_messages(target.file, pkg, doc, context,
                                                             allowed, taken=taken,
                                                             has_preamble=bool(header))
                _write(rd / "prompt_code.txt", _prompt_text(system, user))
                reply = await self.llm.complete(system, user, tag=f"code {target.file} r{r}",
                                                temperature=0.0, max_tokens=code_tokens,
                                                **codegen_kw)
                _write(rd / "reply_code.txt", reply)
            except LLMError as e:
                log.warning("docgen %s: round %d: %s", target.file, r, e)
                result.status = "error"
                result.error = f"round {r}: {e}"
                break
            if go:
                code = apply_cgo_header(extract_go_code(reply), header)
            else:
                code = lang.apply_preamble(lang.extract_code(reply), header)
                code = getattr(lang, "finalize_code", _same_code)(target.file, code)
            ev, code = await self.evaluate(pkg, target, sig, code, fix_allowed)
            _write(rd / code_name, code)
            _write_json(rd / "eval.json", ev.to_dict())
            rnd = DocRound(round=r, doc=doc, code=code, eval=ev, missing=missing,
                           seconds=round(time.monotonic() - t0, 3))
            result.rounds.append(rnd)
            if best is None or ev.phi > best.eval.phi:
                best = rnd
            log.info("docgen %s: round %d phi=%.3f (%s)", target.file, r, ev.phi, sig.name)
            if _toolchain_failed(self.runner, ev.build_ok, ev.build_errors):
                result.status, result.error = "no-toolchain", ev.build_errors[0][:500]
                log.warning("docgen %s: %s", target.file, result.error)
                break
            if ev.phi >= s.doc_threshold:
                result.early_stop = r < max_rounds
                break

        if best is not None:
            result.best_round = best.round
            result.best_phi = best.eval.phi
        write_result(self.out_dir, result)
        return result


def _same_doc(doc: str) -> str:
    return doc


def _same_code(rel: str, code: str) -> str:
    return code


def _toolchain_failed(runner, build_ok: bool, errors: list[str]) -> bool:
    """a failed build caused by the toolchain (no JDK/mvn, ...), per the runner's own rule."""
    fn = getattr(runner, "toolchain_failure", None)
    return bool(fn is not None and not build_ok and errors and fn(errors))


def _progress(res: DocResult, resumed: bool = False) -> str:
    if res.status not in ("ok", "error"):
        return f"docgen {res.file}: skipped ({res.status})"
    text = (f"docgen {res.file}: {res.status} phi={res.best_phi:.2f} "
            f"rounds={len(res.rounds)}/{res.max_rounds} signal={res.signal}")
    if res.early_stop:
        text += " early-stop"
    if res.error:
        text += f" error={res.error}"
    if resumed:
        text += " (resumed)"
    return text


async def run_docgen(targets: list[Target], pkgs: dict[str, PackageInfo], runner, llm,
                     settings: Settings, out_dir: Path, module_root: Path, force: bool = False,
                     on_progress=None, use_generated_tests: bool = True) -> list[DocResult]:
    """specforge over many files: baselines first, then files concurrently (resumable)."""
    out_dir = Path(out_dir)
    loop = SpecForgeLoop(runner, llm, settings, out_dir, module_root,
                         use_generated_tests=use_generated_tests)
    results: list[DocResult | None] = [None] * len(targets)
    pending: list[int] = []
    for i, t in enumerate(targets):
        if t.status in DOC_STATUSES and not force:
            prev = load_result(out_dir, t.pkg, t.stem)
            if prev is not None and prev.status == "ok":
                results[i] = prev
                if on_progress:
                    on_progress(_progress(prev, resumed=True))
                continue
        pending.append(i)

    involved = sorted({targets[i].pkg for i in pending
                       if targets[i].status in DOC_STATUSES and targets[i].pkg in pkgs})
    computed = await asyncio.gather(*(package_baseline(runner, pkgs[p], out_dir) for p in involved))
    baselines = dict(zip(involved, computed))

    sem = asyncio.Semaphore(max(1, int(settings.concurrency)))

    async def one(i: int) -> None:
        t = targets[i]
        async with sem:
            try:
                if t.status in DOC_STATUSES and t.pkg not in pkgs:
                    raise KeyError(f"package {t.pkg!r} not discovered")
                pkg = pkgs.get(t.pkg) or PackageInfo(rel=t.pkg, import_path="", name="")
                base = baselines.get(t.pkg) or Baseline(pkg=t.pkg, build_ok=True)
                res = await loop.run_file(t, pkg, base)
            except Exception as e:
                log.exception("docgen %s failed", t.file)
                res = DocResult(pkg=t.pkg, file=t.file, stem=t.stem, status="error",
                                threshold=settings.doc_threshold,
                                max_rounds=max(1, int(settings.doc_rounds)),
                                error=f"{type(e).__name__}: {e}", lang=loop.lang.name)
                write_result(out_dir, res)
        results[i] = res
        if on_progress:
            on_progress(_progress(res))

    await asyncio.gather(*(one(i) for i in pending))
    return [r for r in results if r is not None]
