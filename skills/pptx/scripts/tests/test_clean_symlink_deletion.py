"""Security spec for pptx cleanup. These tests fail until clean.py stops
deleting files outside the unpacked deck.

`clean.clean_unused_files` (what `clean.py <unpacked_dir>` runs) unlinks
top-level files in resource, slide, theme, notes, and trash directories.
Those paths must be real directories inside the unpacked deck. A symlink,
including one reached through a parent symlink, must be refused with
`RefusedToClean` before any outside file is removed.

The race tests swap a real directory for a symlink inside the unlink call,
after any earlier stat or resolve. Path-based deletion still removes the
outside file. Deletion has to use a directory fd opened so the directory
itself is not followed (`O_NOFOLLOW`), then unlink the entry through that fd.
"""

from __future__ import annotations

import os
import posix
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
CLEAN_PY = SCRIPTS / "clean.py"
sys.path.insert(0, str(SCRIPTS))

import clean  # noqa: E402


SENTINEL = "outside-marker"
NESTED = "nested-marker"
PACKAGE = "package-copy"
EMPTY_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "</Relationships>"
)
ANCHOR_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId2" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
    'Target="slides/slide1.xml"/>'
    "</Relationships>"
)

RESOURCE_DIRS = (
    "media",
    "embeddings",
    "charts",
    "diagrams",
    "tags",
    "drawings",
    "ink",
)
RELS_NAMES = {
    "charts": "chart1.xml.rels",
    "diagrams": "diagram1.xml.rels",
    "drawings": "drawing1.xml.rels",
}


def plant(root: Path, relative: str, content: str) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_anchor_rels(unpacked: Path) -> None:
    """A resolvable relationship so the empty-reference guard does not fire.

    No presentation.xml, so slide*.xml stay unreferenced by sldIdLst.
    """
    ppt = unpacked / "ppt"
    (ppt / "slides").mkdir(parents=True, exist_ok=True)
    slide = ppt / "slides" / "slide1.xml"
    if not slide.exists():
        slide.write_text("<p:sld/>", encoding="utf-8")
    (ppt / "_rels").mkdir(exist_ok=True)
    (ppt / "_rels" / "presentation.xml.rels").write_text(ANCHOR_RELS, encoding="utf-8")


def write_minimal_deck(unpacked: Path) -> None:
    """A readable deck whose only referenced parts are slide1 and keep.png."""
    ppt = unpacked / "ppt"
    (ppt / "slides" / "_rels").mkdir(parents=True)
    (ppt / "slides" / "slide1.xml").write_text("<p:sld/>", encoding="utf-8")
    (ppt / "_rels").mkdir()
    (ppt / "presentation.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
        "</p:presentation>",
        encoding="utf-8",
    )
    (ppt / "_rels" / "presentation.xml.rels").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        'Target="slides/slide1.xml"/>'
        "</Relationships>",
        encoding="utf-8",
    )
    (ppt / "slides" / "_rels" / "slide1.xml.rels").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="../media/keep.png"/>'
        "</Relationships>",
        encoding="utf-8",
    )


