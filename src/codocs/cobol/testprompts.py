"""COBOL testgen prompts.

`build_test_prompt` is the one-shot prompt of a program; the loop's user messages are built
from its parts: `seed_user` (round 1: the prompt body), `refine_user` (marked source, the
current best plan and the contract sections again) and `spec_user` (the one call of a spec
program). The record layouts, file table, CICS facts and the control-flow block come from
codocs.cobol.layout.
"""

from __future__ import annotations

import re

from codocs.cobol.classify import SPEC_TERMINAL, spec_kind
from codocs.cobol.layout import (
    _COPYBOOK_CAP,
    _COPYBOOKS_TOTAL_CAP,
    _FLOW_RULES,
    _LANG_NAMES,
    _LEAFDOC_CAP,
    _SOURCE_CAP,
    _files_table,
    cics_facts,
    dialect_of,
    display_source,
    driver_hint,
    exact_program_name,
    flow_section,
    layouts_for,
    working_storage_layouts,
)
from codocs.cobol.models import CobolProgram
from codocs.cobol.testmodels import Testability

__all__ = ["build_test_prompt", "seed_user", "refine_user", "spec_user", "SYSTEM",
           "TC_SHAPE_RULE", "_FLOW_RULES", "cics_facts", "flow_section"]

# measured with GnuCOBOL 3.2: how DISPLAY renders numeric items in each dialect
_DISPLAY_RULES = {
    "ibm": (
        "DISPLAY output of numeric items under -std=ibm: unsigned zoned 9(5)V99 holding "
        "12.5 prints `0001250` (no decimal point); signed zoned S9(5)V99 prints the digits "
        "then the sign: -12.5 -> `0001250-`, +12.5 -> `0001250+`; COMP-3 prints a leading "
        "sign when signed (`-0001250`); binary S9(4) COMP holding 42 prints `+00042` "
        "(digits of the full binary capacity), 9(4) COMP prints `00042`; edited items "
        "print as edited. A group item prints its raw bytes: no signs or decimal points, "
        "a negative signed zoned field ends in an overpunched letter (-12.5 in S9(5)V99 "
        "-> `000125p`; p..y mean -0..-9)."
    ),
    "default": (
        "DISPLAY output of numeric items in the default GnuCOBOL dialect: the decimal "
        "point is shown and signed items get a leading sign: 9(5)V99 holding 12.5 prints "
        "`00012.50`, S9(5)V99 holding -12.5 prints `-00012.50`, S9(4) holding 42 prints "
        "`+0042`; edited items print as edited. A group item prints its raw bytes: no "
        "signs or decimal points, a negative signed zoned field ends in an overpunched "
        "letter (-12.5 in S9(5)V99 -> `000125p`; p..y mean -0..-9)."
    ),
}

_SYSTEM = """\
You are an expert COBOL test engineer. You write characterization tests for existing \
COBOL programs: tests that PASS against the code exactly as written, pinning down its \
current behavior (quirks included). Tests are compiled and run with GnuCOBOL 3.2 on \
Linux/macOS, not on a mainframe. You trace the code carefully before writing any \
expected value. You answer with exactly one JSON object and nothing else."""

_FILES_RULES = """\
### Files
The harness runs in an empty directory and maps every SELECT ... ASSIGN name to a file \
there (environment variables DD_<name>, dd_<name> and <name> all point to the file).
- "inputs": [{{"dd": "<ASSIGN name exactly as in the file table>", "path": "<relative file \
name>", "content": [record, record, ...]}}]
- A record is either a JSON object {{"FIELD": "value", ...}} or a string.
  - Objects (preferred for every file that has a record layout table): keys are field \
names from that file's layout tables (the FD record or a same-size record; "NAME(2)" for \
OCCURS entries), values are plain values ("123.45", "-7", "A", "2024-01-31"). The \
harness builds the bytes: alphanumeric fields are left-justified and space-padded, numeric \
fields get zero padding, implied decimals, sign and COMP/COMP-3 encoding. Unlisted fields \
are spaces (alphanumeric) or zero (numeric). Never count columns yourself.
  - Strings are whole raw records/lines: use them for free-form text files (CSV, report \
lines) that have no layout table.
- Every organization works the same way: LINE SEQUENTIAL files are written directly; \
SEQUENTIAL (record) and INDEXED files are loaded record by record by a generated loader \
that uses the program's own SELECT/FD. INDEXED records need unique keys (any order).
- When the program ASSIGNs to a data item (dynamic file name), use the name the program \
puts in that item as "dd" and the relative path it resolves to as "path" (sub directories \
are created).
- "expected_files": files the program writes, same shape. With object records only the \
listed fields are compared (numerically for numeric fields), so list the fields you are \
sure about; with strings the whole record is compared (trailing spaces ignored). The \
number of records must match exactly. Only list outputs you are sure about."""

