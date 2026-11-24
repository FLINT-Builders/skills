"""Specs for relationship scans that must stay linear in the size of the part.

They fail while a lazy closing-tag regular expression scans a slide or master
relationship file once per opening tag. They pass once those scans walk
Relationship elements directly, including when the part is not well-formed
and the scan rejects it.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parents[2]
sys.path.insert(0, str(SCRIPTS))

import add_slide  # noqa: E402

SMALL_REPEATS = 2000
LARGE_REPEATS = 8000  # four times SMALL_REPEATS

NOTES_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
LAYOUT_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
IMAGE_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

_THEME_DRIVER = r"""
import sys
import time

sys.path.insert(0, sys.argv[1])
from helpers.pptx_theme import live_shared_master_themes

repeats = int(sys.argv[2])
comment = "<!--" + ("<Relationship" * repeats) + "-->"
text = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    + comment
    + "\n</Relationships>\n"
)
files = {
    "ppt/slideMasters/slideMaster1.xml": b"<p:sldMaster/>",
    "ppt/slideMasters/_rels/slideMaster1.xml.rels": text.encode(),
}
started = time.perf_counter()
live_shared_master_themes(files)
print(f"{time.perf_counter() - started:.6f}", flush=True)
"""


def _assert_subquadratic(small_seconds: float, large_seconds: float) -> None:
    """Four times the tags must not cost about sixteen times the CPU.

    A linear scan lands near four times the smaller timing. The expression
    under test lands near sixteen. Eight times, plus a small allowance for
    the clock, separates the two.
    """

    limit = small_seconds * 8 + 0.15
    if large_seconds >= limit:
        raise AssertionError(
            f"{LARGE_REPEATS} tags took {large_seconds:.3f}s after "
            f"{SMALL_REPEATS} tags took {small_seconds:.3f}s "
            f"(limit {limit:.3f}s)"
        )


def _run_duplication(work) -> float:
    started = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        try:
            work()
        except SystemExit as exc:
            raise AssertionError(f"slide duplication exited: {exc}") from exc
        except Exception:
            # Rejecting the part from a real XML parse is a successful fix,
            # as long as the rejection itself is not quadratic.
            pass
    return time.perf_counter() - started


def _write_minimal_deck(root: Path, slide_rels: str) -> None:
    slides = root / "ppt" / "slides"
    (slides / "_rels").mkdir(parents=True)
    (root / "ppt" / "_rels").mkdir(parents=True)
    (slides / "slide1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sld/>\n',
        encoding="utf-8",
    )
    (slides / "_rels" / "slide1.xml.rels").write_text(slide_rels, encoding="utf-8")
    (root / "ppt" / "presentation.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
</p:presentation>
""",
        encoding="utf-8",
    )
    (root / "ppt" / "_rels" / "presentation.xml.rels").write_text(
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
""",
        encoding="utf-8",
    )
    (root / "[Content_Types].xml").write_text(
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>
""",
        encoding="utf-8",
    )


def _zip_deck(root: Path, package: Path) -> None:
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())


class SlideDuplicationRelationshipTests(unittest.TestCase):
    def test_duplicate_slide_drops_speaker_note_relationships(self) -> None:
        rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{LAYOUT_TYPE}" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="{NOTES_TYPE}" Target="../notesSlides/notesSlide1.xml"/>
  <Relationship Id="rId3" Type='{IMAGE_TYPE}' Target="../media/image1.png"/>
  <Relationship Id="rId4" Type="{NOTES_TYPE}" Target="../notesSlides/notesSlide2.xml"></Relationship>
</Relationships>
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_minimal_deck(root, rels)
            with redirect_stdout(io.StringIO()):
                dest = add_slide.add_slide(root, "slide1.xml")
            written = (root / "ppt" / "slides" / "_rels" / f"{dest}.rels").read_text(
                encoding="utf-8"
            )

        self.assertEqual(dest, "slide2.xml")
        self.assertNotIn("notesSlide", written)
        self.assertIn(LAYOUT_TYPE, written)
        self.assertIn("../slideLayouts/slideLayout1.xml", written)
        self.assertIn(IMAGE_TYPE, written)
        self.assertIn("../media/image1.png", written)

    def test_unclosed_start_tags_in_slide_rels_are_not_quadratic(self) -> None:
        def once(repeats: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_minimal_deck(root, "<Relationship" * repeats)
                add_slide.add_slide(root, "slide1.xml")

        _assert_subquadratic(_run_duplication(lambda: once(SMALL_REPEATS)), _run_duplication(lambda: once(LARGE_REPEATS)))

    def test_unclosed_relationship_elements_in_slide_rels_are_not_quadratic(self) -> None:
        def once(repeats: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write_minimal_deck(root, "<Relationship>" * repeats)
                add_slide.add_slide(root, "slide1.xml")

        _assert_subquadratic(_run_duplication(lambda: once(SMALL_REPEATS)), _run_duplication(lambda: once(LARGE_REPEATS)))

    def test_packed_pptx_unclosed_start_tags_are_not_quadratic(self) -> None:
        def once(repeats: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                unpacked = root / "unpacked"
                _write_minimal_deck(unpacked, "<Relationship" * repeats)
                package = root / "deck.pptx"
                output = root / "out.pptx"
                _zip_deck(unpacked, package)
                add_slide.add_slide_to_package(package, "slide1.xml", output=output)

        _assert_subquadratic(_run_duplication(lambda: once(SMALL_REPEATS)), _run_duplication(lambda: once(LARGE_REPEATS)))

    def test_master_theme_relationship_scan_is_not_quadratic(self) -> None:
        offices = {
            "pptx": REPO / "skills" / "pptx" / "scripts" / "office",
            "docx": REPO / "skills" / "docx" / "scripts" / "office",
            "xlsx": REPO / "skills" / "xlsx" / "scripts" / "office",
        }
        for skill, office in offices.items():
            with self.subTest(skill=skill):
                small = _theme_scan_seconds(office, SMALL_REPEATS)
                large = _theme_scan_seconds(office, LARGE_REPEATS)
                try:
                    _assert_subquadratic(small, large)
                except AssertionError as exc:
                    raise AssertionError(f"{skill}: {exc}") from exc


def _theme_scan_seconds(office: Path, repeats: int) -> float:
    completed = subprocess.run(
        [sys.executable, "-c", _THEME_DRIVER, str(office), str(repeats)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"theme scan failed for {office}:\n{completed.stderr}"
        )
    return float(completed.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
