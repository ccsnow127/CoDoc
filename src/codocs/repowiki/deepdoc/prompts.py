"""prompt templates for the deepdoc high-level wiki pipeline.

The style contract (reader-oriented prose, grounded mermaid diagrams, source
citations) follows the codewiki/deepwiki format.
"""

from __future__ import annotations

_LANG = {
    "en": "Write all output in English.",
    "ja": "日本語で出力してください。",
    "ko": "한국어로 작성해주세요.",
}


def _lang(language: str) -> str:
    return _LANG.get(language, _LANG["en"])


_MERMAID_RULES = """\
Mermaid diagram rules (mandatory):
- Node IDs must be plain alphanumeric/underscore; put human-readable text in the label, e.g. common_logger["common/logger"].
- Quote any label that contains spaces, slashes, parentheses or other punctuation.
- Keep each diagram under 25 nodes; use subgraphs to group related packages.
- Add edge labels for important relationships ("registers with", "reads", "publishes").
- For DEPENDENCY/COMPONENT diagrams you may ONLY draw edges listed in the provided facts. Never invent an edge. You may omit uninteresting edges and merge trivially similar nodes.
- For SEQUENCE diagrams, base every step on behavior described in the provided symbol documentation; name participants after real packages/components.
- Output diagrams as fenced blocks: ```mermaid ... ```"""

_STYLE_RULES = """\
Writing rules:
- You are writing one page of a technical wiki for engineers new to this codebase, in the style of DeepWiki/CodeWiki.
- Be direct and specific; no filler ("comprehensive", "robust", "leveraging"), no marketing tone.
- Ground every claim in the provided facts and symbol documentation. Do not speculate about code you cannot see; if a responsibility is unclear, describe what the symbols indicate and no more.
- Reference packages with the exact link placeholder syntax [[pkg:PATH]] (e.g. [[pkg:common/logger]]) so the build can cross-link them. Use it the first time a package is mentioned in a section, plain `code` afterwards.
- Use tables for component inventories. Keep paragraphs short."""


def build_plan_refine_prompt(skeleton_json: str, facts: str, language: str = "en") -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are the information architect of a code wiki. You refine a "
                "mechanically generated table of contents into a reader-friendly one. "
                + _lang(language)
            ),
        },
        {
            "role": "user",
            "content": (
                "Below is a draft wiki plan (sections and pages were derived from the "
                "directory tree) plus facts about the codebase.\n\n"
                f"## Facts\n{facts}\n\n"
                f"## Draft plan (JSON)\n```json\n{skeleton_json}\n```\n\n"
                "Improve it and return JSON with the SAME schema, except: do NOT output "
                "`package_count` or `sample_packages`, and do NOT output any `packages` field — "
                "package assignment is fixed and re-attached by id.\n"
                "- Rewrite `title` of sections and pages into descriptive, reader-oriented names "
                "(e.g. 'auth/session' -> 'Session Management (auth/session)'). Keep the directory hint in parentheses.\n"
                "- You may reorder and regroup sections and set better section titles "
                "(e.g. group related services together), but every page `id` must appear exactly once.\n"
                "- For each page fill `description` (1 sentence: what a reader learns) and `workflows` "
                "(1-3 concrete runtime flows worth a sequence diagram, judged from the symbol summaries).\n"
                "- Fill `project_title` and `project_summary` (2-3 sentences on what the whole system is).\n\n"
                "Output ONLY the JSON object, no fences, no commentary."
            ),
        },
    ]


def build_module_page_prompt(
    title: str,
    description: str,
    workflows: list[str],
    context: str,
    project_summary: str,
    language: str = "en",
) -> list[dict]:
    workflows_txt = "\n".join(f"- {w}" for w in workflows) or "- (choose the most important flow yourself)"
    return [
        {
            "role": "system",
            "content": f"{_STYLE_RULES}\n\n{_MERMAID_RULES}\n\n{_lang(language)}",
        },
        {
            "role": "user",
            "content": (
                f"Project context: {project_summary}\n\n"
                f"Write the wiki page titled \"{title}\".\n"
                f"Page goal: {description or 'document this subsystem for new engineers'}\n\n"
                f"## Facts and symbol documentation\n{context}\n\n"
                "Produce a markdown page with exactly this structure:\n"
                f"# {title}\n"
                "(1-2 paragraph introduction: what this subsystem does and where it sits in the system)\n"
                "## Architecture\n"
                "(a mermaid `graph TD`/`flowchart` of this subsystem's packages and their real "
                "dependency edges — only edges from the facts; group with subgraphs; then 1-2 "
                "paragraphs walking the reader through the diagram)\n"
                "## Key Components\n"
                "(a table: Component | Package | Responsibility — cover the packages of this page, "
                "merging tiny helpers; cite packages with [[pkg:...]])\n"
                "## Core Workflows\n"
                "(for these flows:\n" + workflows_txt + "\n"
                "one mermaid sequenceDiagram each, grounded in the symbol documentation, followed by "
                "a short prose explanation)\n"
                "## Dependencies & Integration\n"
                "(what this subsystem depends on — internal packages outside this page and external "
                "libraries — and what depends on it; use the inbound/outbound edge facts)\n\n"
                "Start directly with the `#` heading. No preamble, no closing remarks."
            ),
        },
    ]


