"""Containment tests for unpacked PPTX edits.

add_slide.py and clean.py open, copy, and delete paths under an unpacked
deck. Those calls follow symbolic links, and a duplicate source is accepted
whenever the joined path exists. The tests below are the spec for closing
that: they fail while the behavior is present and pass once a slide, its
relationships, and the package registration files cannot leave the deck.

The check-then-use cases arm a Python audit hook that replaces the looked-up
path with a symlink after any userspace check and before the kernel opens or
unlinks it. A fix that resolves a path and then opens that same path again
still follows the new link, so those tests stay failed until the write uses
a directory the check already pinned down.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import add_slide  # noqa: E402
import clean  # noqa: E402

_AUDIT = {
    "armed": False,
    "swapped": False,
    "victim": "",
    "backup": "",
    "link": "",
    "needle": "",
    "kind": "dir",
}


def _audit_hook(event, args):
    if not _AUDIT["armed"] or _AUDIT["swapped"]:
        return
    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        text = os.fsdecode(path)
        if not _path_matches(text):
            return
        access = flags & os.O_ACCMODE
        writing = access in (os.O_WRONLY, os.O_RDWR) or any(
            mark in str(mode) for mark in ("w", "a", "+")
        )
        if writing:
            _swap_in_symlink()
        return
    if event == "os.remove":
        path = args[0]
        dir_fd = args[1] if len(args) > 1 else -1
        if dir_fd not in (-1, None):
            return
        if _path_matches(os.fsdecode(path)):
            _swap_in_symlink()


def _path_matches(text):
    needle = _AUDIT["needle"]
    if not needle or not text:
        return False
    if _AUDIT["kind"] == "dir":
        return text.startswith(needle)
    return text == needle


def _swap_in_symlink():
    _AUDIT["swapped"] = True
    os.rename(_AUDIT["victim"], _AUDIT["backup"])
    try:
        os.rename(_AUDIT["link"], _AUDIT["victim"])
    except OSError:
        os.rename(_AUDIT["backup"], _AUDIT["victim"])
        _AUDIT["swapped"] = False
        raise


def _install_audit_hook():
    sys.addaudithook(_audit_hook)


def _arm_swap(victim: Path, backup: Path, link: Path, needle: str, kind: str):
    _AUDIT.update(
        armed=True,
        swapped=False,
        victim=str(victim),
        backup=str(backup),
        link=str(link),
        needle=needle,
        kind=kind,
    )


def _disarm_swap():
    _AUDIT["armed"] = False


_install_audit_hook()


PRESENTATION_EMPTY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst></p:sldIdLst>
</p:presentation>
"""

PRESENTATION_ONE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
</p:presentation>
"""

CONTENT_TYPES_EMPTY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
</Types>
"""

CONTENT_TYPES_SLIDE1 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>
"""

CONTENT_TYPES_SLIDE1_AND_9 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slides/slide9.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>
"""

RELS_EMPTY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
</Relationships>
"""

RELS_SLIDE1 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""

RELS_SLIDE1_AND_9 = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
  <Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide9.xml"/>
</Relationships>
"""

