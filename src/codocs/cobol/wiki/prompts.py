"""prompt templates for the COBOL repo wiki (RepoWiki deepdoc, adapted for mainframe readers).

The style rules (reader-oriented prose, grounded mermaid diagrams, cross-link
placeholders) follows RepoWiki; units are COBOL programs instead of Go packages and the
page structure speaks the language of CICS transactions, JCL jobs, VSAM files and copybooks.
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
- Node IDs must be plain alphanumeric/underscore (e.g. COACTVWC, txn_CAVW, ds_ACCTDAT); put readable text in a quoted label, e.g. ds_ACCTDAT[("ACCTDAT (VSAM)")].
- Quote any label that contains spaces, slashes, dots, parentheses or other punctuation.
- Keep each diagram under 25 nodes; use subgraphs to group related programs.
- Label edges with the real relationship kind: "CALL", "LINK", "XCTL", "START", "runs", "reads", "writes".
- For PROGRAM/COMPONENT diagrams you may ONLY draw edges listed in the provided facts (program edges, job steps, transaction starts, file readers/writers). Never invent an edge. You may omit uninteresting edges.
- For SEQUENCE diagrams, base every step on behavior stated in the program documentation; participants are real things: a terminal user, a CICS transaction, programs, VSAM files / datasets, DB2 tables, queues, JCL steps. Declare them as `participant ID as Label text` (no quotes) and keep message text free of semicolons and # characters.
- In a sequence diagram a program may only send a request to a program it directly CALLs / LINKs / XCTLs to according to the facts (follow the call chains hop by hop; do not skip the intermediate programs), and a file / table / queue access must come from the program the facts list as its reader or writer — not from the program that started the chain.
- Use the program name, transaction id, job name or store name from the facts as the participant / node ID or in its label, so diagrams can be checked: edges and messages that the static analysis does not support are removed automatically after generation.
- Never put angle brackets (< >) or HTML in labels.
- Output diagrams as fenced blocks: ```mermaid ... ```"""

_STYLE_RULES = """\
Writing rules:
- You are writing one page of a technical wiki about a COBOL / mainframe application, for engineers who must maintain or modernize it, in the style of DeepWiki/CodeWiki.
- Use mainframe vocabulary precisely: CICS transaction, BMS map, COMMAREA, LINK/XCTL, JCL job/step, VSAM KSDS, sequential dataset, DB2 table, copybook, paragraph.
- Be direct and specific; no filler ("comprehensive", "robust", "leveraging"), no marketing tone.
- Ground every claim in the provided facts and program documentation. Do not speculate about code you cannot see; if a responsibility is unclear, say what the facts indicate and no more.
- Cross-link with these exact placeholders (the build turns them into links):
  [[prog:NAME]] for a program, [[copy:NAME]] for a copybook, [[job:NAME]] for a JCL job,
  [[data:NAME]] for a file / dataset / DB2 table / queue (use the store name or alias from the facts), [[page:ID]] for another wiki page.
  Use a placeholder the first time something is mentioned in a section, plain `code` afterwards. Never put placeholders inside mermaid blocks.
- Use tables for inventories. Keep paragraphs short."""


