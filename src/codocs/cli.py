"""codocs command-line interface."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from codocs import __version__
from codocs import pipeline as pl
from codocs.config import MODEL_ALIASES, Settings
from codocs.lang.treesitter import MissingDependency

console = Console()


# -- option groups ----------------------------------------------------------------


def _stack(*decorators):
    def wrap(f):
        for d in reversed(decorators):
            f = d(f)
        return f
    return wrap


shared_options = _stack(
    click.option("-m", "--model", default=None,
                 help=f"LLM model name or alias ({', '.join(MODEL_ALIASES)}; "
                      f"default: CODOCS_MODEL or {Settings.model})"),
    click.option("-j", "--concurrency", type=int, default=None,
                 help="Files processed in parallel (LLM side)"),
    click.option("--workers", type=int, default=None,
                 help="Concurrent build and test processes (all languages)"),
    click.option("--go", "go_bin", default=None, help="Path to the go binary"),
    click.option("--go-timeout", type=int, default=None, help="Seconds per go test invocation"),
    click.option("--go-env", "go_env", multiple=True,
                 help="Extra env for go as KEY=VALUE (repeatable); {root} expands to the module "
                      "root go runs in, e.g. LD_LIBRARY_PATH={root}/lib"),
    click.option("--go-flags", default=None,
                 help='Extra build flags for go list/test, e.g. "-tags=integration"'),
    click.option("--force", is_flag=True, help="Recompute results that already exist"),
    click.option("--refresh-llm", is_flag=True, default=None,
                 help="Ignore cached LLM replies (fresh replies still update the cache)"),
    click.option("--no-sandbox", "no_sandbox", is_flag=True, default=None,
                 help="Run builds and tests directly in REPO instead of a mirror under "
                      "OUT/work/sandbox/<repo dir name> (tests may then write files into the "
                      "repository)"),
    # multi-language
    click.option("--source-lang", "source_lang", default=None,
                 help="Source languages to process: auto (default: Go only in a Go module, "
                      "else every language found), all, or a comma list of "
                      "go,python,java,c,cpp,cobol"),
    click.option("--test-timeout", type=int, default=None,
                 help="Seconds per non-Go test run (default: --go-timeout)"),
    click.option("--py", "py", default=None,
                 help="Python interpreter that runs the tests (needs pytest and coverage)"),
    click.option("--pytest-args", default=None, help="Extra pytest arguments"),
    click.option("--java-home", default=None, help="JDK to compile and run Java tests with"),
    click.option("--mvn", default=None, help="Path to the mvn binary"),
    click.option("--java-runner", type=click.Choice(["fast", "mvn"]), default=None,
                 help="Java test runner: fast (javac + JUnit launcher) or mvn (surefire)"),
    click.option("--jacoco-version", default=None,
                 help="JaCoCo version for Java coverage (default: picked per class version)"),
    click.option("--cc", "cc", default=None, help="C compiler (default cc)"),
    click.option("--cxx", default=None, help="C++ compiler (default c++)"),
    click.option("--cc-flags", default=None, help="Extra C/C++ compile flags"),
    click.option("--ld-flags", default=None, help="Extra C/C++ link flags"),
    click.option("--cc-db", default=None, help="compile_commands.json with C/C++ flags"),
    click.option("--cc-root", default=None,
                 help="Root dir for --build-cmd/--test-cmd (default: the repository root)"),
    click.option("--build-cmd", default=None,
                 help="Shell command that builds the C/C++ code (compile-only checks)"),
    click.option("--test-cmd", default=None,
                 help="Shell command that runs the existing C/C++ tests (exit 0 = pass)"),
    click.option("--gcov", default=None, help="gcov binary for C/C++ coverage"),
    click.option("--include-generated", is_flag=True, default=None,
                 help="Also process generated files (protobuf, asn1c, ...)"),
    # COBOL
    click.option("--cobc", default=None, help="GnuCOBOL compiler (default cobc)"),
    click.option("--cobc-flags", default=None,
                 help='cobc dialect flags, replacing the per-program dialect probing, e.g. '
                      '"-std=ibm -frelax-syntax-checks"'),
    click.option("--cobol-coverage", type=click.Choice(["statement", "paragraph"]),
                 default=None, help="COBOL test coverage: statement (-ftraceall, default) or "
                                    "paragraph (-ftrace)"),
    click.option("--cobol-spec-signal", type=click.Choice(["facts", "compile"]), default=None,
                 help="Docgen signal of COBOL spec programs (CICS/DB2/IMS/screen): facts "
                      "(default) or compile"),
    click.option("--exclude-path", "exclude_path", multiple=True,
                 help="exclude COBOL files below GLOB from the COBOL index (repeatable)"),
    click.option("--judge-model", default=None,
                 help=f"Model of the COBOL doc judge (default: CODOCS_JUDGE_MODEL or "
                      f"{Settings.judge_model})"),
    click.option("--codegen-model", default=None,
                 help="Model that regenerates COBOL programs from their docs (default: "
                      "CODOCS_CODEGEN_MODEL or --model; COBOL regeneration benefits from your "
                      "strongest model)"),
)

selection_options = _stack(
    click.option("--subtree", default=None, help="Restrict to one subdirectory of the module"),
    click.option("-p", "--packages", default=None,
                 help="Comma-separated package dirs (each includes its subpackages)"),
    click.option("--files", default=None, help="Glob on file paths relative to the module root"),
    click.option("--max-files", type=int, default=None, help="Process at most N files"),
    click.option("--max-file-lines", type=int, default=None,
                 help="Skip files longer than this (status too-large)"),
    click.option("--programs", multiple=True,
                 help="Comma-separated COBOL program ids (exact, then case-insensitive; "
                      "repeatable); other languages' files are then not processed"),
)

doc_options = _stack(
    click.option("--doc-rounds", type=int, default=None, help="Docgen rounds (default 3)"),
    click.option("--doc-threshold", type=float, default=None,
                 help="Docgen early-stop phi (default 0.9)"),
    click.option("--no-generated-tests", is_flag=True,
                 help="Do not use delivered test suites as a docgen signal"),
)

test_options = _stack(
    click.option("--test-rounds", type=int, default=None, help="Testgen rounds (default 3)"),
    click.option("--test-threshold", type=float, default=None,
                 help="Testgen early-stop score (default 0.85)"),
    click.option("--mutants", type=int, default=None,
                 help="Mutants per target file (0 disables mutation)"),
    click.option("--mutant-seed", type=int, default=None, help="Mutant sampling seed"),
)

COBOL_LANG = click.option("-l", "--lang", default=None,
                          help="Prose language of COBOL docs and test plans (en/ja/ko; "
                               "docs of other languages stay English)")


wiki_options = _stack(
    click.option("-l", "--lang", default=None,
                 help="Wiki language (en/ja/ko); also the prose language of COBOL docs "
                      "and test plans"),
    click.option("-f", "--format", "fmt", type=click.Choice(["markdown", "html", "both"]),
                 default="both", help="Wiki output format"),
    click.option("--no-tree-docs", is_flag=True,
                 help="Ignore in-tree markdown_docs/ directories"),
)


def _settings(**kw) -> Settings:
    names = {
        "model": "model", "concurrency": "concurrency", "workers": "workers", "go_bin": "go",
        "go_timeout": "go_timeout", "doc_rounds": "doc_rounds", "doc_threshold": "doc_threshold",
        "test_rounds": "test_rounds", "test_threshold": "test_threshold", "mutants": "mutants",
        "mutant_seed": "mutant_seed", "max_file_lines": "max_file_lines", "lang": "language",
        "refresh_llm": "refresh_llm", "go_flags": "go_flags",
        "source_lang": "source_lang", "test_timeout": "test_timeout", "py": "py",
        "pytest_args": "pytest_args", "java_home": "java_home", "mvn": "mvn",
        "java_runner": "java_runner", "jacoco_version": "jacoco_version", "cc": "cc",
        "cxx": "cxx", "cc_flags": "cc_flags", "ld_flags": "ld_flags", "cc_db": "cc_db",
        "cc_root": "cc_root", "build_cmd": "build_cmd", "test_cmd": "test_cmd",
        "gcov": "gcov", "include_generated": "include_generated",
        "cobc": "cobc", "cobc_flags": "cobc_flags", "cobol_coverage": "cobol_coverage",
        "cobol_spec_signal": "cobol_spec_signal", "judge_model": "judge_model",
        "codegen_model": "codegen_model",
    }
    over = {names[k]: v for k, v in kw.items() if k in names}
    if kw.get("no_sandbox"):
        over["sandbox"] = False
    if kw.get("go_env"):
        over["go_env"] = ";".join(kw["go_env"])
    if kw.get("exclude_path"):
        over["exclude_paths"] = "\n".join(e for e in kw["exclude_path"] if e)
    return Settings.load(**over)


def _selection(kw: dict) -> pl.Selection:
    from codocs.cobol.select import parse_program_list

    pkgs = [p.strip() for p in (kw.get("packages") or "").split(",") if p.strip()]
    programs = parse_program_list(kw.get("programs") or None)
    return pl.Selection(subtree=kw.get("subtree"), packages=pkgs or None,
                        files=kw.get("files"), max_files=kw.get("max_files"),
                        programs=programs or None)


def _fail(msg: str) -> None:
    console.print(f"[red]{escape(str(msg))}[/]")
    raise SystemExit(1)


def _need_key(settings: Settings) -> None:
    if not settings.api_key:
        _fail("No API key configured — this command needs an LLM.\n"
              "Set one with: export OPENAI_API_KEY=... (and OPENAI_BASE_URL for another OpenAI-compatible endpoint)")


def _runner(repo: str, settings: Settings, out: str | Path | None = None):
    try:
        return pl.make_runner(repo, settings, Path(out) if out else None)
    except (pl.GoUnavailable, ValueError) as e:
        _fail(str(e))


def _detect(repo: str | Path, settings: Settings, subtree: str | None = None,
            out: str | Path | None = None):
    """the languages to process; a detection error is a red message."""
    try:
        return pl.resolve_languages(Path(repo).expanduser(), settings, subtree,
                                    out_dir=Path(out) if out else None)
    except ValueError as e:
        _fail(str(e))


def _no_programs_in_go(det, kw: dict) -> None:
    if det.legacy and kw.get("programs"):
        _fail("--programs selects COBOL programs; this repository is processed as Go only "
              "(pass --source-lang cobol or all)")


def _print_notes(det) -> None:
    for note in det.notes:
        console.print(f"[dim]{escape(note)}[/]")


def _runners(repo: str, settings: Settings, out: str | Path | None, det):
    try:
        return pl.make_runners(repo, settings, Path(out) if out else None, det)
    except (pl.GoUnavailable, ValueError) as e:
        _fail(str(e))


def _setup_logging(out_dir: str | Path | None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_codocs", False):
            root.removeHandler(h)
            h.close()
    root.setLevel(logging.INFO)
    con = RichHandler(console=console, level=logging.WARNING, show_path=False)
    con._codocs = True
    root.addHandler(con)
    if out_dir:
        logs = Path(out_dir) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logs / "codocs.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        fh.setLevel(logging.INFO)
        fh._codocs = True
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _guard(fn):
    """turn expected pipeline failures into a red message and exit code 1."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (pl.GoUnavailable, ValueError, FileNotFoundError, MissingDependency) as e:
            _fail(str(e))
    return wrapper


