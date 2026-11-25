"""
Helper for running LibreOffice (soffice) in environments where AF_UNIX
sockets may be blocked (e.g., sandboxed VMs).  Detects the restriction
at runtime and applies an LD_PRELOAD shim if needed.

Usage:
    from office.soffice import run_soffice, sanitized_copy

    doc = sanitized_copy(Path("input.docx"), private_tmp_dir)
    result = run_soffice(["--headless", "--convert-to", "pdf", str(doc)])

Call soffice through run_soffice, not through subprocess with get_soffice_env():
the env dict carries the shim but names no user profile, and a non-root sandbox
cannot bootstrap the default one -- soffice aborts with "User installation could
not be completed" and converts nothing. get_soffice_env() stays public for the
callers that build their own argv (they must pass -env:UserInstallation too, and
call harden_profile() on that profile before every launch).

LibreOffice fetches whatever a document links to while it renders: a picture
whose relationship points at http://, file:// or a path outside the package
becomes a request from this host or a read of a local file. Never hand it a
caller's file. Open the copy sanitized_copy() writes, which keeps only the
relationships that stay inside the package. run_soffice() also hardens the
profile it starts with, so LibreOffice cannot reach the network and blocks most
links out of a document. The profile alone still loads some linked fills from
local files, so it backs the copy up rather than replacing it. Run as a script,
this module does both for every document on the command line.
"""

import contextlib
import os
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import urllib.parse
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO
from xml.sax.saxutils import quoteattr


class UnsafeDocumentError(ValueError):
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
        profile_arg = next(
            (str(a) for a in args if str(a).startswith("-env:UserInstallation=")), None
        )
        if profile_arg is None:
            profile = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="lo_profile_", ignore_cleanup_errors=True)
            )
            args = [f"-env:UserInstallation={Path(profile).as_uri()}"] + args
            harden_profile(Path(profile))
        else:
            harden_profile(profile_url_to_path(profile_arg.split("=", 1)[1]))
        return subprocess.run(["soffice"] + args, env=get_soffice_env(), **kwargs)


def profile_url_to_path(url: str) -> Path:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise UnsafeDocumentError(f"LibreOffice profile must be a local file: URL, got {url!r}")
    return Path(urllib.parse.unquote(parsed.path))


# Only the providers LibreOffice needs to open local packages. Dropping the
# http, https, WebDAV, CMIS and any platform (gio) providers means no URL in a
# document can reach the network, whichever format or code path follows it.
_LOCAL_CONTENT_PROVIDERS = (
    ("Provider1", "com.sun.star.ucb.HierarchyContentProvider", "vnd.sun.star.hier"),
    ("Provider2", "com.sun.star.ucb.FileContentProvider", "file"),
    ("Provider5", "com.sun.star.help.XMLHelp", "vnd.sun.star.help"),
    ("Provider8", "com.sun.star.ucb.PackageContentProvider", "vnd.sun.star.pkg"),
    ("Provider9", "com.sun.star.ucb.ExpandContentProvider", "vnd.sun.star.expand"),
    ("Provider10", "com.sun.star.ucb.TransientDocumentsContentProvider", "vnd.sun.star.tdoc"),
    ("Provider11", "com.sun.star.ucb.PackageContentProvider", "vnd.sun.star.zip"),
    ("Provider14", "com.sun.star.ucb.ExtensionContentProvider", "vnd.sun.star.extension"),
    ("Provider16", "com.sun.star.ucb.ImageContentProvider", "vnd.libreoffice.image"),
    ("Provider900", "", "private"),
)

_HARDENING_ITEMS = (
    '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
    '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>'
    "</item>",
    "<item oor:path=\"/org.openoffice.ucb.Configuration/ContentProviders/['Local']/SecondaryKeys\">"
    '<node oor:name="Office" oor:op="replace"><node oor:name="ProviderData">'
    + "".join(
        f'<node oor:name="{name}" oor:op="replace">'
        f'<prop oor:name="ServiceName"><value>{service}</value></prop>'
        f'<prop oor:name="URLTemplate"><value>{template}</value></prop>'
        '<prop oor:name="Arguments"><value/></prop>'
        "</node>"
        for name, service, template in _LOCAL_CONTENT_PROVIDERS
    )
    + "</node></node></item>",
)

_EMPTY_REGISTRY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
    'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    "</oor:items>\n"
)


