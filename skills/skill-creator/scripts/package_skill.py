#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable .skill file of a skill folder

Usage:
    python utils/package_skill.py <path/to/skill-folder> [output-directory]

Example:
    python utils/package_skill.py skills/public/my-skill
    python utils/package_skill.py skills/public/my-skill ./dist
"""

import errno
import fnmatch
import os
import stat
import sys
import time
import zipfile
from pathlib import Path
from scripts.quick_validate import validate_skill

# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # rel_path is relative to skill_path.parent, so parts[0] is the skill
    # folder name and parts[1] (if present) is the first subdir.
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


class UnsafeSkillFileError(Exception):
    """A file was swapped or replaced between the directory walk and the read."""


# With dir_fd support every lookup is relative to an already-open parent
# directory, so a parent swapped for a link mid-walk cannot redirect a read.
_HAS_DIR_FD = (
    hasattr(os, "fwalk")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
)
# O_NONBLOCK keeps a FIFO swapped in after the walk from blocking the open.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _is_link(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _walk_skill(skill_path: Path):
    """Yield (relative path, lstat result, open name, dir_fd) for each non-directory.

    Links to directories are yielded as entries and never descended into.
    """
    if _HAS_DIR_FD:
        for dirpath, dirnames, filenames, dirfd in os.fwalk(os.fspath(skill_path)):
            rel_dir = Path(dirpath).relative_to(skill_path)
            for name in list(dirnames):
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode):
                    dirnames.remove(name)
                    yield rel_dir / name, st, name, dirfd
            for name in filenames:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                yield rel_dir / name, st, name, dirfd
    else:
        for dirpath, dirnames, filenames in os.walk(skill_path):
            rel_dir = Path(dirpath).relative_to(skill_path)
            for name in list(dirnames):
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                if _is_link(st) or not stat.S_ISDIR(st.st_mode):
                    dirnames.remove(name)
                    yield rel_dir / name, st, full, None
            for name in filenames:
                full = os.path.join(dirpath, name)
                yield rel_dir / name, os.lstat(full), full, None


def _add_file(zipf: zipfile.ZipFile, name, dir_fd, walked: os.stat_result, arcname: Path):
    """Copy the file the walk saw into the archive, refusing if it was replaced."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafeSkillFileError(
                f"{arcname} became a symlink while packaging; refusing to read it"
            ) from e
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(walked, opened):
            raise UnsafeSkillFileError(
                f"{arcname} was replaced while packaging; refusing to read it"
            )
        zinfo = zipfile.ZipInfo(arcname.as_posix(), time.localtime(opened.st_mtime)[:6])
        zinfo.external_attr = (opened.st_mode & 0xFFFF) << 16
        zinfo.file_size = opened.st_size
        zinfo.compress_type = zipfile.ZIP_DEFLATED
        with zipf.open(zinfo, "w") as dest:
            while chunk := os.read(fd, 1024 * 64):
                dest.write(chunk)
    finally:
        os.close(fd)


def package_skill(skill_path, output_dir=None):
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the .skill file (defaults to current directory)

    Returns:
        Path to the created .skill file, or None if error
    """
    skill_path = Path(skill_path).resolve()

    # Validate skill folder exists
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None

    # Validate SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return None

    # Run validation before packaging
    print("🔍 Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return None
    print(f"✅ {message}\n")

    # Determine output location
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"

    # Create the .skill file (zip format)
    created = False
    try:
        with zipfile.ZipFile(skill_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            created = True
            # Walk through the skill directory, excluding build artifacts.
            # Symlinks are never followed: their targets may live anywhere on
            # this machine (e.g. /proc/self/environ or credential files).
            for rel_path, st, name, dir_fd in _walk_skill(skill_path):
                arcname = Path(skill_name) / rel_path
                if should_exclude(arcname):
                    print(f"  Skipped: {arcname}")
                    continue
                if _is_link(st):
                    print(f"  ⚠️  Skipped symlink (not followed): {arcname}")
                    continue
                if not stat.S_ISREG(st.st_mode):
                    print(f"  ⚠️  Skipped non-regular file: {arcname}")
                    continue
                _add_file(zipf, name, dir_fd, st, arcname)
                print(f"  Added: {arcname}")

        print(f"\n✅ Successfully packaged skill to: {skill_filename}")
        return skill_filename

    except Exception as e:
        print(f"❌ Error creating .skill file: {e}")
        if created:
            skill_filename.unlink(missing_ok=True)
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python utils/package_skill.py <path/to/skill-folder> [output-directory]")
        print("\nExample:")
        print("  python utils/package_skill.py skills/public/my-skill")
        print("  python utils/package_skill.py skills/public/my-skill ./dist")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"📦 Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
