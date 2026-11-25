import os
import posixpath
import re
import shutil
import stat
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path

OOXML_FAMILY = {
    ".docx": "docx",
    ".dotx": "docx",
    ".pptx": "pptx",
    ".potx": "pptx",
    ".xlsx": "xlsx",
    ".xltx": "xlsx",
}

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"


def opc_target(target: str, source_part: str, target_mode: str = "") -> str | None:
    if not target:
        return None
    if target_mode.lower() == "external":
        return None
    if _SCHEME_RE.match(target):
        return None

    target = urllib.parse.unquote(target)

    if "\\" in target:
        raise ValueError(f"relationship target is not a POSIX part name: {target!r}")

    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(source_part), target)

    parts: list[str] = []
    for segment in posixpath.normpath(joined).split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise ValueError(f"relationship target escapes the package: {target!r}")
            parts.pop()
        else:
            parts.append(segment)

    if not parts:
        raise ValueError(f"relationship target resolves to nothing: {target!r}")
    return "/".join(parts)


def rels_source_part(rels_file: Path, unpacked_dir: Path) -> str:
    owner_dir = rels_file.parent.parent.relative_to(unpacked_dir)
    return posixpath.join(owner_dir.as_posix(), rels_file.name[: -len(".rels")]).lstrip("./")


def part_text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


XML_SPACE = " \t\r\n"


def rendered_text(text: str, preserve: bool) -> str:
    return text if preserve else text.strip(XML_SPACE)


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for m in zf.infolist():
        if stat.S_ISLNK(m.external_attr >> 16):
            raise ValueError(f"symlink archive entry not allowed: {m.filename!r}")
        target = (dest / m.filename).resolve()
        if not target.is_relative_to(dest):
            raise ValueError(f"unsafe archive entry: {m.filename!r}")
        zf.extract(m, dest)


_CONTENT_TYPES = "[Content_Types].xml"
_OPEN_NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _zip_tree(zf: zipfile.ZipFile, dir_fd: int, prefix: str) -> None:
    # Every entry is opened relative to its parent's fd with O_NOFOLLOW and
    # checked with fstat on that fd, so a path swapped to a symlink after the
    # listing is refused rather than read through.
    for name in sorted(os.listdir(dir_fd), key=lambda n: (n != _CONTENT_TYPES, n)):
        arcname = prefix + name
        try:
            fd = os.open(name, _OPEN_NOFOLLOW, dir_fd=dir_fd)
        except OSError as e:
            raise ValueError(f"refusing to pack {arcname!r}: {e.strerror}") from None
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                _zip_tree(zf, fd, arcname + "/")
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError(
                    f"refusing to pack {arcname!r}: not a regular file with a single link"
                )
            info = zipfile.ZipInfo(
                arcname, max(time.localtime(st.st_mtime)[:6], (1980, 1, 1, 0, 0, 0))
            )
            info.external_attr = (st.st_mode & 0xFFFF) << 16
            info.file_size = st.st_size
            info.compress_type = (
                zipfile.ZIP_STORED if arcname == _CONTENT_TYPES else zipfile.ZIP_DEFLATED
            )
            with os.fdopen(fd, "rb", closefd=False) as src, zf.open(info, "w") as dst:
                shutil.copyfileobj(src, dst)
        finally:
            os.close(fd)


def rezip(src_dir: Path, out_path: Path) -> None:
    # The archive is built in an anonymous temp file so nothing appears next to
    # out_path until every entry has been read.
    with tempfile.TemporaryFile() as staged:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as zf:
            top = os.open(src_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _zip_tree(zf, top, "")
            finally:
                os.close(top)
        staged.seek(0)
        fd, tmp_name = tempfile.mkstemp(
            prefix=out_path.name + ".", suffix=".tmp", dir=out_path.parent
        )
        tmp_out = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                shutil.copyfileobj(staged, fh)
            if out_path.exists():
                mode = out_path.stat().st_mode & 0o777
            else:
                umask = os.umask(0)
                os.umask(umask)
                mode = 0o666 & ~umask
            os.chmod(tmp_out, mode)
            os.replace(tmp_out, out_path)
        finally:
            if tmp_out.exists():
                tmp_out.unlink()