# -- rich progress ------------------------------------------------------------------


class RichHooks(pl.Hooks):
    """one progress bar per stage; per-file results advance it."""

    def __init__(self, progress: Progress):
        self.p = progress
        self.tasks: dict[str, int] = {}
        self.t0: dict[str, float] = {}

    def stage_start(self, name: str, total: int | None = None) -> None:
        self.t0[name] = time.monotonic()
        self.tasks[name] = self.p.add_task(f"[cyan]{name}[/]", total=total)

    def note(self, text: str) -> None:
        self.p.console.print(f"[dim]{escape(text)}[/]")

    def progress(self, name: str, *args) -> None:
        task = self.tasks.get(name)
        if task is None:
            return
        ints = [a for a in args if isinstance(a, int) and not isinstance(a, bool)]
        texts = [a for a in args if isinstance(a, str)]
        objs = [a for a in args if not isinstance(a, (str, int, float, bool)) and a is not None]
        if len(ints) >= 2:
            self.p.update(task, completed=ints[0], total=ints[1])
        label = ""
        for o in objs:
            label = getattr(o, "file", "") or getattr(o, "pkg", "") or label
        counted = next(t for t in self.p.tasks if t.id == task).total is not None
        if objs or (texts and counted and len(ints) < 2):
            self.p.advance(task)
        if texts and not label:
            label = texts[-1].removeprefix(name).strip()
        if label:
            self.p.update(task, description=f"[cyan]{name}[/] {label}"[:120])

    def stage_end(self, name: str, info: dict) -> None:
        task = self.tasks.get(name)
        if task is not None:
            t = next(t for t in self.p.tasks if t.id == task)
            total = t.total if t.total is not None else max(t.completed, 1)
            self.p.update(task, description=f"[green]{name}[/]", total=total, completed=total)
        brief = {k: v for k, v in info.items()
                 if k not in ("finished", "totals", "selection") and v not in (None, {}, [])}
        self.p.console.print(f"[bold green]{name}[/] " + escape(", ".join(
            f"{k}={v}" for k, v in brief.items())))


def _progress(out: Console | None = None) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=out or console,
    )


def _print_analysis(a: pl.Analysis, out: str | None) -> None:
    tot = a.totals()
    console.print(f"[bold green]Module:[/]    {a.module} ({a.module_root})")
    console.print(f"[bold green]Go:[/]        {a.go_version}")
    console.print(f"[bold green]Packages:[/]  {tot['packages']} "
                  f"({tot['packages_with_tests']} with tests)")
    console.print(f"[bold green]Files:[/]     {tot['files']} ({tot['lines']:,} lines)")
    console.print("[bold green]Statuses:[/]  "
                  + ", ".join(f"{k}={v}" for k, v in tot["statuses"].items()))
    table = Table(show_header=True, header_style="bold")
    for col in ("package", "files", "tests", "ok"):
        table.add_column(col, justify="left" if col == "package" else "right")
    per: dict[str, list[int]] = {}
    for t in a.targets:
        row = per.setdefault(t.pkg, [0, 0])
        row[0] += 1
        row[1] += t.status == "ok"
    shown = list(a.pkgs.items())[:40]
    for rel, p in shown:
        files, ok = per.get(rel, [0, 0])
        table.add_row(rel or ".", str(files), str(len(p.all_test_files)), str(ok))
    console.print(table)
    if len(a.pkgs) > len(shown):
        console.print(f"[dim]... and {len(a.pkgs) - len(shown)} more packages[/]")
    if out:
        console.print(f"[bold green]Analysis:[/]  {Path(out) / 'analysis.json'}")


