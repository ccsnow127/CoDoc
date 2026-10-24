"""deterministic identifier check: names a leaf doc mentions that exist nowhere.

Every identifier-like token inside an inline `code` span of the doc (outside fenced
blocks) must occur in the program's source, its copybooks, or the repo index (programs,
ENTRY names, copybooks, JCL jobs/steps/DDs, data stores, transactions, maps, tables).
Anything else is a candidate fabrication. No LLM is involved, so this layer also serves
to calibrate the judge: a fabricated name the judge does not flag is a judge miss.
"""

from __future__ import annotations

import re

from codocs.cobol.index import CobolRepoIndex
from codocs.cobol.judge.models import NameCheck

_FENCE_RE = re.compile(r"```.*?```", re.S)
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_TOKEN_RE = re.compile(r"[A-Za-z0-9$#@_][A-Za-z0-9$#@_-]*(?:\.[A-Za-z0-9$#@_-]+)*")
_WORD_RE = re.compile(r"[A-Za-z0-9$#@_-]+")
# system names that live in IBM / GnuCOBOL copybooks or registers, not in the repo
_SYSTEM_PREFIXES = ("DFH", "EIB", "SQL", "CEE", "CBL_", "END-", "COMP-", "BINARY-", "FUNCTION-")
_COBOL_WORDS = frozenset("""
    RETURN-CODE WORKING-STORAGE LOCAL-STORAGE LINKAGE-SECTION FILE-CONTROL I-O I-O-CONTROL
    PROGRAM-ID FUNCTION-ID DATE-WRITTEN DATE-COMPILED SOURCE-COMPUTER OBJECT-COMPUTER
    SPECIAL-NAMES INPUT-OUTPUT PACKED-DECIMAL NEXT-SENTENCE GO-TO STOP-RUN CURRENT-DATE
    WHEN-COMPILED NUMVAL NUMVAL-C UPPER-CASE LOWER-CASE INTEGER-OF-DATE DATE-OF-INTEGER
    LINE-SEQUENTIAL RECORD-KEY FILE-STATUS DFHCOMMAREA ADDRESS-OF LENGTH-OF
    PERFORM-THRU PERFORM-UNTIL EXEC-CICS EXEC-SQL END-EXEC GOBACK CONTINUE EVALUATE
    COMMAREA KSDS ESDS RRDS VSAM JCL CICS BMS DB2 IMS CALL LINK XCTL STEP
    HANDLE CONDITION ABEND ABCODE RESP RESP2 SYNCPOINT ROLLBACK ENTER CLEAR INVALID
    EXCEPTION STATUS ERROR AFTER BEFORE FILE NOTFND DUPREC DUPKEY ENDFILE MAPFAIL LENGERR
    NORMAL QIDERR PGMIDERR ITEMERR NOTOPEN DISABLED CURSOR ERASE ERASEAUP FREEKB MAPONLY
    DATAONLY COMMIT FETCH CURSOR DECLARE SELECT INSERT UPDATE DELETE OPEN CLOSE READ WRITE
    REWRITE START READNEXT READPREV STARTBR ENDBR RECEIVE SEND RETURN TRANSID PROGRAM
    MAPSET RIDFLD KEYLENGTH GENERIC GTEQ EQUAL UPDATE INTO FROM ASKTIME FORMATTIME
    ENQ DEQ WRITEQ READQ DELETEQ RETRIEVE ASSIGN INQUIRE BACKSPACE DISPLAY ACCEPT MOVE
    PERFORM EVALUATE INITIALIZE INSPECT STRING UNSTRING COMPUTE SEARCH SORT MERGE CANCEL
    INDEXED SEQUENTIAL RELATIVE DYNAMIC RANDOM OUTPUT INPUT EXTEND SPACES ZEROES ZEROS
    LOW-VALUES HIGH-VALUES LOW-VALUE HIGH-VALUE QUOTES NULLS TRUE FALSE OTHER WHEN
""".split())
# edit-mask / date-format pictures: YYYY-MM-DD, XXX-XX-XXXX, HH.MM.SS, 9(5)
_MASK_RE = re.compile(r"[XYMDHSAZN0-9]+(?:[.:/-][XYMDHSAZN0-9]+)*")


