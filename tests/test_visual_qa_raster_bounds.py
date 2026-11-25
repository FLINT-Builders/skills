"""Bounds for the visual QA raster path an agent is told to run.

These tests execute the shell recipes in the pptx and docx skills, and the
thumbnail.py CLI, against decks this process builds. They are the spec for a
fix: they fail while a caller-supplied deck can be rasterized without a page
cap or a pixel cap, or while the pptx recipe deletes working-directory
slide-*.jpg files.

Contract (what a fix must make true):

- A deck or document with more than MAX_PAGES pages is refused before any PDF
  or raster image is created, and before any pre-existing slide-*.jpg or
  page-*.jpg in the working directory is removed or rewritten.
- A deck or document of exactly MAX_PAGES pages, at the default 16:9 or US
  Letter size, still rasterizes every page, with every image's longer edge
  at most MAX_LONG_EDGE_PX.
- A single page of OVERSIZED_INCHES inches is refused, or is rasterized only
  after the longer edge has been bounded to MAX_LONG_EDGE_PX. No image whose
  longer edge exceeds that limit may appear, including one that is deleted
  before the process exits. Pre-existing slide-*.jpg files in the working
  directory stay byte-for-byte intact.
- The same rules apply when the input was a .potx renamed to .pptx, which is
  how the pptx skill tells the agent to preview a template.

Run:

    python3 -m unittest discover -s tests -v

The recipes are executed with bash's default settings (no set -e), because
that is how a copied shell block behaves. A guard that returns non-zero and
then falls through into pdftoppm still fails these tests.
"""

from __future__ import annotations

import ctypes
import os
import select
import stat
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from PIL import Image


REPO = Path(__file__).resolve().parents[1]
PPTX_SKILL = REPO / "skills" / "pptx"
DOCX_SKILL = REPO / "skills" / "docx"

# A normal 16:9 slide is 10in x 5.625in. At the pptx recipe's 150 DPI that is
# 1500 x 844, and at thumbnail.py's 100 DPI it is 1000 x 563. US Letter at the
# docx recipe's 100 DPI is 850 x 1100. All of those fit under this edge.
MAX_LONG_EDGE_PX = 2000
MAX_PAGES = 40
OVERSIZED_INCHES = 30.0
OVERSIZED_HEIGHT_INCHES = 16.875

SLIDE_SENTINEL_NAME = "slide-keep.jpg"
PAGE_SENTINEL_NAME = "page-keep.jpg"
SENTINEL_BYTES = b"KEEP-SENTINEL-not-a-rendered-slide\n"


def build_pptx(path: Path, slides: int, width_in: float, height_in: float) -> None:
    """Minimal PPTX LibreOffice Impress will export. Page size is p:sldSz."""
    emu = 914400
    cx = int(round(width_in * emu))
    cy = int(round(height_in * emu))
    ct_overrides = "\n".join(
        f'  <Override PartName="/ppt/slides/slide{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slides + 1)
    )
    sld_ids = "\n".join(
        f'    <p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(1, slides + 1)
    )
    pres_rels = [
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
        'Target="slideMasters/slideMaster1.xml"/>'
    ]
    for i in range(1, slides + 1):
        pres_rels.append(
            f'  <Relationship Id="rId{i + 1}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{i}.xml"/>'
        )
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
{ct_overrides}
</Types>
"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>
"""
    presentation = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>
{sld_ids}
  </p:sldIdLst>
  <p:sldSz cx="{cx}" cy="{cy}"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""
    pres_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(pres_rels)
        + "\n</Relationships>\n"
    )
    master = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld name="Office Theme"><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>
</p:sldMaster>
"""
    master_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>
"""
    layout = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
  <p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>
"""
    layout_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>