def _print_multi_analysis(a: pl.MultiAnalysis, out: str | None) -> None:
    tot = a.totals()
    console.print(f"[bold green]Repository:[/] {a.module} ({a.module_root})")
    for lang, version in a.versions().items():
        shown = version or f"no toolchain ({a.unavailable.get(lang, 'unavailable')})"
        console.print(f"[bold green]{lang}:[/] {escape(shown)}")
    for line in a.cost_lines():
        console.print(f"  {escape(line)}")
    console.print(f"[bold green]Packages:[/]  {tot['packages']} "
                  f"({tot['packages_with_tests']} with tests)")
    console.print(f"[bold green]Files:[/]     {tot['files']} ({tot['lines']:,} lines)")
    console.print("[bold green]Statuses:[/]  "
                  + ", ".join(f"{k}={v}" for k, v in tot["statuses"].items()))
    table = Table(show_header=True, header_style="bold")
    for col in ("lang", "package", "files", "tests", "ok"):
        table.add_column(col, justify="left" if col in ("lang", "package") else "right")
    rows = []
    for lang in a.languages:
        part = a.parts[lang]
        per: dict[str, list[int]] = {}
        for t in part.targets:
            row = per.setdefault(t.pkg, [0, 0])
            row[0] += 1
            row[1] += t.status == "ok"
        for rel, p in part.pkgs.items():
            files, ok = per.get(rel, [0, 0])
            rows.append((lang, rel or ".", str(files), str(len(p.all_test_files)), str(ok)))
    for row in rows[:40]:
        table.add_row(*row)
    console.print(table)
    if len(rows) > 40:
        console.print(f"[dim]... and {len(rows) - 40} more packages[/]")
    if out:
        console.print(f"[bold green]Analysis:[/]  {Path(out) / 'analysis.json'}")


def _print_cobol_summary(index, top: int = 15) -> None:
    """the COBOL analyze tables: counts, edges and the most depended-upon programs."""
    from codocs.cobol.wiki.planner import role_label

    s = index.summary()
    ep = index.entrypoints()
    table = Table(title=f"COBOL: {index.name}", show_header=True, header_style="bold")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Programs", s.programs),
        ("Copybooks", s.copybooks),
        ("JCL jobs", s.jcl_jobs),
        ("BMS mapsets", s.bms_mapsets),
        ("COBOL lines", f"{s.cobol_loc:,}"),
        ("Program call edges", s.call_edges),
        ("Data stores", len(index.datastores)),
        ("CICS transactions", len(index.transactions)),
        ("Entrypoints", ", ".join(f"{k} {len(v)}" for k, v in ep.items()) or "-"),
        ("Unresolved calls", len(s.unresolved_calls)),
        ("Unresolved dynamic calls", len(s.unresolved_dynamic)),
        ("Missing copybooks", len(s.missing_copybooks)),
        ("Warnings", len(s.warnings)),
    ):
        table.add_row(label, str(value))
    console.print(table)
    edges = index.edge_counts()
    if edges:
        console.print("[bold]Graph edges:[/] "
                      + ", ".join(f"{k} {v}" for k, v in sorted(edges.items())))
    if index.programs and top > 0:
        ranking = Table(title="Most depended-upon programs", header_style="bold")
        ranking.add_column("Program", style="cyan")
        ranking.add_column("Role")
        ranking.add_column("Path", style="dim")
        ranking.add_column("Callers", justify="right")
        ranking.add_column("Callees", justify="right")
        for pid, _score in index.rank_programs()[:top]:
            prog = index.programs[pid]
            ranking.add_row(pid, role_label(index, pid), prog.path,
                            str(len(index.callers(pid))), str(len(index.callees(pid))))
        console.print(ranking)
    for label, items in (("Unresolved calls", s.unresolved_calls),
                         ("Missing copybooks", s.missing_copybooks)):
        if items:
            more = f" (+{len(items) - 12} more)" if len(items) > 12 else ""
            console.print(f"[yellow]{label}:[/] {escape(', '.join(items[:12]))}{more}")
    if s.warnings:
        console.print(f"[yellow]Warnings:[/] {len(s.warnings)} "
                      f"(first: {escape(s.warnings[0])})")


# -- commands ---------------------------------------------------------------------


@click.group()
@click.version_option(__version__, prog_name="codocs")
def cli():
    """Codocs - bounded-loop docs, unit tests, a repo wiki and business logic for Go, Python,
    Java, C, C++ and COBOL repositories."""


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", default=None,
              help="Output directory (writes analysis.json; with COBOL also cobol/index.json)")
@click.option("--top", type=int, default=15, show_default=True,
              help="COBOL programs to list in the ranking table")
@selection_options
@shared_options
@_guard
def analyze(repo: str, output: str | None, top: int, **kw):
    """List packages and target files (no LLM, no tests run); COBOL also gets the static
    analysis summary (programs, copybooks, JCL, BMS, call graph)."""
    _setup_logging(output)
    settings = _settings(**kw)
    det = _detect(repo, settings, kw.get("subtree"), output)
    _print_notes(det)
    _no_programs_in_go(det, kw)
    if not det.legacy:
        runners = _runners(repo, settings, output, det)
        with console.status("[bold cyan]Analyzing repository..."):
            ma = asyncio.run(pl.analyze_all(runners, settings, Path(output) if output else None,
                                            _selection(kw)))
        _print_multi_analysis(ma, output)
        if "cobol" in ma.languages:
            from codocs.lang.registry import adapter

            _print_cobol_summary(adapter("cobol", runners.repo_ctx).index(kw.get("subtree")),
                                 top)
            if output:
                console.print(f"[bold green]COBOL index:[/] "
                              f"{Path(output) / 'cobol' / 'index.json'}")
        return
    runner = _runner(repo, settings)
    with console.status("[bold cyan]Analyzing module..."):
        a = asyncio.run(pl.analyze(runner, settings, Path(output) if output else None,
                                   _selection(kw)))
    _print_analysis(a, output)


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", required=True, help="Output directory")
@selection_options
@shared_options
@_guard
def baseline(repo: str, output: str, **kw):
    """Run the existing tests on the original code and record the passing tests per package."""
    _setup_logging(output)
    settings = _settings(**kw)
    det = _detect(repo, settings, kw.get("subtree"), output)
    _print_notes(det)
    _no_programs_in_go(det, kw)
    if not det.legacy:
        _multi_baseline(repo, output, settings, det, kw)
        return
    runner = _runner(repo, settings, output)
    out = Path(output)

    async def go():
        a = await pl.analyze(runner, settings, out, _selection(kw))
        pkgs = a.active_packages()
        with _progress() as progress:
            hooks = RichHooks(progress)
            t0 = time.monotonic()
            hooks.stage_start("baseline", len(pkgs))
            bases = await pl.baseline(runner, a, out, force=kw["force"],
                                      on_progress=hooks.callback("baseline"))
            info = {"seconds": round(time.monotonic() - t0, 3), **pl.summarize_baselines(bases)}
            pl.record_stage(out, settings, "baseline", info, repo=a.module_root)
            hooks.stage_end("baseline", info)
        return bases

    bases = asyncio.run(go())
    broken = [b for b in bases.values() if not b.build_ok]
    for b in broken[:20]:
        first = b.build_errors[0][:200] if b.build_errors else ""
        console.print(f"[yellow]build failed:[/] {escape(b.pkg or '.')}"
                      + (f"  {escape(first)}" if first else ""))
    flagged = [b for b in bases.values() if b.build_ok and (b.timed_out or b.incomplete)]
    for b in flagged[:20]:
        what = "timed out" if b.timed_out else "incomplete"
        console.print(f"[yellow]{what}:[/] {b.pkg or '.'} ({len(b.passing)} passing, "
                      f"{len(b.failing)} failing; rerun to retry)")
    console.print(f"[bold green]Baselines:[/] {out / 'baseline'}/")


