"""Preview rasterization must refuse oversized documents before it writes.

The docx skill's "Verify the output" block and the pptx skill's "Converting
to Images" block tell the agent to convert the whole file with LibreOffice
and then run pdftoppm on every page. pptx ``scripts/thumbnail.py`` does the
same conversion in code. None of those paths caps page count or page size.

These tests are the required behavior. They fail while that unbounded
rasterization is still what the skill runs, and they pass once an over-limit
document is rejected with a non-zero status before any PDF or image is
created. In-limit letter documents and small decks must still preview.

Envelope the cases enforce (a fix may use any limit inside it):

- 2 pages / 2 slides still preview; 31 and 45 do not.
- A US Letter page still previews. A page whose long edge is 72in, 72.0in,
  or 72cm does not, including when that page is a middle section whose
  following section is Letter again.
- Page size is attacker-controlled OOXML. Integer twips and
  ST_PositiveUniversalMeasure strings (``72in``, ``72.0in``, ``72cm``) both
  reach LibreOffice, so a check of only one spelling does not satisfy this.

Run from the repository root:

    python3 -m pytest tests/test_office_preview_raster_limits.py -q

Requires LibreOffice Writer and Impress, poppler-utils (pdftoppm, pdfinfo),
Pillow, and defusedxml. Tests are serial: they each start soffice.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCX_SKILL = REPO / "skills" / "docx"
PPTX_SKILL = REPO / "skills" / "pptx"
THUMBNAIL = PPTX_SKILL / "scripts" / "thumbnail.py"

# US Letter, in twips (1440 twips = 1 inch). Matches the docx skill.
LETTER_W = "12240"
LETTER_H = "15840"
# 10in x 5.625in, the pptx skill's default 16:9 canvas, in EMUs.
SLIDE_CX = 10 * 914400
SLIDE_CY = int(5.625 * 914400)

RASTER_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".ppm", ".pbm", ".pgm", ".tif", ".tiff"}
# Docx verification uses 100 DPI, so a Letter preview is 850 x 1100.
DOCX_MAX_PX = 1100
# Pptx verification uses 150 DPI, so a 10in slide edge is 1500px.
PPTX_MAX_PX = 1500


def _twips(inches: float) -> str:
    return str(int(round(inches * 1440)))


def _emu(inches: float) -> int:
    return int(round(inches * 914400))


def _write_docx(path: Path, document_xml: str) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)


def _sect(width: str, height: str) -> str:
    return (
        f'<w:sectPr><w:pgSz w:w="{width}" w:h="{height}"/>'
        '<w:pgMar w:top="360" w:right="360" w:bottom="360" w:left="360" '
        'w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
    )


def _docx_pages(pages: int, width: str = LETTER_W, height: str = LETTER_H) -> str:
    parts: list[str] = []
    for index in range(pages):
        if index:
            parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        parts.append(f"<w:p><w:r><w:t>Page {index + 1}</w:t></w:r></w:p>")
    parts.append(_sect(width, height))
    body = "".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )


def _docx_middle_oversize() -> str:
    """Letter, then a 72in section, then Letter again in the body sectPr.

    The oversized page is not the last section, and its size is written as
    ``72in`` rather than twips.
    """
    body = (
        "<w:p><w:r><w:t>Letter section</w:t></w:r></w:p>"
        f"<w:p><w:pPr>{_sect(LETTER_W, LETTER_H)}</w:pPr></w:p>"
        "<w:p><w:r><w:t>Huge section</w:t></w:r></w:p>"
        f'<w:p><w:pPr>{_sect("72in", "72in")}</w:pPr></w:p>'
        "<w:p><w:r><w:t>Letter again</w:t></w:r></w:p>"
        f"{_sect(LETTER_W, LETTER_H)}"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )


def _write_pptx(path: Path, slides: int, cx: int, cy: int) -> None:
    ns_p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    ns_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    slide_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    sld_ids: list[str] = []
    rels: list[str] = []
    overrides: list[str] = []
    parts: dict[str, str] = {}
    for index in range(slides):
        number = index + 1
        rid = f"rId{number}"
        sld_ids.append(f'<p:sldId id="{256 + index}" r:id="{rid}"/>')
        rels.append(
            f'<Relationship Id="{rid}" Type="{slide_rel}" Target="slides/slide{number}.xml"/>'
        )
        overrides.append(
            f'<Override PartName="/ppt/slides/slide{number}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
        parts[f"ppt/slides/slide{number}.xml"] = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{ns_a}" xmlns:r="{ns_r}" xmlns:p="{ns_p}">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr/>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr/>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Slide {number}</a:t></a:r></a:p></p:txBody>
    </p:sp>
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>'''
    presentation = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{ns_a}" xmlns:r="{ns_r}" xmlns:p="{ns_p}">
  <p:sldIdLst>{"".join(sld_ids)}</p:sldIdLst>
  <p:sldSz cx="{cx}" cy="{cy}"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>'''
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{ns_ct}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  {"".join(overrides)}
</Types>'''
    root_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{ns_rel}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>'''
    pres_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{ns_rel}">{"".join(rels)}</Relationships>'''
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        for name, data in parts.items():
            archive.writestr(name, data)


def _bash_after(skill_md: Path, heading: str) -> str:
    text = skill_md.read_text(encoding="utf-8")
    start = text.find(heading)
    if start < 0:
        pytest.fail(f"{skill_md} has no {heading!r} section for the preview procedure")
    match = re.search(r"```bash\n(.*?)```", text[start:], re.DOTALL)
    if not match:
        pytest.fail(f"{skill_md} has no bash procedure under {heading}")
    return match.group(1)


def _preview_script(skill_dir: Path, heading: str) -> str:
    block = _bash_after(skill_dir / "SKILL.md", heading)
    # The skill documents paths relative to the skill directory. Run the
    # procedure in a throwaway cwd, so point those paths at the skill tree.
    return re.sub(r"(?<![\w./-])scripts/", f"{skill_dir}/scripts/", block)


def _python_env(work: Path) -> dict[str, str]:
    bindir = work / "bin"
    bindir.mkdir(exist_ok=True)
    link = bindir / "python"
    if not link.exists():
        link.symlink_to(sys.executable)
    env = os.environ.copy()
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["TMPDIR"] = str(work / "tmp")
    (work / "tmp").mkdir(exist_ok=True)
    return env


def _jpeg_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    index = 2
    while index + 8 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if marker in (0xD8, 0xD9):
            index += 2
            continue
        segment = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + segment
    pytest.fail(f"{path} is not a readable JPEG")


class _RasterWatch:
    """Record PDF and image files created under a directory while a command runs.

    A refusal that writes a preview and then deletes it is still a failure.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.seen: set[Path] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def __enter__(self) -> "_RasterWatch":
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._scan()

    def _scan(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix.lower() in RASTER_SUFFIXES:
                self.seen.add(path)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._scan()
            self._stop.wait(0.02)


def _run(work: Path, script: str, args: list[str] | None = None) -> tuple[subprocess.CompletedProcess[str], set[Path]]:
    env = _python_env(work)
    (work / "sentinel.txt").write_text("keep", encoding="utf-8")
    with _RasterWatch(work) as watch:
        proc = subprocess.run(
            ["bash", "-c", script, "bash", *(args or [])],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    return proc, set(watch.seen)


def _describe(paths: set[Path]) -> str:
    lines = []
    total = 0
    for path in sorted(paths, key=str):
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        total += max(size, 0)
        lines.append(f"{path} ({size} bytes)")
    lines.append(f"total {total} bytes across {len(paths)} file(s)")
    return "\n".join(lines)


def _assert_refused(proc: subprocess.CompletedProcess[str], rasters: set[Path], work: Path) -> None:
    assert proc.returncode != 0 and not rasters, (
        "oversized preview must exit non-zero and must not create a PDF or image, "
        "including one that is deleted before the command returns\n"
        f"exit={proc.returncode}\n{_describe(rasters)}\n"
        f"stdout:\n{proc.stdout[-800:]}\nstderr:\n{proc.stderr[-800:]}"
    )
    assert (work / "sentinel.txt").is_file(), "preview deleted an existing file before refusing"


def _assert_jpegs(rasters: set[Path], count: int, max_px: int) -> None:
    images = sorted(path for path in rasters if path.suffix.lower() in {".jpg", ".jpeg"})
    assert len(images) == count, _describe(rasters)
    for image in images:
        width, height = _jpeg_size(image)
        assert width <= max_px and height <= max_px, f"{image} is {width}x{height}"


def test_docx_letter_preview_renders_every_page_within_bounds(tmp_path: Path) -> None:
    _write_docx(tmp_path / "output.docx", _docx_pages(2))
    proc, rasters = _run(tmp_path, _preview_script(DOCX_SKILL, "## Verify the output"))
    assert proc.returncode == 0, proc.stderr[-800:]
    _assert_jpegs(rasters, 2, DOCX_MAX_PX)


@pytest.mark.parametrize("pages", [31, 45])
def test_docx_page_count_over_preview_limit_writes_nothing(tmp_path: Path, pages: int) -> None:
    _write_docx(tmp_path / "output.docx", _docx_pages(pages))
    proc, rasters = _run(tmp_path, _preview_script(DOCX_SKILL, "## Verify the output"))
    _assert_refused(proc, rasters, tmp_path)


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (_twips(72), _twips(72)),
        ("72in", "72in"),
        ("72.0in", LETTER_H),
        ("72cm", "72cm"),
    ],
)
def test_docx_oversize_page_writes_nothing(tmp_path: Path, width: str, height: str) -> None:
    _write_docx(tmp_path / "output.docx", _docx_pages(1, width, height))
    proc, rasters = _run(tmp_path, _preview_script(DOCX_SKILL, "## Verify the output"))
    _assert_refused(proc, rasters, tmp_path)


