#ifndef CRING_H
#define CRING_H

struct cring {
	int *buf;
	int cap;
	int head;
	int len;
};

struct cring *cring_new(int cap);
void cring_free(struct cring *r);
void cring_push(struct cring *r, int v);
int cring_len(const struct cring *r);

/* implemented in Go (ring/export.go, //export ring_on_full) */
extern void ring_on_full(int n);

#endif
