import os
import posixpath
import re
import shutil
import stat
import tempfile
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


_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | _CLOEXEC


def _member_parts(m: zipfile.ZipInfo) -> list[str]:
    if stat.S_ISLNK(m.external_attr >> 16):
        raise ValueError(f"symlink archive entry not allowed: {m.filename!r}")
    name = m.filename
    if name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if "\\" in name or "\0" in name or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe archive entry: {m.filename!r}")
    return parts


def _open_subdir(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o777, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


def extract_to_new_dir(zf: zipfile.ZipFile, dest: Path) -> None:
    """Create `dest` and extract every member into it.

    Refuses when anything, including a symlink, already exists at `dest`.
    Members are written through descriptors of the directory created here,
    never through the path, which another user may swap for a link.
    """
    members = [(m, _member_parts(m)) for m in zf.infolist()]
    dest = Path(dest)
    if dest.name in ("", ".", ".."):
        raise ValueError(f"not a new directory name: {str(dest)!r}")

    parent_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        try:
            os.mkdir(dest.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            raise FileExistsError(
                f"{dest} already exists; unpack into a new directory"
            ) from None
        root_fd = os.open(dest.name, _DIR_FLAGS, dir_fd=parent_fd)
        try:
            st = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.geteuid()
                or os.listdir(root_fd)
            ):
                raise ValueError(f"{dest} was replaced while it was being created")
            for m, parts in members:
                opened: list[int] = []
                try:
                    cur = root_fd
                    dirs = parts if m.is_dir() else parts[:-1]
                    for part in dirs:
                        cur = _open_subdir(cur, part)
                        opened.append(cur)
                    if m.is_dir():
                        continue
                    out = os.open(parts[-1], _FILE_FLAGS, 0o666, dir_fd=cur)
                    with os.fdopen(out, "wb") as fh, zf.open(m) as src:
                        shutil.copyfileobj(src, fh)
                finally:
                    for fd in opened:
                        os.close(fd)
            try:
                now = os.stat(dest.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                now = None
            if now is None or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                raise ValueError(f"{dest} was moved or replaced while unpacking")
        finally:
            os.close(root_fd)
    finally:
        os.close(parent_fd)


def rezip(src_dir: Path, out_path: Path) -> None:
    files = sorted(p for p in src_dir.rglob("*") if p.is_file())
    ct = src_dir / "[Content_Types].xml"
    fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".", suffix=".tmp", dir=out_path.parent
    )
    tmp_out = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                if ct.exists():
                    zf.write(ct, ct.relative_to(src_dir), compress_type=zipfile.ZIP_STORED)
                for f in files:
                    if f == ct:
                        continue
                    zf.write(f, f.relative_to(src_dir))
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
