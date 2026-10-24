package com.example.calc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class CalcTest {

    @Test
    void clamp() {
        assertEquals(5, Calc.clamp(5, 0, 10));
        assertEquals(0, Calc.clamp(-1, 0, 10));
        assertEquals(10, Calc.clamp(11, 0, 10));
    }

    @Test
    void sum() {
        assertEquals(6, Calc.sum(new int[] {1, 2, 3}));
        IllegalArgumentException e =
                assertThrows(IllegalArgumentException.class, () -> Calc.sum(new int[0]));
        assertEquals("calc: empty input", e.getMessage());
    }

    @Test
    void normalize() {
        assertEquals("hello-world", Calc.normalize("  Hello World "));
    }

    /** Fails on the original code; baselines must exclude it. */
    @Test
    void knownBroken() {
        assertEquals(2, Calc.clamp(1, 2, 3) + 1, "known broken");
    }
}
