#include <stdlib.h>
#include "cring.h"

static int wrap(const struct cring *r, int i) {
	return i % r->cap;
}

struct cring *cring_new(int cap) {
	struct cring *r = calloc(1, sizeof *r);
	if (r == NULL) {
		return NULL;
	}
	r->buf = calloc((size_t)(cap > 0 ? cap : 1), sizeof(int));
	r->cap = cap > 0 ? cap : 1;
	return r;
}

void cring_free(struct cring *r) {
	if (r != NULL) {
		free(r->buf);
		free(r);
	}
}

void cring_push(struct cring *r, int v) {
	if (r->len == r->cap) {
		r->head = wrap(r, r->head + 1);
		r->len--;
		ring_on_full(1);
	}
	r->buf[wrap(r, r->head + r->len)] = v;
	r->len++;
}

int cring_len(const struct cring *r) {
	return r->len;
}