def build_architecture_page_prompt(
    facts: str,
    page_summaries: str,
    base_diagram: str,
    language: str = "en",
) -> list[dict]:
    return [
        {
            "role": "system",
            "content": f"{_STYLE_RULES}\n\n{_MERMAID_RULES}\n\n{_lang(language)}",
        },
        {
            "role": "user",
            "content": (
                "Write the \"Architecture\" page of the wiki: the system-wide view that ties the "
                "module pages together.\n\n"
                f"## System facts\n{facts}\n\n"
                f"## Verified top-level dependency edges (mermaid skeleton you must base the main diagram on)\n"
                f"```mermaid\n{base_diagram}\n```\n"
                "You may rename node labels to friendly names, group nodes into subgraphs, drop "
                "low-weight edges, and add edge labels — but not invent edges.\n\n"
                f"## Module summaries\n{page_summaries}\n\n"
                "Produce a markdown page:\n"
                "# Architecture\n"
                "(2-3 paragraphs: the architectural style, the major layers/runtime processes, how "
                "requests and data move)\n"
                "## System Diagram\n(the refined mermaid diagram + walkthrough)\n"
                "## Runtime Processes\n(table of the `package main` entrypoints: Binary | Package | Role)\n"
                "## Key System Flows\n(2-3 mermaid sequenceDiagrams for the most important "
                "cross-module flows, each with prose)\n"
                "## Data & External Systems\n(databases, caches, protocols from the facts; which "
                "subsystems touch them)\n\n"
                "Start directly with the `#` heading."
            ),
        },
    ]


def build_overview_page_prompt(
    facts: str,
    page_summaries: str,
    toc: str,
    language: str = "en",
) -> list[dict]:
    return [
        {
            "role": "system",
            "content": f"{_STYLE_RULES}\n\n{_MERMAID_RULES}\n\n{_lang(language)}",
        },
        {
            "role": "user",
            "content": (
                "Write the landing (\"Overview\") page of the wiki.\n\n"
                f"## System facts\n{facts}\n\n"
                f"## Module summaries\n{page_summaries}\n\n"
                f"## Wiki table of contents (link with [[page:ID]])\n{toc}\n\n"
                "Produce a markdown page:\n"
                "# <project title>\n"
                "(2-3 paragraphs: what the system is, its domain — be concrete about the domain if "
                "the facts make it clear (e.g. a payment-processing backend) — and how the "
                "codebase is organized)\n"
                "## System at a Glance\n(one mermaid `graph TD` with subgraphs per major area, "
                "top-level components only, based on the dependency facts)\n"
                "## Tech Stack\n(table from the facts: Technology | Category | Where it is used)\n"
                "## How to Read This Wiki\n(a guided path through the wiki pages using [[page:ID]] "
                "links: where to start, what to read next, grouped by reader goal)\n\n"
                "Start directly with the `#` heading."
            ),
        },
    ]


def build_mermaid_repair_prompt(page_markdown: str, problems: list[str], language: str = "en") -> list[dict]:
    problems_txt = "\n".join(f"- {p}" for p in problems)
    return [
        {
            "role": "system",
            "content": (
                "You fix invalid mermaid diagrams inside markdown pages without changing anything else. "
                + _MERMAID_RULES
            ),
        },
        {
            "role": "user",
            "content": (
                "The following wiki page contains mermaid blocks that fail validation:\n"
                f"{problems_txt}\n\n"
                "Return the COMPLETE page with the mermaid blocks fixed (valid syntax, same intent). "
                "Do not alter prose, headings, or tables. Output only the page markdown.\n\n"
                f"{page_markdown}"
            ),
        },
    ]
