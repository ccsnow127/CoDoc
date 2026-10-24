package calc

import (
	"errors"
	"strings"
)

// ErrEmpty is returned when the input is empty.
var ErrEmpty = errors.New("calc: empty input")

// Scale is the fixed-point factor used by Stats.Mean.
const Scale = 10

// Clamp limits v to the closed range [lo, hi].
func Clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// Sum adds all values and returns ErrEmpty when there are none.
func Sum(values []int) (int, error) {
	if len(values) == 0 {
		return 0, ErrEmpty
	}
	total := 0
	for _, v := range values {
		total += v
	}
	return total, nil
}

// Normalize trims and lower-cases s, then replaces inner spaces with "-".
func Normalize(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	return strings.ReplaceAll(s, " ", "-")
}
