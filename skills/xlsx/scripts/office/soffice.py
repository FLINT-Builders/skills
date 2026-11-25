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
callers that build their own argv (they must pass -env:UserInstallation too, and
call harden_profile() on that profile before soffice opens a document).

Documents handled here come from other people. A picture, OLE object or field
can point at an http URL or a local path, and soffice would fetch it while
loading -- reaching the internal network, or putting a local file onto the
rendered page. run_soffice() therefore always runs with a hardened profile that
blocks every link from a document and has no network content providers, opens
only a private copy of each document, strips external relationships from OOXML
inputs, and refuses formats other than OOXML and legacy .doc/.xls/.ppt.
"""

import contextlib
import io
import os
import posixpath
import re
import socket
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree


def get_soffice_env() -> dict:
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"

    if _needs_shim():
        shim = _ensure_shim()
        env["LD_PRELOAD"] = str(shim)

    return env


def run_soffice(args: Iterable[str], **kwargs) -> subprocess.CompletedProcess:
    args = [str(a) for a in args]
    with contextlib.ExitStack() as stack:
        profile_args = [a for a in args if a.startswith("-env:UserInstallation=")]
        if profile_args:
            for arg in profile_args:
                harden_profile(profile_path(arg.split("=", 1)[1]))
        else:
            profile = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="lo_profile_", ignore_cleanup_errors=True)
            )
            harden_profile(Path(profile))
            args = [f"-env:UserInstallation={Path(profile).as_uri()}"] + args
        inputs = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="lo_input_", ignore_cleanup_errors=True)
        )
        args = _sanitized_inputs(args, Path(inputs))
        return subprocess.run(["soffice"] + args, env=get_soffice_env(), **kwargs)


# BlockUntrustedRefererLinks makes soffice refuse every link whose referer is a
# document outside SecureURL; SecureURL is emptied so no location is trusted.
# DisableActiveContent stops OLE links and DDE. The Update settings stop Writer
# and Calc refreshing links and fields from their sources while loading.
_HARDENED_SETTINGS = (
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableActiveContent", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "SecureURL", ""),
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "0"),  # sw UpdateLinks::NEVER
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "1"),  # sc ScLkUpdMode::LM_NEVER
)

# The UCB resolves every URL soffice opens. Only these local schemes stay
# registered, so http(s), WebDAV, CMIS and any GIO/SMB provider the install
# adds cannot be reached from a document.
_UCB_PATH = "/org.openoffice.ucb.Configuration/ContentProviders"
_LOCAL_CONTENT_PROVIDERS = (
    ("com.sun.star.ucb.HierarchyContentProvider", "vnd.sun.star.hier"),
    ("com.sun.star.ucb.FileContentProvider", "file"),
    ("com.sun.star.help.XMLHelp", "vnd.sun.star.help"),
    ("com.sun.star.ucb.PackageContentProvider", "vnd.sun.star.pkg"),
    ("com.sun.star.ucb.ExpandContentProvider", "vnd.sun.star.expand"),
    ("com.sun.star.ucb.TransientDocumentsContentProvider", "vnd.sun.star.tdoc"),
    ("com.sun.star.ucb.PackageContentProvider", "vnd.sun.star.zip"),
    ("com.sun.star.ucb.ExtensionContentProvider", "vnd.sun.star.extension"),
    ("com.sun.star.ucb.ImageContentProvider", "vnd.libreoffice.image"),
)

_XCU_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
    'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)
_XCU_ITEM = re.compile(r'<item oor:path="([^"]*)">(.*?)</item>\n?', re.S)


def profile_path(user_installation: str) -> Path:
    url = urlparse(user_installation)
    if url.scheme != "file" or url.netloc not in ("", "localhost"):
        raise ValueError(f"UserInstallation must be a local file: URL, got {user_installation!r}")
    return Path(unquote(url.path))


def harden_profile(profile: Path) -> None:
    """Write the settings that stop soffice following document links into profile."""
    xcu = profile / "user" / "registrymodifications.xcu"
    xcu.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = xcu.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if "</oor:items>" not in text:
        text = _XCU_HEADER + "</oor:items>\n"

    def managed(item: re.Match) -> bool:
        path, body = item.group(1), item.group(2)
        return path.startswith(_UCB_PATH) or any(
            path == p and f'<prop oor:name="{name}"' in body for p, name, _ in _HARDENED_SETTINGS
        )

    text = _XCU_ITEM.sub(lambda item: "" if managed(item) else item.group(0), text)

    items = [
        f'<item oor:path="{path}"><prop oor:name="{name}" oor:op="fuse">'
        f"<value>{value}</value></prop></item>\n"
        for path, name, value in _HARDENED_SETTINGS
    ]
    providers = "".join(
        f'<node oor:name="Provider{index}" oor:op="replace">'
        f'<prop oor:name="ServiceName"><value>{service}</value></prop>'
        f'<prop oor:name="URLTemplate"><value>{scheme}</value></prop>'
        f'<prop oor:name="Arguments"><value/></prop></node>'
        for index, (service, scheme) in enumerate(_LOCAL_CONTENT_PROVIDERS, 1)
    )
    items.append(
        f'<item oor:path="{_UCB_PATH}"><node oor:name="Local" oor:op="replace">'
        f'<node oor:name="SecondaryKeys"><node oor:name="Office" oor:op="replace">'
        f'<node oor:name="ProviderData">{providers}</node></node></node></node></item>\n'
    )
    head, tail = text.rsplit("</oor:items>", 1)
    xcu.write_text(head + "".join(items) + "</oor:items>" + tail, encoding="utf-8")


_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_VML_RELID = "{urn:schemas-microsoft-com:office:office}relid"
_HYPERLINK_ELEMENTS = {"hyperlink", "hlinkClick", "hlinkHover", "hlinkMouseOver"}
_WEB_SCHEMES = ("http", "https", "mailto")
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_NESTING = 3

_OOXML_FAMILIES = (
    (("officedocument.wordprocessingml.", "vnd.ms-word."), "MS Word 2007 XML", ".docx"),
    (("officedocument.spreadsheetml.", "vnd.ms-excel."), "Calc MS Excel 2007 XML", ".xlsx"),
    (("officedocument.presentationml.", "vnd.ms-powerpoint."), "Impress MS PowerPoint 2007 XML", ".pptx"),
)
# A template loads as an untitled document, which skips the referer check
# above, so template main parts are rewritten to the matching document type.
_TEMPLATE_TO_DOCUMENT = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.ms-word.template.macroenabledtemplate.main+xml":
        "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "application/vnd.ms-excel.template.macroenabled.main+xml":
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    "application/vnd.ms-powerpoint.template.macroenabled.main+xml":
        "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
}
_LEGACY_FILTERS = {
    ".doc": ("MS Word 97", ".doc"),
    ".dot": ("MS Word 97", ".doc"),
    ".xls": ("MS Excel 97", ".xls"),
    ".xlt": ("MS Excel 97", ".xls"),
    ".ppt": ("MS PowerPoint 97", ".ppt"),
    ".pot": ("MS PowerPoint 97", ".ppt"),
    ".pps": ("MS PowerPoint 97", ".ppt"),
}

_VALUE_OPTIONS = {"convert-to", "outdir", "printer-name", "pt", "display", "convert-images-to"}
_DOCUMENT_MODES = {"convert-to", "print-to-file", "p", "pt", "cat"}


def sanitize_ooxml(source, destination: Path, keep_external: Iterable[str] = ()) -> tuple[str, str]:
    """Copy the OOXML package source to destination without anything soffice could follow.

    Drops every external relationship except hyperlinks that only hyperlink
    elements use (or that point at a web or mail address), plus any whose type
    contains one of keep_external. Drops altChunk parts that are not OOXML,
    sanitizes nested OOXML parts, and turns templates into documents.
    Returns (import filter, file extension) for the sanitized package.
    """
    keep = tuple(k.lower() for k in keep_external)
    with zipfile.ZipFile(source) as package:
        data, family = _sanitize_package(package, keep, 0)
    destination.write_bytes(data)
    _, import_filter, extension = family
    return import_filter, extension


def _sanitize_package(package: zipfile.ZipFile, keep: tuple, depth: int) -> tuple[bytes, tuple]:
    infos = package.infolist()
    names = {}
    for info in infos:
        key = info.filename.lower()
        if key in names:
            raise ValueError(f"package lists {info.filename!r} more than once")
        names[key] = info

    root_rels = names.get("_rels/.rels")
    content_types = names.get("[content_types].xml")
    if root_rels is None or content_types is None:
        raise ValueError("not an Office Open XML package")

    main_part = None
    for rel in ElementTree.fromstring(package.read(root_rels)).iter(f"{{{_REL_NS}}}Relationship"):
        if rel.get("Type", "").endswith("/officeDocument") and not _is_external(rel):
            main_part = "/" + rel.get("Target", "").lstrip("/")
    if main_part is None:
        raise ValueError("package has no main document part")

    types = ElementTree.fromstring(package.read(content_types))
    family = None
    for override in types.iter(f"{{{_CT_NS}}}Override"):
        if override.get("PartName", "").lower() != main_part.lower():
            continue
        content_type = override.get("ContentType", "").lower()
        family = next((f for f in _OOXML_FAMILIES if any(m in content_type for m in f[0])), None)
        if family is None or ".binary." in content_type:
            raise ValueError(f"unsupported main part type {content_type!r}")
        if content_type in _TEMPLATE_TO_DOCUMENT:
            override.set("ContentType", _TEMPLATE_TO_DOCUMENT[content_type])
    if family is None:
        raise ValueError("package does not declare its main document part")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as sanitized:
        for info in infos:
            body = package.read(info)
            if info is content_types:
                body = _serialize(types, _CT_NS)
            elif info.filename.lower().endswith(".rels"):
                body = _sanitize_relationships(package, names, info.filename, body, keep)
            elif body[:4] == b"PK\x03\x04" and _is_ooxml(body):
                if depth >= _MAX_NESTING:
                    raise ValueError("Office Open XML packages are nested too deeply")
                with zipfile.ZipFile(io.BytesIO(body)) as nested:
                    body, _ = _sanitize_package(nested, keep, depth + 1)
            entry = zipfile.ZipInfo(info.filename, info.date_time)
            entry.compress_type = (
                zipfile.ZIP_STORED if info.compress_type == zipfile.ZIP_STORED else zipfile.ZIP_DEFLATED
            )
            sanitized.writestr(entry, body)
    return output.getvalue(), family


def _sanitize_relationships(package, names, rels_name, body, keep) -> bytes:
    rels = ElementTree.fromstring(body)
    rels_dir, rels_file = posixpath.split(rels_name)
    source_dir = posixpath.dirname(rels_dir)
    source = names.get(posixpath.join(source_dir, rels_file[: -len(".rels")]).lower())
    users = _relationship_users(package.read(source)) if source is not None else None
    changed = False
    for rel in list(rels):
        kind = rel.get("Type", "")
        target = rel.get("Target", "")
        if _is_external(rel):
            if any(k in kind.lower() for k in keep):
                allowed = True
            elif kind.endswith("/hyperlink"):
                only_hyperlinks = users is not None and users.get(rel.get("Id"), set()) <= _HYPERLINK_ELEMENTS
                allowed = only_hyperlinks or urlparse(target).scheme.lower() in _WEB_SCHEMES
            else:
                allowed = False
        elif kind.endswith("/aFChunk"):
            part = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join(source_dir, target)
            )
            info = names.get(part.lower())
            allowed = info is not None and _is_ooxml(package.read(info))
        else:
            allowed = True
        if not allowed:
            rels.remove(rel)
            changed = True
    return _serialize(rels, _REL_NS) if changed else body


def _relationship_users(xml: bytes) -> dict[str, set[str]] | None:
    """Map each relationship id the part refers to onto the elements that refer to it."""
    users: dict[str, set[str]] = {}
    try:
        for _, element in ElementTree.iterparse(io.BytesIO(xml)):
            for attribute, value in element.attrib.items():
                if attribute == _VML_RELID or attribute.split("}")[0].endswith("/relationships"):
                    users.setdefault(value, set()).add(element.tag.rsplit("}", 1)[-1])
    except ElementTree.ParseError:
        return None
    return users


def _is_external(rel) -> bool:
    return rel.get("TargetMode", "").strip().lower() == "external"


def _is_ooxml(body: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as nested:
            return any(n.lower() == "[content_types].xml" for n in nested.namelist())
    except zipfile.BadZipFile:
        return False


def _serialize(root, default_namespace: str) -> bytes:
    ElementTree.register_namespace("", default_namespace)
    return ElementTree.tostring(root, encoding="UTF-8", xml_declaration=True)


def _sanitized_inputs(args: list[str], workdir: Path) -> list[str]:
    """Replace every document argument with a private, sanitized copy.

    Each document is read once, and soffice only ever opens the copy, so the
    file cannot be swapped between the check and the load.
    """
    result, filters, modes, documents = [], set(), set(), 0
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg.startswith("-"):
            name = arg.lstrip("-").split("=", 1)[0]
            if name == "infilter":
                continue
            result.append(arg)
            modes.update({name} & _DOCUMENT_MODES)
            if name in _VALUE_OPTIONS and "=" not in arg and index < len(args):
                result.append(args[index])
                index += 1
            continue
        copy, import_filter = _private_copy(arg, Path(tempfile.mkdtemp(dir=workdir)))
        result.append(str(copy))
        filters.add(import_filter)
        documents += 1

    if documents and not modes:
        raise ValueError("run_soffice only opens documents to convert or print them")
    if len(filters) > 1:
        raise ValueError("pass documents of one kind (Word, Excel or PowerPoint) per call")
    if filters:
        result.insert(0, f"--infilter={filters.pop()}")
    return result


def _private_copy(arg: str, directory: Path) -> tuple[Path, str]:
    url = urlparse(arg)
    if url.scheme.lower() == "file":
        path = Path(unquote(url.path))
    elif len(url.scheme) > 1:
        raise ValueError(f"only local documents can be opened, got {arg!r}")
    else:
        path = Path(arg)

    with open(path, "rb") as source:
        magic = source.read(len(_OLE2_MAGIC))
        source.seek(0)
        if magic.startswith(b"PK\x03\x04"):
            staged = directory / "staged"
            import_filter, extension = sanitize_ooxml(source, staged)
            copy = directory / (path.stem + extension)
            staged.rename(copy)
            return copy, import_filter
        if magic == _OLE2_MAGIC and path.suffix.lower() in _LEGACY_FILTERS:
            data = source.read()
            import_filter, extension = _LEGACY_FILTERS[path.suffix.lower()]
            # Same test LibreOffice uses: only Word 97 and later have a table stream.
            if extension == ".doc" and not any(n.encode("utf-16-le") in data for n in ("0Table", "1Table")):
                import_filter = "MS WinWord 6.0"
            copy = directory / (path.stem + extension)
            copy.write_bytes(data)
            return copy, import_filter
    raise ValueError(
        f"{path} is not an Office Open XML package or a .doc/.xls/.ppt file; "
        "soffice.py does not open other formats because they can load linked files"
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
    try:
        result = run_soffice(sys.argv[1:])
    except (ValueError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as e:
        sys.exit(f"soffice.py: {e}")
    sys.exit(result.returncode)
