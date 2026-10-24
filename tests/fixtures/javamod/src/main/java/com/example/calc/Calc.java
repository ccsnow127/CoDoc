package com.example.calc;

import java.util.Locale;

/** Small integer helpers used by {@link Stats}. */
public final class Calc {

    /** Fixed-point factor used by {@link Stats#mean()}. */
    public static final int SCALE = 10;

    private Calc() {
    }

    /** Limits v to the closed range [lo, hi]. */
    public static int clamp(int v, int lo, int hi) {
        if (v < lo) {
            return lo;
        }
        if (v > hi) {
            return hi;
        }
        return v;
    }

    /** Adds all values; an empty array is rejected. */
    public static int sum(int[] values) {
        if (values == null || values.length == 0) {
            throw new IllegalArgumentException("calc: empty input");
        }
        int total = 0;
        for (int v : values) {
            total += v;
        }
        return total;
    }

    /** Trims and lower-cases s, then replaces inner spaces with "-". */
    public static String normalize(String s) {
        String t = s.trim().toLowerCase(Locale.ROOT);
        return t.replace(" ", "-");
    }
}
