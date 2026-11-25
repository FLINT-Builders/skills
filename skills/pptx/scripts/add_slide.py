"""Add a slide to a PPTX: duplicate an existing slide or instantiate a layout.

Does all of the package bookkeeping, so the deck stays valid:
  - writes the new ppt/slides/slideN.xml (and its .rels, minus any
    notesSlide reference, so the source's speaker notes aren't shared)
  - registers it in [Content_Types].xml
  - adds a slide relationship with a fresh rId to presentation.xml.rels
  - inserts <p:sldId id="..." r:id="..."/> with a fresh id into
    <p:sldIdLst> — at the end, or after --after SLIDE

Works on an unpacked directory (during an editing session) or directly on a
.pptx/.potx file (extracted to a temp dir, then rezipped atomically; the
temp dir is discarded, so unpack the output if you still need to edit the
new slide's content).

Usage:
    python add_slide.py unpacked/ slide2.xml                 # duplicate slide2
    python add_slide.py unpacked/ slideLayout3.xml           # new slide from a layout
    python add_slide.py unpacked/ slide2.xml --after slide2.xml
    python add_slide.py deck.pptx slide2.xml                 # rewrite deck.pptx in place
    python add_slide.py deck.pptx slide2.xml -o out.pptx

A duplicated slide still holds the source's content: edit ppt/slides/slideN.xml
(printed on success) to change it. To list layouts: ls <dir>/ppt/slideLayouts/
"""

import argparse
import re
import sys
from typing import NoReturn
import tempfile
import zipfile
from pathlib import Path

from office.helpers import PackageDir, rezip, safe_extract

MINIMAL_SLIDE_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr>
        <p:cNvPr id="1" name=""/>
        <p:cNvGrpSpPr/>
        <p:nvPr/>
      </p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm>
          <a:off x="0" y="0"/>
          <a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/>
          <a:chExt cx="0" cy="0"/>
        </a:xfrm>
      </p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr>
    <a:masterClrMapping/>
  </p:clrMapOvr>
</p:sld>'''

SHARED_PART_TYPES = ("chart", "diagramData", "oleObject", "package")

NOTES_SLIDE_TYPE_RE = re.compile(r"""Type=["'][^"']*/relationships/notesSlide["']""")
RELATIONSHIP_RE = re.compile(r"<Relationship\b[^>]*?(?:/>|>.*?</Relationship\s*>)", re.DOTALL)

SLIDE_ID_MIN = 256
SLIDE_ID_MAX = 2147483647

SLIDE_NAME_RE = re.compile(r"slide(\d+)\.xml")
LAYOUT_NAME_RE = re.compile(r"slideLayout\d+\.xml")

PRESENTATION = "ppt/presentation.xml"
PRESENTATION_RELS = "ppt/_rels/presentation.xml.rels"
CONTENT_TYPES = "[Content_Types].xml"
SLIDES = "ppt/slides"
SLIDE_RELS = "ppt/slides/_rels"


def _die(msg: str) -> NoReturn:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def get_next_slide_number(names: list[str]) -> int:
    existing = [int(m.group(1)) for name in names if (m := SLIDE_NAME_RE.fullmatch(name))]
    return max(existing) + 1 if existing else 1


def parse_source(source: str) -> tuple[str, str]:
    if LAYOUT_NAME_RE.fullmatch(source):
        return ("layout", source)
    if SLIDE_NAME_RE.fullmatch(source):
        return ("slide", source)
    _die(
        f"{source!r} is not a slide or layout file name — pass slideN.xml from ppt/slides/ "
        f"or slideLayoutN.xml from ppt/slideLayouts/"
    )


def _layout_rels(layout_file: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/{layout_file}"/>
</Relationships>'''


