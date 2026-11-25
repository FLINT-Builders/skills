"""Output prefixes for thumbnail grids must stay in the working directory.

The checks below are the spec for thumbnail.py. They fail while a caller-supplied
prefix can create directories, replace a JPEG outside the working directory, or
follow a symlink at the destination. A safe single-segment name still writes a
grid, including a numbered grid and a replacement of a regular file in the
working directory.

Rasterizing the deck is not part of the prefix path: convert_to_images never
receives the prefix (thumbnail.py). Tests that go through main() substitute a
local JPEG for that rasterization step so argument parsing, get_slide_info,
build_slide_list, and create_grids still run. Direct create_grids tests cover
both the single-file write and the numbered-grid write.

Run from the repository root:

    python3 -m unittest skills/pptx/scripts/test_thumbnail_prefix_traversal.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import thumbnail  # noqa: E402
from PIL import Image  # noqa: E402

_MARKER = b"TRUSTED-IMAGE-NOT-A-GRID"
_OUTPUT_NAME = "grid.jpg"
_TOCTOU_BASENAME = _OUTPUT_NAME

# Lie only to lstat of the destination name. Python 3.12 os.lstat / Path.resolve
# bind lstat64 (posixpath.realpath -> os.lstat). A following open is left alone,
# so a check that stats the path and then saves through Pillow still replaces
# the symlink target. Pillow Image.save stats with os.path.exists and then
# builtins.open(..., "w+b"), both of which follow the link.
_LSTAT_LIE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static int ends_with_sentinel(const char *path) {
    const char *env = getenv("THUMB_TOCTOU_BASENAME");
    if (!env || !path) return 0;
    size_t n = strlen(env);
    size_t m = strlen(path);
    if (m == n && strcmp(path, env) == 0) return 1;
    if (m > n && path[m - n - 1] == '/' && strcmp(path + (m - n), env) == 0)
        return 1;
    return 0;
}

int lstat64(const char *pathname, struct stat64 *statbuf) {
    static int (*real_lstat64)(const char *, struct stat64 *) = NULL;
    if (!real_lstat64) real_lstat64 = dlsym(RTLD_NEXT, "lstat64");
    int rc = real_lstat64(pathname, statbuf);
    if (rc == 0 && ends_with_sentinel(pathname)) {
        statbuf->st_mode = (statbuf->st_mode & ~S_IFMT) | S_IFREG;
    }
    return rc;
}
"""


def _write_pptx(path: Path) -> None:
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""
    presentation = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>