_STUB_RULES = """\
### Stubs
By default the harness compiles and links the program under test and its repository \
callees ({callees}) for real. You MAY stub a repository callee instead when that isolates \
the unit (the callee needs files, a terminal or other programs, or its behavior is not what \
the case is about): put a stub with the callee's exact PROGRAM-ID in "stubs_source" and the \
harness links your stub INSTEAD of the real program. Every expected value must then follow \
from your stub's behavior, not the real callee's. A program with ENTRY points is replaced \
only when every one of its names the SUT calls is stubbed. Never stub the program under \
test or its own ENTRY points. These CALL targets are not in the repository and MUST be \
stubbed: {externals}.
Put the stubs in "stubs_source": one complete fixed-format program per target \
(IDENTIFICATION DIVISION, PROGRAM-ID. <the exact CALL literal>, a LINKAGE SECTION that \
matches the caller's USING arguments, GOBACK). Make stubs deterministic: return success \
codes, or the data your cases need. Abend routines (CEE3ABD, ILBOABN0, ...) should DISPLAY \
'ABEND' and STOP RUN. Leave "stubs_source" empty when there is nothing to stub."""

_UNIT_CONTRACT = """\
## What to produce (mode: unit)
A COBOL driver program that CALLs the program under test (the SUT) directly.

### Driver rules
1. Fixed reference format: 7 leading spaces, code in columns 8-72, '*' in column 7 for \
comments. Never write continuation lines ('-' in column 7): put every literal, however \
long, on one line; the harness re-wraps long lines and splits long literals correctly. \
Keep one statement per line.
2. PROGRAM-ID is "driver_name": at most 8 characters, e.g. "{driver_hint}", and different \
from every program in the repository.
3. WORKING-STORAGE declares one record per SUT parameter, byte-for-byte identical to the \
SUT's LINKAGE items (same levels, PIC and USAGE). When a LINKAGE item comes from a \
copybook, COPY that same copybook (the copybook directories are on the include path); \
otherwise copy the definitions verbatim. Prefix every driver-only data name with "TD-".
4. Each case: INITIALIZE every parameter record, MOVE the inputs (including fields the SUT \
expects pre-filled), CALL '{sut_name}' USING {using} (this exact order), check the outputs, \
then CANCEL '{sut_name}' so the next case starts with fresh WORKING-STORAGE.
5. Each case prints exactly one line: DISPLAY 'TEST <id> PASS' or DISPLAY 'TEST <id> FAIL ' \
followed by expected and actual values. <id> is the case id (letters, digits, '-', '_'). \
Every PASS must sit inside an IF (or EVALUATE WHEN) that compares a value the SUT produced \
(an output parameter, RETURN-CODE, a field it changed) with the expected value, and the \
ELSE branch prints FAIL. Never DISPLAY PASS unconditionally: "it returned" checks \
nothing, and the harness grades such cases as weak, not passed.
6. After the last case: STOP RUN. The driver itself must not ACCEPT, open files or use the \
current date/time.
7. A SUT path that executes STOP RUN (or an abend stub) ends the whole run, so the driver \
cannot print that case's TEST line: make such a case the LAST one, set "ends_run": true on \
it, and grade it through "expected_stdout" (what the SUT DISPLAYs before it stops) and \
"expected_return_code".
8. Compare numeric fields numerically (IF X = 12.5) and alphanumeric fields against \
literals (trailing spaces are implied). Assert the fields you are certain about; when the \
exact text of a message is hard to predict, assert a reference-modified prefix or fewer \
fields rather than guessing.
9. GnuCOBOL 3.2 syntax{dialect_note}. Avoid reserved words as data names.

### Example driver (shape only)
```cobol
       IDENTIFICATION DIVISION.
       PROGRAM-ID. TADDAMT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY AMTPARM.
       PROCEDURE DIVISION.
      * C01: two positive amounts are added
           INITIALIZE AMT-PARMS
           MOVE 100.25 TO AMT-IN-1
           MOVE 50 TO AMT-IN-2
           CALL 'ADDAMT' USING AMT-PARMS
           IF AMT-TOTAL = 150.25 AND AMT-RC = '00'
               DISPLAY 'TEST C01 PASS'
           ELSE
               DISPLAY 'TEST C01 FAIL total=' AMT-TOTAL ' rc=' AMT-RC
           END-IF
           CANCEL 'ADDAMT'
      * C02: ... more cases ...
           STOP RUN.
```

Output checks (optional, recommended when the SUT DISPLAYs): "expected_stdout" lists text \
the run prints, in order (see Output checks); "expected_return_code" is the exit code of \
the driver's STOP RUN (RETURN-CODE: the value the last CALLed program left in it, or null \
to skip). If the SUT opens files, also provide "inputs" (and optionally "expected_files") \
as below; they are created before the driver runs."""

