"""Recalculation must not materialize an unbounded used range.

These tests fail while the vulnerability is present. They pass once every cell
walk in recalc.py either visits only stored cells or rejects an oversized used
range before walking cells and before rewriting the file. A rejection counts
only when its error text contains one of: dimension, used range, oversized,
too large, exceeds, max_row, max_column, row limit, column limit.

The triggering workbook stores a handful of cells, including one at XFD1048576,
so the worksheet dimension is A1:XFD1048576. openpyxl 3.1.5 ignores a dimension
element that no stored cell reaches (load_workbook is not read-only). The
rectangle it materializes is the bounding box of stored cells: iter_rows calls
Worksheet.cell for every coordinate from A1 through that box.

Run from this directory:

    python3 -m unittest test_recalc_unbounded_range.py
"""

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

SCRIPT_DIR = Path(__file__).resolve().parent
EXCEL_MAX_ROW = 1048576
EXCEL_MAX_COL = 16384  # XFD
EXTERNAL_FORMULA = "='[1]Other'!A1"

# Virtual address space for the post-recalc walks. LibreOffice's recalc of this
# fixture fits; the full A1:XFD1048576 cell walk does not.
POST_ADDRESS_SPACE = 2 * 1024**3
EXTERNAL_ADDRESS_SPACE = 512 * 1024**2


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_workbook(path, formula, far_cell):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = 1
    sheet["B1"] = formula
    if far_cell:
        sheet.cell(row=EXCEL_MAX_ROW, column=EXCEL_MAX_COL, value=1)
    workbook.save(path)
    workbook.close()


def _add_external_link_part(path):
    temporary = path.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(
            "xl/externalLinks/externalLink1.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
        )
    temporary.replace(path)


def _sheet_xml(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


def _assert_sparse_full_sheet(testcase, path):
    xml = _sheet_xml(path)
    testcase.assertIn('ref="A1:XFD1048576"', xml)
    # A dense sheet would store a cell element per coordinate. This fixture
    # stores the input, the formula, and the far marker only.
    testcase.assertLessEqual(xml.count("<c "), 5)
    testcase.assertIn('r="XFD1048576"', xml)


def _run_worker(mode, path, address_space, timeout):
    """Run recalc in a child so an unbounded allocation cannot exhaust this process."""
    env = os.environ.copy()
    env["RECALC_TEST_ADDRESS_SPACE"] = str(address_space)
    env["RECALC_TEST_MODE"] = mode
    env["RECALC_TEST_PATH"] = str(path)
    env["PYTHONPATH"] = str(SCRIPT_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    command = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    process = subprocess.Popen(
        command,
        cwd=str(SCRIPT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return process.returncode, stdout, stderr


# Phrases a fix may use when it refuses an oversized used range before walking.
SIZE_REJECTION_MARKERS = (
    "dimension",
    "used range",
    "oversized",
    "too large",
    "exceeds",
    "max_row",
    "max_column",
    "row limit",
    "column limit",
)


def _is_size_rejection(error_text):
    lowered = error_text.lower()
    return any(marker in lowered for marker in SIZE_REJECTION_MARKERS)


class RecalcUnboundedRangeTest(unittest.TestCase):
    def test_small_workbook_recalculates(self):
        """A sheet whose stored cells are a small rectangle still recalculates."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.xlsx"
            _write_workbook(path, "=A1+1", far_cell=False)
            code, stdout, stderr = _run_worker(
                "post", path, POST_ADDRESS_SPACE, timeout=30
            )
        self.assertEqual(
            code,
            0,
            f"small workbook recalc failed\nstdout:\n{stdout}\nstderr:\n{stderr}",
        )
        self.assertIn("total_formulas=1", stdout)
        self.assertNotIn("allocation_failure", stdout)

    def test_external_link_scan_does_not_materialize_full_sheet(self):
        """The pre-LibreOffice external-link walk must not fill A1:XFD1048576.

        recalc() reaches this walk whenever the archive contains an
        xl/externalLinks part and --force is absent. The file must be unchanged:
        refusing the sheet, or reporting the stored external formula, both
        happen before LibreOffice rewrites it.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "external.xlsx"
            _write_workbook(path, EXTERNAL_FORMULA, far_cell=True)
            _add_external_link_part(path)
            _assert_sparse_full_sheet(self, path)
            before = _sha256(path)
            code, stdout, stderr = _run_worker(
                "external", path, EXTERNAL_ADDRESS_SPACE, timeout=30
            )
            after = _sha256(path)
        self.assertEqual(
            before,
            after,
            "external-link inspection rewrote the workbook before it finished",
        )
        self.assertEqual(code, 0, f"stdout:\n{stdout}\nstderr:\n{stderr}")
        self.assertNotIn("allocation_failure", stdout, stdout + stderr)
        self.assertTrue(
            "Sheet1!B1" in stdout or "rejected_oversized_range" in stdout,
            f"scan neither reported the stored external formula nor rejected the range\n{stdout}",
        )

    def test_post_recalc_scans_do_not_materialize_full_sheet(self):
        """Both walks after LibreOffice returns must not fill A1:XFD1048576.

        Those are the cached-error scan and the formula count. LibreOffice keeps
        the cell at XFD1048576, so the used range is still the full sheet when
        the walks start. An oversized-range rejection must happen before the
        file is rewritten.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "post.xlsx"
            _write_workbook(path, "=A1+1", far_cell=True)
            _assert_sparse_full_sheet(self, path)
            before = _sha256(path)
            code, stdout, stderr = _run_worker(
                "post", path, POST_ADDRESS_SPACE, timeout=30
            )
            after = _sha256(path)
        self.assertEqual(code, 0, f"stdout:\n{stdout}\nstderr:\n{stderr}")
        self.assertNotIn("allocation_failure", stdout, stdout + stderr)
        if "rejected_oversized_range" in stdout:
            self.assertEqual(
                before,
                after,
                "oversized range was rejected after the workbook was rewritten",
            )
        else:
            self.assertIn("total_formulas=1", stdout)


def _worker():
    """Child entry. Prints a single status line and exits 0 only for a bounded result."""
    sys.path.insert(0, str(SCRIPT_DIR))
    import resource

    import recalc

    address_space = int(os.environ["RECALC_TEST_ADDRESS_SPACE"])
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    mode = os.environ["RECALC_TEST_MODE"]
    path = os.environ["RECALC_TEST_PATH"]

    try:
        if mode == "external":
            result = recalc.recalc(path, timeout=20, force=False)
        elif mode == "post":
            result = recalc.recalc(path, timeout=20, force=True)
        else:
            print(f"unknown mode {mode}")
            return 1
    except MemoryError:
        print("allocation_failure")
        return 0

    if not isinstance(result, dict):
        print(f"unexpected result {result!r}")
        return 1

    error = result.get("error", None)
    if "status" not in result:
        error_text = "" if error is None else str(error)
        if error_text.strip() == "" or "MemoryError" in error_text:
            print("allocation_failure")
            return 0
        if error_text.rstrip().endswith("for external links:"):
            print("allocation_failure")
            return 0
        if mode == "external" and "Sheet1!B1" in result.get("external_link_cells", []):
            print("reported_stored_formula")
            return 0
        # A deliberate refusal before the walk. The parent checks the file
        # was not rewritten. LibreOffice failures are not that refusal.
        if _is_size_rejection(error_text):
            print("rejected_oversized_range")
            return 0
        print(f"unexpected error {error_text}")
        return 1

    formulas = result.get("total_formulas")
    print(f"total_formulas={formulas}")
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        sys.exit(_worker())
    unittest.main()
