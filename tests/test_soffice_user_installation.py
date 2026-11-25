"""Profile selection for the LibreOffice wrappers used by the document skills.

The docx, pptx, and xlsx copies of ``scripts/office/soffice.py`` are the same
program. ``run_soffice`` is supposed to open documents in a throwaway profile.
These tests fail while a caller-supplied ``-env:UserInstallation`` argument
suppresses that profile, and while a document path that starts with ``-`` is
forwarded to LibreOffice. They pass once every launch uses a fresh temporary
profile, caller-supplied UserInstallation arguments are removed before
``soffice`` starts, and a document path that starts with ``-`` is rejected
without starting ``soffice``.

``accept_changes.py`` and the second ``soffice`` invocation in ``recalc.py``
always put their own UserInstallation first and do not share this skip, so
they are not covered here. ``recalc.py``'s profile bootstrap does call
``run_soffice``; the ``run_soffice`` cases below cover that function.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
import urllib.parse
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "docx": REPO / "skills" / "docx" / "scripts" / "office" / "soffice.py",
    "pptx": REPO / "skills" / "pptx" / "scripts" / "office" / "soffice.py",
    "xlsx": REPO / "skills" / "xlsx" / "scripts" / "office" / "soffice.py",
}
SKILL_DIRS = {
    "docx": REPO / "skills" / "docx",
    "pptx": REPO / "skills" / "pptx",
}

# LibreOffice core 88289f1ea16f (master, 2026-11-25). The product does not pin
# a LibreOffice version; this is the launcher and bootstrap those sources use.
#
# desktop/scripts/soffice.sh:185 execs oosplash with "$@".
# desktop/unx/source/args.cxx:15-18 and :59-64 move every argument whose first
# five characters are "-env:" to the front, preserving their relative order,
# then desktop/unx/source/start.cxx:80-89 and :616 pass that list to soffice.bin.
# sal/rtl/bootstrap.cxx:154-197 records "-env:" and "/env:" NAME=VALUE pairs
# from the process arguments. sal/rtl/bootstrap.cxx:134-146 keeps the first
# value for a name. getAmbienceValue (bootstrap.cxx:614-615) consults those
# pairs before the environment, and getValue (bootstrap.cxx:537, then :567)
# consults them before the ini file. rtl::Bootstrap::getFrom
# (include/rtl/bootstrap.hxx:204-207) calls rtl_bootstrap_get_from_handle with
# override false (bootstrap.cxx:749-750). unotools
# Bootstrap::Impl::initUserInstallationData
# (unotools/source/config/bootstrap.cxx:646-650) stores that value as the
# user-profile URL.
# sal/rtl/cmdargs.cxx:65-77 drops those same pairs from the application
# argument list, so a separate document argument is still opened.
# desktop/source/app/cmdlineargs.cxx:559-570 treats "--convert-to" as a
# conversion and :684-686 appends each following non-option to the conversion
# list.


def _load_soffice(label: str):
    path = SCRIPTS[label]
    spec = importlib.util.spec_from_file_location(f"soffice_{label}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file_uri_to_path(url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "file":
        raise AssertionError(f"profile URL is not a file URI: {url}")
    return Path(urllib.parse.unquote(parsed.path))


def _is_env_assignment(arg: str) -> bool:
    """True when LibreOffice treats arg as a bootstrap -env:/env: assignment."""
    if not (arg.startswith("-") or arg.startswith("/")):
        return False
    if not arg[1:].startswith("env:"):
        return False
    return "=" in arg


def _oosplash_order(args: list[str]) -> list[str]:
    env_args = [arg for arg in args if arg.startswith("-env:")]
    rest = [arg for arg in args if not arg.startswith("-env:")]
    return env_args + rest


def effective_user_installation(args: list[str]) -> str | None:
    """First UserInstallation value soffice.bin would keep, or None."""
    for arg in _oosplash_order(args):
        if not _is_env_assignment(arg):
            continue
        name, value = arg[5:].split("=", 1)
        if name == "UserInstallation":
            return value
    return None


def conversion_documents(args: list[str]) -> list[str]:
    """Document operands ``--convert-to`` would still open."""
    filtered = [arg for arg in _oosplash_order(args) if not _is_env_assignment(arg)]
    converting = False
    documents: list[str] = []
    index = 0
    while index < len(filtered):
        arg = filtered[index]
        option = None
        if arg.startswith("--"):
            option = arg[2:]
        elif arg.startswith("-") and len(arg) > 2:
            option = arg[1:]
        if option == "convert-to":
            converting = True
            index += 2
            continue
        if option == "outdir":
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if converting:
            documents.append(arg)
        index += 1
    return documents


class SofficeProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="soffice_profile_test_")
        self.work = Path(self._tmp.name)
        self.attacker_profile = self.work / "attacker-lo-profile"
        self.attacker_profile.mkdir()
        self.marker = self.attacker_profile / "user" / "registrymodifications.xcu"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("attacker-profile-marker\n", encoding="utf-8")
        self.attacker_url = self.attacker_profile.as_uri()
        self.attacker_arg = f"-env:UserInstallation={self.attacker_url}"
        self.bin_dir = self.work / "bin"
        self.bin_dir.mkdir()
        self.log_path = self.work / "soffice-argv.json"
        stub = self.bin_dir / "soffice"
        stub.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, os, sys
                from pathlib import Path
                from urllib.parse import unquote, urlparse

                def existed(url):
                    parsed = urlparse(url)
                    if parsed.scheme != "file":
                        return False
                    return Path(unquote(parsed.path)).is_dir()

                profiles = {}
                for arg in sys.argv[1:]:
                    for prefix in ("-env:UserInstallation=", "/env:UserInstallation="):
                        if arg.startswith(prefix):
                            url = arg[len(prefix):]
                            profiles[url] = existed(url)
                with open(os.environ["SOFFICE_ARGV_LOG"], "w", encoding="utf-8") as handle:
                    json.dump({"argv": sys.argv, "profiles": profiles}, handle)
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self._saved_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + self._saved_path
        os.environ["SOFFICE_ARGV_LOG"] = str(self.log_path)
        self.modules = {label: _load_soffice(label) for label in SCRIPTS}

    def tearDown(self):
        os.environ["PATH"] = self._saved_path
        os.environ.pop("SOFFICE_ARGV_LOG", None)
        self._tmp.cleanup()

    def _read_log(self):
        if not self.log_path.exists():
            return None
        return json.loads(self.log_path.read_text(encoding="utf-8"))

    def _clear_log(self):
        self.log_path.unlink(missing_ok=True)

    def _attacker_marker_unchanged(self):
        self.assertTrue(self.marker.is_file())
        self.assertEqual(
            self.marker.read_text(encoding="utf-8"),
            "attacker-profile-marker\n",
        )

    def _assert_fresh_profile(self, record, document):
        argv = record["argv"]
        self.assertEqual(Path(argv[0]).name, "soffice")
        args = argv[1:]
        self.assertNotIn(self.attacker_arg, args)
        self.assertFalse(
            any(self.attacker_url in arg for arg in args),
            f"caller profile URL reached soffice: {args}",
        )
        chosen = effective_user_installation(args)
        self.assertIsNotNone(chosen, f"soffice was started without a profile: {args}")
        self.assertNotEqual(chosen, self.attacker_url)
        self.assertTrue(
            record["profiles"].get(chosen),
            f"temporary profile did not exist when soffice started: {chosen}",
        )
        profile_path = _file_uri_to_path(chosen)
        self.assertNotEqual(profile_path.resolve(), self.attacker_profile.resolve())
        tmp_root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(
            profile_path.resolve().is_relative_to(tmp_root),
            f"profile is not a temporary directory: {profile_path}",
        )
        self.assertFalse(
            profile_path.exists(),
            f"profile was not removed after the launch: {profile_path}",
        )
        self.assertIn(document, conversion_documents(args))
        self._attacker_marker_unchanged()

    def _run_cli(self, script: Path, args: list[str]):
        self._clear_log()
        env = os.environ.copy()
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_normal_conversion_uses_a_throwaway_profile(self):
        """A document name that is not an option still converts. Passes today."""
        for label, script in SCRIPTS.items():
            with self.subTest(script=label):
                result = self._run_cli(
                    script, ["--headless", "--convert-to", "pdf", "report.docx"]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self._read_log()
                self.assertIsNotNone(record)
                self._assert_fresh_profile(record, "report.docx")

    def test_cli_user_installation_argument_cannot_select_the_profile(self):
        """The documented conversion command, plus an attacker profile argument."""
        for label, script in SCRIPTS.items():
            with self.subTest(script=label):
                result = self._run_cli(
                    script,
                    [
                        "--headless",
                        "--convert-to",
                        "pdf",
                        self.attacker_arg,
                        "malicious.docx",
                    ],
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self._read_log()
                self.assertIsNotNone(record, "conversion did not start soffice")
                self._assert_fresh_profile(record, "malicious.docx")

    def test_run_soffice_drops_caller_user_installation(self):
        for label, module in self.modules.items():
            with self.subTest(script=label):
                self._clear_log()
                result = module.run_soffice(
                    [
                        "--headless",
                        "--convert-to",
                        "pdf",
                        self.attacker_arg,
                        "malicious.docx",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0)
                record = self._read_log()
                self.assertIsNotNone(record)
                self._assert_fresh_profile(record, "malicious.docx")

    def test_document_path_that_is_a_user_installation_option_is_rejected(self):
        """A relative document name that begins with the profile option."""
        for label, script in SCRIPTS.items():
            with self.subTest(script=label):
                self._clear_log()
                result = self._run_cli(
                    script,
                    ["--headless", "--convert-to", "pdf", self.attacker_arg],
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(
                    self._read_log(),
                    "soffice started for a document path that is an option",
                )
                self._attacker_marker_unchanged()

    def test_document_path_starting_with_a_dash_is_rejected(self):
        for label, module in self.modules.items():
            with self.subTest(script=label):
                self._clear_log()
                with self.assertRaises(Exception):
                    module.run_soffice(
                        ["--headless", "--convert-to", "pdf", "-quarterly-report.docx"]
                    )
                self.assertIsNone(self._read_log())
                self._attacker_marker_unchanged()

    def test_skill_command_does_not_split_a_hostile_relative_name_into_a_profile(self):
        """SKILL.md runs the wrapper with the document name unquoted.

        A relative name whose first shell word is the profile option and whose
        second word is the document must not select the attacker profile.
        """
        for label, skill_dir in SKILL_DIRS.items():
            with self.subTest(skill=label):
                self._clear_log()
                hostile_name = f"{self.attacker_arg} malicious.docx"
                command = (
                    f"exec {sys.executable} scripts/office/soffice.py "
                    f"--headless --convert-to pdf {hostile_name}"
                )
                result = subprocess.run(
                    ["bash", "-c", command],
                    cwd=skill_dir,
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self._read_log()
                self.assertIsNotNone(record)
                self._assert_fresh_profile(record, "malicious.docx")

    def test_thumbnail_convert_passes_a_relative_option_name_to_run_soffice(self):
        """``thumbnail.convert_to_images`` forwards ``str(pptx_path)`` unchanged."""
        scripts = str(REPO / "skills" / "pptx" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        self._install_import_stubs()
        import thumbnail  # noqa: WPS433  module under skills/pptx/scripts

        # A single path component: '/' cannot appear in a filename, so the
        # profile URL is percent-encoded. str(Path) still begins with the option.
        name = "-env:UserInstallation=file:%2F%2F%2Ftmp%2Fattacker-lo-profile"
        self.assertFalse(Path(name).is_absolute())
        self.assertTrue(name.startswith("-env:UserInstallation"))
        (self.work / name).write_bytes(b"placeholder")
        previous = os.getcwd()
        os.chdir(self.work)
        self._clear_log()
        try:
            with self.assertRaises(Exception):
                thumbnail.convert_to_images(Path(name), self.work / "thumbs")
        finally:
            os.chdir(previous)
        self.assertIsNone(
            self._read_log(),
            "thumbnail conversion started soffice with an option-shaped path",
        )
        self._attacker_marker_unchanged()

    def test_slash_env_user_installation_does_not_win(self):
        """LibreOffice also accepts ``/env:``. The temporary profile stays first."""
        module = self.modules["docx"]
        self._clear_log()
        slash_arg = f"/env:UserInstallation={self.attacker_url}"
        result = module.run_soffice(
            ["--headless", "--convert-to", "pdf", slash_arg, "report.docx"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        record = self._read_log()
        self.assertIsNotNone(record)
        chosen = effective_user_installation(record["argv"][1:])
        self.assertNotEqual(chosen, self.attacker_url)
        self.assertIn("report.docx", conversion_documents(record["argv"][1:]))
        self._attacker_marker_unchanged()

    def _install_import_stubs(self):
        """Allow ``thumbnail`` to import. ``convert_to_images`` must not use these."""

        def stub(name):
            module = types.ModuleType(name)

            def __getattr__(attr):
                child = types.ModuleType(f"{name}.{attr}")
                return child

            module.__getattr__ = __getattr__
            return module

        try:
            __import__("PIL")
        except ModuleNotFoundError:
            pil = stub("PIL")
            image = stub("PIL.Image")
            image.Image = type("Image", (), {})
            sys.modules["PIL"] = pil
            sys.modules["PIL.Image"] = image
            sys.modules["PIL.ImageDraw"] = stub("PIL.ImageDraw")
            sys.modules["PIL.ImageFont"] = stub("PIL.ImageFont")
            pil.Image = image
            pil.ImageDraw = sys.modules["PIL.ImageDraw"]
            pil.ImageFont = sys.modules["PIL.ImageFont"]
        try:
            __import__("defusedxml")
        except ModuleNotFoundError:
            xml = stub("defusedxml")
            sys.modules["defusedxml"] = xml
            sys.modules["defusedxml.minidom"] = stub("defusedxml.minidom")
            element_tree = stub("defusedxml.ElementTree")
            sys.modules["defusedxml.ElementTree"] = element_tree
            xml.minidom = sys.modules["defusedxml.minidom"]
            xml.ElementTree = element_tree


if __name__ == "__main__":
    unittest.main()
