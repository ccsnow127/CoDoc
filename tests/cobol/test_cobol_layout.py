"""tests for codocs.cobol.layout: record layouts, group usage and the source display."""

from __future__ import annotations

from codocs.cobol.layout import (
    display_source,
    driver_hint,
    exact_program_name,
    item_size,
    pic_info,
    record_fields,
    record_layout,
)
from codocs.cobol.models import DataItem


def test_pic_info_and_sizes():
    assert pic_info("X(10)") == (10, 0)
    assert pic_info("S9(5)V99") == (7, 7)
    assert pic_info("9(3)V9(2)") == (5, 5)
    assert pic_info("-ZZ9.99") == (7, 3)
    assert pic_info("99CR") == (4, 2)
    assert item_size("S9(4)", "COMP") == 2
    assert item_size("S9(9)", "COMP") == 4
    assert item_size("S9(18)", "BINARY") == 8
    assert item_size("9(2)", "COMP", dialect="default") == 1
    assert item_size("9(2)", "COMP", dialect="ibm") == 2
    assert item_size("S9(10)V99", "COMP-3") == 7
    assert item_size("9(3)", "PACKED-DECIMAL") == 2
    assert item_size("", "COMP-2") == 8


def test_record_layout_with_occurs_and_redefines():
    item = DataItem(name="REC", section="FILE", children=[
        "05 R-ID PIC 9(11)",
        "05 R-BAL OCCURS 5",
        "10 R-AMT PIC S9(10)V99",
        "10 R-DEB PIC S9(10)V99 COMP-3",
        "05 R-DATE PIC X(10)",
        "05 R-DATE-P REDEFINES R-DATE",
        "10 R-YYYY PIC X(4)",
        "10 R-REST PIC X(6)",
        "05 FILLER PIC X(4)",
        "88 NOT-A-FIELD VALUE 'Y'",
    ])
    size, fields, complete = record_fields(item)
    assert complete
    assert size == 11 + 5 * (12 + 7) + 10 + 4
    by = {f.name: f for f in fields}
    assert by["R-BAL"].offset == 11 and by["R-BAL"].size == 19 and by["R-BAL"].occurs == 5
    assert by["R-DEB"].offset == 23 and by["R-DEB"].dims == ((5, 19),)
    assert by["R-DATE"].offset == 106
    assert by["R-DATE-P"].offset == 106 and by["R-YYYY"].shadow
    assert by["R-DEB"].usage == "COMP-3"
    _, rows, _ = record_layout(item)
    assert rows[0].split()[:2] == ["1", "11"]
    assert any("OCCURS 5 (each 19 bytes)" in r for r in rows)


def test_group_usage_is_inherited():
    item = DataItem(name="REC", children=["05 G COMP-3", "10 A PIC S9(5)", "10 B PIC 9(3)"])
    size, fields, _ = record_fields(item)
    assert size == 3 + 2
    assert [f.usage for f in fields] == ["COMP-3", "COMP-3", "COMP-3"]


def test_display_source_strips_sequence_area():
    text = "000100 IDENTIFICATION DIVISION.                                         SEQ00001\n"
    assert display_source(text, "fixed") == " IDENTIFICATION DIVISION."
    assert display_source("program-id. x.\n", "free") == "program-id. x."


def test_exact_program_name_and_driver_hint():
    src = "       program-id. createAuthCode.\n"
    assert exact_program_name(src, "CREATEAUTHCODE") == "createAuthCode"
    assert exact_program_name("", "X") == "X"
    assert driver_hint("CSUTLDTC") == "TCSUTLDT"
    assert driver_hint("print-text") == "TPRINTTE"
