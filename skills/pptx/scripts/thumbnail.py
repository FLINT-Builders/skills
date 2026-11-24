"""Create thumbnail grids from PowerPoint presentation slides.

Creates a grid layout of slide thumbnails for quick visual analysis.
Labels each thumbnail with its XML filename (e.g., slide1.xml).
Hidden slides are shown with a placeholder pattern.
Decks with more than MAX_SLIDES slides are refused, and pages are rendered
at a lower DPI when needed to stay within PIXEL_BUDGET pixels each.

Usage:
    python thumbnail.py input.pptx [output_prefix] [--cols N]

Examples:
    python thumbnail.py presentation.pptx
    # Creates: thumbnails.jpg

    python thumbnail.py template.pptx grid --cols 4
    # Creates: grid.jpg (or grid-1.jpg, grid-2.jpg for large decks)
"""

import argparse
import io
import math
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from defusedxml import ElementTree
from office.helpers import SLIDE_REL_TYPE, opc_target
from PIL import Image, ImageDraw, ImageFont


THUMBNAIL_WIDTH = 300
CONVERSION_DPI = 100
MAX_SLIDES = 40
PIXEL_BUDGET = 4_000_000
IMPRESS_OOXML_FILTER = "Impress MS PowerPoint 2007 XML"
OFFICE_DOCUMENT_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
}
SLIDE_REL_TYPES = {
    SLIDE_REL_TYPE,
    "http://purl.oclc.org/ooxml/officeDocument/relationships/slide",
}
_MEDIA_BOX_RE = re.compile(
    r"^Page\s+(\d+)\s+MediaBox:\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$",
    re.MULTILINE,
)
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)\s*$", re.MULTILINE)
MAX_COLS = 6
DEFAULT_COLS = 3
JPEG_QUALITY = 95
GRID_PADDING = 20
BORDER_WIDTH = 2
FONT_SIZE_RATIO = 0.10
LABEL_PADDING_RATIO = 0.4


