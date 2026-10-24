#include <cstdio>

#include "vec.hpp"

int main() {
    geom::Vec2 a(1, 2), b(3, 4);
    if (!(a.add(b) == geom::Vec2(4, 6))) {
        std::printf("FAIL: add\n");
        return 1;
    }
    if (a.dot(b) != 11.0) {
        std::printf("FAIL: dot\n");
        return 1;
    }
    if (geom::Vec2(3, 4).norm() != 5.0) {
        std::printf("FAIL: norm\n");
        return 1;
    }
    std::printf("ok\n");
    return 0;
}