def _print_baselines(bases: dict) -> None:
    broken = [b for b in bases.values() if not b.build_ok]
    for b in broken[:20]:
        first = b.build_errors[0][:200] if b.build_errors else ""
        lang = getattr(b, "lang", "go")
        where = (b.pkg or ".") if lang == "go" else f"{lang}:{b.pkg or '.'}"
        console.print(f"[yellow]build failed:[/] {escape(where)}"
                      + (f"  {escape(first)}" if first else ""))
    flagged = [b for b in bases.values() if b.build_ok and (b.timed_out or b.incomplete)]
    for b in flagged[:20]:
        what = "timed out" if b.timed_out else "incomplete"
        console.print(f"[yellow]{what}:[/] {b.pkg or '.'} ({len(b.passing)} passing, "
                      f"{len(b.failing)} failing; rerun to retry)")
    for b in bases.values():
        for note in getattr(b, "notes", [])[:5]:
            console.print(f"[dim]{getattr(b, 'lang', 'go')}:{escape(b.pkg or '.')}: "
                          f"{escape(note)}[/]")


def _multi_baseline(repo: str, output: str, settings: Settings, det, kw: dict) -> None:
    out = Path(output)
    runners = _runners(repo, settings, output, det)

    async def go():
        a = await pl.analyze_all(runners, settings, out, _selection(kw))
        total = sum(len(a.parts[lang].active_packages()) for lang, _ in runners.usable()
                    if lang in a.parts)
        with _progress() as progress:
            hooks = RichHooks(progress)
            t0 = time.monotonic()
            hooks.stage_start("baseline", total)
            bases = await pl.baseline_all(runners, a, out, force=kw["force"],
                                          on_progress=hooks.callback("baseline"))
            info = {"seconds": round(time.monotonic() - t0, 3),
                    **pl.summarize_all("baseline", bases)}
            pl.record_stage(out, settings, "baseline", info, repo=a.module_root)
            hooks.stage_end("baseline", info)
        return bases

    by_lang = asyncio.run(go())
    _print_baselines({f"{lang}:{rel}": b for lang, bases in by_lang.items()
                      for rel, b in bases.items()})
    console.print(f"[bold green]Baselines:[/] {out / 'baseline'}/")


def _multi_llm_stage(name: str, repo: str, output: str, settings: Settings, det,
                     kw: dict) -> dict:
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    runners = _runners(repo, settings, output, det)

    async def go():
        from codocs.llm import LLM

        a = await pl.analyze_all(runners, settings, out, _selection(kw))
        for line in a.cost_lines():
            console.print(f"[dim]{escape(line)}[/]")
        async with LLM(settings) as llm:
            with _progress() as progress:
                hooks = RichHooks(progress)
                t0 = time.monotonic()
                total = sum(len(a.work_targets(lang)) for lang, _ in runners.usable()
                            if lang in a.parts)
                hooks.stage_start(name, total)
                meter = pl.TokenMeter(out, llm)
                cb = meter.wrap(hooks.callback(name))
                try:
                    if name == "testgen":
                        res = await pl.testgen_all(runners, llm, settings, a, out,
                                                   force=kw["force"], on_progress=cb)
                    else:
                        res = await pl.docgen_all(
                            runners, llm, settings, a, out, force=kw["force"],
                            use_generated_tests=not kw.get("no_generated_tests"),
                            on_progress=cb)
                    info = pl.summarize_all(name, res)
                finally:
                    meter.flush()
                tokens, stats = meter.stage()
                info = {"seconds": round(time.monotonic() - t0, 3), **info,
                        "tokens": tokens, "llm": stats}
                pl.record_stage(out, settings, name, info, repo=a.module_root, llm_stats=stats,
                                accumulate=False)
                hooks.stage_end(name, info)
        return res

    return asyncio.run(go())


def _llm_stage(name: str, repo: str, output: str, kw: dict) -> list:
    _setup_logging(output)
    settings = _settings(**kw)
    _need_key(settings)
    det = _detect(repo, settings, kw.get("subtree"), output)
    _print_notes(det)
    _no_programs_in_go(det, kw)
    if not det.legacy:
        by_lang = _multi_llm_stage(name, repo, output, settings, det, kw)
        res = [r for rs in by_lang.values() for r in rs]
        _report_tail(name, Path(output))
        return res
    runner = _runner(repo, settings, output)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)

    async def go():
        from codocs.llm import LLM

        a = await pl.analyze(runner, settings, out, _selection(kw))
        async with LLM(settings) as llm:
            with _progress() as progress:
                hooks = RichHooks(progress)
                t0 = time.monotonic()
                hooks.stage_start(name, len(a.targets))
                meter = pl.TokenMeter(out, llm)
                cb = meter.wrap(hooks.callback(name))
                try:
                    if name == "testgen":
                        res = await pl.testgen(runner, llm, settings, a, out, force=kw["force"],
                                               on_progress=cb)
                        info = pl.summarize_suites(res)
                    else:
                        res = await pl.docgen(runner, llm, settings, a, out, force=kw["force"],
                                              use_generated_tests=not kw.get("no_generated_tests"),
                                              on_progress=cb)
                        info = pl.summarize_docs(res)
                finally:
                    meter.flush()
                tokens, stats = meter.stage()
                info = {"seconds": round(time.monotonic() - t0, 3), **info,
                        "tokens": tokens, "llm": stats}
                pl.record_stage(out, settings, name, info, repo=a.module_root, llm_stats=stats,
                                accumulate=False)
                hooks.stage_end(name, info)
        return res

    res = asyncio.run(go())
    _report_tail(name, out)
    return res


