"""Thumbnail grids must not follow a symbolic link at the output path.

The grid path is the output prefix plus ``.jpg`` (``thumbnails.jpg`` by
default, or ``<prefix>-N.jpg`` when a deck is split). If that path is
already a symlink, generation must stop before it creates or deletes
anything, and the link target must stay byte-for-byte unchanged.

A check that looks the path up and then opens it by name is not enough.
These tests swap a regular output path for a symlink after that lookup
and before the write. The target must still be unchanged.
"""

import builtins
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import thumbnail  # noqa: E402


SECRET = b"ORIGINAL-SECRET-do-not-touch\n"
SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"


def _mode_writes(mode) -> bool:
    return isinstance(mode, str) and any(flag in mode for flag in "wax+")


def _os_open_writes(flags) -> bool:
    access = flags & os.O_ACCMODE
    return access != os.O_RDONLY or bool(flags & (os.O_CREAT | os.O_TRUNC))


def _is_refusal(exc: BaseException) -> bool:
    if isinstance(exc, SystemExit):
        return exc.code not in (0, None)
    return not isinstance(exc, (KeyboardInterrupt, GeneratorExit))


class _WriteMonitor:
    """Record write opens and deletions without changing their behavior."""

    def __init__(self):
        self.writes = []
        self.deleted = []
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "open": builtins.open,
            "os_open": os.open,
            "remove": os.remove,
            "unlink": os.unlink,
        }
        builtins.open = self._open
        os.open = self._os_open
        os.remove = self._remove
        os.unlink = self._unlink
        return self

    def __exit__(self, *exc):
        builtins.open = self._saved["open"]
        os.open = self._saved["os_open"]
        os.remove = self._saved["remove"]
        os.unlink = self._saved["unlink"]

    def _open(self, file, mode="r", *args, **kwargs):
        if _mode_writes(mode) and not isinstance(file, int):
            self.writes.append(os.fspath(file))
        return self._saved["open"](file, mode, *args, **kwargs)

    def _os_open(self, path, flags, *args, **kwargs):
        if _os_open_writes(flags) and not isinstance(path, int):
            self.writes.append(os.fspath(path))
        return self._saved["os_open"](path, flags, *args, **kwargs)

    def _remove(self, path, *args, **kwargs):
        self.deleted.append(os.fspath(path))
        return self._saved["remove"](path, *args, **kwargs)

    def _unlink(self, path, *args, **kwargs):
        self.deleted.append(os.fspath(path))
        return self._saved["unlink"](path, *args, **kwargs)


class _SwapBeforeUse:
    """After a path is observed, make the write itself hit a symlink.

    Stats and link checks see the original file. The next write open,
    rename, or replace of that path plants the symlink first, which is
    the window between a check and a use.
    """

    def __init__(self, mapping):
        self.mapping = {
            os.path.abspath(os.fspath(path)): os.path.abspath(os.fspath(secret))
            for path, secret in mapping.items()
        }
        self._planted = set()
        self._busy = False
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "open": builtins.open,
            "os_open": os.open,
            "replace": os.replace,
            "rename": os.rename,
        }
        builtins.open = self._open
        os.open = self._os_open
        os.replace = self._replace
        os.rename = self._rename
        return self

    def __exit__(self, *exc):
        builtins.open = self._saved["open"]
        os.open = self._saved["os_open"]
        os.replace = self._saved["replace"]
        os.rename = self._saved["rename"]

    def _key(self, file):
        if isinstance(file, int):
            return None
        try:
            key = os.path.abspath(os.fspath(file))
        except (TypeError, ValueError):
            return None
        if key in self.mapping:
            return key
        return None

    def _plant(self, key):
        if self._busy or key in self._planted:
            return
        self._planted.add(key)
        self._busy = True
        try:
            try:
                os.unlink(key)
            except FileNotFoundError:
                pass
            os.symlink(self.mapping[key], key)
        finally:
            self._busy = False

    def _open(self, file, mode="r", *args, **kwargs):
        key = self._key(file)
        if key is not None and _mode_writes(mode):
            self._plant(key)
        return self._saved["open"](file, mode, *args, **kwargs)

    def _os_open(self, path, flags, *args, **kwargs):
        key = self._key(path)
        if key is not None and _os_open_writes(flags):
            self._plant(key)
        return self._saved["os_open"](path, flags, *args, **kwargs)

    def _replace(self, src, dst, *args, **kwargs):
        key = self._key(dst)
        if key is not None:
            self._plant(key)
        return self._saved["replace"](src, dst, *args, **kwargs)

    def _rename(self, src, dst, *args, **kwargs):
        key = self._key(dst)
        if key is not None:
            self._plant(key)
        return self._saved["rename"](src, dst, *args, **kwargs)


