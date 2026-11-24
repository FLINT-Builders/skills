"""Security spec for presentation thumbnail rasterization.

`thumbnail.py` turns a caller-supplied deck into one full-page JPEG per
slide by sending the whole file through LibreOffice and then `pdftoppm`
at a fixed 100 DPI, with no slide cap and no pixel cap. These tests are
the behavior a fix has to meet. They fail while that conversion still
rasterizes every page.

Limits (the spec, not a guess at a private constant in the script):

- At most ``MAX_SLIDES`` slides may be rasterized. A deck with more
  slides, counted in the PresentationML namespace whether or not the
  ``p:`` prefix is used, is refused before the process creates any file
  (no profile directory, no PDF, no JPEG).
- Every raster that is written, including a file created and removed
  before the process exits, must stay within ``PIXEL_BUDGET`` pixels
  (width times height). Pages whose media box would exceed that budget
  at the script's 100 DPI are skipped or downscaled first.
- The file that is converted is the file that was read to apply those
  limits. Replacing the path after the count and before LibreOffice
  opens it must not rasterize the replacement.

The visual-QA shell recipe in ``skills/pptx/SKILL.md`` ("Converting to
Images") and the similar recipe in ``skills/docx/SKILL.md`` are agent
instructions, not functions this suite runs.
"""

from __future__ import annotations

import ctypes
import os
import select
import shutil
import struct
import subprocess
import sys
import textwrap
import threading
import zipfile
from pathlib import Path

import pytest
from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[1]
THUMBNAIL = SCRIPTS / "thumbnail.py"

# A template-layout grid, not an unbounded render. 41 slides is the first
# deck the script must refuse. Widescreen 10" x 5.625" at 100 DPI is
# 1000 x 563, so ordinary slides fit. A 30" x 30" slide at that DPI is
# 9_000_000 pixels and must not be written at full resolution.
MAX_SLIDES = 40
PIXEL_BUDGET = 4_000_000
EMU_PER_INCH = 914400
WIDE_CX = 10 * EMU_PER_INCH
WIDE_CY = int(5.625 * EMU_PER_INCH)
HUGE_INCHES = 30

RASTER_SUFFIXES = {".jpg", ".jpeg", ".png", ".ppm", ".pgm", ".pbm", ".tif", ".tiff"}

PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
MASTER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
LAYOUT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
THEME_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"

PRELOAD_C = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int (*real_open)(const char *, int, ...);
static int (*real_open64)(const char *, int, ...);
static int (*real_openat)(int, const char *, int, ...);
static int (*real_openat64)(int, const char *, int, ...);
static int (*real_close)(int);

static int logfd = -1;
static int swapped = 0;
static __thread int in_hook = 0;

static int caller_is_soffice(void) {
    FILE *f = fopen("/proc/self/cmdline", "re");
    if (!f) return 0;
    char buf[4096];
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    for (size_t i = 0; i < n; i++) if (buf[i] == '\0') buf[i] = ' ';
    buf[n] = '\0';
    return strstr(buf, "soffice") != NULL;
}

static void copy_overwrite(const char *src, const char *dst) {
    int in = real_open(src, O_RDONLY);
    if (in < 0) return;
    int out = real_open(dst, O_WRONLY | O_TRUNC);
    if (out < 0) out = real_open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (out < 0) { real_close(in); return; }
    char buf[1 << 16];
    ssize_t n;
    while ((n = read(in, buf, sizeof buf)) > 0) {
        ssize_t off = 0;
        while (off < n) {
            ssize_t w = write(out, buf + off, (size_t)(n - off));
            if (w < 0) { real_close(in); real_close(out); return; }
            off += w;
        }
    }
    real_close(in);
    real_close(out);
}

static void maybe_swap(const char *path) {
    if (in_hook || !path) return;
    const char *watch = getenv("THUMB_WATCH_PATH");
    const char *repl = getenv("THUMB_REPLACE_WITH");
    if (!watch || !repl || strcmp(path, watch) != 0) return;
    in_hook = 1;
    if (!swapped && caller_is_soffice()) {
        swapped = 1;
        copy_overwrite(repl, watch);
        if (logfd >= 0) dprintf(logfd, "SWAP %s\n", watch);
    }
    in_hook = 0;
}

