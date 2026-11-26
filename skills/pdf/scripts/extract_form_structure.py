"""
Extract form structure from a non-fillable PDF.

This script analyzes the PDF to find:
- Text labels with their exact coordinates
- Horizontal lines (row boundaries)
- Checkboxes (small rectangles)

Output: A JSON file with the form structure that can be used to generate
accurate field coordinates for filling.

The PDF is untrusted input, so extraction is bounded: a file with more pages,
labels, lines, checkboxes, drawn objects, content, or decompressed data than
the limits below is refused with FormStructureLimitError and no output is
written.

Usage: python extract_form_structure.py <input.pdf> <output.json>
"""

import contextlib
import io
import json
import sys
import zlib

import pdfplumber
from pdfminer import pdftypes
from pdfminer.lzw import LZWDecoder
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import stream_value
from pdfminer.psparser import PSStackParser
from pdfplumber.page import Page, PDFPageAggregatorWithMarkedContent
from pdfplumber.utils.exceptions import PdfminerException


MAX_PAGES = 10
MAX_LABELS = 1000
MAX_LINES = 800
MAX_CHECKBOXES = 800
# Characters, path segments, images, and form XObjects drawn across all pages.
MAX_LAYOUT_OBJECTS = 60_000
# Content-stream bytes executed across all pages; a form XObject counts every
# time it is drawn.
MAX_CONTENT_BYTES = 8 * 1024 * 1024
# Bytes produced by stream filters (Flate, LZW, ...) while reading the file.
MAX_DECODED_BYTES = 64 * 1024 * 1024
# Tokens read by pdfminer's parsers, from the file and from content streams.
MAX_PARSED_TOKENS = 2_000_000
# Nested arrays, dictionaries, and procedures open at once in one parser.
MAX_NESTING_DEPTH = 100
MAX_GRAPHICS_STATE_DEPTH = 256


class FormStructureLimitError(Exception):
    """The PDF is larger than this script will analyze."""


class _Budget:
    """Shared allowance for everything pdfminer allocates from the file.

    pdfminer and pdfplumber swallow or wrap some exceptions, so the first
    refusal is remembered and raised again at the next checkpoint.
    """

    def __init__(self):
        self.layout_objects = 0
        self.content_bytes = 0
        self.decoded_bytes = 0
        self.parsed_tokens = 0
        self.refusal = None

    def refuse(self, message):
        if self.refusal is None:
            self.refusal = message
        raise FormStructureLimitError(self.refusal)

    def check(self):
        if self.refusal is not None:
            raise FormStructureLimitError(self.refusal)

    def charge_layout_object(self):
        self.check()
        self.layout_objects += 1
        if self.layout_objects > MAX_LAYOUT_OBJECTS:
            self.refuse(
                f"PDF draws more than {MAX_LAYOUT_OBJECTS} layout objects "
                "(characters, paths, images)"
            )

    def charge_content(self, size):
        self.check()
        self.content_bytes += size
        if self.content_bytes > MAX_CONTENT_BYTES:
            self.refuse(f"PDF content streams exceed {MAX_CONTENT_BYTES} bytes")

    def remaining_decoded(self):
        self.check()
        return MAX_DECODED_BYTES - self.decoded_bytes

    def charge_decoded(self, size):
        self.check()
        self.decoded_bytes += size
        if self.decoded_bytes > MAX_DECODED_BYTES:
            self.refuse(f"PDF streams decompress to more than {MAX_DECODED_BYTES} bytes")

    def charge_tokens(self, count):
        self.check()
        self.parsed_tokens += count
        if self.parsed_tokens > MAX_PARSED_TOKENS:
            self.refuse(f"PDF has more than {MAX_PARSED_TOKENS} syntax tokens")


class _BoundedDecompressor:
    def __init__(self, budget):
        self._budget = budget
        self._decompressor = zlib.decompressobj()

    @property
    def eof(self):
        return self._decompressor.eof

    def decompress(self, data):
        # Output is capped one byte past the allowance, so a short result
        # means all input was consumed.
        out = self._decompressor.decompress(data, self._budget.remaining_decoded() + 1)
        self._budget.charge_decoded(len(out))
        return out


class _BoundedZlib:
    """Stands in for the zlib module inside pdfminer.pdftypes."""

    error = zlib.error

    def __init__(self, budget):
        self._budget = budget

    def decompressobj(self):
        return _BoundedDecompressor(self._budget)

    def decompress(self, data):
        decompressor = self.decompressobj()
        out = decompressor.decompress(data)
        if not decompressor.eof:
            raise zlib.error("incomplete or truncated stream")
        return out


