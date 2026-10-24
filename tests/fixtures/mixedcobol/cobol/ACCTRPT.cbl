       IDENTIFICATION DIVISION.                                         00000100
       PROGRAM-ID. ACCTRPT.                                             00000200
      *---------------------------------------------------------------- 00000300
      * ACCTRPT: account report. Reads the account file (DD ACCTIN),    00000400
      * validates each open date with DATEUTIL, writes one report line  00000500
      * per account (DD RPTOUT) and the totals.                         00000600
      *---------------------------------------------------------------- 00000700
       ENVIRONMENT DIVISION.                                            00000800
       INPUT-OUTPUT SECTION.                                            00000900
       FILE-CONTROL.                                                    00001000
           SELECT ACCT-FILE ASSIGN TO ACCTIN                            00001100
               ORGANIZATION IS LINE SEQUENTIAL                          00001200
               FILE STATUS IS WS-ACCT-STATUS.                           00001300
           SELECT RPT-FILE ASSIGN TO RPTOUT                             00001400
               ORGANIZATION IS LINE SEQUENTIAL                          00001500
               FILE STATUS IS WS-RPT-STATUS.                            00001600
       DATA DIVISION.                                                   00001700
       FILE SECTION.                                                    00001800
       FD  ACCT-FILE.                                                   00001900
       COPY ACCTREC.                                                    00002000
       FD  RPT-FILE.                                                    00002100
       01  RPT-LINE                PIC X(80).                           00002200
       WORKING-STORAGE SECTION.                                         00002300
       01  WS-ACCT-STATUS          PIC XX.                              00002400
       01  WS-RPT-STATUS           PIC XX.                              00002500
       01  WS-EOF                  PIC X VALUE 'N'.                     00002600
           88  WS-AT-END               VALUE 'Y'.                       00002700
       01  WS-VALID                PIC 9(5) VALUE 0.                    00002800
       01  WS-INVALID              PIC 9(5) VALUE 0.                    00002900
       01  WS-COUNT-TEXT           PIC X(5).                            00003000
       COPY DATEPARM.                                                   00003100
       01  WS-RPT-DETAIL.                                               00003200
           05  RD-ACCT-ID          PIC 9(9).                            00003300
           05  FILLER              PIC X VALUE SPACE.                   00003400
           05  RD-DATE             PIC X(8).                            00003500
           05  FILLER              PIC X VALUE SPACE.                   00003600
           05  RD-STATUS           PIC X(7).                            00003700
       01  WS-RPT-TOTAL.                                                00003800
           05  FILLER              PIC X(14) VALUE 'TOTAL VALID: '.     00003900
           05  RT-VALID            PIC ZZZZ9.                           00004000
           05  FILLER              PIC X(11) VALUE ' INVALID: '.        00004100
           05  RT-INVALID          PIC ZZZZ9.                           00004200
       PROCEDURE DIVISION.                                              00004300
       0000-MAIN.                                                       00004400
           PERFORM 1000-OPEN                                            00004500
           PERFORM 2000-READ                                            00004600
           PERFORM 3000-PROCESS UNTIL WS-AT-END                         00004700
           PERFORM 4000-TOTALS                                          00004800
           PERFORM 9000-CLOSE                                           00004900
           STOP RUN.                                                    00005000
       1000-OPEN.                                                       00005100
           OPEN INPUT ACCT-FILE                                         00005200
           IF WS-ACCT-STATUS NOT = '00'                                 00005300
               DISPLAY 'ACCTIN OPEN FAILED: ' WS-ACCT-STATUS            00005400
               MOVE 12 TO RETURN-CODE                                   00005500
               STOP RUN                                                 00005600
           END-IF                                                       00005700
           OPEN OUTPUT RPT-FILE.                                        00005800
       2000-READ.                                                       00005900
           READ ACCT-FILE                                               00006000
               AT END MOVE 'Y' TO WS-EOF                                00006100
           END-READ.                                                    00006200
       3000-PROCESS.                                                    00006300
           MOVE ACCT-OPEN-DATE TO LK-DATE                               00006400
           CALL 'DATEUTIL' USING LK-DATE-PARMS                          00006500
           MOVE ACCT-ID TO RD-ACCT-ID                                   00006600
           MOVE ACCT-OPEN-DATE TO RD-DATE                               00006700
           IF LK-RC-OK                                                  00006800
               ADD 1 TO WS-VALID                                        00006900
               MOVE 'VALID' TO RD-STATUS                                00007000
           ELSE                                                         00007100
               ADD 1 TO WS-INVALID                                      00007200
               MOVE 'INVALID' TO RD-STATUS                              00007300
           END-IF                                                       00007400
           CALL 'fmtline' USING WS-RPT-DETAIL                           00007500
           WRITE RPT-LINE FROM WS-RPT-DETAIL                            00007600
           PERFORM 2000-READ.                                           00007700
       4000-TOTALS.                                                     00007800
           MOVE WS-VALID TO RT-VALID                                    00007900
           MOVE WS-INVALID TO RT-INVALID                                00008000
           WRITE RPT-LINE FROM WS-RPT-TOTAL                             00008100
           CALL 'NUMPGM' USING WS-VALID WS-COUNT-TEXT                   00008200
           DISPLAY 'VALID COUNT: ' WS-COUNT-TEXT                        00008300
           DISPLAY 'TOTAL VALID: ' WS-VALID ' INVALID: ' WS-INVALID.    00008400
       9000-CLOSE.                                                      00008500
           CLOSE ACCT-FILE RPT-FILE.                                    00008600
