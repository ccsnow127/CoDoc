       IDENTIFICATION DIVISION.
       PROGRAM-ID. NUMPGM.
      *----------------------------------------------------------------
      * NUMPGM: callable subprogram, formats a count as edited text.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-EDIT                 PIC ZZZZ9.
       LINKAGE SECTION.
       01  LK-COUNT                PIC 9(5).
       01  LK-TEXT                 PIC X(5).
       PROCEDURE DIVISION USING LK-COUNT LK-TEXT.
       0000-MAIN.
           MOVE LK-COUNT TO WS-EDIT
           MOVE WS-EDIT TO LK-TEXT
           GOBACK.