def _doc_tokens(markdown: str) -> list[str]:
    """candidate identifiers named in inline code spans, upper-cased, in order."""
    text = _FENCE_RE.sub(" ", markdown)
    out: list[str] = []
    for span in _SPAN_RE.findall(text):
        if span.startswith(("'", '"')):  # a literal value, not a name
            continue
        span = re.sub(r"'[^']*'|\"[^\"]*\"", " ", span)  # literals inside expressions
        span = re.sub(r"<[^<>]*>", " ", span)  # placeholders: <start-value>
        for m in _TOKEN_RE.finditer(span):
            tok = m.group(0).strip(".-")
            if span[m.end():m.end() + 2] in ("-*", "*") or span[m.end():m.end() + 1] == "*":
                out.append(tok.upper() + "-*")  # a prefix pattern such as CDEMO-FROM-*
                continue
            if not tok or not re.search(r"[A-Za-z]", tok):
                continue
            hyphenated = "-" in tok and re.match(r"[A-Za-z]", tok)
            dotted = "." in tok and tok.upper() == tok
            upper_word = tok.upper() == tok and len(tok) >= 4 and re.search(r"[A-Z]", tok)
            if _MASK_RE.fullmatch(tok.upper()) and tok.upper() == tok:
                continue
            if hyphenated or dotted or upper_word:
                out.append(tok.upper())
    return list(dict.fromkeys(out))


def known_names(index: CobolRepoIndex, pid: str, texts: list[str]) -> set[str]:
    """every word of the given source texts plus every name the index knows."""
    known: set[str] = set()
    for t in texts:
        for w in _WORD_RE.findall(t):
            known.add(w.upper().strip("-"))
        for w in _TOKEN_RE.findall(t):  # dotted names (DSNs in comments / literals)
            known.add(w.upper().strip(".-"))
    known.update(index.programs)
    for prog in index.programs.values():
        known.update(e.upper() for e in prog.entry_points)
    known.update(index.copybooks)
    known.update(getattr(index, "transactions", {}) or {})
    for job in index.jobs.values():
        known.add(job.name.upper())
        for st in job.steps:
            known.add(st.name.upper())
            known.update(st.name.upper().split("."))  # expanded PROC steps: STEP.PROCSTEP
            known.update(d.upper() for d in st.dd)
            known.update(v.upper() for v in st.dd.values())
    for ms in index.mapsets.values():
        known.add(ms.name.upper())
        known.update(m.upper() for m in ms.maps)
        try:  # BMS field names: the symbolic map adds I/O/L/A/F suffixes to them
            known.update(w.upper() for w in _WORD_RE.findall((index.root / ms.path).read_text(
                errors="replace")))
        except OSError:
            pass
    for ds in index.datastores.values():
        known.add(ds.name.upper())
        known.update(a.upper() for a in ds.aliases)
    return known


def check_names(index: CobolRepoIndex, pid: str, markdown: str, texts: list[str]) -> NameCheck:
    """NameCheck for *markdown* against the program texts (source, copybooks) and index."""
    known = known_names(index, pid, texts)
    hyphen_prefixes = {k.rsplit("-", n)[0] for k in known if "-" in k
                       for n in range(1, k.count("-") + 1)}
    total = known_n = 0
    unknown: list[str] = []
    for tok in _doc_tokens(markdown):
        if tok in _COBOL_WORDS or tok.startswith(_SYSTEM_PREFIXES):
            continue
        total += 1
        base = re.sub(r"\(\+?-?\d+\)$", "", tok)  # GDG generation suffix
        if base.endswith("-*"):  # prefix pattern: some known name must start with it
            ok = base[:-2] in hyphen_prefixes
        else:
            ok = (base in known or all(p in known for p in base.split("."))
                  or base in hyphen_prefixes  # CDEMO-FROM in "CDEMO-FROM-PROGRAM / -TRANID"
                  or (len(base) > 3 and base[-1] in "IOLAF" and base[:-1] in known))
        if ok:
            known_n += 1
        else:
            unknown.append(tok)
    return NameCheck(total=total, known=known_n, unknown=unknown)
