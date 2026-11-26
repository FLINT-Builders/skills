"""
Excel Formula Recalculation Script
Recalculates all formulas in an Excel file using LibreOffice
"""

import contextlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

from office.soffice import get_soffice_env, run_soffice

from openpyxl import load_workbook
from openpyxl.packaging.relationship import get_dependents, get_rels_path
from openpyxl.reader.excel import ExcelReader
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.xml.constants import COMMENTS_NS, SHEET_MAIN_NS
from openpyxl.xml.functions import iterparse, localname

MACRO_FILENAME = "Module1.xba"
SOFFICE_MISSING = "soffice not found on PATH; LibreOffice is required to recalculate"

MAX_LOCATIONS = 100

# A1 to the furthest cell, per sheet: 16 full-height columns or 1,024 full-width rows.
# Anything that walks a sheet with iter_rows() or pandas visits this whole rectangle.
MAX_USED_RANGE_CELLS = 16_777_216
# openpyxl creates a cell object (~330 bytes) for every coordinate a merged range,
# hyperlink range, or comment range covers while it loads a sheet.
MAX_RANGE_CELLS = 250_000
SHEET_RANGE_TAGS = frozenset({"mergeCell", "hyperlink"})
COMMENT_RANGE_TAGS = frozenset({"comment"})
ROW_TAG = f"{{{SHEET_MAIN_NS}}}row"
CELL_TAG = f"{{{SHEET_MAIN_NS}}}c"

EXTERNAL_REF_RE = re.compile(r"""(?<![\w"\[])'?\[\d+\][^!"\[\]]*'?!""")

RECALCULATE_MACRO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" script:language="StarBasic">
    Sub RecalculateAndSave()
      ThisComponent.calculateAll()
      ThisComponent.store()
      ThisComponent.close(True)
    End Sub