def _report_tail(name: str, out: Path) -> None:
    from codocs.report import build_report

    build_report(out)
    tok = pl._read_json(out / "run_meta.json").get("stages", {}).get(name, {}).get("tokens", {})
    if tok.get("input") or tok.get("output"):
        console.print(f"[dim]Tokens used: {tok.get('input', 0):,} in / "
                      f"{tok.get('output', 0):,} out[/]")
    console.print(f"[bold green]Report:[/] {out / 'report.md'}")


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", required=True, help="Output directory")
@selection_options
@test_options
@COBOL_LANG
@shared_options
@_guard
def testgen(repo: str, output: str, **kw):
    """Generate a unit-test suite per file (TeSearch bounded loop; COBOL spec programs get
    a one-shot test specification)."""
    _llm_stage("testgen", repo, output, kw)


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", required=True, help="Output directory")
@selection_options
@doc_options
@COBOL_LANG
@shared_options
@_guard
def docgen(repo: str, output: str, **kw):
    """Generate a Markdown doc per file (SpecForge bounded loop, scored by phi)."""
    _llm_stage("docgen", repo, output, kw)


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", required=True, help="Output directory (wiki goes to OUT/wiki)")
@click.option("--subtree", default=None, help="Restrict the wiki to one subdirectory")
@wiki_options
@click.option("--plan-only", is_flag=True, help="Static analysis + page plan only, no LLM calls")
@click.option("--no-refine", is_flag=True,
              help="Keep the deterministic page plan (no plan-refinement LLM call)")
@shared_options
@_guard
def wiki(repo: str, output: str, subtree: str | None, fmt: str, no_tree_docs: bool,
         plan_only: bool, no_refine: bool, **kw):
    """Build the RepoWiki architecture wiki from OUT/docs (then in-tree docs, then static)."""
    _setup_logging(output)
    settings = _settings(**kw)
    root = Path(repo).expanduser().resolve()
    det = _detect(root, settings, subtree, output)
    _print_notes(det)
    if not det.legacy:
        for lang, n in det.counts.items():
            console.print(f"[dim]{lang}: {n} source files[/]")
    if not plan_only:
        _need_key(settings)
    out = Path(output)

    async def go():
        if plan_only:
            return await pl.wiki(root, out, settings, subtree=subtree,
                                 use_tree_docs=not no_tree_docs, fmt=fmt, plan_only=True,
                                 refine=not no_refine)
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      console=console) as progress:
            task = progress.add_task("Generating wiki...", total=None)
            t0 = time.monotonic()
            res = await pl.wiki(root, out, settings, subtree=subtree,
                                use_tree_docs=not no_tree_docs, fmt=fmt,
                                on_progress=lambda m: progress.update(task, description=m),
                                refine=not no_refine)
        info = {"seconds": round(time.monotonic() - t0, 3),
                **{k: res.get(k) for k in ("packages", "leaf_out", "leaf_tree", "static",
                                           "pages", "html", "tokens") if k in res},
                **{k: res[k] for k in ("cobol", "grounding") if k in res}}
        pl.record_stage(out, settings, "wiki", info, repo=root)
        return res

    res = asyncio.run(go())
    console.print(f"[bold green]Module:[/]    {res.get('module', '')}")
    console.print(f"[bold green]Packages:[/]  {res['packages']} "
                  f"(generated docs {res['leaf_out']}, in-tree docs {res['leaf_tree']}, "
                  f"static {res['static']})")
    console.print(f"[bold green]Plan:[/]      {res.get('sections', 0)} sections, "
                  f"{res['pages']} pages")
    if plan_only:
        for section in res.get("plan") or []:
            console.print(f"[bold]{section['title']}[/]")
            for page in section["pages"]:
                console.print(f"  - {page['title']} ({page['packages']} packages)")
        return
    if fmt in ("markdown", "both"):
        console.print(f"[bold green]Markdown wiki:[/] {out / 'wiki'}/")
    if res.get("html"):
        console.print(f"[bold green]HTML wiki:[/] {res['html']}")
    tok = res.get("tokens") or {}
    if tok.get("input") or tok.get("output"):
        console.print(f"[dim]Tokens used: {tok['input']:,} in / {tok['output']:,} out[/]")


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", required=True, help="Output directory")
@selection_options
@test_options
@doc_options
@wiki_options
@click.option("--skip", default="", help="Comma-separated stages to skip: "
              + ",".join(pl.SKIPPABLE))
@click.option("--no-refine", is_flag=True,
              help="Keep the deterministic wiki page plan (no plan-refinement LLM call)")
@shared_options
@_guard
def run(repo: str, output: str, skip: str, fmt: str, no_tree_docs: bool,
        no_generated_tests: bool, no_refine: bool, **kw):
    """Run every stage: analyze, baseline, testgen, docgen, judge (COBOL), wiki, report."""
    skipped = tuple(s.strip() for s in skip.split(",") if s.strip())
    legacy = [s for s in skipped if s in pl.LEGACY_SKIPS]
    if legacy:
        _fail("; ".join(f"--skip {s} no longer exists: use {pl.LEGACY_SKIPS[s]}"
                        for s in legacy))
    unknown = sorted(set(skipped) - set(pl.SKIPPABLE))
    if unknown:
        _fail(f"unknown stage(s) for --skip: {', '.join(unknown)} "
              f"(choose from {', '.join(pl.SKIPPABLE)})")
    _setup_logging(output)
    settings = _settings(**kw)
    det = _detect(repo, settings, kw.get("subtree"), output)
    _no_programs_in_go(det, kw)
    judge_runs = ("judge" not in skipped and "cobol" in det.languages
                  and (pl.cobol_requested(settings) or "docgen" not in skipped))
    if any(s not in skipped for s in ("testgen", "docgen", "wiki")) or judge_runs:
        _need_key(settings)
    if det.legacy:
        _print_notes(det)
        _runner(repo, settings)
    out = Path(output)

    async def go():
        with _progress() as progress:
            return await pl.run(
                repo, out, settings, _selection(kw), skip=skipped, force=kw["force"],
                use_generated_tests=not no_generated_tests, use_tree_docs=not no_tree_docs,
                fmt=fmt, hooks=RichHooks(progress), refine=not no_refine,
            )

    meta = asyncio.run(go())
    tok = meta.get("tokens") or {}
    if tok.get("input") or tok.get("output"):
        console.print(f"[dim]Tokens used (cumulative): {tok.get('input', 0):,} in / "
                      f"{tok.get('output', 0):,} out[/]")
    # only this run's stages: run_meta.json keeps entries of earlier runs (e.g. a skipped judge)
    stages = meta.get("stages") or {}
    failed = [name for name in meta.get(pl.RAN_KEY, stages)
              if isinstance(stages.get(name), dict) and stages[name].get("error")]
    for name in failed:
        console.print(f"[red]{name} failed: {escape(str(meta['stages'][name]['error'])[:300])}[/]")
    console.print(f"[bold green]Report:[/] {out / 'report.md'}")
    if failed:
        raise SystemExit(2)


@cli.command()
@click.argument("repo")
@click.option("-o", "--output", required=True,
              help="Output directory (results go to OUT/bizlogic/k<K>/)")
@click.option("-k", "--clusters", "clusters", multiple=True, type=int,
              help="Number of clusters K, the granularity knob (repeatable: -k 10 -k 40). "
                   "Fewer clusters -> fewer trajectories survive deduplication -> coarser "
                   "business logic. Default 20")
