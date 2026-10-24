package com.example.calc;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class StatsTest {

    @Test
    void add() {
        Stats s = new Stats();
        s.add(3);
        s.add(7);
        s.add(5);
        assertEquals(3, s.getCount());
        assertEquals(7, s.getMax());
        assertEquals(15, s.total);
    }

    @Test
    void mean() {
        Stats s = new Stats();
        assertEquals(0, s.mean());
        s.add(2);
        s.add(3);
        assertEquals(25, s.mean());
    }
}
