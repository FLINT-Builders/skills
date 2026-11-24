"""Spec for the uncapped workbook decompression in scripts/recalc.py.

These tests fail while the vulnerability is present. They pass when
recalculation rejects a caller-supplied .xlsx before it decompresses the
offending parts, on every load_workbook path:

- external_links_at_risk (the two loads before LibreOffice)
- the two loads after a successful LibreOffice save, including --force,
  which skips the external-link check

Budgets a fix must enforce from zip metadata (ZipInfo.file_size and
ZipInfo.compress_size), before any entry payload is decompressed:

- MAX_ZIP_ENTRIES = 128
- MAX_TOTAL_UNCOMPRESSED_BYTES = 8 MiB (sum of declared uncompressed sizes)
- MAX_COMPRESSION_RATIO = 100 (declared uncompressed / compressed size,
  per entry; compress_size == 0 with file_size > 0 is over budget)

Rejection text, on every over-budget path, must contain the substring
"exceeds workbook limit". external_links_at_risk must raise. recalc must
return {"error": "...exceeds workbook limit..."} and no "status".
A rejection must decompress at most MAX_REJECT_INFLATED_BYTES of entry
payload (64 KiB total). Looking the file up again and then loading it
still fails these tests: the cap is on bytes decompressed during the call,
not on whether a check ran first.

A workbook within all three budgets must not be rejected for this limit.
The benign fixture is a normal small package: a handful of parts, well
under 8 MiB, no part with a declared ratio above 100.

Not covered here, and still unbounded if this fix only touches recalc.py:
skills/xlsx/scripts/office/validate.py extracts a packed .xlsx through
safe_extract before it notices xlsx has no schema check. The docx and pptx
copies of that helper share safe_extract. SKILL.md also tells the agent to
call load_workbook and pandas.read_excel itself; those calls are not code
in this repository.
"""

import json
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import office.soffice as soffice_mod
import recalc as recalc_mod

MAX_ZIP_ENTRIES = 128
MAX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_REJECT_INFLATED_BYTES = 64 * 1024

NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

LIMIT_TEXT = "exceeds workbook limit"

KINDS = (
    "shared_strings_ratio",
    "worksheet_ratio",
    "stored_uncompressed",
    "entry_count",
    "declared_size",
)


def _sheet(shared=True, inline=None):
    if inline is None:
        value = '<c r="A1" t="s"><v>0</v></c>' if shared else '<c r="A1"><v>1</v></c>'
    else:
        value = f'<c r="A1" t="inlineStr"><is><t>{inline}</t></is></c>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{NS_MAIN}"><sheetData><row r="1">{value}'
        "<c r=\"B1\"><f>A1&amp;A1</f></c></row></sheetData></worksheet>"
    )


def _shared(text):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{NS_MAIN}" count="1" uniqueCount="1"><si><t>{text}</t></si></sst>'
    )


