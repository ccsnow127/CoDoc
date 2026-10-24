package shapes

import "testing"

func TestCodocsRect(t *testing.T) {
	r := Rect{W: 2, H: 3}
	if r.Area() != 6 {
		t.Fatalf("Area = %v", r.Area())
	}
	if (Rect{W: -1, H: 3}).Area() != 0 || (Rect{W: 1, H: -3}).Area() != 0 {
		t.Fatal("negative side must give 0")
	}
	if r.Name() != "rect 2.0x3.0" {
		t.Fatalf("Name = %q", r.Name())
	}
}

func TestCodocsClassify(t *testing.T) {
	for _, c := range []struct {
		r    Rect
		want string
	}{{Rect{0, 5}, "empty"}, {Rect{1, 1}, "small"}, {Rect{2, 5}, "large"}, {Rect{3, 3}, "small"}} {
		if got := Classify(c.r); got != c.want {
			t.Errorf("Classify(%v) = %q, want %q", c.r, got, c.want)
		}
	}
}

func TestCodocsLargest(t *testing.T) {
	if Largest(nil) != nil {
		t.Fatal("Largest(nil) must be nil")
	}
	a, b := Rect{1, 1}, Rect{2, 2}
	if got := Largest([]Shape{a, b, a}); got != b {
		t.Fatalf("Largest = %v", got)
	}
	if got := Largest([]Shape{b, b}); got != b {
		t.Fatalf("Largest = %v", got)
	}
}