_STDOUT_RULES = """\
### Output checks
"expected_stdout" is checked as an ordered subsequence of the run's output: each entry \
must occur after the previous entry's match, and each occurrence is consumed once, so list \
repeated lines as often as they are printed and in print order. A string entry matches as \
a substring of one line (several entries may match in the same line, left to right); \
{{"line": "..."}} must equal a whole line (trailing spaces ignored). Only include text you \
are certain of; avoid runs of spaces whose exact width depends on PIC editing (check \
"Total paid is:" and "185.00" as two entries)."""

_FUNCTION_NOTE = """\
### The SUT is a user-defined FUNCTION
'{sut_name}' is a FUNCTION-ID, not a program: rule 4 changes. Declare it in the driver \
(ENVIRONMENT DIVISION. CONFIGURATION SECTION. REPOSITORY. FUNCTION {sut_name}.) and \
invoke it inside an expression with the USING arguments in order, e.g. \
MOVE FUNCTION {sut_name}({using_args}) TO TD-RESULT, where TD-RESULT matches its \
RETURNING item{returning}. Never CALL or CANCEL it; INITIALIZE the argument records \
before each case."""

_UNBOUNDED_NOTE = """\
Copybooks with `OCCURS ... TO UNBOUNDED` tables are legal only in LINKAGE; COPY them \
into WORKING-STORAGE as usual: the harness bounds those tables to 9999 entries there."""

_ENTRY_NOTE = """\
The SUT also has ENTRY points ({entries}); CALL them by those literals when a case needs \
them. Do not CANCEL the SUT between cases: GnuCOBOL 3.2 can crash when an ENTRY of a \
CANCELled program is called again. Its WORKING-STORAGE therefore persists across cases: \
order the cases so each one's expectations account for what earlier cases left behind."""

_BATCH_CONTRACT = """\
## What to produce (mode: batch)
The program runs ONCE as a main program (cobc -x) in an empty directory with your input \
files; then its outputs are compared with your expectations. Design the input data so that \
this single run exercises as many branches as possible (valid records, each kind of \
rejected record, key-not-found, end of file, totals).
- "inputs" / "expected_files": see Files.
- "expected_stdout": text the DISPLAY output must contain, in order (see Output checks).
- "expected_return_code": the exit code (RETURN-CODE at STOP RUN/GOBACK; 0 unless the \
program sets RETURN-CODE or a stub abends), or null to skip the check.
- "stdin": lines fed to ACCEPT statements that read from SYSIN/console, else [].
- "env": extra environment variables the program reads (ACCEPT ... FROM ENVIRONMENT), \
normally {{}}; "${{WORK_DIR}}" in a value is replaced by the run directory.
- "cases": one entry per scenario the run covers (e.g. per interesting record), with \
given/when/then and inputs/expected maps naming the records/fields involved."""