def harden_profile(profile_dir: Path) -> None:
    """Make the profile block remote content and links out of documents.

    LibreOffice applies registrymodifications.xcu items in order, so these go
    last, after anything an earlier run of the same profile wrote back.
    """
    registry = Path(profile_dir) / "user" / "registrymodifications.xcu"
    registry.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = registry.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = _EMPTY_REGISTRY
    lines = [line for line in text.splitlines() if line.strip() not in _HARDENING_ITEMS]
    closing = max((i for i, line in enumerate(lines) if line.strip() == "</oor:items>"), default=None)
    if closing is None:
        lines = _EMPTY_REGISTRY.splitlines()
        closing = len(lines) - 1
    lines[closing:closing] = list(_HARDENING_ITEMS)
    registry.write_text("\n".join(lines) + "\n", encoding="utf-8")


_PACKAGE_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL_ATTR_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_OFFICE_DOCUMENT_TYPES = tuple(ns + "/officeDocument" for ns in _REL_ATTR_NAMESPACES)

# The only relationships kept when they point outside the package: type ->
# (elements allowed to refer to the Id, target schemes allowed, None = any).
# Hyperlinks are followed on click, never while rendering, and only web and
# mail ones are kept, so even a misread one cannot name a local file.
# externalBook names the workbook behind [1]Sheet!A1 formulas, which recalc.py
# must not destroy. Any other reference to the same Id -- a picture's r:link
# naming a hyperlink relationship, say -- drops it.
_HYPERLINK_ELEMENTS = frozenset({"hlinkClick", "hlinkHover", "hlinkMouseOver", "hyperlink"})
_WEB_SCHEMES = frozenset({"http", "https", "mailto"})
_KEPT_EXTERNAL = {
    **{ns + "/hyperlink": (_HYPERLINK_ELEMENTS, _WEB_SCHEMES) for ns in _REL_ATTR_NAMESPACES},
    **{ns + "/externalLinkPath": (frozenset({"externalBook"}), None) for ns in _REL_ATTR_NAMESPACES},
    "http://schemas.microsoft.com/office/2006/relationships/xlExternalLinkPath/xlPathMissing": (
        frozenset({"externalBook"}),
        None,
    ),
}

