"""Reject untrusted slide-layout names before add_slide writes a relationship.

A deck can store a slideLayouts entry whose name starts with slideLayout,
ends with .xml, and contains quotes and relationship markup. add_slide.py
classifies that string as a layout and, after the file-exists check, pastes
it into the new slide's relationship Target. These tests fail while that
paste is reachable: the command must exit non-zero, leave the unpacked tree
and the input package unchanged, and must not create an output deck. A
later fix should accept only one path segment of the form slideLayout
followed by digits and .xml, and escape any value it writes into XML.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


REPO_SCRIPTS = Path(__file__).resolve().parents[1]
ADD_SLIDE = REPO_SCRIPTS / "add_slide.py"
SAFE_LAYOUT_NAME = re.compile(r"slideLayout\d+\.xml\Z")
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
HYPERLINK_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)
OLE_OBJECT_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject"
)
HTTP_TARGET = "http://example.com/p"
FILE_TARGET = "file:///tmp/payload.txt"
FORBIDDEN_TARGETS = (HTTP_TARGET, FILE_TARGET)


def _relationship_breakout(prefix: str, rel_type: str, target: str, tail: str) -> str:
    """Zip member name that closes the layout Target and adds another Relationship.

    The trailing characters of add_slide's relationship template are a
    closing quote and "/>", so the name itself ends with .xml and the
    inserted element is closed by that template text.
    """
    name = (
        f'{prefix}"/>'
        f'<Relationship Id="r2" Type="{rel_type}" '
        f'Target="{target}" TargetMode="External"/>'
        f'<Relationship Id="r1" Target="{tail}'
    )
    if "/" not in name or not name.startswith("slideLayout") or not name.endswith(".xml"):
        raise AssertionError(f"breakout name is not shaped like the finding: {name!r}")
    return name


HTTP_LAYOUT_NAME = _relationship_breakout(
    "slideLayout1.xml", HYPERLINK_TYPE, HTTP_TARGET, "x.xml"
)
FILE_LAYOUT_NAME = _relationship_breakout(
    "slideLayout2.xml", OLE_OBJECT_TYPE, FILE_TARGET, "y.xml"
)
# One path segment, so a non-recursive listing of ppt/slideLayouts returns it.
QUOTE_LAYOUT_NAME = 'slideLayout9.xml" TargetMode="External.xml'
NESTED_LAYOUT_NAME = "slideLayout3/slideLayout4.xml"
TRAVERSAL_LAYOUT_NAME = "slideLayout8/../slideLayout1.xml"


def _content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
</Types>
"""


def _presentation() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst></p:sldIdLst>
</p:presentation>
"""


def _presentation_rels() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
</Relationships>
"""


def build_deck(path: Path, extra_layouts: dict[str, str]) -> None:
    """Write a minimal pptx/potx whose slideLayouts names are extra_layouts."""
    members = {
        "[Content_Types].xml": _content_types(),
        "ppt/presentation.xml": _presentation(),
        "ppt/_rels/presentation.xml.rels": _presentation_rels(),
        "ppt/slideLayouts/slideLayout1.xml": "<layout>safe</layout>",
    }
    for name, body in extra_layouts.items():
        members[f"ppt/slideLayouts/{name}"] = body
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)


def layout_names_in_zip(package: Path) -> list[str]:
    """Layout names as stored in the package, relative to ppt/slideLayouts."""
    prefix = "ppt/slideLayouts/"
    names = []
    with zipfile.ZipFile(package) as zf:
        for name in zf.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                names.append(name[len(prefix) :])
    return names


def extract_skill_way(package: Path, dest: Path) -> None:
    """Unpack the way SKILL.md tells the agent to unpack a deck."""
    with zipfile.ZipFile(package) as zf:
        zf.extractall(dest)


def run_add_slide(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADD_SLIDE), *args],
        capture_output=True,
        text=True,
    )