_PARM_NOTE = """\
### JCL PARM
The program is a JCL step program that receives the EXEC PARM through PROCEDURE DIVISION \
USING {param}. The harness runs it through a generated main program that CALLs \
'{sut_name}' USING a PARM structure laid out like z/OS passes it: a binary halfword \
(PIC S9(4) COMP) holding the length of the text, then the text itself (up to 100 bytes). \
Set "parm" in your answer to the PARM text (without quotes); its length is computed. \
{jcl}The program's view of it is the {param} layout below."""

_GROUP_RULES = """\
Byte-level rules for the layouts: positions are 1-based; a group item (DISPLAY, or MOVE to \
an alphanumeric item) is its bytes in layout order, with each FILLER / item holding its \
VALUE padded with spaces to its length until the program MOVEs into it; INITIALIZE sets \
named alphanumeric items to spaces and numeric items to zero but leaves FILLER unchanged; \
binary (COMP / BINARY / COMP-5) and packed (COMP-3) fields inside a group are raw bytes, \
not digits (a halfword holding 10 is X'000A': a NUL and a newline byte, and MOVE of such a \
group to PIC X copies those bytes); an 88-level VALUE X'...' sets the bytes of its parent, \
so a binary field under it takes the big-endian value of its bytes (X'09CB' -> 2507). In \
"initial bytes", '~' marks bytes no VALUE sets and '#' binary / packed bytes."""

_SPEC_CONTRACT = """\
## What to produce (mode: spec)
This program cannot run outside the mainframe ({reasons}). Write a structured, \
non-executable test specification that a mainframe team can implement later (CICS / \
DB2 / IMS test harness).
- "cases": each with "given" (preconditions with concrete values: COMMAREA fields, map \
input fields, DB2 rows, VSAM records, TS queue contents), "when" (the trigger: transaction \
+ key pressed, LINK with COMMAREA, ...), "then" (observable result: screen message, \
program XCTL'd/LINKed to, records written/updated, SQLCODE handling, RETURN TRANSID), plus \
"inputs" and "expected" maps of field -> concrete value.
- Leave driver_source, stubs_source, inputs and expected_files empty."""

_SPEC_TERMINAL_CONTRACT = """\
## What to produce (mode: spec)
This program cannot run headless ({reasons}): it needs an interactive (curses) terminal. \
Write a structured, non-executable test specification that a tester can run by hand or \
with a terminal-automation harness.
- "cases": each with "given" (preconditions with concrete values: parameters passed in, \
terminal size, keys typed and their timing), "when" (the call or key sequence), "then" \
(observable result: text and its screen position/colors, values returned in the \
parameters, RETURN-CODE), plus "inputs" and "expected" maps of field -> concrete value.
- Leave driver_source, stubs_source, inputs and expected_files empty."""

_CICS_RULES = """\
### CICS facts (computed from the source; use them when writing given/then)
{facts}
- WORKING-STORAGE is fresh in every task: in a pseudo-conversational re-entry (RETURN \
TRANSID ... COMMAREA, then a new task) WORKING-STORAGE items hold only their VALUE clause \
initial values (items without VALUE are undefined: never predict them) and nothing from \
the previous task; state survives only through the COMMAREA the program reads from \
DFHCOMMAREA, TS/TD queues, files or tables.
- EIBCALEN is the length of the COMMAREA passed to this task (0 on a first start from a \
terminal); referencing DFHCOMMAREA while EIBCALEN = 0 is an addressing exception (abend \
ASRA), unless the code checks EIBCALEN first.
- EXEC CICS LINK passes the COMMAREA by reference: what the linked program changes is \
visible to the caller after the LINK returns; XCTL does not return.
- EXEC CICS FORMATTIME without DATESEP / TIMESEP returns digits only (MMDDYYYY -> \
'10142026', YYYYMMDD -> '20261014', TIME -> 'hhmmss'); DATESEP / TIMESEP without a value \
insert '/' and ':'."""

