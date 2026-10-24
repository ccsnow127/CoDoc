#include "poly.hpp"

#include <cmath>
#include <utility>

namespace geom {

Polygon::Polygon(std::vector<Vec2> pts) : pts_(std::move(pts)) {}

// area returns the absolute shoelace area, 0 for fewer than 3 vertices.
double Polygon::area() const {
    if (pts_.size() < 3) {
        return 0.0;
    }
    double twice = 0.0;
    for (std::size_t i = 0; i < pts_.size(); i++) {
        const Vec2 &a = pts_[i];
        const Vec2 &b = pts_[(i + 1) % pts_.size()];
        twice += a.x() * b.y() - b.x() * a.y();
    }
    return std::fabs(twice) / 2.0;
}

// perimeter returns the sum of the edge lengths.
double Polygon::perimeter() const {
    double total = 0.0;
    for (std::size_t i = 0; i + 1 < pts_.size() + 1 && pts_.size() > 1; i++) {
        const Vec2 &a = pts_[i];
        const Vec2 &b = pts_[(i + 1) % pts_.size()];
        total += Vec2(b.x() - a.x(), b.y() - a.y()).norm();
    }
    return total;
}

}  // namespace geom
