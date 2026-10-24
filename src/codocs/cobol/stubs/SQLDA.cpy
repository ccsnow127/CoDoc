      * SQLDA: SQL descriptor area
      * codocs stub copybook of the EXEC neutraliser (compile gate)
       01  SQLDA.
           05  SQLDAID     PIC X(8).
           05  SQLDABC     PIC S9(9) COMP-5.
           05  SQLN        PIC S9(4) COMP-5.
           05  SQLD        PIC S9(4) COMP-5.
           05  SQLVAR      OCCURS 750 TIMES.
               10  SQLTYPE PIC S9(4) COMP-5.
               10  SQLLEN  PIC S9(4) COMP-5.
               10  SQLDATA USAGE POINTER.
               10  SQLIND  USAGE POINTER.
               10  SQLNAME.
                   15  SQLNAMEL PIC S9(4) COMP-5.
                   15  SQLNAMEC PIC X(30).
