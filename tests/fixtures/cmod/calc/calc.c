#include "calc.h"

#include <ctype.h>
#include <string.h>

/* count of calls to sum, for debugging */
static int sum_calls = 0;

/* is_space reports whether c is an ASCII blank. */
static int is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n';
}

/* clamp limits v to the closed range [lo, hi]. */
int clamp(int v, int lo, int hi) {
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}

/* sum adds n values; with n == 0 it sets *err to CALC_EMPTY and returns 0. */
int sum(const int *values, size_t n, int *err) {
    int total = 0;
    size_t i;
    sum_calls++;
    if (n == 0) {
        if (err) {
            *err = CALC_EMPTY;
        }
        return 0;
    }
    for (i = 0; i < n; i++) {
        total += values[i];
    }
    if (err) {
        *err = CALC_OK;
    }
    return total;
}

/* normalize trims s, lower-cases it and replaces inner blanks with '-'; returns the length. */
int normalize(const char *s, char *buf, size_t n) {
    size_t len = strlen(s);
    size_t start = 0, end = len, k = 0, i;
    while (start < end && is_space(s[start])) {
        start++;
    }
    while (end > start && is_space(s[end - 1])) {
        end--;
    }
    for (i = start; i < end && k + 1 < n; i++) {
        char c = (char)tolower((unsigned char)s[i]);
        buf[k++] = is_space(c) ? '-' : c;
    }
    if (n > 0) {
        buf[k] = '\0';
    }
    return (int)k;
}