</script:module>"""


def has_gtimeout():
    try:
        subprocess.run(
            ["gtimeout", "--version"], capture_output=True, timeout=1, check=False
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _stamp(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def setup_libreoffice_macro(profile_dir: Path, timeout=30):
    url = profile_dir.as_uri()
    try:
        run_soffice(
            ["--headless", "--terminate_after_init", f"-env:UserInstallation={url}"],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, SOFFICE_MISSING
    except subprocess.TimeoutExpired:
        return None, "LibreOffice timed out creating its profile; formulas were NOT recalculated"

    macro_dir = profile_dir / "user" / "basic" / "Standard"
    if not macro_dir.exists():
        return None, "LibreOffice did not create a usable profile; formulas were NOT recalculated"

    try:
        (macro_dir / MACRO_FILENAME).write_text(RECALCULATE_MACRO)
    except OSError as e:
        return None, f"Could not install the recalculation macro: {e}"

    return url, None


class OversizedRangeError(ValueError):
    pass


def _ref_bounds(ref):
    """Return (min_col, min_row, max_col, max_row) as openpyxl parses ref, or None if it cannot."""
    try:
        bounds = range_boundaries(ref)
    except (TypeError, ValueError):
        return None
    if all(b is None for b in bounds):
        return None
    return bounds


def _row_number(row, previous):
    r = row.get("r")
    if r is None:
        return previous + 1
    try:
        return int(r)
    except ValueError:
        value = float(r)
        if not value.is_integer():
            raise
        return int(value)


class _SheetUsage:
    def __init__(self, workbook_usage, sheet_name):
        self.workbook = workbook_usage
        self.sheet_name = sheet_name
        self.max_col = 0
        self.max_row = 0
        self.whole_refs = []

    def add_cell(self, row, col, source=None):
        self.max_row = max(self.max_row, row)
        self.max_col = max(self.max_col, col)
        if self.max_row * self.max_col > MAX_USED_RANGE_CELLS:
            source = source or f"row {row}, column {col}"
            raise OversizedRangeError(
                f"Refusing to load the workbook: the used range of sheet '{self.sheet_name}' "
                f"reaches {source}, which exceeds the limit of {MAX_USED_RANGE_CELLS} cells "
                "(A1 to its furthest cell). Delete the stray cells far from the data."
            )

    def add_dimension(self, ref):
        bounds = _ref_bounds(ref)
        if bounds is None:
            return
        min_col, min_row, max_col, max_row = bounds
        if None in bounds:
            max_col, max_row = math.inf, math.inf
        self.add_cell(max_row, max_col, f"dimension {ref}")

    def _add_range_cells(self, cells, ref):
        if cells <= 1:
            return
        self.workbook.range_cells += cells
        if self.workbook.range_cells > MAX_RANGE_CELLS:
            raise OversizedRangeError(
                f"Refusing to load the workbook: merged cells, hyperlinks, and comments "
                f"(sheet '{self.sheet_name}', range {ref}) span more than {MAX_RANGE_CELLS} "
                "cells, which exceeds the limit. Unmerge or shrink those ranges."
            )

    def add_range(self, ref, tag):
        bounds = _ref_bounds(ref)
        if bounds is None:
            return
        min_col, min_row, max_col, max_row = bounds
        if None in bounds:
            # openpyxl cannot load a merged whole row or column, so it creates no cells for one.
            # Hyperlinks and comments on one expand to the sheet's furthest cell; see finish().
            if tag != "mergeCell":
                self.whole_refs.append((bounds, ref))
                self.add_cell(max_row or 0, max_col or 0, ref)
            return
        self._add_range_cells((abs(max_col - min_col) + 1) * (abs(max_row - min_row) + 1), ref)
        self.add_cell(max(min_row, max_row), max(min_col, max_col), ref)

    def finish(self):
        for (min_col, min_row, max_col, max_row), ref in self.whole_refs:
            if min_row is None:
                cells = (abs(max_col - min_col) + 1) * self.max_row
            else:
                cells = (abs(max_row - min_row) + 1) * self.max_col
            self._add_range_cells(cells, ref)


class _WorkbookUsage:
    def __init__(self):
        self.range_cells = 0


def _scan_part(source, usage, range_tags):
    """Bound every cell openpyxl's WorkSheetParser and WorksheetReader would create."""
    stack = []
    row_counter = 0
    for event, element in iterparse(source, events=("start", "end")):
        if event == "start":
            stack.append(element)
            continue
        stack.pop()
        tag = element.tag
        if tag == ROW_TAG:
            row_counter = _row_number(element, row_counter)
            col_counter = 0
            for child in element:
                coordinate = child.get("r")
                if coordinate and child.tag == CELL_TAG:
                    row, col_counter = coordinate_to_tuple(coordinate)
                else:
                    # openpyxl may have cleared this child's attributes before reading it,
                    # so take the larger of the two positions it could land on.
                    col_counter += 1
                    row = row_counter
                    if coordinate:
                        with contextlib.suppress(TypeError, ValueError):
                            r_row, r_col = coordinate_to_tuple(coordinate)
                            row = max(row, r_row)
                            col_counter = max(col_counter, r_col)
                usage.add_cell(row, col_counter)
            element.clear()
        else:
            name = localname(element)
            ref = element.get("ref")
            if ref is not None and name in range_tags:
                usage.add_range(ref, name)
            elif ref is not None and name == "dimension":
                usage.add_dimension(ref)
        if stack and stack[-1].tag != ROW_TAG:
            stack[-1].remove(element)


def check_workbook_limits(data):
    """Raise OversizedRangeError if loading data would make openpyxl create too many cells.

    Finds worksheets and their comments the way openpyxl's ExcelReader does.
    """
    reader = ExcelReader(BytesIO(data), read_only=True)
    try:
        reader.read_manifest()
        reader.read_workbook()
        workbook_usage = _WorkbookUsage()
        for sheet, rel in reader.parser.find_sheets():
            if rel.target not in reader.valid_files or "chartsheet" in rel.Type:
                continue
            usage = _SheetUsage(workbook_usage, sheet.name)
            parts = [(rel.target, SHEET_RANGE_TAGS)]
            rels_path = get_rels_path(rel.target)
            if rels_path in reader.valid_files:
                for comments in get_dependents(reader.archive, rels_path).find(COMMENTS_NS):
                    parts.append((comments.target, COMMENT_RANGE_TAGS))
            for part, range_tags in parts:
                if part in reader.valid_files:
                    with reader.archive.open(part) as source:
                        _scan_part(source, usage, range_tags)
            usage.finish()
    finally:
        reader.archive.close()


