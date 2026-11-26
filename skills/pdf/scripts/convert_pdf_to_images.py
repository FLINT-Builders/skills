import os
import sys
import tempfile

from pdf2image import convert_from_path, pdfinfo_from_path
from PIL import Image


MAX_PAGES = 50


class PageLimitError(Exception):
    """The PDF has more pages than this script will convert."""


def convert(pdf_path, output_dir, max_dim=1000):
    page_count = int(pdfinfo_from_path(pdf_path)["Pages"])
    if page_count > MAX_PAGES:
        raise PageLimitError(f"PDF has {page_count} pages, more than the {MAX_PAGES} page limit")

    # pdftoppm scales each page to fit max_dim while rendering and writes it to
    # disk, so no page is rendered larger than that and none is held in memory.
    with tempfile.TemporaryDirectory(dir=output_dir) as render_dir:
        rendered = convert_from_path(
            pdf_path,
            size=max_dim,
            first_page=1,
            last_page=page_count,
            output_folder=render_dir,
            fmt="png",
            paths_only=True,
        )
        for i, rendered_path in enumerate(rendered):
            image_path = os.path.join(output_dir, f"page_{i+1}.png")
            os.replace(rendered_path, image_path)
            with Image.open(image_path) as image:
                size = image.size
            print(f"Saved page {i+1} as {image_path} (size: {size})")

    print(f"Converted {len(rendered)} pages to PNG images")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: convert_pdf_to_images.py [input pdf] [output directory]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    output_directory = sys.argv[2]
    try:
        convert(pdf_path, output_directory)
    except PageLimitError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
