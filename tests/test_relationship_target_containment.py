"""Host-file oracle in Office relationship target checks.

The validator joins each internal relationship Target to the unpacked
package, resolves it, and calls exists and is_file. These tests require
that a target whose resolved path is outside the unpacked package be
rejected as a package reference before that lookup, so the result does not
depend on whether the host path is a regular file, a directory, or absent.

They fail while the lookup result is still visible in the validator output.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = ("pptx", "docx", "xlsx")
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
PLACEHOLDER = "placeholder\n"


def _load_base_validator(skill: str):
    office = REPO / "skills" / skill / "scripts" / "office"
    helpers_name = f"_rel_helpers_{skill}"
    helpers_spec = importlib.util.spec_from_file_location(
        helpers_name, office / "helpers" / "__init__.py"
    )
    helpers = importlib.util.module_from_spec(helpers_spec)
    sys.modules[helpers_name] = helpers
    saved = sys.modules.get("helpers")
    sys.modules["helpers"] = helpers
    try:
        helpers_spec.loader.exec_module(helpers)
        base_name = f"_rel_base_{skill}"
        base_spec = importlib.util.spec_from_file_location(
            base_name, office / "validators" / "base.py"
        )
        module = importlib.util.module_from_spec(base_spec)
        sys.modules[base_name] = module
        base_spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("helpers", None)
        else:
            sys.modules["helpers"] = saved
    return module.BaseSchemaValidator


VALIDATORS = {skill: _load_base_validator(skill) for skill in SKILLS}


def _rels_xml(relationships: list[tuple[str, str, str, str]]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Relationships xmlns="{REL_NS}">',
    ]
    for rid, rel_type, target, mode in relationships:
        mode_attr = f' TargetMode="{mode}"' if mode else ""
        lines.append(
            f'  <Relationship Id="{rid}" Type="{rel_type}" Target="{target}"{mode_attr}/>'
        )
    lines.append("</Relationships>")
    return "\n".join(lines) + "\n"


def _write_rels(path: Path, relationships: list[tuple[str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_rels_xml(relationships), encoding="utf-8")


def _climb(base_dir: Path, outside: Path, extra_ups: int = 8) -> str:
    """Parent-segment target that resolves to `outside`, with extra ups past root."""
    base_dir = base_dir.resolve()
    outside = outside.resolve()
    ups = len(base_dir.parts) - 1 + extra_ups
    relative = "../" * ups + outside.as_posix().lstrip("/")
    if (base_dir / relative).resolve() != outside:
        raise AssertionError(f"{relative} resolved to {(base_dir / relative).resolve()}, not {outside}")
    return relative


def _lexical_inside(root: Path, candidate: Path) -> bool:
    root_s = os.path.abspath(root)
    cand = os.path.normpath(os.path.abspath(candidate))
    try:
        Path(cand).relative_to(root_s)
    except ValueError:
        return False
    return True


@contextmanager
def _forbid_outside_stat(root: Path):
    """Record exists/is_file calls whose path is lexically outside `root`.

    Path.resolve() stats while it normalizes. The check under test is the
    later exists/is_file branch, so those two are the calls that must not
    see an outside path.
    """
    root = root.resolve()
    seen: list[str] = []
    original_exists = Path.exists
    original_is_file = Path.is_file
    original_os_exists = os.path.exists
    original_os_isfile = os.path.isfile

    def note(candidate) -> None:
        if not _lexical_inside(root, Path(candidate)):
            seen.append(os.path.abspath(candidate))

    def path_exists(self, *, follow_symlinks=True):
        note(self)
        return original_exists(self, follow_symlinks=follow_symlinks)

    def path_is_file(self):
        note(self)
        return original_is_file(self)

    def os_exists(path):
        note(path)
        return original_os_exists(path)

    def os_isfile(path):
        note(path)
        return original_os_isfile(path)

    Path.exists = path_exists
    Path.is_file = path_is_file
    os.path.exists = os_exists
    os.path.isfile = os_isfile
    try:
        yield seen
    finally:
        Path.exists = original_exists
        Path.is_file = original_is_file
        os.path.exists = original_os_exists
        os.path.isfile = original_os_isfile


def _run_references(validator_cls, deck: Path):
    stdout = io.StringIO()
    with _forbid_outside_stat(deck) as probes:
        with redirect_stdout(stdout):
            ok = validator_cls(deck).validate_file_references()
    return ok, stdout.getvalue(), list(probes)


def _set_host_path(path: Path, kind: str) -> None:
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    if kind == "file":
        path.write_text(PLACEHOLDER, encoding="utf-8")
    elif kind == "dir":
        path.mkdir()
    elif kind == "absent":
        return
    else:
        raise AssertionError(kind)


class RelationshipTargetContainmentTests(unittest.TestCase):
    def _assert_same_rejection(self, validator_cls, build, outside: Path):
        """Three host states of one target must produce the same rejection."""
        results = []
        for kind in ("file", "dir", "absent"):
            _set_host_path(outside, kind)
            deck, target = build()
            ok, stdout, probes = _run_references(validator_cls, deck)
            self.assertNotIn(PLACEHOLDER.strip(), stdout)
            self.assertEqual(
                probes,
                [],
                f"{kind}: exists/is_file consulted a path outside the package",
            )
            self.assertFalse(ok, f"{kind}: outside target was accepted")
            self.assertIn(target, stdout, f"{kind}: outside target was not reported")
            results.append(stdout)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        if outside.exists():
            if outside.is_dir():
                outside.rmdir()
            else:
                outside.unlink()
        self.assertFalse(outside.exists())

    def test_relative_target_outside_package_does_not_reveal_host_file(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                root = Path(tempfile.mkdtemp())
                outside = root / "host-file-probe.txt"
                deck = root / "deck"
                base = deck / "ppt" / "slides"
                rels = base / "_rels" / "slide1.xml.rels"

                def build(base=base, rels=rels, outside=outside):
                    target = _climb(base, outside)
                    _write_rels(rels, [("rId1", IMAGE_REL, target, "")])
                    return deck, target

                self._assert_same_rejection(VALIDATORS[skill], build, outside)

    def test_package_absolute_target_outside_package_does_not_reveal_host_file(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                root = Path(tempfile.mkdtemp())
                outside = root / "host-file-probe.txt"
                deck = root / "deck"
                rels = deck / "ppt" / "slides" / "_rels" / "slide1.xml.rels"

                def build(deck=deck, rels=rels, outside=outside):
                    relative = _climb(deck, outside)
                    target = "/" + relative
                    joined = deck / target.lstrip("/")
                    if joined.resolve() != outside.resolve():
                        raise AssertionError(joined.resolve())
                    _write_rels(rels, [("rId1", IMAGE_REL, target, "")])
                    return deck, target

                self._assert_same_rejection(VALIDATORS[skill], build, outside)

    def test_root_rels_target_outside_package_does_not_reveal_host_file(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                root = Path(tempfile.mkdtemp())
                outside = root / "host-file-probe.txt"
                deck = root / "deck"
                rels = deck / "_rels" / ".rels"

                def build(deck=deck, rels=rels, outside=outside):
                    target = _climb(deck, outside)
                    if target.startswith("/"):
                        raise AssertionError(target)
                    _write_rels(rels, [("rId1", IMAGE_REL, target, "")])
                    return deck, target

                self._assert_same_rejection(VALIDATORS[skill], build, outside)

    def test_symlink_target_outside_package_does_not_reveal_host_file(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                root = Path(tempfile.mkdtemp())
                outside = root / "host-file-probe.txt"
                deck = root / "deck"
                base = deck / "ppt" / "slides"
                link = base / "alias"
                rels = base / "_rels" / "slide1.xml.rels"
                target = "alias"
                results = []
                for kind in ("file", "absent"):
                    _set_host_path(outside, "file" if kind == "file" else "absent")
                    if link.is_symlink() or link.exists():
                        link.unlink()
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(outside)
                    _write_rels(rels, [("rId1", IMAGE_REL, target, "")])
                    ok, stdout, probes = _run_references(VALIDATORS[skill], deck)
                    self.assertNotIn(PLACEHOLDER.strip(), stdout)
                    self.assertEqual(probes, [], f"{kind}: probed outside the package")
                    self.assertFalse(ok, f"{kind}: symlink target was accepted")
                    self.assertIn(target, stdout, f"{kind}: symlink target was not reported")
                    results.append(stdout)
                self.assertEqual(results[0], results[1])

    def test_parent_segments_that_stay_inside_remain_valid(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                root = Path(tempfile.mkdtemp())
                deck = root / "deck"
                base = deck / "ppt" / "slides"
                slide = base / "slide1.xml"
                slide.parent.mkdir(parents=True)
                slide.write_text("<p:sld/>", encoding="utf-8")
                inside = _climb(base, slide)
                self.assertIn("..", inside)
                _write_rels(
                    base / "_rels" / "slide1.xml.rels",
                    [("rId1", IMAGE_REL, inside, "")],
                )
                ok, stdout, probes = _run_references(VALIDATORS[skill], deck)
                self.assertEqual(probes, [])
                self.assertTrue(ok, stdout)
                self.assertNotIn("Broken reference", stdout)

    def test_external_relationship_is_not_a_host_probe(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                root = Path(tempfile.mkdtemp())
                outside = root / "host-file-probe.txt"
                outside.write_text(PLACEHOLDER, encoding="utf-8")
                deck = root / "deck"
                base = deck / "ppt" / "slides"
                slide = base / "slide1.xml"
                slide.parent.mkdir(parents=True)
                slide.write_text("<p:sld/>", encoding="utf-8")
                _write_rels(
                    base / "_rels" / "slide1.xml.rels",
                    [
                        ("rId1", IMAGE_REL, "slide1.xml", ""),
                        ("rId2", IMAGE_REL, outside.as_posix(), "External"),
                    ],
                )
                ok, stdout, probes = _run_references(VALIDATORS[skill], deck)
                self.assertEqual(probes, [])
                self.assertTrue(ok, stdout)
                self.assertNotIn(outside.name, stdout)

    def _pack(self, deck: Path, dest: Path) -> None:
        with zipfile.ZipFile(dest, "w") as archive:
            for path in sorted(deck.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(deck).as_posix())

    def _run_cli(self, skill: str, package: Path):
        script = REPO / "skills" / skill / "scripts" / "office" / "validate.py"
        completed = subprocess.run(
            [sys.executable, str(script), str(package)],
            cwd=script.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout + completed.stderr

    def test_cli_packed_deck_outside_target_does_not_reveal_host_file(self):
        cases = (
            ("pptx", ".pptx", Path("ppt/slides"), Path("ppt/slides/_rels/slide1.xml.rels")),
            ("docx", ".docx", Path("word"), Path("word/_rels/document.xml.rels")),
            ("xlsx", ".pptx", Path("ppt/slides"), Path("ppt/slides/_rels/slide1.xml.rels")),
        )
        for skill, suffix, base_rel, rels_rel in cases:
            with self.subTest(skill=skill, suffix=suffix):
                root = Path(tempfile.mkdtemp())
                outside = root / "host-file-probe.txt"
                deck = root / "deck"
                base = deck / base_rel
                target = _climb(base, outside)
                _write_rels(deck / rels_rel, [("rId1", IMAGE_REL, target, "")])
                package = root / f"deck{suffix}"
                self._pack(deck, package)
                outputs = []
                for kind in ("file", "dir", "absent"):
                    _set_host_path(outside, kind)
                    before = outside.read_bytes() if kind == "file" else None
                    code, output = self._run_cli(skill, package)
                    self.assertNotIn(PLACEHOLDER.strip(), output)
                    self.assertIn(target, output, f"{kind}: validator hid the outside target")
                    self.assertNotEqual(code, 0, f"{kind}: validator accepted the deck")
                    if kind == "file":
                        self.assertEqual(outside.read_bytes(), before)
                    outputs.append(output)
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(outputs[1], outputs[2])
                self.assertFalse(outside.exists())

    def test_cli_parent_segments_that_stay_inside_are_not_broken(self):
        root = Path(tempfile.mkdtemp())
        deck = root / "deck"
        base = deck / "ppt" / "slides"
        slide = base / "slide1.xml"
        slide.parent.mkdir(parents=True)
        slide.write_text("<p:sld/>", encoding="utf-8")
        target = _climb(base, slide)
        _write_rels(base / "_rels" / "slide1.xml.rels", [("rId1", IMAGE_REL, target, "")])
        package = root / "deck.pptx"
        self._pack(deck, package)
        _code, output = self._run_cli("pptx", package)
        self.assertNotIn(f"Broken reference to {target}", output)


if __name__ == "__main__":
    unittest.main()