OOXML_SUFFIXES = {
    "docx": (".docx", ".docm", ".dotx", ".dotm"),
    "pptx": (".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"),
    "xlsx": (".xlsx", ".xlsm", ".xltx", ".xltm"),
}
_FAMILY_MARKERS = {
    "docx": ("wordprocessingml", "ms-word"),
    "pptx": ("presentationml", "ms-powerpoint"),
    "xlsx": ("spreadsheetml", "ms-excel"),
}

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def sanitized_copy(src: Path, dest_dir: Path, family: str | None = None) -> Path:
    """Copy the OOXML package src into dest_dir without links out of the package.

    Every relationship that is external, absolute, or climbs out of the
    package is removed, except the hyperlinks and workbook links in
    _KEPT_EXTERNAL. Every .rels part is rewritten from what was parsed, so
    LibreOffice reads exactly the relationships checked here. The file is
    read through one handle, so swapping src mid-copy changes nothing.

    Raises UnsafeDocumentError when src is not a well-formed OOXML package
    (of `family`, if given) or carries OpenDocument markers LibreOffice would
    detect instead.
    """
    src = Path(src)
    try:
        with open(src, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise UnsafeDocumentError(f"{src} is not a regular file")
            return _copy_package(fh, src.name, Path(dest_dir), family)
    except OSError as e:
        raise UnsafeDocumentError(f"cannot read {src}: {e}") from e


def _copy_package(fh: BinaryIO, name: str, dest_dir: Path, family: str | None) -> Path:
    try:
        return _copy_zip(fh, name, dest_dir, family)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as e:
        raise UnsafeDocumentError(f"{name} is not a readable Office Open XML package: {e}") from e


def _copy_zip(fh: BinaryIO, name: str, dest_dir: Path, family: str | None) -> Path:
    import defusedxml.ElementTree as ET
    from defusedxml.common import DefusedXmlException

    with zipfile.ZipFile(fh) as zf:
        infos = zf.infolist()
        by_key: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            key = info.filename.replace("\\", "/").lower()
            if key in by_key:
                raise UnsafeDocumentError(f"{name} has two entries named {info.filename!r}")
            if key == "mimetype" or key.startswith("meta-inf/"):
                raise UnsafeDocumentError(f"{name} carries OpenDocument entries; refusing to open it")
            by_key[key] = info

        def read(key: str) -> bytes | None:
            info = by_key.get(key)
            return None if info is None else zf.read(info)

        def parse(data: bytes):
            try:
                return ET.fromstring(data)
            except (ET.ParseError, DefusedXmlException, ValueError) as e:
                raise UnsafeDocumentError(f"{name} has an unreadable XML part") from e

        found = _package_family(read, parse)
        if found is None or (family is not None and found != family):
            want = family or "Office Open XML"
            raise UnsafeDocumentError(f"{name} is not a {want} package")

        suffix = Path(name).suffix.lower()
        out_name = name if suffix in OOXML_SUFFIXES[found] else f"{Path(name).stem}.{found}"
        out_path = dest_dir / out_name

        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for info in infos:
                data = zf.read(info)
                key = info.filename.replace("\\", "/").lower()
                if key.endswith(".rels"):
                    data = _strip_rels(key, data, read, parse)
                else:
                    data = _strip_svg_links(key, data, parse)
                out.writestr(info.filename, data)
    return out_path


_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_MAX_SVG_BYTES = 64 * 1024 * 1024


def _strip_svg_links(key: str, data: bytes, parse) -> bytes:
    """Drop every href that leaves the picture from anything LibreOffice reads as SVG.

    Its SVG reader loads <image href="file:///..."> and similar from inside an
    embedded picture, no relationship involved. LibreOffice treats a part as
    SVG when its name ends in .svg/.svgz, or when "<svg" shows up in its first
    8 KiB (gunzipped first if it is gzip); the test below is wider than that.
    """
    named_svg = posixpath.splitext(key)[1] in (".svg", ".svgz")
    gzipped = data[:2] == b"\x1f\x8b"
    content = data
    if gzipped:
        inflater = zlib.decompressobj(wbits=31)
        try:
            content = inflater.decompress(data, _MAX_SVG_BYTES)
            complete = inflater.eof and not inflater.unconsumed_tail
        except zlib.error:
            content, complete = b"", False
        if not complete:
            if named_svg or _looks_like_svg(content):
                raise UnsafeDocumentError(f"cannot inspect the compressed picture {key}")
            return data

    if not (named_svg or _looks_like_svg(content)):
        return data

    root = parse(content)
    if not named_svg and not any(
        isinstance(el.tag, str) and _local(el.tag).lower() == "svg" for el in root.iter()
    ):
        return data

    for el in root.iter():
        for attr in [a for a in el.attrib if _local(a).lower() == "href"]:
            value = el.attrib[attr].strip()
            if not (value.startswith("#") or value.lower().startswith("data:")):
                del el.attrib[attr]

    # LibreOffice's SVG reader matches "svg:"/"xlink:" prefixes literally, so
    # write those two namespaces under their usual names, not ElementTree's ns0.
    svg_prefix, xlink_prefix = f"{{{_SVG_NS}}}", f"{{{_XLINK_NS}}}"
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith(svg_prefix):
            el.tag = el.tag[len(svg_prefix):]
        for attr in [a for a in el.attrib if a.startswith(xlink_prefix)]:
            el.attrib[f"xlink:{attr[len(xlink_prefix):]}"] = el.attrib.pop(attr)
    root.set("xmlns", _SVG_NS)
    root.set("xmlns:xlink", _XLINK_NS)

    import xml.etree.ElementTree as StdET

    cleaned = StdET.tostring(root, encoding="utf-8", xml_declaration=True)
    if gzipped:
        compressor = zlib.compressobj(wbits=31)
        cleaned = compressor.compress(cleaned) + compressor.flush()
    return cleaned


def _looks_like_svg(content: bytes) -> bool:
    head = content[:8192].lower()
    return b"<svg" in head or (b"<?xml" in head[:256] and b"svg" in head[:256])


def _package_family(read, parse) -> str | None:
    types_xml, root_rels = read("[content_types].xml"), read("_rels/.rels")
    if types_xml is None or root_rels is None:
        return None

    main = None
    for rel in _elements(parse(root_rels), "Relationship"):
        if rel.get("Type") in _OFFICE_DOCUMENT_TYPES and _inside_package(rel, ""):
            main = _resolve(rel.get("Target", ""), "")
            break
    if main is None:
        return None

    content_type = None
    for el in _elements(parse(types_xml), "Override"):
        if el.get("PartName", "").lstrip("/").lower() == main.lower():
            content_type = el.get("ContentType", "")
    if content_type is None:
        ext = posixpath.splitext(main)[1].lstrip(".").lower()
        for el in _elements(parse(types_xml), "Default"):
            if el.get("Extension", "").lower() == ext:
                content_type = el.get("ContentType", "")
    content_type = (content_type or "").lower()

    for family, markers in _FAMILY_MARKERS.items():
        if any(marker in content_type for marker in markers):
            return family
    return None


def _strip_rels(key: str, data: bytes, read, parse) -> bytes:
    rels_dir, rels_name = posixpath.split(key)
    owner_dir = posixpath.dirname(rels_dir)
    source = posixpath.join(owner_dir, rels_name[: -len(".rels")]) if rels_name != ".rels" else ""

    source_tree = None
    if source:
        source_data = read(source)
        if source_data is not None:
            with contextlib.suppress(UnsafeDocumentError):
                source_tree = parse(source_data)

    kept = []
    for rel in _elements(parse(data), "Relationship"):
        attrs = {k: rel.get(k) for k in ("Id", "Type", "Target", "TargetMode") if rel.get(k) is not None}
        if _inside_package(rel, source):
            attrs.pop("TargetMode", None)
            kept.append(attrs)
            continue
        elements, schemes = _KEPT_EXTERNAL.get(attrs.get("Type", ""), (None, None))
        if (
            elements
            and source_tree is not None
            and (schemes is None or _scheme(attrs.get("Target", "")) in schemes)
            and _only_referenced_by(source_tree, attrs.get("Id", ""), elements)
        ):
            attrs["TargetMode"] = "External"
            kept.append(attrs)

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Relationships xmlns="{_PACKAGE_RELS_NS}">',
    ]
    for attrs in kept:
        rendered = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
        lines.append(f"<Relationship {rendered}/>")
    lines.append("</Relationships>")
    return "\n".join(lines).encode("utf-8")


