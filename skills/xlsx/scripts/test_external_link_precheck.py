"""Bounds for the external-link pre-check in recalc.py.

Anyone who supplies a workbook controls the defined names and the cell text
that this pre-check inspects. The check runs from recalc() before LibreOffice
is started, so the timeout argument does not cover it.

These tests fail while that check compiles workbook-level defined names into
one regular-expression alternation and searches it against every cell string
that starts with '='. A fix has to finish the adversarial workbook well under
the budget below, and still report the same broken external references on a
normal workbook.
"""

import multiprocessing
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import recalc  # noqa: E402

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# One search of this shape takes many times BUDGET_SECONDS on the interpreter
# that ships with this environment (CPython 3.12). Loading the same file twice
# is a fraction of a second, which is what a linear scan has to stay near.
NAME_COUNT = 14000
WORD_COUNT = 500_000
BUDGET_SECONDS = 3.5

NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_WB = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_BOMB_PATH = None


def _content_types(shared_strings):
    shared = ""
    if shared_strings:
        shared = (
            '<Override PartName="/xl/sharedStrings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="{NS_CT}">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{shared}
</Types>"""


def _workbook_rels(shared_strings):
    shared = ""
    if shared_strings:
        shared = (
            f'<Relationship Id="rId2" Type="{NS_R}/sharedStrings" Target="sharedStrings.xml"/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{NS_REL}">
<Relationship Id="rId1" Type="{NS_R}/worksheet" Target="worksheets/sheet1.xml"/>
{shared}
<Relationship Id="rId3" Type="{NS_R}/styles" Target="styles.xml"/>
</Relationships>"""


def _styles():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{NS_WB}">
<fonts count="1"><font><sz val="11"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="1"><xf/></cellXfs>
</styleSheet>"""


def _write_xlsx(path, workbook_xml, sheet_xml, shared_xml=None, external_link=True):
    parts = {
        "[Content_Types].xml": _content_types(shared_xml is not None),
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/_rels/workbook.xml.rels": _workbook_rels(shared_xml is not None),
        "xl/styles.xml": _styles(),
        "xl/workbook.xml": workbook_xml,
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    if shared_xml is not None:
        parts["xl/sharedStrings.xml"] = shared_xml
    if external_link:
        # Presence of any part under this prefix turns the pre-check on.
        parts["xl/externalLinks/externalLink1.xml"] = "<externalLink/>"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def _workbook(defined_names_xml):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{NS_WB}" xmlns:r="{NS_R}">
<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets>
<definedNames>{defined_names_xml}</definedNames>
</workbook>"""


def _sheet(cells_xml):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{NS_WB}"><sheetData><row r="1">{cells_xml}</row></sheetData></worksheet>"""


def build_detection_workbook(path):
    """Small workbook used to lock the pre-check's results."""
    names = (
        '<definedName name="ExtSales">[1]Other!$B$2</definedName>'
        '<definedName name="Sum">[1]Other!$A$1</definedName>'
    )
    cells = (
        '<c r="A1"><f>ExtSales+1</f></c>'
        '<c r="B1"><f>1+1</f></c>'
        '<c r="C1"><f>[1]Other!A1</f></c>'
        '<c r="D1"><f>SumX+1</f></c>'
        '<c r="E1"><f>Sum+1</f></c>'
        '<c r="F1"><f>ExtSales+2</f><v>9</v></c>'
    )
    _write_xlsx(path, _workbook(names), _sheet(cells))


def build_unlinked_workbook(path):
    names = '<definedName name="ExtSales">[1]Other!$B$2</definedName>'
    cells = '<c r="A1"><f>ExtSales+1</f></c>'
    _write_xlsx(path, _workbook(names), _sheet(cells), external_link=False)


def build_adversarial_workbook(path):
    """Names n00001.. share only the letter n once the range crosses n10000.

    The cell text is '=' plus the token n repeated. None of those tokens is a
    defined name, so a search has to walk the whole text. The same text is
    stored as a real formula, a shared string, and an inline string.
    """
    names = "".join(
        f'<definedName name="n{i:05d}">[1]S!$A$1</definedName>'
        for i in range(1, NAME_COUNT + 1)
    )
    body = ("n " * WORD_COUNT).strip()
    text = "=" + body
    cells = (
        f'<c r="A1"><f>{body}</f></c>'
        '<c r="B1" t="s"><v>0</v></c>'
        f'<c r="C1" t="inlineStr"><is><t>{text}</t></is></c>'
    )
    shared = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{NS_WB}" count="1" uniqueCount="1"><si><t>{text}</t></si></sst>'
    )
    _write_xlsx(path, _workbook(names), _sheet(cells), shared_xml=shared)


def _worker_links(path, queue):
    warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
    queue.put(recalc.external_links_at_risk(path))


def _worker_recalc(path, queue):
    warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
    # The pre-check is the subject. LibreOffice must not be started for this
    # assertion, and a stub after the check must not hide a hang inside it.
    recalc._recalc_with_profile = lambda *args, **kwargs: {"status": "success"}
    queue.put(recalc.recalc(path, timeout=1, force=False))


def _finish_within(worker, path, budget):
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=worker, args=(str(path), queue))
    proc.start()
    proc.join(budget)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
        raise AssertionError(
            f"{worker.__name__} was still running after {budget} seconds"
        )
    if proc.exitcode != 0:
        raise AssertionError(f"{worker.__name__} exited {proc.exitcode}")
    return queue.get_nowait()


class ExternalLinkPrecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="recalc-precheck-")
        root = Path(cls._tmp.name)
        cls.detection = root / "detection.xlsx"
        cls.unlinked = root / "unlinked.xlsx"
        cls.bomb = root / "adversarial.xlsx"
        build_detection_workbook(cls.detection)
        build_unlinked_workbook(cls.unlinked)
        build_adversarial_workbook(cls.bomb)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_reports_uncached_external_names_and_direct_refs(self):
        found = recalc.external_links_at_risk(str(self.detection))
        self.assertEqual(sorted(found), ["S!A1", "S!C1", "S!E1"])

    def test_skips_workbooks_without_an_external_link_part(self):
        self.assertEqual(recalc.external_links_at_risk(str(self.unlinked)), [])

    def test_recalc_refuses_before_libreoffice_when_links_are_uncached(self):
        called = []

        def _stub(*args, **kwargs):
            called.append(True)
            return {"status": "success"}

        original = recalc._recalc_with_profile
        recalc._recalc_with_profile = _stub
        try:
            result = recalc.recalc(str(self.detection), timeout=1, force=False)
        finally:
            recalc._recalc_with_profile = original
        self.assertIn("error", result)
        self.assertIn("S!A1", result["external_link_cells"])
        self.assertEqual(called, [])

    def test_adversarial_defined_names_finish_within_budget(self):
        # Formula cell, shared string, and inline string all start with '='.
        _finish_within(_worker_links, self.bomb, BUDGET_SECONDS)

    def test_recalc_timeout_does_not_leave_the_precheck_unbounded(self):
        # timeout=1 is passed through. The pre-check still has to return,
        # because that argument only wraps the LibreOffice subprocess.
        _finish_within(_worker_recalc, self.bomb, BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
