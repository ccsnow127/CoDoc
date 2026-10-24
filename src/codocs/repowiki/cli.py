"""repowiki command-line interface."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from codocs.repowiki import __version__
from codocs.repowiki.config import Config, resolve_model
from codocs.repowiki.ingest.github import parse_git_url

console = Console()


def _is_url(s: str) -> bool:
    return s.startswith("http") or parse_git_url(s) is not None


@click.group()
@click.version_option(__version__, prog_name="repowiki")
def cli():
    """RepoWiki - generate wiki documentation for any codebase."""
    pass


@cli.command()
@click.argument("path_or_url")
@click.option("-o", "--output", default=None, help="Output directory (default: ./wiki)")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["markdown", "json", "html"]),
    default="markdown",
    help="Output format",
)
@click.option("-l", "--lang", default=None, help="Output language (en/ja/ko)")
@click.option("-m", "--model", default=None, help="LLM model name or alias")
@click.option("--open", "open_browser", is_flag=True, help="Open HTML output in browser")
def scan(
    path_or_url: str,
    output: str | None,
    fmt: str,
    lang: str | None,
    model: str | None,
    open_browser: bool,
):
    """Scan a local directory or GitHub URL and generate wiki documentation."""
    cfg = Config.load()
    if lang:
        cfg.language = lang
    if model:
        cfg.model = resolve_model(model)
    if output:
        cfg.output_dir = output

    with console.status("[bold cyan]Scanning project..."):
        if _is_url(path_or_url):
            from codocs.repowiki.ingest.github import ingest_github

            project = ingest_github(
                path_or_url,
                max_file_size=cfg.max_file_size,
                max_files=cfg.max_files,
            )
        else:
            from codocs.repowiki.ingest.local import ingest_local

            project = ingest_local(
                path_or_url,
                max_file_size=cfg.max_file_size,
                max_files=cfg.max_files,
            )

    # display scan results
    console.print()
    console.print(f"[bold green]Project:[/] {project.name}")
    console.print(f"[bold green]Files:[/]   {len(project.files)}")
    console.print(f"[bold green]Lines:[/]   {project.total_lines:,}")

    # language breakdown
    lang_counts: dict[str, int] = {}
    for f in project.files:
        lang_counts[f.language] = lang_counts.get(f.language, 0) + 1

    if lang_counts:
        table = Table(title="Languages", show_header=True, header_style="bold")
        table.add_column("Language", style="cyan")
        table.add_column("Files", justify="right")
        for language, count in sorted(lang_counts.items(), key=lambda x: -x[1])[:10]:
            table.add_row(language, str(count))
        console.print(table)

    # file tree (top 30 entries)
    tree_widget = Tree(f"[bold]{project.name}/[/]")
    _build_rich_tree(tree_widget, project.files, max_entries=30)
    console.print(tree_widget)

    # if we have an API key, run the LLM analysis
    if not cfg.api_key:
        console.print()
        console.print(
            "[yellow]No API key configured. Showing scan results only.[/]\n"
            "Set one with: [bold]export OPENAI_API_KEY=...[/] "
            "(plus OPENAI_BASE_URL for another OpenAI-compatible endpoint)."
        )
        return

    import asyncio

    asyncio.run(_run_analysis(project, cfg, fmt, open_browser))


async def _run_analysis(project, cfg: Config, fmt: str, open_browser: bool):
    """run the full LLM analysis pipeline."""
    from codocs.repowiki.core.analyzer import Analyzer
    from codocs.repowiki.core.cache import Cache
    from codocs.repowiki.llm.client import LLMClient

    llm = LLMClient(model=cfg.model, api_key=cfg.api_key, api_base=cfg.api_base)
    cache = Cache()
    await cache.init()

    analyzer = Analyzer(llm=llm, cache=cache, language=cfg.language, concurrency=cfg.concurrency)

    from rich.progress import Progress, SpinnerColumn, TextColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Analyzing project...", total=None)

        def on_progress(step: str):
            progress.update(task, description=step)

        wiki_data = await analyzer.analyze(project, on_progress=on_progress)

    # export
    from codocs.repowiki.core.graph import DependencyGraph
    from codocs.repowiki.core.wiki_builder import WikiBuilder

    graph = DependencyGraph.build_from_project(project)
    builder = WikiBuilder()
    wiki = builder.build(project, wiki_data, graph)

    output_dir = cfg.output_dir
    if fmt == "markdown":
        from codocs.repowiki.export.markdown import export_markdown

        export_markdown(wiki, output_dir)
        console.print(f"\n[bold green]Wiki generated:[/] {output_dir}/")
    elif fmt == "json":
        from codocs.repowiki.export.json_export import export_json

        out_path = f"{output_dir}/repowiki.json"
        export_json(wiki, out_path)
        console.print(f"\n[bold green]Wiki generated:[/] {out_path}")
    elif fmt == "html":
        from codocs.repowiki.export.html import export_html

        out_path = f"{output_dir}/repowiki.html"
        export_html(wiki, out_path)
        console.print(f"\n[bold green]Wiki generated:[/] {out_path}")
        if open_browser:
            import webbrowser

            webbrowser.open(f"file://{out_path}")

    # show token usage
    if llm.total_input_tokens or llm.total_output_tokens:
        console.print(
            f"[dim]Tokens used: {llm.total_input_tokens:,} in / "
            f"{llm.total_output_tokens:,} out[/]"
        )

    await cache.close()


def _build_rich_tree(tree: Tree, files, max_entries: int = 30):
    """add files to a Rich tree widget, grouped by directory."""
    dirs: dict[str, list] = {}
    for f in files[:max_entries]:
        from pathlib import Path as P

        parts = P(f.path).parts
        if len(parts) == 1:
            icon = "📄" if not f.is_config else "⚙️"
            tree.add(f"{icon} {f.path} [dim]({f.language})[/]")
        else:
            top = parts[0]
            if top not in dirs:
                dirs[top] = tree.add(f"📁 {top}/")
            # just show the filename under the dir
            icon = "📄" if not f.is_config else "⚙️"
            dirs[top].add(f"{icon} {'/'.join(parts[1:])} [dim]({f.language})[/]")

    remaining = len(files) - max_entries
    if remaining > 0:
        tree.add(f"[dim]... and {remaining} more files[/]")


@cli.command()
@click.argument("path")
@click.option("-o", "--output", default=None, help="Output directory (default: ./wiki)")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["markdown", "html", "both"]),
    default="both",
    help="Output format",
)
@click.option("-l", "--lang", default=None, help="Output language (en/ja/ko)")
@click.option("-m", "--model", default=None, help="LLM model name or alias")
@click.option(
    "--leaf-docs",
    default="markdown_docs",
    help="Name of per-package leaf documentation directories",
)
@click.option("--subtree", default=None, help="Restrict analysis to one subdirectory")
@click.option(
    "--plan-only", is_flag=True, help="Static analysis + page plan only, no LLM calls"
)
def deepdoc(
    path: str,
    output: str | None,
    fmt: str,
    lang: str | None,
    model: str | None,
    leaf_docs: str,
    subtree: str | None,
    plan_only: bool,
):
    """Generate a high-level architecture wiki from leaf docs + static analysis.

    Designed for large codebases that already have per-symbol documentation
    (e.g. the markdown_docs written by `codocs docgen` or RepoAgent); combines it with a package-level
    dependency graph to produce DeepWiki/CodeWiki-style pages with Mermaid diagrams.
    """
    cfg = Config.load()
    if lang:
        cfg.language = lang
    if model:
        cfg.model = resolve_model(model)
    if output:
        cfg.output_dir = output

    from codocs.repowiki.deepdoc.pipeline import analyze

    with console.status("[bold cyan]Analyzing module..."):
        index, digests, skeleton = analyze(path, leaf_dirname=leaf_docs, subtree=subtree)

    leaf_count = sum(1 for d in digests.values() if d.source == "leafdocs")
    console.print(f"[bold green]Module:[/]    {index.module}")
    console.print(f"[bold green]Packages:[/]  {len(index.packages)}")
    console.print(f"[bold green]Dep edges:[/] {index.graph.number_of_edges()}")
    console.print(f"[bold green]Leaf docs:[/] {leaf_count}/{len(index.packages)} packages")
    console.print(
        f"[bold green]Plan:[/]      {len(skeleton.sections)} sections, "
        f"{len(skeleton.all_pages())} pages"
    )

    if plan_only:
        for section in skeleton.sections:
            console.print(f"[bold]{section.title}[/]")
            for page in section.pages:
                console.print(f"  - {page.title} ({len(page.packages)} packages)")
        return

    if not cfg.api_key:
        console.print(
            "[yellow]No API key configured — cannot generate pages.[/]\n"
            "Set one with: [bold]export OPENAI_API_KEY=...[/] "
            "or use --plan-only for the static analysis."
        )
        raise SystemExit(1)

    import asyncio

    asyncio.run(_run_deepdoc(path, cfg, fmt, leaf_docs, subtree))


async def _run_deepdoc(path: str, cfg: Config, fmt: str, leaf_docs: str, subtree: str | None):
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from codocs.repowiki.deepdoc.pipeline import run_deepdoc

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Generating wiki...", total=None)

        def on_progress(step: str):
            progress.update(task, description=step)

        wiki, llm = await run_deepdoc(
            path, cfg, leaf_dirname=leaf_docs, subtree=subtree, on_progress=on_progress
        )

    output_dir = cfg.output_dir
    if fmt in ("markdown", "both"):
        from codocs.repowiki.export.markdown import export_markdown

        export_markdown(wiki, output_dir)
        console.print(f"[bold green]Markdown wiki:[/] {output_dir}/")
    if fmt in ("html", "both"):
        import re

        from codocs.repowiki.export.html import export_html, load_local_mermaid

        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", wiki.project_name).strip("-") or "wiki"
        out_path = f"{output_dir}/{slug}.html"
        # self-contained output: no tool branding, no external requests when a
        # local mermaid bundle is available
        export_html(wiki, out_path, branding=False, mermaid_js=load_local_mermaid())
        console.print(f"[bold green]HTML wiki:[/] {out_path}")

    if llm.total_input_tokens or llm.total_output_tokens:
        console.print(
            f"[dim]Tokens used: {llm.total_input_tokens:,} in / "
            f"{llm.total_output_tokens:,} out[/]"
        )


@cli.command()
@click.argument("wiki_dir")
@click.option("-o", "--output", required=True, help="Output directory for the translated wiki")
@click.option("-l", "--lang", default="ja", help="Target language code (ja/ko/...)")
@click.option("-m", "--model", default=None, help="LLM model name or alias")
@click.option("--no-html", is_flag=True, help="Skip regenerating the HTML viewer")
def translate(wiki_dir: str, output: str, lang: str, model: str | None, no_html: bool):
    """Translate an exported markdown wiki into another language.

    Structure-preserving: code blocks and link targets are protected, mermaid
    diagrams have only their labels translated (validated afterwards), and any
    page that fails validation keeps the source language instead of shipping
    broken structure.
    """
    from pathlib import Path

    cfg = Config.load()
    if model:
        cfg.model = resolve_model(model)
    if not cfg.api_key:
        console.print("[red]No API key configured — translation needs an LLM.[/]")
        raise SystemExit(1)
    src = Path(wiki_dir)
    if not src.is_dir() or not list(src.glob("*.md")):
        console.print(f"[red]{wiki_dir} is not a markdown wiki directory.[/]")
        raise SystemExit(1)

    import asyncio

    asyncio.run(_run_translate(src, Path(output), lang, cfg, no_html))


async def _run_translate(src, dst, lang: str, cfg: Config, no_html: bool):
    from codocs.repowiki.core.cache import Cache
    from codocs.repowiki.deepdoc.translator import WikiTranslator, build_wiki_from_dir
    from codocs.repowiki.llm.client import LLMClient

    llm = LLMClient(model=cfg.model, api_key=cfg.api_key, api_base=cfg.api_base)
    cache = Cache()
    await cache.init()

    from rich.progress import Progress, SpinnerColumn, TextColumn

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Translating wiki to '{lang}'...", total=None)
            translator = WikiTranslator(
                llm,
                cache,
                target_lang=lang,
                concurrency=cfg.concurrency,
                on_progress=lambda msg: progress.update(task, description=msg),
            )
            report = await translator.translate_dir(src, dst)
    finally:
        await cache.close()

    console.print(f"[bold green]Translated wiki:[/] {dst}/ ({len(report.files_ok)} pages)")
    if report.files_fallback:
        console.print(
            "[yellow]Kept in source language (failed validation):[/] "
            + ", ".join(report.files_fallback)
        )
    if report.mermaid_fallback:
        console.print(f"[yellow]Mermaid diagrams kept in source language:[/] {report.mermaid_fallback}")

    if not no_html:
        import re

        from codocs.repowiki.export.html import export_html, load_local_mermaid

        # keep the same HTML basename as the source wiki so the two correspond
        src_htmls = list(src.glob("*.html"))
        if len(src_htmls) == 1:
            html_name = src_htmls[0].name
        else:
            wiki_tmp = build_wiki_from_dir(dst)
            slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", wiki_tmp.project_name).strip("-") or "wiki"
            html_name = f"{slug}.html"
        wiki = build_wiki_from_dir(dst)
        out_path = dst / html_name
        export_html(wiki, out_path, branding=False, mermaid_js=load_local_mermaid())
        console.print(f"[bold green]HTML wiki:[/] {out_path}")

    if llm.total_input_tokens or llm.total_output_tokens:
        console.print(
            f"[dim]Tokens used: {llm.total_input_tokens:,} in / "
            f"{llm.total_output_tokens:,} out[/]"
        )


@cli.command()
@click.argument("path_or_url", default=".")
@click.option("-m", "--model", default=None, help="LLM model name or alias")
@click.option("-l", "--lang", default=None, help="Answer language (en/ja/ko)")
def chat(path_or_url: str, model: str | None, lang: str | None):
    """Ask questions about a codebase in the terminal."""
    cfg = Config.load()
    if model:
        cfg.model = resolve_model(model)
    if lang:
        cfg.language = lang

    if not cfg.api_key:
        console.print(
            "[yellow]No API key configured.[/] Chat needs an LLM.\n"
            "Set one with: [bold]export OPENAI_API_KEY=...[/] "
            "(plus OPENAI_BASE_URL for another OpenAI-compatible endpoint)."
        )
        return

    console.print("[dim]Indexing repository...[/]")
    if _is_url(path_or_url):
        from codocs.repowiki.ingest.github import ingest_github

        project = ingest_github(
            path_or_url, max_file_size=cfg.max_file_size, max_files=cfg.max_files
        )
    else:
        from codocs.repowiki.ingest.local import ingest_local

        project = ingest_local(
            path_or_url, max_file_size=cfg.max_file_size, max_files=cfg.max_files
        )

    from codocs.repowiki.core.rag import SimpleRAG

    rag = SimpleRAG()
    rag.index(project)
    if not rag.chunks:
        console.print("[yellow]No readable source found to chat about.[/]")
        return

    console.print(
        f"[bold cyan]RepoWiki Chat[/] — {project.name} "
        f"({len(project.files)} files). Type 'exit' to quit.\n"
    )

    import asyncio

    while True:
        try:
            question = console.input("[bold green]?[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", ":q"}:
            break
        answer = asyncio.run(_answer_question(question, rag, cfg))
        console.print(f"\n{answer}\n")


async def _answer_question(question: str, rag, cfg: Config) -> str:
    """Retrieve relevant code and ask the LLM a single question."""
    from codocs.repowiki.core.rag import format_context
    from codocs.repowiki.llm.client import LLMClient
    from codocs.repowiki.llm.prompts import build_chat_prompt

    chunks = rag.retrieve(question, top_k=5)
    messages = build_chat_prompt(question, format_context(chunks), cfg.language)
    llm = LLMClient(model=cfg.model, api_key=cfg.api_key, api_base=cfg.api_base)
    return await llm.complete(messages)


@cli.group("config")
def config_group():
    """Manage RepoWiki configuration."""
    pass


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value (e.g., repowiki config set model gpt-mini)."""
    cfg = Config.load()
    if key == "model":
        value = resolve_model(value)

    if not hasattr(cfg, key):
        console.print(f"[red]Unknown config key: {key}[/]")
        console.print(f"Valid keys: {', '.join(cfg.__dataclass_fields__.keys())}")
        raise SystemExit(1)

    setattr(cfg, key, value)
    cfg.save()
    console.print(f"[green]Set {key} = {value}[/]")


@config_group.command("get")
@click.argument("key")
def config_get(key: str):
    """Get a config value."""
    cfg = Config.load()
    if not hasattr(cfg, key):
        console.print(f"[red]Unknown config key: {key}[/]")
        raise SystemExit(1)
    val = getattr(cfg, key)
    # mask API key
    if key == "api_key" and val:
        val = val[:8] + "..." + val[-4:]
    console.print(f"{key} = {val}")


@config_group.command("list")
def config_list():
    """Show all config values."""
    cfg = Config.load()
    table = Table(title="Configuration", show_header=True, header_style="bold")
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_column("Source", style="dim")

    for key in cfg.__dataclass_fields__:
        val = getattr(cfg, key)
        if key == "api_key" and val:
            val = val[:8] + "..." + val[-4:]
        source = "default"
        import os

        env_key = {"api_key": "OPENAI_API_KEY", "api_base": "OPENAI_BASE_URL"}.get(
            key, f"REPOWIKI_{key.upper()}"
        )
        if os.getenv(env_key):
            source = f"env ({env_key})"
        table.add_row(key, str(val), source)

    console.print(table)
