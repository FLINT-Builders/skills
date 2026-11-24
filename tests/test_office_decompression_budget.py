"""Spec: Office unpackers must refuse decompression bombs before any write.

The pptx, xlsx, and docx copies of ``safe_extract`` extract every zip member
after only a path-traversal and symlink check. Thumbnail generation reads
``ppt/_rels/presentation.xml.rels`` and ``ppt/presentation.xml`` with
``ZipFile.read``, and ``_is_hidden`` streams a slide part until its first
start tag. None of those paths caps uncompressed size, entry count, or
compression ratio.

A later fix has to make every test in this file pass without loosening the
numbers below. Callers already treat ``ValueError`` as a bad package
(``validate.py``, ``add_slide.py``, ``comment.py``, ``merge_runs.py``), so
raising ``ValueError`` is enough for those CLIs. The refusal must happen
before a member is created and before its uncompressed bytes are allocated.
Deleting a member after writing it still fails: the tests watch the
destination with inotify.

Budgets (a tighter limit also passes):

- ``MAX_MEMBER_UNCOMPRESSED_BYTES`` — one member's declared uncompressed size
- ``MAX_TOTAL_UNCOMPRESSED_BYTES`` — sum of declared uncompressed sizes
- ``MAX_COMPRESSION_RATIO`` — ``file_size / max(compress_size, 1)``
- ``MAX_MEMBER_COUNT`` — number of central-directory entries
- ``MAX_READ_PEAK_BYTES`` — tracemalloc peak while thumbnail reads a package

If an archive exceeds any budget, refuse it before creating any of its
members, including members that would have fit on their own. Thumbnail reads
of the relationships part, the presentation part, and any slide part opened
to test ``show`` use the same member size and ratio limits, and must not
allocate the uncompressed member first.

Not covered here (they do not call this unpacker):

- ``skills/xlsx/scripts/recalc.py`` lists names, and only then may hand the
  file to ``openpyxl`` when an ``xl/externalLinks/`` entry exists.
- The ``zipfile.ZipFile.extractall`` one-liner in ``skills/pptx/SKILL.md`` is
  a documented shell snippet, not a function this suite imports.

Run (these fail until the budgets are enforced):

    python3 tests/test_office_decompression_budget.py
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path


MAX_MEMBER_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_MEMBER_COUNT = 256
# A tiny deck peaks near 128 KiB under Python 3.12 / defusedxml 0.7.1.
# Anything at or above this has started materialising a bomb.
MAX_READ_PEAK_BYTES = 512 * 1024

REPO = Path(__file__).resolve().parents[1]
SKILLS = ("pptx", "xlsx", "docx")
IN_CREATE = 0x00000100
IN_Q_OVERFLOW = 0x00004000
_EVENT = struct.Struct("iIII")

_libc = ctypes.CDLL(None, use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int


class CreateWatch:
    """Record inotify IN_CREATE names under a directory that already exists."""

    def __init__(self, directory: Path):
        self.fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        watch = _libc.inotify_add_watch(self.fd, os.fsencode(directory), IN_CREATE)
        if watch < 0:
            err = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(err, "inotify_add_watch")

    def created(self) -> list[str]:
        pending = b""
        while True:
            try:
                pending += os.read(self.fd, 65536)
            except BlockingIOError:
                break
        names: list[str] = []
        offset = 0
        while offset + _EVENT.size <= len(pending):
            _wd, mask, _cookie, length = _EVENT.unpack_from(pending, offset)
            offset += _EVENT.size
            raw = pending[offset : offset + length]
            offset += length
            if mask & IN_Q_OVERFLOW:
                raise AssertionError("inotify queue overflowed; bomb test is inconclusive")
            if mask & IN_CREATE:
                names.append(raw.split(b"\x00", 1)[0].decode())
        return names

    def close(self) -> None:
        os.close(self.fd)


def _zeros(n: int) -> bytes:
    return b"\x00" * n


def write_zip(path: Path, entries: list[tuple[str, bytes, int]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload, method in entries:
            info = zipfile.ZipInfo(filename=name)
            info.compress_type = method
            archive.writestr(info, payload)


def ratio_bomb() -> list[tuple[str, bytes, int]]:
    # Highly compressible, but under the per-member and total size caps.
    return [("bomb.bin", _zeros(1 << 20), zipfile.ZIP_DEFLATED)]


def member_bomb() -> list[tuple[str, bytes, int]]:
    # Stored, so the compression ratio stays 1. Declared size is over the cap
    # and still under the total cap.
    return [("bomb.bin", _zeros(MAX_MEMBER_UNCOMPRESSED_BYTES + 1), zipfile.ZIP_STORED)]


def total_bomb() -> list[tuple[str, bytes, int]]:
    chunk = MAX_TOTAL_UNCOMPRESSED_BYTES // 2 + 4096
    return [
        ("a.bin", _zeros(chunk), zipfile.ZIP_STORED),
        ("b.bin", _zeros(chunk), zipfile.ZIP_STORED),
    ]


def count_bomb() -> list[tuple[str, bytes, int]]:
    return [(f"p{i:04d}.bin", b"x", zipfile.ZIP_STORED) for i in range(MAX_MEMBER_COUNT + 1)]


def small_member() -> list[tuple[str, bytes, int]]:
    return [("hello.txt", b"hello office pack", zipfile.ZIP_STORED)]


def _shape(path: Path) -> tuple[list[int], list[float], int]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
    sizes = [info.file_size for info in infos]
    ratios = [info.file_size / max(info.compress_size, 1) for info in infos]
    return sizes, ratios, len(infos)


class OfficeDecompressionBudgetTest(unittest.TestCase):
    def _run(self, op: str, skill: str, archive: Path, watch: Path, unpacked: Path | None = None):
        argv = [sys.executable, str(Path(__file__).resolve()), "--worker", op, skill, str(archive), str(watch)]
        if unpacked is not None:
            argv.append(str(unpacked))
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(argv, capture_output=True, text=True, timeout=180, env=env, cwd=watch.parent)

    def _refuse(self, op: str, skill: str, entries: list[tuple[str, bytes, int]], suffix: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / f"pack{suffix}"
            watch = root / "unpack"
            watch.mkdir()
            out = root / f"out{suffix}"
            write_zip(archive, entries)
            watcher = CreateWatch(watch)
            try:
                proc = self._run(op, skill, archive, watch)
            finally:
                created = watcher.created()
                watcher.close()
            detail = f"{op} {skill}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            self.assertEqual(proc.returncode, 0, detail)
            self.assertIn("REACHED", proc.stdout, detail)
            self.assertEqual(created, [], detail)
            self.assertFalse(out.exists(), detail)

    def _allow_extract(self, op: str, skill: str, suffix: str):
        entries = small_member()
        payload = entries[0][1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / f"pack{suffix}"
            watch = root / "unpack"
            watch.mkdir()
            write_zip(archive, entries)
            watcher = CreateWatch(watch)
            try:
                proc = self._run(op, skill, archive, watch)
            finally:
                created = watcher.created()
                watcher.close()
            detail = f"{op} {skill}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            self.assertEqual(proc.returncode, 0, detail)
            self.assertIn("REACHED", proc.stdout, detail)
            self.assertEqual(created, ["hello.txt"], detail)
            self.assertEqual((watch / "hello.txt").read_bytes(), payload, detail)

    def _assert_ratio_only(self, path: Path):
        sizes, ratios, count = _shape(path)
        self.assertGreater(max(ratios), MAX_COMPRESSION_RATIO)
        self.assertLessEqual(max(sizes), MAX_MEMBER_UNCOMPRESSED_BYTES)
        self.assertLessEqual(sum(sizes), MAX_TOTAL_UNCOMPRESSED_BYTES)
        self.assertLessEqual(count, MAX_MEMBER_COUNT)

    def _assert_member_only(self, path: Path):
        sizes, ratios, count = _shape(path)
        self.assertGreater(max(sizes), MAX_MEMBER_UNCOMPRESSED_BYTES)
        self.assertLessEqual(max(ratios), MAX_COMPRESSION_RATIO)
        self.assertLessEqual(sum(sizes), MAX_TOTAL_UNCOMPRESSED_BYTES)
        self.assertLessEqual(count, MAX_MEMBER_COUNT)

    def _assert_total_only(self, path: Path):
        sizes, ratios, count = _shape(path)
        self.assertGreater(sum(sizes), MAX_TOTAL_UNCOMPRESSED_BYTES)
        self.assertLessEqual(max(sizes), MAX_MEMBER_UNCOMPRESSED_BYTES)
        self.assertLessEqual(max(ratios), MAX_COMPRESSION_RATIO)
        self.assertLessEqual(count, MAX_MEMBER_COUNT)

    def _assert_count_only(self, path: Path):
        sizes, ratios, count = _shape(path)
        self.assertGreater(count, MAX_MEMBER_COUNT)
        self.assertLessEqual(max(sizes), MAX_MEMBER_UNCOMPRESSED_BYTES)
        self.assertLessEqual(sum(sizes), MAX_TOTAL_UNCOMPRESSED_BYTES)
        self.assertLessEqual(max(ratios), MAX_COMPRESSION_RATIO)

    def test_fixture_shapes_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                (ratio_bomb, self._assert_ratio_only),
                (member_bomb, self._assert_member_only),
                (total_bomb, self._assert_total_only),
                (count_bomb, self._assert_count_only),
            )
            for builder, check in cases:
                path = root / builder.__name__
                # zipfile refuses to overwrite some names; use a .zip suffix via write
                archive = path.with_suffix(".zip")
                write_zip(archive, builder())
                check(archive)

    def test_safe_extract_allows_a_small_member(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self._allow_extract("safe-extract", skill, ".zip")

    def test_safe_extract_refuses_high_compression_ratio(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self._refuse("safe-extract", skill, ratio_bomb(), ".zip")

    def test_safe_extract_refuses_member_uncompressed_size(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self._refuse("safe-extract", skill, member_bomb(), ".zip")

    def test_safe_extract_refuses_total_uncompressed_size(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self._refuse("safe-extract", skill, total_bomb(), ".zip")

    def test_safe_extract_refuses_member_count(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self._refuse("safe-extract", skill, count_bomb(), ".zip")

    def test_validate_allows_a_small_member(self):
        for skill, suffix in (("pptx", ".pptx"), ("xlsx", ".xlsx"), ("docx", ".docx")):
            with self.subTest(skill=skill):
                self._allow_extract("validate", skill, suffix)

    def test_validate_refuses_each_budget(self):
        bombs = (
            ("ratio", ratio_bomb),
            ("member", member_bomb),
            ("total", total_bomb),
            ("count", count_bomb),
        )
        for skill, suffix in (("pptx", ".pptx"), ("xlsx", ".xlsx"), ("docx", ".docx")):
            for name, builder in bombs:
                with self.subTest(skill=skill, bomb=name):
                    self._refuse("validate", skill, builder(), suffix)

    def test_add_slide_allows_a_small_member_then_may_fail_later(self):
        self._allow_extract("add-slide", "pptx", ".pptx")

    def test_add_slide_refuses_each_budget(self):
        for name, builder in (
            ("ratio", ratio_bomb),
            ("member", member_bomb),
            ("total", total_bomb),
            ("count", count_bomb),
        ):
            with self.subTest(bomb=name):
                self._refuse("add-slide", "pptx", builder(), ".pptx")

    def test_comment_allows_a_small_member_then_may_fail_later(self):
        self._allow_extract("comment", "docx", ".docx")

    def test_comment_refuses_each_budget(self):
        for name, builder in (
            ("ratio", ratio_bomb),
            ("member", member_bomb),
            ("total", total_bomb),
            ("count", count_bomb),
        ):
            with self.subTest(bomb=name):
                self._refuse("comment", "docx", builder(), ".docx")

    def test_merge_runs_allows_a_small_member_then_may_fail_later(self):
        self._allow_extract("merge-runs", "docx", ".docx")

    def test_merge_runs_refuses_each_budget(self):
        for name, builder in (
            ("ratio", ratio_bomb),
            ("member", member_bomb),
            ("total", total_bomb),
            ("count", count_bomb),
        ):
            with self.subTest(bomb=name):
                self._refuse("merge-runs", "docx", builder(), ".docx")

    def test_original_unpack_paths_refuse_ratio_and_member_bombs(self):
        ops = ("original-errors", "slide-defects", "paragraphs", "redlining")
        for skill in SKILLS:
            for op in ops:
                for name, builder in (("ratio", ratio_bomb), ("member", member_bomb)):
                    with self.subTest(skill=skill, op=op, bomb=name):
                        self._refuse(op, skill, builder(), ".docx")

    def _thumbnail(self, entries: list[tuple[str, bytes, int]]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "deck.pptx"
            watch = root / "unpack"
            watch.mkdir()
            write_zip(archive, entries)
            proc = self._run("thumbnail", "pptx", archive, watch)
        detail = f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        self.assertEqual(proc.returncode, 0, detail)
        self.assertIn("REACHED", proc.stdout, detail)
        peak_lines = [line for line in proc.stdout.splitlines() if line.startswith("PEAK ")]
        self.assertEqual(len(peak_lines), 1, detail)
        peak = int(peak_lines[0].split()[1])
        return proc, peak, detail

    def test_thumbnail_allows_a_small_deck(self):
        proc, peak, detail = self._thumbnail(_minimal_deck())
        self.assertIn("RESULT accepted", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)

    def test_thumbnail_refuses_rels_ratio_bomb(self):
        entries = [("ppt/_rels/presentation.xml.rels", _zeros(1 << 20), zipfile.ZIP_DEFLATED)]
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "deck.pptx"
            write_zip(archive, entries)
            self._assert_ratio_only(archive)
        proc, peak, detail = self._thumbnail(entries)
        self.assertIn("RESULT refused", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)

    def test_thumbnail_refuses_rels_member_size(self):
        entries = [
            (
                "ppt/_rels/presentation.xml.rels",
                _zeros(MAX_MEMBER_UNCOMPRESSED_BYTES + 1),
                zipfile.ZIP_STORED,
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "deck.pptx"
            write_zip(archive, entries)
            self._assert_member_only(archive)
        proc, peak, detail = self._thumbnail(entries)
        self.assertIn("RESULT refused", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)

    def test_thumbnail_refuses_presentation_ratio_bomb(self):
        entries = [item for item in _minimal_deck() if item[0] != "ppt/presentation.xml"]
        entries.append(("ppt/presentation.xml", _zeros(1 << 20), zipfile.ZIP_DEFLATED))
        proc, peak, detail = self._thumbnail(entries)
        self.assertIn("RESULT refused", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)

    def test_thumbnail_refuses_presentation_member_size(self):
        entries = [item for item in _minimal_deck() if item[0] != "ppt/presentation.xml"]
        entries.append(
            (
                "ppt/presentation.xml",
                _zeros(MAX_MEMBER_UNCOMPRESSED_BYTES + 1),
                zipfile.ZIP_STORED,
            )
        )
        proc, peak, detail = self._thumbnail(entries)
        self.assertIn("RESULT refused", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)

    def test_thumbnail_refuses_slide_part_ratio_bomb(self):
        comment = b"<!--" + (b"A" * (1 << 20)) + b"--><p:sld></p:sld>"
        entries = _minimal_deck()
        entries.append(("ppt/slides/slide1.xml", comment, zipfile.ZIP_DEFLATED))
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "deck.pptx"
            write_zip(archive, entries)
            sizes, ratios, _count = _shape(archive)
            self.assertGreater(max(ratios), MAX_COMPRESSION_RATIO)
            self.assertLessEqual(max(sizes), MAX_MEMBER_UNCOMPRESSED_BYTES)
        proc, peak, detail = self._thumbnail(entries)
        self.assertIn("RESULT refused", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)

    def test_thumbnail_refuses_slide_part_member_size(self):
        # Stored (ratio 1) but valid comment text, so the slide scan must
        # read the whole member to reach the first start tag.
        opaque = bytes(0x41 + (byte % 26) for byte in os.urandom(MAX_MEMBER_UNCOMPRESSED_BYTES + 1))
        comment = b"<!--" + opaque + b"-->"
        entries = _minimal_deck()
        entries.append(("ppt/slides/slide1.xml", comment, zipfile.ZIP_STORED))
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "deck.pptx"
            write_zip(archive, entries)
            _sizes, ratios, _count = _shape(archive)
            self.assertLessEqual(max(ratios), MAX_COMPRESSION_RATIO)
            self.assertGreater(max(_sizes), MAX_MEMBER_UNCOMPRESSED_BYTES)
        proc, peak, detail = self._thumbnail(entries)
        self.assertIn("RESULT refused", proc.stdout, detail)
        self.assertLess(peak, MAX_READ_PEAK_BYTES, detail)


def _minimal_deck() -> list[tuple[str, bytes, int]]:
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>
"""
    presentation = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>
