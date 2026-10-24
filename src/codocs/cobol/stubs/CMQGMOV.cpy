      * CMQGMOV: MQGMO get-message options
      * codocs stub copybook of the EXEC neutraliser (compile gate)
           10  MQGMO-STRUCID              PIC X(4) VALUE 'GMO '.
           10  MQGMO-VERSION              PIC S9(9) BINARY VALUE 1.
           10  MQGMO-OPTIONS              PIC S9(9) BINARY.
           10  MQGMO-WAITINTERVAL         PIC S9(9) BINARY.
           10  MQGMO-SIGNAL1              PIC S9(9) BINARY.
           10  MQGMO-SIGNAL2              PIC S9(9) BINARY.
           10  MQGMO-RESOLVEDQNAME        PIC X(48).
           10  MQGMO-MATCHOPTIONS         PIC S9(9) BINARY.
           10  MQGMO-GROUPSTATUS          PIC X.
           10  MQGMO-SEGMENTSTATUS        PIC X.
           10  MQGMO-SEGMENTATION         PIC X.
           10  MQGMO-RESERVED1            PIC X.
           10  MQGMO-MSGTOKEN             PIC X(16).
           10  MQGMO-RETURNEDLENGTH       PIC S9(9) BINARY.
