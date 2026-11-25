"""Remove unreferenced files from an unpacked PPTX directory.

Usage: python clean.py <unpacked_dir>

Example:
    python clean.py unpacked/

This script removes:
- Orphaned slides (not in sldIdLst) and their relationships
- [trash] directory (unreferenced files)
- Orphaned .rels files for deleted resources
- Unreferenced media, embeddings, charts, diagrams, drawings, ink files
- Unreferenced theme files
- Unreferenced notes slides
- Content-Type overrides for deleted files

It refuses to run when a directory it deletes from, or a file it rewrites,
is a symlink. Deletion goes through directory file descriptors opened
without following symlinks, so swapping a directory for a link mid-run
cannot redirect it outside the package.
"""

import contextlib
import errno
import fnmatch
import os
import posixpath
import re
import stat
import sys
from pathlib import Path

import defusedxml.minidom

from office.helpers import SLIDE_REL_TYPE, opc_target, rels_source_part


RESOURCE_DIRS = ["media", "embeddings", "charts", "diagrams", "tags", "drawings", "ink"]
RELS_RESOURCE_DIRS = ["charts", "diagrams", "drawings"]

PACKAGE_DIRS = [
    "ppt",
    "ppt/_rels",
    "ppt/slides",
    "ppt/slides/_rels",
    "[trash]",
    *(f"ppt/{d}" for d in RESOURCE_DIRS),
    *(f"ppt/{d}/_rels" for d in RELS_RESOURCE_DIRS),
    "ppt/theme",
    "ppt/theme/_rels",
    "ppt/notesSlides",
    "ppt/notesSlides/_rels",
]
REWRITTEN_PARTS = ["[Content_Types].xml", "ppt/_rels/presentation.xml.rels"]

_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC


def _slide_rids(pres_rels_path: Path, unpacked_dir: Path) -> dict[str, str]:
    source_part = rels_source_part(pres_rels_path, unpacked_dir)
    rels_dom = defusedxml.minidom.parse(str(pres_rels_path))

    rids: dict[str, str] = {}
    for rel in rels_dom.getElementsByTagName("Relationship"):
        if rel.getAttribute("Type") != SLIDE_REL_TYPE:
            continue
        part = opc_target(
            rel.getAttribute("Target"), source_part, rel.getAttribute("TargetMode")
        )
        if part is not None:
            rids[rel.getAttribute("Id")] = part
    return rids


def get_slides_in_sldidlst(unpacked_dir: Path) -> set[str]:
    pres_path = unpacked_dir / "ppt" / "presentation.xml"
    pres_rels_path = unpacked_dir / "ppt" / "_rels" / "presentation.xml.rels"

    if not pres_path.exists() or not pres_rels_path.exists():
        return set()

    rid_to_slide = _slide_rids(pres_rels_path, unpacked_dir)

    pres_content = pres_path.read_text(encoding="utf-8")
    referenced_rids = set(re.findall(r'<p:sldId[^>]*r:id="([^"]+)"', pres_content))

    return {
        posixpath.basename(rid_to_slide[rid])
        for rid in referenced_rids
        if rid in rid_to_slide
    }


class RefusedToClean(Exception):
    """The package does not look the way a readable package should."""


