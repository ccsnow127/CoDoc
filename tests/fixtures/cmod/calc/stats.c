#include "stats.h"

#include "calc.h"

/* stats_add records one observation. */
void stats_add(struct stats *s, int v) {
    if (s->count == 0 || v > s->max) {
        s->max = v;
    }
    s->count++;
    s->total += v;
}

/* stats_mean returns total/count in fixed point (times CALC_SCALE), or 0 when empty. */
int stats_mean(const struct stats *s) {
    if (s->count == 0) {
        return 0;
    }
    return s->total * CALC_SCALE / s->count;
}
