package com.example.shapes;

/** An axis-aligned rectangle. */
public record Rect(int w, int h) {

    /** Returns w * h. */
    public int area() {
        return w * h;
    }

    /** Returns "square" when both sides are equal, else "rect". */
    public String name() {
        if (w == h) {
            return "square";
        }
        return "rect";
    }
}
