#ifndef GEOM_VEC_HPP
#define GEOM_VEC_HPP

namespace geom {

// Vec2 is an immutable 2-d vector of doubles.
class Vec2 {
public:
    Vec2(double x, double y);
    double x() const { return x_; }
    double y() const { return y_; }
    Vec2 add(const Vec2 &o) const;
    double dot(const Vec2 &o) const;
    double norm() const;
    bool operator==(const Vec2 &o) const;

private:
    double x_;
    double y_;
};

}  // namespace geom

#endif
