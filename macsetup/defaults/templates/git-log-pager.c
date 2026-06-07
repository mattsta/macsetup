/*
 * git-log-pager: zero-latency dispatcher for git's `pager.log`.
 *
 * git fires the SAME pager for `git log` (no diff) and `git log -p` (with
 * diffs), and exposes no flag to the pager telling them apart. We want plain
 * logs rendered by `bat` (gitlog syntax highlighting) and patches rendered by
 * `delta`.
 *
 * The previous shell wrapper achieved this by buffering ALL of git's output to
 * a temp file (`cat > tmp`) and grepping it before launching a pager. That
 * destroys git's lazy streaming: git is forced to generate the entire history
 * (e.g. 175MB across 16k commits) before a single line is shown, costing
 * seconds on large repositories.
 *
 * This program instead peeks only a small bounded prefix of stdin to decide,
 * then exec-replaces itself is impossible (we still owe the prefix to the
 * pager), so it forks the chosen pager, replays the peeked prefix, and then
 * streams the rest through verbatim. Crucially it NEVER buffers the whole
 * stream and preserves backpressure: when `less` parks on page 1, the pipe to
 * the pager fills, our writes block, and git blocks too -- exactly as a direct
 * `pager.log = delta` would behave. Time-to-first-page is therefore identical
 * to using delta/bat directly.
 *
 * Decision rule: scan the prefix for a line beginning with "diff --git ". If
 * found -> delta. If the prefix ends (EOF) or the byte budget is exhausted
 * without one -> bat. The budget is generous enough that an ordinary commit
 * message preceding the first diff still routes to delta.
 *
 * Build: cc -O2 -o git-log-pager git-log-pager.c
 * Configure: git config --global pager.log /path/to/git-log-pager
 */

#include <errno.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* Peek up to this many bytes deciding delta-vs-bat. Large enough to clear a
 * long commit message before the first "diff --git", tiny vs full history. */
#define PEEK_MAX (256 * 1024)

#define DELTA_ARGV0 "delta"
#define BAT_ARGV0 "bat"

/* bat arguments for rendering a plain `git log` as gitlog-highlighted text. */
static char *const BAT_ARGS[] = {
    BAT_ARGV0,        "--language=gitlog", "--style=plain",
    "--color=always", "--paging=always",   NULL,
};

static char *const DELTA_ARGS[] = {DELTA_ARGV0, NULL};

/* Read exactly count bytes (or until EOF) into buf. Returns bytes read, or -1
 * on a hard error. Restarts on EINTR. */
static ssize_t read_full(int fd, char *buf, size_t count) {
    size_t total = 0;
    while (total < count) {
        ssize_t n = read(fd, buf + total, count - total);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            break; /* EOF */
        }
        total += (size_t)n;
    }
    return (ssize_t)total;
}

/* Write exactly count bytes from buf to fd. Returns 0 on success, -1 on error.
 * Restarts on EINTR; treats EPIPE as a clean early exit (pager quit). */
static int write_full(int fd, const char *buf, size_t count) {
    size_t total = 0;
    while (total < count) {
        ssize_t n = write(fd, buf + total, count - total);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == EPIPE) {
                return -1; /* downstream pager closed */
            }
            return -1;
        }
        total += (size_t)n;
    }
    return 0;
}

/* Return 1 if the buffer contains a line beginning with "diff --git ". A diff
 * marker is at the start of the buffer or immediately after a newline. */
/* Skip a single ANSI CSI escape sequence (ESC '[' ... final-byte) starting at
 * buf[pos], if present. git colorizes diff output by default when writing to a
 * pager, so a "diff --git" header line is typically emitted as
 * "\x1b[33mdiff --git ...". Returns the index just past the escape, or pos if
 * there is no escape there. */
