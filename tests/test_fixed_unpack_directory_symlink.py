"""Unpacking a deck or document must not follow a pre-created `unpacked` symlink.

The editing recipes write every archive member through whatever `unpacked`
already is. These tests fail until each recipe refuses a symbolic link at
that path and leaves the link target untouched, including when the directory
is replaced with a symbolic link after a name lookup and before the write.

A fresh output directory, with no symlink in the way, still receives the
archive.

Run from the repository root:

    python3 tests/test_fixed_unpack_directory_symlink.py
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PPTX_SKILL = REPO / "skills" / "pptx" / "SKILL.md"
DOCX_SKILL = REPO / "skills" / "docx" / "SKILL.md"
HOOK_SRC = Path(__file__).resolve().parent / "toctou_swap.c"

ORIGINAL = b"placeholder-original-marker\n"
ATTACKER = b"placeholder-attacker-marker\n"
SLIDE = b"<p:sld>placeholder-slide</p:sld>"
DOCUMENT = b"<w:document>placeholder-document</w:document>"

HOOK_SO: Path | None = None


def setUpModule() -> None:
    global HOOK_SO
    directory = Path(tempfile.mkdtemp(prefix="unpack-hook-"))
    so = directory / "toctou_swap.so"
    compiled = subprocess.run(
        [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-D_GNU_SOURCE",
            "-Werror=implicit-function-declaration",
            str(HOOK_SRC),
            "-o",
            str(so),
            "-ldl",
        ],
        capture_output=True,
        text=True,
    )
    if compiled.returncode != 0:
        raise RuntimeError(compiled.stderr)
    HOOK_SO = so


def recipe_command(skill_md: Path, heading: str) -> str:
    text = skill_md.read_text(encoding="utf-8")
    try:
        section = text.split(heading, 1)[1]
    except IndexError as exc:
        raise AssertionError(f"{skill_md} has no section {heading}") from exc
    section = section.split("\n## ", 1)[0]
    fence = section.split("```bash\n", 1)
    if len(fence) != 2:
        raise AssertionError(f"{skill_md} section {heading} has no bash recipe")
    body = fence[1].split("```", 1)[0]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    raise AssertionError(f"{skill_md} section {heading} has no command")


def snapshot(root: Path) -> dict[str, tuple]:
    """File type, mode, and bytes under root. Directory symlinks are not followed."""
    recorded: dict[str, tuple] = {}
    if not root.exists():
        return recorded
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            path = Path(dirpath) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                recorded[rel] = ("symlink", os.readlink(path), stat.S_IMODE(info.st_mode))
            else:
                recorded[rel + "/"] = ("dir", stat.S_IMODE(info.st_mode))
        for name in filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                recorded[rel] = ("symlink", os.readlink(path), stat.S_IMODE(info.st_mode))
            else:
                recorded[rel] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
    return recorded


def write_archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def restore_unpacked(work: Path) -> None:
    unpacked = work / "unpacked"
    held = work / "unpacked.held"
    if unpacked.is_symlink():
        unpacked.unlink()
    if held.exists():
        if unpacked.exists() or unpacked.is_symlink():
            if unpacked.is_dir() and not unpacked.is_symlink():
                shutil.rmtree(unpacked)
            else:
                unpacked.unlink()
        held.rename(unpacked)
    marker = work / ".toctou-swapped"
    if marker.exists():
        marker.unlink()


class FixedUnpackDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="unpack-symlink-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def work_and_home(self) -> tuple[Path, Path]:
        work = self.tmp / "work"
        home = self.tmp / "home"
        work.mkdir()
        home.mkdir()
        return work, home

    def plant_keys(self, home: Path, mode: int = 0o600) -> Path:
        keys = home / ".ssh" / "authorized_keys"
        keys.parent.mkdir(mode=0o700)
        keys.write_bytes(ORIGINAL)
        keys.chmod(mode)
        return keys

    def link_unpacked(self, work: Path, home: Path) -> Path:
        link = work / "unpacked"
        link.symlink_to(home)
        return link

    def run_recipe(
        self,
        work: Path,
        command: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("LD_PRELOAD", None)
        env.pop("PPTX_UNPACK_TOCTOU_WORKDIR", None)
        env.pop("PPTX_UNPACK_TOCTOU_TARGET", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", "-c", command],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
        )

    def assert_home_unchanged(self, home: Path, before: dict[str, tuple], proc: subprocess.CompletedProcess[str]) -> None:
        after = snapshot(home)
        self.assertEqual(
            before,
            after,
            f"archive members reached the symlink target\n"
            f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )

    def assert_symlink_kept(self, link: Path, home: Path) -> None:
        self.assertTrue(link.is_symlink(), f"{link} is no longer a symlink")
        self.assertEqual(os.path.realpath(link), str(home.resolve()))

    def prepare_skill_scripts(self, work: Path, skill_dir: Path) -> None:
        os.symlink(skill_dir / "scripts", work / "scripts")

    def test_pptx_symlink_does_not_overwrite_existing_dotfile(self) -> None:
        work, home = self.work_and_home()
        self.plant_keys(home)
        link = self.link_unpacked(work, home)
        write_archive(
            work / "deck.pptx",
            {".ssh/authorized_keys": ATTACKER, "ppt/slides/slide1.xml": SLIDE},
        )
        self.prepare_skill_scripts(work, PPTX_SKILL.parent)
        before = snapshot(home)
        command = recipe_command(PPTX_SKILL, "## Editing existing decks and templates")
        proc = self.run_recipe(work, command)
        self.assert_home_unchanged(home, before, proc)
        self.assert_symlink_kept(link, home)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)

    def test_pptx_symlink_does_not_create_missing_parents(self) -> None:
        work, home = self.work_and_home()
        (home / "sentinel.txt").write_bytes(ORIGINAL)
        link = self.link_unpacked(work, home)
        write_archive(
            work / "deck.pptx",
            {".ssh/authorized_keys": ATTACKER, "ppt/slides/slide1.xml": SLIDE},
        )
        self.prepare_skill_scripts(work, PPTX_SKILL.parent)
        before = snapshot(home)
        command = recipe_command(PPTX_SKILL, "## Editing existing decks and templates")
        proc = self.run_recipe(work, command)
        self.assert_home_unchanged(home, before, proc)
        self.assert_symlink_kept(link, home)
        self.assertFalse((home / ".ssh").exists())
        self.assertNotEqual(proc.returncode, 0, proc.stderr)

    def test_pptx_fresh_directory_still_unpacks(self) -> None:
        work, _home = self.work_and_home()
        write_archive(work / "deck.pptx", {"ppt/slides/slide1.xml": SLIDE})
        self.prepare_skill_scripts(work, PPTX_SKILL.parent)
        command = recipe_command(PPTX_SKILL, "## Editing existing decks and templates")
        proc = self.run_recipe(work, command)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        unpacked = work / "unpacked"
        self.assertTrue(unpacked.is_dir())
        self.assertFalse(unpacked.is_symlink())
        self.assertEqual((unpacked / "ppt" / "slides" / "slide1.xml").read_bytes(), SLIDE)

    def test_docx_symlink_does_not_overwrite_existing_dotfile(self) -> None:
        work, home = self.work_and_home()
        self.plant_keys(home)
        link = self.link_unpacked(work, home)
        write_archive(
            work / "doc.docx",
            {".ssh/authorized_keys": ATTACKER, "word/document.xml": DOCUMENT},
        )
        self.prepare_skill_scripts(work, DOCX_SKILL.parent)
        before = snapshot(home)
        command = recipe_command(DOCX_SKILL, "## Editing existing documents")
        proc = self.run_recipe(work, command)
        self.assert_home_unchanged(home, before, proc)
        self.assert_symlink_kept(link, home)
        self.assertNotEqual(proc.returncode, 0, proc.stderr)

    def test_docx_symlink_does_not_create_missing_parents(self) -> None:
        work, home = self.work_and_home()
        (home / "sentinel.txt").write_bytes(ORIGINAL)
        link = self.link_unpacked(work, home)
        write_archive(
            work / "doc.docx",
            {".ssh/authorized_keys": ATTACKER, "word/document.xml": DOCUMENT},
        )
        self.prepare_skill_scripts(work, DOCX_SKILL.parent)
        before = snapshot(home)
        command = recipe_command(DOCX_SKILL, "## Editing existing documents")
        proc = self.run_recipe(work, command)
        self.assert_home_unchanged(home, before, proc)
        self.assert_symlink_kept(link, home)
        self.assertFalse((home / ".ssh").exists())
        self.assertNotEqual(proc.returncode, 0, proc.stderr)

    def test_docx_fresh_directory_still_unpacks(self) -> None:
        work, _home = self.work_and_home()
        write_archive(work / "doc.docx", {"word/document.xml": DOCUMENT})
        self.prepare_skill_scripts(work, DOCX_SKILL.parent)
        command = recipe_command(DOCX_SKILL, "## Editing existing documents")
        proc = self.run_recipe(work, command)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        unpacked = work / "unpacked"
        self.assertTrue(unpacked.is_dir())
        self.assertFalse(unpacked.is_symlink())
        self.assertEqual((unpacked / "word" / "document.xml").read_bytes(), DOCUMENT)

    def toctou_env(self, work: Path, home: Path) -> dict[str, str]:
        assert HOOK_SO is not None
        return {
            "LD_PRELOAD": str(HOOK_SO),
            "PPTX_UNPACK_TOCTOU_WORKDIR": str(work),
            "PPTX_UNPACK_TOCTOU_TARGET": str(home),
        }

    def assert_hook_arms(self, work: Path, env: dict[str, str]) -> None:
        proc = subprocess.run(
            ["python3", "-c", "import os; os.stat('unpacked')"],
            cwd=work,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((work / ".toctou-swapped").is_file(), "lookup hook did not replace unpacked")
        self.assertTrue((work / "unpacked").is_symlink())
        restore_unpacked(work)
        self.assertTrue((work / "unpacked").is_dir())
        self.assertFalse((work / "unpacked").is_symlink())

    def test_pptx_swap_after_lookup_does_not_write_through_link(self) -> None:
        work, home = self.work_and_home()
        # The link target has no .ssh yet. Creating that directory and the
        # archive member under it is the failure, including when the swap
        # happens on the lookup inside extraction rather than before it.
        (home / "sentinel.txt").write_bytes(ORIGINAL)
        (work / "unpacked").mkdir()
        write_archive(
            work / "deck.pptx",
            {".ssh/authorized_keys": ATTACKER, "ppt/slides/slide1.xml": SLIDE},
        )
        self.prepare_skill_scripts(work, PPTX_SKILL.parent)
        env = self.toctou_env(work, home)
        self.assert_hook_arms(work, env)
        before = snapshot(home)
        command = recipe_command(PPTX_SKILL, "## Editing existing decks and templates")
        proc = self.run_recipe(work, command, env)
        self.assert_home_unchanged(home, before, proc)

    def test_docx_swap_after_lookup_does_not_write_through_link(self) -> None:
        work, home = self.work_and_home()
        (home / "sentinel.txt").write_bytes(ORIGINAL)
        (work / "unpacked").mkdir()
        write_archive(
            work / "doc.docx",
            {".ssh/authorized_keys": ATTACKER, "word/document.xml": DOCUMENT},
        )
        self.prepare_skill_scripts(work, DOCX_SKILL.parent)
        env = self.toctou_env(work, home)
        self.assert_hook_arms(work, env)
        before = snapshot(home)
        command = recipe_command(DOCX_SKILL, "## Editing existing documents")
        proc = self.run_recipe(work, command, env)
        self.assert_home_unchanged(home, before, proc)


if __name__ == "__main__":
    unittest.main()
