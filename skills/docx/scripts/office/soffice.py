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

For a PDF that will be rasterized for preview, use export_pdf (CLI:
`soffice.py --pdf input.pptx out.pdf`). It refuses documents with more than
DEFAULT_MAX_PDF_PAGES pages before any PDF is written, and never exports more.
"""

import contextlib
import os
import socket
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_MAX_PDF_PAGES = 40


class PageLimitError(RuntimeError):
    pass


def get_soffice_env() -> dict:
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"

    if _needs_shim():
        shim = _ensure_shim()
        env["LD_PRELOAD"] = str(shim)

    return env


def run_soffice(args: Iterable[str], **kwargs) -> subprocess.CompletedProcess:
    args = list(args)
    with contextlib.ExitStack() as stack:
        if not any(str(a).startswith("-env:UserInstallation") for a in args):
            profile = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="lo_profile_", ignore_cleanup_errors=True)
            )
            args = [f"-env:UserInstallation={Path(profile).as_uri()}"] + args
        return subprocess.run(["soffice"] + args, env=get_soffice_env(), **kwargs)


def export_pdf(
    source,
    target,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
    timeout: float = 300,
) -> int:
    """Export a text or presentation document to PDF; return its page count.

    The page count is read from the document LibreOffice loaded, and that same
    loaded document is exported, so the file cannot change between the check
    and the export. Raises PageLimitError, without writing target, when the
    document has more than max_pages pages. The export itself is restricted to
    pages 1..max_pages.
    """
    source_path = Path(source).resolve(strict=True)
    target_path = Path(target).absolute()
    if target_path.suffix.lower() != ".pdf":
        raise ValueError(f"PDF target must end in .pdf: {target}")
    if not target_path.parent.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {target_path.parent}")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    env = get_soffice_env()
    with tempfile.TemporaryDirectory(
        prefix="lo_profile_", ignore_cleanup_errors=True
    ) as profile_dir:
        profile = Path(profile_dir)
        profile_arg = f"-env:UserInstallation={profile.as_uri()}"
        try:
            subprocess.run(
                ["soffice", profile_arg, "--headless", "--norestore", "--terminate_after_init"],
                env=env,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            library = profile / "user" / "basic" / "Standard"
            if not library.is_dir():
                raise RuntimeError("LibreOffice did not create its user profile")
            (library / f"{_EXPORT_MODULE}.xba").write_text(_export_module_xml(), encoding="utf-8")
            (library / "script.xlb").write_text(_EXPORT_LIBRARY_XML, encoding="utf-8")

            status = profile / "export-status.txt"
            env.update(
                SKILLS_PDF_SOURCE=str(source_path),
                SKILLS_PDF_TARGET=str(target_path),
                SKILLS_PDF_MAX_PAGES=str(max_pages),
                SKILLS_PDF_STATUS=str(status),
            )
            result = subprocess.run(
                [
                    "soffice",
                    profile_arg,
                    "--headless",
                    "--invisible",
                    "--norestore",
                    f"macro:///Standard.{_EXPORT_MODULE}.Run",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"PDF export timed out after {timeout:g}s") from None
        outcome = status.read_text(encoding="utf-8").strip() if status.exists() else ""

    kind, _, detail = outcome.partition(" ")
    if kind == "OK" and target_path.exists():
        return int(detail)
    if kind == "LIMIT":
        raise PageLimitError(
            f"{source_path.name} has {detail} pages; PDF export for preview is "
            f"limited to {max_pages}"
        )
    if kind == "UNSUPPORTED":
        raise RuntimeError(f"{source_path.name} is not a text or presentation document")
    detail = detail or (result.stderr or "").strip() or "no result from LibreOffice"
    raise RuntimeError(f"PDF export failed: {detail}")


_EXPORT_MODULE = "SkillsPdfExport"

# Runs inside soffice: load once, count pages on the loaded model, export it.
_EXPORT_BASIC = """
Sub Run
    Dim sResult As String
    Dim oDoc As Object
    Dim sFilter As String
    Dim nPages As Long
    Dim nMax As Long
    On Error GoTo Failed
    nMax = CLng(Environ("SKILLS_PDF_MAX_PAGES"))
    Dim aLoad(3) As New com.sun.star.beans.PropertyValue
    aLoad(0).Name = "Hidden" : aLoad(0).Value = True
    aLoad(1).Name = "ReadOnly" : aLoad(1).Value = True
    aLoad(2).Name = "MacroExecutionMode"
    aLoad(2).Value = com.sun.star.document.MacroExecMode.NEVER_EXECUTE
    aLoad(3).Name = "UpdateDocMode"
    aLoad(3).Value = com.sun.star.document.UpdateDocMode.NO_UPDATE
    oDoc = StarDesktop.loadComponentFromURL( _
        ConvertToURL(Environ("SKILLS_PDF_SOURCE")), "_blank", 0, aLoad())
    If IsNull(oDoc) Then
        sResult = "ERROR LibreOffice could not open the document"
        GoTo Finish
    End If
    If oDoc.supportsService("com.sun.star.text.TextDocument") Then
        sFilter = "writer_pdf_Export"
        nPages = oDoc.getCurrentController().getPropertyValue("PageCount")
    ElseIf oDoc.supportsService("com.sun.star.presentation.PresentationDocument") Then
        sFilter = "impress_pdf_Export"
        nPages = oDoc.getDrawPages().getCount()
    ElseIf oDoc.supportsService("com.sun.star.drawing.DrawingDocument") Then
        sFilter = "draw_pdf_Export"
        nPages = oDoc.getDrawPages().getCount()
    Else
        sResult = "UNSUPPORTED"
        GoTo Finish
    End If
    If nPages > nMax Then
        sResult = "LIMIT " & nPages
        GoTo Finish
    End If
    Dim aFilterData(0) As New com.sun.star.beans.PropertyValue
    aFilterData(0).Name = "PageRange" : aFilterData(0).Value = "1-" & nMax
    Dim aStore(1) As New com.sun.star.beans.PropertyValue
    aStore(0).Name = "FilterName" : aStore(0).Value = sFilter
    aStore(1).Name = "FilterData" : aStore(1).Value = aFilterData()
    oDoc.storeToURL(ConvertToURL(Environ("SKILLS_PDF_TARGET")), aStore())
    sResult = "OK " & nPages
    GoTo Finish
