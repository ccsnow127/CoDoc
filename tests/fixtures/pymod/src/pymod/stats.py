"""running statistics over integer observations."""

from __future__ import annotations

from dataclasses import dataclass

from pymod import calc


@dataclass
class Stats:
    """accumulates integer observations."""

    count: int = 0
    total: int = 0
    max: int = 0

    def add(self, v: int) -> None:
        """record one observation."""
        if self.count == 0 or v > self.max:
            self.max = v
        self.count += 1
        self.total += v

    def mean(self) -> int:
        """total/count in fixed point (multiplied by calc.SCALE), or 0 when empty."""
        if self.count == 0:
            return 0
        return self.total * calc.SCALE // self.count
