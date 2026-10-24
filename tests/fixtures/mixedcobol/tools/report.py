"""summaries of ACCTRPT report files."""

from __future__ import annotations


def parse_line(line: str) -> tuple[str, str, str]:
    """(account id, open date, status) of one report detail line."""
    return line[0:9].strip(), line[10:18].strip(), line[19:26].strip()


def summarize(lines: list[str]) -> dict[str, int]:
    """counts of VALID and INVALID detail lines; the totals line is ignored."""
    out = {"VALID": 0, "INVALID": 0}
    for line in lines:
        if line.startswith("TOTAL"):
            continue
        status = parse_line(line)[2]
        if status in out:
            out[status] += 1
    return out
