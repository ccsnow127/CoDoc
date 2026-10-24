import pytest

from pymod.calc import clamp, normalize, total


def test_clamp():
    for v, lo, hi, want in [(5, 0, 10, 5), (-1, 0, 10, 0), (11, 0, 10, 10)]:
        assert clamp(v, lo, hi) == want


def test_total():
    assert total([1, 2, 3]) == 6
    with pytest.raises(ValueError, match="calc: empty input"):
        total([])


def test_normalize():
    assert normalize("  Hello World ") == "hello-world"


# test_known_broken fails on the original code; baselines must exclude it.
def test_known_broken():
    assert clamp(1, 2, 3) == 1, "known broken"
