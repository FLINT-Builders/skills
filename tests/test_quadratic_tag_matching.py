"""Quadratic tag scans in the presentation validator must finish quickly.

The pptx, docx, and xlsx copies of the office validator each load every
ppt/charts/chartN.xml and every master relationship part and search the raw
text for a closing tag. Opening tags hidden in a comment, a CDATA section, or
a processing instruction are still visible to those expressions, and so are
unclosed p:sldIdLst tags in a CDATA section of ppt/presentation.xml. With no
closing tag, each opening tag walks the rest of the part.

These tests build a small packed deck (the repeated text compresses to a few
kilobytes) and run that skill's validate.py, the same entry point the pptx
skill tells an agent to run. They fail while the scan is quadratic. They pass
when the scan finishes within the time limit and still reports a real defect
that sits outside the hidden text: a chart with no axis references, a stacked
bar label position PowerPoint rejects, or two masters sharing one theme.

Run:
    python -m unittest tests.test_quadratic_tag_matching -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("pptx", "docx", "xlsx")

# 40_000 unclosed tags is a few hundred kilobytes of XML and tens of seconds
# for the current close-tag search. A linear parse of the same part, or a
# search that ignores comments, CDATA, and processing instructions, finishes
# well under the limit on a slow machine.
REPEATS = 40_000
TIMEOUT_SECONDS = 5

CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
THEME_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"

LINE_DEFECT = "<c:lineChart></c:lineChart>"
BAR_DEFECT = (
    '<c:barChart><c:grouping val="stacked"/>'
    '<c:dLblPos val="outEnd"/></c:barChart>'
)
LINE_MESSAGE = "<c:lineChart> declares no <c:axId>"
BAR_MESSAGE = 'dLblPos="outEnd" on a stacked barChart'

MASTER_FILE = {
    "slideMasters": "slideMaster",
    "notesMasters": "notesMaster",
    "handoutMasters": "handoutMaster",
}


def _hide(payload: str, wrapper: str) -> str:
    if wrapper == "comment":
        return f"<!--{payload}-->"
    if wrapper == "cdata":
        return f"<![CDATA[{payload}]]>"
    if wrapper == "pi":
        return f"<?note {payload}?>"
    raise ValueError(wrapper)


def _content_types() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="{CT_NS}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>
"""


def _presentation(extra: str = "") -> str:
    # sldSz follows sldIdLst, so a notes-master theme share stays live.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="{PML_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  {extra}
  <p:sldMasterIdLst/>
  <p:sldIdLst/>
  <p:sldSz cx="9144000" cy="5143500"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""


def _base_parts(presentation: str | None = None) -> dict[str, str]:
    return {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG_NS}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>
""",
        "ppt/presentation.xml": presentation if presentation is not None else _presentation(),
    }


def _chart_xml(hidden_tag: str, wrapper: str, real: str, repeats: int) -> str:
    hidden = _hide(f"<{hidden_tag}>" * repeats, wrapper)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="{CHART_NS}">{hidden}{real}</c:chartSpace>
"""


def _theme_relationship() -> str:
    return (
        f'<Relationship Id="rId1" Type="{THEME_TYPE}" '
        'Target="../theme/theme1.xml"/>'
    )


def _rels_xml(wrapper: str | None, repeats: int) -> str:
    hidden = ""
    if wrapper is not None:
        hidden = _hide("<Relationship>" * repeats, wrapper)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG_NS}">{hidden}{_theme_relationship()}</Relationships>
