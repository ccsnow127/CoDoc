      * CMQMDV: MQMD message descriptor
      * codocs stub copybook of the EXEC neutraliser (compile gate)
           10  MQMD-STRUCID              PIC X(4) VALUE 'MD  '.
           10  MQMD-VERSION              PIC S9(9) BINARY VALUE 1.
           10  MQMD-REPORT               PIC S9(9) BINARY.
           10  MQMD-MSGTYPE              PIC S9(9) BINARY.
           10  MQMD-EXPIRY               PIC S9(9) BINARY.
           10  MQMD-FEEDBACK             PIC S9(9) BINARY.
           10  MQMD-ENCODING             PIC S9(9) BINARY.
           10  MQMD-CODEDCHARSETID       PIC S9(9) BINARY.
           10  MQMD-FORMAT               PIC X(8).
           10  MQMD-PRIORITY             PIC S9(9) BINARY.
           10  MQMD-PERSISTENCE          PIC S9(9) BINARY.
           10  MQMD-MSGID                PIC X(24).
           10  MQMD-CORRELID             PIC X(24).
           10  MQMD-BACKOUTCOUNT         PIC S9(9) BINARY.
           10  MQMD-REPLYTOQ             PIC X(48).
           10  MQMD-REPLYTOQMGR          PIC X(48).
           10  MQMD-USERIDENTIFIER       PIC X(12).
           10  MQMD-ACCOUNTINGTOKEN      PIC X(32).
           10  MQMD-APPLIDENTITYDATA     PIC X(32).
           10  MQMD-PUTAPPLTYPE          PIC S9(9) BINARY.
           10  MQMD-PUTAPPLNAME          PIC X(28).
           10  MQMD-PUTDATE              PIC X(8).
           10  MQMD-PUTTIME              PIC X(8).
           10  MQMD-APPLORIGINDATA       PIC X(4).
