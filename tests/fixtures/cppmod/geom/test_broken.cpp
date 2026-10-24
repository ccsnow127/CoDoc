#include <cstdio>

#include "vec.hpp"

// known broken: fails on the original code; baselines must exclude it.
int main() {
    if (geom::Vec2(1, 1).norm() != 1.0) {
        std::printf("FAIL: known broken\n");
        return 1;
    }
    return 0;
}
