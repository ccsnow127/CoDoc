# COBOL

COBOL goes through the same doc and test loops as the other languages. This page covers what is
specific to it. Tests need GnuCOBOL 3.2 (`cobc`); docs, the wiki, business logic and the judge
work without it.

## Static index

`codocs analyze` builds an index of the repository (written to `OUT/cobol/index.json`):

- programs, copybooks (including `COPY ... REPLACING`), JCL jobs and PROCs, BMS mapsets and CSD
  transactions;
- CICS, SQL and DL/I usage, and data stores;
- a graph with program, copybook, job, data, transaction and map nodes and edges for `CALL`,
  CICS `LINK`/`XCTL`/`START`, JCL steps and data access. Dynamic `CALL` targets are resolved from
  `VALUE` and `MOVE` literals;
- control-flow hazards: `NEXT SENTENCE`, `GO TO` out of a `PERFORM` range, `ALTER`,
  `PERFORM ... THRU` and fall-through.

Program extensions are `.cbl .cob .cobol .ccp .pco .sqb .cblle .sqlcblle .scbl .cicscbl .cbx`
(case-insensitive); copybooks `.cpy .copy .cpb .cblcpy .dcl .dclgen .cpybk .copybook`; members
`.jcl .prc .proc .job .bms .csd`. `--exclude-path GLOB` (repeatable, `CODOCS_EXCLUDE_PATHS`)
leaves files out of the index. `--programs A,B` restricts a run to some program ids.

## Program modes

Each program gets one of three modes:

| Mode | When | Tests |
|---|---|---|
| `unit` | `PROCEDURE DIVISION USING`, a `FUNCTION-ID`, or a parameterless called subprogram without native file I/O | a fixed-format driver that prints `TEST <id> PASS/FAIL`, linked with stubs of its callees |
| `batch` | a JCL step program receiving the `PARM` through one `USING` item (run by a job, no callers), or a program without `USING` that is a main program, has no callers or does native file I/O | golden files: input fixtures per DD, expected files, stdout and return code |
| `spec` | CICS, SQL, DL/I or screen I/O, in the program or in a program it calls, or curses terminal I/O | a structured, non-executable test specification |

- Batch fixtures are written field by field (`COMP-3`, zoned decimal, `OCCURS`); indexed (VSAM)
  files are loaded through a generated loader. A program called with a JCL `PARM` runs through a
  generated wrapper that passes the halfword-length `PARM`.
- A `PASS` printed outside a comparison is marked `weak`.
- Spec programs get one LLM call and no loop. Without `cobc`, unit and batch programs get status
  `no-toolchain` and no LLM call.
- Compile-only stub copybooks for common IBM interfaces (for example `DFHEIBLK`, `SQLCA`, `CMQV`)
  ship in `src/codocs/cobol/stubs/`, so programs that copy them can be compiled.
- `--cobc-flags` replaces the per-program dialect probing (e.g. `"-std=ibm -frelax-syntax-checks"`).
- Coverage is statement coverage from `-ftraceall`; `--cobol-coverage paragraph` uses `-ftrace`.

Each delivered bundle `<STEM>_codocs_test.cbt` is also expanded into
`OUT/tests/<pkg>/<STEM>_codocs_test/` with a README that gives the `cobc` command line, and
`codocs testrun OUT` re-runs all of them without the LLM. Build directories live in
`OUT/work/tmp/` and are removed afterwards unless `CODOCS_KEEP_WORK=1`.

## Docs

A COBOL doc has one `## ClassDef <PROGRAM-ID>` section (purpose, outline, invocation, interface,
inputs and outputs, processing logic, business rules, error handling, dependencies, control-flow
hazards) and one `## MethodDef` section per paragraph. `-l ja` or `-l ko` writes the prose of
COBOL docs and test plans in that language.

The doc loop regenerates the program from its doc with the `--codegen-model` model (default: the
`-m` model). This is the hardest LLM call in the pipeline; give it your strongest model.

### Facts signal

Unit and batch programs are scored by their tests (φ). Spec programs, every program when `cobc`
is missing, and unit and batch programs that have neither relevant existing tests nor a
delivered CoDoc suite use the **facts signal** instead:

- φ = Jaccard similarity of the *scored* facts × Jaccard similarity of the *structural* facts of
  the regenerated program and the original.
- Structural facts: program id, entry points, `USING` parameters, files, copybooks, data items,
  paragraphs. Scored facts: `PERFORM`, `CALL`, SQL, CICS, transfers of control, response
  handling, conditions, `WHEN` branches, `MOVE`s and literal text.
- Compile gate: if the original compiles (with CICS/SQL neutralised), a regenerated program that
  does not compile scores 0.

`--cobol-spec-signal compile` scores spec programs with a compile check instead.

## Judge

`codocs run` runs the judge stage when COBOL is selected and either this run wrote COBOL docs or
`--source-lang` names `cobol` explicitly;
`codocs judge OUT` runs it alone (`--programs` to restrict it).

- The judge model (`--judge-model`, default `gpt-5.5`) is deliberately not the generator's model.
- The judge checks up to 60 claims per doc against the source and verifies its evidence quotes.
  A checklist built from the static index (not by the LLM) measures coverage, and a deterministic
  check verifies the names the doc mentions.
- **Accuracy** = supported / (supported + contradicted). **Coverage** scores each checklist item
  yes = 1, partial = 0.5.
- **Pass gate:** no high-severity error, accuracy ≥ 0.9 and coverage ≥ 0.7.
- `--calibrate` measures the judge itself: it injects five kinds of errors (flipped comparison,
  swapped file access, wrong paragraph attribution, changed value, fabricated rule) into `--sample`
  docs (default 5) and judges each `--repeats` times (default 3) to measure sensitivity and
  run-to-run stability.

## Wiki

A COBOL-only wiki uses a COBOL page planner (JCL jobs, CICS transactions, call graph) and adds the
appendix pages `program-index`, `copybook-index`, `batch-jobs` and `data-stores`. Mermaid edges
are checked against the index. `codocs translate --glossary auto` picks the COBOL glossary (CICS,
JCL, VSAM, KSDS, ...) for COBOL wikis.

## Public COBOL repositories to try

| Repository | License | Good for |
|---|---|---|
| [shamrice/cbi](https://github.com/shamrice/cbi) | GPL-3.0 | executable unit tests (GnuCOBOL BASIC interpreter) |
| [aws-samples/aws-mainframe-modernization-carddemo](https://github.com/aws-samples/aws-mainframe-modernization-carddemo) | Apache-2.0 | CICS online + VSAM batch: wiki, judge, batch tests |
| [cicsdev/cics-genapp](https://github.com/cicsdev/cics-genapp) | EPL-2.0 | layered CICS/DB2 application: wiki, spec-mode tests |

```bash
codocs analyze cbi -o out/cbi                    # lists the program ids
codocs run cbi -o out/cbi --programs LTRIM
codocs testrun out/cbi
codocs judge out/cbi --calibrate
```

`scripts/cobol/fetch_datasets.sh` clones these at the commits pinned in
`datasets/manifest.json` into `$CODOCS_DATASETS` (they are never vendored); the dataset tests in
`tests/cobol/` use them when `CODOCS_DATASETS` is set.

## Limits

- φ only measures behaviour the tests exercise.
- CICS, DB2, IMS and screen programs are not executed; they get specifications and the facts
  signal.
- Production code written for other compilers may need `--cobc-flags`, and some of it does not
  compile with GnuCOBOL at all.
