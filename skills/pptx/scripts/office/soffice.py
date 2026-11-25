"""
Helper for running LibreOffice (soffice) in environments where AF_UNIX
sockets may be blocked (e.g., sandboxed VMs).  Detects the restriction
at runtime and applies an LD_PRELOAD shim if needed.

Usage:
    from office.soffice import run_soffice

    result = run_soffice(["--headless", "--convert-to", "pdf", "input.docx"])

Call soffice through run_soffice, not through subprocess with get_soffice_env():
the env dict carries the shim but names no user profile, and a non-root sandbox
cannot bootstrap the default one -- soffice aborts with "User installation could
not be completed" and converts nothing. get_soffice_env() stays public for the
callers that build their own argv (they must pass -env:UserInstallation too).
run_soffice always uses its own fresh profile; callers that need a specific
profile must build their own argv.
"""

import os
import socket
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path


def get_soffice_env() -> dict:
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"

    if _needs_shim():
        shim = _ensure_shim()
        env["LD_PRELOAD"] = str(shim)

    return env


def run_soffice(args: Iterable[str], **kwargs) -> subprocess.CompletedProcess:
    """Run soffice with args in a fresh, empty user profile.

    Caller-supplied bootstrap assignments (-env:NAME=VALUE or /env:NAME=VALUE,
    including UserInstallation) are dropped: the profile is always a new
    temporary directory. Raises ValueError for an unsupported option, which
    includes a document path starting with "-" (pass it as ./-name instead).
    """
    args = _checked_args(args)
    with tempfile.TemporaryDirectory(prefix="lo_profile_", ignore_cleanup_errors=True) as profile:
        args = [f"-env:UserInstallation={Path(profile).as_uri()}"] + args
        return subprocess.run(["soffice"] + args, env=get_soffice_env(), **kwargs)


_FLAG_OPTIONS = frozenset({
    "headless",
    "invisible",
    "minimized",
    "norestore",
    "nologo",
    "nodefault",
    "nolockcheck",
    "nofirststartwizard",
    "terminate_after_init",
    "cat",
    "print-to-file",
})
_VALUE_OPTIONS = frozenset({"convert-to", "outdir", "convert-images-to", "printer-name"})
_PREFIX_OPTIONS = ("infilter=",)
_DOCUMENT_ACTIONS = frozenset({"convert-to", "print-to-file", "cat"})


def _is_bootstrap_assignment(arg: str) -> bool:
    # LibreOffice reads these from any argv position, and the first one for a
    # name wins (sal/rtl/bootstrap.cxx getFromCommandLineArgs).
    return arg[:1] in ("-", "/") and arg[1:5] == "env:" and "=" in arg


def _checked_args(args: Iterable[str]) -> list[str]:
    args = [str(a) for a in args]
    args = [a for a in args if not _is_bootstrap_assignment(a)]

    documents = 0
    needs_document = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            name = arg[2:] if arg.startswith("--") else arg[1:]
            if name in _VALUE_OPTIONS:
                if i + 1 >= len(args):
                    raise ValueError(f"soffice option {arg} needs a value")
                needs_document |= name in _DOCUMENT_ACTIONS
                i += 2
                continue
            if name not in _FLAG_OPTIONS and not name.startswith(_PREFIX_OPTIONS):
                raise ValueError(
                    f"unsupported soffice option {arg!r}; "
                    "a document path starting with '-' must be passed as ./" + arg
                )
            needs_document |= name in _DOCUMENT_ACTIONS
        elif arg:
            documents += 1
        i += 1

    if needs_document and not documents:
        raise ValueError("no document given to soffice")
    return args



_SHIM_SO = Path(tempfile.gettempdir()) / "lo_socket_shim.so"


def _needs_shim() -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.close()
        return False
    except OSError:
        return True


def _ensure_shim() -> Path:
    if _SHIM_SO.exists():
        return _SHIM_SO

    src = Path(tempfile.gettempdir()) / "lo_socket_shim.c"
    src.write_text(_SHIM_SOURCE)
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-o", str(_SHIM_SO), str(src), "-ldl"],
        check=True,
        capture_output=True,
    )
    src.unlink()
    return _SHIM_SO



