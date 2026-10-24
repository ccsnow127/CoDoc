package ring

import "C"

// fullEvents counts how often the C side reported a full ring.
var fullEvents int

//export ring_on_full
func ring_on_full(n C.int) {
	fullEvents += int(n)
}

// FullEvents is the number of times a push hit a full ring.
func FullEvents() int {
	return fullEvents
}