def main():
    parser = argparse.ArgumentParser(
        description="Create thumbnail grids from PowerPoint slides."
    )
    parser.add_argument("input", help="Input PowerPoint file (.pptx)")
    parser.add_argument(
        "output_prefix",
        nargs="?",
        default="thumbnails",
        help="Output prefix for image files (default: thumbnails)",
    )
    parser.add_argument(
        "--cols",
        type=int,
        default=DEFAULT_COLS,
        help=f"Number of columns (default: {DEFAULT_COLS}, max: {MAX_COLS})",
    )

    args = parser.parse_args()

    cols = min(args.cols, MAX_COLS)
    if args.cols > MAX_COLS:
        print(f"Warning: Columns limited to {MAX_COLS}")

    input_path = Path(args.input)
    if not input_path.exists() or input_path.suffix.lower() != ".pptx":
        print(f"Error: Invalid PowerPoint file: {args.input}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(f"{args.output_prefix}.jpg")

    try:
        deck = input_path.read_bytes()
        slide_info = get_slide_info(deck)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            visible_images = convert_to_images(deck, temp_path)

            if not visible_images and not any(s["hidden"] for s in slide_info):
                print("Error: No slides found", file=sys.stderr)
                sys.exit(1)

            slides = build_slide_list(slide_info, visible_images, temp_path)

            grid_files = create_grids(slides, cols, THUMBNAIL_WIDTH, output_path)

            print(f"Created {len(grid_files)} grid(s):")
            for grid_file in grid_files:
                print(f"  {grid_file}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _is_hidden(zf: zipfile.ZipFile, part: str) -> bool:
    try:
        with zf.open(part) as f:
            for _, root in ElementTree.iterparse(f, events=("start",)):
                return root.get("show") in ("0", "false")  
    except (KeyError, ElementTree.ParseError):
        return False
    return False


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _open_package(deck: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(io.BytesIO(deck), "r")
    seen = set()
    for name in zf.namelist():
        key = name.replace("\\", "/").lstrip("/").lower()
        if key in seen:
            zf.close()
            raise ValueError(f"duplicate package part: {name!r}")
        seen.add(key)
    return zf


def _relationships(zf: zipfile.ZipFile, rels_part: str, source_part: str) -> list[tuple[str, str, str | None]]:
    try:
        root = ElementTree.fromstring(zf.read(rels_part))
    except KeyError:
        return []
    rels = []
    for rel in root.iter():
        if _local(rel.tag) != "Relationship":
            continue
        target = opc_target(rel.get("Target", ""), source_part, rel.get("TargetMode", ""))
        rels.append((rel.get("Id", ""), rel.get("Type", ""), target))
    return rels


def _presentation_part(zf: zipfile.ZipFile) -> str:
    parts = {
        target
        for _, rel_type, target in _relationships(zf, "_rels/.rels", "")
        if rel_type in OFFICE_DOCUMENT_REL_TYPES and target is not None
    }
    if len(parts) != 1:
        raise ValueError("package does not name exactly one presentation part")
    return parts.pop()


def get_slide_info(deck: bytes) -> list[dict]:
    return _read_slides(deck)[1]


def _read_slides(deck: bytes) -> tuple[int, list[dict]]:
    """Return (sldId count, slides), refusing decks with more than MAX_SLIDES.

    Every sldId in the presentation part counts toward the limit, whatever
    its prefix or namespace and whether or not its slide part exists, since
    LibreOffice may still render a page for it.
    """
    with _open_package(deck) as zf:
        pres_part = _presentation_part(zf)
        pres_dir, pres_name = posixpath.split(pres_part)
        rels_part = posixpath.join(pres_dir, "_rels", pres_name + ".rels")

        rid_to_part = {
            rid: target
            for rid, rel_type, target in _relationships(zf, rels_part, pres_part)
            if rel_type in SLIDE_REL_TYPES and target is not None
        }

        pres_root = ElementTree.fromstring(zf.read(pres_part))
        sld_ids = [el for el in pres_root.iter() if _local(el.tag) == "sldId"]
        if len(sld_ids) > MAX_SLIDES:
            raise ValueError(
                f"deck has {len(sld_ids)} slides; thumbnails are limited to {MAX_SLIDES}"
            )

        present = set(zf.namelist())

        slides = []
        for sld_id in sld_ids:
            rid = next(
                (v for k, v in sld_id.attrib.items() if k.startswith("{") and _local(k) == "id"),
                None,
            )
            part = rid_to_part.get(rid)
            if part is not None and part in present:
                slides.append(
                    {"name": posixpath.basename(part), "hidden": _is_hidden(zf, part)}
                )

        return len(sld_ids), slides


def build_slide_list(
    slide_info: list[dict],
    visible_images: list[Path],
    temp_dir: Path,
) -> list[tuple[Path, str]]:
    visible_count = sum(1 for info in slide_info if not info["hidden"])
    rendered_hidden = len(visible_images) == len(slide_info) != visible_count

    if not rendered_hidden and visible_count != len(visible_images):
        raise ValueError(
            f"LibreOffice rendered {len(visible_images)} page(s) for {visible_count} "
            f"visible slide(s) of {len(slide_info)}; thumbnails would be mislabeled"
        )

    if visible_images:
        with Image.open(visible_images[0]) as img:
            placeholder_size = img.size
    else:
        placeholder_size = (1920, 1080)

    slides = []
    visible_idx = 0

    for info in slide_info:
        if info["hidden"] and not rendered_hidden:
            placeholder_path = temp_dir / f"hidden-{info['name']}.jpg"
            placeholder_img = create_hidden_placeholder(placeholder_size)
            placeholder_img.save(placeholder_path, "JPEG")
            slides.append((placeholder_path, f"{info['name']} (hidden)"))
        else:
            label = f"{info['name']} (hidden)" if info["hidden"] else info["name"]
            slides.append((visible_images[visible_idx], label))
            visible_idx += 1

    return slides


def create_hidden_placeholder(size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGB", size, color="#F0F0F0")
    draw = ImageDraw.Draw(img)
    line_width = max(5, min(size) // 100)
    draw.line([(0, 0), size], fill="#CCCCCC", width=line_width)
    draw.line([(size[0], 0), (0, size[1])], fill="#CCCCCC", width=line_width)
    return img


def _raster_pixels(width_pt: float, height_pt: float, dpi: float) -> int:
    return (math.ceil(width_pt * dpi / 72) + 1) * (math.ceil(height_pt * dpi / 72) + 1)


def _raster_dpi(media_boxes: list[tuple[float, float]]) -> float:
    dpi = float(CONVERSION_DPI)
    for width_pt, height_pt in media_boxes:
        area = (width_pt / 72) * (height_pt / 72)
        if area > 0:
            dpi = min(dpi, math.sqrt(PIXEL_BUDGET / area))
        while _raster_pixels(width_pt, height_pt, dpi) > PIXEL_BUDGET:
            dpi *= 0.9
    return dpi


def _pdf_media_boxes(pdf_path: Path, max_pages: int) -> list[tuple[float, float]]:
    result = subprocess.run(
        ["pdfinfo", "-box", "-f", "1", "-l", str(max_pages), str(pdf_path)],
        capture_output=True,
        text=True,
    )
    pages = _PAGES_RE.search(result.stdout or "")
    if result.returncode != 0 or pages is None:
        raise RuntimeError("Could not read page sizes from the converted PDF")
    page_count = int(pages.group(1))
    if page_count > max_pages:
        raise RuntimeError(
            f"LibreOffice rendered {page_count} page(s) for a deck of {max_pages} slide(s)"
        )

    boxes = {}
    for match in _MEDIA_BOX_RE.finditer(result.stdout):
        x0, y0, x1, y1 = (float(v) for v in match.groups()[1:])
        boxes[int(match.group(1))] = (abs(x1 - x0), abs(y1 - y0))
    if sorted(boxes) != list(range(1, page_count + 1)):
        raise RuntimeError("Could not read page sizes from the converted PDF")
    return [boxes[i] for i in range(1, page_count + 1)]


def convert_to_images(deck: bytes | Path, temp_dir: Path) -> list[Path]:
    """Render the deck's pages to JPEGs in temp_dir.

    The bytes that are counted are the bytes LibreOffice converts: the deck
    is read once and a private copy of it is what gets converted.
    """
    if not isinstance(deck, bytes):
        deck = Path(deck).read_bytes()
    slide_count, _ = _read_slides(deck)
    if slide_count == 0:
        return []

    # Importing office.soffice probes the temp directory by creating a file,
    # so it must not happen before the deck has passed the slide limit.
    from office.soffice import run_soffice

    fd, copy_name = tempfile.mkstemp(suffix=".pptx", dir=temp_dir)
    with os.fdopen(fd, "wb") as fh:
        fh.write(deck)
    deck_copy = Path(copy_name)
    pdf_path = deck_copy.with_suffix(".pdf")
    if pdf_path.exists():
        raise RuntimeError(f"PDF conversion output already exists: {pdf_path}")

    result = run_soffice(
        [
            "--headless",
            f"--infilter={IMPRESS_OOXML_FILTER}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(temp_dir),
            str(deck_copy),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not pdf_path.exists():
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"PDF conversion failed: {detail}" if detail else "PDF conversion failed")

    media_boxes = _pdf_media_boxes(pdf_path, slide_count)
    if not media_boxes:
        return []

    result = subprocess.run(
        [
            "pdftoppm",
            "-jpeg",
            "-r",
            f"{math.floor(_raster_dpi(media_boxes) * 10000) / 10000:.4f}",
            "-f",
            "1",
            "-l",
            str(len(media_boxes)),
            str(pdf_path),
            str(temp_dir / "slide"),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Image conversion failed")

    return sorted(temp_dir.glob("slide-*.jpg"))


def create_grids(
    slides: list[tuple[Path, str]],
    cols: int,
    width: int,
    output_path: Path,
) -> list[str]:
    max_per_grid = cols * (cols + 1)
    grid_files = []

    for chunk_idx, start_idx in enumerate(range(0, len(slides), max_per_grid)):
        end_idx = min(start_idx + max_per_grid, len(slides))
        chunk_slides = slides[start_idx:end_idx]

        grid = create_grid(chunk_slides, cols, width)

        if len(slides) <= max_per_grid:
            grid_filename = output_path
        else:
            stem = output_path.stem
            suffix = output_path.suffix
            grid_filename = output_path.parent / f"{stem}-{chunk_idx + 1}{suffix}"

        grid_filename.parent.mkdir(parents=True, exist_ok=True)
        grid.save(str(grid_filename), quality=JPEG_QUALITY)
        grid_files.append(str(grid_filename))

    return grid_files


def create_grid(
    slides: list[tuple[Path, str]],
    cols: int,
    width: int,
) -> Image.Image:
    font_size = int(width * FONT_SIZE_RATIO)
    label_padding = int(font_size * LABEL_PADDING_RATIO)

    with Image.open(slides[0][0]) as img:
        aspect = img.height / img.width
    height = int(width * aspect)

    rows = (len(slides) + cols - 1) // cols
    grid_w = cols * width + (cols + 1) * GRID_PADDING
    grid_h = rows * (height + font_size + label_padding * 2) + (rows + 1) * GRID_PADDING

    grid = Image.new("RGB", (grid_w, grid_h), "white")
    draw = ImageDraw.Draw(grid)

    try:
        font = ImageFont.load_default(size=font_size)
    except Exception:
        font = ImageFont.load_default()

    for i, (img_path, slide_name) in enumerate(slides):
        row, col = i // cols, i % cols
        x = col * width + (col + 1) * GRID_PADDING
        y_base = (
            row * (height + font_size + label_padding * 2) + (row + 1) * GRID_PADDING
        )

        label = slide_name
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text(
            (x + (width - text_w) // 2, y_base + label_padding),
            label,
            fill="black",
            font=font,
        )

        y_thumbnail = y_base + label_padding + font_size + label_padding

        with Image.open(img_path) as img:
            img.thumbnail((width, height), Image.Resampling.LANCZOS)
            w, h = img.size
            tx = x + (width - w) // 2
            ty = y_thumbnail + (height - h) // 2
            grid.paste(img, (tx, ty))

            if BORDER_WIDTH > 0:
                draw.rectangle(
                    [
                        (tx - BORDER_WIDTH, ty - BORDER_WIDTH),
                        (tx + w + BORDER_WIDTH - 1, ty + h + BORDER_WIDTH - 1),
                    ],
                    outline="gray",
                    width=BORDER_WIDTH,
                )

    return grid


if __name__ == "__main__":
    main()