def read_checked_workbook(path):
    """Return the file's bytes after check_workbook_limits; load only from these bytes."""
    data = Path(path).read_bytes()
    check_workbook_limits(data)
    return data


def _load(data, data_only):
    return load_workbook(BytesIO(data), data_only=data_only)


def _stored_cells(ws):
    # iter_rows() creates a Cell for every coordinate in the bounding box of the stored
    # cells, so a single far-away cell makes it walk billions of empty ones.
    cells = getattr(ws, "_cells", None)
    if cells is None:
        return []
    return [cells[key] for key in sorted(cells)]


def external_links_at_risk(filename):
    try:
        data = Path(filename).read_bytes()
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return []
    if not any(n.startswith("xl/externalLinks/") for n in names):
        return []

    check_workbook_limits(data)
    with contextlib.ExitStack() as stack:
        formulas = _load(data, data_only=False)
        stack.callback(formulas.close)
        values = _load(data, data_only=True)
        stack.callback(values.close)

        external_names = [
            name
            for name, dn in formulas.defined_names.items()
            if isinstance(getattr(dn, "value", None), str) and EXTERNAL_REF_RE.search(dn.value)
        ]
        name_re = (
            re.compile(r"\b(" + "|".join(re.escape(n) for n in external_names) + r")\b")
            if external_names
            else None
        )

        at_risk = []
        for sheet in formulas.sheetnames:
            ws = formulas[sheet]
            cached_cells = getattr(values[sheet], "_cells", {})
            for cell in _stored_cells(ws):
                v = cell.value
                if not (isinstance(v, str) and v.startswith("=")):
                    continue
                reaches_out = EXTERNAL_REF_RE.search(v) or (name_re and name_re.search(v))
                cached = cached_cells.get((cell.row, cell.column))
                if reaches_out and (cached is None or cached.value is None):
                    at_risk.append(f"{sheet}!{cell.coordinate}")
        return at_risk


def recalc(filename, timeout=30, force=False):
    if not Path(filename).exists():
        return {"error": f"File {filename} does not exist"}

    abs_path = str(Path(filename).absolute())

    if not os.access(abs_path, os.W_OK):
        return {"error": f"{filename} is not writable; recalculation rewrites the file in place"}

    try:
        get_soffice_env()
    except Exception as e:  
        return {"error": f"Could not prepare the LibreOffice environment: {e}"}

    try:
        check_workbook_limits(Path(abs_path).read_bytes())
    except OversizedRangeError as e:
        return {"error": str(e)}
    except Exception:
        # Not a workbook openpyxl can read; the load after LibreOffice checks again.
        pass

    if not force:
        try:
            at_risk = external_links_at_risk(filename)
        except OversizedRangeError as e:
            return {"error": str(e)}
        except Exception as e:  
            return {"error": f"Could not inspect {filename} for external links: {e}"}
        if at_risk:
            shown = at_risk[:MAX_LOCATIONS]
            return {
                "error": (
                    "Refusing to recalculate: this workbook links to another workbook, and "
                    f"{len(at_risk)} linked cell(s) have lost their cached value (openpyxl strips "
                    "these on save). Recalculating would resolve them to #NAME? and delete the "
                    "external links for good. Copy those cells' values from the original file "
                    "before saving, or pass --force to accept the loss. Charts and conditional "
                    "formats can hold external references too, so this list may not be exhaustive."
                ),
                "external_link_cells": shown,
                "external_link_cells_truncated": max(0, len(at_risk) - len(shown)),
            }

    with tempfile.TemporaryDirectory(
        prefix="recalc-lo-profile-", ignore_cleanup_errors=True
    ) as profile_dir:
        return _recalc_with_profile(filename, abs_path, timeout, Path(profile_dir))


