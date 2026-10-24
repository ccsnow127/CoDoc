# Installation

CoDoc needs Python 3.10 or newer and runs on Linux and macOS. It drives the build and test
toolchain of every language it processes, so besides the Python package you need the toolchains
of the languages in your repository.

## The Python package

```bash
pipx install codocs        # recommended for a command-line tool
uv tool install codocs     # same, with uv
pip install codocs         # into the current environment
```

`codocs --help` lists the commands. The package brings pytest, coverage and tree-sitter, so a
Python repository needs nothing else.

From a clone, for development:

```bash
git clone https://github.com/ccsnow127/CoDoc.git && cd CoDoc
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Everything at once: the conda environment

With conda ([Miniforge](https://github.com/conda-forge/miniforge) recommended), one script
installs CoDoc together with every toolchain it drives:

```bash
git clone https://github.com/ccsnow127/CoDoc.git && cd CoDoc
./install.sh              # environment "codocs"; ./install.sh NAME for another name
conda activate codocs
```

`install.sh` creates the environment from [`environment.yml`](../environment.yml) (Python 3.12,
Go, OpenJDK 21, Maven, GnuCOBOL 3.2 and CoDoc itself, installed editable), adds GCC (`cc`, `c++`,
`gcov`) on Linux, downloads the [offline Mermaid bundle](#offline-mermaid-bundle) and prints the
version of every toolchain. On macOS, C and C++ (and the C compiler that `cobc` calls) come from
the Xcode Command Line Tools: `xcode-select --install`. On Linux, `install.sh` also adds
`go-cgo` (conda-forge's plain `go` has cgo off there) and `binutils`, and writes an activation
script that points `cobc` at the conda GCC in C17 mode (conda-forge's GnuCOBOL 3.2 output does
not compile under the C23 default of newer GCC). `conda env create -f environment.yml` alone
skips these steps, the Mermaid download and the version report, so prefer `install.sh`.
conda-forge has no GnuCOBOL for linux-aarch64; there, install `cobc` from the distribution and
drop `gnucobol` from `environment.yml`.

## Toolchains

Install only the toolchains of the languages you process. Each one can be pointed at explicitly
with an option or an environment variable.

| Language | Toolchain | Options (environment variable) |
|---|---|---|
| Go | `go` | `--go` (`CODOCS_GO`), `--go-flags` (`CODOCS_GO_FLAGS`), `--go-env` (`CODOCS_GO_ENV`), `--go-timeout` |
| Python | an interpreter with `pytest` and `coverage` | `--py` (`CODOCS_PY`), `--pytest-args` (`CODOCS_PYTEST_ARGS`) |
| Java | a JDK; Maven (Maven modules, and once per machine to fetch JUnit and JaCoCo into `~/.m2`) | `--java-home` (`CODOCS_JAVA_HOME`), `--mvn` (`CODOCS_MVN`), `--java-runner fast\|mvn` (`CODOCS_JAVA_RUNNER`), `--jacoco-version` (`CODOCS_JACOCO_VERSION`) |
| C / C++ | `cc` / `c++` and a matching `gcov` | `--cc` (`CODOCS_CC`), `--cxx` (`CODOCS_CXX`), `--cc-flags`, `--ld-flags`, `--cc-db` (compile_commands.json), `--cc-root`, `--build-cmd`, `--test-cmd`, `--gcov` (each also `CODOCS_<NAME>`) |
| COBOL | GnuCOBOL 3.2 (`cobc`) | `--cobc` (`CODOCS_COBC`), `--cobc-flags` (`CODOCS_COBC_FLAGS`) |

Notes:

- **Python.** Tests run with the repository's own `.venv/bin/python` (or `venv/`) when it has
  pytest and coverage, otherwise with the interpreter that runs CoDoc. The code under test must be
  importable by that interpreter. With `pipx` or `uv tool`, CoDoc lives in an isolated
  environment, so either create `.venv` in the repository with its dependencies plus
  `pytest coverage`, or pass `--py /path/to/python` of an environment that has them.
- **Java.** `JAVA_HOME` (set by `conda activate`) or `--java-home` selects the JDK. The first run
  uses `mvn` to download JUnit and JaCoCo from Maven Central into `~/.m2`, so it needs Maven and
  network access even for a plain source tree without `pom.xml`; without them every Java build
  fails with "cannot resolve the Java tool jars" (`analyze` does not warn about this). Gradle
  builds are not supported (status `unsupported-build`).
- **C / C++.** Use `--cc-db compile_commands.json` or `--cc-flags` when the code needs include
  paths or macros. On macOS use Apple's `cc`/`gcov`; a conda GCC's `gcov` cannot read clang's
  coverage data.
- **COBOL.** `brew install gnucobol` on macOS; on Linux the distribution package if it is 3.2
  (Ubuntu 26.04: `gnucobol3`; Ubuntu 24.04 ships 3.1.2, whose `cobc` lacks options CoDoc uses)
  or a source build. Check `cobc --version`; CoDoc is developed against 3.2. Without `cobc`,
  COBOL docs, the wiki, business logic and the judge still work, but COBOL tests are not
  generated and docs are scored with the static facts signal (see [COBOL](cobol.md)).

When a toolchain is missing, CoDoc prints a warning and marks that language's files
`no-toolchain`; no LLM calls are spent on them. The command fails instead when the language was
named with `--source-lang` or is the only language in the repository. COBOL is the exception:
without `cobc` its files stay `ok`, docs are still generated (scored with the static facts
signal) and only the tests are skipped. `codocs analyze REPO` prints the detected toolchain
versions, the status counts and a per-package table; `OUT/analysis.json` lists every target file
with its status.

## Offline Mermaid bundle

The wiki's HTML export looks for `~/.repowiki/mermaid.min.js`. If the file exists and is larger
than 100 KB, it is inlined, so the page works offline. Otherwise the HTML loads Mermaid from the
jsDelivr CDN. `install.sh` downloads it; by hand:

```bash
mkdir -p ~/.repowiki
curl -fsSL https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.min.js -o ~/.repowiki/mermaid.min.js
```

## Check the installation

```bash
codocs --version
codocs analyze path/to/repo -o out    # no LLM, no key needed
```
