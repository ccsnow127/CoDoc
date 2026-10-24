/* calc: small integer and string helpers. */
#ifndef CALC_H
#define CALC_H

#include <stddef.h>

/* fixed-point factor used by stats_mean */
#define CALC_SCALE 10

/* error codes reported through the err out-parameter */
#define CALC_OK 0
#define CALC_EMPTY 1

int clamp(int v, int lo, int hi);
int sum(const int *values, size_t n, int *err);
int normalize(const char *s, char *buf, size_t n);

#endif