def _recalc_with_profile(filename, abs_path, timeout, profile_dir: Path):
    started = time.monotonic()
    profile_url, err = setup_libreoffice_macro(profile_dir, timeout=timeout)
    if err:
        return {"error": err}

    timeout = max(5, int(timeout - (time.monotonic() - started)))

    before = _stamp(abs_path)

    cmd = [
        "soffice",
        "--headless",
        "--norestore",
        f"-env:UserInstallation={profile_url}",
        "vnd.sun.star.script:Standard.Module1.RecalculateAndSave?language=Basic&location=application",
        abs_path,
    ]

    if platform.system() == "Linux" and shutil.which("timeout"):
        cmd = ["timeout", str(timeout)] + cmd
    elif platform.system() == "Darwin" and has_gtimeout():
        cmd = ["gtimeout", str(timeout)] + cmd

    timed_out = f"LibreOffice timed out after {timeout}s; formulas were NOT recalculated. Re-run with a longer timeout."

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=get_soffice_env(), timeout=timeout + 15
        )
    except subprocess.TimeoutExpired:
        return {"error": timed_out}
    except FileNotFoundError:
        return {"error": SOFFICE_MISSING}

    if result.returncode == 124:
        return {"error": timed_out}

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"soffice exited {result.returncode}"
        return {"error": f"LibreOffice failed to recalculate: {detail}"}

    if _stamp(abs_path) == before:
        return {
            "error": (
                "LibreOffice exited cleanly but never rewrote the file, so nothing was "
                "recalculated. Check that no other LibreOffice instance is running, then retry."
            )
        }

    try:
        data = read_checked_workbook(abs_path)
        wb = _load(data, data_only=True)

        excel_errors = [
            "#VALUE!",
            "#DIV/0!",
            "#REF!",
            "#NAME?",
            "#NULL!",
            "#NUM!",
            "#N/A",
        ]
        error_details = {err: [] for err in excel_errors}
        total_errors = 0

        for sheet_name in wb.sheetnames:
            for cell in _stored_cells(wb[sheet_name]):
                if cell.value is not None and isinstance(cell.value, str):
                    for err in excel_errors:
                        if err in cell.value:
                            location = f"{sheet_name}!{cell.coordinate}"
                            error_details[err].append(location)
                            total_errors += 1
                            break

        result = {
            "status": "success" if total_errors == 0 else "errors_found",
            "total_errors": total_errors,
            "error_summary": {},
        }

        for err_type, locations in error_details.items():
            if locations:
                entry = {"count": len(locations), "locations": locations[:MAX_LOCATIONS]}
                if len(locations) > MAX_LOCATIONS:
                    entry["locations_truncated"] = len(locations) - MAX_LOCATIONS
                result["error_summary"][err_type] = entry

        wb.close()

        wb_formulas = _load(data, data_only=False)
        formula_count = 0
        for sheet_name in wb_formulas.sheetnames:
            for cell in _stored_cells(wb_formulas[sheet_name]):
                if (
                    cell.value
                    and isinstance(cell.value, str)
                    and cell.value.startswith("=")
                ):
                    formula_count += 1
        wb_formulas.close()

        result["total_formulas"] = formula_count

        return result

    except Exception as e:
        return {"error": str(e)}


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]

    if not args:
        print("Usage: python recalc.py <excel_file> [timeout_seconds] [--force]")
        print("\nRecalculates all formulas in an Excel file using LibreOffice")
        print("\nReturns JSON with error details:")
        print("  - status: 'success' or 'errors_found'")
        print("  - total_errors: Total number of Excel errors found")
        print("  - total_formulas: Number of formulas in the file")
        print("  - error_summary: Breakdown by error type with locations")
        print("    - #VALUE!, #DIV/0!, #REF!, #NAME?, #NULL!, #NUM!, #N/A")
        print("\nOn any failure the JSON has an 'error' key and no 'status'.")
        print("--force recalculates even when it would destroy external links.")
        sys.exit(1)

    filename = args[0]
    timeout = int(args[1]) if len(args) > 1 else 30

    result = recalc(filename, timeout, force=force)
    print(json.dumps(result, indent=2))
    sys.exit(1 if "error" in result else 0)


if __name__ == "__main__":
    main()
