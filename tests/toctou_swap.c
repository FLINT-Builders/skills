/* Test harness: after the first name lookup of a real directory called
   "unpacked", replace that directory with a symbolic link. A later lookup
   of the same path then sees the link. The archive tools themselves are
   not implemented here. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static int (*real_stat64)(const char *, struct stat64 *);
static int (*real_lstat64)(const char *, struct stat64 *);
static int (*real_fstatat64)(int, const char *, struct stat64 *, int);
static int swapped;

__attribute__((constructor)) static void init_hooks(void) {
    real_stat64 = dlsym(RTLD_NEXT, "stat64");
    real_lstat64 = dlsym(RTLD_NEXT, "lstat64");
    real_fstatat64 = dlsym(RTLD_NEXT, "fstatat64");
}

static int is_unpacked(const char *path) {
    const char *work = getenv("PPTX_UNPACK_TOCTOU_WORKDIR");
    char buf[PATH_MAX];
    char cwd[PATH_MAX];
    char expected[PATH_MAX];
    size_t n;
    const char *base;
    ssize_t got;

    if (!work || !path || !path[0] || strlen(path) >= sizeof buf)
        return 0;
    memcpy(buf, path, strlen(path) + 1);
    n = strlen(buf);
    while (n > 1 && buf[n - 1] == '/')
        buf[--n] = '\0';
    base = strrchr(buf, '/');
    base = base ? base + 1 : buf;
    if (strcmp(base, "unpacked") != 0)
        return 0;
    if (snprintf(expected, sizeof expected, "%s/unpacked", work) >= (int)sizeof expected)
        return 0;
    if (strcmp(buf, expected) == 0)
        return 1;
    if (buf[0] == '/')
        return 0;
    if (strcmp(buf, "unpacked") != 0 && strcmp(buf, "./unpacked") != 0)
        return 0;
    got = readlink("/proc/self/cwd", cwd, sizeof cwd - 1);
    if (got < 0)
        return 0;
    cwd[got] = '\0';
    return strcmp(cwd, work) == 0;
}

static void swap_unpacked(void) {
    const char *work = getenv("PPTX_UNPACK_TOCTOU_WORKDIR");
    const char *target = getenv("PPTX_UNPACK_TOCTOU_TARGET");
    char unpacked[PATH_MAX];
    char held[PATH_MAX];
    char marker[PATH_MAX];
    int fd;

    if (swapped || !work || !target)
        return;
    if (snprintf(unpacked, sizeof unpacked, "%s/unpacked", work) >= (int)sizeof unpacked)
        return;
    if (snprintf(held, sizeof held, "%s/unpacked.held", work) >= (int)sizeof held)
        return;
    if (snprintf(marker, sizeof marker, "%s/.toctou-swapped", work) >= (int)sizeof marker)
        return;
    swapped = 1;
    if (syscall(SYS_rename, unpacked, held) != 0)
        return;
    if (syscall(SYS_symlink, target, unpacked) != 0) {
        syscall(SYS_rename, held, unpacked);
        return;
    }
    fd = syscall(SYS_open, marker, O_CREAT | O_WRONLY | O_TRUNC, 0644);
    if (fd >= 0)
        syscall(SYS_close, fd);
}

static void consider(const char *path, const struct stat64 *st) {
    if (swapped || !S_ISDIR(st->st_mode) || S_ISLNK(st->st_mode))
        return;
    if (!is_unpacked(path))
        return;
    swap_unpacked();
}

int stat64(const char *path, struct stat64 *buf) {
    int rc = real_stat64(path, buf);
    if (rc == 0)
        consider(path, buf);
    return rc;
}

int lstat64(const char *path, struct stat64 *buf) {
    int rc = real_lstat64(path, buf);
    if (rc == 0)
        consider(path, buf);
    return rc;
}

int fstatat64(int dirfd, const char *path, struct stat64 *buf, int flags) {
    int rc = real_fstatat64(dirfd, path, buf, flags);
    if (rc == 0 && path && path[0] != '\0' && dirfd == AT_FDCWD)
        consider(path, buf);
    return rc;
}
