       IDENTIFICATION DIVISION.
       PROGRAM-ID. DATEUTIL.
      *----------------------------------------------------------------
      * DATEUTIL: validates a YYYYMMDD date (month range, day range,
      * leap years). LK-RC: '00' valid, '10' bad month, '20' bad day.
      *----------------------------------------------------------------
       ENVIRONMENT DIVISION.
       CONFIGURATION SECTION.
       REPOSITORY.
           FUNCTION DAYS-IN-MONTH.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-LEAP-FLAG            PIC X VALUE 'N'.
           88  WS-LEAP-YEAR            VALUE 'Y'.
       01  WS-QUOT                 PIC 9(4).
       01  WS-REM                  PIC 9(4).
       01  WS-MAX-DAY              PIC 9(2).
       LINKAGE SECTION.
       COPY DATEPARM.
       PROCEDURE DIVISION USING LK-DATE-PARMS.
       0000-MAIN.
           MOVE '00' TO LK-RC
           PERFORM 1000-CHECK-MONTH
           IF LK-RC-OK
               PERFORM 3000-LEAP
               PERFORM 2000-CHECK-DAY
           END-IF
           GOBACK.
       1000-CHECK-MONTH.
           EVALUATE TRUE
               WHEN LK-MONTH < 1
                   MOVE '10' TO LK-RC
               WHEN LK-MONTH > 12
                   MOVE '10' TO LK-RC
               WHEN OTHER
                   CONTINUE
           END-EVALUATE.
       2000-CHECK-DAY.
           MOVE FUNCTION DAYS-IN-MONTH(LK-MONTH, WS-LEAP-FLAG)
               TO WS-MAX-DAY
           IF LK-DAY < 1 OR LK-DAY > WS-MAX-DAY
               MOVE '20' TO LK-RC
           END-IF.
       3000-LEAP.
           MOVE 'N' TO WS-LEAP-FLAG
           DIVIDE LK-YEAR BY 4 GIVING WS-QUOT REMAINDER WS-REM
           IF WS-REM = 0
               MOVE 'Y' TO WS-LEAP-FLAG
               DIVIDE LK-YEAR BY 100 GIVING WS-QUOT REMAINDER WS-REM
               IF WS-REM = 0
                   MOVE 'N' TO WS-LEAP-FLAG
                   DIVIDE LK-YEAR BY 400 GIVING WS-QUOT
                       REMAINDER WS-REM
                   IF WS-REM = 0
                       MOVE 'Y' TO WS-LEAP-FLAG
                   END-IF
               END-IF
           END-IF.
       9999-EXIT.
           EXIT.