"""
    slide = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" show="1"/>
"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/_rels/presentation.xml.rels", rels)
        zf.writestr("ppt/presentation.xml", presentation)
        zf.writestr("ppt/slides/slide1.xml", slide)


def _state(path: Path):
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    if path.is_file():
        return ("file", path.read_bytes())
    if path.is_dir():
        return ("dir",)
    return ("other",)


def _toctou_child() -> None:
    work = Path(os.environ["THUMB_WORK"])
    os.chdir(work)
    slide = Path(os.environ["THUMB_SLIDE"])
    try:
        thumbnail.create_grids(
            [(slide, "slide1.xml")],
            3,
            40,
            Path(os.environ["THUMB_OUTPUT_NAME"]),
        )
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            raise SystemExit(1) from exc
        raise
    except Exception:
        raise SystemExit(2) from None


class ThumbnailPrefixTests(unittest.TestCase):
    maxDiff = 400

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self.root = Path(tempfile.mkdtemp(prefix="thumb-prefix-"))
        self.work = self.root / "work"
        self.work.mkdir()
        self.slide = self.work / "slide-src.jpg"
        Image.new("RGB", (32, 18), (200, 10, 10)).save(self.slide, "JPEG")
        self.pptx = self.work / "deck.pptx"
        _write_pptx(self.pptx)
        os.chdir(self.work)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        shutil.rmtree(self.root, ignore_errors=True)

    def _slides(self, count: int) -> list[tuple[Path, str]]:
        return [(self.slide, f"slide{i}.xml") for i in range(count)]

    def _run_blocked(self, fn, protected: list[Path]) -> None:
        before = [(path, _state(path)) for path in protected]
        refused: BaseException | None = None
        try:
            fn()
        except (Exception, SystemExit) as exc:
            refused = exc
        for path, state in before:
            self.assertEqual(_state(path), state, f"{path} was created or modified")
        self.assertIsNotNone(refused, "unsafe output prefix was accepted")
        if isinstance(refused, SystemExit):
            self.assertNotIn(refused.code, (0, None), "unsafe prefix exited successfully")

    def _call_main(self, prefix: str | None) -> None:
        def fake_convert(pptx_path, temp_dir):
            dest = Path(temp_dir) / "slide-1.jpg"
            shutil.copyfile(self.slide, dest)
            return [dest]

        argv = ["thumbnail.py", str(self.pptx)]
        if prefix is not None:
            argv.append(prefix)
        with mock.patch.object(thumbnail, "convert_to_images", fake_convert):
            old_argv = sys.argv
            sys.argv = argv
            try:
                thumbnail.main()
            finally:
                sys.argv = old_argv

    def test_safe_prefix_writes_grid_and_replaces_regular_file(self) -> None:
        existing = self.work / "grid.jpg"
        existing.write_bytes(_MARKER)
        thumbnail.create_grids(self._slides(1), 3, 40, Path("grid.jpg"))
        self.assertTrue(existing.is_file())
        self.assertFalse(existing.is_symlink())
        self.assertNotEqual(existing.read_bytes(), _MARKER)
        self.assertTrue(existing.read_bytes().startswith(b"\xff\xd8"))

    def test_safe_prefix_writes_numbered_grids(self) -> None:
        thumbnail.create_grids(self._slides(13), 3, 40, Path("grid.jpg"))
        first = self.work / "grid-1.jpg"
        second = self.work / "grid-2.jpg"
        self.assertTrue(first.is_file() and not first.is_symlink())
        self.assertTrue(second.is_file() and not second.is_symlink())
        self.assertFalse((self.work / "grid.jpg").exists())

    def test_main_safe_prefix_uses_argv(self) -> None:
        self._call_main("grid")
        written = self.work / "grid.jpg"
        self.assertTrue(written.is_file() and not written.is_symlink())
        self.assertTrue(written.read_bytes().startswith(b"\xff\xd8"))
        info = thumbnail.get_slide_info(self.pptx)
        self.assertEqual(info, [{"name": "slide1.xml", "hidden": False}])

    def test_parent_prefix_does_not_replace_existing_jpeg(self) -> None:
        victim = self.root / "outside" / "deck.jpg"
        victim.parent.mkdir()
        victim.write_bytes(_MARKER)
        self._run_blocked(
            lambda: thumbnail.create_grids(
                self._slides(1), 3, 40, Path("../outside/deck.jpg")
            ),
            [victim],
        )

    def test_parent_prefix_does_not_create_missing_directories(self) -> None:
        created = self.root / "created"
        self._run_blocked(
            lambda: thumbnail.create_grids(
                self._slides(1), 3, 40, Path("../created/deck.jpg")
            ),
            [created, created / "deck.jpg"],
        )

    def test_absolute_prefix_does_not_write(self) -> None:
        victim = self.root / "absolute-deck.jpg"
        victim.write_bytes(_MARKER)
        output = Path(str(victim))  # already ends with .jpg; main appends .jpg
        # main() does Path(f"{prefix}.jpg"), so the absolute prefix has no suffix.
        prefix = str(victim)[: -len(".jpg")]
        self.assertEqual(Path(f"{prefix}.jpg"), output)
        self._run_blocked(
            lambda: thumbnail.create_grids(self._slides(1), 3, 40, Path(f"{prefix}.jpg")),
            [victim],
        )

    def test_dotdot_inside_prefix_does_not_escape(self) -> None:
        victim = self.root / "nested-escape.jpg"
        victim.write_bytes(_MARKER)
        created = self.root / "subdir"
        prefix = "subdir/../../nested-escape"
        self._run_blocked(
            lambda: thumbnail.create_grids(
                self._slides(1), 3, 40, Path(f"{prefix}.jpg")
            ),
            [victim, created, created / ".." / "nested-escape.jpg"],
        )

    def test_subdirectory_prefix_is_rejected_without_creating_it(self) -> None:
        nested = self.work / "nested"
        self._run_blocked(
            lambda: thumbnail.create_grids(
                self._slides(1), 3, 40, Path("nested/grid.jpg")
            ),
            [nested, nested / "grid.jpg"],
        )

    def test_numbered_grids_do_not_escape(self) -> None:
        folder = self.root / "numbered"
        first = folder / "deck-1.jpg"
        second = folder / "deck-2.jpg"
        self._run_blocked(
            lambda: thumbnail.create_grids(
                self._slides(13), 3, 40, Path("../numbered/deck.jpg")
            ),
            [folder, first, second],
        )

    def test_directory_symlink_is_not_followed(self) -> None:
        real = self.root / "real-dir"
        real.mkdir()
        victim = real / "deck.jpg"
        victim.write_bytes(_MARKER)
        (self.work / "linked").symlink_to(real, target_is_directory=True)
        self._run_blocked(
            lambda: thumbnail.create_grids(
                self._slides(1), 3, 40, Path("linked/deck.jpg")
            ),
            [victim, self.work / "linked"],
        )

    def test_final_symlink_is_not_followed_or_removed(self) -> None:
        victim = self.root / "trusted.jpg"
        victim.write_bytes(_MARKER)
        link = self.work / "grid.jpg"
        link.symlink_to(victim)
        self._run_blocked(
            lambda: thumbnail.create_grids(self._slides(1), 3, 40, Path("grid.jpg")),
            [victim, link],
        )

    def test_numbered_grid_symlink_is_not_followed_or_removed(self) -> None:
        victim = self.root / "trusted-numbered.jpg"
        victim.write_bytes(_MARKER)
        link = self.work / "grid-1.jpg"
        link.symlink_to(victim)
        self._run_blocked(
            lambda: thumbnail.create_grids(self._slides(13), 3, 40, Path("grid.jpg")),
            [victim, link],
        )

    def test_main_parent_prefix_from_argv_does_not_write(self) -> None:
        victim = self.root / "outside" / "deck.jpg"
        victim.parent.mkdir()
        victim.write_bytes(_MARKER)
        self._run_blocked(lambda: self._call_main("../outside/deck"), [victim])

    def test_main_parent_prefix_does_not_create_missing_directories(self) -> None:
        created = self.root / "fresh"
        self._run_blocked(
            lambda: self._call_main("../fresh/deck"),
            [created, created / "deck.jpg"],
        )

    def test_main_absolute_prefix_from_argv_does_not_write(self) -> None:
        victim = self.root / "from-argv.jpg"
        victim.write_bytes(_MARKER)
        prefix = str(victim)[: -len(".jpg")]
        self._run_blocked(lambda: self._call_main(prefix), [victim])

    def test_main_does_not_follow_final_symlink(self) -> None:
        victim = self.root / "argv-trusted.jpg"
        victim.write_bytes(_MARKER)
        link = self.work / "grid.jpg"
        link.symlink_to(victim)
        self._run_blocked(lambda: self._call_main("grid"), [victim, link])

    def test_symlink_check_then_open_still_does_not_follow(self) -> None:
        """A stat that reports a regular file must not be reused for the open.

        lstat64 of the destination is forced to look like a regular file for
        the whole call, which is the window after a check and before the
        write. The directory entry remains a symlink. The trusted JPEG must
        stay unchanged and the symlink must still be there.
        """
        victim = self.root / "toctou-trusted.jpg"
        victim.write_bytes(_MARKER)
        link = self.work / _OUTPUT_NAME
        link.symlink_to(victim)
        so_dir = Path(tempfile.mkdtemp(prefix="thumb-toctou-"))
        self.addCleanup(shutil.rmtree, so_dir, True)
        source = so_dir / "lstat_lie.c"
        source.write_text(_LSTAT_LIE)
        library = so_dir / "lstat_lie.so"
        compiled = subprocess.run(
            ["gcc", "-shared", "-fPIC", "-o", str(library), str(source), "-ldl"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        env = os.environ.copy()
        env["LD_PRELOAD"] = str(library)
        env["THUMB_TOCTOU_BASENAME"] = _TOCTOU_BASENAME
        env["THUMB_TOCTOU_CHILD"] = "1"
        env["THUMB_WORK"] = str(self.work)
        env["THUMB_SLIDE"] = str(self.slide)
        env["THUMB_OUTPUT_NAME"] = _OUTPUT_NAME
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            env=env,
            cwd=self.work,
            capture_output=True,
            text=True,
        )
        self.assertEqual(victim.read_bytes(), _MARKER, proc.stderr)
        self.assertEqual(_state(link), ("symlink", str(victim)), proc.stderr)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)


if __name__ == "__main__":
    if os.environ.get("THUMB_TOCTOU_CHILD") == "1":
        _toctou_child()
    else:
        unittest.main()
