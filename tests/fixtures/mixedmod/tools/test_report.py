from report import parse, render, summarize


def test_parse():
    assert parse("a, 3") == ("a", 3)


def test_summarize_and_render():
    totals = summarize(["a,1", "", "b,2", "a,4"])
    assert totals == {"a": 5, "b": 2}
    assert render(totals) == "a: 5\nb: 2"
