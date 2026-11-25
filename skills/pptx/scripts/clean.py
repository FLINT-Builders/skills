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

A package that contains a symbolic link anywhere is refused before anything
is deleted.
"""

import fnmatch
import posixpath
import re
import stat
import sys
from pathlib import Path

import defusedxml.minidom

from office.helpers import SLIDE_REL_TYPE, PackageDir, opc_target, rels_source_part

PRESENTATION = "ppt/presentation.xml"
PRESENTATION_RELS = "ppt/_rels/presentation.xml.rels"
CONTENT_TYPES = "[Content_Types].xml"


def _source_part(pkg: PackageDir, rels_file: str) -> str:
    return rels_source_part(pkg.root / rels_file, pkg.root)


def _slide_rids(pkg: PackageDir) -> dict[str, str]:
    source_part = _source_part(pkg, PRESENTATION_RELS)
    rels_dom = defusedxml.minidom.parseString(pkg.read_bytes(PRESENTATION_RELS))

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


def get_slides_in_sldidlst(pkg: PackageDir) -> set[str]:
    if not pkg.is_file(PRESENTATION) or not pkg.is_file(PRESENTATION_RELS):
        return set()

    rid_to_slide = _slide_rids(pkg)

    pres_content = pkg.read_text(PRESENTATION)
    referenced_rids = set(re.findall(r'<p:sldId[^>]*r:id="([^"]+)"', pres_content))

    return {
        posixpath.basename(rid_to_slide[rid])
        for rid in referenced_rids
        if rid in rid_to_slide
    }


class RefusedToClean(Exception):
    """The package does not look the way a readable package should."""


def _files(pkg: PackageDir, rel_dir: str, pattern: str) -> list[str]:
    return [
        name
        for name, st in pkg.entries(rel_dir)
        if stat.S_ISREG(st.st_mode) and fnmatch.fnmatchcase(name, pattern)
    ]


def remove_orphaned_slides(pkg: PackageDir) -> list[str]:
    slides_dir = "ppt/slides"
    slides_rels_dir = f"{slides_dir}/_rels"

    if not pkg.has_dir(slides_dir):
        return []

    referenced_slides = get_slides_in_sldidlst(pkg)
    on_disk = _files(pkg, slides_dir, "slide*.xml")

    if on_disk and not any(s in referenced_slides for s in on_disk):
        listed = re.findall(
            r'<p:sldId[^>]*r:id="([^"]+)"',
            pkg.read_text(PRESENTATION) if pkg.is_file(PRESENTATION) else "",
        )
        if listed:
            raise RefusedToClean(
                f"<p:sldIdLst> lists {len(listed)} slide(s) and none of the "
                f"{len(on_disk)} slide(s) on disk match any of them. Refusing to "
                f"delete them all — this is a parse failure, not an empty deck."
            )

    removed = []

    for slide_name in on_disk:
        if slide_name not in referenced_slides:
            rel_path = f"{slides_dir}/{slide_name}"
            pkg.unlink(rel_path)
            removed.append(rel_path)

            rels_file = f"{slides_rels_dir}/{slide_name}.rels"
            if pkg.is_file(rels_file):
                pkg.unlink(rels_file)
                removed.append(rels_file)

    if removed and pkg.is_file(PRESENTATION_RELS):
        rels_dom = defusedxml.minidom.parseString(pkg.read_bytes(PRESENTATION_RELS))
        source_part = _source_part(pkg, PRESENTATION_RELS)
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
            pkg.write_bytes(PRESENTATION_RELS, rels_dom.toxml(encoding="utf-8"))

    return removed


def remove_trash_directory(pkg: PackageDir) -> list[str]:
    trash_dir = "[trash]"
    removed = []

    if pkg.has_dir(trash_dir):
        for name in _files(pkg, trash_dir, "*"):
            rel_path = f"{trash_dir}/{name}"
            removed.append(rel_path)
            pkg.unlink(rel_path)
        pkg.rmdir(trash_dir)

    return removed


def _referenced_by(pkg: PackageDir, rels_files) -> set:
    referenced = set()

    for rels_file in rels_files:
        source_part = _source_part(pkg, rels_file)
        dom = defusedxml.minidom.parseString(pkg.read_bytes(rels_file))
        for rel in dom.getElementsByTagName("Relationship"):
            part = opc_target(
                rel.getAttribute("Target"), source_part, rel.getAttribute("TargetMode")
            )
            if part is not None:
                referenced.add(Path(part))

    return referenced


def remove_orphaned_rels_files(pkg: PackageDir) -> list[str]:
    resource_dirs = ["charts", "diagrams", "drawings"]
    removed = []

    for dir_name in resource_dirs:
        rels_dir = f"ppt/{dir_name}/_rels"
        if not pkg.has_dir(rels_dir):
            continue

        for name in _files(pkg, rels_dir, "*.rels"):
            resource_file = f"ppt/{dir_name}/{name.replace('.rels', '')}"
            if pkg.lstat(resource_file) is None:
                pkg.unlink(f"{rels_dir}/{name}")
                removed.append(f"{rels_dir}/{name}")

    return removed


def _all_rels_files(pkg: PackageDir) -> list[str]:
    return sorted(f for f in pkg.walk_files() if f.endswith(".rels"))


def get_referenced_files(pkg: PackageDir) -> set:
    return _referenced_by(pkg, _all_rels_files(pkg))


def remove_orphaned_files(pkg: PackageDir, referenced: set) -> list[str]:
    resource_dirs = ["media", "embeddings", "charts", "diagrams", "tags", "drawings", "ink"]
    removed = []

    for dir_name in resource_dirs:
        dir_path = f"ppt/{dir_name}"
        for name in _files(pkg, dir_path, "*"):
            rel_path = f"{dir_path}/{name}"
            if Path(rel_path) not in referenced:
                pkg.unlink(rel_path)
                removed.append(rel_path)

    theme_dir = "ppt/theme"
    for name in _files(pkg, theme_dir, "theme*.xml"):
        rel_path = f"{theme_dir}/{name}"
        if Path(rel_path) not in referenced:
            pkg.unlink(rel_path)
            removed.append(rel_path)
            theme_rels = f"{theme_dir}/_rels/{name}.rels"
            if pkg.is_file(theme_rels):
                pkg.unlink(theme_rels)
                removed.append(theme_rels)

    notes_dir = "ppt/notesSlides"
    if pkg.has_dir(notes_dir):
        for name in _files(pkg, notes_dir, "*.xml"):
            rel_path = f"{notes_dir}/{name}"
            if Path(rel_path) not in referenced:
                pkg.unlink(rel_path)
                removed.append(rel_path)

        notes_rels_dir = f"{notes_dir}/_rels"
        for name in _files(pkg, notes_rels_dir, "*.rels"):
            notes_file = f"{notes_dir}/{name.replace('.rels', '')}"
            if pkg.lstat(notes_file) is None:
                pkg.unlink(f"{notes_rels_dir}/{name}")
                removed.append(f"{notes_rels_dir}/{name}")

    return removed


def update_content_types(pkg: PackageDir, removed_files: list[str]) -> None:
    if not pkg.is_file(CONTENT_TYPES):
        return

    dom = defusedxml.minidom.parseString(pkg.read_bytes(CONTENT_TYPES))
    changed = False

    for override in list(dom.getElementsByTagName("Override")):
        part_name = override.getAttribute("PartName").lstrip("/")
        if part_name in removed_files:
            if override.parentNode:
                override.parentNode.removeChild(override)
                changed = True

    if changed:
        pkg.write_bytes(CONTENT_TYPES, dom.toxml(encoding="utf-8"))


def clean_unused_files(unpacked_dir: Path) -> list[str]:
    with PackageDir(unpacked_dir) as pkg:
        return _clean(pkg)


def _clean(pkg: PackageDir) -> list[str]:
    all_removed = []

    # walk_files refuses a symbolic link anywhere in the tree, so nothing is
    # deleted from a package that has one.
    if _all_rels_files(pkg) and not get_referenced_files(pkg):
        raise RefusedToClean(
            "no relationship in this package names a part we can resolve. "
            "Refusing to treat every file as unreferenced."
        )
    for rel in (PRESENTATION, PRESENTATION_RELS, CONTENT_TYPES):
        pkg.is_file(rel)

    slides_removed = remove_orphaned_slides(pkg)
    all_removed.extend(slides_removed)

    trash_removed = remove_trash_directory(pkg)
    all_removed.extend(trash_removed)

    while True:
        removed_rels = remove_orphaned_rels_files(pkg)
        referenced = get_referenced_files(pkg)
        removed_files = remove_orphaned_files(pkg, referenced)

        total_removed = removed_rels + removed_files
        if not total_removed:
            break

        all_removed.extend(total_removed)

    if all_removed:
        update_content_types(pkg, all_removed)

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
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if removed:
        print(f"Removed {len(removed)} unreferenced files:")
        for f in removed:
            print(f"  {f}")
    else:
        print("No unreferenced files found")