@click.option("--subtree", default=None, help="Restrict to one subdirectory of the module")
@click.option("--wiki-dir", type=click.Path(exists=True, file_okay=False), default=None,
              help="Existing RepoWiki markdown wiki to extend with a Business Logic section")
@click.option("--docs", "docs_dirs", multiple=True, type=click.Path(exists=True, file_okay=False),
              help="Extra root holding markdown_docs/ trees (OUT/docs and REPO are always used)")
@click.option("--resolution", type=float, default=1.0, show_default=True,
              help="Louvain resolution of the base communities")
@click.option("--alpha", type=float, default=0.6, show_default=True,
              help="Weight of call structure vs doc/identifier text in the hierarchy")
@click.option("--pkg-weight", type=float, default=0.5, show_default=True,
              help="Weight of the package tree (subsystem boundaries) in the hierarchy")
@click.option("--utility-fanin", type=int, default=40, show_default=True,
              help="Functions with at least this many callers are utilities (skipped)")
@click.option("--infra-callers", type=int, default=40, show_default=True,
              help="Packages called from at least this many packages are infrastructure "
                   "(all their functions are utilities; 0 = off)")
@click.option("--keep-accessors", is_flag=True, help="Do not fold tiny Get/Set/Is/Has methods")
@click.option("--max-roots", type=int, default=300, show_default=True,
              help="Entry roots to walk from (largest reach first; 0 = all)")
@click.option("--min-reach", type=int, default=3, show_default=True,
              help="A root must reach at least this many business functions")
@click.option("--group-depth", type=int, default=1, show_default=True,
              help="Roots are shared among subsystems = first N path segments, in "
                   "proportion to their candidate roots")
@click.option("--exclude", "exclude", multiple=True,
              help="Glob on file paths to leave out (repeatable). Replaces the default: "
                   "test-support dirs (*/ut/*, */stub/*, */mock/*, */test/*, ...); pass "
                   "--exclude '' to keep everything")
@click.option("--exclude-extra", "exclude_extra", multiple=True,
              help="Glob on file paths to leave out in addition to the default (repeatable)")
@click.option("--walks", type=int, default=20, show_default=True, help="Random walks per root")
@click.option("--walk-length", type=int, default=12, show_default=True,
              help="Maximum functions per walk")
@click.option("--max-trajectories", type=click.IntRange(min=0), default=None,
              help="Cap on trajectories sent to the LLM per K (most walks through the "
                   "whole sequence first; 0 = all)")
@click.option("--min-share", type=float, default=0.02, show_default=True,
              help="A cluster sequence needs MIN_SHARE * walks / K walks to be its own "
                   "trajectory; rarer ones are folded into the closest kept one")
@click.option("--no-subsume", is_flag=True,
              help="Keep trajectories whose cluster sequence is a prefix of another one")
@click.option("--seed", type=int, default=0, show_default=True)
@click.option("-l", "--lang", default=None, help="Output language (en/ja/ko)")
@click.option("--plan-only", is_flag=True,
              help="Graph, clusters, roots, walks and dedup only (no LLM)")
@click.option("--domain-profile", "domain_profile", default=None, metavar="NAME|PATH",
              help="Domain vocabulary for prompts and diagrams: a built-in profile name "
                   "(e.g. 3gpp-5gc) or a JSON file (see docs/bizlogic.md). Default: none "
                   "(domain-neutral)")
@shared_options
@_guard
def bizlogic(repo: str, output: str, clusters: tuple[int, ...], subtree: str | None,
             wiki_dir: str | None, docs_dirs: tuple[str, ...], resolution: float, alpha: float,
             pkg_weight: float, utility_fanin: int, infra_callers: int, keep_accessors: bool,
             max_roots: int, min_reach: int, group_depth: int, exclude: tuple[str, ...],
             exclude_extra: tuple[str, ...], walks: int, walk_length: int,
             max_trajectories: int | None, min_share: float, no_subsume: bool,
             seed: int, plan_only: bool, domain_profile: str | None, **kw):
    """Infer business logic: dependency graph -> hierarchical clusters -> roots ->
    random-walk trajectories -> dedup by cluster granularity K -> LLM per trajectory."""
    from codocs.bizlogic.graph import DEFAULT_EXCLUDE
    from codocs.bizlogic.profile import load_profile
    from codocs.bizlogic.run import BizParams, run_bizlogic

    profile = load_profile(domain_profile)            # fail early on a bad profile
    _setup_logging(output)
    settings = _settings(**kw)
    root = Path(repo).expanduser().resolve()
    det = _detect(root, settings, subtree, output)       # an empty dir still says "no go.mod"
    _print_notes(det)
    if not plan_only:
        _need_key(settings)
    base_exclude = tuple(e for e in exclude if e) if exclude else DEFAULT_EXCLUDE
    params = BizParams(
        clusters=tuple(clusters) or (20,), resolution=resolution, alpha=alpha,
        pkg_weight=pkg_weight, utility_fanin=utility_fanin, infra_callers=infra_callers,
        fold_accessors=not keep_accessors,
        max_roots=max_roots or None, min_reach=min_reach, group_depth=group_depth,
        exclude=base_exclude + tuple(e for e in exclude_extra if e),
        walks_per_root=walks,
        walk_length=walk_length, prefix_subsume=not no_subsume, min_share=min_share,
        max_trajectories=max_trajectories or None, seed=seed)

    async def go():
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      console=console) as progress:
            task = progress.add_task("Business logic...", total=None)
            return await run_bizlogic(
                root, Path(output), settings, params, subtree=subtree,
                docs_roots=[Path(d) for d in docs_dirs],
                wiki_dir=Path(wiki_dir) if wiki_dir else None, plan_only=plan_only,
                on_progress=lambda m: progress.update(task, description=m), profile=profile)

    res = asyncio.run(go())
    g = res["graph"]
    console.print(f"[bold green]Graph:[/]     {g['functions'] + g['methods']} functions, "
                  f"{g['edges']} edges, {g['utilities']} utilities, {g['documented']} documented")
    console.print(f"[bold green]Hierarchy:[/] {res['base_communities']} base communities; "
                  f"{res['roots']} roots; {res['walks']} walks")
    table = Table(title="Business logic per granularity")
    for col in ("K", "clusters", "cluster sequences", "trajectories", "inferred", "html"):
        table.add_column(col)
    for k, r in res["per_k"].items():
        kname = str(k) if r.get("requested_k", k) == k else f"{k} (asked {r['requested_k']})"
        table.add_row(kname, str(r["clusters"]), str(r["dedup"]["distinct_signatures"]),
                      str(r["trajectories"]), str(r["inferred"]), r["html"])
    console.print(table)
    if res["tokens"]["input"] or res["tokens"]["output"]:
        console.print(f"[dim]Tokens used: {res['tokens']['input']:,} in / "
                      f"{res['tokens']['output']:,} out[/]")


@cli.command("bizlogic-render")
@click.argument("out_dir")
@click.option("-k", "--clusters", "clusters", multiple=True, type=int,
              help="Granularity to re-render (repeatable). Default: every OUT/bizlogic/k<K>")
