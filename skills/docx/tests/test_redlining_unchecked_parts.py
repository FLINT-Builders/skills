"""Redline validation must reject untracked edits outside the main document body.

The docx skill tells the agent to run `validate.py` with `--author` and
`--original` so every edit is checked. These tests drive that command. They
fail while headers, footers, footnotes, and endnotes are omitted from the
comparison, and they pass once an untracked edit in any of those parts is
reported and a tracked edit there is still accepted.

The same check is copied in the pptx and xlsx office scripts and is selected
for both `.docx` and `.dotx`. Each case runs every copy.

Comments (`word/comments.xml`) and glossary documents are separate parts the
check also does not read. They are not covered here.
"""

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[3]
OFFICES = (
    REPO / "skills/docx/scripts/office/validate.py",
    REPO / "skills/pptx/scripts/office/validate.py",
    REPO / "skills/xlsx/scripts/office/validate.py",
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"

AUTHOR = "Reviewer"
WHEN = "2026-01-15T12:00:00Z"

DEFAULT_PARTS = {
    "header1": "Header price 100",
    "header2": "Second header schedule A",
    "footer1": "Footer confidential",
    "footer2": "Second footer page two",
    "footnote": "Footnote liability cap",
    "endnote": "Endnote governing law",
}
BODY = "The purchase price is one hundred dollars."


def _decl(body: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body


def _run(text: str) -> str:
    return f"<w:r><w:t>{escape(text)}</w:t></w:r>"


def _para(inner: str) -> str:
    return f"<w:p>{inner}</w:p>"


def _ins(text: str, wid: int) -> str:
    return (
        f'<w:ins w:id="{wid}" w:author="{AUTHOR}" w:date="{WHEN}">'
        f"{_run(text)}</w:ins>"
    )


def _tracked_body() -> str:
    return _run(BODY) + _ins("Plus tax.", 1)


def _pack(path: Path, parts: dict) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)


def _document(body_runs: str) -> str:
    return _decl(
        f'''<w:document xmlns:w="{W}" xmlns:r="{R}">
  <w:body>
    <w:p>{body_runs}</w:p>
    <w:p>
      <w:r><w:t>See the notes.</w:t></w:r>
      <w:r><w:footnoteReference w:id="1"/></w:r>
      <w:r><w:endnoteReference w:id="1"/></w:r>
    </w:p>
    <w:p>
      <w:pPr>
        <w:sectPr>
          <w:headerReference w:type="default" r:id="rIdHeader1"/>
          <w:footerReference w:type="default" r:id="rIdFooter1"/>
        </w:sectPr>
      </w:pPr>
    </w:p>
    <w:p><w:r><w:t>Schedule section.</w:t></w:r></w:p>
    <w:sectPr>
      <w:headerReference w:type="default" r:id="rIdHeader2"/>
      <w:footerReference w:type="default" r:id="rIdFooter2"/>
    </w:sectPr>
  </w:body>
</w:document>'''
    )


def _story_part(tag: str, inner: str) -> str:
    return _decl(f'<w:{tag} xmlns:w="{W}" xmlns:r="{R}">{inner}</w:{tag}>')


def _package(body_runs: str, texts: dict, extras: dict | None = None) -> dict:
    extras = extras or {}
    header1 = _para(_run(texts["header1"])) + extras.get("header1", "")
    header2 = _para(_run(texts["header2"])) + extras.get("header2", "")
    footer1 = extras.get("footer1_replace", _para(_run(texts["footer1"])))
    footnote = _para(_run(texts["footnote"]))
    return {
        "[Content_Types].xml": _decl(
            f'''<Types xmlns="{CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/header2.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/word/footer2.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
  <Override PartName="/word/endnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"/>
</Types>'''
        ),
        "_rels/.rels": _decl(
            f'''<Relationships xmlns="{REL}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
        ),
        "word/_rels/document.xml.rels": _decl(
            f'''<Relationships xmlns="{REL}">
  <Relationship Id="rIdHeader1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rIdHeader2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header2.xml"/>
  <Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
  <Relationship Id="rIdFooter2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer2.xml"/>
  <Relationship Id="rIdFootnotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
  <Relationship Id="rIdEndnotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes" Target="endnotes.xml"/>
</Relationships>'''
        ),
        "word/document.xml": _document(body_runs),
        "word/header1.xml": _story_part("hdr", header1),
        "word/header2.xml": _story_part("hdr", header2),
        "word/footer1.xml": _story_part("ftr", footer1),
        "word/footer2.xml": _story_part("ftr", _para(_run(texts["footer2"]))),
        "word/footnotes.xml": _decl(
            f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1">{footnote}</w:footnote></w:footnotes>'
        ),
        "word/endnotes.xml": _decl(
            f'<w:endnotes xmlns:w="{W}"><w:endnote w:id="1">{_para(_run(texts["endnote"]))}</w:endnote></w:endnotes>'
        ),
    }


def _pair(modified_body: str, modified_texts: dict, original_body: str | None = None,
          original_texts: dict | None = None, modified_extras: dict | None = None,
          original_extras: dict | None = None):
    original_body = _run(BODY) if original_body is None else original_body
    original_texts = dict(DEFAULT_PARTS) if original_texts is None else original_texts
    return (
        _package(original_body, original_texts, original_extras),
        _package(modified_body, modified_texts, modified_extras),
    )


class RedliningUncheckedPartsTest(unittest.TestCase):
    def _run_validator(self, validate_py: Path, original: Path, modified: Path):
        proc = subprocess.run(
            [
                sys.executable,
                str(validate_py),
                str(modified),
                "--original",
                str(original),
                "--author",
                AUTHOR,
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def _assert_rejected(self, original_parts: dict, modified_parts: dict, token: str,
                         suffix: str = ".docx"):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            original = tmp / f"original{suffix}"
            modified = tmp / f"modified{suffix}"
            _pack(original, original_parts)
            _pack(modified, modified_parts)
            for validate_py in OFFICES:
                with self.subTest(validator=validate_py.parents[2].name, suffix=suffix):
                    code, output = self._run_validator(validate_py, original, modified)
                    self.assertNotEqual(
                        code,
                        0,
                        f"{validate_py} reported success while {token} changed "
                        f"without a tracked change:\n{output}",
                    )
                    self.assertNotIn("All validations PASSED!", output)
                    self.assertIn("Document text doesn't match", output)
                    self.assertIn(token, output)

    def _assert_accepted(self, original_parts: dict, modified_parts: dict):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            original = tmp / "original.docx"
            modified = tmp / "modified.docx"
            _pack(original, original_parts)
            _pack(modified, modified_parts)
            for validate_py in OFFICES:
                with self.subTest(validator=validate_py.parents[2].name):
                    code, output = self._run_validator(validate_py, original, modified)
                    self.assertEqual(
                        code,
                        0,
                        f"{validate_py} rejected a document whose edits are tracked:\n{output}",
                    )
                    self.assertIn("All validations PASSED!", output)

    def test_untracked_default_header_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["header1"] = "Header price HIDDENPRICE99999"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENPRICE99999")

    def test_untracked_header_is_rejected_when_body_is_unchanged(self):
        texts = dict(DEFAULT_PARTS)
        texts["header1"] = "Header price HIDDENPRICE99999"
        original, modified = _pair(_run(BODY), texts)
        self._assert_rejected(original, modified, "HIDDENPRICE99999")

    def test_untracked_second_section_header_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["header2"] = "Second header HIDDENHEADER2"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENHEADER2")

    def test_untracked_default_footer_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["footer1"] = "Footer HIDDENFOOTER1"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENFOOTER1")

    def test_untracked_second_section_footer_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["footer2"] = "Second footer HIDDENFOOTER2"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENFOOTER2")

    def test_untracked_footnote_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["footnote"] = "Footnote HIDDENNOTE"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENNOTE")

    def test_untracked_endnote_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["endnote"] = "Endnote HIDDENEND"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENEND")

    def test_untracked_header_table_cell_is_rejected(self):
        table = (
            "<w:tbl><w:tblPr><w:tblW w:w=\"5000\" w:type=\"dxa\"/></w:tblPr>"
            "<w:tblGrid><w:gridCol w:w=\"5000\"/></w:tblGrid><w:tr><w:tc>"
            "<w:p><w:r><w:t>HEADERCELLTOKEN</w:t></w:r></w:p>"
            "</w:tc></w:tr></w:tbl>"
        )
        original, modified = _pair(
            _tracked_body(),
            dict(DEFAULT_PARTS),
            modified_extras={"header1": table},
        )
        self._assert_rejected(original, modified, "HEADERCELLTOKEN")

    def test_deleted_header_text_is_rejected(self):
        original_texts = dict(DEFAULT_PARTS)
        original_texts["header1"] = "HEADERDELETE"
        original, modified = _pair(
            _tracked_body(),
            dict(DEFAULT_PARTS),
            original_texts=original_texts,
        )
        modified["word/header1.xml"] = _story_part("hdr", "<w:p/>")
        self._assert_rejected(original, modified, "HEADERDELETE")

    def test_untracked_header_edit_beside_an_older_tracked_change_is_rejected(self):
        older = (
            '<w:ins w:id="3" w:author="Earlier" w:date="2025-06-01T00:00:00Z">'
            f"{_run('Oldnote')}</w:ins>"
        )
        texts = dict(DEFAULT_PARTS)
        texts["header1"] = "Header price HIDDENPRICE99999"
        original, modified = _pair(_tracked_body(), texts)
        original["word/header1.xml"] = _story_part(
            "hdr", _para(_run(DEFAULT_PARTS["header1"]) + older)
        )
        modified["word/header1.xml"] = _story_part(
            "hdr", _para(_run(texts["header1"]) + older)
        )
        self._assert_rejected(original, modified, "HIDDENPRICE99999")

    def test_untracked_header_in_a_dotx_template_is_rejected(self):
        texts = dict(DEFAULT_PARTS)
        texts["header1"] = "Header price HIDDENPRICE99999"
        original, modified = _pair(_tracked_body(), texts)
        self._assert_rejected(original, modified, "HIDDENPRICE99999", suffix=".dotx")

    def test_tracked_body_edit_alone_passes(self):
        original, modified = _pair(_tracked_body(), dict(DEFAULT_PARTS))
        self._assert_accepted(original, modified)

    def test_untracked_body_edit_is_rejected(self):
        original, modified = _pair(
            _run("The purchase price is BODYUNTRACKED dollars.") + _ins("Plus tax.", 1),
            dict(DEFAULT_PARTS),
        )
        self._assert_rejected(original, modified, "BODYUNTRACKED")

    def test_tracked_header_insertion_passes(self):
        original, modified = _pair(_run(BODY), dict(DEFAULT_PARTS))
        modified["word/header1.xml"] = _story_part(
            "hdr",
            _para(_run(DEFAULT_PARTS["header1"]) + _ins("Trackedok", 7)),
        )
        self._assert_accepted(original, modified)

    def test_tracked_footer_deletion_passes(self):
        deleted = (
            f'<w:del w:id="8" w:author="{AUTHOR}" w:date="{WHEN}">'
            f"<w:r><w:delText>{escape(DEFAULT_PARTS['footer1'])}</w:delText></w:r>"
            "</w:del>"
        )
        original, modified = _pair(
            _run(BODY),
            dict(DEFAULT_PARTS),
            modified_extras={"footer1_replace": _para(deleted)},
        )
        self._assert_accepted(original, modified)


if __name__ == "__main__":
    unittest.main()
