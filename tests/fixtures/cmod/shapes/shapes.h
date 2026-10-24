#ifndef SHAPES_H
#define SHAPES_H

/* rect is an axis-aligned rectangle. */
typedef struct rect {
    int w;
    int h;
} rect_t;

int rect_area(rect_t r);
const char *classify(rect_t r);

#endif
