       IDENTIFICATION DIVISION.
       PROGRAM-ID. TDATEUTL.
      *----------------------------------------------------------------
      * TDATEUTL: driver-style tests of DATEUTIL. T99 fails on purpose
      * (it expects a return code DATEUTIL never sets).
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY DATEPARM.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM TC-T01
           PERFORM TC-T02
           PERFORM TC-T03
           PERFORM TC-T99
           STOP RUN.
       TC-T01.
           MOVE '20240229' TO LK-DATE
           CALL 'DATEUTIL' USING LK-DATE-PARMS
           IF LK-RC = '00'
               DISPLAY 'TEST T01 PASS'
           ELSE
               DISPLAY 'TEST T01 FAIL expected 00 got ' LK-RC
           END-IF.
       TC-T02.
           MOVE '20241301' TO LK-DATE
           CALL 'DATEUTIL' USING LK-DATE-PARMS
           IF LK-RC = '10'
               DISPLAY 'TEST T02 PASS'
           ELSE
               DISPLAY 'TEST T02 FAIL expected 10 got ' LK-RC
           END-IF.
       TC-T03.
           MOVE '20230229' TO LK-DATE
           CALL 'DATEUTIL' USING LK-DATE-PARMS
           IF LK-RC = '20'
               DISPLAY 'TEST T03 PASS'
           ELSE
               DISPLAY 'TEST T03 FAIL expected 20 got ' LK-RC
           END-IF.
       TC-T99.
           MOVE '20240115' TO LK-DATE
           CALL 'DATEUTIL' USING LK-DATE-PARMS
           IF LK-RC = '99'
               DISPLAY 'TEST T99 PASS'
           ELSE
               DISPLAY 'TEST T99 FAIL expected 99 got ' LK-RC
           END-IF.
