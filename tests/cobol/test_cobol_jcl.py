"""tests for codocs.cobol.jcl and codocs.cobol.bms."""

from __future__ import annotations

from codocs.cobol.bms import is_bms_text, parse_bms
from codocs.cobol.jcl import define_statements, is_jcl_text, is_utility, parse_jcl

JOB = """\
//POSTTRAN JOB 'POSTTRAN',CLASS=A,MSGCLASS=0,
// NOTIFY=&SYSUID
//******************************************************************
//* Copyright Amazon.com, Inc. or its affiliates.
//* Licensed under the Apache License, Version 2.0 (the "License").
//******************************************************************
//* Process and load the daily transaction file
//  SET HLQ=AWS.M2
//STEP15 EXEC PGM=CBTRN02C,PARM='2022-01-01'
//STEPLIB  DD DISP=SHR,
//            DSN=&HLQ..LOADLIB
//SYSOUT   DD SYSOUT=*
//TRANFILE DD DISP=SHR,
//         DSN=&HLQ..TRANSACT.VSAM.KSDS
//DALYREJS DD DISP=(NEW,CATLG,DELETE),
//         DSN=AWS.M2.CARDDEMO.DALYREJS(+1)
//CONCAT   DD DSN=A.FIRST,DISP=SHR
//         DD DSN=A.SECOND,DISP=SHR
//SYSIN    DD *
 SORT FIELDS=(1,4,CH,A)
//STEP20 EXEC PGM=IDCAMS
//SYSIN    DD *
  DELETE AWS.M2.X
/*
//DB2STEP EXEC PGM=IKJEFT01
//SYSTSIN  DD *
  DSN SYSTEM(DB2P)
  RUN PROGRAM(COBTUPDT) PLAN(CARDDEMO)
  END
/*
//IMSSTEP EXEC PGM=DFSRRC00,PARM='BMP,PAUDBLOD,PSBNAME'
"""


def test_job_steps_dd_parm_and_description():
    job = parse_jcl("jcl/POSTTRAN.jcl", JOB)
    assert job.name == "POSTTRAN" and job.kind == "job"
    assert job.description == "Process and load the daily transaction file"
    names = [s.name for s in job.steps]
    assert names == ["STEP15", "STEP20", "DB2STEP", "IMSSTEP"]
    s15 = job.steps[0]
    assert s15.program == "CBTRN02C" and s15.app_program == "CBTRN02C"
    assert s15.parm == "2022-01-01"
    assert s15.dd["STEPLIB"] == "AWS.M2.LOADLIB"  # SET symbol substituted
    assert s15.dd["TRANFILE"] == "AWS.M2.TRANSACT.VSAM.KSDS"
    assert s15.dd["DALYREJS"] == "AWS.M2.CARDDEMO.DALYREJS(+1)"
    assert s15.dd["SYSOUT"] == "SYSOUT"
    assert s15.dd["SYSIN"] == "*"
    assert s15.dd["CONCAT"] == "A.FIRST,A.SECOND"
    # utilities are kept as steps but run no application program
    assert job.steps[1].program == "IDCAMS" and job.steps[1].app_program == ""
    assert job.steps[2].app_program == "COBTUPDT"  # IKJEFT01 RUN PROGRAM(...)
    assert job.steps[3].app_program == "PAUDBLOD"  # DFSRRC00 PARM='BMP,PGM,...'


PROC = """\
//REPROC PROC CNTLLIB=DEFAULT.CNTL
//* REPRO UTILITY TO LOAD OR UNLOAD VSAM FILE
//PRC001 EXEC PGM=IDCAMS
//FILEIN  DD DISP=SHR,DSN=NULLFILE
//FILEOUT DD DISP=SHR,DSN=NULLFILE
//SYSIN   DD DISP=SHR,DSN=&CNTLLIB(REPROCT)
//PRC002 EXEC PGM=CBACT01C
//ACCTFILE DD DISP=SHR,DSN=AWS.ACCT
// PEND
"""

