# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.x, command-line options may still change between minor versions.

## [Unreleased]

First public release, to be tagged as `v0.1.0`.

### Added

- `codocs` command with 12 subcommands: `analyze`, `baseline`, `testgen`, `docgen`, `wiki`,
  `run`, `bizlogic`, `bizlogic-render`, `judge`, `translate`, `testrun` and `report`.
- Documentation loop after SpecForge: each file's doc is scored by regenerating the file from the
  doc alone and running the relevant tests (φ), with generated suites and a compile check as
  fallbacks.
- Unit-test loop after TeSearch: failing tests pruned, suites scored by coverage × mutation
  score, uncovered lines and surviving mutants fed back.
- Language support for Go, Python, Java (Maven and plain javac), C, C++ and COBOL (GnuCOBOL),
  including mixed-language repositories.
- COBOL: repository index (copybooks, JCL, BMS, CSD, CICS/SQL/DL/I usage), program modes
  (unit, batch, spec), GnuCOBOL test bundles, the facts signal and an LLM doc judge with a
  calibration mode.
- Repository wiki built on a vendored, modified fork of RepoWiki, with Mermaid validation and
  repair, single-file HTML export and structure-preserving translation.
- Business-logic analysis (call graph, hierarchical clusters, random-walk trajectories), domain
  neutral by default, with optional domain profiles (`--domain-profile`) and an example
  `3gpp-5gc` profile.
- Any OpenAI-compatible endpoint via `OPENAI_API_KEY` / `OPENAI_BASE_URL`; model aliases `gpt`,
  `gpt-mini` and `judge`; `--codegen-model` for COBOL regeneration and `--judge-model` for the
  COBOL judge.
- SQLite cache of LLM replies at `~/.codocs/cache.db`, resumable runs, and a sandbox that never
  writes into the analysed repository.
- Documentation under `docs/`, `CITATION.cff`, contributing guide, security policy and CI.

[Unreleased]: https://github.com/ccsnow127/CoDoc/commits/main
