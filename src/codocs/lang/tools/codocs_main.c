/* codocs_main.c: the test runner main of codocs-generated C/C++ suites (see codocs_test.h).
 *
 * usage: suite [case ...]   runs every registered case sorted by (file, line), or only the named
 * ones. each case runs in a forked child (fork mode, the default), so a crash or a hang fails that
 * case only; CODOCS_NOFORK=1 runs the cases in-process.
 *
 * environment:
 *   CODOCS_TIMEOUT=<s>   deadline for the whole run: in fork mode the child of the running case
 *                        gets alarm(<remaining>); in no-fork mode the process gets alarm(<s>).
 *                        on expiry the running case FAILs, "@@CODOCS TIMEOUT" is printed and the
 *                        program exits with status 124.
 *   CODOCS_FAILFAST=1    stop after the first failing case.
 *
 * output (stdout, unbuffered): "@@CODOCS RUN <name>" before each case and
 * "@@CODOCS PASS|FAIL|SKIP <name> [<file>:<line>: <message>]" after it. exit status 0 when no
 * case failed, else 1.
 */
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif

#include <errno.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifdef __cplusplus
#include <exception>
#endif

#include "codocs_test.h"

#define CODOCS_MSG 1024
#define CODOCS_EXIT_PASS 70
#define CODOCS_EXIT_FAIL 71
#define CODOCS_EXIT_SKIP 72

typedef struct {
    const char *name;
    const char *file;
    int line;
    codocs_case_fn fn;
} codocs_case;

static codocs_case *codocs_cases = NULL;
static int codocs_ncases = 0;
static int codocs_cap = 0;
static int codocs_failed_now = 0;
static int codocs_skipped_now = 0;
static char codocs_msg[CODOCS_MSG];
static const char *codocs_running = NULL;

#ifdef __cplusplus
extern "C" {
#endif

void codocs_register(const char *name, const char *file, int line, codocs_case_fn fn) {
    if (codocs_ncases == codocs_cap) {
        int cap = codocs_cap ? codocs_cap * 2 : 64;
        codocs_case *grown = (codocs_case *)realloc(codocs_cases, (size_t)cap * sizeof *grown);
        if (grown == NULL) {
            fprintf(stderr, "codocs: out of memory registering %s\n", name);
            exit(2);
        }
        codocs_cases = grown;
        codocs_cap = cap;
    }
    codocs_cases[codocs_ncases].name = name;
    codocs_cases[codocs_ncases].file = file;
    codocs_cases[codocs_ncases].line = line;
    codocs_cases[codocs_ncases].fn = fn;
    codocs_ncases++;
}

void codocs_fail(const char *file, int line, const char *fmt, ...) {
    char text[CODOCS_MSG];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    printf("%s:%d: %s\n", file, line, text);
    if (!codocs_failed_now) {
        snprintf(codocs_msg, sizeof codocs_msg, "%s:%d: %s", file, line, text);
    }
    codocs_failed_now = 1;
}

void codocs_skip(const char *file, int line, const char *msg) {
    codocs_skipped_now = 1;
    if (!codocs_failed_now) {
        snprintf(codocs_msg, sizeof codocs_msg, "%s:%d: %s", file, line, msg ? msg : "skipped");
    }
}

int codocs_case_failed(void) { return codocs_failed_now; }

#ifdef __cplusplus
}
#endif

static int codocs_cmp(const void *a, const void *b) {
    const codocs_case *x = (const codocs_case *)a;
    const codocs_case *y = (const codocs_case *)b;
    int c = strcmp(x->file, y->file);
    if (c != 0) return c;
    return (x->line > y->line) - (x->line < y->line);
}

static void codocs_oneline(char *s) {
    for (; *s; s++) {
        if (*s == '\n' || *s == '\r') *s = ' ';
    }
}

/* run one case in this process: CODOCS_EXIT_PASS, _FAIL or _SKIP */
static int codocs_run_case(const codocs_case *c) {
    codocs_failed_now = 0;
    codocs_skipped_now = 0;
    codocs_msg[0] = '\0';
#ifdef __cplusplus
    try {
        c->fn();
    } catch (const std::exception &e) {
        codocs_fail(c->file, c->line, "uncaught exception: %s", e.what());
    } catch (...) {
        codocs_fail(c->file, c->line, "uncaught exception");
    }
#else
    c->fn();
#endif
    if (codocs_failed_now) return CODOCS_EXIT_FAIL;
    if (codocs_skipped_now) return CODOCS_EXIT_SKIP;
    return CODOCS_EXIT_PASS;
}

static void codocs_report(const char *verdict, const codocs_case *c, const char *msg) {
    char text[CODOCS_MSG];
    snprintf(text, sizeof text, "%s", msg ? msg : "");
    codocs_oneline(text);
    if (text[0]) {
        printf("@@CODOCS %s %s %s\n", verdict, c->name, text);
    } else {
        printf("@@CODOCS %s %s\n", verdict, c->name);
    }
}

static void codocs_on_alarm(int sig) {
    char line[CODOCS_MSG];
    int n;
    (void)sig;
    if (codocs_running != NULL) {
        n = snprintf(line, sizeof line, "@@CODOCS FAIL %s timed out\n", codocs_running);
        if (n > 0) {
            ssize_t w = write(1, line, (size_t)n < sizeof line ? (size_t)n : sizeof line - 1);
            (void)w;
        }
    }
    {
        static const char t[] = "@@CODOCS TIMEOUT\n";
        ssize_t w = write(1, t, sizeof t - 1);
        (void)w;
    }
    _exit(124);
}

