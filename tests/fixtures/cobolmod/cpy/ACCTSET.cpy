      * ACCTSET: symbolic map of mapset ACCTSET (map ACCTMAP)
       01  ACCTMAPI.
           02  FILLER              PIC X(12).
           02  ACCTIDL             PIC S9(4) COMP.
           02  ACCTIDF             PIC X.
           02  FILLER REDEFINES ACCTIDF.
               03  ACCTIDA         PIC X.
           02  ACCTIDI             PIC X(9).
           02  NAMEL               PIC S9(4) COMP.
           02  NAMEF               PIC X.
           02  FILLER REDEFINES NAMEF.
               03  NAMEA           PIC X.
           02  NAMEI               PIC X(20).
           02  MSGL                PIC S9(4) COMP.
           02  MSGF                PIC X.
           02  FILLER REDEFINES MSGF.
               03  MSGA            PIC X.
           02  MSGI                PIC X(40).
       01  ACCTMAPO REDEFINES ACCTMAPI.
           02  FILLER              PIC X(12).
           02  FILLER              PIC X(3).
           02  ACCTIDO             PIC X(9).
           02  FILLER              PIC X(3).
           02  NAMEO               PIC X(20).
           02  FILLER              PIC X(3).
           02  MSGO                PIC X(40).