"""
    theme = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
      <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="1F497D"/></a:dk2>
      <a:lt2><a:srgbClr val="EEECE1"/></a:lt2>
      <a:accent1><a:srgbClr val="4F81BD"/></a:accent1>
      <a:accent2><a:srgbClr val="C0504D"/></a:accent2>
      <a:accent3><a:srgbClr val="9BBB59"/></a:accent3>
      <a:accent4><a:srgbClr val="8064A2"/></a:accent4>
      <a:accent5><a:srgbClr val="4BACC6"/></a:accent5>
      <a:accent6><a:srgbClr val="F79646"/></a:accent6>
      <a:hlink><a:srgbClr val="0000FF"/></a:hlink>
      <a:folHlink><a:srgbClr val="800080"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
    <a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>
  </a:themeElements>
</a:theme>
"""
    slide_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
"""
    slide_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("ppt/presentation.xml", presentation)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels_xml)
        zf.writestr("ppt/slideMasters/slideMaster1.xml", master)
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", layout)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        zf.writestr("ppt/theme/theme1.xml", theme)
        for i in range(1, slides + 1):
            zf.writestr(f"ppt/slides/slide{i}.xml", slide_xml)
            zf.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", slide_rels)


def build_docx(path: Path, pages: int, width_in: float, height_in: float) -> None:
    """Minimal DOCX LibreOffice Writer will export. Page size is w:pgSz twips."""
    twip = 1440
    width = int(round(width_in * twip))
    height = int(round(height_in * twip))
    paragraphs = []
    for i in range(1, pages + 1):
        break_xml = '<w:r><w:br w:type="page"/></w:r>' if i < pages else ""
        paragraphs.append(f"<w:p><w:r><w:t>Page {i}</w:t></w:r>{break_xml}</w:p>")
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(paragraphs)}
    <w:sectPr>
      <w:pgSz w:w="{width}" w:h="{height}"/>
      <w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)


def extract_bash_recipe(skill_md: Path, heading: str) -> str:
    text = skill_md.read_text(encoding="utf-8")
    heading_at = text.find(heading)
    if heading_at < 0:
        raise AssertionError(f"{skill_md} has no {heading!r} section")
    rest = text[heading_at:]
    next_heading = rest.find("\n## ", len(heading))
    section = rest if next_heading < 0 else rest[:next_heading]
    fence = section.find("```bash\n")
    if fence < 0:
        raise AssertionError(f"{skill_md} section {heading!r} has no bash block")
    start = fence + len("```bash\n")
    end = section.find("\n```", start)
    if end < 0:
        raise AssertionError(f"{skill_md} section {heading!r} has an unclosed bash block")
    return section[start:end] + "\n"


def rewrite_skill_scripts(recipe: str, skill_dir: Path) -> str:
    """Script paths in the skills are relative to the skill directory.

    The working directory of the recipe stays the workspace the agent is
    previewing, which is where output.pptx / output.docx and slide images land.
    """
    return recipe.replace("scripts/", f"{skill_dir.as_posix()}/scripts/")


# Linux inotify constants. The watch records a raster that is created and
# then deleted before the process returns, which a single after-the-fact
# directory listing would miss.
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MODIFY = 0x00000002
_IN_ISDIR = 0x40000000
_IN_IGNORED = 0x00008000
_IN_CLOEXEC = 0x80000
_WATCH_MASK = (
    _IN_CLOSE_WRITE | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE | _IN_MODIFY
)


class _InotifyEvent(ctypes.Structure):
    _fields_ = [
        ("wd", ctypes.c_int),
        ("mask", ctypes.c_uint32),
        ("cookie", ctypes.c_uint32),
        ("length", ctypes.c_uint32),
    ]


class _ArtifactWatch:
    """Record rasters and PDFs created during a run, and sentinel mutation."""

    def __init__(self, roots: list[Path], sentinels: dict[Path, bytes]):
        self.roots = roots
        self.sentinels = sentinels
        self.rasters: dict[str, tuple[int, int]] = {}
        self.pdfs: list[str] = []
        self.sentinel_changes: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="artifact-watch", daemon=True)
        self._lock = threading.Lock()
        self._wd_to_dir: dict[int, Path] = {}
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._fd = self._libc.inotify_init1(_IN_CLOEXEC)
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), "inotify_init1")
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        for root in roots:
            self._add_tree(root)

    def _add_tree(self, root: Path) -> None:
        if not root.exists():
            return
        for dirpath, dirnames, filenames in os.walk(root):
            self._add_dir(Path(dirpath))
            for name in filenames:
                self._note_file(Path(dirpath) / name, preexisting=True)
        self._preexisting: set[str] = getattr(self, "_preexisting", set())

    def _add_dir(self, directory: Path) -> None:
        watch = self._libc.inotify_add_watch(
            self._fd, os.fsencode(directory), _WATCH_MASK
        )
        if watch >= 0:
            self._wd_to_dir[watch] = directory

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        for root in self.roots:
            if not root.exists():
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    self._note_file(Path(dirpath) / name, preexisting=False)
        self._note_sentinels()
        os.close(self._fd)

    def _run(self) -> None:
        header = ctypes.sizeof(_InotifyEvent)
        while not self._stop.is_set():
            readable, _, _ = select.select([self._fd], [], [], 0.2)
            if not readable:
                self._note_sentinels()
                continue
            try:
                blob = os.read(self._fd, 65536)
            except OSError:
                break
            offset = 0
            while offset + header <= len(blob):
                event = _InotifyEvent.from_buffer_copy(blob[offset : offset + header])
                offset += header
                name = b""
                if event.length:
                    raw = blob[offset : offset + event.length]
                    offset += event.length
                    name = raw.split(b"\x00", 1)[0]
                directory = self._wd_to_dir.get(event.wd)
                if directory is None or not name or event.mask & _IN_IGNORED:
                    continue
                path = directory / os.fsdecode(name)
                if event.mask & _IN_ISDIR and event.mask & (_IN_CREATE | _IN_MOVED_TO):
                    self._add_dir(path)
                    self._scan_new_tree(path)
                    continue
                if event.mask & (_IN_DELETE | _IN_MOVED_FROM | _IN_MODIFY | _IN_CLOSE_WRITE):
                    self._note_sentinels()
                if event.mask & (_IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE):
                    self._note_file(path, preexisting=False)

    def _scan_new_tree(self, root: Path) -> None:
        if not root.exists():
            return
        for dirpath, dirnames, filenames in os.walk(root):
            for dirname in dirnames:
                self._add_dir(Path(dirpath) / dirname)
            for name in filenames:
                self._note_file(Path(dirpath) / name, preexisting=False)

    def _note_sentinels(self) -> None:
        for sentinel, original in self.sentinels.items():
            key = str(sentinel)
            with self._lock:
                if key in self.sentinel_changes:
                    continue
            if not sentinel.exists():
                with self._lock:
                    self.sentinel_changes.append(key)
                continue
            try:
                current = sentinel.read_bytes()
            except OSError:
                with self._lock:
                    self.sentinel_changes.append(key)
                continue
            if current != original:
                with self._lock:
                    self.sentinel_changes.append(key)

    def _note_file(self, path: Path, preexisting: bool) -> None:
        key = str(path)
        if preexisting:
            if not hasattr(self, "_preexisting"):
                self._preexisting = set()
            self._preexisting.add(key)
            return
        if key in getattr(self, "_preexisting", set()):
            return
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            with self._lock:
                if key not in self.pdfs:
                    self.pdfs.append(key)
        elif suffix in {".jpg", ".jpeg", ".png"}:
            dims = _image_size(path)
            if dims is None:
                return
            with self._lock:
                self.rasters[key] = dims


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            image.load()
            return image.size
    except Exception:
        return None


def _long_edge(size: tuple[int, int]) -> int:
    return max(size)


class _RunResult:
    def __init__(self, completed: subprocess.CompletedProcess[str], watch: _ArtifactWatch):
        self.completed = completed
        self.watch = watch

    @property
    def returncode(self) -> int:
        return self.completed.returncode


def _python_shim(directory: Path) -> None:
    """The skill recipes invoke `python`. Point that name at python3."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "python"
    shim.write_text("#!/bin/sh\nexec python3 \"$@\"\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)


def _run_bash(recipe: str, work: Path, sentinels: dict[Path, bytes], tmp: Path) -> _RunResult:
    shim_dir = work / ".shim"
    _python_shim(shim_dir)
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    watch = _ArtifactWatch([work, tmp], sentinels)
    watch.start()
    try:
        completed = subprocess.run(
            ["bash", "-c", recipe],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    finally:
        watch.stop()
    return _RunResult(completed, watch)


def _run_thumbnail(deck: Path, work: Path, sentinels: dict[Path, bytes], tmp: Path) -> _RunResult:
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp)
    prefix = work / "grid"
    watch = _ArtifactWatch([work, tmp], sentinels)
    watch.start()
    try:
        completed = subprocess.run(
            [
                "python3",
                str(PPTX_SKILL / "scripts" / "thumbnail.py"),
                str(deck),
                str(prefix),
            ],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    finally:
        watch.stop()
    return _RunResult(completed, watch)


def _place_sentinel(work: Path, name: str) -> dict[Path, bytes]:
    path = work / name
    path.write_bytes(SENTINEL_BYTES)
    return {path: SENTINEL_BYTES}


def _assert_edges(test: unittest.TestCase, rasters: dict[str, tuple[int, int]]) -> None:
    too_big = {path: size for path, size in rasters.items() if _long_edge(size) > MAX_LONG_EDGE_PX}
    test.assertEqual(
        too_big,
        {},
        f"raster longer edge exceeded {MAX_LONG_EDGE_PX}px: {too_big}",
    )


def _failure_detail(result: _RunResult) -> str:
    stderr = (result.completed.stderr or "").strip()
    stdout = (result.completed.stdout or "").strip()
    return (
        f"exit={result.returncode}\n"
        f"rasters={result.watch.rasters}\n"
        f"pdfs={result.watch.pdfs}\n"
        f"sentinel_changes={result.watch.sentinel_changes}\n"
        f"stdout={stdout[-1500:]}\n"
        f"stderr={stderr[-1500:]}"
    )


class PptxVisualQaRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pptx-qa-")
        self.work = Path(self._tmp.name) / "work"
        self.tmp = Path(self._tmp.name) / "tmp"
        self.work.mkdir()
        self.tmp.mkdir()
        self.recipe = rewrite_skill_scripts(
            extract_bash_recipe(PPTX_SKILL / "SKILL.md", "## Converting to Images"),
            PPTX_SKILL,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _deck(self, slides: int, width: float, height: float, name: str = "output.pptx") -> None:
        build_pptx(self.work / name, slides, width, height)

    def test_recipe_contains_the_conversion_commands(self) -> None:
        # The behavioral tests below run this block. Fail loudly if the section
        # moves, instead of silently testing an empty script.
        self.assertIn("soffice.py", self.recipe)
        self.assertIn("pdftoppm", self.recipe)
        self.assertIn("output.pptx", self.recipe)

    def test_normal_deck_rasterizes_every_slide_within_pixel_bound(self) -> None:
        self._deck(2, 10, 5.625)
        result = _run_bash(self.recipe, self.work, {}, self.tmp)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        slide_images = {p: s for p, s in result.watch.rasters.items() if Path(p).name.startswith("slide-")}
        self.assertEqual(len(slide_images), 2, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_max_page_deck_still_rasterizes(self) -> None:
        self._deck(MAX_PAGES, 10, 5.625)
        result = _run_bash(self.recipe, self.work, {}, self.tmp)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        slide_images = {p: s for p, s in result.watch.rasters.items() if Path(p).name.startswith("slide-")}
        self.assertEqual(len(slide_images), MAX_PAGES, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_existing_slide_jpegs_are_not_deleted(self) -> None:
        self._deck(1, 10, 5.625)
        sentinels = _place_sentinel(self.work, SLIDE_SENTINEL_NAME)
        result = _run_bash(self.recipe, self.work, sentinels, self.tmp)
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        self.assertEqual((self.work / SLIDE_SENTINEL_NAME).read_bytes(), SENTINEL_BYTES)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        slide_images = [
            p for p in result.watch.rasters if Path(p).name.startswith("slide-") and SLIDE_SENTINEL_NAME not in p
        ]
        self.assertGreaterEqual(len(slide_images), 1, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_too_many_slides_are_refused_before_any_output(self) -> None:
        self._deck(MAX_PAGES + 1, 10, 5.625)
        sentinels = _place_sentinel(self.work, SLIDE_SENTINEL_NAME)
        result = _run_bash(self.recipe, self.work, sentinels, self.tmp)
        self.assertNotEqual(result.returncode, 0, _failure_detail(result))
        self.assertEqual(result.watch.rasters, {}, _failure_detail(result))
        self.assertEqual(result.watch.pdfs, [], _failure_detail(result))
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        self.assertTrue((self.work / SLIDE_SENTINEL_NAME).exists())

    def test_potx_renamed_oversized_page_is_bounded_without_deleting_jpegs(self) -> None:
        potx = self.work / "template.potx"
        build_pptx(potx, 1, OVERSIZED_INCHES, OVERSIZED_HEIGHT_INCHES)
        (self.work / "output.pptx").write_bytes(potx.read_bytes())
        sentinels = _place_sentinel(self.work, SLIDE_SENTINEL_NAME)
        result = _run_bash(self.recipe, self.work, sentinels, self.tmp)
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        self.assertEqual((self.work / SLIDE_SENTINEL_NAME).read_bytes(), SENTINEL_BYTES)
        _assert_edges(self, result.watch.rasters)
        if result.returncode == 0:
            self.assertGreaterEqual(len(result.watch.rasters), 1, _failure_detail(result))
        else:
            self.assertEqual(result.watch.rasters, {}, _failure_detail(result))
            self.assertEqual(result.watch.pdfs, [], _failure_detail(result))


class ThumbnailRasterBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="thumb-qa-")
        self.work = Path(self._tmp.name) / "work"
        self.tmp = Path(self._tmp.name) / "tmp"
        self.work.mkdir()
        self.tmp.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normal_deck_thumbnails_within_pixel_bound(self) -> None:
        deck = self.work / "deck.pptx"
        build_pptx(deck, 2, 10, 5.625)
        sentinels = _place_sentinel(self.work, SLIDE_SENTINEL_NAME)
        result = _run_thumbnail(deck, self.work, sentinels, self.tmp)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        slide_images = {p: s for p, s in result.watch.rasters.items() if Path(p).name.startswith("slide-")}
        self.assertEqual(len(slide_images), 2, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_max_page_deck_still_thumbnails(self) -> None:
        deck = self.work / "deck.pptx"
        build_pptx(deck, MAX_PAGES, 10, 5.625)
        result = _run_thumbnail(deck, self.work, {}, self.tmp)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        slide_images = {p: s for p, s in result.watch.rasters.items() if Path(p).name.startswith("slide-")}
        self.assertEqual(len(slide_images), MAX_PAGES, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_too_many_slides_are_refused_before_rasterizing(self) -> None:
        deck = self.work / "deck.pptx"
        build_pptx(deck, MAX_PAGES + 1, 10, 5.625)
        sentinels = _place_sentinel(self.work, SLIDE_SENTINEL_NAME)
        result = _run_thumbnail(deck, self.work, sentinels, self.tmp)
        self.assertNotEqual(result.returncode, 0, _failure_detail(result))
        self.assertEqual(result.watch.rasters, {}, _failure_detail(result))
        self.assertEqual(result.watch.pdfs, [], _failure_detail(result))
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        self.assertFalse(list(self.work.glob("grid*.jpg")), _failure_detail(result))

    def test_oversized_slide_is_bounded(self) -> None:
        deck = self.work / "deck.pptx"
        build_pptx(deck, 1, OVERSIZED_INCHES, OVERSIZED_HEIGHT_INCHES)
        sentinels = _place_sentinel(self.work, SLIDE_SENTINEL_NAME)
        result = _run_thumbnail(deck, self.work, sentinels, self.tmp)
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        _assert_edges(self, result.watch.rasters)
        if result.returncode == 0:
            self.assertGreaterEqual(len(result.watch.rasters), 1, _failure_detail(result))
        else:
            self.assertEqual(result.watch.rasters, {}, _failure_detail(result))
            self.assertEqual(result.watch.pdfs, [], _failure_detail(result))


class DocxVisualQaRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="docx-qa-")
        self.work = Path(self._tmp.name) / "work"
        self.tmp = Path(self._tmp.name) / "tmp"
        self.work.mkdir()
        self.tmp.mkdir()
        self.recipe = rewrite_skill_scripts(
            extract_bash_recipe(DOCX_SKILL / "SKILL.md", "## Verify the output"),
            DOCX_SKILL,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recipe_contains_the_conversion_commands(self) -> None:
        self.assertIn("soffice.py", self.recipe)
        self.assertIn("pdftoppm", self.recipe)
        self.assertIn("output.docx", self.recipe)

    def test_normal_document_rasterizes_every_page_within_pixel_bound(self) -> None:
        build_docx(self.work / "output.docx", 2, 8.5, 11)
        result = _run_bash(self.recipe, self.work, {}, self.tmp)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        page_images = {p: s for p, s in result.watch.rasters.items() if Path(p).name.startswith("page-")}
        self.assertEqual(len(page_images), 2, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_max_page_document_still_rasterizes(self) -> None:
        build_docx(self.work / "output.docx", MAX_PAGES, 8.5, 11)
        result = _run_bash(self.recipe, self.work, {}, self.tmp)
        self.assertEqual(result.returncode, 0, _failure_detail(result))
        page_images = {p: s for p, s in result.watch.rasters.items() if Path(p).name.startswith("page-")}
        self.assertEqual(len(page_images), MAX_PAGES, _failure_detail(result))
        _assert_edges(self, result.watch.rasters)

    def test_too_many_pages_are_refused_before_any_output(self) -> None:
        build_docx(self.work / "output.docx", MAX_PAGES + 1, 8.5, 11)
        sentinels = _place_sentinel(self.work, PAGE_SENTINEL_NAME)
        result = _run_bash(self.recipe, self.work, sentinels, self.tmp)
        self.assertNotEqual(result.returncode, 0, _failure_detail(result))
        self.assertEqual(result.watch.rasters, {}, _failure_detail(result))
        self.assertEqual(result.watch.pdfs, [], _failure_detail(result))
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))

    def test_oversized_page_is_bounded_without_deleting_page_jpegs(self) -> None:
        build_docx(self.work / "output.docx", 1, OVERSIZED_INCHES, OVERSIZED_HEIGHT_INCHES)
        sentinels = _place_sentinel(self.work, PAGE_SENTINEL_NAME)
        result = _run_bash(self.recipe, self.work, sentinels, self.tmp)
        self.assertEqual(result.watch.sentinel_changes, [], _failure_detail(result))
        _assert_edges(self, result.watch.rasters)
        if result.returncode == 0:
            self.assertGreaterEqual(len(result.watch.rasters), 1, _failure_detail(result))
        else:
            self.assertEqual(result.watch.rasters, {}, _failure_detail(result))
            self.assertEqual(result.watch.pdfs, [], _failure_detail(result))


if __name__ == "__main__":
    unittest.main()