def _package(shared_text, sheet_xml):
    return {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""",
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_DOC_REL}">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": sheet_xml,
        "xl/sharedStrings.xml": _shared(shared_text),
        "xl/externalLinks/externalLink1.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<externalLink xmlns="{NS_MAIN}"/>'
        ),
    }


def _write_zip(path, parts, compress, extra_entries=0):
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in parts.items():
            archive.writestr(name, text.encode(), compress_type=compress)
        for index in range(extra_entries):
            archive.writestr(
                f"xl/externalLinks/pad{index}.xml",
                b"<pad/>",
                compress_type=zipfile.ZIP_STORED,
            )


def _declare_uncompressed(path, member, declared):
    """Raise one entry's declared uncompressed size without adding payload.

    Python's zip reader stops at the real deflate stream, so this does not
    allocate `declared` bytes. A metadata budget still has to reject it.
    """
    data = bytearray(path.read_bytes())
    name = member.encode()
    start = 0
    found = 0
    while True:
        offset = data.find(b"PK\x01\x02", start)
        if offset < 0:
            break
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, offset + 28)
        filename = bytes(data[offset + 46 : offset + 46 + name_len])
        if filename == name:
            struct.pack_into("<I", data, offset + 24, declared)
            local = struct.unpack_from("<I", data, offset + 42)[0]
            if data[local : local + 4] != b"PK\x03\x04":
                raise AssertionError("central directory offset is not a local header")
            struct.pack_into("<I", data, local + 22, declared)
            found += 1
        start = offset + 46 + name_len + extra_len + comment_len
    if found != 1:
        raise AssertionError(f"patched {found} headers for {member}")
    path.write_bytes(data)


def build_workbook(directory, kind):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{kind}.xlsx"
    extra = 0
    compress = zipfile.ZIP_DEFLATED
    shared = "hello"
    inline = None
    if kind == "shared_strings_ratio":
        shared = "A" * 1_500_000
    elif kind == "worksheet_ratio":
        inline = "B" * 1_500_000
    elif kind == "stored_uncompressed":
        shared = "C" * (MAX_TOTAL_UNCOMPRESSED_BYTES + 65_536)
        compress = zipfile.ZIP_STORED
    elif kind == "entry_count":
        extra = MAX_ZIP_ENTRIES
    elif kind == "declared_size":
        shared = "D" * 64
    elif kind == "benign":
        pass
    else:
        raise AssertionError(kind)
    _write_zip(path, _package(shared, _sheet(inline=inline)), compress, extra_entries=extra)
    if kind == "declared_size":
        _declare_uncompressed(path, "xl/sharedStrings.xml", 100 * 1024 * 1024)
        with zipfile.ZipFile(path) as archive:
            actual = len(archive.read("xl/sharedStrings.xml"))
        if actual > 4096:
            raise AssertionError(f"declared-size fixture actually holds {actual} bytes")
    return path


def archive_budget_facts(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
    total = sum(info.file_size for info in infos)
    ratios = []
    for info in infos:
        if info.compress_size == 0:
            ratios.append(float("inf") if info.file_size else 1.0)
        else:
            ratios.append(info.file_size / info.compress_size)
    return {
        "entries": len(infos),
        "uncompressed": total,
        "max_ratio": max(ratios),
    }


def assert_fixture_is_over_budget(path, kind):
    facts = archive_budget_facts(path)
    over_entries = facts["entries"] > MAX_ZIP_ENTRIES
    over_size = facts["uncompressed"] > MAX_TOTAL_UNCOMPRESSED_BYTES
    over_ratio = facts["max_ratio"] > MAX_COMPRESSION_RATIO
    if kind == "entry_count":
        assert over_entries
        assert not over_size
        assert not over_ratio
    elif kind == "stored_uncompressed":
        assert over_size
        assert not over_entries
        assert not over_ratio
    elif kind in ("shared_strings_ratio", "worksheet_ratio"):
        assert over_ratio
        assert not over_entries
        assert not over_size
    elif kind == "declared_size":
        assert over_size and over_ratio
        assert not over_entries
    else:
        raise AssertionError(kind)
    return facts


@pytest.fixture
def inflated(monkeypatch):
    total = {"n": 0}
    original = zipfile.ZipExtFile.read

    def wrapped(self, n=-1):
        data = original(self, n)
        total["n"] += len(data)
        return data

    monkeypatch.setattr(zipfile.ZipExtFile, "read", wrapped)
    return total


def _assert_rejected_without_inflate(inflated):
    assert inflated["n"] <= MAX_REJECT_INFLATED_BYTES, (
        f"decompressed {inflated['n']} bytes before refusing"
    )


def _assert_recalc_error(result):
    assert "status" not in result
    assert LIMIT_TEXT in result.get("error", "")


def _patch_successful_soffice(monkeypatch, path):
    """Reach the post-save loads without running LibreOffice.

    _recalc_with_profile only calls load_workbook after the macro install
    reports success and subprocess.run returns 0 with a changed mtime or
    size. This stub is that success signal. The workbook bytes stay the
    attacker's, which is what those load_workbook calls then parse.
    """

    def install(profile_dir, timeout=30):
        return "file:///tmp/recalc-profile", None

    def run(cmd, **kwargs):
        stamp = path.stat().st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(recalc_mod, "setup_libreoffice_macro", install)
    monkeypatch.setattr(recalc_mod.subprocess, "run", run)


@pytest.mark.parametrize("kind", KINDS)
def test_external_links_reject_over_budget_archive_before_decompress(tmp_path, inflated, kind):
    path = build_workbook(tmp_path, kind)
    before = path.read_bytes()
    assert_fixture_is_over_budget(path, kind)
    caught = None
    try:
        recalc_mod.external_links_at_risk(path)
    except Exception as exc:
        caught = exc
    _assert_rejected_without_inflate(inflated)
    assert caught is not None and LIMIT_TEXT in str(caught)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", KINDS)
def test_recalc_rejects_over_budget_archive_before_soffice(tmp_path, inflated, monkeypatch, kind):
    path = build_workbook(tmp_path, kind)
    before = path.read_bytes()
    assert_fixture_is_over_budget(path, kind)
    started = {"soffice": False}

    def run(cmd, **kwargs):
        started["soffice"] = True
        raise FileNotFoundError(cmd)

    # The pre-save failure is raised by office.soffice.run_soffice, which
    # holds its own subprocess binding. The post-save command uses recalc's.
    monkeypatch.setattr(soffice_mod.subprocess, "run", run)
    monkeypatch.setattr(recalc_mod.subprocess, "run", run)
    result = recalc_mod.recalc(str(path), timeout=5, force=False)
    _assert_rejected_without_inflate(inflated)
    _assert_recalc_error(result)
    assert started["soffice"] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", KINDS)
def test_post_save_loads_reject_over_budget_archive_before_decompress(
    tmp_path, inflated, monkeypatch, kind
):
    path = build_workbook(tmp_path, kind)
    before = path.read_bytes()
    assert_fixture_is_over_budget(path, kind)
    _patch_successful_soffice(monkeypatch, path)
    result = recalc_mod.recalc(str(path), timeout=5, force=True)
    _assert_rejected_without_inflate(inflated)
    _assert_recalc_error(result)
    assert path.read_bytes() == before


def test_benign_workbook_is_not_rejected_as_over_budget(tmp_path):
    path = build_workbook(tmp_path, "benign")
    facts = archive_budget_facts(path)
    assert facts["entries"] <= MAX_ZIP_ENTRIES
    assert facts["uncompressed"] <= MAX_TOTAL_UNCOMPRESSED_BYTES
    assert facts["max_ratio"] <= MAX_COMPRESSION_RATIO
    at_risk = recalc_mod.external_links_at_risk(path)
    assert isinstance(at_risk, list)
    result = recalc_mod.recalc(str(path), timeout=5, force=False)
    assert LIMIT_TEXT not in result.get("error", "")


def test_cli_rejects_over_budget_workbook(tmp_path):
    path = build_workbook(tmp_path, "shared_strings_ratio")
    before = path.read_bytes()
    script = SCRIPTS / "recalc.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(path), "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert path.read_bytes() == before
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "status" not in payload
    assert LIMIT_TEXT in payload["error"]