_OUTPUT = """\
## Output
Return ONE JSON object (no markdown fences, no commentary) with these keys:
{{
  "driver_name": "{driver_hint}",
  "driver_source": "<unit mode: the complete driver program, else empty>",
  "stubs_source": "<stub programs, or empty>",
  "inputs": [],
  "expected_files": [],
  "expected_stdout": [],
  "expected_return_code": null,
  "stdin": [],
  "env": {{}},{extra_keys}
  "cases": [
    {{"id": "C01", "title": "...", "given": "...", "when": "...", "then": "...",
      "inputs": {{"FIELD": "value"}}, "expected": {{"FIELD": "value"}}{case_keys}}}
  ]
}}
Write {n_cases} cases that together reach every branch and paragraph you can: the normal \
path, each validation/error path, boundary values. Case ids are unique (C01, C02, ...); \
for unit mode every case id must appear in exactly one TEST line of the driver. Derive \
every expected value by tracing the SUT source for that input; the tests must pass \
against the code as given.{lang_note}"""


def prompt_parts(
    t: Testability,
    program: CobolProgram,
    source: str,
    copybooks: dict[str, str],
    leafdoc_md: str | None,
    language: str = "en",
    callee_programs: list[CobolProgram] | None = None,
    dialect: str | None = None,
) -> list[tuple[str, str]]:
    """the prompt body as (section tag, text) parts, in order; tags: head,
    contract, rules, facts, flow, leafdoc, copybooks, source, output."""
    dialect = dialect if dialect in ("ibm", "default") else dialect_of(program.format)
    sut_name = exact_program_name(source, program.program_id)
    is_function = getattr(program, "unit_kind", "program") == "function"
    tagged: list[tuple[str, str]] = []
    tag = ["head"]

    class _Parts(list):
        def append(self, text: str) -> None:  # every part, labelled by section
            tagged.append((tag[0], text))

    parts = _Parts()
    ident = ("- exact FUNCTION-ID (declare in REPOSITORY, invoke as FUNCTION "
             f"{sut_name}(...)): '{sut_name}'\n" if is_function else
             f"- exact PROGRAM-ID (use in CALL/CANCEL literals): '{sut_name}'\n")
    parts.append(
        f"# Program under test: {program.program_id}\n"
        f"- file: `{program.path}` ({program.format} format, {program.sloc} SLOC)\n"
        + ident +
        "- compiled with: GnuCOBOL 3.2, "
        + ("-std=ibm (IBM Enterprise COBOL dialect: binary fields 2/4/8 bytes, no binary "
           "truncation)" if dialect == "ibm" else "default GnuCOBOL dialect")
        + "\n"
        f"- {_DISPLAY_RULES[dialect]}\n"
        f"- test mode: **{t.mode}** ({'; '.join(t.reasons) or 'n/a'})"
    )
    if program.using_params:
        tag[0] = "head"
        parts.append("- PROCEDURE DIVISION USING " + " ".join(program.using_params)
                     + (f" RETURNING {program.returning}" if program.returning else ""))
    if program.paragraphs:
        names = [p.name for p in program.paragraphs if p.name != "(entry)"]
        parts.append("- paragraphs (coverage is measured on these): " + ", ".join(names[:80]))

    tag[0] = "contract"
    if t.mode == "spec" and spec_kind(t) == SPEC_TERMINAL:
        parts.append(_SPEC_TERMINAL_CONTRACT.format(reasons="; ".join(t.reasons)))
    elif t.mode == "spec":
        parts.append(_SPEC_CONTRACT.format(reasons="; ".join(t.reasons)))
        if program.has_cics:
            parts.append(_CICS_RULES.format(facts=cics_facts(program, source, dialect)))
    else:
        if t.mode == "unit":
            note = (" with -std=ibm" if dialect == "ibm" else "")
            parts.append(_UNIT_CONTRACT.format(
                driver_hint=driver_hint(program.program_id), sut_name=sut_name,
                using=" ".join(program.using_params) or "(no arguments)", dialect_note=note,
            ))
            if is_function:
                parts.append(_FUNCTION_NOTE.format(
                    sut_name=sut_name, using_args=" ".join(program.using_params),
                    returning=f" ({program.returning})" if program.returning else ""))
            if program.entry_points:
                parts.append(_ENTRY_NOTE.format(entries=", ".join(program.entry_points)))
            if any(re.search(r"\bTO\s+UNBOUNDED\b", t, re.I) for t in copybooks.values()):
                parts.append(_UNBOUNDED_NOTE)
        else:
            parts.append(_BATCH_CONTRACT.format())
            if t.parm_param:
                jcl = ("JCL passes " + ", ".join(f"PARM='{x}'" for x in t.jcl_parms[:3])
                       + ". ") if t.jcl_parms else ""
                parts.append(_PARM_NOTE.format(param=t.parm_param, sut_name=sut_name, jcl=jcl))
        tag[0] = "rules"
        parts.append(_STDOUT_RULES.format())
        parts.append(_FILES_RULES.format())
        entries = {c.program_id: c.entry_points for c in (callee_programs or [])}
        parts.append(_STUB_RULES.format(
            callees=", ".join(
                c + (f" (and its ENTRY points {', '.join(entries[c])})" if entries.get(c) else "")
                for c in t.callees) or "none",
            externals=", ".join(t.external_callees) or "none",
        ))
        tag[0] = "facts"
        table = _files_table(program, callee_programs)
        if table:
            parts.append("### File table (from the SELECT / FD of the SUT and its callees)\n"
                         + table)
        blocks = layouts_for(program, t, dialect)
        ws_blocks = working_storage_layouts(program, source, dialect)
        if blocks:
            parts.append(
                "### Record layouts (computed from PIC/USAGE; pos is 1-based)\n```\n"
                + "\n\n".join(blocks) + "\n```"
            )
        if ws_blocks:
            parts.append(
                "### WORKING-STORAGE groups whose bytes reach the output (computed)\n```\n"
                + "\n\n".join(ws_blocks) + "\n```"
            )
        if blocks or ws_blocks:
            parts.append(_GROUP_RULES)
    tag[0] = "flow"
    flow = flow_section(program, source)
    if flow:
        parts.append(flow)

    tag[0] = "leafdoc"
    if leafdoc_md:
        md = leafdoc_md if len(leafdoc_md) <= _LEAFDOC_CAP else leafdoc_md[:_LEAFDOC_CAP] + "\n…"
        parts.append("## Documentation of the program (generated, may be imperfect)\n" + md)

    tag[0] = "copybooks"
    if copybooks:
        cb_parts = []
        used = 0
        for name, text in copybooks.items():
            shown = display_source(text)
            if len(shown) > _COPYBOOK_CAP:
                shown = shown[:_COPYBOOK_CAP] + "\n... (truncated)"
            if used + len(shown) > _COPYBOOKS_TOTAL_CAP:
                cb_parts.append(f"### {name}\n(omitted: context budget)")
                continue
            used += len(shown)
            cb_parts.append(f"### {name}\n```cobol\n{shown}\n```")
        parts.append("## Copybooks\n" + "\n\n".join(cb_parts))

    tag[0] = "source"
    shown = display_source(source, program.format)
    if len(shown) > _SOURCE_CAP:
        shown = shown[:_SOURCE_CAP] + "\n... (truncated)"
    parts.append(f"## Source of {program.program_id}\n```cobol\n{shown}\n```")

    lang = _LANG_NAMES.get(language, language)
    lang_note = "" if language == "en" else (
        f" Write titles and given/when/then texts in {lang}; keep COBOL code, ids and data "
        "names unchanged."
    )
    tag[0] = "output"
    parts.append(_OUTPUT.format(
        driver_hint=driver_hint(program.program_id) if t.mode == "unit" else "",
        n_cases="5-12" if t.mode != "batch" else "3-10",
        lang_note=lang_note,
        extra_keys='\n  "parm": "<the PARM text>",' if t.mode == "batch" and t.parm_param else "",
        case_keys=', "ends_run": false' if t.mode == "unit" else "",
    ))
    return tagged


