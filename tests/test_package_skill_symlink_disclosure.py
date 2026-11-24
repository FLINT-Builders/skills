"""Skill packaging must not copy symbolic-link targets into a .skill archive.

These tests fail while the packager follows links. They pass when a link is
skipped or rejected and the target's bytes never appear in the archive,
including when a regular file is replaced by a link after the walk stats it
and before it is read.

Run from the repository root:

    python3 -m unittest tests.test_package_skill_symlink_disclosure -v
"""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_CREATOR = REPO / "skills" / "skill-creator"

SKILL_MD = """---
name: planted-skill
description: Harmless notes skill for packaging.
---
# Notes
"""

# Audit hook state. sys.addaudithook cannot be removed, so the hook stays
# installed and this flag decides whether it rewrites the path under test.
_OPEN_SWAP = {
    "installed": False,
    "armed": False,
    "link_name": "",
    "target": "",
}


def _is_read_open(args):
    mode = args[1] if len(args) > 1 else None
    if isinstance(mode, str):
        return mode.startswith("r")
    flags = args[2] if len(args) > 2 and isinstance(args[2], int) else None
    if not isinstance(flags, int):
        return False
    return (flags & os.O_ACCMODE) != os.O_WRONLY


def _swap_before_open(event, args):
    """Replace a regular file with a symlink before the open that reads it."""
    if not _OPEN_SWAP["armed"] or event != "open" or not args:
        return
    path = os.fspath(args[0])
    if os.path.basename(path) != _OPEN_SWAP["link_name"]:
        return
    if not _is_read_open(args):
        return
    try:
        if os.path.islink(path):
            return
    except OSError:
        return
    _OPEN_SWAP["armed"] = False
    os.remove(path)
    os.symlink(_OPEN_SWAP["target"], path)


def _ensure_open_hook():
    if _OPEN_SWAP["installed"]:
        return
    sys.addaudithook(_swap_before_open)
    _OPEN_SWAP["installed"] = True


def _write_skill(root: Path) -> Path:
    skill = root / "planted-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD)
    return skill


