#ifndef STATS_H
#define STATS_H

/* stats accumulates integer observations. */
struct stats {
    int count;
    int total;
    int max;
};

void stats_add(struct stats *s, int v);
int stats_mean(const struct stats *s);

#endif
