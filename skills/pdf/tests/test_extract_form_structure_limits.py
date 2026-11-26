"""Limits for PDF form-structure extraction.

`extract_form_structure.py` must stop with `FormStructureLimitError` instead of
retaining every page, word, long line, and checkbox from the file it is given.
These tests are the contract for that behavior. They fail while the script
still appends every object and passes once extraction refuses at these limits:

- ``MAX_PAGES`` pages (10)
- ``MAX_LABELS`` stored words (1000)
- ``MAX_LINES`` stored long horizontal lines (800)
- ``MAX_CHECKBOXES`` stored small rectangles (800)

A document at a limit is extracted completely. One more page, label, line, or
checkbox raises ``extract_form_structure.FormStructureLimitError``, a dedicated
exception class defined in that module. The message names the limit that was
hit (``page``, ``label`` or ``word``, ``line``, ``checkbox`` or ``rect``).
Objects on later pages are not parsed, and ``main`` does not create the output
file when it refuses.

Run from the repository root:

    python3 -m unittest discover -s skills/pdf/tests -p 'test_*.py' -v
"""

import json
import os
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import extract_form_structure as form_structure  # noqa: E402


# Inclusive maximums. The script may store this many and must refuse one more.
MAX_PAGES = 10
MAX_LABELS = 1000
MAX_LINES = 800
MAX_CHECKBOXES = 800

# Slack above the cost of the smallest over-limit file. One extra fully parsed
# page of the same objects costs more than this (measured against pdfplumber
# 0.11.11 / pdfminer.six 20260107); opening unread pages does not.
_LABEL_SLACK_BYTES = 6 * 1024 * 1024
_LINE_SLACK_BYTES = 4 * 1024 * 1024
_CHECKBOX_SLACK_BYTES = 5 * 1024 * 1024
_PAGE_SLACK_BYTES = 5 * 1024 * 1024


