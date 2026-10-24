#include "shapes.h"

/* rect_area returns w*h, or 0 for a degenerate rectangle. */
int rect_area(rect_t r) {
    if (r.w <= 0 || r.h <= 0) {
        return 0;
    }
    return r.w * r.h;
}

/* classify names the rectangle: "empty", "square" or "rect". */
const char *classify(rect_t r) {
    if (rect_area(r) == 0) {
        return "empty";
    }
    if (r.w == r.h) {
        return "square";
    }
    return "rect";
}
