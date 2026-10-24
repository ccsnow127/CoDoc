"""data models for COBOL static analysis, shared by every stage.

Leaf node = one COBOL *program* (a compilation unit with a PROGRAM-ID). Copybooks,
JCL jobs and data stores are first-class nodes too, but only programs get
leaf docs and generated tests.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceFile(BaseModel):
    """one physical file of the repository that analysis looked at."""

    path: str  # relative to repo root, forward slashes
    kind: str  # program | copybook | jcl | proc | bms | other
    format: str = "fixed"  # fixed | free (COBOL only)
    encoding: str = "utf-8"
    loc: int = 0  # physical lines


class CallSite(BaseModel):
    """a transfer of control from one program to another."""

    target: str  # upper-cased program name, or the data-name for dynamic calls
    # call | call-dynamic | cics-link | cics-xctl | cics-start | jcl-exec | function
    # (function: a user-defined FUNCTION declared in the REPOSITORY paragraph)
    kind: str
    line: int = 0
    using: list[str] = Field(default_factory=list)  # CALL ... USING args (data names)
    resolved: bool = False  # target is a program defined in this repo
    via: str = ""  # data name the target came through (dynamic CALL / PROGRAM(data-name))
    static: bool = False  # CALL STATIC: resolved by the linker, not at run time
    # set by the index when the target is an ENTRY name: the program id that declares it
    owner: str = ""


class FileUse(BaseModel):
    """a file / dataset the program touches (SELECT ... ASSIGN, or EXEC CICS file ops)."""

    name: str  # logical name: SELECT file-name, or CICS FILE/DATASET name
    assign: str = ""  # ASSIGN TO target (DD name / literal path)
    organization: str = ""  # SEQUENTIAL | LINE SEQUENTIAL | INDEXED | RELATIVE
    access: str = ""  # SEQUENTIAL | RANDOM | DYNAMIC
    record_key: str = ""
    modes: list[str] = Field(default_factory=list)  # INPUT OUTPUT I-O EXTEND / READ WRITE REWRITE DELETE STARTBR ...
    via: str = "native"  # native | cics
    fd_record: str = ""  # first 01 record name under the FD, if any


class SqlUse(BaseModel):
    """an embedded SQL access (EXEC SQL ... END-EXEC)."""

    op: str  # SELECT INSERT UPDATE DELETE DECLARE-CURSOR OPEN FETCH CLOSE INCLUDE ...
    tables: list[str] = Field(default_factory=list)
    line: int = 0
    member: str = ""  # EXEC SQL INCLUDE member (op == "INCLUDE")


class CicsUse(BaseModel):
    """a non-transfer EXEC CICS command (SEND MAP, RECEIVE, RETURN TRANSID, ...)."""

    command: str  # e.g. "SEND MAP", "RETURN", "READ", "WRITEQ TS"
    args: dict[str, str] = Field(default_factory=dict)  # MAP('X') MAPSET('Y') TRANSID('Z') FILE('F')
    line: int = 0
    # literal values of name-like options (MAP MAPSET TRANSID FILE QUEUE ...), data names resolved
    resolved: dict[str, list[str]] = Field(default_factory=dict)


class Paragraph(BaseModel):
    """a paragraph or section in the PROCEDURE DIVISION."""

    name: str
    kind: str = "paragraph"  # paragraph | section
    line: int = 0
    performs: list[str] = Field(default_factory=list)  # PERFORM targets (incl. THRU end)


class DataItem(BaseModel):
    """a level-01/77 item (with a flattened list of its children) in a data section."""

    name: str
    level: int = 1
    section: str = ""  # WORKING-STORAGE | LOCAL-STORAGE | LINKAGE | FILE
    picture: str = ""
    children: list[str] = Field(default_factory=list)  # "05 NAME PIC X(10)" one-liners
    copybook: str = ""  # copybook that defined it, if it came from a COPY


class CobolProgram(BaseModel):
    """one compilation unit (program) — the leaf node of the documentation tree."""

    program_id: str  # upper-cased PROGRAM-ID (FUNCTION-ID for functions)
    path: str  # source file relative to repo root
    unit_kind: str = "program"  # program | function (a FUNCTION-ID unit)
    format: str = "fixed"
    loc: int = 0  # physical lines
    sloc: int = 0  # non-blank, non-comment lines
    author: str = ""
    header_comment: str = ""  # leading comment block / REMARKS, trimmed
    nested_programs: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)  # ENTRY 'X' literals
    using_params: list[str] = Field(default_factory=list)  # PROCEDURE DIVISION USING ...
    returning: str = ""
    copybooks: list[str] = Field(default_factory=list)  # COPY names, upper-cased, in order
    calls: list[CallSite] = Field(default_factory=list)
    files: list[FileUse] = Field(default_factory=list)
    sql: list[SqlUse] = Field(default_factory=list)
    cics: list[CicsUse] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    data_items: list[DataItem] = Field(default_factory=list)  # level 01/77 in all sections
    has_cics: bool = False
    has_sql: bool = False
    has_dli: bool = False  # EXEC DLI / CBLTDLI (IMS)
    is_batch_main: bool = False  # no USING, ends with STOP RUN / GOBACK, run by JCL or standalone
    uses_screen_io: bool = False  # ACCEPT/DISPLAY screen section, BMS maps
    # extended ACCEPT/DISPLAY (AT, LINE/COL, NO-ECHO, TIME-OUT, colors) or screen routines
    # (CBL_GET_SCR_SIZE ...): needs a curses terminal, cannot run headless
    uses_terminal_io: bool = False
    transactions: list[str] = Field(default_factory=list)  # CICS TRANSIDs it RETURNs/STARTs
    maps: list[str] = Field(default_factory=list)  # BMS mapsets/maps it SENDs/RECEIVEs
    # control-flow traps a reader is likely to misread, as human-readable notes with the
    # line number first: NEXT SENTENCE inside scope terminators / inline PERFORM, GO TO out
    # of a PERFORMed range, ALTER, PERFORM THRU spanning paragraphs, main-line fall-through
    flow_hazards: list[str] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)


class Copybook(BaseModel):
    name: str  # upper-cased member name (no extension)
    path: str
    loc: int = 0
    kind: str = "data"  # data | procedure | sql-include | bms-symbolic
    top_items: list[str] = Field(default_factory=list)  # level-01 names it defines
    used_by: list[str] = Field(default_factory=list)  # program ids


class JclStep(BaseModel):
    name: str
    program: str = ""  # EXEC PGM=
    proc: str = ""  # EXEC PROC= / EXEC procname
    dd: dict[str, str] = Field(default_factory=dict)  # DD name -> DSN (or '*' for SYSIN etc.)
    parm: str = ""
    # application program the step really runs: PGM= unless a system utility; the program
    # named in a DFSRRC00 PARM or an IKJEFT01 "RUN PROGRAM(X)" SYSTSIN card
    app_program: str = ""


class JclJob(BaseModel):
    name: str  # JOB card name, else file stem
    path: str
    kind: str = "job"  # job | proc
    description: str = ""  # leading //* comments
    steps: list[JclStep] = Field(default_factory=list)
    symbols: dict[str, str] = Field(default_factory=dict)  # PROC statement symbolic defaults


class BmsMapset(BaseModel):
    name: str
    path: str
    maps: list[str] = Field(default_factory=list)
    fields: int = 0


class DataStore(BaseModel):
    """a logical data store seen across the repo: dataset/DD, CICS file, DB2 table, TS/TD queue."""

    name: str
    kind: str  # dataset | cics-file | db2-table | queue
    readers: list[str] = Field(default_factory=list)  # program ids
    writers: list[str] = Field(default_factory=list)
    jobs: list[str] = Field(default_factory=list)  # JCL jobs that allocate it
    organization: str = ""
    aliases: list[str] = Field(default_factory=list)  # DD / SELECT / CICS FILE names seen for it


class RepoSummary(BaseModel):
    """counts and pointers for logs, reports and prompt facts."""

    name: str
    root: str
    programs: int = 0
    copybooks: int = 0
    jcl_jobs: int = 0
    bms_mapsets: int = 0
    cobol_loc: int = 0
    call_edges: int = 0
    unresolved_calls: list[str] = Field(default_factory=list)
    missing_copybooks: list[str] = Field(default_factory=list)
    unresolved_dynamic: list[str] = Field(default_factory=list)  # "PROG:DATA-NAME" transfers
    warnings: list[str] = Field(default_factory=list)  # duplicate program ids, parse failures