@click.option("--repo", type=click.Path(exists=True, file_okay=False), default=None,
              help="Go module the results came from: function locations for the cluster "
                   "pages when OUT/bizlogic/k<K>/nodes.json is missing (no LLM)")
@click.option("--system-name", default=None,
              help="Name of the system under study (default: the wiki's project name)")
@click.option("--domain-profile", "domain_profile", default=None, metavar="NAME|PATH",
              help="Domain profile for the diagrams: a built-in name or a JSON file; 'none' "
                   "= domain-neutral. Default: the profile the run stored "
                   "(OUT/bizlogic/domain_profile.json), else none")
@_guard
def bizlogic_render(out_dir: str, clusters: tuple[int, ...], repo: str | None,
                    system_name: str | None, domain_profile: str | None):
    """Rebuild the business-logic wiki pages and HTML from stored JSON (no LLM)."""
    from codocs.bizlogic.profile import load_profile
    from codocs.bizlogic.run import render_from_json

    profile = load_profile(domain_profile) if domain_profile is not None else None
    base = Path(out_dir) / "bizlogic"
    if not base.is_dir():
        _fail(f"{base} is not a directory")
    ks = list(clusters) or sorted(int(p.name[1:]) for p in base.glob("k*")
                                  if p.is_dir() and p.name[1:].isdigit())
    if not ks:
        _fail(f"no k<K> results under {base}")
    for k in ks:
        kdir = base / f"k{k}"
        if not (kdir / "clusters.json").is_file():
            _fail(f"{kdir} has no clusters.json")
        html = render_from_json(kdir, k, repo=Path(repo) if repo else None,
                                system_name=system_name, profile=profile)
        console.print(f"[bold green]K={k}:[/] {html}")


@cli.command()
@click.argument("out_dir")
def report(out_dir: str):
    """Rebuild OUT/report.md and OUT/summary.json from the result.json files."""
    from codocs.report import build_report

    out = Path(out_dir)
    if not out.is_dir():
        _fail(f"{out_dir} is not a directory")
    summary = build_report(out)
    for name, label, key in (("docgen", "Docs", "mean_best_phi"),
                             ("testgen", "Tests", "mean_best_score")):
        agg = summary[name]["aggregate"]
        mean = agg.get(key)
        console.print(
            f"[bold green]{label}:[/] {agg['files']} files, {agg['ran']} run, "
            f"{key}={'-' if mean is None else f'{mean:.3f}'}, "
            f"early stop rate={'-' if agg['early_stop_rate'] is None else agg['early_stop_rate']}"
        )
    console.print(f"[bold green]Report:[/] {out / 'report.md'}")


@cli.command()
@click.argument("out_dir")
@click.option("--repo", type=click.Path(exists=True, file_okay=False), default=None,
              help="Repository the docs were generated from (default: from OUT/analysis.json)")
@click.option("--programs", multiple=True,
              help="Comma-separated program ids to judge (repeatable)")
@click.option("--judge-model", default=None,
              help=f"Judge model (default: CODOCS_JUDGE_MODEL or {Settings.judge_model}, i.e. "
                   "not the generator's model)")
@click.option("--calibrate", is_flag=True,
              help="Also measure the judge: injected-error sensitivity and run-to-run stability")
@click.option("--sample", type=int, default=5, show_default=True,
              help="Docs used for calibration (spread over program sizes)")
@click.option("--repeats", type=int, default=3, show_default=True,
              help="Judgements per calibration doc for the stability check")
@click.option("-j", "--concurrency", type=int, default=None, help="Parallel LLM calls")
@_guard
def judge(out_dir: str, repo: str | None, programs: tuple[str, ...], judge_model: str | None,
          calibrate: bool, sample: int, repeats: int, concurrency: int | None):
    """Score the COBOL docs in OUT/docs against the source (LLM judge + deterministic checks)."""
    from codocs.cobol.select import parse_program_list, select_programs

    out = Path(out_dir)
    _setup_logging(out)
    settings = _settings(judge_model=judge_model, concurrency=concurrency)
    _need_key(settings)
    index = pl.cobol_index_from_out(out, settings, repo)
    pids = select_programs(index, parse_program_list(programs)) if programs else None
    console.print(f"[bold green]Judge:[/] {settings.judge_model}  [bold green]Docs:[/] "
                  f"{out / 'docs'}")
    t0 = time.monotonic()
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console) as progress:
        task = progress.add_task("Judging COBOL docs...", total=None)
        info = asyncio.run(pl.judge_docs(
            index, out, settings, pids, calibrate=calibrate, sample=sample, repeats=repeats,
            on_progress=lambda m: progress.update(task, description=str(m))))
    info = {"seconds": round(time.monotonic() - t0, 3), **info}
    pl.record_stage(out, settings, "judge", info, llm_stats=info.get("llm"))
    summary = pl._read_json(out / "judge" / "summary.json")
    g = summary.get("gate") or {}

    def pct(v):
        return "-" if v is None else f"{v * 100:.0f}%"

    console.print(
        f"[bold green]Docs judged:[/] {summary.get('docs', 0)}  gate pass {g.get('pass', 0)} / "
        f"fail {g.get('fail', 0)}  [bold green]Accuracy:[/] {pct(summary.get('accuracy_micro'))}"
        f" of {summary.get('claims', 0)} claims ({summary.get('high_errors', 0)} high-severity "
        f"errors)  [bold green]Coverage:[/] {pct(summary.get('coverage_macro'))}  "
        f"[bold green]Names:[/] {pct(summary.get('name_precision_macro'))}")
    if "calibration" in summary:
        c = summary["calibration"]
        console.print(f"[bold green]Calibration:[/] sensitivity {pct(c.get('sensitivity'))} "
                      f"over {c.get('mutations')} injected errors")
    tok = info.get("tokens") or {}
    if tok.get("input") or tok.get("output"):
        console.print(f"[dim]Tokens used: {tok.get('input', 0):,} in / "
                      f"{tok.get('output', 0):,} out[/]")
    console.print(f"[bold green]Report:[/] {out / 'judge' / 'summary.md'}")


@cli.command()
@click.argument("out_dir")
@click.option("--repo", type=click.Path(exists=True, file_okay=False), default=None,
              help="Repository the tests were generated from (default: from OUT/analysis.json)")
@click.option("--programs", multiple=True,
              help="Comma-separated COBOL program ids (only their suites are re-run; "
                   "repeatable)")
