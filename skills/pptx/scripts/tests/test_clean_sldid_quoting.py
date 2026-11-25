"""Which slides `clean.py` may remove from an unpacked presentation.

The editing workflow unpacks a deck and runs `scripts/clean.py` on that folder
(see SKILL.md). `clean_unused_files` may delete a slide file only when that
slide's relationship id is absent from `p:sldIdLst` in `ppt/presentation.xml`.
Quoting and whitespace around `=` are legal in that attribute. Parts referenced
only by a slide that is still listed — its media, chart, and notes — stay.
Parts referenced only by a slide that is not listed are removed.

Run from the repository root:

    python3 skills/pptx/scripts/tests/test_clean_sldid_quoting.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import clean  # noqa: E402
import defusedxml.minidom  # noqa: E402

NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
CHART_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
NOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"


def _rid_attr(rid: str, style: str) -> str:
    """Spell `r:id` the way a real XML serializer is allowed to."""
    if style == "double":
        return f'r:id="{rid}"'
    if style == "single":
        return f"r:id='{rid}'"
    if style == "spaces":
        return f'r:id = "{rid}"'
    if style == "single_spaces":
        return f"r:id = '{rid}'"
    if style == "tabs":
        return 'r:id\t=\t"%s"' % rid
    if style == "newline_single":
        return f"\n         r:id='{rid}'"
    raise ValueError(style)


class Slide:
    def __init__(self, name: str, rid: str, listed: bool, style: str):
        self.name = name
        self.rid = rid
        self.listed = listed
        self.style = style

    @property
    def stem(self) -> str:
        return Path(self.name).stem


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_unpacked(root: Path, slides: list[Slide]) -> None:
    """Minimal unpacked PPTX: presentation list, rels, and per-slide parts."""
    entries = []
    for slide in slides:
        if not slide.listed:
            continue
        entries.append(
            f'<p:sldId id="{256 + len(entries)}" {_rid_attr(slide.rid, slide.style)}/>'
        )
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:presentation xmlns:p="{NS_P}" xmlns:r="{NS_R}">\n'
        "  <p:sldIdLst>\n"
        + "".join(f"    {entry}\n" for entry in entries)
        + "  </p:sldIdLst>\n"
        "</p:presentation>\n"
    )
    _write(root / "ppt" / "presentation.xml", presentation)

    rels = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Relationships xmlns="{REL_NS}">',
    ]
    for slide in slides:
        rels.append(
            f'  <Relationship Id="{slide.rid}" Type="{SLIDE_REL}" '
            f'Target="slides/{slide.name}"/>'
        )
    rels.append("</Relationships>\n")
    _write(root / "ppt" / "_rels" / "presentation.xml.rels", "\n".join(rels))

    for slide in slides:
        marker = f"content-of-{slide.name}"
        _write(
            root / "ppt" / "slides" / slide.name,
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:sld xmlns:p="{NS_P}"><p:cSld><p:spTree>'
            f"<p:nvGrpSpPr/>{marker}</p:spTree></p:cSld></p:sld>\n",
        )
        media = f"../media/{slide.stem}.png"
        chart = f"../charts/{slide.stem}.xml"
        notes = f"../notesSlides/{slide.stem}.xml"
        _write(
            root / "ppt" / "slides" / "_rels" / f"{slide.name}.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Relationships xmlns="{REL_NS}">\n'
            f'  <Relationship Id="rId1" Type="{IMAGE_REL}" Target="{media}"/>\n'
            f'  <Relationship Id="rId2" Type="{CHART_REL}" Target="{chart}"/>\n'
            f'  <Relationship Id="rId3" Type="{NOTES_REL}" Target="{notes}"/>\n'
            "</Relationships>\n",
        )
        media_path = root / "ppt" / "media" / f"{slide.stem}.png"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(f"png-bytes-{slide.stem}".encode("ascii"))
        _write(
            root / "ppt" / "charts" / f"{slide.stem}.xml",
            f"<c:chart>{slide.stem}-chart</c:chart>\n",
        )
        _write(
            root / "ppt" / "notesSlides" / f"{slide.stem}.xml",
            f"<p:notes>{slide.stem}-notes</p:notes>\n",
        )


def listed_rids(presentation: Path) -> set[str]:
    """Relationship ids the XML parser already used for relationships sees."""
    dom = defusedxml.minidom.parse(str(presentation))
    return {
        element.getAttribute("r:id")
        for element in dom.getElementsByTagName("p:sldId")
        if element.getAttribute("r:id")
    }


def run_clean(unpacked: Path) -> None:
    """Run the product entry point. A refusal is not allowed to be the only trace."""
    try:
        clean.clean_unused_files(unpacked)
    except clean.RefusedToClean:
        return


def assert_outcome(test: unittest.TestCase, unpacked: Path, slides: list[Slide]) -> None:
    presentation = unpacked / "ppt" / "presentation.xml"
    expected_ids = {slide.rid for slide in slides if slide.listed}
    test.assertEqual(listed_rids(presentation), expected_ids)

    pres_rels = (unpacked / "ppt" / "_rels" / "presentation.xml.rels").read_text(
        encoding="utf-8"
    )
    missing: list[str] = []
    unexpected: list[str] = []
    for slide in slides:
        parts = {
            f"ppt/slides/{slide.name}": unpacked / "ppt" / "slides" / slide.name,
            f"ppt/slides/_rels/{slide.name}.rels": unpacked
            / "ppt"
            / "slides"
            / "_rels"
            / f"{slide.name}.rels",
            f"ppt/media/{slide.stem}.png": unpacked / "ppt" / "media" / f"{slide.stem}.png",
            f"ppt/charts/{slide.stem}.xml": unpacked / "ppt" / "charts" / f"{slide.stem}.xml",
            f"ppt/notesSlides/{slide.stem}.xml": unpacked
            / "ppt"
            / "notesSlides"
            / f"{slide.stem}.xml",
        }
        target = f"slides/{slide.name}"
        if slide.listed:
            for label, path in parts.items():
                if not path.is_file():
                    missing.append(label)
            slide_path = parts[f"ppt/slides/{slide.name}"]
            if slide_path.is_file() and f"content-of-{slide.name}" not in slide_path.read_text(
                encoding="utf-8"
            ):
                missing.append(f"{slide.name} content replaced")
            media_path = parts[f"ppt/media/{slide.stem}.png"]
            if media_path.is_file() and media_path.read_bytes() != f"png-bytes-{slide.stem}".encode(
                "ascii"
            ):
                missing.append(f"{slide.stem}.png content replaced")
            if target not in pres_rels:
                missing.append(f"presentation.xml.rels missing {target}")
        else:
            for label, path in parts.items():
                if path.exists():
                    unexpected.append(label)
            if target in pres_rels:
                unexpected.append(f"presentation.xml.rels still has {target}")
    test.assertEqual(
        missing,
        [],
        "listed slides or the media, charts, and notes only they reference were deleted",
    )
    test.assertEqual(
        unexpected,
        [],
        "unlisted slides or the media, charts, and notes only they reference were kept",
    )


def two_listed_and_orphan(style_a: str, style_b: str) -> list[Slide]:
    return [
        Slide("slide1.xml", "rId2", True, style_a),
        Slide("slide2.xml", "rId3", False, "double"),
        Slide("slide3.xml", "rId4", True, style_b),
    ]


class CleanSlideIdQuotingTest(unittest.TestCase):
    def _clean_and_check(self, slides: list[Slide]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            unpacked = Path(tmp)
            build_unpacked(unpacked, slides)
            run_clean(unpacked)
            assert_outcome(self, unpacked, slides)

    def test_double_quoted_rids_keep_listed_slides_and_drop_unlisted(self) -> None:
        self._clean_and_check(two_listed_and_orphan("double", "double"))

    def test_single_quoted_rids_keep_listed_slides_media_charts_notes(self) -> None:
        # Attack path: r:id='rId2' is legal XML and still names the relationship.
        self._clean_and_check(two_listed_and_orphan("single", "single"))

    def test_spaces_around_equals_keep_listed_slides_and_their_parts(self) -> None:
        self._clean_and_check(two_listed_and_orphan("spaces", "spaces"))

    def test_single_quotes_and_spaces_around_equals_keep_listed_slides(self) -> None:
        self._clean_and_check(two_listed_and_orphan("single_spaces", "single_spaces"))

    def test_tabs_around_equals_keep_listed_slides_and_their_parts(self) -> None:
        self._clean_and_check(two_listed_and_orphan("tabs", "tabs"))

    def test_single_quoted_rid_on_its_own_line_keeps_the_slide(self) -> None:
        self._clean_and_check(two_listed_and_orphan("newline_single", "newline_single"))

    def test_mixed_quotes_do_not_drop_the_single_quoted_slide(self) -> None:
        # One double-quoted id is enough for the wipe guard to stand down.
        # The single-quoted sibling is still listed and must be kept, while the
        # unlisted slide and the parts only it references must go.
        self._clean_and_check(two_listed_and_orphan("double", "single"))

    def test_cli_on_unpacked_single_quoted_deck_keeps_listed_parts(self) -> None:
        slides = two_listed_and_orphan("single", "single")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source"
            build_unpacked(source, slides)
            deck = tmp_path / "deck.pptx"
            with zipfile.ZipFile(deck, "w") as archive:
                for path in source.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(source).as_posix())
            unpacked = tmp_path / "unpacked"
            with zipfile.ZipFile(deck) as archive:
                archive.extractall(unpacked)
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "clean.py"), str(unpacked)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            assert_outcome(self, unpacked, slides)


if __name__ == "__main__":
    unittest.main()
