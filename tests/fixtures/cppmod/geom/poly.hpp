#ifndef GEOM_POLY_HPP
#define GEOM_POLY_HPP

#include <vector>

#include "vec.hpp"

namespace geom {

// Polygon is a closed polygon given by its vertices in order.
class Polygon {
public:
    explicit Polygon(std::vector<Vec2> pts);
    double area() const;
    double perimeter() const;
    std::size_t size() const { return pts_.size(); }

private:
    std::vector<Vec2> pts_;
};

}  // namespace geom

#endif