def _run_packager(skill_dir: Path, output_dir: Path, env=None):
    command = [
        sys.executable,
        "-m",
        "scripts.package_skill",
        os.fspath(skill_dir),
        os.fspath(output_dir),
    ]
    return subprocess.run(
        command,
        cwd=SKILL_CREATOR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _archive_members(output_dir: Path):
    members = []
    if not output_dir.exists():
        return members
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    members.append((info.filename, archive.read(info)))
        except zipfile.BadZipFile:
            members.append((os.fspath(path), path.read_bytes()))
    return members


def _contains(output_dir: Path, needle: bytes) -> bool:
    return any(needle in payload for _, payload in _archive_members(output_dir))


class PackageSkillSymlinkDisclosureTests(unittest.TestCase):
    def _assert_not_disclosed(self, proc, output_dir: Path, needle: bytes):
        combined = f"{proc.stdout}\n{proc.stderr}"
        if "ModuleNotFoundError" in combined or "SKILL.md not found" in combined:
            self.fail(f"packager did not reach file packaging:\n{combined}")
        self.assertFalse(
            _contains(output_dir, needle),
            "skill archive contains the linked target contents",
        )

    def test_absolute_symlink_target_is_not_archived(self):
        needle = b"PLACEHOLDER_OUTSIDE_FILE\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "outside" / "credentials"
            secret.parent.mkdir()
            secret.write_bytes(needle)
            skill = _write_skill(root)
            os.symlink(secret, skill / "references" / "notes.md")
            output = root / "dist"
            proc = _run_packager(skill, output)
            self._assert_not_disclosed(proc, output, needle)

    def test_relative_symlink_escape_is_not_archived(self):
        needle = b"PLACEHOLDER_RELATIVE_ESCAPE\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "outside" / "credentials"
            secret.parent.mkdir()
            secret.write_bytes(needle)
            skill = _write_skill(root)
            # references/notes.md -> ../../outside/credentials
            os.symlink(
                "../../outside/credentials",
                skill / "references" / "notes.md",
            )
            output = root / "dist"
            proc = _run_packager(skill, output)
            self._assert_not_disclosed(proc, output, needle)

    def test_symlink_chain_is_not_archived(self):
        needle = b"PLACEHOLDER_LINK_CHAIN\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "outside" / "credentials"
            secret.parent.mkdir()
            secret.write_bytes(needle)
            skill = _write_skill(root)
            hop = skill / "references" / "hop"
            os.symlink(secret, hop)
            os.symlink("hop", skill / "references" / "notes.md")
            output = root / "dist"
            proc = _run_packager(skill, output)
            self._assert_not_disclosed(proc, output, needle)

    def test_proc_environ_symlink_is_not_archived(self):
        marker = "SKILL_PACKAGER_MARKER=placeholder-marker-value"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _write_skill(root)
            os.symlink("/proc/self/environ", skill / "references" / "notes.md")
            output = root / "dist"
            env = os.environ.copy()
            env["SKILL_PACKAGER_MARKER"] = "placeholder-marker-value"
            proc = _run_packager(skill, output, env=env)
            self._assert_not_disclosed(proc, output, marker.encode())

    def test_git_clone_then_package_does_not_archive_symlink_target(self):
        needle = b"PLACEHOLDER_CLONED_LINK\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "outside" / "credentials"
            secret.parent.mkdir()
            secret.write_bytes(needle)
            origin = root / "origin"
            skill = _write_skill(origin)
            os.symlink(secret, skill / "references" / "notes.md")
            self._git(origin, "init", "-q")
            self._git(origin, "config", "user.email", "packager-test@example.com")
            self._git(origin, "config", "user.name", "Packager Test")
            self._git(origin, "add", "-A")
            self._git(origin, "commit", "-q", "-m", "plant link")
            clone = root / "clone"
            self._git(root, "clone", "-q", os.fspath(origin), os.fspath(clone))
            cloned_skill = clone / "planted-skill"
            link = cloned_skill / "references" / "notes.md"
            self.assertTrue(
                link.is_symlink(),
                "git clone did not keep the symbolic link",
            )
            output = root / "dist"
            proc = _run_packager(cloned_skill, output)
            self._assert_not_disclosed(proc, output, needle)

    def test_cp_a_then_package_does_not_archive_symlink_target(self):
        needle = b"PLACEHOLDER_COPIED_LINK\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "outside" / "credentials"
            secret.parent.mkdir()
            secret.write_bytes(needle)
            skill = _write_skill(root)
            os.symlink(secret, skill / "references" / "notes.md")
            copied_parent = root / "copy"
            copied_parent.mkdir()
            subprocess.run(
                ["cp", "-a", os.fspath(skill), os.fspath(copied_parent)],
                check=True,
            )
            copied = copied_parent / "planted-skill"
            link = copied / "references" / "notes.md"
            self.assertTrue(link.is_symlink(), "cp -a did not keep the symbolic link")
            output = root / "dist"
            proc = _run_packager(copied, output)
            self._assert_not_disclosed(proc, output, needle)

    def test_open_does_not_follow_link_swapped_after_walk(self):
        """Fail if the read opens the path again after stat.

        The file is a regular file while the directory is walked. The open
        that copies bytes into the archive is swapped to a symlink first.
        Skipping links only when stat runs still archives the target.
        """
        needle = b"PLACEHOLDER_OPEN_SWAP\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "outside" / "credentials"
            secret.parent.mkdir()
            secret.write_bytes(needle)
            skill = _write_skill(root)
            notes = skill / "references" / "notes.md"
            notes.write_text("benign notes\n")
            output = root / "dist"
            output.mkdir()
            _ensure_open_hook()
            _OPEN_SWAP["link_name"] = notes.name
            _OPEN_SWAP["target"] = os.fspath(secret)
            _OPEN_SWAP["armed"] = True
            sys.path.insert(0, os.fspath(SKILL_CREATOR))
            try:
                from scripts.package_skill import package_skill

                package_skill(skill, output)
            finally:
                _OPEN_SWAP["armed"] = False
            self.assertFalse(
                _contains(output, needle),
                "opening the path followed a symlink created after the walk",
            )

    def _git(self, cwd: Path, *args: str):
        subprocess.run(
            ["git", "-C", os.fspath(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
