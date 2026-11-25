import os
import sys

from pdf2image import convert_from_path, pdfinfo_from_path


MAX_PAGES = 40


def convert(pdf_path, output_dir, max_dim=1000, max_pages=MAX_PAGES):
    page_count = pdfinfo_from_path(pdf_path)["Pages"]
    if page_count > max_pages:
        raise ValueError(
            f"{pdf_path} has {page_count} pages; conversion is limited to {max_pages}"
        )

    # size scales each page's longer edge to max_dim inside pdftoppm, so an
    # oversized page is never rendered at full resolution.
    images = convert_from_path(
        pdf_path, size=max_dim, first_page=1, last_page=max_pages
    )

    for i, image in enumerate(images):
        image_path = os.path.join(output_dir, f"page_{i+1}.png")
        image.save(image_path)
        print(f"Saved page {i+1} as {image_path} (size: {image.size})")

    print(f"Converted {len(images)} pages to PNG images")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: convert_pdf_to_images.py [input pdf] [output directory]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    output_directory = sys.argv[2]
    try:
        convert(pdf_path, output_directory)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
