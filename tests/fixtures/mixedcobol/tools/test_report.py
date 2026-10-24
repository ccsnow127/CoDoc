from report import parse_line, summarize


def test_parse_line():
    assert parse_line("000000001 20240229 VALID  ") == ("000000001", "20240229", "VALID")


def test_summarize_skips_totals():
    lines = ["000000001 20240229 VALID  ", "000000002 20231301 INVALID",
             "TOTAL VALID:     1 INVALID:     1"]
    assert summarize(lines) == {"VALID": 1, "INVALID": 1}
