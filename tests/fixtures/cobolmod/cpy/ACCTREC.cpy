      * ACCTREC: account master record (80 bytes)
       01  ACCT-REC.
           05  ACCT-ID             PIC 9(9).
           05  ACCT-NAME           PIC X(20).
           05  ACCT-OPEN-DATE      PIC X(8).
           05  ACCT-BALANCE        PIC S9(7)V99.
           05  FILLER              PIC X(34).