def test_docx_oversize_middle_section_writes_nothing(tmp_path: Path) -> None:
    _write_docx(tmp_path / "output.docx", _docx_middle_oversize())
    proc, rasters = _run(tmp_path, _preview_script(DOCX_SKILL, "## Verify the output"))
    _assert_refused(proc, rasters, tmp_path)


def test_pptx_small_deck_renders_every_slide_within_bounds(tmp_path: Path) -> None:
    _write_pptx(tmp_path / "output.pptx", 2, SLIDE_CX, SLIDE_CY)
    proc, rasters = _run(tmp_path, _preview_script(PPTX_SKILL, "## Converting to Images"))
    assert proc.returncode == 0, proc.stderr[-800:]
    _assert_jpegs(rasters, 2, PPTX_MAX_PX)


@pytest.mark.parametrize("slides", [31, 45])
def test_pptx_slide_count_over_preview_limit_writes_nothing(tmp_path: Path, slides: int) -> None:
    _write_pptx(tmp_path / "output.pptx", slides, SLIDE_CX, SLIDE_CY)
    proc, rasters = _run(tmp_path, _preview_script(PPTX_SKILL, "## Converting to Images"))
    _assert_refused(proc, rasters, tmp_path)


@pytest.mark.parametrize("inches", [36, 72])
def test_pptx_oversize_slide_writes_nothing(tmp_path: Path, inches: int) -> None:
    _write_pptx(tmp_path / "output.pptx", 1, _emu(inches), _emu(inches))
    proc, rasters = _run(tmp_path, _preview_script(PPTX_SKILL, "## Converting to Images"))
    _assert_refused(proc, rasters, tmp_path)