def _bounded_lzwdecode(budget, data):
    chunks = []
    for chunk in LZWDecoder(io.BytesIO(data)).run():
        budget.charge_decoded(len(chunk))
        chunks.append(chunk)
    return b"".join(chunks)


def _runlength_size(data):
    size = 0
    i = 0
    while i < len(data):
        length = data[i]
        if length == 128:
            break
        if length < 128:
            size += length + 1
            i += length + 2
        else:
            size += 257 - length
            i += 2
    return size


@contextlib.contextmanager
def _bounded_pdfminer(budget):
    """Make pdfminer's stream filters and parsers charge ``budget``.

    pdfminer decodes each stream in full with no size limit, so a small
    compressed stream can otherwise expand without bound before anything is
    parsed from it. Its parsers keep every token of an open array or
    dictionary, about a hundred times the bytes they were read from.
    """
    original_push = PSStackParser.push
    original_start_type = PSStackParser.start_type

    def push(parser, *objs):
        budget.charge_tokens(len(objs))
        original_push(parser, *objs)

    def start_type(parser, pos, type):
        if len(parser.context) >= MAX_NESTING_DEPTH:
            budget.refuse(f"PDF nests objects deeper than {MAX_NESTING_DEPTH}")
        budget.charge_tokens(1)
        original_start_type(parser, pos, type)

    originals = {
        name: getattr(pdftypes, name)
        for name in ("zlib", "lzwdecode", "rldecode", "ccittfaxdecode",
                     "ascii85decode", "asciihexdecode")
    }

    def rldecode(data):
        budget.charge_decoded(_runlength_size(data))
        return originals["rldecode"](data)

    def ccittfaxdecode(data, params):
        columns = int(params.get("Columns") or 1728)
        rows = len(data) * 8 + 1
        budget.charge_decoded(rows * ((columns + 7) // 8))
        return originals["ccittfaxdecode"](data, params)

    def charge_output(decode):
        def wrapper(data):
            budget.check()
            out = decode(data)
            budget.charge_decoded(len(out))
            return out
        return wrapper

    pdftypes.zlib = _BoundedZlib(budget)
    pdftypes.lzwdecode = lambda data: _bounded_lzwdecode(budget, data)
    pdftypes.rldecode = rldecode
    pdftypes.ccittfaxdecode = ccittfaxdecode
    pdftypes.ascii85decode = charge_output(originals["ascii85decode"])
    pdftypes.asciihexdecode = charge_output(originals["asciihexdecode"])
    PSStackParser.push = push
    PSStackParser.start_type = start_type
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(pdftypes, name, value)
        PSStackParser.push = original_push
        PSStackParser.start_type = original_start_type


class _BudgetedAggregator(PDFPageAggregatorWithMarkedContent):
    def __init__(self, *args, budget, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = budget

    def begin_figure(self, *args, **kwargs):
        self.budget.charge_layout_object()
        super().begin_figure(*args, **kwargs)

    def render_char(self, *args, **kwargs):
        self.budget.charge_layout_object()
        return super().render_char(*args, **kwargs)

    def render_image(self, *args, **kwargs):
        self.budget.charge_layout_object()
        super().render_image(*args, **kwargs)

    def paint_path(self, *args, **kwargs):
        self.budget.charge_layout_object()
        super().paint_path(*args, **kwargs)


class _BudgetedInterpreter(PDFPageInterpreter):
    def __init__(self, rsrcmgr, device, budget):
        super().__init__(rsrcmgr, device)
        self.budget = budget

    def dup(self):
        return self.__class__(self.rsrcmgr, self.device, self.budget)

    def execute(self, streams):
        for obj in streams:
            self.budget.charge_content(len(stream_value(obj).get_data()))
        super().execute(streams)

    def do_q(self):
        if len(self.gstack) >= MAX_GRAPHICS_STATE_DEPTH:
            self.budget.refuse(
                f"PDF nests graphics states deeper than {MAX_GRAPHICS_STATE_DEPTH}"
            )
        super().do_q()


class _BudgetedPage(Page):
    """A pdfplumber page whose layout is parsed under ``budget``.

    pdfplumber's own ``layout`` wraps every parsing error in
    PdfminerException, which would hide FormStructureLimitError.
    """

    def __init__(self, *args, budget, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = budget

    @property
    def layout(self):
        if hasattr(self, "_layout"):
            return self._layout
        device = _BudgetedAggregator(
            self.pdf.rsrcmgr,
            pageno=self.page_number,
            laparams=self.pdf.laparams,
            budget=self.budget,
        )
        interpreter = _BudgetedInterpreter(self.pdf.rsrcmgr, device, self.budget)
        try:
            interpreter.process_page(self.page_obj)
        except FormStructureLimitError:
            raise
        except Exception as e:
            self.budget.check()
            raise PdfminerException(e)
        self._layout = device.get_result()
        return self._layout


def _append_limited(items, item, limit, name):
    if len(items) >= limit:
        raise FormStructureLimitError(f"PDF has more than {limit} {name}")
    items.append(item)


def _open_pages(pdf, budget):
    """Return the document's pages, refusing before any page is parsed."""
    page_objs = []
    for page_obj in PDFPage.create_pages(pdf.doc):
        budget.check()
        if len(page_objs) >= MAX_PAGES:
            raise FormStructureLimitError(f"PDF has more than {MAX_PAGES} pages")
        page_objs.append(page_obj)
    budget.check()

    pages = []
    doctop = 0
    for page_number, page_obj in enumerate(page_objs, 1):
        page = _BudgetedPage(
            pdf, page_obj, page_number=page_number, initial_doctop=doctop, budget=budget
        )
        doctop += page.height
        pages.append(page)
    return pages


def extract_form_structure(pdf_path):
    structure = {
        "pages": [],
        "labels": [],
        "lines": [],
        "checkboxes": [],
        "row_boundaries": []
    }

    budget = _Budget()
    try:
        # PDF.close() builds a Page for every page in the file, so the PDF
        # object is not closed; the stream is closed here instead.
        with _bounded_pdfminer(budget), open(pdf_path, "rb") as stream:
            pdf = pdfplumber.PDF(stream, stream_is_external=True)
            budget.check()
            for page in _open_pages(pdf, budget):
                _extract_page(structure, page)
                page.close()
                budget.check()
    except FormStructureLimitError:
        raise
    except Exception:
        budget.check()
        raise

    lines_by_page = {}
    for line in structure["lines"]:
        page = line["page"]
        if page not in lines_by_page:
            lines_by_page[page] = []
        lines_by_page[page].append(line["y"])

    for page, y_coords in lines_by_page.items():
        y_coords = sorted(set(y_coords))
        for i in range(len(y_coords) - 1):
            structure["row_boundaries"].append({
                "page": page,
                "row_top": y_coords[i],
                "row_bottom": y_coords[i + 1],
                "row_height": round(y_coords[i + 1] - y_coords[i], 1)
            })

    return structure


def _extract_page(structure, page):
    page_num = page.page_number
    structure["pages"].append({
        "page_number": page_num,
        "width": float(page.width),
        "height": float(page.height)
    })

    words = page.extract_words()
    for word in words:
        _append_limited(structure["labels"], {
            "page": page_num,
            "text": word["text"],
            "x0": round(float(word["x0"]), 1),
            "top": round(float(word["top"]), 1),
            "x1": round(float(word["x1"]), 1),
            "bottom": round(float(word["bottom"]), 1)
        }, MAX_LABELS, "text labels (words)")

    for line in page.lines:
        if abs(float(line["x1"]) - float(line["x0"])) > page.width * 0.5:
            _append_limited(structure["lines"], {
                "page": page_num,
                "y": round(float(line["top"]), 1),
                "x0": round(float(line["x0"]), 1),
                "x1": round(float(line["x1"]), 1)
            }, MAX_LINES, "horizontal lines")

    for rect in page.rects:
        width = float(rect["x1"]) - float(rect["x0"])
        height = float(rect["bottom"]) - float(rect["top"])
        if 5 <= width <= 15 and 5 <= height <= 15 and abs(width - height) < 2:
            _append_limited(structure["checkboxes"], {
                "page": page_num,
                "x0": round(float(rect["x0"]), 1),
                "top": round(float(rect["top"]), 1),
                "x1": round(float(rect["x1"]), 1),
                "bottom": round(float(rect["bottom"]), 1),
                "center_x": round((float(rect["x0"]) + float(rect["x1"])) / 2, 1),
                "center_y": round((float(rect["top"]) + float(rect["bottom"])) / 2, 1)
            }, MAX_CHECKBOXES, "checkboxes")


def main():
    if len(sys.argv) != 3:
        print("Usage: extract_form_structure.py <input.pdf> <output.json>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_path = sys.argv[2]

    print(f"Extracting structure from {pdf_path}...")
    structure = extract_form_structure(pdf_path)

    with open(output_path, "w") as f:
        json.dump(structure, f, indent=2)

    print(f"Found:")
    print(f"  - {len(structure['pages'])} pages")
    print(f"  - {len(structure['labels'])} text labels")
    print(f"  - {len(structure['lines'])} horizontal lines")
    print(f"  - {len(structure['checkboxes'])} checkboxes")
    print(f"  - {len(structure['row_boundaries'])} row boundaries")
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    try:
        main()
    except FormStructureLimitError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
