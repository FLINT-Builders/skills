"""Source paths passed to add_slide must stay inside the deck.

`add_slide.py` is told to duplicate `slideN.xml` or to instantiate
`slideLayoutN.xml`. These tests are the confinement spec for that argument:

- The source must be a single path segment matching `slide<digits>.xml` or
  `slideLayout<digits>.xml`. A basename check that then copies the original
  string is not enough: `../../slide1.xml` still has a slide basename and
  still resolves outside the deck.
- The resolved slide path must stay inside `ppt/slides`, and the resolved
  layout path must stay inside `ppt/slideLayouts`. A symlink whose name
  matches the pattern still points outside and must be refused.
- The sibling `.rels` lookup (`<source>.rels` under `ppt/slides/_rels`) uses
  the same caller-supplied string and must be refused with it.
- Refusal is `SystemExit` with a non-zero code and must not create, delete,
  or modify any file. That includes a packed deck's output and the input
  package when the script rewrites it in place.

These tests fail while the unsanitized join is still in place.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import add_slide  # noqa: E402

INSIDE_SLIDE = b"INSIDE_SLIDE_CONTENT"
INSIDE_LAYOUT = b"INSIDE_LAYOUT_CONTENT"
OUTSIDE_SLIDE = b"OUTSIDE_SLIDE_MARKER"
OUTSIDE_RELS = b"OUTSIDE_RELS_MARKER"
OUTPUT_SENTINEL = b"OUTPUT_SENTINEL_NOT_A_DECK"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
</Types>
"""

PRESENTATION = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
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


def snapshot(root: Path) -> tuple:
    """File names, symlink targets, and bytes under root. Does not follow links."""
    items = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                items.append((rel, "symlink", os.readlink(path)))
            else:
                items.append((rel, "file", path.read_bytes()))
    return tuple(sorted(items))


def marker_hits(data: bytes) -> list[str]:
    hits = []
    if OUTSIDE_SLIDE in data:
        hits.append("outside-slide")
    if OUTSIDE_RELS in data:
        hits.append("outside-rels")
    return hits


def leaks_under(root: Path) -> list[str]:
    found = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            hits = marker_hits(path.read_bytes())
            if hits:
                found.append(f"{path.relative_to(root).as_posix()} ({', '.join(hits)})")
    return found


def leaks_in_zip(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        with zipfile.ZipFile(path) as zf:
            found = []
            for info in zf.infolist():
                hits = marker_hits(zf.read(info))
                if hits:
                    found.append(f"{info.filename} ({', '.join(hits)})")
            return found
    except zipfile.BadZipFile:
        return ["<not a zip>"]


def write_deck(unpacked: Path, slide: bytes = INSIDE_SLIDE, layout: bytes = INSIDE_LAYOUT) -> None:
    slides = unpacked / "ppt" / "slides"
    layouts = unpacked / "ppt" / "slideLayouts"
    (slides / "_rels").mkdir(parents=True)
    layouts.mkdir(parents=True)
    (unpacked / "ppt" / "_rels").mkdir(parents=True)
    (unpacked / "[Content_Types].xml").write_text(CONTENT_TYPES, encoding="utf-8")
    (unpacked / "ppt" / "presentation.xml").write_text(PRESENTATION, encoding="utf-8")
    (unpacked / "ppt" / "_rels" / "presentation.xml.rels").write_text(
        PRESENTATION_RELS, encoding="utf-8"
    )
    (slides / "slide1.xml").write_bytes(slide)
    (slides / "_rels" / "slide1.xml.rels").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>\n',
        encoding="utf-8",
    )
    (layouts / "slideLayout1.xml").write_bytes(layout)


def pack_deck(unpacked: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as zf:
        for dirpath, _dirnames, filenames in os.walk(unpacked):
            for name in filenames:
                path = Path(dirpath) / name
                zf.write(path, path.relative_to(unpacked).as_posix())


def rel_source(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, from_dir)).as_posix()


class AddSlideSourceConfinementTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.unpacked = self.root / "unpacked"
        write_deck(self.unpacked)
        self.slides = self.unpacked / "ppt" / "slides"
        self.layouts = self.unpacked / "ppt" / "slideLayouts"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assert_rejected(self, source: str, after: str | None = None) -> None:
        before = snapshot(self.unpacked)
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            try:
                created = add_slide.add_slide(self.unpacked, source, after)
            except SystemExit as exc:
                self.assertNotIn(
                    exc.code,
                    (0, None),
                    f"{source!r} exited {exc.code!r}; refusal must be non-zero",
                )
            else:
                self.fail(
                    f"add_slide accepted {source!r} and created {created}; "
                    f"outside bytes landed in {leaks_under(self.unpacked)}"
                )
        self.assertEqual(
            before,
            snapshot(self.unpacked),
            f"rejecting {source!r} still changed the unpacked deck",
        )

    def test_legitimate_slide_duplicate_copies_that_slide(self) -> None:
        outside = self.root / "slide9.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            created = add_slide.add_slide(self.unpacked, "slide1.xml")
        self.assertEqual(created, "slide2.xml")
        self.assertEqual((self.slides / created).read_bytes(), INSIDE_SLIDE)
        self.assertEqual(leaks_under(self.unpacked), [])

    def test_legitimate_layout_creates_minimal_slide(self) -> None:
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            created = add_slide.add_slide(self.unpacked, "slideLayout1.xml")
        slide = (self.slides / created).read_bytes()
        rels = (self.slides / "_rels" / f"{created}.rels").read_text(encoding="utf-8")
        self.assertIn(b"<p:sld", slide)
        self.assertNotIn(OUTSIDE_SLIDE, slide)
        self.assertIn("slideLayouts/slideLayout1.xml", rels)
        self.assertNotIn(INSIDE_LAYOUT.decode(), rels)

    def test_rejects_relative_source_whose_basename_is_a_real_slide(self) -> None:
        """`../../slide1.xml` has a legal basename and a different target.

        A check that resolves only `slide1.xml` and then copies the original
        source string still copies the outside file, and still copies
        `<source>.rels` from outside `ppt/slides/_rels`.
        """
        outside = self.root / "slide1.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        source = rel_source(self.slides, outside)
        self.assertNotEqual(source, "slide1.xml")
        self.assertEqual(Path(source).name, "slide1.xml")
        self.assertEqual((self.slides / source).resolve(), outside.resolve())
        self.assertNotEqual((self.slides / source).resolve(), (self.slides / "slide1.xml").resolve())

        rels_target = (self.slides / "_rels" / f"{source}.rels").resolve()
        rels_target.parent.mkdir(parents=True, exist_ok=True)
        rels_target.write_bytes(OUTSIDE_RELS)
        self.assertFalse(rels_target.is_relative_to(self.slides.resolve()))

        self.assert_rejected(source)

    def test_rejects_absolute_source_whose_basename_is_a_real_slide(self) -> None:
        outside = self.root / "elsewhere" / "slide1.xml"
        outside.parent.mkdir()
        outside.write_bytes(OUTSIDE_SLIDE)
        source = str(outside.resolve())
        self.assertEqual(Path(source).name, "slide1.xml")
        self.assertEqual((self.slides / source).resolve(), outside.resolve())
        rels_target = (self.slides / "_rels" / f"{source}.rels").resolve()
        rels_target.parent.mkdir(parents=True, exist_ok=True)
        rels_target.write_bytes(OUTSIDE_RELS)
        self.assert_rejected(source)

    def test_rejects_basename_that_is_not_a_slide_or_layout_name(self) -> None:
        notes = self.slides / "notes.txt"
        notes.write_bytes(OUTSIDE_SLIDE)
        self.assert_rejected("notes.txt")

    def test_rejects_dotdot_that_normalizes_back_inside_slides(self) -> None:
        # The intermediate directory has to exist. A missing component makes
        # exists() fail before the copy, which is not the same as confinement.
        (self.slides / "nested").mkdir()
        source = "nested/../slide1.xml"
        self.assertEqual((self.slides / source).resolve(), (self.slides / "slide1.xml").resolve())
        self.assertTrue((self.slides / source).exists())
        self.assert_rejected(source)

    def test_rejects_rels_symlink_when_the_slide_file_stays_inside(self) -> None:
        """The second copy joins ``<source>.rels`` under ``ppt/slides/_rels``.

        A slide basename that resolves inside still copies an outside file when
        that sibling path is a symlink. Checking only the slide part, then
        copying the ``.rels`` path, leaves the outside bytes in the new slide.
        """
        outside = self.root / "linked.rels"
        outside.write_bytes(OUTSIDE_RELS)
        rels = self.slides / "_rels" / "slide1.xml.rels"
        rels.unlink()
        rels.symlink_to(outside)
        self.assertEqual(rels.resolve(), outside.resolve())
        self.assert_rejected("slide1.xml")

    def test_rejects_symlink_slide_whose_target_is_outside_slides(self) -> None:
        outside = self.root / "linked-slide.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        link = self.slides / "slide2.xml"
        link.symlink_to(outside)
        self.assertEqual(link.resolve(), outside.resolve())
        self.assert_rejected("slide2.xml")

    def test_rejects_layout_source_whose_basename_matches_but_path_escapes(self) -> None:
        """Layout branch: prefix `slideLayout` and suffix `.xml` is not a basename.

        `slideLayout9.xml/../../../../slideLayout9.xml` is one string. Checking
        only the basename `slideLayout9.xml` (which exists inside slideLayouts)
        and then joining the original string uses the outside file.
        """
        # `slideLayout` must be a real directory. Walking through a missing
        # component, or through a file, makes exists() fail before any write.
        (self.layouts / "slideLayout").mkdir()
        (self.layouts / "slideLayout9.xml").write_bytes(INSIDE_LAYOUT)
        outside = self.root / "slideLayout9.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        source = "slideLayout/../../../../slideLayout9.xml"
        self.assertTrue(source.startswith("slideLayout") and source.endswith(".xml"))
        self.assertEqual(Path(source).name, "slideLayout9.xml")
        self.assertEqual(
            (self.layouts / Path(source).name).resolve(),
            (self.layouts / "slideLayout9.xml").resolve(),
        )
        self.assertNotEqual((self.layouts / source).resolve(), (self.layouts / Path(source).name).resolve())
        self.assertEqual((self.layouts / source).resolve(), outside.resolve())
        self.assertTrue((self.layouts / source).is_file())
        self.assert_rejected(source)

    def test_rejects_layout_symlink_whose_target_is_outside_layouts(self) -> None:
        outside = self.root / "linked-layout.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        link = self.layouts / "slideLayout2.xml"
        link.symlink_to(outside)
        self.assert_rejected("slideLayout2.xml")

    def test_rejects_slideLayout_prefix_that_does_not_end_in_xml(self) -> None:
        """That shape is not the layout branch, so duplicate_slide copies it."""
        (self.slides / "slideLayout").mkdir()
        # The .rels join walks ppt/slides/_rels/<source>. The same intermediate
        # directory has to exist there, or exists() fails before that copy.
        (self.slides / "_rels" / "slideLayout").mkdir()
        outside = self.root / "secret.txt"
        outside.write_bytes(OUTSIDE_SLIDE)
        source = "slideLayout/../../../../secret.txt"
        self.assertTrue(source.startswith("slideLayout"))
        self.assertFalse(source.endswith(".xml"))
        self.assertEqual((self.slides / source).resolve(), outside.resolve())
        self.assertTrue((self.slides / source).is_file())
        rels_target = (self.slides / "_rels" / f"{source}.rels").resolve()
        rels_target.parent.mkdir(parents=True, exist_ok=True)
        rels_target.write_bytes(OUTSIDE_RELS)
        self.assert_rejected(source)

    def _run_cli(self, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "add_slide.py"), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_cli_unpacked_directory_rejects_escaping_source(self) -> None:
        outside = self.root / "slide1.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        source = rel_source(self.slides, outside)
        before = snapshot(self.unpacked)
        proc = self._run_cli([str(self.unpacked), source])
        self.assertNotEqual(
            proc.returncode,
            0,
            f"CLI accepted {source!r}; leaks={leaks_under(self.unpacked)}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}",
        )
        self.assertEqual(before, snapshot(self.unpacked))

    def test_package_duplicate_of_real_slide_writes_that_slide(self) -> None:
        package = self.root / "deck.pptx"
        output = self.root / "out.pptx"
        pack_deck(self.unpacked, package)
        original = package.read_bytes()
        proc = self._run_cli([str(package), "slide1.xml", "-o", str(output)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with zipfile.ZipFile(output) as zf:
            self.assertEqual(zf.read("ppt/slides/slide2.xml"), INSIDE_SLIDE)
        self.assertEqual(leaks_in_zip(output), [])
        self.assertEqual(package.read_bytes(), original)

    def test_add_slide_to_package_rejects_absolute_source_without_writing(self) -> None:
        outside = self.root / "elsewhere" / "slide1.xml"
        outside.parent.mkdir()
        outside.write_bytes(OUTSIDE_SLIDE)
        package = self.root / "deck.pptx"
        output = self.root / "out.pptx"
        pack_deck(self.unpacked, package)
        original = package.read_bytes()
        output.write_bytes(OUTPUT_SENTINEL)
        source = str(outside.resolve())
        # duplicate_slide joins "<source>.rels" onto ppt/slides/_rels. An absolute
        # source discards that directory, so the sibling is the absolute path
        # plus ".rels".
        Path(source + ".rels").write_bytes(OUTSIDE_RELS)
        before_names = set(os.listdir(self.root))
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            try:
                created = add_slide.add_slide_to_package(
                    package, source, None, output
                )
            except SystemExit as exc:
                self.assertNotIn(exc.code, (0, None))
            else:
                self.fail(
                    f"add_slide_to_package accepted {source!r} and created {created}; "
                    f"leaks={leaks_in_zip(output)}"
                )
        self.assertEqual(package.read_bytes(), original)
        self.assertEqual(output.read_bytes(), OUTPUT_SENTINEL)
        self.assertEqual(set(os.listdir(self.root)), before_names)

    def test_cli_package_rejects_parent_directory_source(self) -> None:
        """Parent-directory source from the temp dir add_slide_to_package extracts into.

        mkdtemp creates one directory under TMPDIR. The extracted slides live at
        `$TMPDIR/<rand>/ppt/slides`, so `../../../../secret-slide.xml` is the
        file next to TMPDIR no matter what `<rand>` is. The `.rels` copy joins
        that same string under `ppt/slides/_rels`, one level deeper.
        """
        work = self.root / "work"
        tmpdir = work / "tmpdir"
        tmpdir.mkdir(parents=True)
        outside = work / "secret-slide.xml"
        outside.write_bytes(OUTSIDE_SLIDE)
        source = "../../../../secret-slide.xml"
        dummy_slides = tmpdir / "rand" / "ppt" / "slides"
        self.assertEqual((dummy_slides / source).resolve(), outside.resolve())
        rels_outside = (dummy_slides / "_rels" / f"{source}.rels").resolve()
        self.assertEqual(rels_outside, (tmpdir / "secret-slide.xml.rels").resolve())
        rels_outside.write_bytes(OUTSIDE_RELS)

        package = work / "deck.pptx"
        output = work / "out.pptx"
        pack_deck(self.unpacked, package)
        original = package.read_bytes()
        output.write_bytes(OUTPUT_SENTINEL)
        env = os.environ.copy()
        env["TMPDIR"] = str(tmpdir)
        before_names = set(os.listdir(work))
        proc = self._run_cli(
            [str(package), source, "-o", str(output)],
            env=env,
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            f"CLI packed parent source; leaks={leaks_in_zip(output)}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}",
        )
        self.assertEqual(package.read_bytes(), original)
        self.assertEqual(output.read_bytes(), OUTPUT_SENTINEL)
        self.assertEqual(set(os.listdir(work)), before_names)

    def test_cli_inplace_package_rejects_absolute_source(self) -> None:
        outside = self.root / "elsewhere" / "slide1.xml"
        outside.parent.mkdir()
        outside.write_bytes(OUTSIDE_SLIDE)
        package = self.root / "deck.pptx"
        pack_deck(self.unpacked, package)
        original = package.read_bytes()
        before_names = set(os.listdir(self.root))
        proc = self._run_cli([str(package), str(outside.resolve())])
        self.assertNotEqual(
            proc.returncode,
            0,
            f"in-place rewrite accepted an absolute source; leaks={leaks_in_zip(package)}\n"
            f"stderr={proc.stderr}",
        )
        self.assertEqual(package.read_bytes(), original)
        self.assertEqual(set(os.listdir(self.root)), before_names)

    def test_cli_potx_rejects_absolute_source(self) -> None:
        outside = self.root / "elsewhere" / "slide1.xml"
        outside.parent.mkdir()
        outside.write_bytes(OUTSIDE_SLIDE)
        package = self.root / "template.potx"
        output = self.root / "out.potx"
        pack_deck(self.unpacked, package)
        original = package.read_bytes()
        output.write_bytes(OUTPUT_SENTINEL)
        proc = self._run_cli(
            [str(package), str(outside.resolve()), "-o", str(output)]
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            f"potx output accepted an absolute source; leaks={leaks_in_zip(output)}\n"
            f"stderr={proc.stderr}",
        )
        self.assertEqual(package.read_bytes(), original)
        self.assertEqual(output.read_bytes(), OUTPUT_SENTINEL)


if __name__ == "__main__":
    unittest.main()