def _thumbnail(work: Path, deck: Path) -> tuple[subprocess.CompletedProcess[str], set[Path]]:
    env = _python_env(work)
    (work / "sentinel.txt").write_text("keep", encoding="utf-8")
    with _RasterWatch(work) as watch:
        proc = subprocess.run(
            [sys.executable, str(THUMBNAIL), str(deck), str(work / "grid")],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    return proc, set(watch.seen)


def test_thumbnail_small_deck_still_renders(tmp_path: Path) -> None:
    deck = tmp_path / "deck.pptx"
    _write_pptx(deck, 1, SLIDE_CX, SLIDE_CY)
    proc, _rasters = _thumbnail(tmp_path, deck)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert (tmp_path / "grid.jpg").is_file()
    assert (tmp_path / "sentinel.txt").is_file()


@pytest.mark.parametrize("slides", [31, 45])
def test_thumbnail_slide_count_over_preview_limit_writes_nothing(tmp_path: Path, slides: int) -> None:
    deck = tmp_path / "deck.pptx"
    _write_pptx(deck, slides, SLIDE_CX, SLIDE_CY)
    proc, rasters = _thumbnail(tmp_path, deck)
    _assert_refused(proc, rasters, tmp_path)


@pytest.mark.parametrize("inches", [36, 72])
def test_thumbnail_oversize_slide_writes_nothing(tmp_path: Path, inches: int) -> None:
    deck = tmp_path / "deck.pptx"
    _write_pptx(deck, 1, _emu(inches), _emu(inches))
    proc, rasters = _thumbnail(tmp_path, deck)
    _assert_refused(proc, rasters, tmp_path)
