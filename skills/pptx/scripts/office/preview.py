"""
Render a Word document or slide deck to JPEG page images for visual review.

Usage:
    python scripts/office/preview.py output.docx page               # page-1.jpg, page-2.jpg, ...
    python scripts/office/preview.py output.pptx slide --dpi 150
    python scripts/office/preview.py report.docx page --pages 31-45  # page-31.jpg ... page-45.jpg

LibreOffice opens the file once; a macro measures that loaded document's laid-out
pages and exports a PDF from it only when the requested pages fit the limits
below. The PDF is checked again with pdfinfo before pdftoppm rasterizes exactly
those pages. A file over any limit exits non-zero before a PDF or image exists.

Prints the absolute path of every image written, one per line.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

try:
    from .soffice import get_soffice_env, run_soffice
except ImportError:
    from soffice import get_soffice_env, run_soffice


MAX_PAGES = 30
MAX_PAGE_EDGE_IN = 22
MAX_DPI = 150
DEFAULT_DPI = 100
SOFFICE_TIMEOUT = 120

_HMM_PER_INCH = 2540
_PT_PER_INCH = 72

_MACRO_BASIC = """
Sub ExportPreview()
    Dim doc As Object
    doc = ThisComponent
    On Error Goto Failed

    Dim maxPages As Long, maxEdge As Long, first As Long, last As Long
    maxPages = CLng(Environ("OFFICE_PREVIEW_MAX_PAGES"))
    maxEdge = CLng(Environ("OFFICE_PREVIEW_MAX_EDGE"))
    first = CLng(Environ("OFFICE_PREVIEW_FIRST"))
    last = CLng(Environ("OFFICE_PREVIEW_LAST"))

    Dim filter As String
    If doc.supportsService("com.sun.star.text.TextDocument") Then
        filter = "writer_pdf_Export"
    ElseIf doc.supportsService("com.sun.star.presentation.PresentationDocument") Then
        filter = "impress_pdf_Export"
    Else
        Finish(doc, "REFUSED only Word documents and slide decks can be previewed")
        Exit Sub
    End If

    ' Writer lays out every page only when given a RenderDevice; without one it
    ' counts just the pages formatted so far.
    Dim renderOpts(0) As New com.sun.star.beans.PropertyValue
    renderOpts(0).Name = "RenderDevice"
    renderOpts(0).Value = CreateUnoService("com.sun.star.awt.Toolkit").createScreenCompatibleDevice(1, 1)
    Dim total As Long
    total = doc.getRendererCount(doc, renderOpts())

    If last = 0 Then
        first = 1
        last = total
        If total > maxPages Then
            Finish(doc, "REFUSED the file has " & total & " pages; previews render at most " & maxPages & " pages at a time (preview.py --pages FIRST-LAST renders a range)")
            Exit Sub
        End If
    End If
    If total < 1 Then
        Finish(doc, "REFUSED the file has no pages")
        Exit Sub
    End If
    If last > total Then
        Finish(doc, "REFUSED the file has only " & total & " pages")
        Exit Sub
    End If
    If first < 1 Or last < first Or last - first + 1 > maxPages Then
        Finish(doc, "REFUSED previews render at most " & maxPages & " pages at a time")
        Exit Sub
    End If

    Dim p As Long, i As Integer, w As Long, h As Long, props
    For p = first To last
        w = 0
        h = 0
        props = doc.getRenderer(p - 1, doc, renderOpts())
        For i = LBound(props) To UBound(props)
            If props(i).Name = "PageSize" Then
                w = props(i).Value.Width
                h = props(i).Value.Height
            End If
        Next
        If w <= 0 Or h <= 0 Or w > maxEdge Or h > maxEdge Then
            Finish(doc, "REFUSED page " & p & " is " & Format(w / 2540, "0.0") & " x " & Format(h / 2540, "0.0") & " in; previews allow at most " & Format(maxEdge / 2540, "0") & " in per side")
            Exit Sub
        End If
    Next

    Dim filterData(0) As New com.sun.star.beans.PropertyValue
    filterData(0).Name = "PageRange"
    filterData(0).Value = first & "-" & last
    Dim args(1) As New com.sun.star.beans.PropertyValue
    args(0).Name = "FilterName"
    args(0).Value = filter
    args(1).Name = "FilterData"
    args(1).Value = filterData()
    doc.storeToURL(ConvertToURL(Environ("OFFICE_PREVIEW_PDF")), args())
    Finish(doc, "OK " & first & " " & last)
    Exit Sub

