package ring

import "testing"

func TestPushLen(t *testing.T) {
	r := New(2)
	defer r.Close()
	r.Push(1)
	r.Push(2)
	r.Push(3)
	if got := r.Len(); got != 2 {
		t.Fatalf("Len() = %d, want 2", got)
	}
	if FullEvents() == 0 {
		t.Fatalf("expected a full-ring event")
	}
}

func TestFill(t *testing.T) {
	r := New(0)
	defer r.Close()
	if got := Fill(r, []int{4, 5}); got != 1 {
		t.Fatalf("Fill() = %d, want 1", got)
	}
}
