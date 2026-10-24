# Usage

The recommended flow is **analyze (no LLM) → small trial → full run → translate → check**.
`REPO` is the repository root (for Go, the module root) and `OUT` is any new directory. Nothing
is written into `REPO`.

## 1. Static analysis (no LLM, seconds)

```bash
codocs analyze REPO -o OUT
```

This prints the languages and toolchains, the status counts (`ok`, `too-large`, `generated`,
`header`, `no-toolchain`, ...) and a per-package table of the files CoDoc will process, and writes
`OUT/analysis.json`, which lists every target file with its status.
For COBOL it also prints the program / copybook / JCL summary and writes `OUT/cobol/index.json`.
Use it to check the language selection and filters before spending LLM calls.

**Languages.** `--source-lang auto` (default) processes only Go in a Go module (a `go.mod` with Go
sources) and every language found elsewhere. Pass `all` or a list such as `go,c` or
`python,cobol` to choose.

**Selection.** These options narrow the files and are accepted by `analyze`, `baseline`,
`testgen`, `docgen` and `run`:

| Option | Effect |
|---|---|
| `--subtree DIR` | one subdirectory of the repository |
| `-p/--packages A,B` | package directories (each includes its subpackages) |
| `--files GLOB` | glob on file paths relative to the repository root |
| `--max-files N` | at most N files |
| `--max-file-lines N` | skip longer files (status `too-large`; default 1500) |
| `--programs A,B` | COBOL program ids; other languages are then not processed |
| `--include-generated` | also process generated files (protobuf, asn1c, ...) |

`wiki` and `bizlogic` take `--subtree` only; `judge` and `testrun` take `--programs` only.

Two more static views:

```bash
codocs wiki REPO -o OUT --plan-only        # wiki page plan, no LLM
codocs bizlogic REPO -o OUT --plan-only    # call graph, clusters and walks, no LLM
```

## 2. Small trial (a few minutes)

Run the whole pipeline on a few files to check the key, endpoint, model and toolchains:

```bash
codocs run REPO -o OUT_TRIAL --subtree some/package --max-files 3
```

## 3. Full run

```bash
codocs run REPO -o OUT
```

A full run on a large repository can take hours. Run it in a terminal session that survives a
disconnect; if it is interrupted, run the same command again and it resumes (see
[Configuration](configuration.md#cache-resume-and---force)).

| Group | Options |
|---|---|
| Doc loop | `--doc-rounds` (3), `--doc-threshold` (0.9), `--no-generated-tests` (do not use CoDoc's suites as a doc signal) |
| Test loop | `--test-rounds` (3), `--test-threshold` (0.85), `--mutants` per file (20; 0 = coverage only), `--mutant-seed` |
| Parallelism | `-j N` files on the LLM side (4), `--workers N` build and test processes (8) |
| Time limits | `--go-timeout` / `--test-timeout` seconds per test run (300) |
| Stages | `--skip baseline,testgen,docgen,judge,wiki` |
| Wiki | `-l en\|ja\|ko`, `-f markdown\|html\|both`, `--no-refine`, `--no-tree-docs` |
| Models | `-m`, `--codegen-model` (COBOL regeneration), `--judge-model` (COBOL judge) |

`run` exits with status 2 when a stage it ran recorded an error; the details are in
`OUT/report.md` and `OUT/run_meta.json`. A failing wiki or judge stage does not abort the run.

Each stage also runs on its own: `codocs baseline | testgen | docgen | wiki REPO -o OUT`, and
`codocs judge OUT` for the COBOL judge. `docgen` uses the suites a previous `testgen` delivered
to the same `OUT` as its signal for files without relevant existing tests.

## 4. Translate the wiki (optional)

Docs, test plans and the wiki are written in English unless `-l` says otherwise. To get the wiki
in another language afterwards:

```bash
codocs translate OUT/wiki -o OUT/wiki_ja -l ja
```

Code blocks and link targets are protected, only diagram labels are translated, and each diagram
is syntax-checked again. A page that fails validation stays in the source language and is listed
at the end. `--glossary auto|go|cobol` selects the term glossary; `--no-html` skips the HTML. See
[Wiki and translation](wiki.md).

## 5. Check the results

```bash
codocs report OUT     # rebuild OUT/report.md and OUT/summary.json
codocs testrun OUT    # re-run every delivered suite on the original code (no LLM) -> OUT/testrun/
```

`OUT/report.md` has aggregates (docs run, mean φ, suites run, mean coverage and mutation score;
per language in multi-language runs) and one row per file for docs (status, signal, rounds, φ per
round, best φ) and for tests (status, rounds, score per round, coverage, mutation score). Open `OUT/wiki/*.html` in
a browser and spot-check that the diagrams render. Every round's prompts, replies and
measurements are kept; see [Output layout](outputs.md).

## Business logic

```bash
codocs bizlogic REPO -o OUT -k 20          # one granularity
codocs bizlogic REPO -o OUT -k 10 -k 40    # several granularities
codocs bizlogic-render OUT                 # rebuild pages and HTML from stored JSON (no LLM)
```

`bizlogic` is not part of `run`. See [Business logic](bizlogic.md) for the options and the
optional `--domain-profile`.
