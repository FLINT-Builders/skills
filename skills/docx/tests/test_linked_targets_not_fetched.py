"""Linked picture targets must not be fetched when this skill opens a document.

The docx skill converts and previews caller-supplied Word files with
``scripts/office/soffice.py`` and opens them again from ``scripts/accept_changes.py``.
A document can point a picture relationship at an external target
(``TargetMode="External"``): an http URL, a ``file:`` URL, an absolute path, or a
path next to the file. These tests fail while LibreOffice still requests that
target during the conversion the skill runs.

Run from the repository root:

    python3 skills/docx/tests/test_linked_targets_not_fetched.py

Not covered here, because this LibreOffice did not request them when exercised,
or because they belong to another skill: VML ``imagedata`` relationships, OLE
object relationships, ``INCLUDEPICTURE`` fields, footnote-part pictures, and the
separate ``soffice.py`` copies under the pptx and xlsx skills.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SOFFICE_PY = SKILL_DIR / "scripts" / "office" / "soffice.py"
ACCEPT_CHANGES_PY = SKILL_DIR / "scripts" / "accept_changes.py"

IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
HEADER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
FOOTER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"

# Distinct from a placeholder rectangle. 3x2, so a re-encoded embed still matches.
CANARY_PIXELS = (
    ((12, 34, 56), (78, 90, 123), (34, 56, 78)),
    ((90, 12, 34), (56, 78, 90), (11, 22, 33)),
)

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def _png(pixels: tuple[tuple[tuple[int, int, int], ...], ...]) -> bytes:
    height = len(pixels)
    width = len(pixels[0])
    raw = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in pixels
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


CANARY_PNG = _png(CANARY_PIXELS)


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_up and distance_left <= distance_upper_left:
        return left
    if distance_up <= distance_upper_left:
        return up
    return upper_left


def _png_rgb(data: bytes) -> list[tuple[int, int, int]] | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    index = 8
    width = height = None
    bit_depth = color_type = None
    idat = b""
    while index + 8 <= len(data):
        length = struct.unpack(">I", data[index : index + 4])[0]
        tag = data[index + 4 : index + 8]
        chunk = data[index + 8 : index + 8 + length]
        index += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
    if width is None or height is None or bit_depth != 8 or color_type != 2:
        return None
    raw = zlib.decompress(idat)
    stride = width * 3
    rows: list[bytearray] = []
    previous = bytearray(stride)
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for column in range(stride):
            left = row[column - 3] if column >= 3 else 0
            up = previous[column]
            upper_left = previous[column - 3] if column >= 3 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                predictor = _paeth(left, up, upper_left)
            else:
                return None
            row[column] = (row[column] + predictor) & 255
        rows.append(row)
        previous = row
    pixels: list[tuple[int, int, int]] = []
    for row in rows:
        for column in range(0, stride, 3):
            pixels.append((row[column], row[column + 1], row[column + 2]))
    return pixels


def _canary_embedded(pdf: Path) -> bool:
    if not pdf.is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="pdfimg_") as directory:
        prefix = Path(directory) / "img"
        subprocess.run(
            ["pdfimages", "-png", str(pdf), str(prefix)],
            check=False,
            capture_output=True,
        )
        expected = [pixel for row in CANARY_PIXELS for pixel in row]
        for image in Path(directory).glob("img-*.png"):
            if _png_rgb(image.read_bytes()) == expected:
                return True
    return False


def _picture(relationship_id: str) -> str:
    return f"""<w:p><w:r><w:drawing><wp:inline>
