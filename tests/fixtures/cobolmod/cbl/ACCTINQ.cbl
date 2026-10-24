       IDENTIFICATION DIVISION.
       PROGRAM-ID. ACCTINQ.
      *----------------------------------------------------------------
      * ACCTINQ: account inquiry (CICS transaction AINQ). Sends map
      * ACCTMAP, reads the account (file ACCTDAT) for the entered id,
      * LINKs DATEUTIL to check its open date and shows the result.
      *----------------------------------------------------------------
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-RESP                 PIC S9(8) COMP.
       01  WS-ACCT-KEY             PIC 9(9).
       01  WS-COMMAREA.
           05  CA-ACCT-ID          PIC 9(9).
           05  CA-MSG              PIC X(40).
       COPY ACCTREC.
       COPY DATEPARM.
       COPY DFHAID.
       COPY ACCTSET.
       LINKAGE SECTION.
       01  DFHCOMMAREA             PIC X(49).
       PROCEDURE DIVISION.
       0000-MAIN.
           IF EIBCALEN = 0
               MOVE LOW-VALUES TO ACCTMAPO
               PERFORM 3000-SEND
           ELSE
               MOVE DFHCOMMAREA TO WS-COMMAREA
               PERFORM 1000-RECEIVE
           END-IF
           EXEC CICS RETURN TRANSID('AINQ')
                COMMAREA(WS-COMMAREA) LENGTH(49)
           END-EXEC.
       1000-RECEIVE.
           EXEC CICS RECEIVE MAP('ACCTMAP') MAPSET('ACCTSET')
                INTO(ACCTMAPI) RESP(WS-RESP)
           END-EXEC
           IF WS-RESP NOT = DFHRESP(NORMAL)
               MOVE 'INVALID INPUT' TO MSGO
               PERFORM 3000-SEND
           ELSE
               MOVE ACCTIDI TO WS-ACCT-KEY
               MOVE WS-ACCT-KEY TO CA-ACCT-ID
               PERFORM 2000-READ-ACCT
           END-IF.
       2000-READ-ACCT.
           EXEC CICS READ FILE('ACCTDAT') INTO(ACCT-REC)
                RIDFLD(WS-ACCT-KEY) RESP(WS-RESP)
           END-EXEC
           EVALUATE WS-RESP
               WHEN DFHRESP(NORMAL)
                   MOVE ACCT-OPEN-DATE TO LK-DATE
                   EXEC CICS LINK PROGRAM('DATEUTIL')
                        COMMAREA(LK-DATE-PARMS)
                   END-EXEC
                   MOVE ACCT-NAME TO NAMEO
                   MOVE 'ACCOUNT FOUND' TO MSGO
               WHEN DFHRESP(NOTFND)
                   MOVE 'ACCOUNT NOT FOUND' TO MSGO
               WHEN OTHER
                   MOVE 'READ ERROR' TO MSGO
           END-EVALUATE
           MOVE MSGO TO CA-MSG
           PERFORM 3000-SEND.
       3000-SEND.
           EXEC CICS SEND MAP('ACCTMAP') MAPSET('ACCTSET')
                FROM(ACCTMAPO) ERASE
           END-EXEC.