Failed:
    sResult = "ERROR " & Error$
    Resume Finish
Finish:
    On Error Resume Next
    If Not IsNull(oDoc) Then oDoc.close(True)
    Dim iFile As Integer
    iFile = FreeFile
    Open Environ("SKILLS_PDF_STATUS") For Output As #iFile
    Print #iFile, sResult
    Close #iFile
    StarDesktop.terminate()
End Sub
"""

_EXPORT_LIBRARY_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE library:library PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "library.dtd">
<library:library xmlns:library="http://openoffice.org/2000/library" library:name="Standard" library:readonly="false" library:passwordprotected="false">
 <library:element library:name="{_EXPORT_MODULE}"/>
</library:library>
"""


def _export_module_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">\n'
        '<script:module xmlns:script="http://openoffice.org/2000/script" '
        f'script:name="{_EXPORT_MODULE}" script:language="StarBasic">'
        f"{escape(_EXPORT_BASIC, {chr(34): '&quot;'})}</script:module>\n"
    )



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

    if sys.argv[1:2] == ["--pdf"]:
        if len(sys.argv) != 4:
            print("Usage: soffice.py --pdf INPUT OUTPUT.pdf", file=sys.stderr)
            sys.exit(2)
        try:
            pages = export_pdf(sys.argv[2], sys.argv[3])
        except (OSError, RuntimeError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Wrote {pages} page(s) to {Path(sys.argv[3]).absolute()}")
        sys.exit(0)

    result = run_soffice(sys.argv[1:])
    sys.exit(result.returncode)
