"""Per-part re-extraction of --original in the Office validator.

The pptx, docx, and xlsx skills each ship a copy of
``validators/base.py``. ``validate_file_against_xsd`` calls
``_get_original_file_errors`` for every XML or ``.rels`` part that fails
schema validation, and that helper unpacks the entire ``--original``
archive into a fresh temporary directory. The number of file extractions
therefore grows with the product of the failing-part count and the
archive size.

These tests drive ``validate.py`` the way the skills do
(``validate.py <file> --original <template>``). They fail while each
failing part unpacks the original again. A fix should unpack that
archive once per validator run (or cache its per-part XSD errors) in
every copy of ``validators/base.py``, and should still:

* suppress schema errors the original already has, and
* report schema errors the original does not have.

``validate.py`` does not schema-check xlsx-family packages (it exits
before constructing a validator), so these tests reach the xlsx skill's
copy by validating pptx and docx packages with that copy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PARTS = 8
# One pass for the cached XSD comparison, plus one existing single pass
# (slide-defect baseline on pptx, paragraph count on docx).
MAX_ORIGINAL_OPENS = 2

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
OFFICE_DOCUMENT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
EXTRA_PART = "junk/_rels/only-in-output.rels"

SKILLS = ("pptx", "docx", "xlsx")
FAMILIES = ("pptx", "docx")


def _bad_rels(index: int) -> str:
    # Relationship is missing the required Type and Target attributes
    # (opc-relationships.xsd, CT_Relationship).
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{REL_NS}">\n'
        f'  <Relationship Id="rId{index}"/>\n'
        "</Relationships>\n"
    )


def _content_types(family: str) -> str:
    if family == "pptx":
        part = "/ppt/presentation.xml"
        content_type = (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml"
        )
    else:
        part = "/word/document.xml"
        content_type = (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{CT_NS}">\n'
        '  <Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        f'  <Override PartName="{part}" ContentType="{content_type}"/>\n'
        "</Types>\n"
    )


def _main_part(family: str) -> tuple[str, str]:
    if family == "pptx":
        path = "ppt/presentation.xml"
        body = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:presentation xmlns:p="{PML_NS}" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
            "</p:presentation>\n"
        )
    else:
        path = "word/document.xml"
        body = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:document xmlns:w="{WORD_NS}"><w:body><w:p><w:r><w:t>'
            "Hello</w:t></w:r></w:p></w:body></w:document>\n"
        )
    return path, body


def _root_rels(main_part: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{REL_NS}">\n'
        f'  <Relationship Id="rId1" Type="{OFFICE_DOCUMENT}" Target="{main_part}"/>\n'
        "</Relationships>\n"
    )


def build_package(path: Path, family: str, *, extra_invalid_part: bool) -> None:
    main_part, main_xml = _main_part(family)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", _content_types(family))
        archive.writestr(main_part, main_xml)
        archive.writestr("_rels/.rels", _root_rels(main_part))
        for index in range(PARTS):
            archive.writestr(f"junk/_rels/bad{index}.rels", _bad_rels(index))
        if extra_invalid_part:
            archive.writestr(EXTRA_PART, _bad_rels(10_000))


def run_validate(
    tmp_path: Path, skill: str, family: str, *, extra_invalid_part: bool
) -> dict:
    suffix = "." + family
    original = tmp_path / f"template{suffix}"
    edited = tmp_path / f"out{suffix}"
    build_package(original, family, extra_invalid_part=False)
    build_package(edited, family, extra_invalid_part=extra_invalid_part)
    result_path = tmp_path / "result.json"
    office = REPO_ROOT / "skills" / skill / "scripts" / "office"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            str(office),
            str(edited),
            str(original),
            str(result_path),
        ],
        cwd=tmp_path,
        env={**os.environ, "OFFICE_REEXTRACT_CHILD": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    if not result_path.is_file():
        raise AssertionError(
            "validator child produced no result\n"
            f"exit={completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    stats = json.loads(result_path.read_text())
    stats["stdout"] = completed.stdout
    stats["stderr"] = completed.stderr
    stats["entries"] = len(zipfile.ZipFile(original).namelist())
    return stats


def _assert_original_unpacked_once(stats: dict) -> None:
    opens = stats["original_opens"]
    reads = stats["original_member_reads"]
    entries = stats["entries"]
    assert opens <= MAX_ORIGINAL_OPENS, (
        f"opened the original archive {opens} times "
        f"({PARTS} schema-invalid .rels parts); "
        f"expected at most {MAX_ORIGINAL_OPENS} opens per validator run"
    )
    assert reads <= MAX_ORIGINAL_OPENS * entries, (
        f"read {reads} members from the original "
        f"({entries} entries, {opens} opens); "
        "a full unpack once per failing part still blows up with the part count"
    )


@pytest.mark.parametrize("skill", SKILLS)
@pytest.mark.parametrize("family", FAMILIES)
def test_matching_template_errors_do_not_reextract_original(
    tmp_path: Path, skill: str, family: str
) -> None:
    stats = run_validate(tmp_path, skill, family, extra_invalid_part=False)
    _assert_original_unpacked_once(stats)
    assert stats["exit_code"] == 0, stats["stdout"] + stats["stderr"]
    assert "NEW validation errors" not in stats["stdout"]
    assert "All validations PASSED!" in stats["stdout"]


@pytest.mark.parametrize("skill", SKILLS)
@pytest.mark.parametrize("family", FAMILIES)
def test_new_schema_error_is_reported_without_reextracting_original(
    tmp_path: Path, skill: str, family: str
) -> None:
    stats = run_validate(tmp_path, skill, family, extra_invalid_part=True)
    _assert_original_unpacked_once(stats)
    assert stats["exit_code"] == 1, stats["stdout"] + stats["stderr"]
    assert "NEW validation errors" in stats["stdout"]
    assert EXTRA_PART in stats["stdout"]


def _child(argv: list[str]) -> int:
    office, edited, original, result_path = map(Path, argv)
    sys.path.insert(0, str(office))

    import zipfile as zipfile_mod

    original_resolved = original.resolve()
    state = {"opens": 0, "reads": 0}
    orig_init = zipfile_mod.ZipFile.__init__
    orig_open = zipfile_mod.ZipFile.open

    def _init(self, file, *args, **kwargs):
        orig_init(self, file, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode != "r" or not self.filename:
            return
        try:
            same = Path(self.filename).resolve() == original_resolved
        except OSError:
            return
        if same:
            state["opens"] += 1

    def _open(self, name, mode="r", pwd=None, *, force_zip64=False):
        if mode == "r" and self.filename:
            try:
                same = Path(self.filename).resolve() == original_resolved
            except OSError:
                same = False
            if same:
                state["reads"] += 1
        return orig_open(self, name, mode, pwd, force_zip64=force_zip64)

    zipfile_mod.ZipFile.__init__ = _init
    zipfile_mod.ZipFile.open = _open

    import validate

    sys.argv = ["validate.py", str(edited), "--original", str(original)]
    exit_code = 0
    try:
        validate.main()
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    finally:
        result_path.write_text(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "original_opens": state["opens"],
                    "original_member_reads": state["reads"],
                }
            )
        )
    return exit_code


if __name__ == "__main__":
    if os.environ.get("OFFICE_REEXTRACT_CHILD") == "1":
        raise SystemExit(_child(sys.argv[1:]))