def build_test_prompt(
    t: Testability,
    program: CobolProgram,
    source: str,
    copybooks: dict[str, str],
    leafdoc_md: str | None,
    language: str = "en",
    callee_programs: list[CobolProgram] | None = None,
    dialect: str | None = None,
) -> list[dict]:
    """messages for the single test-generation call of one program (one-shot).

    *callee_programs* (optional) are the repo programs linked with the SUT; the files
    they open are listed too, since the run needs those fixtures as well. *dialect*
    ('ibm' | 'default') is the one the runner builds the program with (default: the
    preferred one of its source format).
    """
    parts = prompt_parts(t, program, source, copybooks, leafdoc_md, language,
                         callee_programs, dialect)
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n\n".join(text for _, text in parts)},
    ]


# -- the loop's user messages ------------------------------------------------------------

SYSTEM = _SYSTEM

TC_SHAPE_RULE = """\
### Case paragraphs
Write each case as one paragraph TC-<id> of the driver (the case id in its name), \
PERFORMed in order from the driver's first paragraph; the paragraph ends in exactly one \
DISPLAY 'TEST <id> PASS' or DISPLAY 'TEST <id> FAIL ' ... (inside the IF / ELSE that checks \
the outcome). The harness inserts DISPLAY 'TEST <id> RUN' as the first statement of each \
TC-<id> paragraph, so a case that crashes the run is blamed alone."""

