"""tests for CobolProgram.flow_hazards and the screen / function flags of the parser."""

from __future__ import annotations

from codocs.cobol.parser import parse_program_file


def fx(*lines: str) -> str:
    """fixed format: code at col 8 ('*' first char -> comment indicator in col 7)."""
    return "\n".join(("      " if ln.startswith("*") else "       ") + ln for ln in lines) + "\n"


def parse_one(text: str):
    progs = parse_program_file("src/prog.cbl", text)
    assert len(progs) == 1
    return progs[0]


HEAD = (
    "IDENTIFICATION DIVISION.",
    "PROGRAM-ID. FLOWPGM.",
    "DATA DIVISION.",
    "WORKING-STORAGE SECTION.",
    "01 WS-EOF PIC X VALUE 'N'.",
    "01 WS-DATE PIC X(10).",
    "PROCEDURE DIVISION.",
)


def test_next_sentence_in_inline_perform_leaves_the_loop():
    # the CBTRN03C pattern: NEXT SENTENCE inside IF ... END-IF inside PERFORM ... END-PERFORM
    prog = parse_one(fx(
        *HEAD,                                        # lines 1-7
        "    PERFORM UNTIL WS-EOF = 'Y'",             # 8
        "      IF WS-EOF = 'N'",                      # 9
        "         PERFORM 1000-READ",                 # 10
        "         IF WS-DATE >= '2024-01-01'",        # 11
        "            CONTINUE",                       # 12
        "         ELSE",                              # 13
        "            NEXT SENTENCE",                  # 14
        "         END-IF",                            # 15
        "         DISPLAY WS-DATE",                   # 16
        "      END-IF",                               # 17
        "    END-PERFORM.",                           # 18
        "    GOBACK.",                                # 19
        "1000-READ.",                                 # 20
        "    MOVE 'Y' TO WS-EOF.",                    # 21
    ))
    hz = [h for h in prog.flow_hazards if "NEXT SENTENCE" in h]
    assert len(hz) == 1
    h = hz[0]
    assert h.startswith("line 14: NEXT SENTENCE inside IF (line 11) ... END-IF (line 15)")
    assert "within inline PERFORM (lines 8-18)" in h
    assert "leaves the PERFORM loop" in h and "not CONTINUE" in h


def test_next_sentence_classic_uses_are_not_hazards():
    prog = parse_one(fx(
        *HEAD,
        "    IF WS-EOF = 'Y' NEXT SENTENCE ELSE DISPLAY 'MORE'.",  # implicit scope
        "    IF WS-EOF = 'Y'",
        "       NEXT SENTENCE",
        "    END-IF.",                                # END-IF directly before the period
        "    PERFORM 1000-READ 3 TIMES.",            # out-of-line PERFORM: no END-PERFORM
        "    GOBACK.",
        "1000-READ.",
        "    MOVE 'Y' TO WS-EOF.",
    ))
    assert prog.flow_hazards == []


def test_next_sentence_skipping_statements_after_end_if():
    prog = parse_one(fx(
        *HEAD,
        "    IF WS-EOF = 'Y'",                        # 8
        "       NEXT SENTENCE",                       # 9
        "    END-IF",                                 # 10
        "    DISPLAY 'SKIPPED WHEN EOF'.",            # 11
        "    GOBACK.",
    ))
    assert len(prog.flow_hazards) == 1
    h = prog.flow_hazards[0]
    assert h.startswith("line 9: NEXT SENTENCE inside IF (line 8) ... END-IF (line 10)")
    assert "skipping the statements after END-IF (line 10)" in h
    assert "PERFORM loop" not in h