static size_t skip_csi(const char *buf, size_t len, size_t pos) {
    if (pos + 1 >= len || buf[pos] != '\x1b' || buf[pos + 1] != '[') {
        return pos;
    }
    size_t i = pos + 2;
    /* CSI parameter/intermediate bytes are 0x20-0x3F; final byte is 0x40-0x7E.
     */
    while (i < len && (unsigned char)buf[i] >= 0x20 &&
           (unsigned char)buf[i] <= 0x3F) {
        i++;
    }
    if (i < len && (unsigned char)buf[i] >= 0x40 &&
        (unsigned char)buf[i] <= 0x7E) {
        return i + 1; /* consumed the final byte */
    }
    return pos; /* malformed; don't skip */
}

/* Return 1 if a line in buf begins with "diff --git " (optionally preceded by
 * an ANSI color escape). Anchoring to line-start avoids false positives from
 * the literal text appearing inside a commit message body. */
static int has_diff_marker(const char *buf, size_t len) {
    static const char needle[] = "diff --git ";
    const size_t nlen = sizeof(needle) - 1;
    for (size_t i = 0; i < len; i++) {
        /* A candidate line starts at the buffer start or right after '\n'. */
        if (i != 0 && buf[i - 1] != '\n') {
            continue;
        }
        /* git may stack several color escapes before the literal text; skip
         * every consecutive CSI sequence. */
        size_t start = i;
        for (;;) {
            size_t next = skip_csi(buf, len, start);
            if (next == start) {
                break;
            }
            start = next;
        }
        if (start + nlen <= len && memcmp(buf + start, needle, nlen) == 0) {
            return 1;
        }
    }
    return 0;
}

int main(void) {
    /* We manage EPIPE ourselves via write() return values rather than dying. */
    signal(SIGPIPE, SIG_IGN);

    char *peek = malloc(PEEK_MAX);
    if (peek == NULL) {
        /* Degrade gracefully: just exec delta and let it stream. */
        execvp(DELTA_ARGV0, DELTA_ARGS);
        _exit(127);
    }

    /* Peek a bounded prefix to decide the pager. We stop early as soon as we
     * see a diff marker so the common `git log -p` case decides almost
     * instantly without reading the whole budget. */
    size_t peeked = 0;
    int decided_delta = 0;
    while (peeked < PEEK_MAX) {
        ssize_t n = read(STDIN_FILENO, peek + peeked, PEEK_MAX - peeked);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (n == 0) {
            break; /* EOF: whole output fit in the prefix */
        }
        peeked += (size_t)n;
        if (has_diff_marker(peek, peeked)) {
            decided_delta = 1;
            break;
        }
    }
    if (!decided_delta) {
        decided_delta = has_diff_marker(peek, peeked);
    }

    char *const *argv = decided_delta ? DELTA_ARGS : BAT_ARGS;
    const char *prog = decided_delta ? DELTA_ARGV0 : BAT_ARGV0;

    int pipefd[2];
    if (pipe(pipefd) != 0) {
        _exit(1);
    }

    pid_t pid = fork();
    if (pid < 0) {
        _exit(1);
    }
    if (pid == 0) {
        /* Child: become the pager, reading from the pipe as its stdin. */
        close(pipefd[1]);
        if (dup2(pipefd[0], STDIN_FILENO) < 0) {
            _exit(126);
        }
        close(pipefd[0]);
        signal(SIGPIPE, SIG_DFL);
        execvp(prog, argv);
        _exit(127); /* exec failed (pager not installed) */
    }

    /* Parent: feed the peeked prefix, then stream the remainder verbatim. */
    close(pipefd[0]);

    if (peeked > 0) {
        if (write_full(pipefd[1], peek, peeked) != 0) {
            goto drain;
        }
    }
    free(peek);
    peek = NULL;

    {
        char buf[64 * 1024];
        for (;;) {
            ssize_t n = read_full(STDIN_FILENO, buf, sizeof(buf));
            if (n < 0) {
                break;
            }
            if (n == 0) {
                break; /* EOF */
            }
            if (write_full(pipefd[1], buf, (size_t)n) != 0) {
                break;
            }
        }
    }

drain:
    free(peek);
    close(pipefd[1]); /* signal EOF to the pager */

    int status = 0;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
        /* retry */
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}
