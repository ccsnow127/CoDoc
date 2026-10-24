package com.example.shapes;

import java.util.List;

/** Helpers over {@link Rect} values. */
public final class Shapes {

    private Shapes() {
    }

    /** Classifies a rectangle by its area: "small" (< 10), "medium" (< 100) or "large". */
    public static String classify(Rect r) {
        int a = r.area();
        if (a < 10) {
            return "small";
        } else if (a < 100) {
            return "medium";
        }
        return "large";
    }

    /** Returns the rectangle with the largest area, or null for an empty list. */
    public static Rect largest(List<Rect> shapes) {
        Rect best = null;
        for (Rect r : shapes) {
            if (best == null || r.area() > best.area()) {
                best = r;
            }
        }
        return best;
    }
}
