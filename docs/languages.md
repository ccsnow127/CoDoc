# Languages

Every language goes through the same two loops ([How it works](how-it-works.md)). What differs is
how units and targets are found, how code is built and tested, and how coverage and mutants are
produced. Language adapters live in `src/codocs/lang/`.

## Detection

- `--source-lang auto` (default): a repository with `go.mod` and Go sources is processed as Go
  only; any other repository processes every language found. `all` or a comma list
  (`go,python,java,c,cpp,cobol`) chooses explicitly.
- Extensions: Go `.go`; Python `.py`; Java `.java`; C `.c`; C++ `.cc .cpp .cxx .c++ .C`; COBOL
  programs `.cbl .cob .cobol ...` plus copybooks, JCL, BMS and CSD members (see [COBOL](cobol.md)).
  `.h` files are attributed to C or C++ and never processed as targets themselves (status
  `header`).
- Skipped directories: dot-directories, `vendor`, `node_modules`, `testdata`, `markdown_docs`,
  virtual environments, build outputs (`build`/`dist` next to a build file, `target` next to
  `pom.xml`, CMake and Bazel trees) and the output directory itself.
- Generated files (protobuf, asn1c, "Code generated ... DO NOT EDIT" and similar) are skipped
  unless `--include-generated`.

## Overview

| | Unit / target | Test runner | Coverage | Mutation |
|---|---|---|---|---|
| Go | package / `.go` file | `go test -json -overlay` | `-cover -covermode=set` | `==`/`!=`/`<`/`<=`..., `&&`/`\|\|`, `+=`/`-=`, `++`/`--`, arithmetic, `true`/`false`; never in imports or top-level `var`/`const` |
| Python | package directory / `.py` file | pytest in hardlink farms (no byte-code, no pytest cache) | coverage.py | the Go set plus `is`/`is not`, `in`/`not in`, `and`/`or`, `//`; only inside function bodies; a mutant that fails at import counts as killed |
| Java | Maven module package, or a plain source tree / `.java` file | `fast`: javac + JUnit Platform launcher; `mvn`: Maven surefire | JaCoCo 0.8.12–0.8.14, picked by class-file version | Go-style table |
| C / C++ | directory / `.c`, `.cpp` file | generated suites: built-in `codocs_test.h` harness; existing tests: programs with `main`, or `--test-cmd` | gcov text format (GCC or Apple LLVM) | Go-style table |
| COBOL | directory / program file | GnuCOBOL driver + stubs, or batch fixtures | statement (`-ftraceall`) or paragraph (`-ftrace`) | relational, `AND`/`OR`, `NOT`, `ADD`/`SUBTRACT`, `MULTIPLY`/`DIVIDE`, `COMPUTE` operators, `MOVE` literals, `PERFORM` → `CONTINUE`; fixed-format safe |

Shared behaviour:

- Up to two automatic import fixes per evaluation (doc loop and test loop).
- A file whose evaluation fails because the toolchain is missing gets status `no-toolchain` and
  no further LLM calls.
- Mixed repositories get one wiki and one business-logic graph across all languages, with
  cross-language edges for cgo (Go ↔ C) and COBOL `CALL 'x'` → C.

## Doc signal

The doc loop scores a doc by regenerating the target from it and running tests. Which tests:

1. **Relevant existing tests** that pass on the original code (signal `tests`).
2. Otherwise **CoDoc's delivered suite** for that file from a previous `testgen`
   (`generated-tests`; disable with `--no-generated-tests`).
3. Otherwise a **compile check** (`compile`).

COBOL replaces step 3 with its facts signal and uses it for spec programs and whenever `cobc` is
missing ([COBOL](cobol.md#facts-signal)).

## Suite names

| Language | Delivered suite in `OUT/tests/<pkg>/` |
|---|---|
| Go | `<stem>_codocs_test.go` |
| Python | `test_<module>_codocs.py` |
| Java | `<Class>CodocsTest.java` |
| C / C++ | `<stem>_codocs_test.c` / `.cpp` |
| COBOL unit / batch | `<STEM>_codocs_test.cbt` and the expanded `<STEM>_codocs_test/` directory |
| COBOL spec | `<STEM>_codocs_spec.json` / `.md` |

## Per-language notes

### Go

- `REPO` is the module root. Builds and tests run in a mirror under `OUT/work/sandbox/<repo>`
  with `go test -overlay`, so regenerated files and suites never touch the repository.
- `--go-flags "-tags=integration"` adds build flags; `--go-env KEY=VALUE` (repeatable) adds
  environment, with `{root}` expanding to the module root (e.g. `LD_LIBRARY_PATH={root}/lib`).

### Python

- Units are package directories. Projects are found by `pyproject.toml`, `setup.cfg`,
  `setup.py`, `pytest.ini` or `tox.ini`, which also give the pytest configuration.
- Tests run with the repository's `.venv`/`venv` interpreter if it has pytest and coverage, else
  CoDoc's own interpreter, or `--py`. Extra pytest arguments: `--pytest-args`.
- Relevant tests of a target are the tests in its companion test file and the tests whose
  body uses a name the target declares.

### Java

- Maven modules are detected from `pom.xml`; a tree without a build file is compiled with plain
  `javac`. Gradle builds are reported as `unsupported-build`.
- `--java-runner fast` (default) compiles with javac and runs JUnit through the Platform
  launcher; `--java-runner mvn` runs Maven surefire. The first run needs Maven Central.

### C and C++

- Generated suites include the target through `codocs_test.h` (`#include CODOCS_TARGET`), so no
  test framework is needed.
- Existing tests are recognised as sources with a `main`. Frameworks such as GoogleTest or
  Catch2 run only through `--test-cmd` (with `--build-cmd` and `--cc-root` when needed).
- Compile flags come from `--cc-db compile_commands.json`, `--cc-flags` and `--ld-flags`.

### COBOL

See [COBOL](cobol.md).