<wp:extent cx="914400" cy="457200"/><wp:docPr id="1" name="Picture"/>
<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="Picture"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:link="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="457200"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic>
</wp:inline></w:drawing></w:r></w:p>"""


def _relationships(items: list[tuple[str, str, str, bool]]) -> str:
    body = []
    for relationship_id, relationship_type, target, external in items:
        mode = ' TargetMode="External"' if external else ""
        body.append(
            f'<Relationship Id="{relationship_id}" Type="{relationship_type}" '
            f'Target="{target}"{mode}/>'
        )
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(body)
        + "</Relationships>"
    )


def _content_types(parts: list[tuple[str, str]], template: bool = False) -> str:
    main = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
        if template
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    overrides = [
        f'<Override PartName="/word/document.xml" ContentType="{main}"/>'
    ]
    overrides.extend(
        f'<Override PartName="/{name}" ContentType="{content_type}"/>' for name, content_type in parts
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(overrides)
        + "</Types>"
    )


def _package(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in parts.items():
            archive.writestr(name, contents)
    return buffer.getvalue()


def _part(tag: str, inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<{tag} {NS}>{inner}</{tag}>'
    )


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.server.record(("OPTIONS", self.path))
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.server.record(("GET", self.path))
        body = CANARY_PNG
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self._lock = threading.Lock()
        self.hits: list[tuple[str, str]] = []

    def record(self, hit: tuple[str, str]) -> None:
        with self._lock:
            self.hits.append(hit)

    def snapshot(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self.hits)

    def reset(self) -> None:
        with self._lock:
            self.hits.clear()


class LinkedTargetTests(unittest.TestCase):
    server: _Server
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()

    def setUp(self) -> None:
        self.server.reset()
        self.workdir = tempfile.TemporaryDirectory(prefix="docx-link-")
        self.addCleanup(self.workdir.cleanup)
        self.root = Path(self.workdir.name)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _write(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def _document(
        self,
        name: str,
        *,
        body_target: str | None,
        marker: str,
        header_target: str | None = None,
        footer_target: str | None = None,
        template: bool = False,
    ) -> Path:
        extras: list[tuple[str, str]] = []
        relationships: list[tuple[str, str, str, bool]] = []
        section = ""
        if header_target is not None:
            relationships.append(("rId2", HEADER_REL, "header1.xml", False))
            section += '<w:headerReference w:type="default" r:id="rId2"/>'
            extras.append(
                (
                    "word/header1.xml",
                    _part("w:hdr", _picture("rId9")),
                )
            )
            extras.append(
                (
                    "word/_rels/header1.xml.rels",
                    _relationships([("rId9", IMAGE_REL, header_target, True)]),
                )
            )
        if footer_target is not None:
            relationships.append(("rId3", FOOTER_REL, "footer1.xml", False))
            section += '<w:footerReference w:type="default" r:id="rId3"/>'
            extras.append(("word/footer1.xml", _part("w:ftr", _picture("rId9"))))
            extras.append(
                (
                    "word/_rels/footer1.xml.rels",
                    _relationships([("rId9", IMAGE_REL, footer_target, True)]),
                )
            )
        body = f"<w:p><w:r><w:t>{marker}</w:t></w:r></w:p>"
        if body_target is not None:
            relationships.append(("rId5", IMAGE_REL, body_target, True))
            body += _picture("rId5")
        document = _part(
            "w:document",
            "<w:body>"
            + body
            + f"<w:sectPr>{section}<w:pgSz w:w=\"12240\" w:h=\"15840\"/></w:sectPr>"
            + "</w:body>",
        )
        content_parts = []
        if header_target is not None:
            content_parts.append(
                (
                    "word/header1.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
                )
            )
        if footer_target is not None:
            content_parts.append(
                (
                    "word/footer1.xml",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
                )
            )
        parts = {
            "[Content_Types].xml": _content_types(content_parts, template=template),
            "_rels/.rels": _relationships(
                [
                    (
                        "rId1",
                        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                        "word/document.xml",
                        False,
                    )
                ]
            ),
            "word/document.xml": document,
            "word/_rels/document.xml.rels": _relationships(relationships),
        }
        for part_name, part_body in extras:
            parts[part_name] = part_body
        return self._write(name, _package(parts))

    def _run_soffice(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SOFFICE_PY), *args],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def _assert_no_request(self) -> None:
        hits = self.server.snapshot()
        self.assertEqual(hits, [], f"LibreOffice requested a linked target: {hits}")

    def _pdf_text(self, pdf: Path) -> str:
        result = subprocess.run(
            ["pdftotext", str(pdf), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _assert_pdf_has_text_without_canary(self, pdf: Path, marker: str) -> None:
        self.assertTrue(pdf.is_file(), f"missing converted pdf {pdf}")
        self.assertIn(marker, self._pdf_text(pdf))
        self.assertFalse(
            _canary_embedded(pdf),
            "converted pdf still contains the linked picture",
        )

    def test_plain_document_still_converts(self) -> None:
        document = self._document("plain.docx", body_target=None, marker="PLAIN-MARKER")
        result = self._run_soffice(
            ["--headless", "--convert-to", "pdf", "--outdir", str(self.root), str(document)]
        )
        self._assert_no_request()
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_pdf_has_text_without_canary(self.root / "plain.pdf", "PLAIN-MARKER")
        self.assertTrue(document.is_file())

    def test_pdf_preview_does_not_fetch_body_header_or_footer_urls(self) -> None:
        document = self._document(
            "preview.docx",
            body_target=f"{self.base}/body.png",
            header_target=f"{self.base}/header.png",
            footer_target=f"{self.base}/footer.png",
            marker="PREVIEW-MARKER",
        )
        result = self._run_soffice(
            ["--headless", "--convert-to", "pdf", "--outdir", str(self.root), str(document)]
        )
        self._assert_no_request()
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_pdf_has_text_without_canary(self.root / "preview.pdf", "PREVIEW-MARKER")

    def test_pdf_preview_does_not_fetch_dotx_template_url(self) -> None:
        document = self._document(
            "template.dotx",
            body_target=f"{self.base}/template.png",
            marker="TEMPLATE-MARKER",
            template=True,
        )
        result = self._run_soffice(
            ["--headless", "--convert-to", "pdf", "--outdir", str(self.root), str(document)]
        )
        self._assert_no_request()
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_pdf_has_text_without_canary(self.root / "template.pdf", "TEMPLATE-MARKER")

    def test_pdf_preview_does_not_read_local_picture_targets(self) -> None:
        canary = self.root / "canary.png"
        canary.write_bytes(CANARY_PNG)
        targets = {
            "file-url.docx": canary.as_uri(),
            "absolute.docx": str(canary),
            "relative.docx": canary.name,
        }
        for name, target in targets.items():
            with self.subTest(target=name):
                self.server.reset()
                document = self._document(name, body_target=target, marker="LOCAL-MARKER")
                opens = _watch_opens(canary)
                try:
                    result = self._run_soffice(
                        [
                            "--headless",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            str(self.root),
                            str(document),
                        ]
                    )
                finally:
                    opened = opens.stop()
                self._assert_no_request()
                self.assertEqual(opened, [], f"LibreOffice opened {canary}: {opened}")
                self.assertEqual(result.returncode, 0, result.stderr)
                pdf = self.root / (Path(name).stem + ".pdf")
                self._assert_pdf_has_text_without_canary(pdf, "LOCAL-MARKER")

    def test_documented_repack_then_preview_does_not_fetch(self) -> None:
        source = self._document(
            "incoming.docx",
            body_target=f"{self.base}/repack.png",
            marker="REPACK-MARKER",
        )
        unpacked = self.root / "unpacked"
        subprocess.run(["unzip", "-q", str(source), "-d", str(unpacked)], check=True)
        subprocess.run(["find", str(unpacked), "-type", "l", "-delete"], check=True)
        packed = self.root / "out.docx"
        subprocess.run(
            ["zip", "-Xr", str(packed), "."],
            cwd=unpacked,
            check=True,
            capture_output=True,
        )
        self.server.reset()
        result = self._run_soffice(
            ["--headless", "--convert-to", "pdf", "--outdir", str(self.root), str(packed)]
        )
        self._assert_no_request()
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_pdf_has_text_without_canary(self.root / "out.pdf", "REPACK-MARKER")

    def test_legacy_doc_conversion_does_not_fetch_linked_picture(self) -> None:
        source = self._document(
            "legacy.docx",
            body_target=f"{self.base}/legacy.png",
            marker="LEGACY-MARKER",
        )
        profile = self.root / "fixture-profile"
        profile.mkdir()
        soffice = shutil.which("soffice")
        self.assertIsNotNone(soffice)
        fixture = subprocess.run(
            [
                soffice,
                "--headless",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "doc",
                "--outdir",
                str(self.root),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        legacy = self.root / "legacy.doc"
        self.assertEqual(fixture.returncode, 0, fixture.stderr)
        self.assertTrue(legacy.is_file())
        self.server.reset()
        result = self._run_soffice(
            ["--headless", "--convert-to", "docx", "--outdir", str(self.root), str(legacy)]
        )
        self._assert_no_request()
        self.assertEqual(result.returncode, 0, result.stderr)
        converted = self.root / "legacy.docx"
        self.assertTrue(converted.is_file())
        with zipfile.ZipFile(converted) as archive:
            document_xml = archive.read("word/document.xml")
        self.assertIn(b"LEGACY-MARKER", document_xml)

    def test_accept_changes_does_not_fetch_linked_picture(self) -> None:
        source = self._document(
            "tracked.docx",
            body_target=f"{self.base}/accept.png",
            marker="ACCEPT-MARKER",
        )
        output = self.root / "accepted.docx"
        self.server.reset()
        result = subprocess.run(
            [sys.executable, str(ACCEPT_CHANGES_PY), str(source), str(output)],
            cwd=SKILL_DIR,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self._assert_no_request()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Error", result.stdout)
        self.assertTrue(output.is_file())
        self.assertTrue(source.is_file())


class _OpenWatch:
    def __init__(self, path: Path) -> None:
        import ctypes
        import select

        self._select = select
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        # IN_NONBLOCK | IN_CLOEXEC
        self._fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, "inotify_init1")
        # IN_OPEN | IN_ACCESS
        watch = self._libc.inotify_add_watch(self._fd, os.fsencode(path), 0x00000020 | 0x00000001)
        if watch < 0:
            error = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(error, f"inotify_add_watch {path}")
        self.events: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            readable, _, _ = self._select.select([self._fd], [], [], 0.05)
            if not readable:
                continue
            try:
                data = os.read(self._fd, 4096)
            except BlockingIOError:
                continue
            offset = 0
            while offset + 16 <= len(data):
                mask = struct.unpack_from("=iIII", data, offset)[1]
                name_length = struct.unpack_from("=iIII", data, offset)[3]
                self.events.append(mask)
                offset += 16 + name_length

    def stop(self) -> list[int]:
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self._fd)
        return list(self.events)


def _watch_opens(path: Path) -> _OpenWatch:
    return _OpenWatch(path)


if __name__ == "__main__":
    if not SOFFICE_PY.is_file():
        sys.exit(f"missing {SOFFICE_PY}")
    unittest.main(verbosity=2)
