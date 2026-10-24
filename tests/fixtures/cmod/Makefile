CC ?= cc
CFLAGS ?= -O0 -g -w
PROGS = test_calc test_stats test_broken

.PHONY: test build

build: $(addprefix build/,$(PROGS))

build/test_calc: calc/test_calc.c calc/calc.c
	@mkdir -p build
	$(CC) $(CFLAGS) -Icalc -o $@ calc/test_calc.c calc/calc.c

build/test_stats: calc/test_stats.c calc/stats.c
	@mkdir -p build
	$(CC) $(CFLAGS) -Icalc -o $@ calc/test_stats.c calc/stats.c

build/test_broken: calc/test_broken.c calc/calc.c
	@mkdir -p build
	$(CC) $(CFLAGS) -Icalc -o $@ calc/test_broken.c calc/calc.c

test: build
	./build/test_calc && ./build/test_stats && ./build/test_broken
