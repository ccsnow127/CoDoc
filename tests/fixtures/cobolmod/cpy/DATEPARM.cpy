      * DATEPARM: parameter block of DATEUTIL (date in, return code out)
       01  LK-DATE-PARMS.
           05  LK-DATE.
               10  LK-YEAR         PIC 9(4).
               10  LK-MONTH        PIC 9(2).
               10  LK-DAY          PIC 9(2).
           05  LK-RC               PIC X(2).
               88  LK-RC-OK            VALUE '00'.
               88  LK-RC-BAD-MONTH     VALUE '10'.
               88  LK-RC-BAD-DAY       VALUE '20'.