def test_go_to_out_of_performed_range_alter_and_thru_span():
    prog = parse_one(fx(
        *HEAD,
        "0000-MAIN.",                                 # 8
        "    PERFORM 1000-A THRU 1000-A-EXIT",        # 9
        "    PERFORM 2000-B THRU 2000-B-EXIT",        # 10
        "    ALTER 3000-SWITCH TO PROCEED TO 9999-END",  # 11
        "    GO TO 3000-SWITCH.",                     # 12
        "1000-A.",                                    # 13
        "    IF WS-EOF = 'Y' GO TO 1000-A-EXIT END-IF",  # 14: inside the range, fine
        "    IF WS-EOF = 'X' GO TO 9999-END END-IF.",  # 15: leaves the range
        "1000-A-EXIT.",
        "    EXIT.",
        "2000-B.",
        "    MOVE 'Y' TO WS-EOF.",
        "2000-B-MIDDLE.",
        "    MOVE 'N' TO WS-EOF.",
        "2000-B-EXIT.",
        "    EXIT.",
        "3000-SWITCH.",
        "    GO TO 0000-MAIN.",
        "9999-END.",
        "    STOP RUN.",
    ))
    hz = prog.flow_hazards
    goto = [h for h in hz if "GO TO" in h and "leaves" in h]
    assert goto == [
        "line 15: GO TO 9999-END in 1000-A leaves the PERFORMed range 1000-A THRU 1000-A-EXIT"
        " — control does not come back to the PERFORM statement"
    ]
    alter = [h for h in hz if "ALTER" in h]
    assert len(alter) == 1 and alter[0].startswith(
        "line 11: ALTER 3000-SWITCH TO PROCEED TO 9999-END")
    thru = [h for h in hz if "THRU" in h and "also runs" in h]
    assert len(thru) == 1 and "2000-B-MIDDLE" in thru[0] and thru[0].startswith("line 10:")


def test_main_line_fall_through():
    prog = parse_one(fx(
        *HEAD,
        "0001-OPEN.",                                 # 8
        "    DISPLAY 'OPEN'.",                        # 9
        "1001-PROCESS.",                              # 10
        "    PERFORM 2000-WORK",
        "    STOP RUN.",
        "2000-WORK.",
        "    DISPLAY 'WORK'.",
    ))
    assert prog.flow_hazards == [
        "line 9: the main line (0001-OPEN) does not end with STOP RUN, GOBACK, GO TO or a "
        "CICS RETURN — control falls through into 1001-PROCESS (line 10)"
    ]
    # a main line ending in PERFORM of a terminating paragraph is fine
    ok = parse_one(fx(
        *HEAD,
        "0001-MAIN.",
        "    PERFORM 9000-END.",
        "1000-SUB.",
        "    DISPLAY 'SUB'.",
        "9000-END.",
        "    GOBACK.",
    ))
    assert ok.flow_hazards == []


def test_exec_blocks_are_ignored_and_cics_return_ends_the_main_line():
    prog = parse_one(fx(
        *HEAD,
        "MAINLINE SECTION.",
        "    IF WS-EOF = 'Y'",
        "       EXEC CICS RETURN END-EXEC",
        "    END-IF",
        "    EXEC CICS RETURN END-EXEC.",
        "MAINLINE-EXIT.",
        "    EXIT.",
        "OTHER-STUFF SECTION.",
        "    DISPLAY 'X'.",
    ))
    assert prog.flow_hazards == []


def test_screen_io_for_positioned_display_and_functions_are_not_batch_mains():
    prog = parse_one(fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. PRINT-TEXT.",
        "PROCEDURE DIVISION.",
        "    DISPLAY 'HELLO' AT LINE 3 COLUMN 5 FOREGROUND-COLOR 2",
        "    GOBACK.",
    ))
    assert prog.uses_screen_io and prog.uses_terminal_io
    plain = parse_one(fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. PLAIN.",
        "PROCEDURE DIVISION.",
        "    DISPLAY 'HELLO' UPON CONSOLE",
        "    ACCEPT WS-X FROM DATE",
        "    GOBACK.",
    ))
    assert not plain.uses_screen_io and not plain.uses_terminal_io
    noecho = parse_one(fx(
        "IDENTIFICATION DIVISION.",
        "PROGRAM-ID. NOECHO.",
        "PROCEDURE DIVISION.",
        "    ACCEPT WS-X WITH NO-ECHO",
        "    GOBACK.",
    ))
    assert noecho.uses_terminal_io and not noecho.uses_screen_io
    fn = parse_one(fx(
        "IDENTIFICATION DIVISION.",
        "FUNCTION-ID. LEFT-FUNC.",
        "DATA DIVISION.",
        "LINKAGE SECTION.",
        "01 L-RESULT PIC X(10).",
        "PROCEDURE DIVISION RETURNING L-RESULT.",
        "    MOVE 'X' TO L-RESULT",
        "    GOBACK.",
        "END FUNCTION LEFT-FUNC.",
    ))
    assert fn.unit_kind == "function" and not fn.is_batch_main
