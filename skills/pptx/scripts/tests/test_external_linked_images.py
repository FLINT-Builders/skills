"""External picture links must not be loaded during conversion or recalculation.

These tests fail while the product still hands a caller-supplied document to
LibreOffice without removing external picture relationships or disabling
remote and linked graphics. A passing run means thumbnail generation,
presentation and document PDF conversion, tracked-change acceptance, and
spreadsheet recalculation neither request those URLs nor open a linked
local image.

Run from the repository root:

    python3 skills/pptx/scripts/tests/test_external_linked_images.py
"""

import ctypes
import http.server
import io
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from PIL import Image


REPO = Path(__file__).resolve().parents[4]
PPTX_SKILL = REPO / "skills" / "pptx"
DOCX_SKILL = REPO / "skills" / "docx"
XLSX_SKILL = REPO / "skills" / "xlsx"

# Solid colors that survive JPEG quantization. Each predicate is unused by
# the other fixtures, so a hit means that image was painted into the output.
BLUE = (0, 0, 255)
MAGENTA = (255, 0, 170)
GREEN = (0, 255, 60)


def _is_blue(pixel):
    red, green, blue = pixel
    return blue > 180 and red < 80 and green < 80


def _is_magenta(pixel):
    red, green, blue = pixel
    return red > 200 and green < 70 and blue > 130


def _is_green(pixel):
    red, green, blue = pixel
    return green > 180 and red < 90 and blue < 110


def _png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _require_tool(name):
    if shutil.which(name) is None:
        raise AssertionError(f"{name} is not on PATH, so the conversion path did not run")


def _run(args, cwd, timeout=180):
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _jpeg_has(paths, predicate):
    found = False
    for path in paths:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if any(predicate(pixel) for pixel in rgb.getdata()):
                found = True
    return found


class _RequestLog(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _record(self):
        self.server.hits.append((self.command, self.path.split("?", 1)[0]))

    def _body(self):
        path = self.path.split("?", 1)[0]
        if path.endswith(".png"):
            return self.server.png, "image/png"
        return b"not-an-image", "text/plain"

    def do_GET(self):
        self._record()
        body, content_type = self._body()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self._record()
        body, content_type = self._body()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_OPTIONS(self):
        self._record()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._record()
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Server:
    def __init__(self, png):
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RequestLog)
        self._httpd.hits = []
        self._httpd.png = png
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def hits(self):
        return list(self._httpd.hits)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._httpd.shutdown()
        self._httpd.server_close()


class _FileOpenWatch:
    """Record open() of one file while a child process runs."""

    _IN_OPEN = 0x00000020
    _IN_NONBLOCK = 0x00000800

    def __init__(self, path):
        self.path = Path(path)
        self.opened = False
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self._libc = libc
        fd = libc.inotify_init1(self._IN_NONBLOCK)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        watch = libc.inotify_add_watch(fd, os.fsencode(self.path), self._IN_OPEN)
        if watch < 0:
            error = ctypes.get_errno()
            os.close(fd)
            raise OSError(error, "inotify_add_watch")
        self._fd = fd
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self._fd)

    def _loop(self):
        while not self._stop.is_set():
            readable, _, _ = select.select([self._fd], [], [], 0.1)
            if not readable:
                continue
            try:
                data = os.read(self._fd, 4096)
            except BlockingIOError:
                continue
            if data:
                self.opened = True


def _zip(path, parts):
    path = Path(path)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            if isinstance(payload, str):
                payload = payload.encode()
            archive.writestr(name, payload)


_THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme"><a:themeElements>
<a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F497D"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2><a:accent1><a:srgbClr val="4F81BD"/></a:accent1><a:accent2><a:srgbClr val="C0504D"/></a:accent2><a:accent3><a:srgbClr val="9BBB59"/></a:accent3><a:accent4><a:srgbClr val="8064A2"/></a:accent4><a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6><a:hlink><a:srgbClr val="0000FF"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>
</a:themeElements></a:theme>"""


def _picture(blip_attr, rid):
    return f"""<p:pic>
      <p:nvPicPr><p:cNvPr id="4" name="pic"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>
      <p:blipFill><a:blip {blip_attr}="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="457200" y="457200"/><a:ext cx="3657600" cy="3657600"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>"""


