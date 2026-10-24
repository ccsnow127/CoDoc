package calc

import "testing"

func TestStatsAdd(t *testing.T) {
	var s Stats
	s.Add(3)
	s.Add(7)
	s.Add(5)
	if s.Count != 3 || s.Total != 15 || s.Max != 7 {
		t.Fatalf("Stats = %+v", s)
	}
}

func TestStatsMean(t *testing.T) {
	s := Stats{Count: 4, Total: 10}
	if got := s.Mean(); got != 25 {
		t.Fatalf("Mean = %d, want 25", got)
	}
	if got := (Stats{}).Mean(); got != 0 {
		t.Fatalf("empty Mean = %d", got)
	}
}
