/* codocs_test.h: the embedded assertion header of codocs-generated C/C++ test suites.
 *
 * a suite is one translation unit:
 *
 *     #include "codocs_test.h"
 *     #include CODOCS_TARGET
 *
 *     CODOCS_TEST(clamp_bounds) { CHECK_EQ_INT(clamp(5, 0, 10), 5); }
 *
 * it is linked with codocs_main.c (C) or codocs_main.cpp (C++), which runs the registered cases
 * and reports them as "@@CODOCS RUN <name>" / "@@CODOCS PASS|FAIL|SKIP <name> [<message>]".
 * compiles as C99 and as C++11.
 */
#ifndef CODOCS_TEST_H
#define CODOCS_TEST_H

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*codocs_case_fn)(void);

void codocs_register(const char *name, const char *file, int line, codocs_case_fn fn);
void codocs_fail(const char *file, int line, const char *fmt, ...);
void codocs_skip(const char *file, int line, const char *msg);
int codocs_case_failed(void);

#ifdef __cplusplus
}
#endif

/* CODOCS_TEST(name) { ... } defines one test case and registers it before main runs */
#define CODOCS_TEST(name)                                                              \
    static void codocs_case_##name(void);                                              \
    __attribute__((constructor)) static void codocs_register_##name(void) {            \
        codocs_register(#name, __FILE__, __LINE__, codocs_case_##name);                \
    }                                                                                  \
    static void codocs_case_##name(void)

/* CHECK(cond): records a failure and continues */
#define CHECK(cond)                                                                    \
    do {                                                                               \
        if (!(cond)) codocs_fail(__FILE__, __LINE__, "CHECK(%s)", #cond);              \
    } while (0)

#define CHECK_EQ_INT(a, b)                                                             \
    do {                                                                               \
        long long codocs_a_ = (long long)(a), codocs_b_ = (long long)(b);              \
        if (codocs_a_ != codocs_b_)                                                    \
            codocs_fail(__FILE__, __LINE__, "CHECK_EQ_INT(%s, %s): %lld != %lld", #a, #b, \
                        codocs_a_, codocs_b_);                                         \
    } while (0)

#define CHECK_EQ_UINT(a, b)                                                            \
    do {                                                                               \
        unsigned long long codocs_a_ = (unsigned long long)(a);                        \
        unsigned long long codocs_b_ = (unsigned long long)(b);                        \
        if (codocs_a_ != codocs_b_)                                                    \
            codocs_fail(__FILE__, __LINE__, "CHECK_EQ_UINT(%s, %s): %llu != %llu", #a, #b, \
                        codocs_a_, codocs_b_);                                         \
    } while (0)

#define CHECK_EQ_DBL(a, b, eps)                                                        \
    do {                                                                               \
        double codocs_a_ = (double)(a), codocs_b_ = (double)(b);                       \
        if (!(fabs(codocs_a_ - codocs_b_) <= (double)(eps)))                           \
            codocs_fail(__FILE__, __LINE__, "CHECK_EQ_DBL(%s, %s): %.17g != %.17g", #a, #b, \
                        codocs_a_, codocs_b_);                                         \
    } while (0)

#define CHECK_EQ_STR(a, b)                                                             \
    do {                                                                               \
        const char *codocs_a_ = (a), *codocs_b_ = (b);                                 \
        if ((codocs_a_ == NULL || codocs_b_ == NULL) ? codocs_a_ != codocs_b_          \
                                                     : strcmp(codocs_a_, codocs_b_) != 0) \
            codocs_fail(__FILE__, __LINE__, "CHECK_EQ_STR(%s, %s): \"%s\" != \"%s\"", #a, #b, \
                        codocs_a_ ? codocs_a_ : "(null)", codocs_b_ ? codocs_b_ : "(null)"); \
    } while (0)

#define CHECK_EQ_MEM(a, b, n)                                                          \
    do {                                                                               \
        if (memcmp((const void *)(a), (const void *)(b), (size_t)(n)) != 0)            \
            codocs_fail(__FILE__, __LINE__, "CHECK_EQ_MEM(%s, %s, %s): bytes differ", #a, #b, #n); \
    } while (0)

#define CHECK_NULL(p)                                                                  \
    do {                                                                               \
        if ((const void *)(p) != NULL)                                                 \
            codocs_fail(__FILE__, __LINE__, "CHECK_NULL(%s): not NULL", #p);           \
    } while (0)

#define CHECK_NOT_NULL(p)                                                              \
    do {                                                                               \
        if ((const void *)(p) == NULL)                                                 \
            codocs_fail(__FILE__, __LINE__, "CHECK_NOT_NULL(%s): NULL", #p);           \
    } while (0)

/* REQUIRE(cond): like CHECK, then returns from the case */
#define REQUIRE(cond)                                                                  \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            codocs_fail(__FILE__, __LINE__, "REQUIRE(%s)", #cond);                     \
            return;                                                                    \
        }                                                                              \
    } while (0)

/* SKIP(msg): marks the case skipped and returns */
#define SKIP(msg)                                                                      \
    do {                                                                               \
        codocs_skip(__FILE__, __LINE__, (msg));                                        \
        return;                                                                        \
    } while (0)

#endif /* CODOCS_TEST_H */
