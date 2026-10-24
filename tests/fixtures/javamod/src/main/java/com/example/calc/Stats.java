package com.example.calc;

/** Accumulates integer observations. */
public class Stats {

    int count;
    int total;
    int max;

    /** Records one observation. */
    public void add(int v) {
        if (count == 0 || v > max) {
            max = v;
        }
        count++;
        total += v;
    }

    /** Returns total/count in fixed point (multiplied by Calc.SCALE), or 0 when empty. */
    public int mean() {
        if (count == 0) {
            return 0;
        }
        return total * Calc.SCALE / count;
    }

    public int getCount() {
        return count;
    }

    public int getMax() {
        return max;
    }
}
