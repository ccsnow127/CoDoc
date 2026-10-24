// Package ring wraps the C ring buffer of clib through cgo.
package ring

/*
#cgo CFLAGS: -I../clib/include
#include <stdlib.h>
#include "cring.h"

static int ring_cap_hint(int n) {
	return n < 1 ? 1 : n;
}
*/
import "C"

// Ring is a fixed-capacity ring of ints backed by C memory.
type Ring struct {
	p *C.struct_cring
}

// New allocates a ring that holds up to n values (at least one).
func New(n int) *Ring {
	return &Ring{p: C.cring_new(C.ring_cap_hint(C.int(n)))}
}

// Close releases the C memory of the ring.
func (r *Ring) Close() {
	if r.p != nil {
		C.cring_free(r.p)
		r.p = nil
	}
}

// Push appends v, dropping the oldest value when the ring is full.
func (r *Ring) Push(v int) {
	C.cring_push(r.p, C.int(v))
}

// Len is the number of values held.
func (r *Ring) Len() int {
	return int(C.cring_len(r.p))
}

// Fill pushes every value in order and returns the resulting length.
func Fill(r *Ring, values []int) int {
	for _, v := range values {
		r.Push(v)
	}
	return r.Len()
}

// Collect pushes values into a fresh ring sized for them and reports how many it kept.
func Collect(values []int) int {
	r := New(len(values))
	defer r.Close()
	return Fill(r, values)
}
