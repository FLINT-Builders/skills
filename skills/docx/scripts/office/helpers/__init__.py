import io
import os
import posixpath
import re
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


MAX_MEMBER_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_MEMBER_COUNT = 256
_COPY_CHUNK = 64 * 1024


def check_member_budget(info: zipfile.ZipInfo) -> None:
    if info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"archive entry {info.filename!r} expands to {info.file_size} bytes "
            f"(limit {MAX_MEMBER_UNCOMPRESSED_BYTES})"
        )
    if info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1):
        raise ValueError(
            f"archive entry {info.filename!r} compression ratio exceeds {MAX_COMPRESSION_RATIO}:1"
        )


def check_archive_budget(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_MEMBER_COUNT:
        raise ValueError(f"archive has {len(infos)} entries (limit {MAX_MEMBER_COUNT})")
    total = 0
    for info in infos:
        check_member_budget(info)
        total += info.file_size
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"archive expands to {total} bytes (limit {MAX_TOTAL_UNCOMPRESSED_BYTES})"
        )
    return infos


def open_member(zf: zipfile.ZipFile, name: str):
    info = zf.getinfo(name)
    check_member_budget(info)
    return zf.open(info)


def read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    check_member_budget(info)
    buf = io.BytesIO()
    with zf.open(info) as src:
        _copy_capped(src, buf, info.file_size, name)
    return buf.getvalue()


def _copy_capped(src, dst, limit: int, name: str) -> int:
    written = 0
    while chunk := src.read(min(_COPY_CHUNK, limit - written + 1)):
        written += len(chunk)
        if written > limit:
            raise ValueError(f"archive entry {name!r} expands past its budget")
        dst.write(chunk)
    return written


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    planned: list[tuple[zipfile.ZipInfo, Path]] = []
    for m in check_archive_budget(zf):
        if stat.S_ISLNK(m.external_attr >> 16):
            raise ValueError(f"symlink archive entry not allowed: {m.filename!r}")
        target = (dest / m.filename).resolve()
        if not target.is_relative_to(dest):
            raise ValueError(f"unsafe archive entry: {m.filename!r}")
        planned.append((m, target))

    remaining = MAX_TOTAL_UNCOMPRESSED_BYTES
    for m, target in planned:
        if m.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(target, "wb") as dst:
            remaining -= _copy_capped(src, dst, min(m.file_size, remaining), m.filename)


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
