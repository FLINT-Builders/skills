"""Slide duplication must not copy a symlink target into the deck.

These tests are the spec for the unpacked-deck and package entry points in
add_slide.py. They fail while a source slide (or its .rels) is copied with a
follow-symlinks read. A fix has to refuse, before it creates or edits any
file, when the source is a symbolic link or resolves outside ppt/slides,
including when the path changes between the check and the read.

Run from the repository root:

    python3 -m unittest discover -s skills/pptx/scripts/tests -p 'test_add_slide_symlink_disclosure.py' -v

Not covered here, because they do not copy a slide or slide rels into the deck:
create_slide_from_layout (it writes a fixed slide template), clean.py,
rezip / the documented `zip -Xr` pack step, and hard links.
"""

from __future__ import annotations

import builtins
import io
import os
import posix
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import add_slide  # noqa: E402

SCRIPT = SCRIPTS / "add_slide.py"
NEEDLE = b"PLACEHOLDER_NOT_A_SECRET"
SAFE_SLIDE = b"SAFE_SLIDE_BODY"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>
"""

PRESENTATION = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst>
    <p:sldId id="256" r:id="rId2"/>
  </p:sldIdLst>
</p:presentation>
"""

PRESENTATION_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""

SLIDE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
</Relationships>
"""


def build_deck(unpacked: Path, slide: bytes = SAFE_SLIDE) -> None:
    slides = unpacked / "ppt" / "slides"
    rels = slides / "_rels"
    pres_rels = unpacked / "ppt" / "_rels"
    slides.mkdir(parents=True)
    rels.mkdir()
    pres_rels.mkdir(parents=True)
    (unpacked / "[Content_Types].xml").write_text(CONTENT_TYPES, encoding="utf-8")
    (unpacked / "ppt" / "presentation.xml").write_text(PRESENTATION, encoding="utf-8")
    (pres_rels / "presentation.xml.rels").write_text(PRESENTATION_RELS, encoding="utf-8")
    (slides / "slide1.xml").write_bytes(slide)
    (rels / "slide1.xml.rels").write_text(SLIDE_RELS, encoding="utf-8")


def pack_deck(unpacked: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        for path in sorted(unpacked.rglob("*")):
            if path.is_file() and not path.is_symlink():
                zf.write(path, path.relative_to(unpacked).as_posix())


def snapshot(root: Path) -> dict[str, tuple]:
    items: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                items[rel] = ("symlink", os.readlink(path))
            else:
                items[rel] = ("dir",)
        for name in filenames:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                items[rel] = ("symlink", os.readlink(path))
            else:
                items[rel] = ("file", path.read_bytes())
    return items


def _fspath(value) -> str | None:
    if isinstance(value, int):
        return None
    try:
        text = os.fspath(value)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    return text


class OpenSwapper:
    """Replace one path with a symlink on the next open of that path.

    A check that stats the path and only later opens it still reads the
    symlink target. An open that passes O_NOFOLLOW fails on the replaced
    link and never reads the target.
    """

    def __init__(self, target: Path, link_to: Path):
        self.target = os.path.abspath(target)
        self.link_to = os.path.abspath(link_to)
        self.done = False
        self._saved = None

    def matches(self, value, dir_fd=None) -> bool:
        text = _fspath(value)
        if text is None:
            return False
        if os.path.isabs(text):
            return os.path.abspath(text) == self.target
        if os.path.basename(text) != os.path.basename(self.target):
            return False
        if dir_fd is not None:
            return True
        return os.path.abspath(text) == self.target

    def _swap(self, value, dir_fd=None) -> None:
        if self.done or not self.matches(value, dir_fd):
            return
        self.done = True
        os.unlink(self.target)
        os.symlink(self.link_to, self.target)

    def _open_py(self, file, *args, **kwargs):
        self._swap(file)
        return self._saved[0](file, *args, **kwargs)

    def _open_os(self, path, flags, mode=0o777, *, dir_fd=None):
        self._swap(path, dir_fd)
        return self._saved[2](path, flags, mode, dir_fd=dir_fd)

    def __enter__(self):
        self._saved = (builtins.open, io.open, os.open, posix.open)
        builtins.open = self._open_py
        io.open = self._open_py
        os.open = self._open_os
        posix.open = self._open_os
        return self

    def __exit__(self, *exc):
        builtins.open, io.open, os.open, posix.open = self._saved
        return False


class SlideSymlinkDisclosureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.secret = self.base / "secret.bin"
        self.secret.write_bytes(NEEDLE)
        self.unpacked = self.base / "unpacked"
        build_deck(self.unpacked)

    def assert_refused(self, fn, allow=None):
        allow = allow or {}
        roots = [self.base]
        before = {root: snapshot(root) for root in roots}
        caught = None
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - refusal may SystemExit
            if isinstance(exc, KeyboardInterrupt):
                raise
            caught = exc
        problems = []
        for root, prior in before.items():
            after = snapshot(root)
            allowed = allow.get(root, set())
            for key in sorted(set(prior) | set(after)):
                if key in allowed:
                    continue
                if prior.get(key) != after.get(key):
                    new = after.get(key)
                    copied = (
                        isinstance(new, tuple)
                        and len(new) > 1
                        and isinstance(new[-1], (bytes, bytearray))
                        and NEEDLE in new[-1]
                    )
                    problems.append(
                        f"{key} ({'target bytes copied' if copied else 'changed'})"
                    )
        detail = (
            f"{type(caught).__name__}: {caught}" if caught is not None else "call returned"
        )
        self.assertEqual(problems, [], detail + " changed " + ", ".join(problems))
        self.assertIsNotNone(caught, "source was copied instead of refused")
        if isinstance(caught, SystemExit):
            self.assertNotIn(caught.code, (0, None))

    def assert_cli_refused(self, args):
        before = snapshot(self.base)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.base,
            env=env,
            capture_output=True,
            check=False,
        )
        after = snapshot(self.base)
        problems = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                new = after.get(key)
                copied = (
                    isinstance(new, tuple)
                    and new[0] == "file"
                    and isinstance(new[1], (bytes, bytearray))
                    and NEEDLE in new[1]
                )
                problems.append(f"{key} ({'target bytes copied' if copied else 'changed'})")
        output = (proc.stdout + proc.stderr).decode()
        self.assertEqual(problems, [], output + " changed " + ", ".join(problems))
        self.assertNotEqual(proc.returncode, 0, output)

    def test_regular_slide_is_duplicated(self):
        dest = add_slide.add_slide(self.unpacked, "slide1.xml")
        self.assertEqual(dest, "slide2.xml")
        copied = self.unpacked / "ppt" / "slides" / "slide2.xml"
        self.assertFalse(copied.is_symlink())
        self.assertEqual(copied.read_bytes(), SAFE_SLIDE)
        presentation = (self.unpacked / "ppt" / "presentation.xml").read_text(encoding="utf-8")
        self.assertIn("slide2.xml", (self.unpacked / "ppt" / "_rels" / "presentation.xml.rels").read_text(encoding="utf-8"))
        self.assertIn("<p:sldId", presentation)

    def test_cli_refuses_symlink_slide(self):
        slide = self.unpacked / "ppt" / "slides" / "slide1.xml"
        slide.unlink()
        slide.symlink_to(self.secret)
        self.assert_cli_refused([str(self.unpacked), "slide1.xml"])

    def test_relative_symlink_slide_is_not_copied(self):
        slide = self.unpacked / "ppt" / "slides" / "slide1.xml"
        slide.unlink()
        slide.symlink_to(os.path.relpath(self.secret, slide.parent))
        self.assert_refused(lambda: add_slide.add_slide(self.unpacked, "slide1.xml"))

    def test_symlink_inside_slides_directory_is_refused(self):
        slides = self.unpacked / "ppt" / "slides"
        other = slides / "extra.xml"
        other.write_bytes(NEEDLE)
        slide = slides / "slide1.xml"
        slide.unlink()
        slide.symlink_to("extra.xml")
        self.assert_refused(lambda: add_slide.add_slide(self.unpacked, "slide1.xml"))

    def test_symlink_rels_are_not_copied(self):
        rels = self.unpacked / "ppt" / "slides" / "_rels" / "slide1.xml.rels"
        rels.unlink()
        rels.symlink_to(self.secret)
        self.assert_refused(lambda: add_slide.add_slide(self.unpacked, "slide1.xml"))

    def test_dotdot_source_is_not_copied(self):
        source = os.path.relpath(self.secret, self.unpacked / "ppt" / "slides")
        self.assertIn("..", Path(source).parts)
        self.assert_refused(lambda: add_slide.add_slide(self.unpacked, source))

    def test_absolute_source_is_not_copied(self):
        self.assert_refused(lambda: add_slide.add_slide(self.unpacked, str(self.secret)))

    def test_symlink_directory_component_is_not_followed(self):
        hidden = self.base / "hidden"
        hidden.mkdir()
        (hidden / "payload.xml").write_bytes(NEEDLE)
        nested = self.unpacked / "ppt" / "slides" / "nested"
        nested.symlink_to(hidden, target_is_directory=True)
        self.assert_refused(
            lambda: add_slide.add_slide(self.unpacked, "nested/payload.xml")
        )

    def test_dotdot_through_symlink_directory_is_not_copied(self):
        outside = self.base / "outside_dir"
        outside.mkdir()
        escaped = self.base / "slide1.xml"
        escaped.write_bytes(NEEDLE)
        sub = self.unpacked / "ppt" / "slides" / "sub"
        sub.symlink_to(outside, target_is_directory=True)
        self.assert_refused(
            lambda: add_slide.add_slide(self.unpacked, "sub/../slide1.xml")
        )

    def test_symlinked_slides_directory_is_not_followed(self):
        real_slides = self.base / "real_slides"
        real_slides.mkdir()
        (real_slides / "slide1.xml").write_bytes(NEEDLE)
        slides = self.unpacked / "ppt" / "slides"
        shutil.rmtree(slides)
        slides.symlink_to(real_slides, target_is_directory=True)
        self.assert_refused(lambda: add_slide.add_slide(self.unpacked, "slide1.xml"))

    def test_toctou_swap_of_slide_is_not_copied(self):
        slide = self.unpacked / "ppt" / "slides" / "slide1.xml"
        swapper = OpenSwapper(slide, self.secret)

        def run():
            with swapper:
                add_slide.add_slide(self.unpacked, "slide1.xml")

        self.assert_refused(run, allow={self.base: {"unpacked/ppt/slides/slide1.xml"}})
        self.assertTrue(swapper.done, "source was read without passing the open hook")

    def test_toctou_swap_of_rels_is_not_copied(self):
        rels = self.unpacked / "ppt" / "slides" / "_rels" / "slide1.xml.rels"
        swapper = OpenSwapper(rels, self.secret)

        def run():
            with swapper:
                add_slide.add_slide(self.unpacked, "slide1.xml")

        self.assert_refused(
            run,
            allow={self.base: {"unpacked/ppt/slides/_rels/slide1.xml.rels"}},
        )
        self.assertTrue(swapper.done, "rels were read without passing the open hook")

    def test_cli_package_refuses_absolute_source(self):
        deck = self.base / "deck.pptx"
        out = self.base / "out.pptx"
        pack_deck(self.unpacked, deck)
        self.assert_cli_refused([str(deck), str(self.secret), "-o", str(out)])
        self.assertFalse(out.exists())

    def test_package_symlink_member_is_not_followed(self):
        deck = self.base / "linked.pptx"
        out = self.base / "out.pptx"
        info = zipfile.ZipInfo("ppt/slides/slide1.xml")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(deck, "w") as zf:
            zf.writestr(info, os.fspath(self.secret))
            zf.writestr("[Content_Types].xml", CONTENT_TYPES)
            zf.writestr("ppt/presentation.xml", PRESENTATION)
            zf.writestr("ppt/_rels/presentation.xml.rels", PRESENTATION_RELS)
        self.assert_refused(
            lambda: add_slide.add_slide_to_package(deck, "slide1.xml", output=out)
        )
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
