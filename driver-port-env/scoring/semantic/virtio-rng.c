// Post-hoc semantic probes. Run unchanged against every frozen candidate.
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CONTROL "/sys/class/misc/hw_random/"
enum { FAILED = 1, UNSUPPORTED = 77, INVALID = 78 };

static int result(const char *name, int status, const char *detail)
{
    printf("RNG_SEMANTIC case=%s status=%s detail=%s\n", name,
           status == 0 ? "PASS" : status == UNSUPPORTED ? "UNSUPPORTED" :
           status == INVALID ? "HARNESS_INVALID" : "FAIL", detail);
    fflush(stdout);
    return status;
}

static int attribute(const char *name, char *buf, size_t size)
{
    char path[256];
    snprintf(path, sizeof(path), CONTROL "%s", name);
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, size - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = 0;
    buf[strcspn(buf, "\r\n")] = 0;
    return 0;
}

static int set_attribute(const char *name, const char *value)
{
    char path[256], text[256];
    snprintf(path, sizeof(path), CONTROL "%s", name);
    snprintf(text, sizeof(text), "%s\n", value);
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    ssize_t n = write(fd, text, strlen(text));
    int saved = errno;
    close(fd);
    errno = saved;
    return n == (ssize_t)strlen(text) ? 0 : -1;
}

static int selection(void)
{
    char original[256], current[256];
    if (attribute("rng_current", original, sizeof(original)) < 0)
        return result("selection", UNSUPPORTED, "control-interface-unavailable");
    if (!strcmp(original, "none"))
        return result("selection", INVALID, "reference-started-with-no-source");
    int fd = open("/dev/hwrng", O_RDONLY | O_NONBLOCK);
    if (fd < 0) return result("selection", FAILED, "device-unavailable");
    int ok = set_attribute("rng_current", "none") == 0;
    ok = ok && attribute("rng_current", current, sizeof(current)) == 0
            && !strcmp(current, "none");
    unsigned char byte;
    errno = 0;
    ssize_t n = read(fd, &byte, 1); // Selection must apply to an already-open fd.
    ok = ok && n == -1 && errno == ENODEV;
    int restored = set_attribute("rng_current", original) == 0;
    ok = ok && restored && attribute("rng_current", current, sizeof(current)) == 0
            && !strcmp(current, original);
    close(fd);
    if (restored) {
        fd = open("/dev/hwrng", O_RDONLY);
        ok = ok && fd >= 0;
        if (fd >= 0) {
            ok = ok && read(fd, &byte, 1) == 1;
            close(fd);
        }
    }
    return result("selection", ok ? 0 : FAILED, "deselect-existing-fd-and-restore");
}

static int quality(void)
{
    char original[64], observed[64];
    if (attribute("rng_quality", original, sizeof(original)) < 0)
        return result("quality", UNSUPPORTED, "control-interface-unavailable");
    int ok = 1;
    const char *valid[] = { "0", "37", "1024" };
    for (size_t i = 0; i < sizeof(valid) / sizeof(valid[0]); ++i)
        ok = set_attribute("rng_quality", valid[i]) == 0
             && attribute("rng_quality", observed, sizeof(observed)) == 0
             && strtol(observed, NULL, 10) == strtol(valid[i], NULL, 10) && ok;
    const char *invalid[] = { "1025", "65536", "-1", "not-a-number" };
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i)
        ok = set_attribute("rng_quality", invalid[i]) < 0
             && attribute("rng_quality", observed, sizeof(observed)) == 0
             && !strcmp(observed, "1024") && ok;
    ok = set_attribute("rng_quality", original) == 0 && ok;
    return result("quality", ok ? 0 : FAILED, "range-rejection-preserves-state");
}