def link_dir(link: Path, target: Path, *, relative: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    elif link.is_dir():
        for child in sorted(link.rglob("*"), reverse=True):
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        link.rmdir()
    elif link.exists():
        link.unlink()
    source: str | Path
    if relative:
        source = os.path.relpath(target, link.parent)
    else:
        source = target
    os.symlink(source, link, target_is_directory=True)


class SwapDirectoryOnPathUnlink:
    """On a path-based unlink inside `resource_dir`, replace that directory
    with a symlink to `victim_dir` before the syscall.

    Unlink through a directory fd (`dir_fd=`) is left alone so the fd still
    names the directory that was opened. Re-statting the path between a
    check and `os.unlink(path)` does not close this window: the swap runs
    in the unlink itself.
    """

    def __init__(self, resource_dir: Path, victim_dir: Path):
        self.resource_dir = resource_dir.resolve()
        self.victim_dir = victim_dir.resolve()
        self.real_unlink = os.unlink
        self.saved: list[tuple] = []
        self.swapped = False

    def __enter__(self):
        def racing(path, *args, dir_fd=None, **kwargs):
            if dir_fd is None and not self.swapped:
                logical = Path(os.fspath(path))
                if not logical.is_absolute():
                    logical = Path.cwd() / logical
                try:
                    logical.relative_to(self.resource_dir)
                except ValueError:
                    inside = False
                else:
                    inside = logical != self.resource_dir
                if (
                    inside
                    and self.resource_dir.is_dir()
                    and not self.resource_dir.is_symlink()
                ):
                    aside = self.resource_dir.with_name(
                        self.resource_dir.name + ".aside"
                    )
                    os.rename(self.resource_dir, aside)
                    os.symlink(
                        self.victim_dir,
                        self.resource_dir,
                        target_is_directory=True,
                    )
                    self.swapped = True
            if dir_fd is None:
                return self.real_unlink(path, *args, **kwargs)
            return self.real_unlink(path, *args, dir_fd=dir_fd, **kwargs)

        self._rebind(racing)
        return self

    def __exit__(self, exc_type, exc, tb):
        for obj, key, old in reversed(self.saved):
            if isinstance(obj, dict):
                obj[key] = old
            else:
                setattr(obj, key, old)
        self.saved.clear()
        return False

    def _rebind(self, new) -> None:
        bindings: list[tuple] = []
        for module in (os, posix, clean):
            for key in ("unlink", "remove"):
                if hasattr(module, key):
                    bindings.append((module, key, getattr(module, key)))
            for obj in list(vars(module).values()):
                globs = getattr(obj, "__globals__", None)
                if isinstance(globs, dict):
                    for key in ("unlink", "remove"):
                        if key in globs:
                            bindings.append((globs, key, globs[key]))
        seen = set()
        for obj, key, old in bindings:
            ident = (id(obj), key)
            if ident in seen:
                continue
            seen.add(ident)
            self.saved.append((obj, key, old))
            if isinstance(obj, dict):
                obj[key] = new
            else:
                setattr(obj, key, new)


class CleanSymlinkTests(unittest.TestCase):
    def assert_intact(self, path: Path, content: str) -> None:
        self.assertTrue(path.is_file(), f"deleted outside file {path}")
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def assert_refused(self, unpacked: Path, survivors: list[tuple[Path, str]]) -> None:
        refused = False
        try:
            clean.clean_unused_files(unpacked)
        except clean.RefusedToClean:
            refused = True
        finally:
            for path, content in survivors:
                self.assert_intact(path, content)
        self.assertTrue(refused, "cleanup must refuse a symlinked resource directory")

    def test_resource_directory_symlink_is_refused(self):
        for name in RESOURCE_DIRS:
            with self.subTest(directory=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    unpacked = root / "unpacked"
                    outside = root / "outside"
                    write_minimal_deck(unpacked)
                    marker = plant(outside, "marker.txt", SENTINEL)
                    nested = plant(outside, "nested/keep.txt", NESTED)
                    link_dir(unpacked / "ppt" / name, outside)
                    self.assert_refused(
                        unpacked, [(marker, SENTINEL), (nested, NESTED)]
                    )

    def test_charts_diagrams_drawings_symlink_includes_rels(self):
        for name, rel_name in RELS_NAMES.items():
            with self.subTest(directory=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    unpacked = root / "unpacked"
                    outside = root / "outside"
                    write_minimal_deck(unpacked)
                    marker = plant(outside, "marker.txt", SENTINEL)
                    rel = plant(outside, f"_rels/{rel_name}", SENTINEL)
                    link_dir(unpacked / "ppt" / name, outside)
                    self.assert_refused(
                        unpacked, [(marker, SENTINEL), (rel, SENTINEL)]
                    )

    def test_relative_media_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            marker = plant(outside, "marker.txt", SENTINEL)
            link_dir(unpacked / "ppt" / "media", outside, relative=True)
            self.assert_refused(unpacked, [(marker, SENTINEL)])

    def test_trash_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            marker = plant(outside, "marker.txt", SENTINEL)
            nested = plant(outside, "nested/keep.txt", NESTED)
            link_dir(unpacked / "[trash]", outside)
            self.assert_refused(unpacked, [(marker, SENTINEL), (nested, NESTED)])
            self.assertTrue(outside.is_dir())

    def test_theme_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            theme = plant(outside, "theme1.xml", SENTINEL)
            rel = plant(outside, "_rels/theme1.xml.rels", SENTINEL)
            other = plant(outside, "not-theme.txt", NESTED)
            link_dir(unpacked / "ppt" / "theme", outside)
            self.assert_refused(
                unpacked,
                [(theme, SENTINEL), (rel, SENTINEL), (other, NESTED)],
            )

    def test_notes_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            notes = plant(outside, "notesSlide1.xml", SENTINEL)
            other = plant(outside, "other.xml", SENTINEL)
            rel = plant(outside, "_rels/notesSlide1.xml.rels", SENTINEL)
            link_dir(unpacked / "ppt" / "notesSlides", outside)
            self.assert_refused(
                unpacked,
                [(notes, SENTINEL), (other, SENTINEL), (rel, SENTINEL)],
            )

    def test_slides_symlink_without_sldid_list_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            (unpacked / "ppt").mkdir(parents=True)
            slide = plant(outside, "slide1.xml", SENTINEL)
            rel = plant(outside, "_rels/slide1.xml.rels", SENTINEL)
            link_dir(unpacked / "ppt" / "slides", outside)
            self.assert_refused(unpacked, [(slide, SENTINEL), (rel, SENTINEL)])

    def test_slides_symlink_partial_match_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            referenced = plant(outside, "slide1.xml", SENTINEL)
            extra = plant(outside, "slide2.xml", SENTINEL)
            rel = plant(outside, "_rels/slide2.xml.rels", SENTINEL)
            link_dir(unpacked / "ppt" / "slides", outside)
            self.assert_refused(
                unpacked,
                [(referenced, SENTINEL), (extra, SENTINEL), (rel, SENTINEL)],
            )

    def test_slides_symlink_none_match_stays_refused(self):
        # A non-empty sldIdLst that matches no slide*.xml already refuses.
        # That refusal must keep holding, and the outside file must stay.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            slide = plant(outside, "slide9.xml", SENTINEL)
            link_dir(unpacked / "ppt" / "slides", outside)
            self.assert_refused(unpacked, [(slide, SENTINEL)])

    def test_orphaned_rels_directory_symlink_is_refused(self):
        for name, rel_name in RELS_NAMES.items():
            with self.subTest(directory=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    unpacked = root / "unpacked"
                    outside = root / "outside"
                    write_minimal_deck(unpacked)
                    (unpacked / "ppt" / name).mkdir()
                    rel = plant(outside, rel_name, SENTINEL)
                    link_dir(unpacked / "ppt" / name / "_rels", outside)
                    self.assert_refused(unpacked, [(rel, SENTINEL)])

    def test_theme_rels_directory_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            theme = unpacked / "ppt" / "theme"
            theme.mkdir()
            (theme / "theme1.xml").write_text("<a:theme/>", encoding="utf-8")
            rel = plant(outside, "theme1.xml.rels", SENTINEL)
            link_dir(theme / "_rels", outside)
            self.assert_refused(unpacked, [(rel, SENTINEL)])

    def test_notes_rels_directory_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            (unpacked / "ppt" / "notesSlides").mkdir()
            rel = plant(outside, "notesSlide1.xml.rels", SENTINEL)
            link_dir(unpacked / "ppt" / "notesSlides" / "_rels", outside)
            self.assert_refused(unpacked, [(rel, SENTINEL)])

    def test_slides_rels_directory_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            slides = unpacked / "ppt" / "slides"
            slides.mkdir(parents=True)
            (slides / "slide1.xml").write_text("<p:sld/>", encoding="utf-8")
            rel = plant(outside, "slide1.xml.rels", SENTINEL)
            link_dir(slides / "_rels", outside)
            self.assert_refused(unpacked, [(rel, SENTINEL)])

    def test_ppt_ancestor_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            unpacked.mkdir()
            write_minimal_deck(root / "staging")
            ppt = root / "staging" / "ppt"
            survivors: list[tuple[Path, str]] = []
            for name in RESOURCE_DIRS:
                survivors.append((plant(ppt, f"{name}/marker.txt", SENTINEL), SENTINEL))
            survivors.extend(
                [
                    (plant(ppt, "theme/theme1.xml", SENTINEL), SENTINEL),
                    (plant(ppt, "theme/_rels/theme1.xml.rels", SENTINEL), SENTINEL),
                    (plant(ppt, "notesSlides/notesSlide1.xml", SENTINEL), SENTINEL),
                    (
                        plant(ppt, "notesSlides/_rels/notesSlide1.xml.rels", SENTINEL),
                        SENTINEL,
                    ),
                    (plant(ppt, "charts/_rels/chart1.xml.rels", SENTINEL), SENTINEL),
                    (plant(ppt, "diagrams/_rels/diagram1.xml.rels", SENTINEL), SENTINEL),
                    (plant(ppt, "drawings/_rels/drawing1.xml.rels", SENTINEL), SENTINEL),
                    (ppt / "slides" / "slide1.xml", "<p:sld/>"),
                ]
            )
            link_dir(unpacked / "ppt", ppt)
            self.assert_refused(unpacked, survivors)

    def test_resource_symlink_to_a_file_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside-file.txt"
            write_minimal_deck(unpacked)
            outside.write_text(SENTINEL, encoding="utf-8")
            os.symlink(outside, unpacked / "ppt" / "media")
            self.assert_refused(unpacked, [(outside, SENTINEL)])

    def test_file_symlink_inside_real_media_does_not_follow(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside.txt"
            write_minimal_deck(unpacked)
            outside.write_text(SENTINEL, encoding="utf-8")
            media = unpacked / "ppt" / "media"
            media.mkdir()
            os.symlink(outside, media / "link.txt")
            try:
                clean.clean_unused_files(unpacked)
            except clean.RefusedToClean:
                pass
            self.assert_intact(outside, SENTINEL)

    def test_real_directory_still_drops_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as td:
            unpacked = Path(td) / "unpacked"
            write_minimal_deck(unpacked)
            media = unpacked / "ppt" / "media"
            media.mkdir()
            (media / "keep.png").write_bytes(b"keep")
            (media / "orphan.png").write_bytes(b"orphan")
            removed = clean.clean_unused_files(unpacked)
            self.assertIn("ppt/media/orphan.png", removed)
            self.assertTrue((media / "keep.png").is_file())
            self.assertFalse((media / "orphan.png").exists())

    def test_cli_refuses_media_symlink(self):
        self._cli_case("ppt/media")

    def test_cli_refuses_trash_symlink(self):
        self._cli_case("[trash]")

    def _cli_case(self, relative_link: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            outside = root / "outside"
            write_minimal_deck(unpacked)
            marker = plant(outside, "marker.txt", SENTINEL)
            link_dir(unpacked / relative_link, outside)
            proc = subprocess.run(
                [sys.executable, str(CLEAN_PY), str(unpacked)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assert_intact(marker, SENTINEL)
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_race_resource_directories(self):
        for name in RESOURCE_DIRS:
            with self.subTest(directory=name):
                self._race_leaf(name, "marker.txt")

    def test_race_trash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            victim = root / "victim"
            write_minimal_deck(unpacked)
            trash = unpacked / "[trash]"
            trash.mkdir()
            plant(trash, "marker.txt", PACKAGE)
            sentinel = plant(victim, "marker.txt", SENTINEL)
            self._run_race(unpacked, trash, victim, sentinel)

    def test_race_theme_notes_and_slides(self):
        cases = {
            "theme": "theme1.xml",
            "notesSlides": "notesSlide1.xml",
            "slides": "slide1.xml",
        }
        for directory, filename in cases.items():
            with self.subTest(directory=directory):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    unpacked = root / "unpacked"
                    victim = root / "victim"
                    if directory == "slides":
                        (unpacked / "ppt").mkdir(parents=True)
                    else:
                        write_minimal_deck(unpacked)
                    resource = unpacked / "ppt" / directory
                    resource.mkdir(parents=True, exist_ok=True)
                    plant(resource, filename, PACKAGE)
                    sentinel = plant(victim, filename, SENTINEL)
                    self._run_race(unpacked, resource, victim, sentinel)

    def test_race_rels_directories(self):
        for name, rel_name in RELS_NAMES.items():
            with self.subTest(directory=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    unpacked = root / "unpacked"
                    victim = root / "victim"
                    write_minimal_deck(unpacked)
                    rels = unpacked / "ppt" / name / "_rels"
                    rels.mkdir(parents=True)
                    plant(rels, rel_name, EMPTY_RELS)
                    sentinel = plant(victim, rel_name, SENTINEL)
                    self._run_race(unpacked, rels, victim, sentinel)

    def test_race_theme_notes_slides_rels(self):
        cases = (
            ("theme", "theme1.xml", "theme1.xml.rels", True),
            ("notesSlides", None, "notesSlide1.xml.rels", False),
            ("slides", "slide1.xml", "slide1.xml.rels", False),
        )
        for directory, xml_name, rel_name, with_deck in cases:
            with self.subTest(directory=directory):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    unpacked = root / "unpacked"
                    victim = root / "victim"
                    if with_deck:
                        write_minimal_deck(unpacked)
                    else:
                        write_anchor_rels(unpacked)
                    parent = unpacked / "ppt" / directory
                    parent.mkdir(parents=True, exist_ok=True)
                    if xml_name is not None and directory != "notesSlides":
                        (parent / xml_name).write_text("<x/>", encoding="utf-8")
                    rels = parent / "_rels"
                    rels.mkdir()
                    plant(rels, rel_name, EMPTY_RELS)
                    sentinel = plant(victim, rel_name, SENTINEL)
                    self._run_race(unpacked, rels, victim, sentinel)

    def test_race_ppt_ancestor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            victim = root / "victim"
            write_minimal_deck(unpacked)
            plant(unpacked / "ppt", "media/marker.txt", PACKAGE)
            sentinel = plant(victim, "media/marker.txt", SENTINEL)
            self._run_race(unpacked, unpacked / "ppt", victim, sentinel)

    def _race_leaf(self, directory: str, filename: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unpacked = root / "unpacked"
            victim = root / "victim"
            write_minimal_deck(unpacked)
            resource = unpacked / "ppt" / directory
            plant(resource, filename, PACKAGE)
            sentinel = plant(victim, filename, SENTINEL)
            self._run_race(unpacked, resource, victim, sentinel)

    def _run_race(
        self,
        unpacked: Path,
        resource_dir: Path,
        victim_root: Path,
        sentinel: Path,
    ) -> None:
        try:
            with SwapDirectoryOnPathUnlink(resource_dir, victim_root):
                clean.clean_unused_files(unpacked)
        except clean.RefusedToClean:
            pass
        except Exception:
            pass
        self.assert_intact(sentinel, SENTINEL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
