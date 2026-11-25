"""
Write text that came from outside the workbook so it can never become a formula.

pandas and openpyxl store any string that starts with "=" as a live formula, and
xlsxwriter also turns "{=...}" into an array formula. CSV, TSV, JSON, scraped or
user-provided text must go through these helpers instead, so that it is stored
as a plain string cell. Only formulas you write yourself belong in a formula cell.

    from external_cells import write_external_dataframe, assign_external_value

    write_external_dataframe(df, "out.xlsx")            # replaces df.to_excel(...)
    assign_external_value(ws["B2"], row["note"])        # replaces ws["B2"] = ...
    append_external_row(ws, ["a", "=b", "c"])           # replaces ws.append(...)
"""

import os
import re
import tempfile
import zipfile

from openpyxl.cell.cell import Cell
from openpyxl.utils import column_index_from_string

FORMULA_TRIGGERS = ("=", "+", "-", "@", "{", "\t", "\r")

ENGINES = ("openpyxl", "xlsxwriter")

XLSXWRITER_TEXT_OPTIONS = {"strings_to_formulas": False, "strings_to_urls": False}

STRING_PARTS_RE = re.compile(r"^xl/(sharedStrings\.xml|worksheets/[^/]+\.xml)$")
# xlsxwriter writes a carriage return as _x000D_ (and a literal "_x000D_" as
# _x005F_x000D_); openpyxl does not decode that escape when it reads the file.
XLSXWRITER_CR_RE = re.compile(rb"(?<!_x005F)_x000D_")


def _keep_carriage_returns(path, engine):
    """Store carriage returns in cell text as &#13;, the form every reader keeps.

    A raw CR in XML text is read back as LF, so without this an imported
    "\\r=..." would come back changed.
    """
    with zipfile.ZipFile(path) as src:
        entries = [(info, src.read(info)) for info in src.infolist()]
    changed = False
    rewritten = []
    for info, data in entries:
        if STRING_PARTS_RE.match(info.filename):
            fixed = data.replace(b"\r", b"&#13;")
            if engine == "xlsxwriter":
                fixed = XLSXWRITER_CR_RE.sub(b"&#13;", fixed)
            changed = changed or fixed != data
            data = fixed
        rewritten.append((info, data))
    if not changed:
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as out, zipfile.ZipFile(out, "w") as dst:
            for info, data in rewritten:
                dst.writestr(info, data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _store_as_text(cell):
    if not isinstance(cell.value, str):
        return
    cell.data_type = "s"
    if cell.value.startswith(FORMULA_TRIGGERS):
        # Without quotePrefix, Excel re-parses the text as a formula the next
        # time someone edits the cell.
        cell.quotePrefix = True


def assign_external_value(cell, value):
    """Set an openpyxl cell to an outside value; strings are kept as text."""
    cell.value = value
    _store_as_text(cell)


def append_external_row(ws, values):
    """Worksheet.append for outside values; strings are kept as text."""
    if isinstance(values, dict):
        by_index = {
            column_index_from_string(k) if isinstance(k, str) else k: v
            for k, v in values.items()
        }
        width = max(by_index, default=0)
        values = [by_index.get(i) for i in range(1, width + 1)]
    cells = []
    for value in values:
        cell = Cell(ws)
        assign_external_value(cell, value)
        cells.append(cell)
    ws.append(cells)


def write_external_dataframe(df, path, engine=None, sheet_name="Sheet1", **to_excel_kwargs):
    """DataFrame.to_excel for outside data; string cells (headers too) stay text.

    Writes a new workbook. engine is "openpyxl" (default) or "xlsxwriter".
    Extra keyword arguments go to DataFrame.to_excel; index defaults to False.
    """
    import pandas as pd

    path = os.fspath(path)
    engine = engine or "openpyxl"
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, not {engine!r}")
    to_excel_kwargs.setdefault("index", False)

    if engine == "xlsxwriter":
        with pd.ExcelWriter(
            path, engine="xlsxwriter", engine_kwargs={"options": dict(XLSXWRITER_TEXT_OPTIONS)}
        ) as writer:
            df.to_excel(writer, sheet_name=sheet_name, **to_excel_kwargs)
    else:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet_name, **to_excel_kwargs)
            for row in writer.sheets[sheet_name].iter_rows():
                for cell in row:
                    _store_as_text(cell)
    _keep_carriage_returns(path, engine)