_SHIM_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

static int (*real_socket)(int, int, int);
static int (*real_socketpair)(int, int, int, int[2]);
static int (*real_listen)(int, int);
static int (*real_accept)(int, struct sockaddr *, socklen_t *);
static int (*real_close)(int);
static int (*real_read)(int, void *, size_t);

/* Per-FD bookkeeping (FDs >= 1024 are passed through unshimmed). */
static int is_shimmed[1024];
static int peer_of[1024];
static int wake_r[1024];            /* accept() blocks reading this */
static int wake_w[1024];            /* close()  writes to this      */
static int listener_fd = -1;        /* FD that received listen()    */

__attribute__((constructor))
static void init(void) {
    real_socket     = dlsym(RTLD_NEXT, "socket");
    real_socketpair = dlsym(RTLD_NEXT, "socketpair");
    real_listen     = dlsym(RTLD_NEXT, "listen");
    real_accept     = dlsym(RTLD_NEXT, "accept");
    real_close      = dlsym(RTLD_NEXT, "close");
    real_read       = dlsym(RTLD_NEXT, "read");
    for (int i = 0; i < 1024; i++) {
        peer_of[i] = -1;
        wake_r[i]  = -1;
        wake_w[i]  = -1;
    }
}

/* ---- socket ---------------------------------------------------------- */
int socket(int domain, int type, int protocol) {
    if (domain == AF_UNIX) {
        int fd = real_socket(domain, type, protocol);
        if (fd >= 0) return fd;
        /* socket(AF_UNIX) blocked – fall back to socketpair(). */
        int sv[2];
        if (real_socketpair(domain, type, protocol, sv) == 0) {
            if (sv[0] >= 0 && sv[0] < 1024) {
                is_shimmed[sv[0]] = 1;
                peer_of[sv[0]]    = sv[1];
                int wp[2];
                if (pipe(wp) == 0) {
                    wake_r[sv[0]] = wp[0];
                    wake_w[sv[0]] = wp[1];
                }
            }
            return sv[0];
        }
        errno = EPERM;
        return -1;
    }
    return real_socket(domain, type, protocol);
}

/* ---- listen ---------------------------------------------------------- */
int listen(int sockfd, int backlog) {
    if (sockfd >= 0 && sockfd < 1024 && is_shimmed[sockfd]) {
        listener_fd = sockfd;
        return 0;
    }
    return real_listen(sockfd, backlog);
}

/* ---- accept ---------------------------------------------------------- */
int accept(int sockfd, struct sockaddr *addr, socklen_t *addrlen) {
    if (sockfd >= 0 && sockfd < 1024 && is_shimmed[sockfd]) {
        /* Block until close() writes to the wake pipe. */
        if (wake_r[sockfd] >= 0) {
            char buf;
            real_read(wake_r[sockfd], &buf, 1);
        }
        errno = ECONNABORTED;
        return -1;
    }
    return real_accept(sockfd, addr, addrlen);
}

/* ---- close ----------------------------------------------------------- */
int close(int fd) {
    if (fd >= 0 && fd < 1024 && is_shimmed[fd]) {
        int was_listener = (fd == listener_fd);
        is_shimmed[fd] = 0;

        if (wake_w[fd] >= 0) {              /* unblock accept() */
            char c = 0;
            write(wake_w[fd], &c, 1);
            real_close(wake_w[fd]);
            wake_w[fd] = -1;
        }
        if (wake_r[fd] >= 0) { real_close(wake_r[fd]); wake_r[fd]  = -1; }
        if (peer_of[fd] >= 0) { real_close(peer_of[fd]); peer_of[fd] = -1; }

        if (was_listener)
            _exit(0);                        /* conversion done – exit */
    }
    return real_close(fd);
}
"""



if __name__ == "__main__":
    import sys
    try:
        result = run_soffice(sys.argv[1:])
    except ValueError as e:
        print(f"soffice.py: {e}", file=sys.stderr)
        sys.exit(2)
    sys.exit(result.returncode)