def _add_slide(pkg: PackageDir, kind: str, source: str, after: str | None) -> str:
    # Everything is read and checked before the first write, so a refused
    # deck is left exactly as it was.
    pres_xml = _read_presentation(pkg)
    if not pkg.is_file(CONTENT_TYPES):
        _die(f"{pkg.root / CONTENT_TYPES} not found — is this an unpacked PPTX?")
    content_types = pkg.read_text(CONTENT_TYPES)
    if not pkg.is_file(PRESENTATION_RELS):
        _die(f"{pkg.root / PRESENTATION_RELS} not found — is this an unpacked PPTX?")
    pres_rels = pkg.read_text(PRESENTATION_RELS)

    slides_exist = pkg.has_dir(SLIDES)
    pkg.has_dir(SLIDE_RELS)
    dest = f"slide{get_next_slide_number(pkg.listdir(SLIDES))}.xml"
    dest_rels = f"{SLIDE_RELS}/{dest}.rels"
    after_rid = _precheck_registration(pres_xml, content_types, pres_rels, after, dest)
    if pkg.lstat(f"{SLIDES}/{dest}") is not None:
        _die(f"{pkg.root / SLIDES / dest} already exists")
    pkg.is_file(dest_rels)

    shared_parts: list[str] = []
    if kind == "layout":
        if not pkg.is_file(f"ppt/slideLayouts/{source}"):
            _die(f"{pkg.root / 'ppt' / 'slideLayouts' / source} not found")
        slide_bytes = MINIMAL_SLIDE_XML.encode("utf-8")
        rels_content: str | None = _layout_rels(source)
    else:
        if not slides_exist or not pkg.is_file(f"{SLIDES}/{source}"):
            _die(f"{pkg.root / SLIDES / source} not found")
        slide_bytes = pkg.read_bytes(f"{SLIDES}/{source}")
        rels_content = None
        source_rels = f"{SLIDE_RELS}/{source}.rels"
        if pkg.is_file(source_rels):
            rels_content = RELATIONSHIP_RE.sub(
                lambda m: "" if NOTES_SLIDE_TYPE_RE.search(m.group(0)) else m.group(0),
                pkg.read_text(source_rels),
            )
            shared_parts = sorted({
                t for t in re.findall(r'Type="[^"]*/relationships/(\w+)"', rels_content)
                if t in SHARED_PART_TYPES
            })

    new_content_types = _add_to_content_types(content_types, dest)
    rid, new_pres_rels = _add_to_presentation_rels(pres_rels, pres_xml, dest)
    slide_id = _get_next_slide_id(pres_xml)
    new_pres_xml, pos, total = _insert_into_sld_id_lst(pres_xml, slide_id, rid, after_rid)

    pkg.makedirs(SLIDES)
    pkg.write_bytes(f"{SLIDES}/{dest}", slide_bytes, exclusive=True)
    if rels_content is not None:
        pkg.makedirs(SLIDE_RELS)
        pkg.write_text(dest_rels, rels_content)
    if new_content_types != content_types:
        pkg.write_text(CONTENT_TYPES, new_content_types)
    if new_pres_rels != pres_rels:
        pkg.write_text(PRESENTATION_RELS, new_pres_rels)
    pkg.write_text(PRESENTATION, new_pres_xml)

    print(f"Created ppt/slides/{dest} from {source}")
    print(
        f'Inserted <p:sldId id="{slide_id}" r:id="{rid}"/> into <p:sldIdLst> '
        f"at position {pos} of {total}"
    )
    if shared_parts:
        print(
            f"Note: {dest} shares its {', '.join(shared_parts)} part(s) with {source} "
            f"(they are referenced, not copied) — editing those parts changes both slides"
        )
    return dest


def _read_presentation(pkg: PackageDir) -> str:
    if not pkg.is_file(PRESENTATION):
        _die(f"{pkg.root / PRESENTATION} not found — is this an unpacked PPTX?")
    return pkg.read_text(PRESENTATION)


def _precheck_registration(
    xml: str, content_types: str, pres_rels: str, after: str | None, dest: str
) -> str | None:
    has_slot = (
        "</p:sldIdLst>" in xml
        or re.search(r"<p:sldIdLst\s*/>", xml)
        or "</p:sldMasterIdLst>" in xml
    )
    if not has_slot:
        _die("presentation.xml has no <p:sldIdLst> (or <p:sldMasterIdLst> to anchor a new one)")

    stale = []
    if f'PartName="/ppt/slides/{dest}"' in content_types:
        stale.append("[Content_Types].xml")
    if _find_slide_relationship(pres_rels, dest):
        stale.append("presentation.xml.rels")
    if stale:
        _die(
            f"{dest} is still registered in {' and '.join(stale)} but absent from ppt/slides/ — "
            f"run clean.py first"
        )

    if not after:
        return None
    after_rid = _rid_for_slide(pres_rels, after)
    if not re.search(rf'<p:sldId\b[^>]*r:id="{re.escape(after_rid)}"[^>]*>', xml):
        _die(f"{after} ({after_rid}) is not listed in <p:sldIdLst>")
    return after_rid


def _add_to_content_types(content_types: str, dest: str) -> str:
    new_override = f'<Override PartName="/ppt/slides/{dest}" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'

    if f'PartName="/ppt/slides/{dest}"' not in content_types:
        content_types = content_types.replace("</Types>", f"  {new_override}\n</Types>")
    return content_types


def _add_to_presentation_rels(pres_rels: str, pres_xml: str, dest: str) -> tuple[str, str]:
    existing = _find_slide_relationship(pres_rels, dest)
    if existing:
        return existing, pres_rels

    used = {int(n) for n in re.findall(r'\bId="rId(\d+)"', pres_rels)}
    used |= {int(n) for n in re.findall(r'\br:id="rId(\d+)"', pres_xml)}
    rid = f"rId{max(used) + 1 if used else 1}"

    new_rel = f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/{dest}"/>'
    pres_rels = pres_rels.replace("</Relationships>", f"  {new_rel}\n</Relationships>")

    return rid, pres_rels


def _find_slide_relationship(pres_rels: str, slide_name: str) -> str | None:
    for m in re.finditer(r"<Relationship\b[^>]*>", pres_rels):
        element = m.group(0)
        if re.search(rf'Target="(?:/ppt/)?slides/{re.escape(slide_name)}"', element):
            id_match = re.search(r'\bId="([^"]+)"', element)
            if id_match:
                return id_match.group(1)
    return None


