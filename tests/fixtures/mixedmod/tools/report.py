"""summarise ring statistics exported as csv lines (tooling next to the Go code)."""

from __future__ import annotations

import sys


def parse(line: str) -> tuple[str, int]:
    """(name, value) of one `name,value` line."""
    name, _, value = line.partition(",")
    return name.strip(), int(value)


def summarize(lines: list[str]) -> dict[str, int]:
    """total value per name; blank lines are ignored."""
    out: dict[str, int] = {}
    for line in lines:
        if not line.strip():
            continue
        name, value = parse(line)
        out[name] = out.get(name, 0) + value
    return out


def render(totals: dict[str, int]) -> str:
    """one `name: total` line per name, sorted by name."""
    return "\n".join(f"{k}: {v}" for k, v in sorted(totals.items()))


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as fh:
        print(render(summarize(fh.read().splitlines())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