def build_plan_refine_prompt(skeleton_json: str, facts: str, language: str = "en") -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are the information architect of a wiki for a COBOL mainframe application. "
                "You refine a mechanically generated table of contents into a reader-friendly one. "
                + _lang(language)
            ),
        },
        {
            "role": "user",
            "content": (
                "Below is a draft wiki plan (pages are groups of COBOL programs clustered from "
                "directories and the call / job / file graph) plus facts about the application.\n\n"
                f"## Facts\n{facts}\n\n"
                f"## Draft plan (JSON)\n```json\n{skeleton_json}\n```\n\n"
                "Improve it and return JSON with the SAME schema, except: do NOT output "
                "`program_count` or `sample_programs`, and do NOT output any `programs` field — "
                "program assignment is fixed and re-attached by page id.\n"
                "- Rewrite page `title`s into descriptive business/functional names judged from the "
                "program purposes (e.g. 'COACTVWC and related programs' -> 'Account & Card "
                "Maintenance (online)'). Keep titles under 60 characters.\n"
                "- You may reorder sections, move pages between sections and rename sections "
                "(e.g. 'Online Transactions', 'Batch Processing', 'Authorization Module'), but every "
                "page `id` must appear exactly once and must not change.\n"
                "- For each page fill `description` (1 sentence: what a reader learns) and "
                "`workflows` (1-3 concrete runtime flows worth a sequence diagram, e.g. 'user "
                "signs on via transaction CC00', 'nightly POSTTRAN job posts daily transactions'). "
                "A flow may only name transactions, jobs and programs that the facts tie to "
                "programs of THAT page.\n"
                "- Fill `project_title` (the application's name) and `project_summary` (2-3 "
                "sentences on what the whole system does and its runtime environment).\n\n"
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
    workflows_txt = (
        "\n".join(f"- {w}" for w in workflows) or "- (choose the most important flow yourself)"
    )
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
                f"## Facts and program documentation\n{context}\n\n"
                "Produce a markdown page with exactly this structure:\n"
                f"# {title}\n"
                "(1-2 paragraph introduction: what business function this subsystem implements, "
                "whether it runs online under CICS, in batch under JCL, or both, and where it sits "
                "in the application)\n"
                "## Architecture\n"
                "(one mermaid `flowchart TD` of this page's programs and their real edges, based on "
                "the verified diagram skeleton in the facts: programs grouped in subgraphs, plus the "
                "transactions / JCL jobs that start them and the main files or tables they read and "
                "write; then 1-2 paragraphs walking the reader through it)\n"
                "## Key Programs\n"
                "(a table: Program | Type | Responsibility — one row per program on this page, Type "
                "is the given type (CICS online / batch / subprogram, plus DB2/IMS), Program "
                "written as [[prog:NAME]])\n"
                "## Core Workflows\n"
                "(suggested flows:\n" + workflows_txt + "\n"
                "one mermaid sequenceDiagram each, grounded in the program documentation — e.g. "
                "terminal user -> transaction -> program -> VSAM file, or JCL step -> program -> "
                "dataset — followed by a short prose explanation. Every transaction, job, program "
                "and store in a flow must appear in the facts above with that relationship; if a "
                "suggested flow is not supported by these facts, document a supported flow "
                "instead, without commenting on the substitution)\n"
                "## Data & Copybooks\n"
                "(the files, datasets, DB2 tables and queues this subsystem reads and writes — only "
                "those listed under 'Data stores used by this page', SQL tables or native files; "
                "a table Store | Kind | Read by | Written by using [[data:NAME]] and [[prog:NAME]]. "
                "If the page touches no store directly, say so and name the page that does. Then "
                "a short list of the key copybooks with [[copy:NAME]] and what they define)\n"
                "## Dependencies & Integration\n"
                "(which programs on other pages this subsystem calls / LINKs / XCTLs to and which "
                "call it, the JCL jobs ([[job:NAME]]) and transactions that drive it — use the "
                "inbound / outbound facts)\n\n"
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
                "subsystem pages together.\n\n"
                f"## System facts\n{facts}\n\n"
                "## Verified subsystem-level edges (mermaid skeleton you must base the main "
                f"diagram on)\n```mermaid\n{base_diagram}\n```\n"
                "Nodes are wiki pages (subsystems); edge labels give the transfer kinds and "
                "counts. Keep the node IDs exactly as in the skeleton; you may rename node labels "
                "to friendly names, group nodes into subgraphs (e.g. Online / Batch), drop "
                "low-weight edges — but not invent edges.\n\n"
                f"## Subsystem summaries\n{page_summaries}\n\n"
                "Produce a markdown page:\n"
                "# Architecture\n"
                "(2-3 paragraphs: the runtime environment (CICS regions, batch JCL, DB2, IMS, MQ — "
                "only what the facts show), the major subsystems, and how online and batch "
                "processing share data)\n"
                "## System Diagram\n(the refined mermaid diagram + walkthrough)\n"
                "## Runtime View\n"
                "(two tables from the entrypoint facts. Online: Transaction | Program | Purpose. "
                "Batch: Job | Programs (steps) | Purpose — use [[job:NAME]] and [[prog:NAME]]; if "
                "there are many, list the most important and say how many more exist; omit a table "
                "the facts give no rows for)\n"
                "## Key System Flows\n"
                "(2-3 mermaid sequenceDiagrams for the most important end-to-end flows across "
                "subsystems — e.g. an online transaction from sign-on to file update, or a nightly "
                "batch chain — each with prose. Build each one from a chain listed under 'Call / "
                "LINK / XCTL chains' in the system facts: the transaction or job starts the entry "
                "program, each program messages only the next program on its chain, and each file "
                "/ table / queue access is drawn from the program whose [w X] / [r X] tag lists "
                "it)\n"
                "## Data Stores\n"
                "(table of the main files / datasets / DB2 tables / queues from the facts: Store | "
                "Kind | Written by | Read by, using [[data:NAME]]; note which stores are shared "
                "between online and batch)\n\n"
                "Start directly with the `#` heading."
            ),
        },
    ]


