      * CMQV: IBM MQ constants (a commonly used subset)
      * codocs stub copybook of the EXEC neutraliser (compile gate)
           10  MQCC-OK                    PIC S9(9) BINARY VALUE 0.
           10  MQCC-WARNING               PIC S9(9) BINARY VALUE 1.
           10  MQCC-FAILED                PIC S9(9) BINARY VALUE 2.
           10  MQCC-UNKNOWN               PIC S9(9) BINARY VALUE -1.
           10  MQCCSI-Q-MGR               PIC S9(9) BINARY VALUE 0.
           10  MQCO-NONE                  PIC S9(9) BINARY VALUE 0.
           10  MQGMO-WAIT                 PIC S9(9) BINARY VALUE 1.
           10  MQGMO-NO-WAIT              PIC S9(9) BINARY VALUE 0.
           10  MQGMO-SYNCPOINT            PIC S9(9) BINARY VALUE 2.
           10  MQGMO-NO-SYNCPOINT         PIC S9(9) BINARY VALUE 4.
           10  MQGMO-FAIL-IF-QUIESCING    PIC S9(9) BINARY VALUE 8192.
           10  MQGMO-CONVERT              PIC S9(9) BINARY VALUE 16384.
           10  MQMT-REQUEST               PIC S9(9) BINARY VALUE 1.
           10  MQMT-REPLY                 PIC S9(9) BINARY VALUE 2.
           10  MQMT-DATAGRAM              PIC S9(9) BINARY VALUE 8.
           10  MQOO-INPUT-AS-Q-DEF        PIC S9(9) BINARY VALUE 1.
           10  MQOO-INPUT-SHARED          PIC S9(9) BINARY VALUE 2.
           10  MQOO-INPUT-EXCLUSIVE       PIC S9(9) BINARY VALUE 4.
           10  MQOO-BROWSE                PIC S9(9) BINARY VALUE 8.
           10  MQOO-OUTPUT                PIC S9(9) BINARY VALUE 16.
           10  MQOO-INQUIRE               PIC S9(9) BINARY VALUE 32.
           10  MQOO-SAVE-ALL-CONTEXT      PIC S9(9) BINARY VALUE 128.
           10  MQOO-PASS-ALL-CONTEXT      PIC S9(9) BINARY VALUE 256.
           10  MQOO-FAIL-IF-QUIESCING     PIC S9(9) BINARY VALUE 8192.
           10  MQOT-Q                     PIC S9(9) BINARY VALUE 1.
           10  MQOT-Q-MGR                 PIC S9(9) BINARY VALUE 5.
           10  MQPER-NOT-PERSISTENT       PIC S9(9) BINARY VALUE 0.
           10  MQPER-PERSISTENT           PIC S9(9) BINARY VALUE 1.
           10  MQPER-PERSISTENCE-AS-Q-DEF PIC S9(9) BINARY VALUE 2.
           10  MQPMO-SYNCPOINT            PIC S9(9) BINARY VALUE 2.
           10  MQPMO-NO-SYNCPOINT         PIC S9(9) BINARY VALUE 4.
           10  MQPMO-DEFAULT-CONTEXT      PIC S9(9) BINARY VALUE 32.
           10  MQPMO-PASS-ALL-CONTEXT     PIC S9(9) BINARY VALUE 256.
           10  MQPMO-FAIL-IF-QUIESCING    PIC S9(9) BINARY VALUE 8192.
           10  MQRC-NONE                  PIC S9(9) BINARY VALUE 0.
           10  MQRC-NO-MSG-AVAILABLE      PIC S9(9) BINARY VALUE 2033.
           10  MQRC-TRUNCATED-MSG-FAILED  PIC S9(9) BINARY VALUE 2080.
           10  MQEI-UNLIMITED             PIC S9(9) BINARY VALUE -1.
           10  MQMI-NONE                  PIC X(24) VALUE LOW-VALUES.
           10  MQCI-NONE                  PIC X(24) VALUE LOW-VALUES.
           10  MQFMT-NONE                 PIC X(8) VALUE SPACES.
           10  MQFMT-STRING               PIC X(8) VALUE 'MQSTR'.