def fingerprint(root: Path) -> list[tuple[str, str]]:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            rows.append((rel + "/", "dir"))
        elif path.is_file():
            rows.append((rel, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            rows.append((rel, "other"))
    return rows


def relationship_elements(xml_bytes: bytes) -> list[ET.Element]:
    root = ET.fromstring(xml_bytes)
    return list(root)


def slide_rels_in_tree(root: Path) -> list[tuple[str, bytes]]:
    rels_root = root / "ppt" / "slides" / "_rels"
    if not rels_root.exists():
        return []
    found = []
    for path in sorted(rels_root.rglob("*.rels")):
        if path.is_file():
            found.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return found


def slide_rels_in_package(package: Path) -> list[tuple[str, bytes]]:
    if not package.exists():
        return []
    found = []
    with zipfile.ZipFile(package) as zf:
        for name in zf.namelist():
            if name.startswith("ppt/slides/_rels/") and name.endswith(".rels"):
                found.append((name, zf.read(name)))
    return found


def assert_no_extra_targets(testcase: unittest.TestCase, rels: list[tuple[str, bytes]]) -> None:
    for name, data in rels:
        elements = relationship_elements(data)
        testcase.assertEqual(
            len(elements),
            1,
            f"{name} has extra relationships: {data!r}",
        )
        element = elements[0]
        testcase.assertNotIn(element.attrib.get("TargetMode"), ("External",))
        target = element.attrib.get("Target", "")
        for forbidden in FORBIDDEN_TARGETS:
            testcase.assertNotIn(forbidden, target)
        testcase.assertFalse(target.startswith("file:"))
        testcase.assertFalse(target.startswith("http:"))
        testcase.assertNotIn('"', target)
        testcase.assertNotIn("<", target)


class AddSlideLayoutNameTests(unittest.TestCase):
    def _deck(self, directory: Path, suffix: str, extra: dict[str, str]) -> Path:
        package = directory / f"deck{suffix}"
        build_deck(package, extra)
        return package

    def _assert_directory_rejected(
        self, source: str, extra: dict[str, str], *, must_be_zip_member: bool = True
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._deck(root, ".pptx", extra)
            unpacked = root / "unpacked"
            unpacked.mkdir()
            extract_skill_way(package, unpacked)
            if must_be_zip_member:
                self.assertIn(source, layout_names_in_zip(package))
            layout_path = unpacked / "ppt" / "slideLayouts" / source
            self.assertTrue(
                layout_path.exists(),
                f"extracted layout is not visible to the exists() check: {source!r}",
            )
            before = fingerprint(unpacked)
            result = run_add_slide([str(unpacked), source])
            self.assertNotEqual(
                result.returncode,
                0,
                self._failure_detail(result, unpacked, None),
            )
            self.assertEqual(
                fingerprint(unpacked),
                before,
                "add_slide changed the unpacked deck before refusing the layout name",
            )
            assert_no_extra_targets(self, slide_rels_in_tree(unpacked))

    def _assert_package_rejected(self, source: str, suffix: str, extra: dict[str, str], output: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._deck(root, suffix, extra)
            before_hash = hashlib.sha256(package.read_bytes()).hexdigest()
            unpacked = root / "check"
            unpacked.mkdir()
            sys.path.insert(0, str(REPO_SCRIPTS))
            from office.helpers import safe_extract

            with zipfile.ZipFile(package) as zf:
                safe_extract(zf, unpacked)
            self.assertTrue((unpacked / "ppt" / "slideLayouts" / source).exists())
            out = root / f"out{suffix}"
            args = [str(package), source]
            if output:
                args.extend(["-o", str(out)])
            result = run_add_slide(args)
            self.assertNotEqual(
                result.returncode,
                0,
                self._failure_detail(result, unpacked, out if output else package),
            )
            self.assertEqual(
                hashlib.sha256(package.read_bytes()).hexdigest(),
                before_hash,
                "add_slide rewrote the input package before refusing the layout name",
            )
            if output:
                self.assertFalse(out.exists(), "add_slide created an output deck for a rejected layout")
            else:
                assert_no_extra_targets(self, slide_rels_in_package(package))

    def _failure_detail(self, result: subprocess.CompletedProcess[str], tree: Path, package: Path | None) -> str:
        parts = [
            f"rc={result.returncode}",
            f"stdout={result.stdout}",
            f"stderr={result.stderr}",
        ]
        rels = slide_rels_in_tree(tree)
        if package is not None and package.exists() and package.is_file():
            rels = slide_rels_in_package(package)
        for name, data in rels:
            parts.append(f"{name}={data.decode('utf-8', 'replace')}")
        return "\n".join(parts)

    def test_directory_rejects_hyperlink_breakout_from_zip_member_name(self) -> None:
        self._assert_directory_rejected(
            HTTP_LAYOUT_NAME,
            {HTTP_LAYOUT_NAME: "<layout/>"},
        )

    def test_directory_rejects_ole_file_uri_breakout_from_zip_member_name(self) -> None:
        self._assert_directory_rejected(
            FILE_LAYOUT_NAME,
            {FILE_LAYOUT_NAME: "<layout/>"},
        )

    def test_directory_rejects_quote_only_name_returned_by_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._deck(root, ".pptx", {QUOTE_LAYOUT_NAME: "<layout/>"})
            unpacked = root / "unpacked"
            unpacked.mkdir()
            extract_skill_way(package, unpacked)
            listed = sorted(p.name for p in (unpacked / "ppt" / "slideLayouts").iterdir())
            self.assertIn(QUOTE_LAYOUT_NAME, listed)
            self.assertTrue(QUOTE_LAYOUT_NAME.startswith("slideLayout"))
            self.assertTrue(QUOTE_LAYOUT_NAME.endswith(".xml"))
            self.assertNotIn("/", QUOTE_LAYOUT_NAME)
        self._assert_directory_rejected(
            QUOTE_LAYOUT_NAME,
            {QUOTE_LAYOUT_NAME: "<layout/>"},
        )

    def test_directory_rejects_nested_layout_path(self) -> None:
        self._assert_directory_rejected(
            NESTED_LAYOUT_NAME,
            {NESTED_LAYOUT_NAME: "<layout/>"},
        )

    def test_directory_rejects_traversal_to_a_different_layout(self) -> None:
        # slideLayout8 exists, so the exists() check follows ".." onto
        # slideLayout1.xml. The requested string is a different path and
        # must not be written into the relationship.
        self._assert_directory_rejected(
            TRAVERSAL_LAYOUT_NAME,
            {"slideLayout8/keep.xml": "<layout/>"},
            must_be_zip_member=False,
        )

    def test_directory_rejects_collapsed_extracted_path(self) -> None:
        """Passing the on-disk relative path, whose empty '//' segments are gone, still injects today."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._deck(root, ".pptx", {HTTP_LAYOUT_NAME: "<layout/>"})
            unpacked = root / "unpacked"
            unpacked.mkdir()
            extract_skill_way(package, unpacked)
            layouts = unpacked / "ppt" / "slideLayouts"
            relative = [
                path.relative_to(layouts).as_posix()
                for path in layouts.rglob("*")
                if path.is_file() and not SAFE_LAYOUT_NAME.fullmatch(path.relative_to(layouts).as_posix())
            ]
            self.assertTrue(relative)
            for source in relative:
                self.assertNotEqual(source, HTTP_LAYOUT_NAME)
                self._assert_directory_rejected(
                    source,
                    {HTTP_LAYOUT_NAME: "<layout/>"},
                    must_be_zip_member=False,
                )

    def test_pptx_output_rejects_hyperlink_breakout(self) -> None:
        self._assert_package_rejected(HTTP_LAYOUT_NAME, ".pptx", {HTTP_LAYOUT_NAME: "<layout/>"}, output=True)

    def test_potx_output_rejects_ole_file_uri_breakout(self) -> None:
        self._assert_package_rejected(FILE_LAYOUT_NAME, ".potx", {FILE_LAYOUT_NAME: "<layout/>"}, output=True)

    def test_inplace_package_is_unchanged_for_hyperlink_breakout(self) -> None:
        self._assert_package_rejected(HTTP_LAYOUT_NAME, ".pptx", {HTTP_LAYOUT_NAME: "<layout/>"}, output=False)

    def test_safe_layout_still_creates_one_internal_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._deck(root, ".pptx", {})
            unpacked = root / "unpacked"
            unpacked.mkdir()
            extract_skill_way(package, unpacked)
            result = run_add_slide([str(unpacked), "slideLayout1.xml"])
            self.assertEqual(result.returncode, 0, result.stderr)
            rels = slide_rels_in_tree(unpacked)
            self.assertEqual(len(rels), 1)
            elements = relationship_elements(rels[0][1])
            self.assertEqual(len(elements), 1)
            self.assertEqual(
                elements[0].attrib.get("Type"),
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout",
            )
            self.assertEqual(elements[0].attrib.get("Target"), "../slideLayouts/slideLayout1.xml")
            self.assertNotIn("TargetMode", elements[0].attrib)

            out = root / "out.pptx"
            packaged = run_add_slide([str(package), "slideLayout1.xml", "-o", str(out)])
            self.assertEqual(packaged.returncode, 0, packaged.stderr)
            self.assertTrue(out.exists())
            packed_rels = slide_rels_in_package(out)
            self.assertEqual(len(packed_rels), 1)
            packed = relationship_elements(packed_rels[0][1])
            self.assertEqual(len(packed), 1)
            self.assertEqual(packed[0].attrib.get("Target"), "../slideLayouts/slideLayout1.xml")


if __name__ == "__main__":
    unittest.main()