def build_overview_page_prompt(
    facts: str,
    tech_table: str,
    page_summaries: str,
    toc: str,
    language: str = "en",
    skeleton: str = "",
    store_access: str = "",
) -> list[dict]:
    grounding = ""
    if skeleton:
        grounding += (
            "## Verified overview skeleton (base the 'System at a Glance' diagram on it)\n"
            f"```mermaid\n{skeleton}\n```\n"
            "Nodes are wiki pages, the CICS / JCL entry points and the main data stores. Keep "
            "the node IDs; you may relabel nodes, group them into subgraphs and drop edges.\n\n"
        )
    if store_access:
        grounding += (
            "## Verified data store access (which pages / jobs / transactions read or write "
            "each store)\n" + store_access + "\n\n"
        )
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
                f"## Detected technology (deterministic, from static analysis)\n{tech_table}\n\n"
                f"## Subsystem summaries\n{page_summaries}\n\n"
                f"## Wiki table of contents (link with [[page:ID]])\n{toc}\n\n"
                + grounding
                + "Produce a markdown page:\n"
                "# <application title>\n"
                "(2-3 paragraphs: what the application does, its business domain — be concrete "
                "when the facts make it clear (e.g. credit card management, insurance policies) — "
                "and how the codebase is organized)\n"
                "## System at a Glance\n"
                "(one mermaid `flowchart TD`: users / terminals and JCL schedules as entry points "
                "— a JCL entry only when the skeleton has one, i.e. when jobs run repo programs — "
                "subgraphs per major area with the subsystems, and the main data stores. Every "
                "edge must come from the verified overview skeleton or the verified data store "
                "access list: a page, job or transaction may point at a store only if that list "
                "says it reads / writes it, labelled 'reads' or 'writes' accordingly)\n"
                "## Technology\n"
                "(reproduce the detected technology table: Technology | Role | Evidence. Keep "
                "every row and every number; you may reword the Role column)\n"
                "## How to Read This Wiki\n"
                "(a guided path through the wiki pages using [[page:ID]] links: where to start, "
                "what to read next for online flows vs batch processing, and the reference "
                "pages)\n\n"
                "Start directly with the `#` heading."
            ),
        },
    ]


def build_mermaid_repair_prompt(
    page_markdown: str, problems: list[str], language: str = "en"
) -> list[dict]:
    problems_txt = "\n".join(f"- {p}" for p in problems)
    return [
        {
            "role": "system",
            "content": (
                "You fix invalid mermaid diagrams inside markdown pages without changing anything "
                "else. " + _MERMAID_RULES
            ),
        },
        {
            "role": "user",
            "content": (
                "The following wiki page contains mermaid blocks that fail validation:\n"
                f"{problems_txt}\n\n"
                "Return the COMPLETE page with the mermaid blocks fixed (valid syntax, same "
                "intent). Do not alter prose, headings, tables or [[...]] placeholders. Output "
                "only the page markdown.\n\n"
                f"{page_markdown}"
            ),
        },
    ]
