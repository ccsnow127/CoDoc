#include <stdio.h>

#include "stats.h"

int main(void) {
    struct stats s = {0, 0, 0};
    struct stats t = {4, 10, 0};
    stats_add(&s, 3);
    stats_add(&s, 7);
    stats_add(&s, 5);
    if (s.count != 3 || s.total != 15 || s.max != 7) {
        printf("FAIL: stats_add count=%d total=%d max=%d\n", s.count, s.total, s.max);
        return 1;
    }
    if (stats_mean(&t) != 25) {
        printf("FAIL: stats_mean = %d\n", stats_mean(&t));
        return 1;
    }
    printf("ok\n");
    return 0;
}
