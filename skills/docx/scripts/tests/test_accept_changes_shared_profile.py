"""Regression tests for the shared LibreOffice profile used by accept_changes.py.

A local user can plant /tmp/libreoffice_docx_profile before the victim runs
``python3 skills/docx/scripts/accept_changes.py``. The script treats an existing
``Module1.xba`` as trusted when the text contains ``AcceptAllTrackedChanges``,
then starts soffice with that profile and runs
``Standard.Module1.AcceptAllTrackedChanges``. These tests plant that profile as
a different user, with a sticky world-writable tree and a read-only macro the
victim cannot replace, and require both of the following:

- the planted StarBasic does not run (it would create a sentinel file);
- tracked changes are still accepted, so refusing to run LibreOffice is not a fix;
- the planted profile is not modified or removed.

Re-reading the same shared path and accepting it again does not satisfy the
test: the planted module contains the macro name and a ``Shell`` call, and the
victim cannot overwrite or unlink it.

The tests fail while that behavior is present. Run them from the repository root:

    python3 skills/docx/scripts/tests/test_accept_changes_shared_profile.py -v

LibreOffice (``soffice``) must be on ``PATH``. Creating the other user needs
passwordless sudo.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
ACCEPT_CHANGES = REPO_ROOT / "skills" / "docx" / "scripts" / "accept_changes.py"
ATTACKER = "docxprofileatk"
SHARED_PROFILE = Path("/tmp/libreoffice_docx_profile")
INSERTED = "INSERTED_MARKER"
DELETED = "DELETED_MARKER"


def _sudo(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *args], check=check, **kwargs)


def _tree_manifest(root: Path) -> dict[str, tuple]:
    """Content and permission snapshot. Directory mtimes are ignored."""
    manifest: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            manifest[rel] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            manifest[rel] = ("dir", mode)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest[rel] = ("file", mode, digest)
        else:
            manifest[rel] = ("other", mode)
    return manifest


def _manifest_diff(before: dict[str, tuple], after: dict[str, tuple]) -> str:
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            lines.append(f"{key}: {before.get(key)!r} -> {after.get(key)!r}")
    if len(lines) > 12:
        hidden = len(lines) - 12
        lines = lines[:12] + [f"... {hidden} more paths changed"]
    return "\n".join(lines)


class SharedLibreOfficeProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("soffice") is None:
            raise AssertionError("soffice is not on PATH; LibreOffice is required")
        if not ACCEPT_CHANGES.is_file():
            raise AssertionError(f"missing {ACCEPT_CHANGES}")
        probe = _sudo("true", check=False, capture_output=True, text=True)
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout or "").strip()
            raise AssertionError(f"passwordless sudo is required to plant the other user's profile: {detail}")
        try:
            pwd.getpwnam(ATTACKER)
        except KeyError:
            _sudo("useradd", "-m", "-s", "/bin/bash", ATTACKER)

    def test_planted_shared_profile_macro_does_not_run(self) -> None:
        """Victim accept-changes must ignore an attacker-owned shared profile.

        The planted module is immutable to the victim (other owner, mode 0444,
        parent directories sticky). A fix that checks this path again and then
        launches soffice against it still runs the planted Shell. A fix that
        only refuses setup leaves the tracked change in place and fails too.
        """
        work = Path(tempfile.mkdtemp(prefix="accept-changes-profile-"))
        sentinel = work / "planted-macro-ran"
        profile = SHARED_PROFILE
        source = work / "tracked.docx"
        output = work / "accepted.docx"
        try:
            self._stop_profile_soffice(profile)
            self._plant_profile(profile, sentinel)
            macro_text = (profile / "user" / "basic" / "Standard" / "Module1.xba").read_text()
            self.assertIn("AcceptAllTrackedChanges", macro_text)
            self.assertIn(str(sentinel), macro_text)
            self._make_tracked_docx(source, work / "convert-profile")
            before = _tree_manifest(profile)

            proc = subprocess.run(
                [sys.executable, str(ACCEPT_CHANGES), str(source), str(output)],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=work,
            )
            failures: list[str] = []
            if sentinel.exists():
                failures.append(
                    "planted StarBasic ran as the victim and created "
                    f"{sentinel}"
                )
            after = _tree_manifest(profile)
            if after != before:
                failures.append(
                    "shared profile was modified or replaced while accepting changes:\n"
                    + _manifest_diff(before, after)
                )
            if proc.returncode != 0 or not proc.stdout.startswith("Successfully"):
                failures.append(
                    "accept_changes did not accept the document "
                    f"(exit {proc.returncode}): stdout={proc.stdout!r} stderr={proc.stderr!r}"
                )
            if not output.is_file():
                failures.append("accept_changes did not leave an output document")
            else:
                document = self._document_xml(output)
                if INSERTED not in document or DELETED in document:
                    failures.append(
                        "tracked changes were not accepted; output document.xml is:\n"
                        + document
                    )
            self.assertFalse(failures, "\n\n".join(failures))
        finally:
            self._stop_profile_soffice(profile)
            _sudo("rm", "-rf", str(profile), check=False)
            shutil.rmtree(work, ignore_errors=True)

    def _plant_profile(self, profile: Path, sentinel: Path) -> None:
        _sudo("rm", "-rf", str(profile))
        home = pwd.getpwnam(ATTACKER).pw_dir
        bootstrap = _sudo(
            "-u",
            ATTACKER,
            "env",
            "SAL_USE_VCLPLUGIN=svp",
            f"HOME={home}",
            "soffice",
            "--headless",
            "--norestore",
            "--terminate_after_init",
            f"-env:UserInstallation=file://{profile}",
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if bootstrap.returncode != 0:
            self.fail(
                "attacker could not bootstrap the shared profile "
                f"(exit {bootstrap.returncode}): {bootstrap.stderr}"
            )
        # LibreOffice creates the profile owner-readable only. Open it up
        # before the victim process inspects or later runs soffice.
        _sudo("chmod", "-R", "a+rwX", str(profile))
        macro = profile / "user" / "basic" / "Standard" / "Module1.xba"
        if not macro.is_file():
            self.fail(f"bootstrap did not create {macro}")
        library = profile / "user" / "basic" / "Standard" / "script.xlb"
        library_text = library.read_text()
        if 'library:name="Module1"' not in library_text:
            self.fail(f"bootstrap library does not list Module1: {library_text}")

        quote = '"'
        shell = (
            f"Shell({quote}/usr/bin/touch{quote}, 1, {quote}{sentinel}{quote}, True)"
        )
        payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" script:language="StarBasic">
    Sub AcceptAllTrackedChanges()
        {shell}
        Dim desktop As Object
        desktop = createUnoService("com.sun.star.frame.Desktop")
        desktop.terminate()
    End Sub
</script:module>
"""
        staged = sentinel.parent / "Module1.xba"
        staged.write_text(payload)
        _sudo("cp", str(staged), str(macro))
        _sudo("chown", f"{ATTACKER}:{ATTACKER}", str(macro))
        # Victim LibreOffice must be able to write locks and registry files.
        # The macro itself stays read-only, and sticky directories stop the
        # victim from unlinking it and writing a replacement under the same name.
        _sudo("chmod", "-R", "a+rwX", str(profile))
        _sudo("find", str(profile), "-type", "d", "-exec", "chmod", "1777", "{}", ";")
        _sudo("chmod", "0444", str(macro))

        macro_stat = macro.stat()
        if macro_stat.st_uid == os.getuid():
            self.fail("planted macro is owned by the victim")
        if stat.S_IMODE(macro_stat.st_mode) != 0o444:
            self.fail(f"planted macro mode is {oct(stat.S_IMODE(macro_stat.st_mode))}, expected 0444")
        with self.assertRaises(PermissionError):
            macro.write_text("overwrite")
        with self.assertRaises(PermissionError):
            macro.unlink()
        reread = macro.read_text()
        if reread != payload:
            self.fail("victim could change the planted macro during setup")

    def _make_tracked_docx(self, destination: Path, convert_profile: Path) -> None:
        plain = destination.parent / "plain.txt"
        plain.write_text("hello tracked\n")
        converted = destination.parent / "plain.docx"
        convert = subprocess.run(
            [
                "soffice",
                "--headless",
                "--norestore",
                f"-env:UserInstallation=file://{convert_profile}",
                "--convert-to",
                "docx",
                "--outdir",
                str(destination.parent),
                str(plain),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"},
        )
        if convert.returncode != 0 or not converted.is_file():
            self.fail(f"could not build a fixture docx: {convert.stderr}")
        with zipfile.ZipFile(converted) as archive:
            names = archive.namelist()
            files = {name: archive.read(name) for name in names}
        document = files["word/document.xml"].decode()
        match = re.search(
            r"<w:r\b[^>]*>.*?<w:t[^>]*>hello tracked</w:t>.*?</w:r>",
            document,
            flags=re.DOTALL,
        )
        if match is None:
            self.fail(f"fixture document has no replaceable run:\n{document}")
        replacement = (
            f'<w:ins w:id="1" w:author="Editor" w:date="2024-01-01T00:00:00Z">'
            f"<w:r><w:t>{INSERTED}</w:t></w:r></w:ins>"
            f'<w:del w:id="2" w:author="Editor" w:date="2024-01-01T00:00:00Z">'
            f"<w:r><w:delText>{DELETED}</w:delText></w:r></w:del>"
        )
        files["word/document.xml"] = document.replace(match.group(0), replacement, 1).encode()
        with zipfile.ZipFile(destination, "w") as archive:
            for name in names:
                archive.writestr(name, files[name])

    @staticmethod
    def _document_xml(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            return archive.read("word/document.xml").decode()

    @staticmethod
    def _stop_profile_soffice(profile: Path) -> None:
        subprocess.run(
            ["pkill", "-f", f"UserInstallation=file://{profile}"],
            check=False,
            capture_output=True,
        )


if __name__ == "__main__":
    unittest.main()