def _write_pdf(path, page_streams):
    """Write a PDF whose content streams are parsed by pdfplumber."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}

    def write_obj(oid, body):
        offsets[oid] = len(out)
        out.extend(f"{oid} 0 obj\n".encode())
        out.extend(body)
        out.extend(b"\nendobj\n")

    page_count = len(page_streams)
    write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = []
    next_id = 4
    page_ids = []
    for _ in range(page_count):
        page_id = next_id
        content_id = next_id + 1
        next_id += 2
        page_ids.append((page_id, content_id))
        kids.append(f"{page_id} 0 R")
    kids_literal = " ".join(kids)
    write_obj(
        2,
        f"<< /Type /Pages /Kids [{kids_literal}] /Count {page_count} >>".encode(),
    )
    write_obj(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for (page_id, content_id), stream in zip(page_ids, page_streams):
        write_obj(
            page_id,
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode(),
        )
        header = f"<< /Length {len(stream)} >>\nstream\n".encode()
        write_obj(content_id, header + stream + b"\nendstream")

    max_id = next_id - 1
    xref = len(out)
    out.extend(f"xref\n0 {max_id + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for oid in range(1, max_id + 1):
        out.extend(f"{offsets[oid]:010d} 00000 n \n".encode())
    out.extend(
        f"trailer << /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    with open(path, "wb") as handle:
        handle.write(out)


def _word_stream(count):
    """One content stream with ``count`` separated words."""
    operations = ["BT /F1 8 Tf"]
    for index in range(count):
        x = 20 + (index % 8) * 70
        y = 780 - (index // 8) * 12
        operations.append(f"1 0 0 1 {x} {y} Tm (w{index}) Tj")
    operations.append("ET")
    return "\n".join(operations).encode()


def _long_line_stream(count):
    """Horizontal lines wider than half of a 612-point page."""
    return "\n".join(f"10 {index * 3} m 600 {index * 3} l S" for index in range(count)).encode()


def _short_line_stream(count):
    """Horizontal lines too short to be stored as row boundaries."""
    return "\n".join(f"10 {index * 3} m 50 {index * 3} l S" for index in range(count)).encode()


def _checkbox_stream(count):
    """10-by-10 rectangles, which the script stores as checkboxes."""
    operations = []
    for index in range(count):
        x = 10 + (index % 40) * 15
        y = 10 + (index // 40) * 15
        operations.append(f"{x} {y} 10 10 re S")
    return "\n".join(operations).encode()


def _word_and_short_lines(line_count):
    return b"BT /F1 8 Tf 72 700 Td (Hi) Tj ET\n" + _short_line_stream(line_count)


class ExtractFormStructureLimitTests(unittest.TestCase):
    def limit_error(self):
        error_type = getattr(form_structure, "FormStructureLimitError", None)
        self.assertIsInstance(
            error_type,
            type,
            "extract_form_structure.FormStructureLimitError must be a dedicated exception class",
        )
        self.assertTrue(issubclass(error_type, Exception))
        self.assertIsNot(error_type, Exception)
        self.assertEqual(
            error_type.__module__,
            form_structure.__name__,
            "FormStructureLimitError must be defined on extract_form_structure",
        )
        return error_type

    def peak_bytes(self, func):
        tracemalloc.start()
        try:
            func()
        finally:
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return peak

    def assert_refuses(self, path, fragments):
        """Run extraction on ``path`` and require a limit error.

        The PDF is parsed first. While no cap exists the call returns the full
        structure and this assertion fails on those counts.
        """
        if isinstance(fragments, str):
            fragments = (fragments,)
        error_type = getattr(form_structure, "FormStructureLimitError", None)
        try:
            structure = form_structure.extract_form_structure(path)
        except Exception as exc:
            self.assertIsInstance(
                error_type,
                type,
                "extraction failed with "
                f"{type(exc).__name__}: {exc}, and FormStructureLimitError is not defined",
            )
            self.assertIsInstance(
                exc,
                error_type,
                f"expected FormStructureLimitError, got {type(exc).__name__}: {exc}",
            )
            message = str(exc).lower()
            self.assertTrue(
                any(fragment in message for fragment in fragments),
                f"expected the limit error to name one of {fragments!r}, got {exc!r}",
            )
            return
        self.fail(
            "extract_form_structure kept the whole file: "
            f"{len(structure['pages'])} pages, {len(structure['labels'])} labels, "
            f"{len(structure['lines'])} lines, {len(structure['checkboxes'])} checkboxes"
        )

    def assert_extra_pages_not_parsed(self, minimal_streams, extra_streams, slack):
        """The oversized file must be refused without parsing pages past the limit."""
        with tempfile.TemporaryDirectory() as tmp:
            minimal = os.path.join(tmp, "minimal.pdf")
            oversized = os.path.join(tmp, "oversized.pdf")
            _write_pdf(minimal, minimal_streams)
            _write_pdf(oversized, minimal_streams + extra_streams)
            minimal_peak = self.peak_bytes(
                lambda: self.assert_refuses(minimal, self._fragment)
            )
            oversized_peak = self.peak_bytes(
                lambda: self.assert_refuses(oversized, self._fragment)
            )
        self.assertLessEqual(
            oversized_peak,
            minimal_peak + slack,
            "extraction kept parsing pages after the limit was already exceeded",
        )

    def test_form_structure_limit_error_is_defined(self):
        self.limit_error()

    def test_mixed_form_under_every_limit_is_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "form.pdf")
            stream = b"\n".join(
                [
                    _word_stream(3),
                    _long_line_stream(2),
                    _checkbox_stream(2),
                ]
            )
            _write_pdf(path, [stream])
            structure = form_structure.extract_form_structure(path)
        self.assertEqual(len(structure["pages"]), 1)
        self.assertEqual(len(structure["labels"]), 3)
        self.assertEqual([label["text"] for label in structure["labels"]], ["w0", "w1", "w2"])
        self.assertEqual(len(structure["lines"]), 2)
        self.assertEqual(len(structure["checkboxes"]), 2)
        self.assertEqual(len(structure["row_boundaries"]), 1)

    def test_page_limit_allows_exactly_max_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pages.pdf")
            _write_pdf(path, [_word_stream(1)] * MAX_PAGES)
            structure = form_structure.extract_form_structure(path)
        self.assertEqual(len(structure["pages"]), MAX_PAGES)
        self.assertEqual(len(structure["labels"]), MAX_PAGES)

    def test_page_limit_refuses_one_more_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pages.pdf")
            _write_pdf(path, [_word_stream(1)] * (MAX_PAGES + 1))
            self.assert_refuses(path, "page")

    def test_label_limit_allows_exactly_max_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "labels.pdf")
            _write_pdf(path, [_word_stream(MAX_LABELS)])
            structure = form_structure.extract_form_structure(path)
        self.assertEqual(len(structure["labels"]), MAX_LABELS)

    def test_label_limit_refuses_one_more_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "labels.pdf")
            _write_pdf(path, [_word_stream(MAX_LABELS + 1)])
            self.assert_refuses(path, ("label", "word"))

    def test_line_limit_allows_exactly_max_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lines.pdf")
            _write_pdf(path, [_long_line_stream(MAX_LINES)])
            structure = form_structure.extract_form_structure(path)
        self.assertEqual(len(structure["lines"]), MAX_LINES)
        self.assertEqual(len(structure["row_boundaries"]), MAX_LINES - 1)

    def test_line_limit_refuses_one_more_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lines.pdf")
            _write_pdf(path, [_long_line_stream(MAX_LINES + 1)])
            self.assert_refuses(path, "line")

    def test_checkbox_limit_allows_exactly_max_checkboxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "boxes.pdf")
            _write_pdf(path, [_checkbox_stream(MAX_CHECKBOXES)])
            structure = form_structure.extract_form_structure(path)
        self.assertEqual(len(structure["checkboxes"]), MAX_CHECKBOXES)

    def test_checkbox_limit_refuses_one_more_checkbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "boxes.pdf")
            _write_pdf(path, [_checkbox_stream(MAX_CHECKBOXES + 1)])
            self.assert_refuses(path, ("checkbox", "rect"))

    def test_labels_past_the_cap_do_not_parse_later_pages(self):
        self._fragment = ("label", "word")
        minimal = [_word_stream(MAX_LABELS + 1)]
        extra = [_word_stream(MAX_LABELS)] * 5
        self.assert_extra_pages_not_parsed(minimal, extra, _LABEL_SLACK_BYTES)

    def test_lines_past_the_cap_do_not_parse_later_pages(self):
        self._fragment = "line"
        minimal = [_long_line_stream(MAX_LINES + 1)]
        extra = [_long_line_stream(MAX_LINES)] * 5
        self.assert_extra_pages_not_parsed(minimal, extra, _LINE_SLACK_BYTES)

    def test_checkboxes_past_the_cap_do_not_parse_later_pages(self):
        self._fragment = ("checkbox", "rect")
        minimal = [_checkbox_stream(MAX_CHECKBOXES + 1)]
        extra = [_checkbox_stream(MAX_CHECKBOXES)] * 5
        self.assert_extra_pages_not_parsed(minimal, extra, _CHECKBOX_SLACK_BYTES)

    def test_pages_past_the_cap_are_not_parsed(self):
        """Page count alone must stop the walk, before later content is parsed.

        The extra pages contain only a word and short strokes. Short strokes are
        not stored as lines, so the label and line caps stay untouched and the
        page cap is what refuses the file.
        """
        page = _word_and_short_lines(300)
        with tempfile.TemporaryDirectory() as tmp:
            allowed_path = os.path.join(tmp, "allowed.pdf")
            over_path = os.path.join(tmp, "over.pdf")
            _write_pdf(allowed_path, [page] * MAX_PAGES)
            _write_pdf(over_path, [page] * (MAX_PAGES + 12))

            allowed = {}

            def read_allowed():
                allowed["structure"] = form_structure.extract_form_structure(allowed_path)

            allowed_peak = self.peak_bytes(read_allowed)
            structure = allowed["structure"]
            self.assertEqual(len(structure["pages"]), MAX_PAGES)
            self.assertEqual(len(structure["labels"]), MAX_PAGES)
            self.assertEqual(len(structure["lines"]), 0)
            self.assertEqual(len(structure["checkboxes"]), 0)

            over_peak = self.peak_bytes(lambda: self.assert_refuses(over_path, "page"))
        self.assertLessEqual(over_peak, allowed_peak + _PAGE_SLACK_BYTES)

    def test_main_writes_structure_under_the_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "form.pdf")
            out_path = os.path.join(tmp, "form_structure.json")
            stream = b"\n".join([_word_stream(2), _long_line_stream(1), _checkbox_stream(1)])
            _write_pdf(pdf_path, [stream])
            argv = ["extract_form_structure.py", pdf_path, out_path]
            with mock.patch.object(sys, "argv", argv):
                form_structure.main()
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as handle:
                written = json.load(handle)
        self.assertEqual(len(written["labels"]), 2)
        self.assertEqual(len(written["lines"]), 1)
        self.assertEqual(len(written["checkboxes"]), 1)

    def test_main_does_not_create_output_when_a_limit_is_hit(self):
        error_type = getattr(form_structure, "FormStructureLimitError", None)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "form.pdf")
            out_path = os.path.join(tmp, "form_structure.json")
            _write_pdf(pdf_path, [_word_stream(1)] * (MAX_PAGES + 1))
            argv = ["extract_form_structure.py", pdf_path, out_path]
            try:
                with mock.patch.object(sys, "argv", argv):
                    form_structure.main()
            except Exception as exc:
                self.assertIsInstance(
                    error_type,
                    type,
                    "main failed with "
                    f"{type(exc).__name__}: {exc}, and FormStructureLimitError is not defined",
                )
                self.assertIsInstance(exc, error_type)
                self.assertIn("page", str(exc).lower())
                self.assertFalse(
                    os.path.exists(out_path),
                    "main created the output file before refusing the PDF",
                )
                return
            created = os.path.exists(out_path)
            self.fail(
                "main extracted a PDF past the page limit"
                + (" and wrote " + out_path if created else " without writing output")
            )


if __name__ == "__main__":
    unittest.main()
