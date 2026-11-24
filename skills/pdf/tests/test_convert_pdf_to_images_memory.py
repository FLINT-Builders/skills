"""Bounds for skills/pdf/scripts/convert_pdf_to_images.py.

The form-filling guide runs this script on the PDF the user supplied.
These tests feed it the same kind of input that guide accepts: a small PDF
whose pages share one empty content stream and whose MediaBox is chosen by
the file, not by the script.

They fail while convert() renders every page at 200 DPI into one in-memory
list. A fix passes them by refusing that input, or by writing pages whose
long side is at most the script's max_dim, without a resident set above
MEMORY_BUDGET_KB.

Run from the repository root:

    python3 skills/pdf/tests/test_convert_pdf_to_images_memory.py
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "skills" / "pdf" / "scripts" / "convert_pdf_to_images.py"
# Default max_dim in convert(). Output pages must stay inside it.
MAX_DIM = 1000
# A letter page rendered by the current script peaks near 55 MB.
# One 3400pt page peaks near 750 MB, and twenty letter pages near 650 MB,
# because every full-resolution page is kept. 256 MB is above one capped
# page and below either of those unbounded runs.
MEMORY_BUDGET_KB = 256 * 1024
MAX_PDF_BYTES = 8192
MAX_PNG_BYTES = 2_000_000


def _make_pdf(path, pages, media_box, crop_box=None):
    """Write a PDF of `pages` that all reference one empty content stream."""
    width, height = media_box
    page_ids = list(range(3, 3 + pages))
    content_id = 3 + pages
    objects = [
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        "2 0 obj\n<< /Type /Pages /Kids [%s] /Count %d >>\nendobj\n"
        % (" ".join("%d 0 R" % i for i in page_ids), pages),
    ]
    crop = ""
    if crop_box is not None:
        crop = " /CropBox [0 0 %s %s]" % crop_box
    for page_id in page_ids:
        objects.append(
            "%d 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s]%s "
            "/Contents %d 0 R >>\nendobj\n"
            % (page_id, width, height, crop, content_id)
        )
    objects.append(
        "%d 0 obj\n<< /Length 0 >>\nstream\nendstream\nendobj\n" % content_id
    )
    body = "%PDF-1.4\n"
    offsets = [0]
    encoded = body.encode("ascii")
    chunks = [encoded]
    cursor = len(encoded)
    for obj in objects:
        offsets.append(cursor)
        raw = obj.encode("ascii")
        chunks.append(raw)
        cursor += len(raw)
    xref_pos = cursor
    xref = ["xref\n0 %d\n0000000000 65535 f \n" % len(offsets)]
    for offset in offsets[1:]:
        xref.append("%010d 00000 n \n" % offset)
    trailer = (
        "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(offsets), xref_pos)
    )
    data = b"".join(chunks) + ("".join(xref) + trailer).encode("ascii")
    path.write_bytes(data)
    return len(data)


def _vm_hwm_kb(pid):
    try:
        with open("/proc/%d/status" % pid, "r", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return 0
    return 0


def _child_pids(pid):
    children = []
    task_dir = "/proc/%d/task" % pid
    try:
        tasks = os.listdir(task_dir)
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return children
    for task in tasks:
        path = os.path.join(task_dir, task, "children")
        try:
            with open(path, "r", encoding="ascii") as handle:
                raw = handle.read().split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        children.extend(int(item) for item in raw)
    return children


def _tree_hwm_kb(pid):
    best = 0
    stack = [pid]
    seen = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        best = max(best, _vm_hwm_kb(current))
        stack.extend(_child_pids(current))
    return best


def _run_convert(pdf_path, output_dir):
    """Run the script the form guide invokes, sampling peak RSS of its tree."""
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), str(pdf_path), str(output_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak_kb = 0
    stop = False

    def sample():
        nonlocal peak_kb
        while not stop:
            peak_kb = max(peak_kb, _tree_hwm_kb(proc.pid))
            time.sleep(0.01)

    sampler = threading.Thread(target=sample)
    sampler.start()
    try:
        stdout, stderr = proc.communicate(timeout=180)
    finally:
        stop = True
        sampler.join()
        peak_kb = max(peak_kb, _tree_hwm_kb(proc.pid))
    images = []
    for image_path in sorted(Path(output_dir).glob("*")):
        if not image_path.is_file():
            continue
        with Image.open(image_path) as image:
            images.append((image_path.name, image.size, image_path.stat().st_size))
    return proc.returncode, peak_kb, stdout, stderr, images


class ConvertPdfToImagesMemoryTest(unittest.TestCase):
    def _assert_no_full_raster_outputs(self, images):
        for name, size, nbytes in images:
            self.assertLessEqual(
                max(size),
                MAX_DIM,
                "%s is %s, larger than max_dim %s" % (name, size, MAX_DIM),
            )
            self.assertLess(
                nbytes,
                MAX_PNG_BYTES,
                "%s is %d bytes" % (name, nbytes),
            )

    def _assert_within_budget(self, peak_kb, returncode, stderr):
        self.assertLess(
            peak_kb,
            MEMORY_BUDGET_KB,
            "peak resident set was %d KB (budget %d KB); stderr=%r"
            % (peak_kb, MEMORY_BUDGET_KB, stderr[-500:]),
        )
        # A signal means the process was killed while rendering, which is the
        # failure mode under test, not a refusal that happens first.
        self.assertGreaterEqual(returncode, 0, stderr[-500:])
        self.assertNotIn("MemoryError", stderr)

    def test_letter_page_converts(self):
        """A normal single-page form still converts and stays small."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "letter.pdf"
            output_dir = tmp_path / "out"
            output_dir.mkdir()
            _make_pdf(pdf_path, pages=1, media_box=(612, 792))
            returncode, peak_kb, stdout, stderr, images = _run_convert(
                pdf_path, output_dir
            )
            self.assertEqual(returncode, 0, stderr or stdout)
            self._assert_within_budget(peak_kb, returncode, stderr)
            self.assertEqual(len(images), 1)
            self._assert_no_full_raster_outputs(images)
            self.assertTrue((output_dir / "page_1.png").is_file())

    def test_huge_mediabox_stays_within_budget(self):
        """One ~3400pt page is under Pillow's bomb limit and must not allocate it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "huge.pdf"
            output_dir = tmp_path / "out"
            output_dir.mkdir()
            pdf_bytes = _make_pdf(pdf_path, pages=1, media_box=(3400, 3400))
            self.assertLess(pdf_bytes, MAX_PDF_BYTES)
            returncode, peak_kb, stdout, stderr, images = _run_convert(
                pdf_path, output_dir
            )
            self._assert_within_budget(peak_kb, returncode, stderr)
            self._assert_no_full_raster_outputs(images)
            if returncode == 0:
                self.assertEqual(len(images), 1, stdout)
            else:
                self.assertEqual(images, [], "refusing the PDF still wrote %s" % images)

    def test_huge_mediabox_with_small_cropbox_stays_within_budget(self):
        """Raster size follows the MediaBox. A letter CropBox does not cap it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "crop.pdf"
            output_dir = tmp_path / "out"
            output_dir.mkdir()
            pdf_bytes = _make_pdf(
                pdf_path,
                pages=1,
                media_box=(3400, 3400),
                crop_box=(612, 792),
            )
            self.assertLess(pdf_bytes, MAX_PDF_BYTES)
            returncode, peak_kb, stdout, stderr, images = _run_convert(
                pdf_path, output_dir
            )
            self._assert_within_budget(peak_kb, returncode, stderr)
            self._assert_no_full_raster_outputs(images)
            if returncode != 0:
                self.assertEqual(images, [], "refusing the PDF still wrote %s" % images)

    def test_many_letter_pages_are_not_all_resident(self):
        """Twenty ordinary pages must convert without retaining every raster."""
        page_count = 20
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "many.pdf"
            output_dir = tmp_path / "out"
            output_dir.mkdir()
            pdf_bytes = _make_pdf(pdf_path, pages=page_count, media_box=(612, 792))
            self.assertLess(pdf_bytes, MAX_PDF_BYTES)
            returncode, peak_kb, stdout, stderr, images = _run_convert(
                pdf_path, output_dir
            )
            self.assertEqual(
                returncode,
                0,
                "a %d-page letter PDF is within a 50-page cap and must convert: %s"
                % (page_count, stderr or stdout),
            )
            self._assert_within_budget(peak_kb, returncode, stderr)
            self.assertEqual(len(images), page_count, stdout)
            self._assert_no_full_raster_outputs(images)


if __name__ == "__main__":
    unittest.main()