static mode_t take_mode(int flags, va_list ap) {
    if (flags & (O_CREAT | O_TMPFILE)) return va_arg(ap, mode_t);
    return 0;
}

int open(const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags);
    mode_t mode = take_mode(flags, ap);
    va_end(ap);
    if (!in_hook) maybe_swap(path);
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags);
    mode_t mode = take_mode(flags, ap);
    va_end(ap);
    if (!in_hook) maybe_swap(path);
    return real_open64(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags);
    mode_t mode = take_mode(flags, ap);
    va_end(ap);
    if (!in_hook) maybe_swap(path);
    return real_openat(dirfd, path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...) {
    va_list ap; va_start(ap, flags);
    mode_t mode = take_mode(flags, ap);
    va_end(ap);
    if (!in_hook) maybe_swap(path);
    return real_openat64(dirfd, path, flags, mode);
}

__attribute__((constructor)) static void init(void) {
    real_open = dlsym(RTLD_NEXT, "open");
    real_open64 = dlsym(RTLD_NEXT, "open64");
    real_openat = dlsym(RTLD_NEXT, "openat");
    real_openat64 = dlsym(RTLD_NEXT, "openat64");
    real_close = dlsym(RTLD_NEXT, "close");
    const char *logp = getenv("THUMB_LOG");
    if (logp && real_open) logfd = real_open(logp, O_WRONLY | O_CREAT | O_APPEND, 0644);
}
"""

CONVERT_SCRIPT = textwrap.dedent(
    """\
    import sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import thumbnail
    thumbnail.convert_to_images(Path(sys.argv[2]), Path(sys.argv[3]))
    """
)


def make_pptx(
    path: Path,
    slides: int,
    *,
    width_emu: int = WIDE_CX,
    height_emu: int = WIDE_CY,
    namespace: str = "prefixed",
) -> None:
    """Build a deck LibreOffice will render.

    ``namespace``:
    - ``prefixed``: every ``sldId`` uses the ``p:`` prefix.
    - ``default``: the slide list uses the default PresentationML namespace,
      which ``getElementsByTagName("p:sldId")`` does not see.
    - ``mixed``: the first ``sldId`` is prefixed and the rest are not.
    """
    if slides < 1:
        raise ValueError("slides")
    if namespace not in {"prefixed", "default", "mixed"}:
        raise ValueError(namespace)

    def sld_tag(index: int) -> str:
        if namespace == "prefixed":
            return "p:sldId"
        if namespace == "default":
            return "sldId"
        return "p:sldId" if index == 0 else "sldId"

    sld_ids = []
    rels = [
        f'<Relationship Id="rId1" Type="{MASTER_REL}" Target="slideMasters/slideMaster1.xml"/>'
    ]
    overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
    ]
    parts: dict[str, str] = {}
    for i in range(slides):
        rid = f"rId{i + 2}"
        sld_ids.append(f'<{sld_tag(i)} id="{256 + i}" r:id="{rid}"/>')
        rels.append(f'<Relationship Id="{rid}" Type="{SLIDE_REL}" Target="slides/slide{i + 1}.xml"/>')
        overrides.append(
            f'<Override PartName="/ppt/slides/slide{i + 1}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
        parts[f"ppt/slides/slide{i + 1}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            f'xmlns:p="{PML}">'
            "<p:cSld><p:spTree>"
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            "<p:grpSpPr/>"
            "</p:spTree></p:cSld>"
            "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
            "</p:sld>"
        )
        parts[f"ppt/slides/_rels/slide{i + 1}.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{REL_NS}">'
            f'<Relationship Id="rId1" Type="{LAYOUT_REL}" Target="../slideLayouts/slideLayout1.xml"/>'
            "</Relationships>"
        )

    list_open = "<p:sldIdLst>" if namespace != "default" else "<sldIdLst>"
    list_close = "</p:sldIdLst>" if namespace != "default" else "</sldIdLst>"
    parts["ppt/presentation.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        f'xmlns:p="{PML}" xmlns="{PML}">'
        f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"{list_open}{''.join(sld_ids)}{list_close}"
        f'<p:sldSz cx="{width_emu}" cy="{height_emu}"/>'
        f'<p:notesSz cx="{6858000}" cy="{9144000}"/>'
        "</p:presentation>"
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">{"".join(rels)}</Relationships>'
    )
    parts["ppt/slideLayouts/slideLayout1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        f'xmlns:p="{PML}" type="blank" name="Blank">'
        '<p:cSld name="Blank"><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr/></p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{MASTER_REL}" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )
    parts["ppt/slideMasters/slideMaster1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        f'xmlns:p="{PML}">'
        "<p:cSld><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        "<p:grpSpPr/></p:spTree></p:cSld>"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
        'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
        'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        "</p:sldMaster>"
    )
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{LAYOUT_REL}" Target="../slideLayouts/slideLayout1.xml"/>'
        f'<Relationship Id="rId2" Type="{THEME_REL}" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )
    parts["ppt/theme/theme1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">'
        "<a:themeElements>"
        '<a:clrScheme name="Office">'
        '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
        '<a:dk2><a:srgbClr val="1F497D"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2>'
        '<a:accent1><a:srgbClr val="4F81BD"/></a:accent1><a:accent2><a:srgbClr val="C0504D"/></a:accent2>'
        '<a:accent3><a:srgbClr val="9BBB59"/></a:accent3><a:accent4><a:srgbClr val="8064A2"/></a:accent4>'
        '<a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6>'
        '<a:hlink><a:srgbClr val="0000FF"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink>'
        "</a:clrScheme>"
        '<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
        '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
        '<a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
        '<a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '<a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        '<a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
        "<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>"
        "<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>"
        '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
        "</a:fmtScheme></a:themeElements></a:theme>"
    )
    parts["[Content_Types].xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{''.join(overrides)}</Types>"
    )
    parts["_rels/.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{REL_NS}">'
        f'<Relationship Id="rId1" Type="{OFFICE_REL}" Target="ppt/presentation.xml"/>'
        "</Relationships>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            zf.writestr(name, body)


class Run:
    def __init__(
        self,
        returncode: int,
        created: list[str],
        rasters: list[Path],
        page_names: set[str],
        stdout: str,
        stderr: str,
        log: str,
    ):
        self.returncode = returncode
        self.created = created
        self.rasters = rasters
        self.page_names = page_names
        self.stdout = stdout
        self.stderr = stderr
        self.log = log

    def pixels(self) -> list[tuple[Path, int]]:
        found = []
        for path in self.rasters:
            with Image.open(path) as img:
                found.append((path, img.size[0] * img.size[1]))
        return found


# inotify_event header: wd, mask, cookie, len. File names are not observed
# through a libc interposer because pdftoppm writes them with fopen, which
# does not leave libc. The kernel queue still reports the create.
_IN_CREATE = 0x00000100
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_ISDIR = 0x40000000
_IN_CLOEXEC = 0x00080000
_WATCH = _IN_CREATE | _IN_CLOSE_WRITE | _IN_MOVED_TO


def _page_name(name: str) -> bool:
    lower = name.lower()
    return lower.startswith("slide-") and Path(lower).suffix in {".jpg", ".jpeg", ".png", ".ppm"}


_libc = ctypes.CDLL(None, use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int


class _SandboxWatch:
    """Record files created under a directory, including ones removed before exit."""

    def __init__(self, sandbox: Path, preserve: Path):
        self.sandbox = sandbox
        self.preserve = preserve
        self.created: list[str] = []
        self.rasters: list[Path] = []
        self.page_names: set[str] = set()
        self._seen: set[str] = set()
        self._copied: set[str] = set()
        self._copies = 0
        self._fd = -1
        self._watches: dict[int, Path] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _add(self, path: Path) -> None:
        if self._fd < 0:
            return
        wd = _libc.inotify_add_watch(self._fd, str(path).encode(), _WATCH)
        if wd >= 0:
            self._watches[wd] = path

    def _record(self, path: Path, copy: bool) -> None:
        resolved = str(path)
        with self._lock:
            if resolved not in self._seen:
                self._seen.add(resolved)
                self.created.append(resolved)
            if _page_name(path.name):
                self.page_names.add(path.name)
        if (
            copy
            and path.suffix.lower() in RASTER_SUFFIXES
            and resolved not in self._copied
            and path.is_file()
        ):
            self._copied.add(resolved)
            dest = self.preserve / f"{self._copies}-{path.name}"
            self._copies += 1
            try:
                shutil.copy2(path, dest)
            except OSError:
                return
            with self._lock:
                self.rasters.append(dest)

    def _note_existing(self, path: Path) -> None:
        if path.is_dir():
            self._add(path)
            try:
                children = list(path.iterdir())
            except OSError:
                return
            for child in children:
                self._note_existing(child)
        elif path.is_file():
            self._record(path, copy=path.suffix.lower() in RASTER_SUFFIXES)

    def _consume(self, block: bool) -> None:
        if self._fd < 0:
            return
        poller = select.poll()
        poller.register(self._fd, select.POLLIN)
        timeout = 200 if block else 0
        while True:
            ready = poller.poll(timeout)
            if not ready:
                return
            try:
                data = os.read(self._fd, 65536)
            except OSError:
                return
            offset = 0
            while offset + 16 <= len(data):
                wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                name = data[offset + 16 : offset + 16 + length].split(b"\x00", 1)[0]
                offset += 16 + length
                parent = self._watches.get(wd)
                if parent is None or not name:
                    continue
                path = parent / name.decode("utf-8", "surrogateescape")
                if mask & _IN_ISDIR and mask & (_IN_CREATE | _IN_MOVED_TO):
                    with self._lock:
                        if str(path) not in self._seen:
                            self._seen.add(str(path))
                            self.created.append(str(path))
                    self._note_existing(path)
                    continue
                if mask & (_IN_CREATE | _IN_MOVED_TO | _IN_CLOSE_WRITE):
                    self._record(path, copy=bool(mask & (_IN_CLOSE_WRITE | _IN_MOVED_TO)))
            timeout = 0

    def _loop(self) -> None:
        for path in (self.sandbox, *self.sandbox.rglob("*")):
            if path.is_dir():
                self._add(path)
        os.set_inheritable(self._fd, False)
        self._ready.set()
        while not self._stop.is_set():
            self._consume(block=True)
        self._consume(block=False)

    def __enter__(self) -> "_SandboxWatch":
        self._fd = _libc.inotify_init1(_IN_CLOEXEC)
        if self._fd < 0:
            raise OSError("inotify_init1 failed")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


@pytest.fixture(scope="session")
def preload(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("preload")
    source = work / "thumb_watch.c"
    binary = work / "thumb_watch.so"
    source.write_text(PRELOAD_C)
    compiled = subprocess.run(
        ["gcc", "-shared", "-fPIC", "-O2", "-o", str(binary), str(source), "-ldl"],
        capture_output=True,
        text=True,
    )
    if compiled.returncode != 0:
        pytest.fail(f"could not compile raster watcher:\n{compiled.stderr}")
    return binary


def _run(
    preload: Path,
    work: Path,
    pptx: Path,
    *,
    entry: str,
    replace_with: Path | None = None,
) -> Run:
    sandbox = work / "sandbox"
    (sandbox / "home").mkdir(parents=True)
    (sandbox / "tmp").mkdir()
    (sandbox / "out").mkdir()
    preserve = work / "preserve"
    preserve.mkdir()
    log_path = work / "creates.log"
    if entry == "main":
        command = [sys.executable, str(THUMBNAIL), str(pptx), str(sandbox / "out" / "grid")]
    elif entry == "convert_to_images":
        dest = sandbox / "tmp" / "dest"
        dest.mkdir()
        command = [sys.executable, "-c", CONVERT_SCRIPT, str(SCRIPTS), str(pptx), str(dest)]
    else:
        raise ValueError(entry)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(sandbox / "home"),
            "TMPDIR": str(sandbox / "tmp"),
            "XDG_CACHE_HOME": str(sandbox / "home" / "cache"),
            "XDG_CONFIG_HOME": str(sandbox / "home" / "config"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LD_PRELOAD": str(preload),
            "THUMB_LOG": str(log_path),
            "THUMB_SANDBOX": str(sandbox),
            "THUMB_PRESERVE": str(preserve),
        }
    )
    if replace_with is not None:
        env["THUMB_WATCH_PATH"] = str(pptx.resolve())
        env["THUMB_REPLACE_WITH"] = str(replace_with.resolve())
    with _SandboxWatch(sandbox, preserve) as watch:
        proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
    log = log_path.read_text() if log_path.exists() else ""
    # Files still present were created too, even if inotify missed the event.
    for path in sandbox.rglob("*"):
        if path.is_file():
            watch._record(path, copy=path.suffix.lower() in RASTER_SUFFIXES)
    return Run(
        proc.returncode,
        watch.created,
        watch.rasters,
        set(watch.page_names),
        proc.stdout,
        proc.stderr,
        log,
    )


def _detail(result: Run) -> str:
    preview = "\n".join(result.created[:30])
    return (
        f"exit={result.returncode}\n"
        f"created ({len(result.created)}):\n{preview}\n"
        f"pages ({len(result.page_names)}): {sorted(result.page_names)[:12]}\n"
        f"rasters ({len(result.rasters)}): {[p.name for p in result.rasters[:12]]}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n"
    )


def test_process_startup_creates_no_sandbox_files(tmp_path):
    """The file watch has to stay quiet when the product does not run.

    A refusal test that treated interpreter startup as a product write
    would fail for the wrong reason.
    """
    sandbox = tmp_path / "sandbox"
    (sandbox / "home").mkdir(parents=True)
    (sandbox / "tmp").mkdir()
    preserve = tmp_path / "preserve"
    preserve.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(sandbox / "home"), "TMPDIR": str(sandbox / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"})
    with _SandboxWatch(sandbox, preserve) as watch:
        proc = subprocess.run([sys.executable, "-c", "raise SystemExit(0)"], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert watch.created == []


def test_small_deck_still_thumbnails(preload, tmp_path):
    deck = tmp_path / "one.pptx"
    make_pptx(deck, 1)
    result = _run(preload, tmp_path / "run", deck, entry="main")
    assert result.returncode == 0, _detail(result)
    assert result.page_names, _detail(result)
    assert all(pixels <= PIXEL_BUDGET for _, pixels in result.pixels()), _detail(result)


def test_deck_at_slide_limit_is_thumbnailed(preload, tmp_path):
    deck = tmp_path / "at-limit.pptx"
    make_pptx(deck, MAX_SLIDES)
    result = _run(preload, tmp_path / "run", deck, entry="main")
    assert result.returncode == 0, _detail(result)
    assert len(result.page_names) == MAX_SLIDES, _detail(result)
    assert all(pixels <= PIXEL_BUDGET for _, pixels in result.pixels()), _detail(result)


def _assert_refused(result: Run) -> None:
    assert result.created == [], "conversion created files before refusing:\n" + _detail(result)
    assert result.rasters == [], "a raster was written before refusing:\n" + _detail(result)
    assert result.returncode != 0, _detail(result)


def test_slide_count_over_limit_is_refused_before_any_file_is_created(preload, tmp_path):
    deck = tmp_path / "over.pptx"
    make_pptx(deck, MAX_SLIDES + 1)
    result = _run(preload, tmp_path / "run", deck, entry="main")
    _assert_refused(result)


def test_default_namespace_over_limit_is_refused_before_any_file_is_created(preload, tmp_path):
    deck = tmp_path / "over-default-ns.pptx"
    make_pptx(deck, MAX_SLIDES + 1, namespace="default")
    result = _run(preload, tmp_path / "run", deck, entry="main")
    _assert_refused(result)


def test_mixed_namespace_over_limit_is_refused_before_any_file_is_created(preload, tmp_path):
    """One prefixed sldId plus enough unprefixed ones to pass the cap.

    A counter that only looks up ``p:sldId`` sees a single slide and would
    then convert the same path, which LibreOffice reads in full.
    """
    deck = tmp_path / "over-mixed-ns.pptx"
    make_pptx(deck, MAX_SLIDES + 1, namespace="mixed")
    result = _run(preload, tmp_path / "run", deck, entry="main")
    _assert_refused(result)


def _assert_within_pixel_budget(result: Run) -> None:
    """A skipped page refuses with no files. A downscaled page stays in budget.

    Writing the full-resolution raster and shrinking it afterwards still
    fails: the watcher copies every raster when it is closed.
    """
    sizes = result.pixels()
    if not sizes:
        assert result.returncode != 0 and result.created == [], (
            "oversized page was not refused and no raster was preserved\n" + _detail(result)
        )
        return
    over = [(path.name, count) for path, count in sizes if count > PIXEL_BUDGET]
    assert not over, f"raster exceeded {PIXEL_BUDGET} pixels: {over}\n" + _detail(result)


def test_oversized_slide_is_not_rasterized_beyond_pixel_budget(preload, tmp_path):
    deck = tmp_path / "huge.pptx"
    make_pptx(deck, 1, width_emu=HUGE_INCHES * EMU_PER_INCH, height_emu=HUGE_INCHES * EMU_PER_INCH)
    result = _run(preload, tmp_path / "run", deck, entry="main")
    _assert_within_pixel_budget(result)


def test_default_namespace_oversized_slide_is_not_rasterized_beyond_pixel_budget(preload, tmp_path):
    deck = tmp_path / "huge-default-ns.pptx"
    make_pptx(
        deck,
        1,
        width_emu=HUGE_INCHES * EMU_PER_INCH,
        height_emu=HUGE_INCHES * EMU_PER_INCH,
        namespace="default",
    )
    result = _run(preload, tmp_path / "run", deck, entry="main")
    _assert_within_pixel_budget(result)


def test_conversion_uses_the_deck_that_was_counted(preload, tmp_path):
    """Swap the file when soffice opens it, after the script has read it.

    The replacement has more slides than the cap. Rasterizing it means the
    count was applied to a different package than the one that was converted.
    """
    counted = tmp_path / "counted.pptx"
    replacement = tmp_path / "replacement.pptx"
    make_pptx(counted, 1)
    make_pptx(replacement, MAX_SLIDES + 1)
    result = _run(preload, tmp_path / "run", counted, entry="main", replace_with=replacement)
    assert result.returncode >= 0, "thumbnail crashed before conversion\n" + _detail(result)
    assert len(result.page_names) <= 1, (
        "replacement deck was rasterized after the slide count was read\n" + _detail(result)
    )
    over = [item for item in result.pixels() if item[1] > PIXEL_BUDGET]
    assert not over, _detail(result)


def test_convert_to_images_enforces_the_same_slide_cap(preload, tmp_path):
    deck = tmp_path / "over.pptx"
    make_pptx(deck, MAX_SLIDES + 1)
    result = _run(preload, tmp_path / "run", deck, entry="convert_to_images")
    _assert_refused(result)


def test_convert_to_images_enforces_the_same_namespace_independent_cap(preload, tmp_path):
    deck = tmp_path / "over-mixed.pptx"
    make_pptx(deck, MAX_SLIDES + 1, namespace="mixed")
    result = _run(preload, tmp_path / "run", deck, entry="convert_to_images")
    _assert_refused(result)


def test_convert_to_images_enforces_the_same_pixel_budget(preload, tmp_path):
    deck = tmp_path / "huge.pptx"
    make_pptx(deck, 1, width_emu=HUGE_INCHES * EMU_PER_INCH, height_emu=HUGE_INCHES * EMU_PER_INCH)
    result = _run(preload, tmp_path / "run", deck, entry="convert_to_images")
    _assert_within_pixel_budget(result)


def test_convert_to_images_uses_the_deck_that_was_counted(preload, tmp_path):
    counted = tmp_path / "counted.pptx"
    replacement = tmp_path / "replacement.pptx"
    make_pptx(counted, 1)
    make_pptx(replacement, MAX_SLIDES + 1)
    result = _run(
        preload,
        tmp_path / "run",
        counted,
        entry="convert_to_images",
        replace_with=replacement,
    )
    assert result.returncode >= 0, "thumbnail crashed before conversion\n" + _detail(result)
    assert len(result.page_names) <= 1, (
        "replacement deck was rasterized after the slide count was read\n" + _detail(result)
    )
