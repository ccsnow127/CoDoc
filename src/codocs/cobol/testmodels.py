"""data models for COBOL test generation and execution."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Testability(BaseModel):
    """how a program can be tested outside the mainframe."""

    __test__ = False  # not a pytest class

    program_id: str
    mode: str  # unit | batch | spec
    reasons: list[str] = Field(default_factory=list)
    callees: list[str] = Field(default_factory=list)  # repo programs that must be built as modules
    external_callees: list[str] = Field(default_factory=list)  # unresolved CALL targets (need stubs)
    # batch programs that receive the JCL PARM via PROCEDURE DIVISION USING: the USING item
    # a generated wrapper main passes (halfword length + text), and the PARMs JCL gives
    parm_param: str = ""
    jcl_parms: list[str] = Field(default_factory=list)
    # spec mode only: why it cannot run here: "CICS/DB2" (mainframe middleware, also
    # through a callee) | "terminal" (curses / SCREEN SECTION I/O)
    spec_kind: str = ""


class FixtureFile(BaseModel):
    """an input or expected-output file for a batch test (line-sequential text)."""

    dd: str  # DD / ASSIGN name the program uses
    path: str  # file name inside the test work dir
    content: str = ""
    organization: str = "LINE SEQUENTIAL"  # INDEXED inputs are loaded via a generated loader
    # records given as field -> value maps (encoded / compared via the record layout);
    # plain strings are whole records. Empty: `content` holds the records, one per line
    records: list[str | dict[str, str]] = Field(default_factory=list)


class TestCaseSpec(BaseModel):
    """one test case as described by the LLM (all modes)."""

    __test__ = False  # not a pytest class

    id: str
    title: str = ""
    given: str = ""
    when: str = ""
    then: str = ""
    inputs: dict[str, str] = Field(default_factory=dict)  # field -> value
    expected: dict[str, str] = Field(default_factory=dict)  # field / file / stdout -> value
    # unit mode: the SUT ends the run (STOP RUN) on this case's path, so the driver cannot
    # print its TEST line; the case is graded by expected_stdout / expected_return_code
    ends_run: bool = False


class GeneratedTest(BaseModel):
    """the single LLM output for a program, normalized."""

    program_id: str
    mode: str  # unit | batch | spec
    cases: list[TestCaseSpec] = Field(default_factory=list)
    driver_name: str = ""  # PROGRAM-ID of the driver (unit mode)
    driver_source: str = ""  # fixed-format COBOL driver (unit mode), already reflowed
    stubs_source: str = ""  # optional stub programs for unresolved callees (unit/batch)
    inputs: list[FixtureFile] = Field(default_factory=list)  # batch mode
    expected_files: list[FixtureFile] = Field(default_factory=list)  # batch mode
    # ordered DISPLAY expectations (unit and batch): a string must appear as a substring,
    # {"line": text} as a whole line (trailing spaces ignored); matched in order, each
    # output occurrence consumed once
    expected_stdout: list[str | dict[str, str]] = Field(default_factory=list)
    expected_return_code: int | None = None  # exit code of the run (unit and batch)
    parm: str | None = None  # batch with Testability.parm_param: the JCL PARM text
    env: dict[str, str] = Field(default_factory=dict)  # extra env vars (DD_xxx mappings etc.)
    stdin: list[str] = Field(default_factory=list)  # lines fed to ACCEPT (batch)
    testability: Testability | None = None  # the classification the test was generated for
    # unit mode: case id -> why its PASS is not a real check (printed unconditionally, or
    # no FAIL path); found by a static check of the driver (codocs.cobol.oracle)
    weak_cases: dict[str, str] = Field(default_factory=dict)
    model: str = ""  # LLM that produced it
    raw_response: str = ""
    error: str = ""  # generation/parse error


class CaseResult(BaseModel):
    id: str
    # pass | fail | error | not-run | weak (a PASS that checks nothing: not counted as passed)
    status: str
    detail: str = ""


class TestResult(BaseModel):
    """outcome of executing one GeneratedTest."""

    __test__ = False  # not a pytest class

    program_id: str
    mode: str
    # passed | failed | weak (every PASS is weak) | compile-error | runtime-error | timeout |
    # not-executable | generation-error | skipped
    status: str
    compiled: bool = False
    dialect: str = ""  # cobc dialect the SUT was built with (ibm | default | mf | custom)
    cases: list[CaseResult] = Field(default_factory=list)
    passed: int = 0
    failed: int = 0
    weak: int = 0  # cases whose PASS checks nothing (see GeneratedTest.weak_cases)
    stubbed_callees: list[str] = Field(default_factory=list)  # repo programs replaced by stubs
    real_callees: list[str] = Field(default_factory=list)  # repo programs linked for real
    skipped_stubs: list[str] = Field(default_factory=list)  # LLM stubs not linked, and why
    paragraphs_total: int = 0
    paragraphs_hit: int = 0  # paragraph coverage from -ftrace
    return_code: int | None = None
    duration_s: float = 0.0
    compile_log: str = ""  # truncated
    run_log: str = ""  # truncated
    # statement / paragraph hits per program file rel, case ids that printed their RUN
    # marker, and the process status of the run
    line_hits: dict[str, dict[int, int]] = Field(default_factory=dict)
    paragraph_hits: dict[str, list[str]] = Field(default_factory=dict)
    # header lines of the paragraphs / sections entered, per file rel (paragraph coverage
    # by line: the same paragraph name may head several SECTIONs or programs of one file)
    paragraph_lines: dict[str, list[int]] = Field(default_factory=dict)
    started: list[str] = Field(default_factory=list)
    crashed: bool = False
    exit_status: int | None = None
