package calc

import "testing"

func TestClamp(t *testing.T) {
	cases := []struct{ v, lo, hi, want int }{
		{5, 0, 10, 5}, {-1, 0, 10, 0}, {11, 0, 10, 10},
	}
	for _, c := range cases {
		if got := Clamp(c.v, c.lo, c.hi); got != c.want {
			t.Errorf("Clamp(%d,%d,%d) = %d, want %d", c.v, c.lo, c.hi, got, c.want)
		}
	}
}

func TestSum(t *testing.T) {
	if got, err := Sum([]int{1, 2, 3}); err != nil || got != 6 {
		t.Fatalf("Sum = %d, %v; want 6, nil", got, err)
	}
	if _, err := Sum(nil); err != ErrEmpty {
		t.Fatalf("Sum(nil) err = %v, want ErrEmpty", err)
	}
}

func TestNormalize(t *testing.T) {
	if got := Normalize("  Hello World "); got != "hello-world" {
		t.Fatalf("Normalize = %q", got)
	}
}

// TestKnownBroken fails on the original code; baselines must exclude it.
func TestKnownBroken(t *testing.T) {
	if Clamp(1, 2, 3) != 1 {
		t.Fatal("known broken")
	}
}
