"""simple geometric properties of rectangles."""

from __future__ import annotations


class Rect:
    """an axis-aligned rectangle."""

    def __init__(self, w: float, h: float):
        self.w = w
        self.h = h

    def area(self) -> float:
        """w*h, or 0 when either side is negative."""
        if self.w < 0 or self.h < 0:
            return 0.0
        return self.w * self.h

    def name(self) -> str:
        """"rect WxH" with one decimal."""
        return f"rect {self.w:.1f}x{self.h:.1f}"


def classify(r: Rect) -> str:
    """"empty" for a zero area, "square" when both sides are equal, else "rect"."""
    if r.area() == 0:
        return "empty"
    if r.w == r.h:
        return "square"
    return "rect"


def largest(shapes: list[Rect]) -> Rect | None:
    """the rectangle with the largest area (the first on ties), None for no shapes."""
    best = None
    for s in shapes:
        if best is None or s.area() > best.area():
            best = s
    return best
