#include "vec.hpp"

#include <cmath>

namespace geom {

Vec2::Vec2(double x, double y) : x_(x), y_(y) {}

// add returns the component-wise sum.
Vec2 Vec2::add(const Vec2 &o) const {
    return Vec2(x_ + o.x_, y_ + o.y_);
}

// dot returns the dot product.
double Vec2::dot(const Vec2 &o) const {
    return x_ * o.x_ + y_ * o.y_;
}

// norm returns the euclidean length.
double Vec2::norm() const {
    return std::sqrt(dot(*this));
}

// operator== compares both components exactly.
bool Vec2::operator==(const Vec2 &o) const {
    return x_ == o.x_ && y_ == o.y_;
}

}  // namespace geom