REFINE_OUTPUT = ("Return the improved COMPLETE test plan JSON: keep the passing cases, repair "
                 "the failing ones, add cases for the marked gaps.")
FIX_OUTPUT = ("Return the corrected COMPLETE test plan JSON: fix every reported build error "
              "while keeping the plan's testing intent.")
_REFINE_TAGS = ("head", "contract", "rules", "facts", "flow", "copybooks")


def seed_user(target_rel: str, package: str, t: Testability, program: CobolProgram,
              source: str, copybooks: dict[str, str], callees: list[CobolProgram],
              dialect: str | None, reference: str = "", language: str = "en") -> str:
    """round 1: [TARGET FILE], [PACKAGE], the prompt body (no leaf doc), the existing test
    program as reference, the output section and the TC-paragraph rule."""
    out = [f"[TARGET FILE] {target_rel}", f"[PACKAGE] {package}"]
    for tag, text in prompt_parts(t, program, source, copybooks, None, language, callees,
                                  dialect):
        if tag == "output" and reference.strip():
            out.append("[EXISTING TEST CODE OF THE PROJECT - reference only: this test program "
                       "is hidden while your plan runs; it shows how the project calls the "
                       f"program and what it checks]\n```cobol\n{reference.rstrip()}\n```")
        out.append(text)
    if t.mode == "unit":
        out.append(TC_SHAPE_RULE)
    return "\n\n".join(out)


def refine_user(target_rel: str, package: str, t: Testability, program: CobolProgram,
                source: str, copybooks: dict[str, str], callees: list[CobolProgram],
                dialect: str | None, *, marked_source: str, best_block: str = "",
                last_block: str = "", fixing: bool = False, attempt: int = 0,
                reference: str = "", language: str = "en") -> str:
    """a refine round: the marked display source, the current best plan (failing tests
    marked), the last attempt, the contract sections again and the output rule."""
    out: list[str] = []
    if attempt:
        out.append(f"[ROUND {attempt}]")
    out += [f"[TARGET FILE] {target_rel}", f"[PACKAGE] {package}", marked_source.rstrip()]
    if best_block:
        out.append(best_block.rstrip())
    if last_block:
        out.append(last_block.rstrip())
    for tag, text in prompt_parts(t, program, source, copybooks, None, language, callees,
                                  dialect):
        if tag in _REFINE_TAGS:
            out.append(text)
    if reference.strip():
        out.append("[EXISTING TEST CODE OF THE PROJECT - reference only]\n```cobol\n"
                   f"{reference.rstrip()}\n```")
    if t.mode == "unit":
        out.append(TC_SHAPE_RULE)
    out.append(FIX_OUTPUT if fixing else REFINE_OUTPUT)
    return "\n\n".join(out)


def spec_user(t: Testability, program: CobolProgram, source: str,
              copybooks: dict[str, str], language: str = "en") -> str:
    """the spec prompt body (one call, no loop)."""
    return "\n\n".join(text for _, text in prompt_parts(t, program, source, copybooks, None,
                                                         language))