"""
    return [
        ("ppt/_rels/presentation.xml.rels", rels.encode(), zipfile.ZIP_STORED),
        ("ppt/presentation.xml", presentation.encode(), zipfile.ZIP_STORED),
    ]


def _office(skill: str) -> Path:
    return REPO / "skills" / skill / "scripts" / "office"


def _scripts(skill: str) -> Path:
    return REPO / "skills" / skill / "scripts"


def _anchor_temp(watch: str):
    import tempfile

    class AnchoredTemp:
        def __init__(self, *args, **kwargs):
            self.name = watch

        def cleanup(self):
            return None

        def __enter__(self):
            return self.name

        def __exit__(self, *args):
            return False

    tempfile.TemporaryDirectory = AnchoredTemp


def _worker(argv: list[str]) -> None:
    op, skill, archive, watch = argv[0], argv[1], argv[2], argv[3]
    unpacked = argv[4] if len(argv) > 4 else None
    archive_path = Path(archive)
    watch_path = Path(watch)

    if op == "safe-extract":
        sys.path.insert(0, str(_office(skill)))
        from helpers import safe_extract

        print("REACHED", flush=True)
        with zipfile.ZipFile(archive_path) as archive_file:
            safe_extract(archive_file, watch_path)
        print("RESULT accepted")
        return

    if op == "validate":
        sys.path.insert(0, str(_office(skill)))
        import validate as validate_mod

        _anchor_temp(watch)
        sys.argv = ["validate.py", archive]
        print("REACHED", flush=True)
        try:
            validate_mod.main()
        except SystemExit as exc:
            print(f"RESULT exit {exc.code}")
        else:
            print("RESULT accepted")
        return

    if op == "add-slide":
        sys.path.insert(0, str(_scripts(skill)))
        import add_slide

        _anchor_temp(watch)
        out = str(watch_path.parent / "out.pptx")
        sys.argv = ["add_slide.py", archive, "slide1.xml", "-o", out]
        print("REACHED", flush=True)
        add_slide.main()
        print("RESULT accepted")
        return

    if op == "comment":
        sys.path.insert(0, str(_scripts(skill)))
        import comment

        _anchor_temp(watch)
        out = str(watch_path.parent / "out.docx")
        sys.argv = ["comment.py", archive, "note", "-o", out]
        print("REACHED", flush=True)
        comment.main()
        print("RESULT accepted")
        return

    if op == "merge-runs":
        sys.path.insert(0, str(_scripts(skill)))
        import merge_runs

        _anchor_temp(watch)
        out = str(watch_path.parent / "out.docx")
        sys.argv = ["merge_runs.py", archive, "-o", out]
        print("REACHED", flush=True)
        merge_runs.main()
        print("RESULT accepted")
        return

    if op == "thumbnail":
        sys.path.insert(0, str(_scripts(skill)))
        import thumbnail

        print("REACHED", flush=True)
        tracemalloc.start()
        try:
            thumbnail.get_slide_info(archive_path)
        except Exception as exc:
            print(f"RESULT refused {type(exc).__name__}")
        else:
            print("RESULT accepted")
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"PEAK {peak}")
        return

    if op in {"original-errors", "slide-defects", "paragraphs", "redlining"}:
        sys.path.insert(0, str(_office(skill)))
        _anchor_temp(watch)
        unpacked_dir = Path(unpacked) if unpacked else watch_path.parent / "unpacked"
        unpacked_dir.mkdir(parents=True, exist_ok=True)
        document = unpacked_dir / "word" / "document.xml"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(
            "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"/>",
            encoding="utf-8",
        )
        if op == "original-errors":
            from validators.base import BaseSchemaValidator

            validator = BaseSchemaValidator(unpacked_dir, archive_path)
            print("REACHED", flush=True)
            validator._get_original_file_errors(document)
        elif op == "slide-defects":
            from validators.pptx import PPTXSchemaValidator

            validator = PPTXSchemaValidator(unpacked_dir, archive_path)
            print("REACHED", flush=True)
            validator._original_slide_defects(validator.schemas_dir / "unused.xsd")
        elif op == "paragraphs":
            from validators.docx import DOCXSchemaValidator

            validator = DOCXSchemaValidator(unpacked_dir, archive_path)
            print("REACHED", flush=True)
            validator.count_paragraphs_in_original()
        else:
            from validators.redlining import RedliningValidator

            validator = RedliningValidator(unpacked_dir, archive_path)
            print("REACHED", flush=True)
            validator.validate()
        print("RESULT accepted")
        return

    raise SystemExit(f"unknown op {op}")


def worker(argv: list[str]) -> None:
    try:
        _worker(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(f"RESULT refused exit {exc.code}")
        else:
            print("RESULT accepted")
    except Exception as exc:
        print(f"RESULT refused {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2:])
    else:
        unittest.main()
