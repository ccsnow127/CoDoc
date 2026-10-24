# Output layout

Everything CoDoc produces goes under the output directory `OUT` (`-o`). The analysed repository
is never modified.

```
OUT/
├── analysis.json                        # languages, units, targets and their status
├── cobol/index.json                     # COBOL index: programs, copybooks, jobs, data stores, edges
├── cobol/modes.json                     # COBOL program modes and spec kinds, merged across runs
├── baseline/<unit>.json                 # existing tests on the original code (Go)
├── baseline/<lang>/<unit>.json          # the same for other languages
├── testgen/<pkg>/<stem>/round_<r>/      # per-round suite, measurement, prompt and reply
├── tests/<pkg>/<stem>_codocs_test.*     # delivered test suites (names per language below)
├── tests/_support/                      # C/C++ support files (codocs_test.h, ...)
├── docgen/<pkg>/<stem>/round_<r>/       # per-round doc, regenerated code, evaluation, prompt and reply
├── docs/<pkg>/markdown_docs/<stem>.md   # delivered docs
├── judge/                               # COBOL judge results and summary
├── testrun/                             # results of `codocs testrun`
├── wiki/*.md, wiki/<project>.html       # repository wiki (self-contained HTML)
├── bizlogic/k<K>/                       # clusters, trajectories, business logic, wiki pages
├── report.md, summary.json              # per-file results and aggregates
├── run_meta.json                        # settings (without the key), stage status, token usage
├── logs/                                # run log
└── work/                                # sandbox mirror and temporary build directories
```

Path conventions: `<stem>` is the file name without `.go` for Go (`shapes.go` → `shapes`) and the
full file name otherwise (`calc.py`, `ACCTINQ.cbl`). `<pkg>` is the package or unit directory;
the root package is `_root` under `baseline/`, `docgen/`, `testgen/` and `tests/`, while its docs
go to `docs/markdown_docs/`. In `baseline/` file names, `/` in the unit path becomes `__`
(`baseline/c/clib__src.json`).

## Per-round files

Every LLM round is kept, so any result can be traced back to its prompt and reply.

| Directory | Files |
|---|---|
| `docgen/<pkg>/<stem>/round_<r>/` | `prompt_doc.txt`, `reply_doc.txt`, `doc.md`, `prompt_code.txt`, `reply_code.txt`, `code.<ext>` (the regenerated source), `eval.json` (signal, tests run, passed, φ, build errors) |
| `testgen/<pkg>/<stem>/round_<r>/` | `prompt.txt`, `reply.txt`, `suite.<ext>`, `green.<ext>` (after pruning), `measure.json` (passing tests, coverage, mutation score, uncovered lines, surviving mutants) |

`result.json` next to the rounds records the delivered (best) round and the file's status.

## Delivered suites

| Language | Suite |
|---|---|
| Go | `<stem>_codocs_test.go` |
| Python | `test_<module>_codocs.py` |
| Java | `<Class>CodocsTest.java` |
| C / C++ | `<stem>_codocs_test.c` / `.cpp` |
| COBOL unit / batch | `<STEM>_codocs_test.cbt` plus the expanded `<STEM>_codocs_test/` directory |
| COBOL spec | `<STEM>_codocs_spec.json` / `.md` |

## Report

`OUT/report.md` (rebuilt by `codocs report OUT`) contains:

- aggregates: docs run, mean φ, suites run, mean coverage and mutation score (in multi-language
  runs also a per-language table);
- one row per doc: status, signal (`tests`, `generated-tests`, `compile`, COBOL `facts`), rounds,
  φ per round, best φ;
- one row per suite: status, rounds, score per round, coverage, mutation score;
- for COBOL: program modes, φ per signal and a judge section.

`OUT/summary.json` holds the same numbers in machine-readable form. `codocs run` exits with
status 2 when a stage recorded an error in `run_meta.json`.

## Business logic

`OUT/bizlogic/k<K>/` holds `clusters.json`, `nodes.json`, `trajectories.json`,
`business_logic.json` and the generated wiki pages with their HTML. `codocs bizlogic-render OUT`
rebuilds the pages from the JSON without the LLM. See [Business logic](bizlogic.md).
