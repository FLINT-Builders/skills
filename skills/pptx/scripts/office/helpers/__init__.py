import errno
import os
import posixpath
import re
import secrets
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


class UnsafePath(ValueError):
    """A path inside an unpacked package is a symbolic link or not the expected kind of file."""


_LINK_ERRNOS = (errno.ELOOP, errno.EMLINK, errno.ENOTDIR)


class PackageDir:
    """An unpacked package whose paths cannot leave it.

    Every path is walked one component at a time relative to directory
    descriptors that are opened with O_NOFOLLOW and kept for the life of the
    object, so a symbolic link anywhere below the root is refused and a link
    swapped in after a check is never followed. Writes go to a temporary file
    in the pinned directory that is then renamed over the target, and a file
    that was read through this object is only replaced if its inode is unchanged.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self._dirs: dict[tuple[str, ...], int] = {}
        self._seen: dict[str, tuple[int, int]] = {}

    def __enter__(self) -> "PackageDir":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for fd in self._dirs.values():
            os.close(fd)
        self._dirs.clear()
        if self._root_fd != -1:
            os.close(self._root_fd)
            self._root_fd = -1

    @staticmethod
    def _parts(rel: str) -> tuple[str, ...]:
        parts = tuple(rel.split("/")) if rel else ()
        for part in parts:
            if part in ("", ".", "..") or "\\" in part or "\0" in part:
                raise UnsafePath(f"not a plain path inside the package: {rel!r}")
        return parts

    def _split(self, rel: str) -> tuple[tuple[str, ...], str]:
        parts = self._parts(rel)
        if not parts:
            raise UnsafePath("the package root is not a file")
        return parts[:-1], parts[-1]

    def _dir(self, parts: tuple[str, ...], create: bool = False) -> int | None:
        if not parts:
            return self._root_fd
        if parts in self._dirs:
            return self._dirs[parts]
        parent = self._dir(parts[:-1], create)
        if parent is None:
            return None
        name = parts[-1]
        if create:
            try:
                os.mkdir(name, 0o777, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno in _LINK_ERRNOS:
                raise UnsafePath(
                    f"{'/'.join(parts)} is not a directory inside the package "
                    f"(symbolic links are refused)"
                ) from None
            raise
        self._dirs[parts] = fd
        return fd

    def _lstat(self, dfd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def has_dir(self, rel: str) -> bool:
        return self._dir(self._parts(rel)) is not None

    def makedirs(self, rel: str) -> None:
        self._dir(self._parts(rel), create=True)

    def lstat(self, rel: str) -> os.stat_result | None:
        parent, name = self._split(rel)
        dfd = self._dir(parent)
        return None if dfd is None else self._lstat(dfd, name)

    def is_file(self, rel: str) -> bool:
        """True for a regular file, False when absent; refuses anything else."""
        st = self.lstat(rel)
        if st is None:
            return False
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(f"{rel} is not a regular file (symbolic links are refused)")
        return True

    def listdir(self, rel: str) -> list[str]:
        dfd = self._dir(self._parts(rel))
        return [] if dfd is None else sorted(os.listdir(dfd))

    def entries(self, rel: str) -> list[tuple[str, os.stat_result]]:
        dfd = self._dir(self._parts(rel))
        if dfd is None:
            return []
        found = []
        for name in sorted(os.listdir(dfd)):
            st = self._lstat(dfd, name)
            if st is not None:
                found.append((name, st))
        return found

    def walk_files(self, rel: str = "") -> list[str]:
        """Every regular file below rel; any symbolic link in the tree is refused."""
        files = []
        for name, st in self.entries(rel):
            child = f"{rel}/{name}" if rel else name
            if stat.S_ISLNK(st.st_mode):
                raise UnsafePath(f"{child} is a symbolic link; refusing to work on this package")
            if stat.S_ISDIR(st.st_mode):
                files.extend(self.walk_files(child))
            elif stat.S_ISREG(st.st_mode):
                files.append(child)
        return files

    def read_bytes(self, rel: str) -> bytes:
        parent, name = self._split(rel)
        dfd = self._dir(parent)
        if dfd is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.root / rel))
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except OSError as e:
            if e.errno in _LINK_ERRNOS:
                raise UnsafePath(f"{rel} is a symbolic link; refusing to read through it") from None
            if isinstance(e, FileNotFoundError):
                raise FileNotFoundError(e.errno, e.strerror, str(self.root / rel)) from None
            raise
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(f"{rel} is not a regular file")
            data = fh.read()
        self._seen[rel] = (st.st_dev, st.st_ino)
        return data

    def read_text(self, rel: str) -> str:
        return self.read_bytes(rel).decode("utf-8")

    def write_bytes(self, rel: str, data: bytes, exclusive: bool = False) -> None:
        parent, name = self._split(rel)
        dfd = self._dir(parent)
        if dfd is None:
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), str(self.root / "/".join(parent))
            )
        current = self._lstat(dfd, name)
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise UnsafePath(f"{rel} is not a regular file (symbolic links are refused)")
        seen = self._seen.get(rel)
        if seen is not None and (
            current is None or (current.st_dev, current.st_ino) != seen
        ):
            raise UnsafePath(f"{rel} was replaced after it was read; refusing to write it")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        if exclusive:
            if current is not None:
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(self.root / rel))
            fd = os.open(name, flags, 0o666, dir_fd=dfd)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    st = os.fstat(fh.fileno())
            except BaseException:
                os.unlink(name, dir_fd=dfd)
                raise
            self._seen[rel] = (st.st_dev, st.st_ino)
            return

        tmp = f".{name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, flags, 0o666, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                if current is not None:
                    os.fchmod(fh.fileno(), stat.S_IMODE(current.st_mode))
                st = os.fstat(fh.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            raise
        self._seen[rel] = (st.st_dev, st.st_ino)

    def write_text(self, rel: str, text: str, exclusive: bool = False) -> None:
        self.write_bytes(rel, text.encode("utf-8"), exclusive=exclusive)

    def unlink(self, rel: str) -> None:
        parent, name = self._split(rel)
        dfd = self._dir(parent)
        if dfd is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.root / rel))
        os.unlink(name, dir_fd=dfd)
        self._seen.pop(rel, None)

    def rmdir(self, rel: str) -> None:
        parts = self._parts(rel)
        if not parts:
            raise UnsafePath("refusing to remove the package root")
        dfd = self._dir(parts[:-1])
        if dfd is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.root / rel))
        fd = self._dirs.pop(parts, None)
        if fd is not None:
            os.close(fd)
        os.rmdir(parts[-1], dir_fd=dfd)


def write_inside(root: Path, path: Path, data: bytes) -> None:
    """Replace path, which must lie inside root, without following symbolic links."""
    rel = Path(path).relative_to(root).as_posix()
    with PackageDir(root) as pkg:
        pkg.write_bytes(rel, data)


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
