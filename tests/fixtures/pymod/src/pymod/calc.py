"""integer helpers: clamping, summing and string normalisation."""

from __future__ import annotations

# SCALE is the fixed-point factor used by Stats.mean.
SCALE = 10


def clamp(v: int, lo: int, hi: int) -> int:
    """limit v to the closed range [lo, hi]."""
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def total(values: list[int]) -> int:
    """sum of values; raises ValueError when there are none."""
    if len(values) == 0:
        raise ValueError("calc: empty input")
    acc = 0
    for v in values:
        acc += v
    return acc


def normalize(s: str) -> str:
    """trim and lower-case s, then replace inner spaces with "-"."""
    s = s.strip().lower()
    return s.replace(" ", "-")
