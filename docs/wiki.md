# Wiki and translation

`codocs wiki` builds a repository wiki: overview, architecture and subsystem pages with Mermaid
diagrams, exported as Markdown and as one self-contained HTML file. It runs a modified fork of
[RepoWiki](https://github.com/he-yufeng/RepoWiki)'s deepdoc pipeline, vendored as
`codocs.repowiki`.

```bash
codocs wiki REPO -o OUT                 # after docgen, or on its own
codocs wiki REPO -o OUT --plan-only     # static analysis and page plan only, no LLM
```

## Inputs

For every package the wiki uses the best description available, in this order:

1. the docs CoDoc delivered to `OUT/docs/` (from `docgen` or `run`);
2. in-tree `markdown_docs/` directories, for example docs written by
   [RepoAgent](https://github.com/OpenBMB/RepoAgent) (`--no-tree-docs` ignores them);
3. static analysis of the source.

`codocs wiki` prints how many packages came from each source. A Go module uses the Go module
index; every other language set uses a multi-language index with one package per unit directory.
COBOL contributes one package per program and its own page planner ([COBOL](cobol.md#wiki)).

## Options

| Option | Effect |
|---|---|
| `--plan-only` | static analysis and the page plan; no LLM calls |
| `--no-refine` | keep the deterministic page plan (skips the plan-refinement LLM call) |
| `-f markdown\|html\|both` | output format (default `both`) |
| `-l en\|ja\|ko` | wiki language (`CODOCS_LANG`) |
| `--subtree DIR` | restrict the wiki to one subdirectory |
| `-m`, `-j` | model and parallel LLM calls |

## Diagrams and HTML

- Every Mermaid block is validated and sanitized; a block that still fails gets an LLM repair
  pass.
- The Markdown pages go to `OUT/wiki/*.md` and the HTML viewer to `OUT/wiki/<project>.html`.
- The HTML is a single file. If `~/.repowiki/mermaid.min.js` exists and is larger than 100 KB it
  is inlined and the page works offline; otherwise Mermaid is loaded from the jsDelivr CDN
  ([Installation](installation.md#offline-mermaid-bundle)).
- `codocs bizlogic --wiki-dir OUT/wiki` adds a Business Logic section to an existing wiki
  ([Business logic](bizlogic.md)).

## Translation

```bash
codocs translate OUT/wiki -o OUT/wiki_ja -l ja
```

- Code blocks, inline code and wiki placeholders are protected. In Mermaid diagrams only the
  labels are translated, and every diagram is validated again afterwards.
- A page that fails structural validation stays in the source language and is listed at the end
  of the run. Review those pages.
- `--glossary auto|go|cobol` selects the term glossary. `auto` (default) uses the COBOL glossary
  (CICS, JCL, VSAM, KSDS, ...) for COBOL wikis and the RepoWiki translator otherwise.
- `-j` translates pages in parallel (COBOL glossary); `--no-html` skips the HTML viewer.

## Caches

Wiki LLM replies are cached per model and language in `~/.codocs/wiki-<model>-<lang>.db`, next to
the main cache ([Configuration](configuration.md#cache-resume-and---force)). `translate` uses the
same files with either glossary, and the same model settings (`-m`, `CODOCS_MODEL`, `.env`) as
every other command. Wiki cache entries expire after 7 days, and `--refresh-llm` does not apply
to them; delete the cache file to regenerate pages sooner.
