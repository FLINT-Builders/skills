#!/usr/bin/env python3
"""Security regressions for the LibreOffice socket shim.

The pptx, xlsx, and docx wrappers load a shared library from a fixed path in
the shared temp directory when AF_UNIX sockets cannot be created. These tests
plant that path as another local user and also replace it at the exec boundary.

They fail while that planted code still runs, or while preparing the shim
follows a symlink and changes some other file. They pass once those entry
points no longer execute attacker-controlled bytes or touch the planted paths.

Run:
    python3 tests/security/test_lo_socket_shim.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
BUILD = Path(tempfile.mkdtemp(prefix="lo_shim_build_"))
BIN = BUILD / "bin"
BIN.mkdir()
SOFFICE = BIN / "soffice"
EVIL = BUILD / "evil.so"
BENIGN = BUILD / "benign.so"
HOOK = BUILD / "exec_swap.so"

SHIM = Path("/tmp/lo_socket_shim.so")
SHIM_SRC = Path("/tmp/lo_socket_shim.c")
PAYLOAD = Path("/tmp/lo_shim_payload.so")
SWAP_DONE = Path("/tmp/lo_shim_swap_done")
PROFILE = Path("/tmp/libreoffice_docx_profile")

EVIL_C = r"""
#include <stdio.h>
#include <stdlib.h>
__attribute__((constructor)) static void init(void) {
    const char *p = getenv("LO_SHIM_MARKER");
    if (!p) return;
    FILE *f = fopen(p, "a");
    if (!f) return;
    fputs("executed\n", f);
    fclose(f);
}
"""

BENIGN_C = r"""
#include <stdio.h>
#include <stdlib.h>
__attribute__((constructor)) static void init(void) {
    const char *p = getenv("LO_BENIGN_MARKER");
    if (!p) return;
    FILE *f = fopen(p, "a");
    if (!f) return;
    fputs("benign\n", f);
    fclose(f);
}
"""

SOFFICE_C = r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void mkdir_p(const char *path) {
    char buf[4096];
    size_t n = strlen(path);
    if (n == 0 || n >= sizeof buf) return;
    memcpy(buf, path, n + 1);
    for (char *p = buf + 1; *p; ++p) {
        if (*p == '/') {
            *p = 0;
            mkdir(buf, 0755);
            *p = '/';
        }
    }
    mkdir(buf, 0755);
}

int main(int argc, char **argv) {
    const char *ran = getenv("LO_SOFFICE_RAN");
    if (ran) {
        int fd = open(ran, O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (fd >= 0) {
            const char *preload = getenv("LD_PRELOAD");
            if (!preload) preload = "-";
            write(fd, preload, strlen(preload));
            write(fd, "\n", 1);
            close(fd);
        }
    }
    const char *prefix = "-env:UserInstallation=";
    size_t prefix_len = strlen(prefix);
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], prefix, prefix_len) != 0) continue;
        const char *url = argv[i] + prefix_len;
        const char *path = url;
        if (strncmp(url, "file://", 7) == 0) path = url + 7;
        char dir[4096];
        snprintf(dir, sizeof dir, "%s/user/basic/Standard", path);
        mkdir_p(dir);
    }
    return 0;
}
"""

HOOK_C = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/syscall.h>
#include <unistd.h>

static int (*real_execve)(const char *, char *const[], char *const[]);

static int sys_open(const char *path, int flags, int mode) {
    return (int)syscall(SYS_openat, AT_FDCWD, path, flags, mode);
}

__attribute__((constructor)) static void init(void) {
    real_execve = dlsym(RTLD_NEXT, "execve");
}