"""


def _add_shared_masters(
    parts: dict[str, str], group: str, wrapper: str | None, repeats: int
) -> None:
    stem = MASTER_FILE[group]
    for number in (1, 2):
        parts[f"ppt/{group}/{stem}{number}.xml"] = (
            f'<p:{stem} xmlns:p="{PML_NS}"/>'
        )
    parts[f"ppt/{group}/_rels/{stem}1.xml.rels"] = _rels_xml(wrapper, repeats)
    parts[f"ppt/{group}/_rels/{stem}2.xml.rels"] = _rels_xml(None, 0)
    parts["ppt/theme/theme1.xml"] = '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>'
    parts["[Content_Types].xml"] = _content_types().replace(
        "</Types>",
        f'  <Override PartName="/ppt/{group}/{stem}1.xml" '
        'ContentType="application/xml"/>\n'
        f'  <Override PartName="/ppt/{group}/{stem}2.xml" '
        'ContentType="application/xml"/>\n'
        "</Types>",
    )


def _share_message(group: str) -> str:
    stem = MASTER_FILE[group]
    return (
        f"ppt/{group}/{stem}2.xml shares ppt/theme/theme1.xml "
        f"with ppt/{group}/{stem}1.xml"
    )


def chart_case(hidden_tag: str, wrapper: str, real: str, repeats: int) -> dict[str, str]:
    parts = _base_parts()
    # The chart part is not referenced by any relationship. The validator
    # still globs ppt/charts/chartN.xml and checks it.
    parts["ppt/charts/chart1.xml"] = _chart_xml(hidden_tag, wrapper, real, repeats)
    return parts


def theme_case(group: str, wrapper: str, repeats: int) -> dict[str, str]:
    parts = _base_parts()
    _add_shared_masters(parts, group, wrapper, repeats)
    return parts


def sld_id_lst_case(repeats: int) -> dict[str, str]:
    bomb = _hide("<p:sldIdLst>" * repeats, "cdata")
    parts = _base_parts(_presentation(bomb))
    _add_shared_masters(parts, "slideMasters", None, 0)
    return parts


def _pack(parts: dict[str, str], suffix: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    path = Path(tmp.name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


class QuadraticTagMatchTests(unittest.TestCase):
    def _validate(self, skill: str, parts: dict[str, str], suffix: str) -> str:
        script = ROOT / "skills" / skill / "scripts" / "office" / "validate.py"
        deck = _pack(parts, suffix)
        try:
            try:
                completed = subprocess.run(
                    [sys.executable, str(script), str(deck)],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail(
                    f"{skill} {script.name} was still running after "
                    f"{TIMEOUT_SECONDS}s on {deck.name}"
                )
        finally:
            deck.unlink(missing_ok=True)
        output = completed.stdout + completed.stderr
        self.assertIn(
            completed.returncode,
            (0, 1),
            f"{skill} validate.py exited {completed.returncode}:\n{output}",
        )
        return output

    def _expect(self, skill: str, parts: dict[str, str], message: str, suffix: str = ".pptx") -> None:
        output = self._validate(skill, parts, suffix)
        self.assertIn(message, output, output)


def _bind_cases() -> None:
    cases: list[tuple[str, dict[str, str] | None, str]] = []

    def add(name: str, parts: dict[str, str], message: str) -> None:
        cases.append((name, parts, message))

    add(
        "bar_comment_still_reports_line_chart",
        chart_case("c:barChart", "comment", LINE_DEFECT, REPEATS),
        LINE_MESSAGE,
    )
    add(
        "bar3d_comment_still_reports_line_chart",
        chart_case("c:bar3DChart", "comment", LINE_DEFECT, REPEATS),
        LINE_MESSAGE,
    )
    add(
        "line_comment_still_reports_stacked_bar",
        chart_case("c:lineChart", "comment", BAR_DEFECT, REPEATS),
        BAR_MESSAGE,
    )
    add(
        "bar_cdata_still_reports_line_chart",
        chart_case("c:barChart", "cdata", LINE_DEFECT, REPEATS),
        LINE_MESSAGE,
    )
    add(
        "bar_pi_still_reports_line_chart",
        chart_case("c:barChart", "pi", LINE_DEFECT, REPEATS),
        LINE_MESSAGE,
    )
    add(
        "line_cdata_still_reports_stacked_bar",
        chart_case("c:lineChart", "cdata", BAR_DEFECT, REPEATS),
        BAR_MESSAGE,
    )
    add(
        "line_pi_still_reports_stacked_bar",
        chart_case("c:lineChart", "pi", BAR_DEFECT, REPEATS),
        BAR_MESSAGE,
    )
    for group, wrapper in (
        ("slideMasters", "comment"),
        ("slideMasters", "cdata"),
        ("slideMasters", "pi"),
        ("notesMasters", "comment"),
        ("handoutMasters", "comment"),
    ):
        add(
            f"{group}_{wrapper}_still_reports_shared_theme",
            theme_case(group, wrapper, REPEATS),
            _share_message(group),
        )
    add(
        "presentation_cdata_sldidlst_still_reports_shared_theme",
        sld_id_lst_case(REPEATS),
        _share_message("slideMasters"),
    )

    for skill in SKILLS:
        for name, parts, message in cases:
            def make(skill: str, parts: dict[str, str], message: str, suffix: str = ".pptx"):
                def test(self: QuadraticTagMatchTests) -> None:
                    self._expect(skill, parts, message, suffix)

                return test

            setattr(
                QuadraticTagMatchTests,
                f"test_{skill}_{name}",
                make(skill, parts, message),
            )

        def make_control_chart(skill: str):
            def test(self: QuadraticTagMatchTests) -> None:
                self._expect(
                    skill,
                    chart_case("c:barChart", "comment", LINE_DEFECT, 0),
                    LINE_MESSAGE,
                )

            return test

        def make_control_theme(skill: str):
            def test(self: QuadraticTagMatchTests) -> None:
                self._expect(
                    skill,
                    theme_case("slideMasters", "comment", 0),
                    _share_message("slideMasters"),
                )

            return test

        setattr(
            QuadraticTagMatchTests,
            f"test_{skill}_control_chart_defect_is_reported",
            make_control_chart(skill),
        )
        setattr(
            QuadraticTagMatchTests,
            f"test_{skill}_control_shared_theme_is_reported",
            make_control_theme(skill),
        )

    def test_potx(self: QuadraticTagMatchTests) -> None:
        self._expect(
            "pptx",
            chart_case("c:barChart", "comment", LINE_DEFECT, REPEATS),
            LINE_MESSAGE,
            ".potx",
        )

    setattr(QuadraticTagMatchTests, "test_pptx_potx_bar_comment_still_reports_line_chart", test_potx)


_bind_cases()


if __name__ == "__main__":
    unittest.main()