Failed:
    Finish(doc, "ERROR " & Error$)
End Sub

Sub Finish(doc As Object, status As String)
    On Error Resume Next
    Dim n As Integer
    n = FreeFile()
    Open Environ("OFFICE_PREVIEW_STATUS") For Output As #n
    Print #n, status
    Close #n
    doc.close(True)
End Sub
"""

_MACRO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">\n'
    '<script:module xmlns:script="http://openoffice.org/2000/script" '
    'script:name="Module1" script:language="StarBasic">'
    f"{escape(_MACRO_BASIC)}</script:module>"
)
_MACRO_URL = "vnd.sun.star.script:Standard.Module1.ExportPreview?language=Basic&location=application"

_PDF_PAGES = re.compile(r"^Pages:\s+(\d+)\s*$", re.MULTILINE)
_PDF_PAGE_SIZE = re.compile(r"^Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts", re.MULTILINE)


class PreviewRefused(Exception):
    pass


def parse_pages(spec: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", spec)
    if not match:
        raise PreviewRefused(f"--pages must look like 3 or 31-45, not {spec!r}")
    first = int(match.group(1))
    last = int(match.group(2) or first)
    return first, last


def render_pages(
    input_path,
    output_prefix,
    dpi: int = DEFAULT_DPI,
    pages: tuple[int, int] | None = None,
    timeout: int = SOFFICE_TIMEOUT,
) -> list[Path]:
    """Rasterize the file's pages to ``<output_prefix>-<page>.jpg``.

    Raises PreviewRefused, having written nothing, when the file has more than
    MAX_PAGES pages (or ``pages`` spans more), a page edge exceeds
    MAX_PAGE_EDGE_IN, or dpi exceeds MAX_DPI.
    """
    source = Path(input_path).absolute()
    if not source.is_file():
        raise PreviewRefused(f"{input_path} is not a file")
    if not 1 <= dpi <= MAX_DPI:
        raise PreviewRefused(f"--dpi must be between 1 and {MAX_DPI}")
    first, last = pages or (0, 0)
    if pages is not None and (first < 1 or last < first or last - first + 1 > MAX_PAGES):
        raise PreviewRefused(
            f"--pages must be a range of 1 to {MAX_PAGES} pages starting at page 1 or later"
        )

    prefix = Path(output_prefix).absolute()
    workdir = Path(tempfile.mkdtemp(prefix="office_preview_"))
    try:
        pdf = workdir / "preview.pdf"
        status = workdir / "status.txt"
        first, last = _export_pdf(source, pdf, status, workdir / "profile", first, last, timeout)
        count = _check_pdf(pdf, last - first + 1)
        return _rasterize(pdf, prefix, first, count, dpi)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _export_pdf(
    source: Path, pdf: Path, status: Path, profile: Path, first: int, last: int, timeout: int
) -> tuple[int, int]:
    profile_url = profile.as_uri()
    try:
        run_soffice(
            ["--headless", "--terminate_after_init", f"-env:UserInstallation={profile_url}"],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise PreviewRefused("soffice not found on PATH; LibreOffice is required for previews")
    except subprocess.TimeoutExpired:
        raise PreviewRefused("LibreOffice timed out creating its profile")
    macro_dir = profile / "user" / "basic" / "Standard"
    if not macro_dir.is_dir():
        raise PreviewRefused("LibreOffice did not create a usable profile")
    (macro_dir / "Module1.xba").write_text(_MACRO, encoding="utf-8")

    env = get_soffice_env()
    env.update(
        OFFICE_PREVIEW_MAX_PAGES=str(MAX_PAGES),
        OFFICE_PREVIEW_MAX_EDGE=str(MAX_PAGE_EDGE_IN * _HMM_PER_INCH),
        OFFICE_PREVIEW_FIRST=str(first),
        OFFICE_PREVIEW_LAST=str(last),
        OFFICE_PREVIEW_PDF=str(pdf),
        OFFICE_PREVIEW_STATUS=str(status),
    )
    cmd = [
        "soffice",
        "--headless",
        "--norestore",
        f"-env:UserInstallation={profile_url}",
        _MACRO_URL,
        str(source),
    ]
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise PreviewRefused(f"LibreOffice did not finish laying out the file within {timeout}s")

    result = status.read_text(encoding="utf-8").strip() if status.is_file() else ""
    if result.startswith("REFUSED "):
        pdf.unlink(missing_ok=True)
        raise PreviewRefused(result[len("REFUSED "):])
    if not result.startswith("OK ") or not pdf.is_file():
        pdf.unlink(missing_ok=True)
        detail = result or (stderr or "").strip() or f"soffice exited {proc.returncode}"
        raise PreviewRefused(f"LibreOffice could not render the file: {detail}")
    exported_first, exported_last = (int(n) for n in result.split()[1:3])
    return exported_first, exported_last


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def _check_pdf(pdf: Path, expected: int) -> int:
    info = subprocess.run(
        ["pdfinfo", "-f", "1", "-l", str(MAX_PAGES + 1), str(pdf)],
        capture_output=True,
        text=True,
    )
    pages = _PDF_PAGES.search(info.stdout or "")
    if info.returncode != 0 or not pages:
        raise PreviewRefused("pdfinfo could not read the exported PDF")
    count = int(pages.group(1))
    if count > min(expected, MAX_PAGES):
        raise PreviewRefused(f"the exported PDF has {count} pages, expected at most {expected}")
    max_pt = MAX_PAGE_EDGE_IN * _PT_PER_INCH + 1
    sizes = _PDF_PAGE_SIZE.findall(info.stdout)
    if len(sizes) != count:
        raise PreviewRefused("pdfinfo did not report every page size of the exported PDF")
    for number, width, height in sizes:
        if float(width) > max_pt or float(height) > max_pt:
            raise PreviewRefused(
                f"exported page {number} is {float(width) / _PT_PER_INCH:.1f} x "
                f"{float(height) / _PT_PER_INCH:.1f} in; previews allow at most "
                f"{MAX_PAGE_EDGE_IN} in per side"
            )
    return count


def _rasterize(pdf: Path, prefix: Path, first: int, count: int, dpi: int) -> list[Path]:
    width = len(str(first + count - 1))
    written = []
    for index in range(1, count + 1):
        stem = prefix.parent / f"{prefix.name}-{first + index - 1:0{width}d}"
        result = subprocess.run(
            ["pdftoppm", "-jpeg", "-r", str(dpi), "-f", str(index), "-l", str(index),
             "-singlefile", str(pdf), str(stem)],
            capture_output=True,
            text=True,
        )
        image = stem.with_name(stem.name + ".jpg")
        if result.returncode != 0 or not image.is_file():
            for path in written:
                path.unlink(missing_ok=True)
            raise PreviewRefused(f"pdftoppm failed: {(result.stderr or '').strip()}")
        written.append(image)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a Word document or slide deck to JPEG page images."
    )
    parser.add_argument("input", help="File to preview (.docx, .pptx, or anything LibreOffice opens as one)")
    parser.add_argument("output_prefix", help="Images are written as <output_prefix>-<page>.jpg")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"Resolution (default {DEFAULT_DPI}, max {MAX_DPI})")
    parser.add_argument("--pages", help=f"Page range to render, e.g. 31-45 (at most {MAX_PAGES} pages)")
    args = parser.parse_args()

    try:
        pages = parse_pages(args.pages) if args.pages else None
        images = render_pages(args.input, args.output_prefix, dpi=args.dpi, pages=pages)
    except PreviewRefused as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    for image in images:
        print(image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
