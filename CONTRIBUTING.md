# Contributing to CoDoc

Thanks for your interest. Bug reports, fixes, new language adapters and documentation
improvements are all welcome. By taking part you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

```bash
git clone https://github.com/ccsnow127/CoDoc.git && cd CoDoc
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # codocs + pytest, pytest-asyncio, pytest-timeout, ruff, pre-commit
pre-commit install
```

For the toolchain-dependent tests, use the conda environment instead (`./install.sh`, then
`conda activate codocs` and `pip install -e ".[dev]"`); it has Go, a JDK, Maven, GnuCOBOL and, on
Linux, GCC. See [docs/installation.md](docs/installation.md).

## Running the tests

The test suite needs no LLM and no API key: LLM calls are replaced by fakes.

```bash
pytest -m "not slow" -q             # fast run; tests whose toolchain is missing are skipped
pytest -q --timeout 900             # everything, with the toolchains on PATH
pytest tests/cobol -q               # one area: tests/codocs, tests/cobol, tests/repowiki
```

- Tests that need `go`, `javac`/`mvn`, `cc`/`c++` or `cobc` skip themselves when the tool is not on
  `PATH`, so a plain venv gives a green but partial run.
- Markers: `slow` (Maven builds, browser rendering and other long runs) and `needs_cobc`
  (GnuCOBOL). Deselect with `-m "not slow"`.
- The COBOL dataset tests run only when `CODOCS_DATASETS` points at clones made with
  `scripts/cobol/fetch_datasets.sh`.
- The Go golden test records its output with one Go version; re-record with
  `CODOCS_GOLDEN_UPDATE=1 pytest tests/codocs/test_go_golden.py` when a change is intended.
- Fixture repositories live in `tests/fixtures/` and must stay synthetic or permissively
  licensed.

CI runs `ruff check`, the fast suite on Linux and macOS for Python 3.10 to 3.13, and the full
suite with every toolchain on Linux.

## Code style

- `ruff check .` must pass (`pre-commit run --all-files` runs it with the other hooks).
  The code base is not auto-formatted; match the surrounding style.
- Docstrings and comments are short and lowercase, as in the existing modules.
- Keep the guarantee that nothing is written into the analysed repository: every build and test
  goes through the sandbox, an overlay or a temporary directory under `OUT/work/`.
- New behaviour needs a test that runs without an LLM.

## Adding a language

Language support lives in `src/codocs/lang/`:

1. **Adapter.** Subclass `LangAdapterBase` (`lang/base.py`) and implement the static half of the
   `LanguageAdapter` protocol: file ownership, test-file detection, declarations and imports,
   target selection, package context, relevant tests, suite naming, mutation sites, import
   repair and the doc and test prompt messages. `lang/python.py`, `lang/java.py` and `lang/cc.py`
   are complete examples.
2. **Runner.** Implement the `LangRunner` protocol (build, run tests with an overlay, coverage)
   in a module like `lang/pyrun.py`, and return it from the adapter's `make_runner`.
3. **Registration.** Add the extensions to `SOURCE_EXTS` and the adapter to `ADAPTERS` in
   `lang/registry.py`, and the language to `LANG_ORDER` / `DISPLAY` in `lang/base.py`.
4. **Prompts.** Add `src/codocs/prompts/lang/<lang>.txt` with the language-specific prompt
   fragments (`=== SLOT ===` sections, see the existing files).
5. **Tests and fixture.** Add a small synthetic fixture repository under `tests/fixtures/` and
   adapter, runner and end-to-end tests that skip when the toolchain is missing.
6. **Docs.** Add the language to [docs/languages.md](docs/languages.md),
   [docs/installation.md](docs/installation.md) and the README tables.

## Pull requests

- [ ] One topic per pull request, with a short description of the change and why.
- [ ] Tests added or updated; `pytest -m "not slow"` passes locally.
- [ ] `pre-commit run --all-files` passes.
- [ ] User-visible changes are noted under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md), and
      [docs/](docs/index.md) is updated for new options.
- [ ] No secrets, API keys, `.env` files, LLM outputs of private code, or proprietary or
      customer source code anywhere in the change, including test fixtures.

## Reporting bugs and security issues

Open an issue with the bug template. Remove API keys and proprietary code from logs first.
Report security vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
