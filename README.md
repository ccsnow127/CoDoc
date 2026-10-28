<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ccsnow127/CoDoc/main/assets/logo-dark.svg">
  <img alt="CoDoc" src="https://raw.githubusercontent.com/ccsnow127/CoDoc/main/assets/logo-light.svg" width="360">
</picture>

### Documentation and unit tests for your codebase, verified by running the code.

Go · Python · Java · C · C++ · COBOL, including mixed-language repositories

[**Paper: SpecForge (NeurIPS 2026)**](https://arxiv.org/abs/2610.15708) |
[**Paper: TeSearch (NeurIPS 2026 EvoRobust)**](https://arxiv.org/abs/2610.15964) |
[**Blog**](https://ccsnow127.github.io/blog/2026/codoc/) |
[**Docs**](https://github.com/ccsnow127/CoDoc/blob/main/docs/index.md) |
[**Citation**](https://github.com/ccsnow127/CoDoc#citation)

[![PyPI](https://img.shields.io/pypi/v/codocs)](https://pypi.org/project/codocs/)
[![Python](https://img.shields.io/pypi/pyversions/codocs)](https://pypi.org/project/codocs/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/ccsnow127/CoDoc/blob/main/LICENSE)
[![CI](https://github.com/ccsnow127/CoDoc/actions/workflows/ci.yml/badge.svg)](https://github.com/ccsnow127/CoDoc/actions/workflows/ci.yml)
[![arXiv SpecForge](https://img.shields.io/badge/arXiv-2610.15708-b31b1b)](https://arxiv.org/abs/2610.15708)
[![arXiv TeSearch](https://img.shields.io/badge/arXiv-2610.15964-b31b1b)](https://arxiv.org/abs/2610.15964)
[![NeurIPS 2026](https://img.shields.io/badge/NeurIPS-2026-4b44ce)](https://openreview.net/forum?id=yMLqe1DovZ)

</div>

**News.** **[Oct 2026]** v0.1.0 released · SpecForge accepted at NeurIPS 2026 · TeSearch at the NeurIPS 2026 EvoRobust workshop · [blog post](https://ccsnow127.github.io/blog/2026/codoc/)

<p align="center">
  <img alt="codocs analyze and codocs baseline on dustin/go-humanize" src="https://raw.githubusercontent.com/ccsnow127/CoDoc/main/assets/analyze.svg" width="820">
  <br><em><code>codocs analyze</code> and <code>codocs baseline</code> on <a href="https://github.com/dustin/go-humanize">dustin/go-humanize</a>: static analysis plus the repository's own tests, no LLM, nothing written into the repo.</em>
</p>

## Why CoDoc

LLM-written documentation and unit tests usually look plausible and are judged by another LLM, or not at all. CoDoc checks file-level results by building and running code: the repository's tests or CoDoc's own suites where they exist, otherwise a compile check (COBOL mainframe programs: agreement of static facts).

- **Docs are tested.** A file's doc is scored by having the LLM regenerate the file from the doc alone, without the original source, and running the repository's relevant tests on the regenerated code. The score φ is the share of those tests that pass.
- **Tests are measured.** A generated test suite is pruned until it passes on the original code, then scored by coverage × mutation score. Uncovered lines and surviving mutants go back into the next round.
- **Six languages, one command.** Go, Python, Java, C, C++ and COBOL (with GnuCOBOL execution), including repositories that mix them (in a Go module, add `--source-lang all`; elsewhere every language found is processed).
- **Nothing is written into your repository.** Builds and tests run in a mirror or temporary directories under the output directory.

## Highlights

- **File docs** with the [SpecForge loop](https://github.com/ccsnow127/CoDoc/blob/main/docs/how-it-works.md): doc → regenerate → test → refine, early stop at φ ≥ 0.9.
- **Unit tests** with the [TeSearch loop](https://github.com/ccsnow127/CoDoc/blob/main/docs/testgen.md): prune failing tests, coverage × mutation score, gaps fed back.
- **Repository wiki** ([RepoWiki](https://github.com/he-yufeng/RepoWiki)-based): overview, architecture and subsystem pages with validated Mermaid diagrams, as Markdown and one self-contained HTML file; [translation](https://github.com/ccsnow127/CoDoc/blob/main/docs/wiki.md) that keeps code and diagrams intact.
- **Business-logic analysis**: call graph → hierarchical clusters → random-walk trajectories → one LLM summary per trajectory; domain-neutral by default, with optional [domain profiles](https://github.com/ccsnow127/CoDoc/blob/main/docs/bizlogic.md).
- **COBOL**: copybooks, JCL, CICS/SQL usage and call graph indexed; unit and batch programs tested with GnuCOBOL; an [LLM doc judge](https://github.com/ccsnow127/CoDoc/blob/main/docs/cobol.md) with a static checklist and a calibration mode.
- **Any OpenAI-compatible endpoint**, a SQLite cache of every LLM reply, and resumable runs ([configuration](https://github.com/ccsnow127/CoDoc/blob/main/docs/configuration.md)).
- **Static modes with no LLM**: `analyze`, `baseline`, `wiki --plan-only`, `bizlogic --plan-only`, `bizlogic-render`, `testrun`, `report`.

## Quickstart

```bash
pipx install codocs                                   # or: pip install codocs / uv tool install codocs
export OPENAI_API_KEY=sk-...                          # other endpoints: also export OPENAI_BASE_URL=https://<endpoint>/v1 CODOCS_MODEL=<model>
codocs analyze path/to/repo -o out                    # no LLM: languages, toolchains, file status counts
codocs run path/to/repo -o out --max-files 3          # docs and tests for 3 files, a wiki of their packages, report
```

Then open `out/report.md` (φ, coverage and mutation score per file), `out/docs/<pkg>/markdown_docs/` (docs), `out/tests/<pkg>/` (test suites) and `out/wiki/*.html` (wiki). Re-running the same command resumes; finished files are skipped. In `report.md`, check each doc's signal column first: φ from `tests` is the strongest evidence, `compile` the weakest.

Python repositories need nothing else: CoDoc ships pytest and coverage, and runs the tests with the repository's own `.venv` when it has them (or with `--py PYTHON`). Other languages need their toolchain on `PATH`: `go`, a JDK and Maven (Maven also fetches JUnit and JaCoCo on the first run, even for plain source trees), `cc`/`c++` with `gcov`, or GnuCOBOL 3.2 `cobc`. A missing toolchain marks that language's files `no-toolchain` instead of spending LLM calls on them (COBOL without `cobc` still gets docs, scored by static facts). See [Installation](https://github.com/ccsnow127/CoDoc/blob/main/docs/installation.md).

<details>
<summary><b>All toolchains in one command (conda)</b></summary>

```bash
git clone https://github.com/ccsnow127/CoDoc.git && cd CoDoc
./install.sh              # conda env "codocs": Python 3.12, Go, OpenJDK 21, Maven, GnuCOBOL 3.2, GCC (Linux), Mermaid bundle
conda activate codocs
codocs --help
```

`install.sh` builds the environment from [`environment.yml`](https://github.com/ccsnow127/CoDoc/blob/main/environment.yml) and prints the version of every toolchain. On macOS, C/C++ come from the Xcode Command Line Tools (`xcode-select --install`).

</details>

### Try it on public repositories

| Language | Repository | Command |
|---|---|---|
| Go | [dustin/go-humanize](https://github.com/dustin/go-humanize) | `codocs run go-humanize -o out/gh` |
| Python | [pallets/itsdangerous](https://github.com/pallets/itsdangerous) | `codocs run itsdangerous -o out/itsd --subtree src/itsdangerous` |
| Java | [apache/commons-cli](https://github.com/apache/commons-cli) | `codocs run commons-cli -o out/cli --max-files 3` |
| C + C++ | [benhoyt/inih](https://github.com/benhoyt/inih) | `codocs run inih -o out/inih` |
| COBOL | [shamrice/cbi](https://github.com/shamrice/cbi) | `codocs run cbi -o out/cbi --programs LTRIM`, then `codocs testrun out/cbi` |

Clone the repository first. For itsdangerous, give it an environment its tests can import from: `python -m venv itsdangerous/.venv && itsdangerous/.venv/bin/pip install -e itsdangerous freezegun pytest coverage`. The first Java run uses Maven to download JUnit and JaCoCo from Maven Central. For cbi, `codocs analyze cbi -o out/cbi` lists the program ids.

## How it works

<p align="center">
  <img alt="CoDoc pipeline: analyze, baseline, the TeSearch test loop, the SpecForge doc loop, then judge (COBOL), wiki and report" src="https://raw.githubusercontent.com/ccsnow127/CoDoc/main/assets/how-it-works.svg" width="560">
</p>

`codocs run` chains the stages in that order. Tests come before docs so that a file with no relevant existing tests can still be scored against CoDoc's own suite.

**Docs: the SpecForge loop** (`codocs docgen`, one file at a time)

1. The LLM writes a doc for the file.
2. In a separate call, the LLM regenerates the file from the doc. It sees the doc, the signatures of the other files in the package and the allowed imports, but never the original source.
3. The regenerated file replaces the original in a sandbox copy of the package, and the relevant tests that pass on the original code are run. **φ = passed / relevant tests** (0 if it does not build). Without relevant tests, CoDoc uses its generated suite, then a compile check; COBOL programs that cannot run outside a mainframe are scored by agreement of static facts.
4. The failures are fed back and the LLM revises the doc. The loop stops at `--doc-threshold` (0.9) or after `--doc-rounds` (3) and delivers the best round.

**Tests: the TeSearch loop** (`codocs testgen`, one file at a time)

1. The LLM writes a suite for the file; the package's own tests are hidden while it runs.
2. Failing tests are pruned until the suite passes on the original code.
3. **Score = coverage × mutation score** of the passing tests, using deterministic operator-based mutants (`--mutants 20`; 0 = coverage only).
4. Uncovered lines and surviving mutants are marked in the source for the next round. The loop stops at `--test-threshold` (0.85) or after `--test-rounds` (3) and delivers the best round, preferring the smaller suite among equally good ones.

> [!NOTE]
> CoDoc implements **bounded, simplified versions** of the paper algorithms: at most a few seed-then-refine rounds per file. It keeps SpecForge's regenerate-without-source signal with diagnosis and prescription, and TeSearch's pruning, coverage × mutation measurement, gap markers and smallest-among-the-best delivery. It does not include SpecForge's Pareto archive, dependency-ordered entity selection and expansion, or TeSearch's quality-diversity search over suite sizes and learned transition kernel. Details and limits: [How it works](https://github.com/ccsnow127/CoDoc/blob/main/docs/how-it-works.md).

## Results from the papers

These are the published results of the full algorithms, not measurements of this tool.

| Paper | Setting | Result |
|---|---|---|
| [SpecForge](https://arxiv.org/abs/2610.15708) (NeurIPS 2026) | DevEval+, five backbone models | **94.9%** solve rate with Claude-4.5-Sonnet; **+5.7 points** on average over the strongest baseline |
| [TeSearch](https://arxiv.org/abs/2610.15964) (NeurIPS 2026 EvoRobust workshop) | 129 real-world modules, three backbones | As the improvement signal of a self-evolving agent, rejects **59.9%** of behavior-changing rewrites (the repository's own tests: 25.3%) at a 4.0% false-alarm rate and 3.5 s per decision |

## Supported languages

| Language | Unit / target | Test runner | Coverage | Toolchain |
|---|---|---|---|---|
| Go | package / `.go` file | `go test -overlay` | `go test -cover` | `go` |
| Python | package directory / `.py` file | pytest | coverage.py | installed with CoDoc |
| Java | Maven module or source tree / `.java` file | javac + JUnit Platform launcher, or Maven surefire | JaCoCo | JDK, Maven |
| C / C++ | directory / `.c`, `.cpp` file | built-in single-header harness; existing test programs or `--test-cmd` | gcov (GCC or Apple LLVM) | `cc`, `c++`, `gcov` |
| COBOL | directory / program file | GnuCOBOL driver + stubs, or batch fixtures | statement trace (`-ftraceall`) | GnuCOBOL 3.2 `cobc` |

Mutation operators exist for every language (comparison, logic, arithmetic, assignment and more; COBOL has its own fixed-format-safe set). Gradle builds are reported as `unsupported-build`. Per-language details: [languages](https://github.com/ccsnow127/CoDoc/blob/main/docs/languages.md) and [COBOL](https://github.com/ccsnow127/CoDoc/blob/main/docs/cobol.md).

## Commands

| Command | What it does | LLM |
|---|---|---|
| `codocs analyze REPO -o OUT` | Languages, packages, target files and their status; COBOL index | no |
| `codocs baseline REPO -o OUT` | Runs the existing tests on the original code | no |
| `codocs testgen REPO -o OUT` | TeSearch loop: one unit-test suite per file | yes |
| `codocs docgen REPO -o OUT` | SpecForge loop: one Markdown doc per file | yes |
| `codocs wiki REPO -o OUT` | Repository wiki from the docs (`--plan-only`: static plan, no LLM) | yes |
| `codocs run REPO -o OUT` | analyze → baseline → testgen → docgen → judge → wiki → report | yes |
| `codocs bizlogic REPO -o OUT` | Business-logic trajectories (`--plan-only`: graph and walks, no LLM) | yes |
| `codocs bizlogic-render OUT` | Rebuilds the business-logic pages and HTML from stored JSON | no |
| `codocs judge OUT` | COBOL doc judge (`--calibrate` measures the judge itself) | yes |
| `codocs translate OUT/wiki -o OUT/wiki_ja -l ja` | Structure-preserving wiki translation | yes |
| `codocs testrun OUT` | Re-runs every delivered suite on the original code | no |
| `codocs report OUT` | Rebuilds `report.md` and `summary.json` | no |

Selection options (`--subtree`, `-p`, `--files`, `--max-files`, `--programs`), loop settings and parallelism are described in [Usage](https://github.com/ccsnow127/CoDoc/blob/main/docs/usage.md).

## Comparison

| Tool | Docs | Unit tests | Docs checked by running code | Test quality signal | Scope |
|---|---|---|---|---|---|
| **CoDoc** | per-file docs, repo wiki | yes | yes: regenerate from the doc, run the tests | coverage × mutation score | Go, Python, Java, C, C++, COBOL; local CLI |
| [RepoAgent](https://github.com/OpenBMB/RepoAgent) | per-object Markdown docs, updated from git diffs | no | no | none | Python; local CLI |
| [DeepWiki](https://deepwiki.com) / [deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open) | repo wiki with diagrams and chat | no | no | none | hosted service / self-hosted app |
| [Qodo Cover](https://github.com/qodo-ai/qodo-cover) | no | yes, iterated until coverage rises | n/a | coverage | any language with a Cobertura report; local CLI (unmaintained since 2025-06) |
| [Mutahunter](https://github.com/codeintegrity-ai/mutahunter) | no | no (scores existing tests) | n/a | LLM-generated mutants | language-agnostic; local CLI |

CoDoc reuses RepoAgent's `ClassDef` / `MethodDef` doc heading convention and reads in-tree `markdown_docs/` directories, so existing RepoAgent docs feed its wiki.

## Limitations

- φ measures only the behaviour the tests exercise. Files without relevant tests fall back to CoDoc's own suites or a compile check; `report.md` shows which signal scored each doc.
- CoDoc compiles and runs generated code and the repository's tests. Run it in a container on code you do not trust ([SECURITY.md](https://github.com/ccsnow127/CoDoc/blob/main/SECURITY.md)).
- Gradle builds are not supported, C/C++ test frameworks run only through `--test-cmd`, and CICS/DB2/IMS COBOL programs get test specifications instead of executable tests.
- LLM cost grows with files × rounds, plus about one call per wiki page; the wiki covers the packages of the selected files, or the whole repository (or `--subtree`) when no files are selected, and `--skip wiki` leaves it out. Start with `codocs analyze` and `--max-files`.

## Documentation

| Page | Contents |
|---|---|
| [Installation](https://github.com/ccsnow127/CoDoc/blob/main/docs/installation.md) | pip / pipx / uv, the conda environment, per-language toolchains, offline Mermaid |
| [Configuration](https://github.com/ccsnow127/CoDoc/blob/main/docs/configuration.md) | endpoint and key, models and aliases, environment variables, cache, resume, sandbox |
| [Usage](https://github.com/ccsnow127/CoDoc/blob/main/docs/usage.md) | analyze → trial → full run → translate → check |
| [Unit tests (TeSearch)](https://github.com/ccsnow127/CoDoc/blob/main/docs/testgen.md) | running the test loop on its own |
| [Languages](https://github.com/ccsnow127/CoDoc/blob/main/docs/languages.md) | units, targets, runners, coverage, mutation, suite names, doc signals |
| [COBOL](https://github.com/ccsnow127/CoDoc/blob/main/docs/cobol.md) | index, program modes, test bundles, facts signal, judge |
| [Wiki and translation](https://github.com/ccsnow127/CoDoc/blob/main/docs/wiki.md) | wiki inputs, HTML export, translation |
| [Business logic](https://github.com/ccsnow127/CoDoc/blob/main/docs/bizlogic.md) | pipeline, options, domain profiles |
| [Output layout](https://github.com/ccsnow127/CoDoc/blob/main/docs/outputs.md) | everything under `OUT/` |
| [How it works](https://github.com/ccsnow127/CoDoc/blob/main/docs/how-it-works.md) | architecture, both loops in detail, limits |

## Citation

If you use CoDoc, please cite the papers behind its two loops (SpecForge for the documentation loop, TeSearch for the test loop) and this repository for the tool itself.

```bibtex
@inproceedings{cheng2026specforge,
  title     = {{SpecForge}: Recursive Self-Improvement for Coding Agents via Behavioral Specifications},
  author    = {Cheng, Yutong and Chen, Haifeng and Gao, Peng and Cheng, Wei},
  booktitle = {Advances in Neural Information Processing Systems (NeurIPS)},
  year      = {2026},
  eprint    = {2610.15708},
  archivePrefix = {arXiv},
  url       = {https://arxiv.org/abs/2610.15708}
}
```

```bibtex
@inproceedings{cheng2026tesearch,
  title     = {Small but Mighty: Test Suites as Improvement Signals for Self-Evolving Agents},
  author    = {Cheng, Yutong and Cheng, Wei and Yu, Wenchao and Zhou, Dawei and Chen, Haifeng},
  booktitle = {NeurIPS 2026 Workshop on Self-Evolving Diversity-Driven Search for Robust AI Systems (EvoRobust)},
  year      = {2026},
  eprint    = {2610.15964},
  archivePrefix = {arXiv},
  url       = {https://arxiv.org/abs/2610.15964}
}
```

```bibtex
@software{cheng2026codoc,
  author  = {Cheng, Yutong},
  title   = {{CoDoc}: Executably Verified Documentation, Unit Tests and Repository Wikis},
  year    = {2026},
  version = {0.1.0},
  license = {MIT},
  url     = {https://github.com/ccsnow127/CoDoc}
}
```

GitHub's **Cite this repository** button (right sidebar, from [`CITATION.cff`](https://github.com/ccsnow127/CoDoc/blob/main/CITATION.cff)) gives the SpecForge citation. SpecForge code: [ccsnow127/SpecForge](https://github.com/ccsnow127/SpecForge) ([OpenReview](https://openreview.net/forum?id=yMLqe1DovZ)); TeSearch code: [ccsnow127/TeSearch](https://github.com/ccsnow127/TeSearch).

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](https://github.com/ccsnow127/CoDoc/blob/main/CONTRIBUTING.md) for the development setup, the test suite and how to add a language, and [SECURITY.md](https://github.com/ccsnow127/CoDoc/blob/main/SECURITY.md) before running CoDoc on code you do not trust: it executes generated code and the repository's tests.

## Acknowledgements

- [RepoWiki](https://github.com/he-yufeng/RepoWiki) (MIT): CoDoc's wiki is built on a modified fork, extended with the deepdoc planner and generator, Mermaid validation and repair, translation and multi-language indexes.
- [tree-sitter](https://tree-sitter.github.io/) and [tree-sitter-language-pack](https://pypi.org/project/tree-sitter-language-pack/) for parsing Python, Java, C and C++.
- [GnuCOBOL](https://gnucobol.sourceforge.io/) for compiling and running COBOL tests.
- [Mermaid](https://mermaid.js.org/) for the diagrams in the wiki.

See [THIRD_PARTY_NOTICES.md](https://github.com/ccsnow127/CoDoc/blob/main/THIRD_PARTY_NOTICES.md) for licenses.

## License

[MIT](https://github.com/ccsnow127/CoDoc/blob/main/LICENSE) © 2026 Yutong (CiCi) Cheng