static int codocs_selected(const codocs_case *c, int argc, char **argv) {
    int i;
    if (argc <= 1) return 1;
    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], c->name) == 0) return 1;
    }
    return 0;
}

/* run one case in a forked child; returns the verdict exit code, or -1 on a timeout */
static int codocs_fork_case(const codocs_case *c, long remaining) {
    int fds[2];
    pid_t pid;
    int status = 0;
    char msg[CODOCS_MSG];
    ssize_t got = 0;
    if (pipe(fds) != 0) {
        return codocs_run_case(c);
    }
    pid = fork();
    if (pid < 0) {
        close(fds[0]);
        close(fds[1]);
        return codocs_run_case(c);
    }
    if (pid == 0) {
        int rc;
        size_t len;
        close(fds[0]);
        signal(SIGALRM, SIG_DFL);
        if (remaining > 0) alarm((unsigned)remaining);
        rc = codocs_run_case(c);
        alarm(0);
        len = strlen(codocs_msg);
        if (len > 0) {
            ssize_t w = write(fds[1], codocs_msg, len);
            (void)w;
        }
        close(fds[1]);
        fflush(NULL);
        exit(rc); /* exit, not _exit: coverage counters are written at exit */
    }
    close(fds[1]);
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            status = 0;
            break;
        }
    }
    {
        size_t room = sizeof msg - 1;
        while ((size_t)got < room) {
            ssize_t n = read(fds[0], msg + got, room - (size_t)got);
            if (n <= 0) break;
            got += n;
        }
    }
    close(fds[0]);
    msg[got > 0 ? got : 0] = '\0';
    if (WIFEXITED(status)) {
        int code = WEXITSTATUS(status);
        if (code == CODOCS_EXIT_PASS || code == CODOCS_EXIT_FAIL || code == CODOCS_EXIT_SKIP) {
            snprintf(codocs_msg, sizeof codocs_msg, "%s", msg);
            return code;
        }
        snprintf(codocs_msg, sizeof codocs_msg, "%s:%d: the case exited with status %d%s%s",
                 c->file, c->line, code, msg[0] ? "; " : "", msg);
        return CODOCS_EXIT_FAIL;
    }
    if (WIFSIGNALED(status)) {
        int sig = WTERMSIG(status);
        if (sig == SIGALRM) {
            snprintf(codocs_msg, sizeof codocs_msg, "%s:%d: timed out", c->file, c->line);
            return -1;
        }
        snprintf(codocs_msg, sizeof codocs_msg, "%s:%d: crashed with signal %d (%s)%s%s", c->file,
                 c->line, sig, strsignal(sig), msg[0] ? "; " : "", msg);
        return CODOCS_EXIT_FAIL;
    }
    snprintf(codocs_msg, sizeof codocs_msg, "%s:%d: the case did not finish", c->file, c->line);
    return CODOCS_EXIT_FAIL;
}

int main(int argc, char **argv) {
    const char *env;
    long timeout = 0;
    int nofork = 0, failfast = 0, any_failed = 0, i;
    time_t start = time(NULL);

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    env = getenv("CODOCS_TIMEOUT");
    if (env && *env) timeout = strtol(env, NULL, 10);
    env = getenv("CODOCS_NOFORK");
    nofork = env && *env && strcmp(env, "0") != 0;
    env = getenv("CODOCS_FAILFAST");
    failfast = env && *env && strcmp(env, "0") != 0;

    if (codocs_ncases > 1) {
        qsort(codocs_cases, (size_t)codocs_ncases, sizeof *codocs_cases, codocs_cmp);
    }
    if (nofork && timeout > 0) {
        signal(SIGALRM, codocs_on_alarm);
        alarm((unsigned)timeout);
    }
    for (i = 0; i < codocs_ncases; i++) {
        const codocs_case *c = &codocs_cases[i];
        int rc;
        if (!codocs_selected(c, argc, argv)) continue;
        printf("@@CODOCS RUN %s\n", c->name);
        codocs_running = c->name;
        if (nofork) {
            rc = codocs_run_case(c);
        } else {
            long remaining = 0;
            if (timeout > 0) {
                remaining = timeout - (long)(time(NULL) - start);
                if (remaining <= 0) {
                    codocs_report("FAIL", c, "timed out");
                    printf("@@CODOCS TIMEOUT\n");
                    return 124;
                }
            }
            rc = codocs_fork_case(c, remaining);
            if (rc < 0) {
                codocs_report("FAIL", c, codocs_msg);
                printf("@@CODOCS TIMEOUT\n");
                return 124;
            }
        }
        codocs_running = NULL;
        if (rc == CODOCS_EXIT_FAIL) {
            any_failed = 1;
            codocs_report("FAIL", c, codocs_msg[0] ? codocs_msg : "failed");
            if (failfast) break;
        } else if (rc == CODOCS_EXIT_SKIP) {
            codocs_report("SKIP", c, codocs_msg);
        } else {
            codocs_report("PASS", c, NULL);
        }
    }
    return any_failed ? 1 : 0;
}