def _get_next_slide_id(pres_content: str) -> int:
    used = {int(m) for m in re.findall(r'<p:sldId[^>]*\bid="(\d+)"', pres_content)}

    candidate = max((i for i in used if i >= SLIDE_ID_MIN), default=SLIDE_ID_MIN - 1) + 1
    if candidate <= SLIDE_ID_MAX and candidate not in used:
        return candidate
    for i in range(SLIDE_ID_MIN, SLIDE_ID_MAX + 1):
        if i not in used:
            return i
    _die("no slide id available in [256, 2147483647] — the deck is full")


def _insert_into_sld_id_lst(
    xml: str, slide_id: int, rid: str, after_rid: str | None = None
) -> tuple[str, int, int]:
    entry = f'<p:sldId id="{slide_id}" r:id="{rid}"/>'

    if f'r:id="{rid}"' in xml:
        _die(f"presentation.xml already references {rid}; refusing to add a duplicate")

    if after_rid:
        open_tag = re.search(rf'<p:sldId\b[^>]*r:id="{re.escape(after_rid)}"[^>]*>', xml)
        if not open_tag:
            _die(f"{after_rid} is not listed in <p:sldIdLst>")
        end = open_tag.end()
        if not open_tag.group(0).endswith("/>"):
            close = xml.find("</p:sldId>", end)
            if close == -1:
                _die(f"unclosed <p:sldId> for {after_rid} in presentation.xml")
            end = close + len("</p:sldId>")
        xml = xml[:end] + entry + xml[end:]
    elif "</p:sldIdLst>" in xml:
        xml = xml.replace("</p:sldIdLst>", f"{entry}</p:sldIdLst>", 1)
    elif re.search(r"<p:sldIdLst\s*/>", xml):
        xml = re.sub(r"<p:sldIdLst\s*/>", f"<p:sldIdLst>{entry}</p:sldIdLst>", xml, count=1)
    elif "</p:sldMasterIdLst>" in xml:
        xml = xml.replace(
            "</p:sldMasterIdLst>", f"</p:sldMasterIdLst><p:sldIdLst>{entry}</p:sldIdLst>", 1
        )
    else:
        _die("presentation.xml has no <p:sldIdLst> (or <p:sldMasterIdLst> to anchor a new one)")

    lst = re.search(r"<p:sldIdLst>(.*)</p:sldIdLst>", xml, re.DOTALL)
    entries = re.findall(r"<p:sldId\b[^>]*>", lst.group(1)) if lst else []
    position = next(
        (i for i, e in enumerate(entries, 1) if f'r:id="{rid}"' in e), len(entries)
    )
    return xml, position, len(entries)


def _rid_for_slide(pres_rels: str, slide_name: str) -> str:
    rid = _find_slide_relationship(pres_rels, slide_name)
    if not rid:
        _die(f"{slide_name} has no relationship in presentation.xml.rels")
    return rid


def add_slide(unpacked_dir: Path, source: str, after: str | None = None) -> str:
    kind, name = parse_source(source)
    try:
        with PackageDir(unpacked_dir) as pkg:
            return _add_slide(pkg, kind, name, after)
    except (OSError, ValueError) as e:
        _die(str(e))


def add_slide_to_package(
    package: Path, source: str, after: str | None = None, output: Path | None = None
) -> str:
    out = output or package
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(package) as zf:
            safe_extract(zf, tmp_path)
        dest = add_slide(tmp_path, source, after)
        rezip(tmp_path, out)
    print(f"Wrote {out} — the new slide is ppt/slides/{dest} inside it (unpack to edit its content)")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a slide to a PPTX: duplicate a slide or instantiate a layout. "
        "Registers content types, relationships, and <p:sldIdLst>."
    )
    parser.add_argument("target", help="Unpacked PPTX directory OR a .pptx/.potx file")
    parser.add_argument(
        "source",
        help="slideN.xml to duplicate, or slideLayoutN.xml to create from a layout "
        "(list layouts with: ls <dir>/ppt/slideLayouts/)",
    )
    parser.add_argument(
        "--after",
        metavar="SLIDE",
        help="insert after this slide, e.g. slide2.xml (default: append at the end)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="output file (only with a .pptx/.potx target; default: rewrite the input in place)",
    )
    args = parser.parse_args()

    target = Path(args.target)
    if target.is_dir():
        if args.output:
            parser.error("--output is only valid for .pptx/.potx input; a directory is modified in place")
        add_slide(target, args.source, args.after)
    elif target.is_file() and target.suffix.lower() in (".pptx", ".potx"):
        try:
            add_slide_to_package(target, args.source, args.after, Path(args.output) if args.output else None)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            _die(str(e))
    else:
        _die(f"{target} is neither a directory nor a .pptx/.potx file")


if __name__ == "__main__":
    main()
