from pymod.stats import Stats


def test_stats_add():
    s = Stats()
    s.add(3)
    s.add(7)
    s.add(5)
    assert (s.count, s.total, s.max) == (3, 15, 7)


def test_mean():
    assert Stats(count=4, total=10).mean() == 25
    assert Stats().mean() == 0