def _rels(relationships):
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for rel_id, rel_type, target, external in relationships:
        mode = ' TargetMode="External"' if external else ""
        lines.append(
            f'  <Relationship Id="{rel_id}" Type="{rel_type}" Target="{target}"{mode}/>'
        )
    lines.append("</Relationships>")
    return "\n".join(lines)


def _presentation_skeleton(slide_count, notes=False):
    slide_ids = "\n".join(
        f'    <p:sldId id="{256 + index}" r:id="rId{index + 2}"/>'
        for index in range(slide_count)
    )
    parts = {
        "ppt/presentation.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>
{slide_ids}
  </p:sldIdLst>
  <p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>""",
        "ppt/theme/theme1.xml": _THEME,
        "ppt/slideLayouts/slideLayout1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
  <p:cSld name="Blank"><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {layout_picture}
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>""".format(layout_picture=""),
        "ppt/slideMasters/slideMaster1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {master_picture}
  </p:spTree></p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
</p:sldMaster>""".format(master_picture=""),
    }
    pres_rels = [
        ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster", "slideMasters/slideMaster1.xml", False),
    ]
    for index in range(slide_count):
        pres_rels.append(
            (
                f"rId{index + 2}",
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
                f"slides/slide{index + 1}.xml",
                False,
            )
        )
    parts["ppt/_rels/presentation.xml.rels"] = _rels(pres_rels)
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout", "../slideLayouts/slideLayout1.xml", False),
            ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme", "../theme/theme1.xml", False),
        ]
    )
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster", "../slideMasters/slideMaster1.xml", False),
        ]
    )
    parts["_rels/.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", "ppt/presentation.xml", False),
        ]
    )
    return parts


def _content_types(parts):
    overrides = {
        "ppt/presentation.xml": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "ppt/slideMasters/slideMaster1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
        "ppt/slideLayouts/slideLayout1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
        "ppt/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml",
        "ppt/slides/slide1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        "ppt/slides/slide2.xml": "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        "ppt/slides/slide3.xml": "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        "ppt/notesSlides/notesSlide1.xml": "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml",
    }
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '  <Default Extension="xml" ContentType="application/xml"/>',
        '  <Default Extension="png" ContentType="image/png"/>',
    ]
    for name, content_type in overrides.items():
        if name in parts:
            lines.append(f'  <Override PartName="/{name}" ContentType="{content_type}"/>')
    lines.append("</Types>")
    return "\n".join(lines)


def write_control_pptx(path, png):
    parts = _presentation_skeleton(1)
    tree = """
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    """ + _picture("r:embed", "rId2")
    parts["ppt/slides/slide1.xml"] = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>{tree}</p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""
    parts["ppt/slides/_rels/slide1.xml.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout", "../slideLayouts/slideLayout1.xml", False),
            ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", "../media/image1.png", False),
        ]
    )
    parts["ppt/media/image1.png"] = png
    parts["[Content_Types].xml"] = _content_types(parts)
    _zip(path, parts)


def write_attack_pptx(path, urls, file_uri):
    parts = _presentation_skeleton(3)
    parts["ppt/slideMasters/slideMaster1.xml"] = parts["ppt/slideMasters/slideMaster1.xml"].replace(
        "</p:spTree>", _picture("r:link", "rId9") + "</p:spTree>", 1
    )
    parts["ppt/slideLayouts/slideLayout1.xml"] = parts["ppt/slideLayouts/slideLayout1.xml"].replace(
        "</p:spTree>", _picture("r:link", "rId8") + "</p:spTree>", 1
    )
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout", "../slideLayouts/slideLayout1.xml", False),
            ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme", "../theme/theme1.xml", False),
            ("rId9", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", urls["master"], True),
        ]
    )
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster", "../slideMasters/slideMaster1.xml", False),
            ("rId8", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", urls["layout"], True),
        ]
    )
    background = f"""<p:bg><p:bgPr><a:blipFill><a:blip r:link="rId3"/><a:stretch><a:fillRect/></a:stretch></a:blipFill><a:effectLst/></p:bgPr></p:bg>"""
    slide1_tree = _picture("r:link", "rId2")
    parts["ppt/slides/slide1.xml"] = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>{background}<p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {slide1_tree}
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""
    parts["ppt/slides/slide2.xml"] = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {_picture("r:link", "rId2")}
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""
    parts["ppt/slides/slide3.xml"] = parts["ppt/slides/slide2.xml"]
    image_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    layout_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
    parts["ppt/slides/_rels/slide1.xml.rels"] = _rels(
        [
            ("rId1", layout_rel, "../slideLayouts/slideLayout1.xml", False),
            ("rId2", image_rel, urls["slide"], True),
            ("rId3", image_rel, urls["background"], True),
            ("rId4", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide", "../notesSlides/notesSlide1.xml", False),
        ]
    )
    parts["ppt/slides/_rels/slide2.xml.rels"] = _rels(
        [
            ("rId1", layout_rel, "../slideLayouts/slideLayout1.xml", False),
            ("rId2", image_rel, urls["text"], True),
        ]
    )
    parts["ppt/slides/_rels/slide3.xml.rels"] = _rels(
        [
            ("rId1", layout_rel, "../slideLayouts/slideLayout1.xml", False),
            ("rId2", image_rel, file_uri, True),
        ]
    )
    parts["ppt/notesSlides/notesSlide1.xml"] = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {_picture("r:link", "rId2")}
  </p:spTree></p:cSld>
</p:notes>"""
    parts["ppt/notesSlides/_rels/notesSlide1.xml.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide", "../slides/slide1.xml", False),
            ("rId2", image_rel, urls["notes"], True),
        ]
    )
    parts["[Content_Types].xml"] = _content_types(parts)
    _zip(path, parts)