@click.option("--workers", type=int, default=None, help="Parallel test runs")
@click.option("--test-timeout", type=int, default=None, help="Seconds per test run")
@click.option("--cobc", default=None, help="GnuCOBOL compiler (default: as recorded in OUT)")
@_guard
def testrun(out_dir: str, repo: str | None, programs: tuple[str, ...], workers: int | None,
            test_timeout: int | None, cobc: str | None):
    """Re-run every delivered test suite in OUT/tests (no LLM) into OUT/testrun."""
    from codocs.cobol.select import parse_program_list
    from codocs.cobol.testrun import restore_settings, run_testrun

    out = Path(out_dir)
    if not out.is_dir():
        _fail(f"{out_dir} is not a directory")
    _setup_logging(out)
    settings = _settings(workers=workers, test_timeout=test_timeout, cobc=cobc)
    keep = tuple(k for k, v in (("test_timeout", test_timeout), ("cobc", cobc)) if v)
    settings = restore_settings(settings, out, keep=keep)
    with console.status("[bold cyan]Running tests..."):
        summary = asyncio.run(run_testrun(out, settings, repo,
                                          parse_program_list(programs) or None))
    table = Table(title="Test re-run", header_style="bold")
    for col in ("language", "suites", "built", "passed", "failed", "mean coverage"):
        table.add_column(col, justify="left" if col == "language" else "right")
    for lang, s in summary.get("by_language", {}).items():
        cov = s.get("mean_coverage")
        table.add_row(lang, str(s.get("suites")), str(s.get("build_ok")),
                      str(s.get("tests_passed")), str(s.get("tests_failed")),
                      "-" if cov is None else f"{cov * 100:.1f}%")
    console.print(table)
    cob = summary.get("cobol")
    if cob:
        def pct(v):
            return "-" if v is None else f"{v * 100:.1f}%"

        console.print(f"[bold green]COBOL:[/] {cob['programs']} programs ("
                      + ", ".join(f"{k} {v}" for k, v in sorted(cob["modes"].items()))
                      + f"), compiled {cob['compiled']}/{cob['executable']} "
                      f"({pct(cob['compile_rate'])}), checks {cob['cases_passed']} passed / "
                      f"{cob['cases_failed']} failed, paragraph coverage "
                      f"{pct(cob['mean_paragraph_coverage'])}, statement coverage "
                      f"{pct(cob.get('mean_statement_coverage'))}")
        if cob.get("no_toolchain"):
            console.print(f"[yellow]COBOL: {cob['no_toolchain']} unit/batch programs not run: "
                          f"cobc not found or not runnable ({escape(str(cob.get('cobc')))})[/]")
    console.print(f"[bold green]Results:[/] {out / 'testrun'}/ (summary.md)")


@cli.command(hidden=True, context_settings={"ignore_unknown_options": True,
                                            "allow_extra_args": True})
def phi():
    """Removed: phi is the docgen loop's score."""
    console.print("[red]phi is now the docgen loop's score: run codocs docgen (best_phi in "
                  "OUT/docgen/**/result.json); choose the regeneration model with "
                  "--codegen-model[/]")
    raise SystemExit(2)


@cli.command(hidden=True, context_settings={"ignore_unknown_options": True,
                                            "allow_extra_args": True})
def leafdoc():
    """Removed: one-shot leaf docs are the docgen loop."""
    console.print("[red]codocs leafdoc was removed: run codocs docgen REPO -o OUT (bounded "
                  "doc loop) and codocs testgen REPO -o OUT (tests)[/]")
    raise SystemExit(2)


def _glossary(wiki_dir: Path, choice: str) -> str:
    """auto: cobol for a COBOL wiki (program-index.md, or analysis.json naming cobol)."""
    if choice != "auto":
        return choice
    if (wiki_dir / "program-index.md").is_file():
        return "cobol"
    langs = pl._read_json(wiki_dir.parent / "analysis.json").get("languages")
    if isinstance(langs, list) and "cobol" in langs:
        return "cobol"
    return "go"


@cli.command()
@click.argument("wiki_dir")
@click.option("-o", "--output", required=True, help="Output directory for the translated wiki")
@click.option("-l", "--lang", default="ja", help="Target language code (ja/ko/...)")
@click.option("-m", "--model", default=None, help="LLM model name or alias")
@click.option("-j", "--concurrency", type=int, default=None,
              help="Pages translated in parallel (--glossary cobol only)")
@click.option("--no-html", is_flag=True, help="Skip regenerating the HTML viewer")
@click.option("--glossary", type=click.Choice(["auto", "go", "cobol"]), default="auto",
              show_default=True,
              help="Translator: go (RepoWiki) or cobol (COBOL glossary: CICS, JCL, VSAM, ...); "
                   "auto picks cobol for COBOL wikis")
def translate(wiki_dir: str, output: str, lang: str, model: str | None,
              concurrency: int | None, no_html: bool, glossary: str):
    """Translate an exported markdown wiki into another language (structure-preserving)."""
    src = Path(wiki_dir)
    kind = _glossary(src, glossary)
    if kind == "go" and concurrency is not None:
        console.print("[dim]-j applies only to --glossary cobol; ignored[/]")
        concurrency = None
    settings = _settings(model=model, concurrency=concurrency)
    _need_key(settings)
    if not src.is_dir() or not list(src.glob("*.md")):
        _fail(f"{wiki_dir} is not a markdown wiki directory.")
    asyncio.run(_translate_wiki(src, Path(output), lang, settings, no_html, kind))


async def _translate_wiki(src: Path, dst: Path, lang: str, settings: Settings,
                          no_html: bool, kind: str) -> None:
    """translate with the COBOL glossary (kind cobol) or the RepoWiki translator (go); both
    use codocs' settings and the per-model, per-language wiki cache."""
    import dataclasses
    import re

    if kind == "cobol":
        from codocs.cobol.wiki.translator import WikiTranslator, build_wiki_from_dir
    else:
        from codocs.repowiki.deepdoc.translator import WikiTranslator, build_wiki_from_dir
    from codocs.repowiki.core.cache import Cache
    from codocs.repowiki.export.html import export_html, load_local_mermaid
    from codocs.repowiki.llm.client import LLMClient
    from codocs.wiki import wiki_cache_path

    llm = LLMClient(model=settings.model, api_key=settings.api_key, api_base=settings.api_base)
    path = wiki_cache_path(dataclasses.replace(settings, language=lang))
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = Cache(path)
    await cache.init()
    try:
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      console=console) as progress:
            task = progress.add_task(f"Translating wiki to '{lang}'...", total=None)
            translator = WikiTranslator(llm, cache, target_lang=lang,
                                        concurrency=settings.concurrency,
                                        on_progress=lambda m: progress.update(task,
                                                                              description=m))
            report = await translator.translate_dir(src, dst)
    finally:
        await cache.close()
    console.print(f"[bold green]Translated wiki:[/] {dst}/ ({len(report.files_ok)} pages)")
    if report.files_fallback:
        console.print("[yellow]Kept in source language (failed validation):[/] "
                      + ", ".join(report.files_fallback))
    if report.mermaid_fallback:
        console.print(f"[yellow]Mermaid diagrams kept in source language:[/] "
                      f"{report.mermaid_fallback}")
    if not no_html:
        wiki = build_wiki_from_dir(dst)
        htmls = list(src.glob("*.html"))
        if len(htmls) == 1:
            name = htmls[0].name
        else:
            name = (re.sub(r"[^a-zA-Z0-9._-]+", "-", wiki.project_name).strip("-")
                    or "wiki") + ".html"
        out_path = dst / name
        export_html(wiki, out_path, branding=False, mermaid_js=load_local_mermaid())
        console.print(f"[bold green]HTML wiki:[/] {out_path}")
    if llm.total_input_tokens or llm.total_output_tokens:
        console.print(f"[dim]Tokens used: {llm.total_input_tokens:,} in / "
                      f"{llm.total_output_tokens:,} out[/]")
