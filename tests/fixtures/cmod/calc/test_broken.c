#include <stdio.h>

#include "calc.h"

/* known broken: fails on the original code; baselines must exclude it. */
int main(void) {
    if (clamp(1, 2, 3) != 1) {
        printf("FAIL: known broken\n");
        return 1;
    }
    return 0;
}
