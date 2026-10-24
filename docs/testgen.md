# Unit tests: running the TeSearch loop on its own

`codocs testgen` runs only the test loop. It needs neither `baseline` nor `docgen`. Unlike the
wiki, tests have to be built and run, so it needs an LLM key **and** the toolchain of every
language it processes ([Installation](installation.md)). (docgen needs the toolchains too, except
for COBOL, whose docs fall back to the static facts signal.)

## 1. Set up

```bash
export OPENAI_API_KEY="sk-..."            # other endpoints: also OPENAI_BASE_URL and CODOCS_MODEL
codocs analyze REPO -o OUT                # every target should be `ok`, none `no-toolchain`
```

Also check the toolchain lines `analyze` prints: every language must show a version (COBOL: not
"no cobc", since COBOL targets stay `ok` without it), and Java also needs `mvn` once to fetch
JUnit and JaCoCo, which `analyze` does not check for a plain source tree.

- **Python:** the tests run with the repository's `.venv/bin/python` (or `venv/`) if it has
  pytest and coverage, otherwise with the Python that runs CoDoc. The code under test must be
  importable by that interpreter: install the repository's dependencies into it, or point `--py`
  at an interpreter that has them plus `pytest` and `coverage`.
- **Java:** needs `JAVA_HOME` or `--java-home`, and `mvn`: Maven fetches JUnit and JaCoCo on the
  first run (also for a plain source tree without `pom.xml`), which needs network access to
  Maven Central.
- **C / C++:** pass `--cc-db compile_commands.json` (or `--cc-flags`) when the code needs include
  paths or macros.
- **COBOL:** needs `cobc`; without it no COBOL tests are generated.

## 2. Small trial, then the full run

```bash
codocs testgen REPO -o OUT_TRIAL --subtree some/package --max-files 3
codocs testgen REPO -o OUT
```

| Group | Options |
|---|---|
| Loop | `--test-rounds` (3), `--test-threshold` (0.85, early stop on coverage × mutation score), `--mutants` per file (20; 0 = coverage only), `--mutant-seed` (0) |
| Parallelism and time | `-j N` (LLM side, 4), `--workers N` (build and test processes, 8), `--go-timeout` / `--test-timeout` seconds per test run (300) |
| Selection | `--source-lang`, `--subtree`, `-p`, `--files`, `--max-files`, `--programs` (COBOL), as in [Usage](usage.md) |
| Rerun | finished files are skipped; `--force` regenerates them |

## 3. What one round does

1. The LLM writes (round 1) or revises (later rounds) a suite for one target file. The prompt has
   the source, the package context and a reference to the existing tests.
2. The suite is placed next to the target in the sandbox, with the package's own tests hidden,
   and run. Import errors get up to two automatic fixes.
3. Failing tests are removed and the rest re-run until the suite is green (at most 8 runs).
4. Coverage of the target is measured on the green suite. Then mutants of the target are
   sampled (`--mutants`) and the green suite is run against each; a mutant on an uncovered line
   is only compiled and counts as survived if it builds.
5. **Score = coverage × mutation score** (coverage alone when mutation is off or no mutant is
   valid). The next round sees the source with the uncovered lines and surviving mutants marked.

The loop stops early at `--test-threshold` and delivers the best round. Ranking: score, then a
non-empty suite, then coverage, then the smaller suite, then the earlier round.

## 4. Results

- **Delivered suites** in `OUT/tests/<pkg>/`:

  | Language | Suite |
  |---|---|
  | Go | `<stem>_codocs_test.go` |
  | Python | `test_<module>_codocs.py` |
  | Java | `<Class>CodocsTest.java` |
  | C / C++ | `<stem>_codocs_test.c` / `.cpp` (support files in `OUT/tests/_support/`) |
  | COBOL unit and batch programs | `<STEM>_codocs_test.cbt` plus the expanded `<STEM>_codocs_test/` directory |
  | COBOL spec programs | `<STEM>_codocs_spec.json` and `.md` (a test specification, not executable) |

- **Per round** in `OUT/testgen/<pkg>/<stem>/round_<r>/`: `prompt.txt`, `reply.txt`, the suite,
  the green suite and `measure.json` (passing tests, coverage, mutation score, uncovered lines,
  surviving mutants). `result.json` holds the best round.
- `OUT/report.md` lists every suite with its score per round, coverage and mutation score.
- `codocs testrun OUT` re-runs all delivered suites on the original code without the LLM.
- A later `codocs docgen` with the same `OUT` uses these suites as the doc signal for files that
  have no relevant existing tests (`--no-generated-tests` turns that off).