CALLER = """\
//UNLOAD JOB CLASS=A
//STEP05R EXEC PROC=REPROC,
// CNTLLIB=AWS.M2.CARDDEMO.CNTL
//PRC001.FILEIN  DD DISP=SHR,
//        DSN=AWS.M2.TRANSACT.VSAM.KSDS
//STEP10 EXEC LOCALP
//LOCALP PROC
//LSTEP EXEC PGM=CBTRN03C
//TRANFILE DD DSN=AWS.TRAN,DISP=SHR
// PEND
"""


def test_catalogued_proc_file_and_expansion():
    proc = parse_jcl("proc/REPROC.prc", PROC)
    assert proc.kind == "proc" and proc.name == "REPROC"
    assert proc.symbols == {"CNTLLIB": "DEFAULT.CNTL"}
    assert proc.description == "REPRO UTILITY TO LOAD OR UNLOAD VSAM FILE"
    assert proc.steps[0].dd["SYSIN"] == "&CNTLLIB(REPROCT)"  # raw inside the proc

    job = parse_jcl("jcl/UNLOAD.jcl", CALLER, {"REPROC": proc})
    names = [(s.name, s.program, s.proc) for s in job.steps]
    assert names[:2] == [("STEP05R.PRC001", "IDCAMS", "REPROC"),
                         ("STEP05R.PRC002", "CBACT01C", "REPROC")]
    first = job.steps[0]
    assert first.dd["FILEIN"] == "AWS.M2.TRANSACT.VSAM.KSDS"  # PRC001.FILEIN override
    assert first.dd["FILEOUT"] == "NULLFILE"
    assert first.dd["SYSIN"] == "AWS.M2.CARDDEMO.CNTL(REPROCT)"  # EXEC symbol override
    assert job.steps[1].app_program == "CBACT01C"


def test_instream_proc_is_expanded_without_catalogue():
    job = parse_jcl("jcl/UNLOAD.jcl", CALLER)
    # REPROC unknown: the step stays as written
    assert job.steps[0].name == "STEP05R" and job.steps[0].proc == "REPROC"
    local = job.steps[-1]
    assert local.name == "STEP10.LSTEP"
    assert local.app_program == "CBTRN03C"
    assert local.dd["TRANFILE"] == "AWS.TRAN"


def test_jcl_helpers():
    assert is_jcl_text(JOB)
    assert not is_jcl_text("       IDENTIFICATION DIVISION.\n")
    assert is_utility("IEBGENER") and is_utility("DFSRRC00") and not is_utility("CBTRN02C")
    job = parse_jcl("x/NOJOB.jcl", "//S1 EXEC PGM=ABC\n")
    assert job.name == "NOJOB" and job.steps[0].app_program == "ABC"


def test_define_statements_csd():
    csd = """\
 DEFINE TRANSACTION(CC00) GROUP(CARDDEMO)
        DESCRIPTION(SIGNON)
        PROGRAM(COSGN00C) TWASIZE(0)
 DEFINE FILE(ACCTDAT) GROUP(CARDDEMO)
        DSNAME(AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS)
"""
    defs = define_statements(csd)
    assert defs[0][0] == "TRANSACTION"
    assert defs[0][1]["TRANSACTION"] == "CC00" and defs[0][1]["PROGRAM"] == "COSGN00C"
    assert defs[1][1]["DSNAME"] == "AWS.M2.CARDDEMO.ACCTDATA.VSAM.KSDS"


BMS = """\
***********************************************************************
* Login screen
***********************************************************************
COSGN00 DFHMSD CTRL=(ALARM,FREEKB),                                    -
               LANG=COBOL,                                             -
               TYPE=&&SYSPARM
COSGN0A DFHMDI COLUMN=1,                                               -
               LINE=1,                                                 -
               SIZE=(24,80)
        DFHMDF ATTRB=(ASKIP,NORM),                                     -
               INITIAL='Tran:'
USERID  DFHMDF ATTRB=(FSET,IC,NORM,UNPROT),                            -
               LENGTH=8
COSGN0B DFHMDI SIZE=(24,80)
        DFHMSD TYPE=FINAL
        END
"""


def test_parse_bms():
    assert is_bms_text(BMS)
    ms = parse_bms("bms/COSGN00.bms", BMS)
    assert ms.name == "COSGN00"
    assert ms.maps == ["COSGN0A", "COSGN0B"]
    assert ms.fields == 2