def _open_root(unpacked_dir: Path) -> int:
    if not (
        _NOFOLLOW
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    ):
        raise RefusedToClean(
            "this platform cannot delete files without following symlinks."
        )
    try:
        return os.open(unpacked_dir, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    except NotADirectoryError:
        raise RefusedToClean(f"{unpacked_dir} is not a directory.") from None


def _open_dir(base_fd: int, rel: str) -> int | None:
    """Open `rel` under `base_fd` one component at a time without following
    symlinks. Returns None if it does not exist."""
    fd = os.dup(base_fd)
    try:
        for name in filter(None, rel.split("/")):
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                return None
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RefusedToClean(
                        f"{rel} is a symlink or not a directory. Refusing to "
                        f"delete through it."
                    ) from None
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _package_dir(base_fd: int, rel: str):
    fd = _open_dir(base_fd, rel)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _lstat(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _is_regular(dir_fd: int, name: str) -> bool:
    st = _lstat(dir_fd, name)
    return st is not None and stat.S_ISREG(st.st_mode)


def _regular_files(dir_fd: int, pattern: str) -> list[str]:
    with os.scandir(dir_fd) as entries:
        return sorted(
            e.name
            for e in entries
            if fnmatch.fnmatchcase(e.name, pattern) and e.is_file(follow_symlinks=False)
        )


def _read_part(dir_fd: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RefusedToClean(f"{name} is a symlink. Refusing to rewrite it.") from None
        raise
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _write_part(dir_fd: int, name: str, data: bytes) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_TRUNC | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RefusedToClean(f"{name} is a symlink. Refusing to rewrite it.") from None
        raise
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _refuse_links(root_fd: int) -> None:
    for rel in PACKAGE_DIRS:
        with _package_dir(root_fd, rel):
            pass
    for rel in REWRITTEN_PARTS:
        parent, _, name = rel.rpartition("/")
        with _package_dir(root_fd, parent) as parent_fd:
            if parent_fd is None:
                continue
            st = _lstat(parent_fd, name)
            if st is not None and stat.S_ISLNK(st.st_mode):
                raise RefusedToClean(f"{rel} is a symlink. Refusing to rewrite it.")


def remove_orphaned_slides(unpacked_dir: Path, root_fd: int) -> list[str]:
    pres_rels_path = unpacked_dir / "ppt" / "_rels" / "presentation.xml.rels"
    removed = []

    with _package_dir(root_fd, "ppt/slides") as slides_fd:
        if slides_fd is None:
            return []

        referenced_slides = get_slides_in_sldidlst(unpacked_dir)
        on_disk = _regular_files(slides_fd, "slide*.xml")

        if on_disk and not any(name in referenced_slides for name in on_disk):
            listed = re.findall(
                r'<p:sldId[^>]*r:id="([^"]+)"',
                (unpacked_dir / "ppt" / "presentation.xml").read_text(encoding="utf-8")
                if (unpacked_dir / "ppt" / "presentation.xml").exists()
                else "",
            )
            if listed:
                raise RefusedToClean(
                    f"<p:sldIdLst> lists {len(listed)} slide(s) and none of the "
                    f"{len(on_disk)} slide(s) on disk match any of them. Refusing to "
                    f"delete them all — this is a parse failure, not an empty deck."
                )

        with _package_dir(slides_fd, "_rels") as slides_rels_fd:
            for name in on_disk:
                if name in referenced_slides:
                    continue
                os.unlink(name, dir_fd=slides_fd)
                removed.append(f"ppt/slides/{name}")

                rels_name = f"{name}.rels"
                if slides_rels_fd is not None and _is_regular(slides_rels_fd, rels_name):
                    os.unlink(rels_name, dir_fd=slides_rels_fd)
                    removed.append(f"ppt/slides/_rels/{rels_name}")

    if not removed:
        return removed

    with _package_dir(root_fd, "ppt/_rels") as pres_rels_fd:
        data = (
            _read_part(pres_rels_fd, "presentation.xml.rels")
            if pres_rels_fd is not None
            else None
        )
        if data is None:
            return removed

        rels_dom = defusedxml.minidom.parseString(data)
        source_part = rels_source_part(pres_rels_path, unpacked_dir)
        changed = False

        for rel in list(rels_dom.getElementsByTagName("Relationship")):
            if rel.getAttribute("Type") != SLIDE_REL_TYPE:
                continue
            part = opc_target(
                rel.getAttribute("Target"), source_part, rel.getAttribute("TargetMode")
            )
            if part is None:
                continue
            if posixpath.basename(part) not in referenced_slides:
                if rel.parentNode:
                    rel.parentNode.removeChild(rel)
                    changed = True

        if changed:
            _write_part(
                pres_rels_fd, "presentation.xml.rels", rels_dom.toxml(encoding="utf-8")
            )

    return removed


def remove_trash_directory(root_fd: int) -> list[str]:
    removed = []

    with _package_dir(root_fd, "[trash]") as trash_fd:
        if trash_fd is None:
            return []
        for name in _regular_files(trash_fd, "*"):
            os.unlink(name, dir_fd=trash_fd)
            removed.append(f"[trash]/{name}")

    try:
        os.rmdir("[trash]", dir_fd=root_fd)
    except OSError as e:
        if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise

    return removed


def _referenced_by(rels_files, unpacked_dir: Path) -> set:
    referenced = set()

    for rels_file in rels_files:
        source_part = rels_source_part(rels_file, unpacked_dir)
        dom = defusedxml.minidom.parse(str(rels_file))
        for rel in dom.getElementsByTagName("Relationship"):
            part = opc_target(
                rel.getAttribute("Target"), source_part, rel.getAttribute("TargetMode")
            )
            if part is not None:
                referenced.add(Path(part))

    return referenced


def remove_orphaned_rels_files(root_fd: int) -> list[str]:
    removed = []

    for dir_name in RELS_RESOURCE_DIRS:
        with _package_dir(root_fd, f"ppt/{dir_name}") as parent_fd:
            if parent_fd is None:
                continue
            with _package_dir(parent_fd, "_rels") as rels_fd:
                if rels_fd is None:
                    continue
                for name in _regular_files(rels_fd, "*.rels"):
                    if _lstat(parent_fd, name.replace(".rels", "")) is None:
                        os.unlink(name, dir_fd=rels_fd)
                        removed.append(f"ppt/{dir_name}/_rels/{name}")

    return removed


def get_referenced_files(unpacked_dir: Path) -> set:
    return _referenced_by(sorted(unpacked_dir.rglob("*.rels")), unpacked_dir)


def remove_orphaned_files(root_fd: int, referenced: set) -> list[str]:
    removed = []

    for dir_name in RESOURCE_DIRS:
        rel_dir = f"ppt/{dir_name}"
        with _package_dir(root_fd, rel_dir) as dir_fd:
            if dir_fd is None:
                continue
            for name in _regular_files(dir_fd, "*"):
                rel_path = f"{rel_dir}/{name}"
                if Path(rel_path) not in referenced:
                    os.unlink(name, dir_fd=dir_fd)
                    removed.append(rel_path)

    with _package_dir(root_fd, "ppt/theme") as theme_fd:
        if theme_fd is not None:
            with _package_dir(theme_fd, "_rels") as theme_rels_fd:
                for name in _regular_files(theme_fd, "theme*.xml"):
                    rel_path = f"ppt/theme/{name}"
                    if Path(rel_path) in referenced:
                        continue
                    os.unlink(name, dir_fd=theme_fd)
                    removed.append(rel_path)
                    rels_name = f"{name}.rels"
                    if theme_rels_fd is not None and _is_regular(theme_rels_fd, rels_name):
                        os.unlink(rels_name, dir_fd=theme_rels_fd)
                        removed.append(f"ppt/theme/_rels/{rels_name}")

    with _package_dir(root_fd, "ppt/notesSlides") as notes_fd:
        if notes_fd is not None:
            for name in _regular_files(notes_fd, "*.xml"):
                rel_path = f"ppt/notesSlides/{name}"
                if Path(rel_path) not in referenced:
                    os.unlink(name, dir_fd=notes_fd)
                    removed.append(rel_path)

            with _package_dir(notes_fd, "_rels") as notes_rels_fd:
                if notes_rels_fd is not None:
                    for name in _regular_files(notes_rels_fd, "*.rels"):
                        if _lstat(notes_fd, name.replace(".rels", "")) is None:
                            os.unlink(name, dir_fd=notes_rels_fd)
                            removed.append(f"ppt/notesSlides/_rels/{name}")

    return removed


def update_content_types(root_fd: int, removed_files: list[str]) -> None:
    data = _read_part(root_fd, "[Content_Types].xml")
    if data is None:
        return

    dom = defusedxml.minidom.parseString(data)
    changed = False

    for override in list(dom.getElementsByTagName("Override")):
        part_name = override.getAttribute("PartName").lstrip("/")
        if part_name in removed_files:
            if override.parentNode:
                override.parentNode.removeChild(override)
                changed = True

    if changed:
        _write_part(root_fd, "[Content_Types].xml", dom.toxml(encoding="utf-8"))


def clean_unused_files(unpacked_dir: Path) -> list[str]:
    root_fd = _open_root(unpacked_dir)
    try:
        return _clean(unpacked_dir, root_fd)
    finally:
        os.close(root_fd)


def _clean(unpacked_dir: Path, root_fd: int) -> list[str]:
    all_removed = []

    _refuse_links(root_fd)

    if list(unpacked_dir.rglob("*.rels")) and not get_referenced_files(unpacked_dir):
        raise RefusedToClean(
            "no relationship in this package names a part we can resolve. "
            "Refusing to treat every file as unreferenced."
        )

    slides_removed = remove_orphaned_slides(unpacked_dir, root_fd)
    all_removed.extend(slides_removed)

    trash_removed = remove_trash_directory(root_fd)
    all_removed.extend(trash_removed)

    while True:
        removed_rels = remove_orphaned_rels_files(root_fd)
        referenced = get_referenced_files(unpacked_dir)
        removed_files = remove_orphaned_files(root_fd, referenced)

        total_removed = removed_rels + removed_files
        if not total_removed:
            break

        all_removed.extend(total_removed)

    if all_removed:
        update_content_types(root_fd, all_removed)

    return all_removed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python clean.py <unpacked_dir>", file=sys.stderr)
        print("Example: python clean.py unpacked/", file=sys.stderr)
        sys.exit(1)

    unpacked_dir = Path(sys.argv[1])

    if not unpacked_dir.exists():
        print(f"Error: {unpacked_dir} not found", file=sys.stderr)
        sys.exit(1)

    try:
        removed = clean_unused_files(unpacked_dir)
    except (RefusedToClean, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Nothing was deleted.", file=sys.stderr)
        sys.exit(1)

    if removed:
        print(f"Removed {len(removed)} unreferenced files:")
        for f in removed:
            print(f"  {f}")
    else:
        print("No unreferenced files found")
