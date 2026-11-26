/*
 * Test harness. On the first open or opendir of TOCTOU_ENTRY that follows
 * links, exchange that directory entry with TOCTOU_SPARE before the real
 * call runs. A path that was a regular file or directory for every earlier
 * stat is a symlink by the time it is used.
 *
 * Opens that pass O_NOFOLLOW do not exchange. The real call then observes
 * the entry that was already there. opendir has no no-follow flag; a caller
 * that does not want this exchange has to open the directory itself with
 * O_NOFOLLOW and iterate that file descriptor.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef RENAME_EXCHANGE
#define RENAME_EXCHANGE (1 << 1)
#endif

static int swapped = 0;

static void swap_once(const char *path) {
    const char *entry;
    const char *spare;
    if (swapped || path == NULL)
        return;
    entry = getenv("TOCTOU_ENTRY");
    spare = getenv("TOCTOU_SPARE");
    if (entry == NULL || spare == NULL || strcmp(path, entry) != 0)
        return;
    if (syscall(SYS_renameat2, AT_FDCWD, entry, AT_FDCWD, spare, RENAME_EXCHANGE) == 0)
        swapped = 1;
}

int open64(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...) = NULL;
    mode_t mode = 0;
    if (!real)
        real = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open64");
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    if ((flags & O_NOFOLLOW) == 0)
        swap_once(path);
    return real(path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int, const char *, int, ...) = NULL;
    mode_t mode = 0;
    if (!real)
        real = (int (*)(int, const char *, int, ...))dlsym(RTLD_NEXT, "openat64");
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    if ((flags & O_NOFOLLOW) == 0)
        swap_once(path);
    return real(dirfd, path, flags, mode);
}

DIR *opendir(const char *name) {
    static DIR *(*real)(const char *) = NULL;
    if (!real)
        real = (DIR *(*)(const char *))dlsym(RTLD_NEXT, "opendir");
    swap_once(name);
    return real(name);
}
