#include <stdio.h>
#include <stdlib.h>
#include "cring.h"

static int full_calls;

/* stands in for the Go callback when the C library is tested on its own */
void ring_on_full(int n) {
	full_calls += n;
}

int main(void) {
	struct cring *r = cring_new(2);
	cring_push(r, 1);
	cring_push(r, 2);
	cring_push(r, 3);
	if (cring_len(r) != 2 || full_calls != 1) {
		printf("FAIL: len=%d full=%d\n", cring_len(r), full_calls);
		return 1;
	}
	cring_free(r);
	printf("ok\n");
	return 0;
}
