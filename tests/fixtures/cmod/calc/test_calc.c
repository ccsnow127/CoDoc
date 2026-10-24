#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "calc.h"

static int failures = 0;

static void expect(int ok, const char *what) {
    if (!ok) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

int main(void) {
    int values[] = {1, 2, 3};
    int err = -1;
    char buf[32];
    expect(clamp(5, 0, 10) == 5, "clamp inside");
    expect(clamp(-1, 0, 10) == 0, "clamp low");
    expect(clamp(11, 0, 10) == 10, "clamp high");
    expect(sum(values, 3, &err) == 6 && err == CALC_OK, "sum");
    expect(sum(values, 0, &err) == 0 && err == CALC_EMPTY, "sum empty");
    expect(normalize("  Hello World ", buf, sizeof buf) == 11, "normalize length");
    expect(strcmp(buf, "hello-world") == 0, "normalize text");
    if (failures) {
        return 1;
    }
    printf("ok\n");
    exit(0);
}