def _write_pptx(path: Path, slides: int) -> None:
    relationships = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    slide_ids = []
    parts = {}
    for number in range(1, slides + 1):
        relationships.append(
            f'<Relationship Id="rId{number}" Type="{SLIDE_REL}" '
            f'Target="slides/slide{number}.xml"/>'
        )
        slide_ids.append(f'<p:sldId id="{255 + number}" r:id="rId{number}"/>')
        parts[f"ppt/slides/slide{number}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
        )
    relationships.append("</Relationships>")
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<p:sldIdLst>{''.join(slide_ids)}</p:sldIdLst>"
        "</p:presentation>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/_rels/presentation.xml.rels", "".join(relationships))
        archive.writestr("ppt/presentation.xml", presentation)
        for name, body in parts.items():
            archive.writestr(name, body)


def _rendered_slides(count):
    def convert(pptx_path, temp_dir):
        paths = []
        for number in range(1, count + 1):
            image_path = Path(temp_dir) / f"slide-{number}.jpg"
            Image.new("RGB", (16, 12), (number * 40, 64, 96)).save(
                image_path, quality=90
            )
            paths.append(image_path)
        return paths

    return mock.patch.object(thumbnail, "convert_to_images", convert)


class ThumbnailSymlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.outside = self.root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.workspace)

    def _secret(self, name: str, content: bytes = SECRET) -> Path:
        path = self.outside / name
        path.write_bytes(content)
        return path

    def _slides(self, count: int):
        folder = self.workspace / "rendered"
        folder.mkdir(exist_ok=True)
        slides = []
        for number in range(count):
            image_path = folder / f"slide-{number}.jpg"
            Image.new("RGB", (16, 12), "white").save(image_path, quality=90)
            slides.append((image_path, f"slide{number + 1}.xml"))
        return slides

    def _identities(self, paths):
        found = {}
        for path in paths:
            if path.exists():
                stat = path.stat()
                found[path] = (stat.st_dev, stat.st_ino, path.read_bytes())
            else:
                found[path] = None
        return found

    def _assert_unchanged(self, before, paths):
        after = self._identities(paths)
        for path in paths:
            self.assertEqual(
                before[path],
                after[path],
                f"link target {path} was created or modified",
            )

    def _assert_symlink(self, link: Path, secret: Path):
        self.assertTrue(link.is_symlink(), f"{link} is no longer a symlink")
        self.assertEqual(os.readlink(link), str(secret))

    def _snapshot(self):
        return sorted(
            path.relative_to(self.workspace).as_posix()
            for path in self.workspace.rglob("*")
        )

    def _refuse(self, fn, links):
        """links maps each symlink path to the outside file it must spare."""
        secrets = list(links.values())
        before = self._identities(secrets)
        listing = self._snapshot()
        monitor = _WriteMonitor()
        refused = False
        with monitor:
            try:
                fn()
            except BaseException as exc:
                self.assertTrue(_is_refusal(exc), f"unexpected {exc!r}")
                refused = True
        self._assert_unchanged(before, secrets)
        self.assertTrue(refused, "thumbnail generation wrote through the symlink")
        for link, secret in links.items():
            self._assert_symlink(link, secret)
        self.assertEqual(monitor.writes, [], f"wrote a file before refusing: {monitor.writes}")
        self.assertEqual(monitor.deleted, [], f"deleted a file before refusing: {monitor.deleted}")
        self.assertEqual(self._snapshot(), listing)

    def _spare_when_swapped(self, fn, links):
        secrets = list(links.values())
        before = self._identities(secrets)
        for link in links:
            link.write_bytes(b"regular-output-not-a-link")
            self.assertFalse(link.is_symlink())
        with _SwapBeforeUse(links):
            try:
                fn()
            except BaseException as exc:
                self.assertTrue(_is_refusal(exc), f"unexpected {exc!r}")
        self._assert_unchanged(before, secrets)

    def _grids(self, slides, cols, output_path):
        thumbnail.create_grids(slides, cols, thumbnail.THUMBNAIL_WIDTH, output_path)

    def _run_main(self, argv):
        with mock.patch.object(sys, "argv", argv):
            thumbnail.main()

    def test_single_grid_refuses_existing_symlink(self):
        secret = self._secret("id_rsa_standin")
        link = self.workspace / "thumbnails.jpg"
        os.symlink(secret, link)
        slides = self._slides(1)

        self._refuse(
            lambda: self._grids(slides, thumbnail.DEFAULT_COLS, link),
            {link: secret},
        )

    def test_single_grid_refuses_dangling_symlink(self):
        secret = self.outside / "missing-key"
        self.assertFalse(secret.exists())
        link = self.workspace / "thumbnails.jpg"
        os.symlink(secret, link)
        slides = self._slides(1)

        self._refuse(
            lambda: self._grids(slides, thumbnail.DEFAULT_COLS, link),
            {link: secret},
        )

    def test_prefix_directory_refuses_existing_symlink(self):
        secret = self._secret("document.txt", b"keep this document\n")
        folder = self.workspace / "grids"
        folder.mkdir()
        link = folder / "template-thumbs.jpg"
        os.symlink(secret, link)
        slides = self._slides(1)

        self._refuse(
            lambda: self._grids(slides, thumbnail.DEFAULT_COLS, link),
            {link: secret},
        )

    def test_numbered_grids_refuse_existing_symlinks(self):
        first = self._secret("first-key")
        second = self._secret("second-key", b"second-secret\n")
        stem = self.workspace / "template-thumbs.jpg"
        link_1 = self.workspace / "template-thumbs-1.jpg"
        link_2 = self.workspace / "template-thumbs-2.jpg"
        os.symlink(first, link_1)
        os.symlink(second, link_2)
        # cols 1 splits 3 slides into template-thumbs-1.jpg and -2.jpg.
        slides = self._slides(3)

        self._refuse(
            lambda: self._grids(slides, 1, stem),
            {link_1: first, link_2: second},
        )

    def test_single_grid_does_not_follow_link_swapped_after_lookup(self):
        secret = self._secret("swapped-target")
        link = self.workspace / "thumbnails.jpg"
        slides = self._slides(1)

        self._spare_when_swapped(
            lambda: self._grids(slides, thumbnail.DEFAULT_COLS, link),
            {link: secret},
        )

    def test_numbered_grids_do_not_follow_link_swapped_after_lookup(self):
        first = self._secret("swapped-1")
        second = self._secret("swapped-2", b"second-secret\n")
        stem = self.workspace / "template-thumbs.jpg"
        link_1 = self.workspace / "template-thumbs-1.jpg"
        link_2 = self.workspace / "template-thumbs-2.jpg"
        slides = self._slides(3)

        self._spare_when_swapped(
            lambda: self._grids(slides, 1, stem),
            {link_1: first, link_2: second},
        )

    def test_main_default_prefix_refuses_existing_symlink(self):
        secret = self._secret("default-prefix-target")
        link = self.workspace / "thumbnails.jpg"
        os.symlink(secret, link)
        deck = self.workspace / "deck.pptx"
        _write_pptx(deck, 1)
        deck_bytes = deck.read_bytes()

        def run():
            with _rendered_slides(1):
                self._run_main(["thumbnail.py", str(deck)])

        self._refuse(run, {link: secret})
        self.assertEqual(deck.read_bytes(), deck_bytes)

    def test_main_deck_prefix_refuses_existing_symlink(self):
        secret = self._secret("deck-prefix-target", b"#!/bin/sh\nexit 0\n")
        link = self.workspace / "template-thumbs.jpg"
        os.symlink(secret, link)
        deck = self.workspace / "template.pptx"
        _write_pptx(deck, 1)

        def run():
            with _rendered_slides(1):
                self._run_main(
                    ["thumbnail.py", str(deck), "template-thumbs"]
                )

        self._refuse(run, {link: secret})

    def test_main_numbered_prefix_refuses_existing_symlinks(self):
        first = self._secret("main-grid-1")
        second = self._secret("main-grid-2", b"second-secret\n")
        link_1 = self.workspace / "template-thumbs-1.jpg"
        link_2 = self.workspace / "template-thumbs-2.jpg"
        os.symlink(first, link_1)
        os.symlink(second, link_2)
        deck = self.workspace / "template.pptx"
        _write_pptx(deck, 3)

        def run():
            with _rendered_slides(3):
                self._run_main(
                    ["thumbnail.py", str(deck), "template-thumbs", "--cols", "1"]
                )

        self._refuse(run, {link_1: first, link_2: second})

    def test_main_does_not_follow_link_swapped_after_lookup(self):
        secret = self._secret("main-swapped")
        link = self.workspace / "thumbnails.jpg"
        deck = self.workspace / "deck.pptx"
        _write_pptx(deck, 1)

        def run():
            with _rendered_slides(1):
                self._run_main(["thumbnail.py", str(deck)])

        self._spare_when_swapped(run, {link: secret})

    def test_regular_output_is_replaced_with_jpeg(self):
        slides = self._slides(1)
        output = self.workspace / "thumbnails.jpg"
        output.write_bytes(b"previous-grid")

        self._grids(slides, thumbnail.DEFAULT_COLS, output)

        self.assertFalse(output.is_symlink())
        self.assertTrue(output.read_bytes().startswith(b"\xff\xd8"))

    def test_main_regular_default_prefix_writes_jpeg(self):
        deck = self.workspace / "deck.pptx"
        _write_pptx(deck, 1)
        output = self.workspace / "thumbnails.jpg"
        output.write_bytes(b"previous-grid")

        with _rendered_slides(1):
            self._run_main(["thumbnail.py", str(deck)])

        self.assertFalse(output.is_symlink())
        self.assertTrue(output.read_bytes().startswith(b"\xff\xd8"))
        self.assertEqual(len(thumbnail.get_slide_info(deck)), 1)


if __name__ == "__main__":
    unittest.main()
