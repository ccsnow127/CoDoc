# Configuration

Settings come from three places, later ones winning: built-in defaults, then a `.env` file and
the environment, then command-line options.

## LLM endpoint and key

CoDoc talks to any OpenAI-compatible chat-completions API through the official `openai` SDK:
OpenAI itself, or any server or proxy that implements the same API.

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://<openai-compatible-endpoint>/v1"   # optional; default: api.openai.com
```

A `.env` file in the current directory or one of its parents is loaded automatically; start
from [`.env.example`](../.env.example). Never commit a key.

These commands make no LLM call and need no key: `analyze`, `baseline`, `wiki --plan-only`,
`bizlogic --plan-only`, `bizlogic-render`, `testrun` and `report`.

## Models

`-m/--model` (environment: `CODOCS_MODEL`) sets the model of every LLM command except `judge`,
which takes `--judge-model`. Any model name your endpoint
serves is passed through unchanged; three short aliases exist:

| Alias | Model sent to the API | Default of |
|---|---|---|
| `gpt` | `gpt-5.4` | `-m` (writes docs, tests, wiki pages and summaries) |
| `gpt-mini` | `gpt-5.4-mini` | |
| `judge` | `gpt-5.5` | `--judge-model` |

Two more models can be set separately:

- `--codegen-model` (`CODOCS_CODEGEN_MODEL`): the model that regenerates **COBOL** programs from
  their docs inside the doc loop. The default (empty) is the `-m` model. Regenerating a COBOL
  program from prose is the hardest call in the pipeline, so use the strongest model you have.
- `--judge-model` (`CODOCS_JUDGE_MODEL`, default `gpt-5.5`): the COBOL doc judge. It is
  deliberately a different model from the generator.

Aliases work for all three options.

### Client behaviour

These points describe CoDoc's own client, used by docgen, testgen, the judge and bizlogic. The
wiki and translation stages use the vendored RepoWiki client and its caches (see below).

- The client sends `max_completion_tokens` and falls back to `max_tokens` if a model rejects it;
  it drops `temperature` for models that reject it. Each fallback is remembered per model.
- Rate limits (HTTP 429) are retried with a jittered backoff of 15, 30, 60, 120 and 240 s, then
  300 s, at most 8 times. A 429 whose message reports an exhausted budget fails at once, since
  waiting does not clear it.
- A reply cut off at the token limit, an empty reply or an API error counts as a failed call, not
  as content. A reply that fails validation gets one retry; only valid replies are cached.
- Requests time out after 900 s. Generation uses temperature 0.2; code regeneration uses 0.0.
  Doc-loop token budgets (doc and code regeneration) grow with the file size, up to 64,000;
  other calls use fixed budgets (test generation 16,000, COBOL test generation 32,000; the
  wiki, bizlogic and the judge set their own).

## Cache, resume and `--force`

- **LLM cache.** Every valid reply is stored in SQLite at `~/.codocs/cache.db`, keyed by model,
  prompts, temperature and token limit. `--refresh-llm` ignores cached replies; fresh replies
  still update the cache.
- **Wiki caches.** The wiki keeps its own caches next to it, one per model and language
  (`~/.codocs/wiki-<model>-<lang>.db`); `translate` (either glossary) uses the same files. Their entries
  expire after 7 days and `--refresh-llm` does not apply to them: delete the file to regenerate
  pages sooner.
- **Resume.** Every finished file result is stored under `OUT/`. Re-running the same command
  skips files whose status is `ok` and reuses cached replies, so an interrupted run continues
  where it stopped. Result files are written atomically.
- **`--force`** recomputes results that already exist.

## Sandbox

Nothing is written into the analysed repository:

- Go runs in a mirror of the module under `OUT/work/sandbox/<repo>` and uses `go test -overlay`.
  The repository's sibling directories are symlinked next to the mirror, so relative paths that
  climb out of the module (`-I../common/include`, `go.mod` replaces) resolve; for a repository
  directly under `$HOME`, that is every entry of the home directory. Do not archive `OUT/work/`
  with symlinks followed.
- Python runs in hardlink farms; Java in a mirror; C and C++ in per-call temporary directories;
  COBOL in `OUT/work/tmp/` (kept for inspection with `CODOCS_KEEP_WORK=1`).
- `--no-sandbox` runs builds and tests of every language directly in the repository; tests may
  then write files there.

CoDoc compiles and runs generated code and the repository's tests. Read
[SECURITY.md](../SECURITY.md) before running it on code you do not trust.

## Loop and parallelism defaults

| Setting | Option | Default |
|---|---|---|
| Doc rounds / early-stop φ | `--doc-rounds` / `--doc-threshold` | 3 / 0.9 |
| Test rounds / early-stop score | `--test-rounds` / `--test-threshold` | 3 / 0.85 |
| Mutants per file / sampling seed | `--mutants` / `--mutant-seed` | 20 / 0 (0 mutants = coverage only) |
| Files in parallel (LLM side) | `-j/--concurrency` | 4 |
| Concurrent build and test processes | `--workers` | 8 |
| Seconds per Go test run / other test runs | `--go-timeout` / `--test-timeout` | 300 / same as Go |
| Longest file processed | `--max-file-lines` | 1500 |
| Source languages | `--source-lang` | `auto` |
| Wiki and COBOL prose language | `-l/--lang` | `en` |

## Environment variables

| Variable | Option | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | | API key |
| `OPENAI_BASE_URL` | | OpenAI-compatible endpoint (optional) |
| `CODOCS_MODEL` | `-m` | main model or alias |
| `CODOCS_CODEGEN_MODEL` | `--codegen-model` | COBOL regeneration model (empty = `-m`) |
| `CODOCS_JUDGE_MODEL` | `--judge-model` | COBOL judge model |
| `CODOCS_LANG` | `-l` | wiki / COBOL prose language (`en`, `ja`, `ko`) |
| `CODOCS_SOURCE_LANG` | `--source-lang` | `auto`, `all` or a list such as `go,c` |
| `CODOCS_TEST_TIMEOUT` | `--test-timeout` | seconds per non-Go test run |
| `CODOCS_GO`, `CODOCS_GO_FLAGS`, `CODOCS_GO_ENV` | `--go`, `--go-flags`, `--go-env` | Go toolchain |
| `CODOCS_PY`, `CODOCS_PYTEST_ARGS` | `--py`, `--pytest-args` | Python test interpreter and pytest arguments |
| `CODOCS_JAVA_HOME`, `CODOCS_MVN`, `CODOCS_JAVA_RUNNER`, `CODOCS_JACOCO_VERSION` | `--java-home`, `--mvn`, `--java-runner`, `--jacoco-version` | Java toolchain |
| `CODOCS_CC`, `CODOCS_CXX`, `CODOCS_CC_FLAGS`, `CODOCS_LD_FLAGS`, `CODOCS_CC_DB`, `CODOCS_CC_ROOT`, `CODOCS_BUILD_CMD`, `CODOCS_TEST_CMD`, `CODOCS_GCOV` | `--cc`, `--cxx`, `--cc-flags`, `--ld-flags`, `--cc-db`, `--cc-root`, `--build-cmd`, `--test-cmd`, `--gcov` | C / C++ toolchain |
| `CODOCS_COBC`, `CODOCS_COBC_FLAGS` | `--cobc`, `--cobc-flags` | GnuCOBOL compiler and dialect flags |
| `CODOCS_COBOL_COVERAGE` | `--cobol-coverage` | `statement` or `paragraph` |
| `CODOCS_COBOL_SPEC_SIGNAL` | `--cobol-spec-signal` | `facts` or `compile` |
| `CODOCS_EXCLUDE_PATHS` | `--exclude-path` | globs excluded from the COBOL index |
| `CODOCS_KEEP_WORK` | | `1` keeps COBOL build directories under `OUT/work/tmp/` |