static void copy_file(const char *src, const char *dst) {
    int in = sys_open(src, O_RDONLY, 0);
    if (in < 0) return;
    int out = sys_open(dst, O_WRONLY | O_TRUNC, 0);
    if (out < 0) {
        syscall(SYS_close, in);
        return;
    }
    char buf[65536];
    for (;;) {
        long n = syscall(SYS_read, in, buf, sizeof buf);
        if (n < 0) {
            syscall(SYS_close, in);
            syscall(SYS_close, out);
            return;
        }
        if (n == 0) break;
        long off = 0;
        while (off < n) {
            long w = syscall(SYS_write, out, buf + off, (size_t)(n - off));
            if (w < 0) {
                syscall(SYS_close, in);
                syscall(SYS_close, out);
                return;
            }
            off += w;
        }
    }
    syscall(SYS_close, in);
    syscall(SYS_close, out);
    int flag = sys_open("/tmp/lo_shim_swap_done", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (flag >= 0) {
        syscall(SYS_write, flag, "1\n", 2);
        syscall(SYS_close, flag);
    }
}

int execve(const char *path, char *const argv[], char *const envp[]) {
    copy_file("/tmp/lo_shim_payload.so", "/tmp/lo_socket_shim.so");
    return real_execve(path, argv, envp);
}
"""


def _cc(output: Path, source: str, extra: list[str] | None = None) -> None:
    src = output.with_suffix(output.suffix + ".c")
    src.write_text(source)
    cmd = ["gcc", "-o", str(output), str(src)]
    if extra:
        cmd[1:1] = extra
    subprocess.run(cmd, check=True, capture_output=True)


def _sudo(*args: str) -> None:
    subprocess.run(["sudo", "-n", *args], check=True, capture_output=True)


def _remove(path: Path) -> None:
    subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(path)], check=False, capture_output=True)


def _compile_fixtures() -> None:
    _cc(EVIL, EVIL_C, ["-shared", "-fPIC"])
    _cc(BENIGN, BENIGN_C, ["-shared", "-fPIC"])
    _cc(HOOK, HOOK_C, ["-shared", "-fPIC", "-ldl"])
    _cc(SOFFICE, SOFFICE_C)
    if not SOFFICE.is_file() or SOFFICE.stat().st_mode & stat.S_IXUSR == 0:
        raise RuntimeError("fake soffice was not built")
    ldd = subprocess.run(["ldd", str(SOFFICE)], check=True, capture_output=True, text=True)
    if "not a dynamic executable" in ldd.stdout:
        raise RuntimeError("fake soffice is static, so LD_PRELOAD would not be tested")


def _clean_shared_names() -> None:
    for path in (SHIM, SHIM_SRC, PAYLOAD, SWAP_DONE, PROFILE):
        _remove(path)


class LoShimTests(unittest.TestCase):
    EXEC_CASES = (
        ("run_soffice", "pptx", 1),
        ("run_soffice", "xlsx", 1),
        ("run_soffice", "docx", 1),
        ("cli", "pptx", 1),
        ("cli", "xlsx", 1),
        ("cli", "docx", 1),
        ("thumbnail", "pptx", 1),
        ("recalc", "xlsx", 2),
        ("accept_changes", "docx", 2),
    )

    def setUp(self) -> None:
        _clean_shared_names()
        previous = getattr(self, "work", None)
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
        self.work = Path(tempfile.mkdtemp(prefix="lo_shim_case_"))
        self.marker = self.work / "marker"
        self.ran = self.work / "soffice_ran"
        self.status = self.work / "status.json"

    def tearDown(self) -> None:
        _clean_shared_names()
        shutil.rmtree(self.work, ignore_errors=True)

    def _run_entry(self, case: str, skill: str, *, swap: bool) -> dict:
        env = os.environ.copy()
        for key in ("TMPDIR", "TEMP", "TMP", "LD_PRELOAD", "LO_BENIGN_MARKER"):
            env.pop(key, None)
        env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
        env["LO_SHIM_CHILD"] = "1"
        env["LO_SHIM_CASE"] = case
        env["LO_SHIM_SKILL"] = skill
        env["LO_SHIM_MARKER"] = str(self.marker)
        env["LO_SOFFICE_RAN"] = str(self.ran)
        env["LO_SHIM_STATUS"] = str(self.status)
        env["LO_SHIM_WORK"] = str(self.work)
        if swap:
            env["LD_PRELOAD"] = str(HOOK)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        status = {}
        if self.status.is_file():
            status = json.loads(self.status.read_text())
        status["returncode"] = proc.returncode
        status["stdout"] = proc.stdout
        status["stderr"] = proc.stderr
        status["marker_hits"] = (
            self.marker.read_text().count("executed") if self.marker.is_file() else 0
        )
        status["soffice_runs"] = (
            len(self.ran.read_text().splitlines()) if self.ran.is_file() else 0
        )
        status["preloads"] = (
            self.ran.read_text().splitlines() if self.ran.is_file() else []
        )
        status["swap_done"] = SWAP_DONE.is_file()
        return status

    def _assert_child_ok(self, status: dict) -> None:
        self.assertEqual(
            status["returncode"],
            0,
            "entry point failed before the assertion\n"
            f"stdout:\n{status['stdout']}\nstderr:\n{status['stderr']}",
        )
        self.assertTrue(status.get("unix_blocked"), "AF_UNIX was not blocked in the child")
        self.assertTrue(status.get("invoked"), "product entry point was not called")

    def _assert_attacker_did_not_run(self, status: dict, min_runs: int, *, swap: bool) -> None:
        self._assert_child_ok(status)
        self.assertEqual(
            status["marker_hits"],
            0,
            "attacker constructor ran in the process started with the wrapper environment\n"
            f"LD_PRELOAD seen by soffice: {status['preloads']}",
        )
        if status["soffice_runs"]:
            self.assertGreaterEqual(
                status["soffice_runs"],
                min_runs,
                f"soffice was started {status['soffice_runs']} time(s); "
                f"this entry point reaches it {min_runs} time(s) "
                f"(preloads={status['preloads']})",
            )
        if swap and status["soffice_runs"]:
            self.assertTrue(
                status["swap_done"],
                "the exec-time replacement did not run, so the check-then-use window was not tested",
            )

    def _plant_regular(self) -> None:
        _sudo("install", "-o", "nobody", "-g", "nogroup", "-m", "0644", str(EVIL), str(SHIM))
        st = SHIM.lstat()
        self.assertNotEqual(st.st_uid, os.getuid())
        self.assertFalse(stat.S_ISLNK(st.st_mode))

    def _plant_benign_for_swap(self) -> None:
        shutil.copyfile(BENIGN, SHIM)
        os.chmod(SHIM, 0o644)
        shutil.copyfile(EVIL, PAYLOAD)
        st = SHIM.stat()
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(st.st_mode) & 0o022, 0)
        self.assertFalse(SHIM.is_symlink())

    def test_other_user_library_is_not_executed(self) -> None:
        for case, skill, min_runs in self.EXEC_CASES:
            with self.subTest(case=case, skill=skill):
                self.setUp()
                try:
                    self._plant_regular()
                    status = self._run_entry(case, skill, swap=False)
                    self._assert_attacker_did_not_run(status, min_runs, swap=False)
                finally:
                    self.tearDown()

    def test_library_replaced_at_exec_is_not_executed(self) -> None:
        for case, skill, min_runs in self.EXEC_CASES:
            with self.subTest(case=case, skill=skill):
                self.setUp()
                try:
                    self._plant_benign_for_swap()
                    status = self._run_entry(case, skill, swap=True)
                    self._assert_attacker_did_not_run(status, min_runs, swap=True)
                finally:
                    self.tearDown()


def _install_seccomp() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)

    class SockFilter(ctypes.Structure):
        _fields_ = (
            ("code", ctypes.c_uint16),
            ("jt", ctypes.c_uint8),
            ("jf", ctypes.c_uint8),
            ("k", ctypes.c_uint32),
        )

    class SockFprog(ctypes.Structure):
        _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter)))

    bpf_ld = 0x00
    bpf_w = 0x00
    bpf_abs = 0x20
    bpf_jmp = 0x05
    bpf_jeq = 0x10
    bpf_k = 0x00
    bpf_ret = 0x06
    audit_arch_x86_64 = 0xC000003E
    sys_socket = 41
    af_unix = 1
    seccomp_ret_kill = 0x80000000
    seccomp_ret_errno = 0x00050000
    seccomp_ret_allow = 0x7FFF0000
    eperm = 1

    def stmt(code: int, k: int) -> SockFilter:
        return SockFilter(code, 0, 0, k)

    def jump(code: int, k: int, jt: int, jf: int) -> SockFilter:
        return SockFilter(code, jt, jf, k)

    filters = (SockFilter * 9)(
        stmt(bpf_ld | bpf_w | bpf_abs, 4),
        jump(bpf_jmp | bpf_jeq | bpf_k, audit_arch_x86_64, 1, 0),
        stmt(bpf_ret | bpf_k, seccomp_ret_kill),
        stmt(bpf_ld | bpf_w | bpf_abs, 0),
        jump(bpf_jmp | bpf_jeq | bpf_k, sys_socket, 0, 3),
        stmt(bpf_ld | bpf_w | bpf_abs, 16),
        jump(bpf_jmp | bpf_jeq | bpf_k, af_unix, 0, 1),
        stmt(bpf_ret | bpf_k, seccomp_ret_errno | eperm),
        stmt(bpf_ret | bpf_k, seccomp_ret_allow),
    )
    prog = SockFprog(len(filters), filters)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(22, 2, ctypes.addressof(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP failed")
    # Keep the filter buffer alive for the rest of the process.
    global _SECCOMP_KEEPALIVE
    _SECCOMP_KEEPALIVE = (filters, prog)


def _unix_blocked() -> bool:
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return True
    probe.close()
    return False


def _prepare_imports(case: str, skill: str) -> None:
    scripts = REPO / "skills" / skill / "scripts"
    sys.path.insert(0, str(scripts))
    os.environ.pop("TMPDIR", None)
    os.environ.pop("TEMP", None)
    os.environ.pop("TMP", None)
    import tempfile as tempfile_mod

    tempfile_mod.tempdir = None
    if case == "thumbnail":
        import thumbnail

        sys.modules["__lo_thumbnail__"] = thumbnail
    elif case == "recalc":
        import recalc

        sys.modules["__lo_recalc__"] = recalc
    elif case == "accept_changes":
        import accept_changes

        sys.modules["__lo_accept__"] = accept_changes
    import office.soffice as soffice

    sys.modules["__lo_soffice__"] = soffice


def _invoke(case: str, skill: str) -> None:
    if case == "run_soffice":
        sys.modules["__lo_soffice__"].run_soffice(
            ["--headless", "--convert-to", "pdf", "input.pptx"],
            capture_output=True,
        )
        return
    if case == "cli":
        import runpy

        script = REPO / "skills" / skill / "scripts" / "office" / "soffice.py"
        sys.argv = [str(script), "--headless", "--convert-to", "pdf", "input.pptx"]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise
        return
    if case == "thumbnail":
        thumb = sys.modules["__lo_thumbnail__"]
        try:
            thumb.convert_to_images(Path("deck.pptx"), Path(os.environ["LO_SHIM_WORK"]))
        except RuntimeError:
            return
        return
    if case == "recalc":
        from openpyxl import Workbook

        book = Path(os.environ["LO_SHIM_WORK"]) / "book.xlsx"
        wb = Workbook()
        wb.active["A1"] = "=1+1"
        wb.save(book)
        sys.modules["__lo_recalc__"].recalc(str(book), timeout=10, force=True)
        return
    if case == "accept_changes":
        src = Path(os.environ["LO_SHIM_WORK"]) / "in.docx"
        dst = Path(os.environ["LO_SHIM_WORK"]) / "out.docx"
        src.write_bytes(b"PK\x03\x04not-a-real-zip")
        sys.modules["__lo_accept__"].accept_changes(str(src), str(dst))
        return
    raise AssertionError(f"unknown case {case}")


def child_main() -> int:
    case = os.environ["LO_SHIM_CASE"]
    skill = os.environ["LO_SHIM_SKILL"]
    status_path = Path(os.environ["LO_SHIM_STATUS"])
    status = {"unix_blocked": False, "invoked": False, "error": None}
    try:
        _prepare_imports(case, skill)
        _install_seccomp()
        status["unix_blocked"] = _unix_blocked()
        if not status["unix_blocked"]:
            raise RuntimeError("seccomp did not block AF_UNIX")
        status_path.write_text(json.dumps(status))
        _invoke(case, skill)
        status["invoked"] = True
        status_path.write_text(json.dumps(status))
        return 0
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
        status_path.write_text(json.dumps(status))
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    if os.environ.get("LO_SHIM_CHILD") == "1":
        sys.exit(child_main())
    _compile_fixtures()
    try:
        unittest.main(verbosity=2)
    finally:
        _clean_shared_names()
        shutil.rmtree(BUILD, ignore_errors=True)