def _docx(path, *, link=None, embed=None, tracked=False):
    paragraphs = ["<w:p><w:r><w:t>UNIQUE_KEEP_TOKEN</w:t></w:r></w:p>"]
    if tracked:
        paragraphs.append(
            "<w:p><w:del w:id=\"1\" w:author=\"A\" w:date=\"2020-01-01T00:00:00Z\">"
            "<w:r><w:delText>UNIQUE_DELETED_TOKEN</w:delText></w:r></w:del></w:p>"
        )
    if link or embed is not None:
        attr = 'r:link="rId2"' if isinstance(link, str) else 'r:embed="rId2"'
        paragraphs.append(
            "<w:p><w:r><w:drawing><wp:inline>"
            "<wp:extent cx=\"360000\" cy=\"360000\"/>"
            "<wp:docPr id=\"1\" name=\"Picture 1\"/>"
            "<a:graphic><a:graphicData uri=\"http://schemas.openxmlformats.org/drawingml/2006/picture\">"
            "<pic:pic><pic:nvPicPr><pic:cNvPr id=\"0\" name=\"p\"/><pic:cNvPicPr/></pic:nvPicPr>"
            f"<pic:blipFill><a:blip {attr}/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>"
            "<pic:spPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"360000\" cy=\"360000\"/></a:xfrm>"
            "<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>"
            "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        )
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<w:body>
{''.join(paragraphs)}
<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
</w:body></w:document>"""
    rels = []
    parts = {"word/document.xml": document}
    if embed is not None:
        parts["word/media/image1.png"] = embed
        rels.append(
            ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", "media/image1.png", False)
        )
    elif isinstance(link, str):
        rels.append(
            ("rId2", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", link, True)
        )
    parts["word/_rels/document.xml.rels"] = _rels(rels)
    parts["_rels/.rels"] = _rels(
        [
            ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", "word/document.xml", False),
        ]
    )
    parts["[Content_Types].xml"] = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    _zip(path, parts)


def _rasterize(pdf, dest):
    _require_tool("pdftoppm")
    result = _run(["pdftoppm", "-jpeg", "-r", "72", str(pdf), str(dest / "slide")], dest)
    images = sorted(dest.glob("slide*.jpg"))
    return result, images


def _external_urls(base):
    return {
        "slide": f"{base}/slide.png",
        "text": f"{base}/text",
        "background": f"{base}/background.png",
        "master": f"{base}/master.png",
        "layout": f"{base}/layout.png",
        "notes": f"{base}/notes.png",
    }


class ExternalLinkedImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _require_tool("soffice")
        _require_tool("pdftoppm")

    def test_thumbnail_does_not_load_external_pictures(self):
        script = PPTX_SKILL / "scripts" / "thumbnail.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blue = _png(BLUE)
            control = root / "control.pptx"
            write_control_pptx(control, blue)
            control_out = root / "control-grid"
            control_run = _run(
                [sys.executable, str(script), str(control), str(control_out)],
                PPTX_SKILL,
            )
            grid = control_out.with_suffix(".jpg")
            self.assertEqual(
                control_run.returncode,
                0,
                f"embedded thumbnail did not render:\n{control_run.stderr}",
            )
            self.assertTrue(
                _jpeg_has([grid], _is_blue),
                "thumbnail grid dropped an embedded picture, so this run did not exercise rendering",
            )

            magenta = _png(MAGENTA)
            green_path = root / "linked.png"
            green_path.write_bytes(_png(GREEN))
            with _Server(magenta) as server:
                attack = root / "attack.pptx"
                write_attack_pptx(attack, _external_urls(server.base), green_path.as_uri())
                attack_out = root / "attack-grid"
                with _FileOpenWatch(green_path) as watch:
                    _run(
                        [sys.executable, str(script), str(attack), str(attack_out)],
                        PPTX_SKILL,
                    )
                self.assertEqual(
                    server.hits,
                    [],
                    f"thumbnail.py requested linked picture targets: {server.hits}",
                )
                self.assertFalse(
                    watch.opened,
                    "thumbnail.py opened a file-URI picture while rendering the deck",
                )
                produced = list(root.glob("attack-grid*.jpg"))
                self.assertFalse(
                    _jpeg_has(produced, _is_magenta) or _jpeg_has(produced, _is_green),
                    "thumbnail grid contains a linked external picture",
                )

    def test_presentation_visual_qa_does_not_load_external_pictures(self):
        script = PPTX_SKILL / "scripts" / "office" / "soffice.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_dir = root / "control"
            control_dir.mkdir()
            control = control_dir / "control.pptx"
            write_control_pptx(control, _png(BLUE))
            control_run = _run(
                [sys.executable, str(script), "--headless", "--convert-to", "pdf", control.name],
                control_dir,
            )
            self.assertEqual(control_run.returncode, 0, control_run.stderr)
            _, images = _rasterize(control_dir / "control.pdf", control_dir)
            self.assertTrue(
                images and _jpeg_has(images, _is_blue),
                "visual QA did not render an embedded picture",
            )

            attack_dir = root / "attack"
            attack_dir.mkdir()
            green_path = attack_dir / "linked.png"
            green_path.write_bytes(_png(GREEN))
            with _Server(_png(MAGENTA)) as server:
                attack = attack_dir / "attack.pptx"
                write_attack_pptx(attack, _external_urls(server.base), green_path.as_uri())
                with _FileOpenWatch(green_path) as watch:
                    _run(
                        [sys.executable, str(script), "--headless", "--convert-to", "pdf", attack.name],
                        attack_dir,
                    )
                    pdf = attack_dir / "attack.pdf"
                    if pdf.exists():
                        _rasterize(pdf, attack_dir)
                self.assertEqual(
                    server.hits,
                    [],
                    f"presentation PDF conversion requested linked picture targets: {server.hits}",
                )
                self.assertFalse(
                    watch.opened,
                    "presentation PDF conversion opened a file-URI picture",
                )
                produced = sorted(attack_dir.glob("slide*.jpg"))
                self.assertFalse(
                    _jpeg_has(produced, _is_magenta) or _jpeg_has(produced, _is_green),
                    "visual QA JPEGs contain a linked external picture",
                )

    def test_docx_visual_qa_does_not_load_external_pictures(self):
        script = DOCX_SKILL / "scripts" / "office" / "soffice.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_dir = root / "control"
            control_dir.mkdir()
            _docx(control_dir / "control.docx", embed=_png(BLUE))
            control_run = _run(
                [sys.executable, str(script), "--headless", "--convert-to", "pdf", "control.docx"],
                control_dir,
            )
            self.assertEqual(control_run.returncode, 0, control_run.stderr)
            _, images = _rasterize(control_dir / "control.pdf", control_dir)
            self.assertTrue(
                images and _jpeg_has(images, _is_blue),
                "document PDF conversion did not render an embedded picture",
            )

            attack_dir = root / "attack"
            attack_dir.mkdir()
            with _Server(_png(MAGENTA)) as server:
                _docx(attack_dir / "attack.docx", link=f"{server.base}/docx.png")
                _run(
                    [sys.executable, str(script), "--headless", "--convert-to", "pdf", "attack.docx"],
                    attack_dir,
                )
                pdf = attack_dir / "attack.pdf"
                if pdf.exists():
                    _rasterize(pdf, attack_dir)
                self.assertEqual(
                    server.hits,
                    [],
                    f"document PDF conversion requested a linked picture: {server.hits}",
                )
                produced = sorted(attack_dir.glob("slide*.jpg"))
                self.assertFalse(
                    _jpeg_has(produced, _is_magenta),
                    "document QA JPEGs contain a linked external picture",
                )

    def test_accept_changes_does_not_load_external_pictures(self):
        script = DOCX_SKILL / "scripts" / "accept_changes.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / "control.docx"
            accepted = root / "accepted.docx"
            _docx(control, embed=_png(BLUE), tracked=True)
            control_run = _run([sys.executable, str(script), str(control), str(accepted)], root)
            self.assertEqual(control_run.returncode, 0, control_run.stdout + control_run.stderr)
            with zipfile.ZipFile(accepted) as archive:
                document = archive.read("word/document.xml").decode()
            self.assertIn("UNIQUE_KEEP_TOKEN", document)
            self.assertNotIn(
                "UNIQUE_DELETED_TOKEN",
                document,
                "accept_changes did not open the document, so this run did not exercise LibreOffice",
            )

            with _Server(_png(MAGENTA)) as server:
                attack = root / "attack.docx"
                _docx(attack, link=f"{server.base}/docx.png", tracked=True)
                _run(
                    [sys.executable, str(script), str(attack), str(root / "attack-out.docx")],
                    root,
                )
                self.assertEqual(
                    server.hits,
                    [],
                    f"accept_changes requested a linked picture: {server.hits}",
                )

    def test_xlsx_recalc_does_not_load_external_pictures(self):
        script = XLSX_SKILL / "scripts" / "recalc.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / "control.xlsx"
            book = Workbook()
            book.active["A1"] = "=1+1"
            book.save(control)
            book.close()
            control_run = _run([sys.executable, str(script), str(control), "60"], XLSX_SKILL)
            self.assertEqual(control_run.returncode, 0, control_run.stdout + control_run.stderr)
            values = load_workbook(control, data_only=True)
            self.assertEqual(
                values.active["A1"].value,
                2,
                "recalc.py did not evaluate a formula, so this run did not exercise LibreOffice",
            )
            values.close()

            with _Server(_png(MAGENTA)) as server:
                attack = root / "attack.xlsx"
                embedded = root / "embedded.png"
                embedded.write_bytes(_png(MAGENTA))
                attack_book = Workbook()
                attack_book.active["A1"] = "=1+1"
                attack_book.active.add_image(XLImage(str(embedded)), "C1")
                attack_book.save(attack)
                attack_book.close()
                _retarget_xlsx_image(attack, f"{server.base}/sheet.png")
                _run([sys.executable, str(script), str(attack), "60"], XLSX_SKILL)
                self.assertEqual(
                    server.hits,
                    [],
                    f"recalc.py requested a linked picture: {server.hits}",
                )


def _retarget_xlsx_image(path, url):
    tmp = path.with_suffix(".retarget.xlsx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as dest:
        for info in source.infolist():
            payload = source.read(info.filename)
            name = info.filename
            if name.startswith("xl/media/"):
                continue
            if name.startswith("xl/drawings/") and name.endswith(".xml") and not name.startswith("xl/drawings/_rels/"):
                payload = payload.replace(b"r:embed=", b"r:link=")
            if name.endswith(".rels") and b"relationships/image" in payload:
                text = payload.decode()
                target_at = text.find('Target="')
                if target_at != -1:
                    end = text.find('"', target_at + len('Target="'))
                    text = text[:target_at] + f'Target="{url}" TargetMode="External"' + text[end + 1 :]
                    payload = text.encode()
            dest.writestr(name, payload)
    tmp.replace(path)


if __name__ == "__main__":
    unittest.main()
