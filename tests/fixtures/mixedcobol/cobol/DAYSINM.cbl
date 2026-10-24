       IDENTIFICATION DIVISION.
       FUNCTION-ID. DAYS-IN-MONTH.
      *----------------------------------------------------------------
      * DAYS-IN-MONTH: user-defined function, the number of days of a
      * month (01-12) given a leap-year flag ('Y' = leap year).
      *----------------------------------------------------------------
       DATA DIVISION.
       LINKAGE SECTION.
       01  LK-MON                  PIC 9(2).
       01  LK-LEAP                 PIC X.
       01  LK-DAYS                 PIC 9(2).
       PROCEDURE DIVISION USING LK-MON LK-LEAP RETURNING LK-DAYS.
       0000-MAIN.
           EVALUATE LK-MON
               WHEN 2
                   PERFORM 1000-FEB
               WHEN 4
               WHEN 6
               WHEN 9
               WHEN 11
                   MOVE 30 TO LK-DAYS
               WHEN OTHER
                   MOVE 31 TO LK-DAYS
           END-EVALUATE
           GOBACK.
       1000-FEB.
           IF LK-LEAP = 'Y'
               MOVE 29 TO LK-DAYS
           ELSE
               MOVE 28 TO LK-DAYS
           END-IF.
       END FUNCTION DAYS-IN-MONTH.
