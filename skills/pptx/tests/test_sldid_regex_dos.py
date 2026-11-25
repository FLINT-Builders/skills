"""Spec for the slide-list regular-expression denial of service.

`clean.py` and `add_slide.py` locate `<p:sldId>` entries by scanning the raw
text of `ppt/presentation.xml`. The patterns `<p:sldId[^>]*r:id="..."` and
`<p:sldId[^>]*\\bid="..."` (and the `--after` searches built the same way)
walk every copy of the start tag. An XML comment full of `<p:sldId ` with no
closing `>` makes each attempt backtrack to the end of the comment, so the
work grows with the square of the comment size.

These tests build that deck and run the real cleanup and add-slide
operations. They fail while those scans are in place: each run is killed
after a few seconds. They pass once the slide list is taken from parsed
`p:sldId` elements (comments are not elements) and the same decks still
clean and gain a slide correctly.

Run from the repository root:

    python3 skills/pptx/tests/test_sldid_regex_dos.py

`defusedxml` is required, the same dependency the scripts import.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import defusedxml.minidom

import add_slide
import clean
from office.helpers import SLIDE_REL_TYPE

# 200_000 copies is ~1.8MB of comment. The vulnerable scans take on the
# order of minutes at this size; reading the same file with an XML parser
# takes a few milliseconds. Three seconds is far above a fixed run and far
# below a vulnerable one, including on a much faster machine.
REPEATS = 200_000
TIMEOUT_SECONDS = 3

OPEN_TAG = "<p:sldId "
SLIDE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    "<p:cSld><p:spTree/></p:cSld></p:sld>\n"
)
LAYOUT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>\n'
)


def presentation_xml(repeats: int, entries: list[tuple[int, str]]) -> str:
    """Well-formed presentation whose comment repeats an unclosed slide-id tag."""
    comment = "<!--" + (OPEN_TAG * repeats) + "-->"
    body = "\n".join(f'<p:sldId id="{sid}" r:id="{rid}"/>' for sid, rid in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">\n'
        f"{comment}\n"
        "<p:sldIdLst>\n"
        f"{body}\n"
        "</p:sldIdLst>\n"
        "</p:presentation>\n"
    )


def content_types(slide_names: list[str]) -> str:
    overrides = "\n".join(
        f'  <Override PartName="/ppt/slides/{name}" '
        f'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for name in slide_names
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>\n'
        f"{overrides}\n"
        "</Types>\n"
    )


def presentation_rels(slides: list[tuple[str, str]]) -> str:
    rels = "\n".join(
        f'  <Relationship Id="{rid}" Type="{SLIDE_REL_TYPE}" Target="slides/{name}"/>'
        for rid, name in slides
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f"{rels}\n"
        "</Relationships>\n"
    )


def write_deck(
    root: Path,
    *,
    repeats: int,
    entries: list[tuple[int, str]],
    rels: list[tuple[str, str]],
    slides: list[str],
    layout: bool = False,
) -> None:
    (root / "ppt" / "slides").mkdir(parents=True, exist_ok=True)
    (root / "ppt" / "_rels").mkdir(parents=True, exist_ok=True)
    (root / "[Content_Types].xml").write_text(content_types(slides), encoding="utf-8")
    (root / "ppt" / "_rels" / "presentation.xml.rels").write_text(
        presentation_rels(rels), encoding="utf-8"
    )
    (root / "ppt" / "presentation.xml").write_text(
        presentation_xml(repeats, entries), encoding="utf-8"
    )
    for name in slides:
        (root / "ppt" / "slides" / name).write_text(SLIDE_XML, encoding="utf-8")
    if layout:
        layouts = root / "ppt" / "slideLayouts"
        layouts.mkdir(parents=True, exist_ok=True)
        (layouts / "slideLayout1.xml").write_text(LAYOUT_XML, encoding="utf-8")


def zip_dir(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(src).as_posix())


def sld_ids(xml_bytes: bytes) -> list[tuple[str, str]]:
    dom = defusedxml.minidom.parseString(xml_bytes)
    return [
        (el.getAttribute("id"), el.getAttribute("r:id"))
        for el in dom.getElementsByTagName("p:sldId")
    ]


def slide_relationships(rels_xml: bytes) -> list[tuple[str, str]]:
    dom = defusedxml.minidom.parseString(rels_xml)
    found = []
    for rel in dom.getElementsByTagName("Relationship"):
        if rel.getAttribute("Type") != SLIDE_REL_TYPE:
            continue
        found.append((rel.getAttribute("Id"), rel.getAttribute("Target")))
    return found


def _case_clean_keep(root: Path) -> dict:
    removed = clean.clean_unused_files(root / "unpacked")
    return {"removed": removed}


def _case_clean_refuse(root: Path) -> dict:
    removed = clean.clean_unused_files(root / "unpacked")
    return {"removed": removed}


def _case_add_duplicate(root: Path) -> dict:
    dest = add_slide.add_slide(root / "unpacked", "slide1.xml")
    return {"dest": dest}


def _case_add_after(root: Path) -> dict:
    dest = add_slide.add_slide(root / "unpacked", "slide1.xml", after="slide1.xml")
    return {"dest": dest}


def _case_add_layout(root: Path) -> dict:
    dest = add_slide.add_slide(root / "unpacked", "slideLayout1.xml")
    return {"dest": dest}


def _case_add_package(root: Path) -> dict:
    dest = add_slide.add_slide_to_package(
        root / "in.pptx", "slide1.xml", output=root / "out.pptx"
    )
    return {"dest": dest}


CASES = {
    "clean-keep": _case_clean_keep,
    "clean-refuse": _case_clean_refuse,
    "add-duplicate": _case_add_duplicate,
    "add-after": _case_add_after,
    "add-layout": _case_add_layout,
    "add-package": _case_add_package,
}


def _child_main(case: str, root: Path) -> None:
    try:
        payload = CASES[case](root)
    except clean.RefusedToClean as exc:
        payload = {"refused": True, "message": str(exc)}
    except SystemExit as exc:
        payload = {"exit": exc.code}
    print("RESULT " + json.dumps(payload), flush=True)


class SlideListRegexDosTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_case(self, case: str) -> dict:
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    case,
                    str(self.tmp),
                ],
                timeout=TIMEOUT_SECONDS,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return {"_timeout": True}
        self.assertEqual(
            proc.returncode,
            0,
            f"{case} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}",
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT ") :])
        self.fail(f"{case} produced no RESULT line\n{proc.stdout}\n{proc.stderr}")
        return {}

    def assert_finished(self, case: str, result: dict) -> None:
        self.assertFalse(
            result.get("_timeout"),
            f"{case} did not finish within {TIMEOUT_SECONDS}s; "
            "the slide-list scan is still quadratic in the comment",
        )
        self.assertNotIn("exit", result, result)

    def assert_comment_payload(self, presentation: Path, real_entries: int) -> None:
        text = presentation.read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count(OPEN_TAG), REPEATS + real_entries)
        self.assertIn("<!--" + (OPEN_TAG * 3), text)
        # The comment is not a slide entry. Only the real elements count.
        self.assertEqual(len(sld_ids(text.encode())), real_entries)

    def test_clean_keeps_referenced_slide(self) -> None:
        unpacked = self.tmp / "unpacked"
        write_deck(
            unpacked,
            repeats=REPEATS,
            entries=[(256, "rId2")],
            rels=[("rId2", "slide1.xml")],
            slides=["slide1.xml"],
        )
        slide = unpacked / "ppt" / "slides" / "slide1.xml"
        before = slide.read_bytes()
        self.assert_comment_payload(unpacked / "ppt" / "presentation.xml", 1)

        result = self.run_case("clean-keep")

        self.assertTrue(slide.is_file(), "clean deleted the referenced slide")
        self.assertEqual(slide.read_bytes(), before)
        self.assert_finished("clean-keep", result)
        self.assertFalse(result.get("refused"), result)
        self.assertEqual(result.get("removed"), [])
        self.assertEqual(
            sld_ids((unpacked / "ppt" / "presentation.xml").read_bytes()),
            [("256", "rId2")],
        )

    def test_clean_refuses_to_wipe_unmatched_slides(self) -> None:
        # The listed rId is not the slide on disk, so remove_orphaned_slides
        # runs its second findall and must refuse instead of deleting.
        unpacked = self.tmp / "unpacked"
        write_deck(
            unpacked,
            repeats=REPEATS,
            entries=[(256, "rId9")],
            rels=[("rId2", "slide1.xml")],
            slides=["slide1.xml"],
        )
        slide = unpacked / "ppt" / "slides" / "slide1.xml"
        before = slide.read_bytes()
        self.assert_comment_payload(unpacked / "ppt" / "presentation.xml", 1)

        result = self.run_case("clean-refuse")

        self.assertTrue(slide.is_file(), "clean deleted a slide it should have kept")
        self.assertEqual(slide.read_bytes(), before)
        self.assert_finished("clean-refuse", result)
        self.assertTrue(result.get("refused"), result)
        self.assertEqual(
            sld_ids((unpacked / "ppt" / "presentation.xml").read_bytes()),
            [("256", "rId9")],
        )

    def test_add_duplicate_registers_next_id(self) -> None:
        unpacked = self.tmp / "unpacked"
        write_deck(
            unpacked,
            repeats=REPEATS,
            entries=[(256, "rId2")],
            rels=[("rId2", "slide1.xml")],
            slides=["slide1.xml"],
        )
        self.assert_comment_payload(unpacked / "ppt" / "presentation.xml", 1)

        result = self.run_case("add-duplicate")

        self.assert_finished("add-duplicate", result)
        self.assertEqual(result.get("dest"), "slide2.xml")
        self.assertTrue((unpacked / "ppt" / "slides" / "slide1.xml").is_file())
        self.assertTrue((unpacked / "ppt" / "slides" / "slide2.xml").is_file())
        ids = sld_ids((unpacked / "ppt" / "presentation.xml").read_bytes())
        rels = slide_relationships(
            (unpacked / "ppt" / "_rels" / "presentation.xml.rels").read_bytes()
        )
        self.assertEqual(ids[0], ("256", "rId2"))
        self.assertEqual(len(ids), 2)
        new_id, new_rid = ids[1]
        self.assertEqual(new_id, "257")
        targets = {rid: target for rid, target in rels}
        self.assertEqual(targets.get(new_rid), "slides/slide2.xml")

    def test_add_after_inserts_between_existing_slides(self) -> None:
        unpacked = self.tmp / "unpacked"
        write_deck(
            unpacked,
            repeats=REPEATS,
            entries=[(256, "rId2"), (257, "rId3")],
            rels=[("rId2", "slide1.xml"), ("rId3", "slide2.xml")],
            slides=["slide1.xml", "slide2.xml"],
        )
        self.assert_comment_payload(unpacked / "ppt" / "presentation.xml", 2)

        result = self.run_case("add-after")

        self.assert_finished("add-after", result)
        self.assertEqual(result.get("dest"), "slide3.xml")
        ids = sld_ids((unpacked / "ppt" / "presentation.xml").read_bytes())
        rels = slide_relationships(
            (unpacked / "ppt" / "_rels" / "presentation.xml.rels").read_bytes()
        )
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids[0][0], "256")
        self.assertEqual(ids[0][1], "rId2")
        self.assertEqual(ids[2], ("257", "rId3"))
        new_id, new_rid = ids[1]
        self.assertEqual(new_id, "258")
        targets = {rid: target for rid, target in rels}
        self.assertEqual(targets.get(new_rid), "slides/slide3.xml")

    def test_add_layout_registers_next_id(self) -> None:
        unpacked = self.tmp / "unpacked"
        write_deck(
            unpacked,
            repeats=REPEATS,
            entries=[(256, "rId2")],
            rels=[("rId2", "slide1.xml")],
            slides=["slide1.xml"],
            layout=True,
        )
        self.assert_comment_payload(unpacked / "ppt" / "presentation.xml", 1)

        result = self.run_case("add-layout")

        self.assert_finished("add-layout", result)
        self.assertEqual(result.get("dest"), "slide2.xml")
        ids = sld_ids((unpacked / "ppt" / "presentation.xml").read_bytes())
        self.assertEqual([item[0] for item in ids], ["256", "257"])
        rels_path = unpacked / "ppt" / "slides" / "_rels" / "slide2.xml.rels"
        self.assertIn("slideLayout1.xml", rels_path.read_text(encoding="utf-8"))

    def test_add_slide_package_registers_next_id(self) -> None:
        unpacked = self.tmp / "unpacked"
        write_deck(
            unpacked,
            repeats=REPEATS,
            entries=[(256, "rId2")],
            rels=[("rId2", "slide1.xml")],
            slides=["slide1.xml"],
        )
        zip_dir(unpacked, self.tmp / "in.pptx")
        self.assert_comment_payload(unpacked / "ppt" / "presentation.xml", 1)

        result = self.run_case("add-package")

        self.assert_finished("add-package", result)
        self.assertEqual(result.get("dest"), "slide2.xml")
        with zipfile.ZipFile(self.tmp / "out.pptx") as zf:
            ids = sld_ids(zf.read("ppt/presentation.xml"))
            rels = slide_relationships(zf.read("ppt/_rels/presentation.xml.rels"))
            self.assertIn("ppt/slides/slide2.xml", zf.namelist())
        self.assertEqual(ids[0], ("256", "rId2"))
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[1][0], "257")
        targets = {rid: target for rid, target in rels}
        self.assertEqual(targets.get(ids[1][1]), "slides/slide2.xml")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _child_main(sys.argv[2], Path(sys.argv[3]))
        raise SystemExit(0)
    unittest.main()