SLIDE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree/></p:cSld></p:sld>
"""

SLIDE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""


def _snapshot(root: Path):
    root = Path(root)
    files = {}
    directories = []
    if not root.exists() and not root.is_symlink():
        return {"missing": True, "files": files, "directories": directories}
    if root.is_symlink():
        return {
            "missing": False,
            "files": {".": ("symlink", os.readlink(root))},
            "directories": directories,
        }
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        rel_dir = str(base.relative_to(root))
        if rel_dir != ".":
            directories.append(rel_dir)
        kept = []
        for name in list(dirnames):
            path = base / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                files[rel] = ("symlink", os.readlink(path))
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = base / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                files[rel] = ("symlink", os.readlink(path))
            elif path.is_file():
                files[rel] = ("file", path.read_bytes())
            else:
                files[rel] = ("other", path.lstat().st_mode)
    directories.sort()
    return {"missing": False, "files": files, "directories": directories}


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _deck(root: Path, presentation: str, content_types: str, rels: str) -> Path:
    deck = root / "deck"
    _write(deck / "ppt" / "presentation.xml", presentation)
    _write(deck / "[Content_Types].xml", content_types)
    _write(deck / "ppt" / "_rels" / "presentation.xml.rels", rels)
    return deck


def _add_real_slide(deck: Path, name: str = "slide1.xml", body: str = SLIDE_XML):
    _write(deck / "ppt" / "slides" / name, body)
    _write(deck / "ppt" / "slides" / "_rels" / f"{name}.rels", SLIDE_RELS)


def _add_layout(deck: Path):
    _write(deck / "ppt" / "slideLayouts" / "slideLayout1.xml", "<p:sldLayout/>")


class ContainmentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pptx-contain-")
        self.root = Path(self._tmp.name)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        _disarm_swap()

    def tearDown(self):
        _disarm_swap()
        self._tmp.cleanup()

    def assertRefusedWithoutChanges(self, roots, fn):
        before = [_snapshot(path) for path in roots]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = fn()
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            after = [_snapshot(path) for path in roots]
            self.assertEqual(
                before,
                after,
                f"{type(exc).__name__}: {exc} changed the deck or the link target",
            )
            return exc
        self.fail(f"operation completed and returned {result!r}")

    def assertOutsideUntouched(self, fn):
        before = _snapshot(self.outside)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fn()
        except KeyboardInterrupt:
            raise
        except BaseException:
            pass
        finally:
            _disarm_swap()
        self.assertEqual(before, _snapshot(self.outside))


class AddSlideContainmentTests(ContainmentTestCase):
    def _empty_deck(self) -> Path:
        deck = _deck(self.root, PRESENTATION_EMPTY, CONTENT_TYPES_EMPTY, RELS_EMPTY)
        _add_layout(deck)
        return deck

    def _one_slide_deck(self) -> Path:
        deck = _deck(self.root, PRESENTATION_ONE, CONTENT_TYPES_SLIDE1, RELS_SLIDE1)
        _add_real_slide(deck)
        _add_layout(deck)
        return deck

    def test_duplicate_and_layout_stay_inside_a_real_directory(self):
        deck = self._one_slide_deck()
        with contextlib.redirect_stdout(io.StringIO()):
            duplicated = add_slide.add_slide(deck, "slide1.xml")
            created = add_slide.add_slide(deck, "slideLayout1.xml")
        slides = deck / "ppt" / "slides"
        self.assertEqual(duplicated, "slide2.xml")
        self.assertEqual(created, "slide3.xml")
        self.assertTrue((slides / "slide2.xml").is_file())
        self.assertFalse((slides / "slide2.xml").is_symlink())
        self.assertTrue((slides / "slide3.xml").is_file())
        self.assertTrue((slides / "_rels" / "slide2.xml.rels").is_file())
        self.assertTrue((slides / "_rels" / "slide3.xml.rels").is_file())
        self.assertEqual(_snapshot(self.outside)["files"], {})

    def test_layout_slides_symlink_does_not_create_files_outside(self):
        deck = self._empty_deck()
        os.symlink(self.outside, deck / "ppt" / "slides")
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slideLayout1.xml"),
        )

    def test_cli_layout_slides_symlink_does_not_create_files_outside(self):
        deck = self._empty_deck()
        os.symlink(self.outside, deck / "ppt" / "slides")
        before = [_snapshot(deck), _snapshot(self.outside)]

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "add_slide.py"),
                str(deck),
                "slideLayout1.xml",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(before, [_snapshot(deck), _snapshot(self.outside)])

    def test_duplicate_normal_name_through_slides_symlink_does_not_write_outside(self):
        deck = self._one_slide_deck()
        _write(self.outside / "slide1.xml", SLIDE_XML)
        _write(self.outside / "_rels" / "slide1.xml.rels", SLIDE_RELS)
        # Registration still reads the real presentation.xml. slide2 is not
        # pre-registered, so the stale-registration check does not stop the copy.
        held = deck / "ppt" / "slides_held"
        os.rename(deck / "ppt" / "slides", held)
        os.symlink(self.outside, deck / "ppt" / "slides")
        self.assertRefusedWithoutChanges(
            [deck, self.outside, held],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_duplicate_absolute_source_through_slides_symlink_does_not_write_outside(self):
        deck = self._empty_deck()
        secret = self.root / "secret.xml"
        secret.write_text("SECRET-PAYLOAD", encoding="utf-8")
        os.symlink(self.outside, deck / "ppt" / "slides")
        self.assertRefusedWithoutChanges(
            [deck, self.outside, secret],
            lambda: add_slide.add_slide(deck, str(secret)),
        )

    def test_cli_duplicate_absolute_source_through_slides_symlink(self):
        deck = self._empty_deck()
        secret = self.root / "secret.xml"
        secret.write_text("SECRET-PAYLOAD", encoding="utf-8")
        os.symlink(self.outside, deck / "ppt" / "slides")
        before = [_snapshot(deck), _snapshot(self.outside), secret.read_bytes()]
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "add_slide.py"), str(deck), str(secret)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(
            before,
            [_snapshot(deck), _snapshot(self.outside), secret.read_bytes()],
        )

    def test_duplicate_absolute_source_into_real_slides_is_refused(self):
        deck = self._one_slide_deck()
        secret = self.root / "secret.xml"
        secret.write_text("SECRET-PAYLOAD", encoding="utf-8")
        self.assertRefusedWithoutChanges(
            [deck, self.outside, secret],
            lambda: add_slide.add_slide(deck, str(secret)),
        )

    def test_duplicate_parent_traversal_source_is_refused(self):
        deck = self._one_slide_deck()
        secret = self.root / "secret.xml"
        secret.write_text("SECRET-PAYLOAD", encoding="utf-8")
        source = os.path.relpath(secret, deck / "ppt" / "slides")
        self.assertNotEqual(source, "slide1.xml")
        self.assertRefusedWithoutChanges(
            [deck, self.outside, secret],
            lambda: add_slide.add_slide(deck, source),
        )

    def _populate_named(self, root: Path) -> Path:
        deck = _deck(root, PRESENTATION_ONE, CONTENT_TYPES_SLIDE1, RELS_SLIDE1)
        _add_real_slide(deck)
        return deck

    def test_source_names_that_are_not_a_single_slide_file_are_refused(self):
        for index, source in enumerate(("./slide1.xml", "slide1.xml/")):
            with self.subTest(source=source):
                deck = self._populate_named(self.root / f"name-{index}")
                self.assertRefusedWithoutChanges(
                    [deck, self.outside],
                    lambda source=source, deck=deck: add_slide.add_slide(deck, source),
                )

    def test_layout_name_that_is_not_a_single_file_is_refused(self):
        deck = self._empty_deck()
        secret = self.root / "secret.xml"
        secret.write_text("SECRET-LAYOUT", encoding="utf-8")
        (deck / "ppt" / "slideLayouts" / "slideLayout").mkdir(parents=True, exist_ok=True)
        source = "slideLayout/../../../../secret.xml"
        self.assertTrue((deck / "ppt" / "slideLayouts" / source).exists())
        self.assertRefusedWithoutChanges(
            [deck, self.outside, secret],
            lambda: add_slide.add_slide(deck, source),
        )

    def test_source_slide_symlink_is_not_followed(self):
        deck = self._one_slide_deck()
        secret = self.outside / "secret.xml"
        secret.write_text("SECRET-PAYLOAD", encoding="utf-8")
        slide = deck / "ppt" / "slides" / "slide1.xml"
        slide.unlink()
        os.symlink(secret, slide)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_source_slide_symlink_inside_the_deck_is_not_followed(self):
        deck = self._one_slide_deck()
        slides = deck / "ppt" / "slides"
        real = slides / "slide1-real.xml"
        real.write_text(SLIDE_XML, encoding="utf-8")
        slide = slides / "slide1.xml"
        slide.unlink()
        os.symlink(real.name, slide)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_source_rels_symlink_is_not_followed(self):
        deck = self._one_slide_deck()
        secret = self.outside / "secret.rels"
        secret.write_text(SLIDE_RELS, encoding="utf-8")
        rels = deck / "ppt" / "slides" / "_rels" / "slide1.xml.rels"
        rels.unlink()
        os.symlink(secret, rels)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_rels_directory_symlink_does_not_receive_the_new_rels(self):
        deck = self._one_slide_deck()
        rels_dir = deck / "ppt" / "slides" / "_rels"
        held = deck / "ppt" / "slides" / "_rels_held"
        os.rename(rels_dir, held)
        os.symlink(self.outside, rels_dir)
        self.assertRefusedWithoutChanges(
            [deck, self.outside, held],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_slides_symlink_whose_target_is_inside_the_deck_is_refused(self):
        deck = self._one_slide_deck()
        slides = deck / "ppt" / "slides"
        held = deck / "ppt" / "slides_held"
        os.rename(slides, held)
        os.symlink(held, slides)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_content_types_symlink_is_not_written(self):
        deck = self._one_slide_deck()
        marker = self.outside / "content-types.xml"
        current = deck / "[Content_Types].xml"
        marker.write_bytes(current.read_bytes())
        current.unlink()
        os.symlink(marker, current)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_presentation_xml_symlink_is_not_written(self):
        deck = self._one_slide_deck()
        marker = self.outside / "presentation.xml"
        current = deck / "ppt" / "presentation.xml"
        marker.write_bytes(current.read_bytes())
        current.unlink()
        os.symlink(marker, current)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def test_presentation_rels_symlink_is_not_written(self):
        deck = self._one_slide_deck()
        marker = self.outside / "presentation.xml.rels"
        current = deck / "ppt" / "_rels" / "presentation.xml.rels"
        marker.write_bytes(current.read_bytes())
        current.unlink()
        os.symlink(marker, current)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: add_slide.add_slide(deck, "slide1.xml"),
        )

    def _arm_directory_swap(self, victim: Path):
        link = victim.parent / (victim.name + "_link")
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(self.outside, link)
        _arm_swap(
            victim,
            victim.parent / (victim.name + "_held"),
            link,
            str(victim) + os.sep,
            "dir",
        )

    def _arm_file_swap(self, victim: Path, marker_name: str):
        marker = self.outside / marker_name
        marker.write_text("SENTINEL", encoding="utf-8")
        link = victim.parent / (victim.name + ".link")
        os.symlink(marker, link)
        _arm_swap(victim, victim.parent / (victim.name + ".held"), link, str(victim), "file")

    def test_slide_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._one_slide_deck()
        self._arm_directory_swap(deck / "ppt" / "slides")
        self.assertOutsideUntouched(lambda: add_slide.add_slide(deck, "slide1.xml"))

    def test_layout_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._empty_deck()
        (deck / "ppt" / "slides").mkdir()
        self._arm_directory_swap(deck / "ppt" / "slides")
        self.assertOutsideUntouched(lambda: add_slide.add_slide(deck, "slideLayout1.xml"))

    def test_content_types_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._one_slide_deck()
        self._arm_file_swap(deck / "[Content_Types].xml", "ct-sentinel.xml")
        self.assertOutsideUntouched(lambda: add_slide.add_slide(deck, "slide1.xml"))

    def test_presentation_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._one_slide_deck()
        self._arm_file_swap(deck / "ppt" / "presentation.xml", "pres-sentinel.xml")
        self.assertOutsideUntouched(lambda: add_slide.add_slide(deck, "slide1.xml"))

    def test_presentation_rels_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._one_slide_deck()
        self._arm_file_swap(
            deck / "ppt" / "_rels" / "presentation.xml.rels",
            "rels-sentinel.xml",
        )
        self.assertOutsideUntouched(lambda: add_slide.add_slide(deck, "slide1.xml"))


class CleanContainmentTests(ContainmentTestCase):
    def _referenced_deck(self, presentation_rels: str = RELS_SLIDE1, root: Path | None = None) -> Path:
        deck = _deck(root or self.root, PRESENTATION_ONE, CONTENT_TYPES_SLIDE1, presentation_rels)
        _add_real_slide(deck, "slide1.xml")
        return deck

    def test_clean_removes_an_orphan_slide_inside_a_real_directory(self):
        deck = self._referenced_deck()
        _add_real_slide(deck, "slide9.xml", "<p:sld>orphan</p:sld>")
        removed = clean.clean_unused_files(deck)
        self.assertIn("ppt/slides/slide9.xml", removed)
        self.assertFalse((deck / "ppt" / "slides" / "slide9.xml").exists())
        self.assertTrue((deck / "ppt" / "slides" / "slide1.xml").is_file())
        self.assertEqual(_snapshot(self.outside)["files"], {})

    def test_clean_slides_symlink_does_not_delete_outside(self):
        deck = _deck(self.root, PRESENTATION_ONE, CONTENT_TYPES_SLIDE1, RELS_SLIDE1)
        _write(self.outside / "slide1.xml", SLIDE_XML)
        _write(self.outside / "slide9.xml", "DELETE-ME")
        os.symlink(self.outside, deck / "ppt" / "slides")
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: clean.clean_unused_files(deck),
        )

    def test_cli_clean_slides_symlink_does_not_delete_outside(self):
        deck = _deck(self.root, PRESENTATION_ONE, CONTENT_TYPES_SLIDE1, RELS_SLIDE1)
        _write(self.outside / "slide1.xml", SLIDE_XML)
        _write(self.outside / "slide9.xml", "DELETE-ME")
        os.symlink(self.outside, deck / "ppt" / "slides")
        before = [_snapshot(deck), _snapshot(self.outside)]
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "clean.py"), str(deck)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(before, [_snapshot(deck), _snapshot(self.outside)])

    def test_clean_content_types_symlink_is_not_written(self):
        deck = self._referenced_deck()
        _add_real_slide(deck, "slide9.xml", "<p:sld>orphan</p:sld>")
        current = deck / "[Content_Types].xml"
        current.write_text(CONTENT_TYPES_SLIDE1_AND_9, encoding="utf-8")
        marker = self.outside / "content-types.xml"
        marker.write_bytes(current.read_bytes())
        current.unlink()
        os.symlink(marker, current)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: clean.clean_unused_files(deck),
        )

    def test_clean_presentation_rels_symlink_is_not_written(self):
        deck = self._referenced_deck(RELS_SLIDE1_AND_9)
        _add_real_slide(deck, "slide9.xml", "<p:sld>orphan</p:sld>")
        current = deck / "ppt" / "_rels" / "presentation.xml.rels"
        marker = self.outside / "presentation.xml.rels"
        marker.write_bytes(current.read_bytes())
        current.unlink()
        os.symlink(marker, current)
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: clean.clean_unused_files(deck),
        )

    def test_clean_resource_directory_symlink_does_not_delete_outside(self):
        names = (
            "media",
            "embeddings",
            "charts",
            "diagrams",
            "tags",
            "drawings",
            "ink",
        )
        for name in names:
            with self.subTest(directory=name):
                base = self.root / name
                deck = self._referenced_deck(root=base)
                target = base / "outside"
                target.mkdir()
                payload = target / "payload.bin"
                payload.write_bytes(b"keep")
                os.symlink(target, deck / "ppt" / name)
                self.assertRefusedWithoutChanges(
                    [deck, target],
                    lambda deck=deck: clean.clean_unused_files(deck),
                )

    def test_clean_theme_symlink_does_not_delete_outside(self):
        deck = self._referenced_deck()
        payload = self.outside / "theme1.xml"
        payload.write_text("<a:theme/>", encoding="utf-8")
        rels = self.outside / "_rels" / "theme1.xml.rels"
        _write(rels, RELS_EMPTY)
        os.symlink(self.outside, deck / "ppt" / "theme")
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: clean.clean_unused_files(deck),
        )

    def test_clean_notes_symlink_does_not_delete_outside(self):
        deck = self._referenced_deck()
        _write(self.outside / "notesSlide1.xml", "<p:notes/>")
        _write(self.outside / "_rels" / "notesSlide1.xml.rels", RELS_EMPTY)
        os.symlink(self.outside, deck / "ppt" / "notesSlides")
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: clean.clean_unused_files(deck),
        )

    def test_clean_orphaned_rels_symlink_does_not_delete_outside(self):
        for name in ("charts", "diagrams", "drawings"):
            with self.subTest(directory=name):
                base = self.root / f"rels-{name}"
                deck = self._referenced_deck(root=base)
                target = base / "outside"
                target.mkdir()
                payload = target / "orphan.xml.rels"
                payload.write_text(RELS_EMPTY, encoding="utf-8")
                (deck / "ppt" / name).mkdir(parents=True, exist_ok=True)
                os.symlink(target, deck / "ppt" / name / "_rels")
                self.assertRefusedWithoutChanges(
                    [deck, target],
                    lambda deck=deck: clean.clean_unused_files(deck),
                )

    def test_clean_trash_symlink_does_not_delete_outside(self):
        deck = self._referenced_deck()
        payload = self.outside / "note.txt"
        payload.write_text("keep", encoding="utf-8")
        os.symlink(self.outside, deck / "[trash]")
        self.assertRefusedWithoutChanges(
            [deck, self.outside],
            lambda: clean.clean_unused_files(deck),
        )

    def test_clean_unlink_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._referenced_deck()
        _add_real_slide(deck, "slide9.xml", "<p:sld>orphan</p:sld>")
        precious = self.outside / "slide9.xml"
        precious.write_text("PRECIOUS", encoding="utf-8")
        slides = deck / "ppt" / "slides"
        link = deck / "ppt" / "slides_link"
        os.symlink(self.outside, link)
        _arm_swap(slides, deck / "ppt" / "slides_held", link, str(slides) + os.sep, "dir")
        self.assertOutsideUntouched(lambda: clean.clean_unused_files(deck))

    def test_clean_content_types_write_does_not_follow_a_link_swapped_in_after_the_check(self):
        deck = self._referenced_deck()
        _add_real_slide(deck, "slide9.xml", "<p:sld>orphan</p:sld>")
        current = deck / "[Content_Types].xml"
        current.write_text(CONTENT_TYPES_SLIDE1_AND_9, encoding="utf-8")
        marker = self.outside / "ct-sentinel.xml"
        marker.write_text("SENTINEL", encoding="utf-8")
        link = deck / "content-types.link"
        os.symlink(marker, link)
        _arm_swap(current, deck / "content-types.held", link, str(current), "file")
        self.assertOutsideUntouched(lambda: clean.clean_unused_files(deck))


if __name__ == "__main__":
    unittest.main()