static int partial(void)
{
    unsigned char bytes[128];
    // Frameworks may submit lazily: establish a completed request first.
    int prime = open("/dev/hwrng", O_RDONLY);
    if (prime < 0) return result("partial", FAILED, "device-unavailable");
    ssize_t primed = read(prime, bytes, 1);
    close(prime);
    if (primed != 1) return result("partial", FAILED, "blocking-prime-failed");
    int fd = open("/dev/hwrng", O_RDONLY | O_NONBLOCK);
    if (fd < 0) return result("partial", FAILED, "device-unavailable");
    // Controller allows exactly 8 device bytes per 60 seconds.
    // A positive short return must take precedence over subsequent EAGAIN.
    ssize_t n = read(fd, bytes, sizeof(bytes));
    printf("RNG_OBSERVATION first_read=%ld errno=%d\n", (long)n, errno);
    int ok = n > 0 && n <= 7;
    errno = 0;
    ssize_t next = read(fd, bytes, sizeof(bytes));
    printf("RNG_OBSERVATION next_read=%ld errno=%d\n", (long)next, errno);
    ok = ok && next == -1 && errno == EAGAIN;
    close(fd);
    return result("partial", ok ? 0 : FAILED, "positive-partial-before-eagain");
}

static int interrupted(void)
{
    unsigned char bytes[128];
    int prime = open("/dev/hwrng", O_RDONLY);
    if (prime < 0) return result("interrupt", FAILED, "device-unavailable");
    ssize_t primed = read(prime, bytes, 1);
    close(prime);
    if (primed != 1) return result("interrupt", FAILED, "blocking-prime-failed");
    int fd = open("/dev/hwrng", O_RDONLY | O_NONBLOCK);
    if (fd < 0) return result("interrupt", FAILED, "device-unavailable");
    size_t drained = 0;
    for (int i = 0; i < 32; ++i) {
        ssize_t n = read(fd, bytes, 1);
        if (n > 0) drained += (size_t)n;
        else if (n < 0 && errno == EAGAIN) break;
        else { close(fd); return result("interrupt", INVALID, "cannot-establish-starvation"); }
    }
    close(fd);
    printf("RNG_OBSERVATION drained_after_prime=%zu\n", drained);
    if (drained != 7)
        return result("interrupt", INVALID, "unexpected-rate-limit-budget");
    int ready[2];
    if (pipe(ready)) return result("interrupt", INVALID, "pipe-failed");
    pid_t child = fork();
    if (child < 0) return result("interrupt", INVALID, "fork-failed");
    if (!child) {
        close(ready[0]);
        int blocking = open("/dev/hwrng", O_RDONLY);
        if (blocking < 0) _exit(FAILED);
        if (write(ready[1], "R", 1) != 1) _exit(INVALID);
        errno = 0;
        ssize_t n = read(blocking, bytes, 1);
        printf("RNG_OBSERVATION unexpected_read_return=%ld errno=%d\n", (long)n, errno);
        fflush(stdout);
        _exit(FAILED);
    }
    close(ready[1]);
    char marker;
    if (read(ready[0], &marker, 1) != 1) {
        kill(child, SIGKILL);
        return result("interrupt", INVALID, "reader-not-ready");
    }
    close(ready[0]);
    struct timespec delay = { .tv_sec = 0, .tv_nsec = 100000000 };
    nanosleep(&delay, NULL);
    int status;
    if (waitpid(child, &status, WNOHANG) != 0)
        return result("interrupt", INVALID, "reader-did-not-block");
    // Linux virtio_read uses wait_for_completion_killable, not interruptible.
    // A handled SIGUSR1 need not interrupt it. SIGKILL must terminate the task.
    if (kill(child, SIGKILL)) return result("interrupt", INVALID, "signal-send-failed");
    for (int i = 0; i < 30; ++i) {
        pid_t done = waitpid(child, &status, WNOHANG);
        if (done == child)
            return result("interrupt", WIFSIGNALED(status) && WTERMSIG(status) == SIGKILL ? 0 : FAILED,
                          "blocked-read-remains-killable");
        if (done < 0) return result("interrupt", INVALID, "waitpid-failed");
        nanosleep(&delay, NULL);
    }
    kill(child, SIGKILL);
    // Do not wait forever for a reader stuck in an uninterruptible driver wait.
    // The external controller destroys this VM after recording the observation.
    return result("interrupt", FAILED, "signal-did-not-terminate-read-within-3s");
}

int main(int argc, char **argv)
{
    setbuf(stdout, NULL);
    if (argc != 2) return INVALID;
    if (!strcmp(argv[1], "selection")) return selection();
    if (!strcmp(argv[1], "quality")) return quality();
    if (!strcmp(argv[1], "partial")) return partial();
    if (!strcmp(argv[1], "interrupt")) return interrupted();
    return INVALID;
}