def _elements(root, local_name: str):
    return [el for el in root.iter() if isinstance(el.tag, str) and _local(el.tag) == local_name]


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _scheme(target: str) -> str | None:
    match = _SCHEME_RE.match(target)
    return match.group(0)[:-1].lower() if match else None


def _inside_package(rel, source_part: str) -> bool:
    if rel.get("TargetMode", "Internal").strip().lower() != "internal":
        return False
    return _resolve(rel.get("Target", ""), source_part) is not None


def _resolve(target: str, source_part: str) -> str | None:
    decoded = urllib.parse.unquote(target)
    for candidate in (target, decoded):
        if not candidate.strip() or _SCHEME_RE.match(candidate.strip()):
            return None
        if "\\" in candidate or candidate.lstrip().startswith("//"):
            return None
    if decoded.startswith("/"):
        joined = decoded.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(source_part), decoded)
    parts: list[str] = []
    for segment in joined.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts) or None


def _only_referenced_by(tree, rel_id: str, elements: frozenset) -> bool:
    if not rel_id.strip():
        return False
    allowed_attrs = {f"{{{ns}}}id" for ns in _REL_ATTR_NAMESPACES}
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        if (el.text or "").strip() == rel_id.strip():
            return False
        for attr, value in el.attrib.items():
            if value.strip() != rel_id.strip():
                continue
            if _local(el.tag) not in elements or attr not in allowed_attrs:
                return False
    return True


# Options LibreOffice's command line reads the following argument(s) for
# (desktop/source/app/cmdlineargs.cxx). Every other bare argument is a
# document to open.
_OPTION_VALUES = {
    "convert-to": 1,
    "outdir": 1,
    "convert-images-to": 1,
    "printer-name": 1,
    "pt": 1,
    "display": 1,
    "protector": 2,
}


def sanitize_arguments(args: Iterable[str], workdir: Path) -> list[str]:
    """Replace every document on a soffice command line with a safe copy.

    OOXML packages become sanitized_copy() output. Legacy binary Office files
    (.doc, .ppt, .xls) are copied as-is; their linked pictures go through the
    link manager the hardened profile blocks. Anything else -- URLs, ODF,
    RTF, HTML -- is refused, because its links can neither be stripped here
    nor are all of them blocked by the profile.
    """
    out: list[str] = []
    pending_values = 0
    for index, arg in enumerate(args):
        arg = str(arg)
        if pending_values:
            pending_values -= 1
            out.append(arg)
        elif arg.startswith("-"):
            name = arg[2:] if arg.startswith("--") else arg[1:]
            pending_values = _OPTION_VALUES.get(name, 0)
            out.append(arg)
        elif not arg:
            out.append(arg)
        else:
            dest = Path(workdir) / f"input{index}"
            dest.mkdir()
            out.append(str(_safe_input(arg, dest)))
    return out


def _safe_input(arg: str, dest: Path) -> Path:
    name = Path(arg).name
    try:
        with open(arg, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise UnsafeDocumentError(f"{arg} is not a regular file")
            if fh.read(len(OLE2_MAGIC)) == OLE2_MAGIC:
                fh.seek(0)
                copy = dest / name
                with open(copy, "wb") as out:
                    shutil.copyfileobj(fh, out)
                return copy
            fh.seek(0)
            return _copy_package(fh, name, dest, None)
    except OSError as e:
        raise UnsafeDocumentError(f"{arg} is not a local file soffice.py can open: {e}") from e



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
    with tempfile.TemporaryDirectory(prefix="soffice_inputs_") as workdir:
        try:
            result = run_soffice(sanitize_arguments(sys.argv[1:], Path(workdir)))
        except UnsafeDocumentError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    sys.exit(result.returncode)
