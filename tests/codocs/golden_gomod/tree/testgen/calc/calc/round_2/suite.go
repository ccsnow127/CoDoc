package calc

import "testing"

func TestCodocsClamp(t *testing.T) {
	cases := []struct{ v, lo, hi, want int }{
		{5, 0, 10, 5}, {-1, 0, 10, 0}, {11, 0, 10, 10}, {0, 0, 10, 0}, {10, 0, 10, 10},
	}
	for _, c := range cases {
		if got := Clamp(c.v, c.lo, c.hi); got != c.want {
			t.Errorf("Clamp(%d,%d,%d) = %d, want %d", c.v, c.lo, c.hi, got, c.want)
		}
	}
}

func TestCodocsSum(t *testing.T) {
	if got, err := Sum([]int{4, 5, 6}); err != nil || got != 15 {
		t.Fatalf("Sum = %d, %v", got, err)
	}
	if got, err := Sum(nil); err != ErrEmpty || got != 0 {
		t.Fatalf("Sum(nil) = %d, %v", got, err)
	}
}

func TestCodocsNormalize(t *testing.T) {
	if got := Normalize("  Go Is Fun "); got != "go-is-fun" {
		t.Fatalf("Normalize = %q", got)
	}
}
