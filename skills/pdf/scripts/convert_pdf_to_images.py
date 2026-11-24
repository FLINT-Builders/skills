import os
import re
import sys
import tempfile

from pdf2image import convert_from_path
from PIL import Image


RENDERED_PAGE = re.compile(r"-(\d+)\.png$")


def convert(pdf_path, output_dir, max_dim=1000):
    # The page boxes come from the PDF, so the raster size must not. size= makes
    # pdftoppm derive its resolution from max_dim (-scale-to) before allocating
    # the page, and output_folder has it write each page to disk as it goes
    # rather than streaming every page back into this process.
    with tempfile.TemporaryDirectory(dir=output_dir) as render_dir:
        rendered = convert_from_path(
            pdf_path,
            size=max_dim,
            fmt="png",
            output_folder=render_dir,
            output_file="page",
            paths_only=True,
        )

        for rendered_path in rendered:
            page_number = int(RENDERED_PAGE.search(rendered_path).group(1))
            image_path = os.path.join(output_dir, f"page_{page_number}.png")
            os.replace(rendered_path, image_path)
            with Image.open(image_path) as image:
                size = image.size
            print(f"Saved page {page_number} as {image_path} (size: {size})")

    print(f"Converted {len(rendered)} pages to PNG images")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: convert_pdf_to_images.py [input pdf] [output directory]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    output_directory = sys.argv[2]
    convert(pdf_path, output_directory)
