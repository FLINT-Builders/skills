"""Fail-open timeout in the Word tracked-change acceptor.

These tests exercise the real ``accept_changes.py`` entry point. They are
expected to fail while a LibreOffice timeout (or a LibreOffice exit that
leaves revision markup in place) is reported as success and the copied
document is left at the output path.

Run from the repository root:

    python3 -m unittest skills/docx/tests/test_accept_changes_timeout_fail_open.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "skills" / "docx" / "scripts"
ACCEPT_CHANGES = SCRIPTS / "accept_changes.py"
PROFILE = Path("/tmp/libreoffice_docx_profile")

INSERTION = "REDLINE_INSERTION_DO_NOT_SHARE"
DELETION = "REDLINE_DELETION_DO_NOT_SHARE"
COMMENT = "REDLINE_COMMENT_DO_NOT_SHARE"
KEEP = "VISIBLE_KEEP_TEXT"
SUCCESS = "Successfully accepted all tracked changes"

WML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class AcceptChangesFailOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.argv_log = self.root / "soffice-argv.log"
        self.pid_file = self.root / "soffice.pid"
        self.shim_dir = self.root / "bin"
        self.shim_dir.mkdir()
        self._write_soffice_shim()
        self._clean_profile()
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.shim_dir) + os.pathsep + self.env.get("PATH", "")
        scripts = str(SCRIPTS)
        self.env["PYTHONPATH"] = scripts + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env["ACCEPT_CHANGES_TEST_ARGV_LOG"] = str(self.argv_log)
        self.env["ACCEPT_CHANGES_TEST_PID_FILE"] = str(self.pid_file)
        self.env["ACCEPT_CHANGES_TEST_MODE"] = "hang"

    def tearDown(self) -> None:
        self._kill_recorded_soffice()
        self._clean_profile()
        self._tmp.cleanup()

    def test_timeout_is_failure_and_does_not_publish_redlines(self) -> None:
        """The attack path: soffice does not exit within the script timeout.

        The script copies the supplied document to the output path, then waits
        on soffice. When that wait exceeds the hardcoded timeout, the run must
        not exit zero, must not print the success line, and must not leave the
        caller's output file (that file would still hold the tracked
        insertions, deletions, and comments).
        """
        proc, src, dst = self._run("hang", timeout=45)
        self._assert_accept_invoked(dst)
        self._assert_recorded_soffice_dead()
        self.assertEqual(src.read_bytes(), self._docx_bytes(), "input document was modified")
        self._assert_failed_closed(proc, dst)

    def test_clean_exit_with_revision_markup_is_failure(self) -> None:
        """Success is only valid once the output no longer contains markup.

        A soffice process that exits zero without accepting changes must not
        be reported as a clean copy. The output copied before the run still
        has the insertions, deletions, and comments.
        """
        proc, _src, dst = self._run("noop", timeout=20)
        self._assert_accept_invoked(dst)
        self._assert_failed_closed(proc, dst)

    def test_soffice_failure_does_not_publish_redlines(self) -> None:
        """A non-zero soffice exit is a refusal, so the copied output is gone.

        The copy happens before soffice is started. Leaving that copy at the
        output path publishes the redlines the caller asked to remove.
        """
        proc, _src, dst = self._run("fail", timeout=20)
        self._assert_accept_invoked(dst)
        self._assert_failed_closed(proc, dst)

    def test_success_when_soffice_removes_revision_markup(self) -> None:
        """The acceptor still reports success after markup is actually gone."""
        proc, src, dst = self._run("strip", timeout=20)
        self._assert_accept_invoked(dst)
        self.assertEqual(proc.returncode, 0, self._details(proc, dst))
        self.assertIn(SUCCESS, proc.stdout)
        self.assertTrue(dst.is_file(), self._details(proc, dst))
        self.assertFalse(self._contains_redlines(dst), self._details(proc, dst))
        self.assertIn(KEEP, self._package_text(dst))
        self.assertEqual(src.read_bytes(), self._docx_bytes())

    def _run(self, mode: str, timeout: int):
        self.env["ACCEPT_CHANGES_TEST_MODE"] = mode
        self.argv_log.write_text("")
        if self.pid_file.exists():
            self.pid_file.unlink()
        src = self.root / "in.docx"
        dst = self.root / "out" / "clean.docx"
        src.write_bytes(self._docx_bytes())
        self.assertTrue(self._contains_redlines(src))
        proc = subprocess.run(
            [sys.executable, str(ACCEPT_CHANGES), str(src), str(dst)],
            cwd=str(SCRIPTS),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc, src, dst

    def _assert_failed_closed(self, proc: subprocess.CompletedProcess, dst: Path) -> None:
        details = self._details(proc, dst)
        self.assertNotEqual(proc.returncode, 0, details)
        self.assertNotIn(SUCCESS, proc.stdout, details)
        self.assertFalse(dst.exists(), details)
        self.assertFalse(self._contains_redlines(dst), details)

    def _assert_accept_invoked(self, dst: Path) -> None:
        invocations = self._invocations()
        accept_calls = [
            args
            for args in invocations
            if any("AcceptAllTrackedChanges" in arg for arg in args)
            and any(arg == str(dst.absolute()) or arg == str(dst) for arg in args)
        ]
        self.assertTrue(
            accept_calls,
            f"accept-changes soffice was not started for {dst}; invocations={invocations}",
        )

    def _assert_recorded_soffice_dead(self) -> None:
        self.assertTrue(self.pid_file.is_file(), "hanging soffice did not record its pid")
        pid = int(self.pid_file.read_text().strip())
        self.assertFalse(_alive(pid), f"soffice pid {pid} is still running")

    def _details(self, proc: subprocess.CompletedProcess, dst: Path) -> str:
        return (
            f"returncode={proc.returncode}\n"
            f"stdout={proc.stdout!r}\n"
            f"stderr={proc.stderr!r}\n"
            f"output_exists={dst.exists()}\n"
            f"redlines={self._contains_redlines(dst)}\n"
            f"invocations={self._invocations()}"
        )

    def _invocations(self) -> list[list[str]]:
        if not self.argv_log.is_file():
            return []
        lines = [line for line in self.argv_log.read_text().splitlines() if line]
        return [json.loads(line) for line in lines]

    def _write_soffice_shim(self) -> None:
        shim = self.shim_dir / "soffice"
        shim.write_text(SHIM)
        shim.chmod(0o755)

    def _clean_profile(self) -> None:
        if PROFILE.exists():
            shutil.rmtree(PROFILE)

    def _kill_recorded_soffice(self) -> None:
        if not self.pid_file.exists():
            return
        try:
            pid = int(self.pid_file.read_text().strip())
        except ValueError:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            return

    @staticmethod
    def _docx_bytes() -> bytes:
        return build_redline_docx()

    @staticmethod
    def _contains_redlines(path: Path) -> bool:
        if not path.is_file():
            return False
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if any(name.endswith("comments.xml") for name in names):
                return True
            document = archive.read("word/document.xml").decode("utf-8")
            blob = "\n".join(archive.read(name).decode("utf-8", "replace") for name in names)
        if any(secret in blob for secret in (INSERTION, DELETION, COMMENT)):
            return True
        markup = ("<w:ins", "<w:del", "<w:delText", "<w:comment")
        return any(token in document for token in markup)

    @staticmethod
    def _package_text(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            return "\n".join(
                archive.read(name).decode("utf-8", "replace") for name in archive.namelist()
            )


def build_redline_docx() -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{WML}">
  <w:body>
    <w:p>
      <w:ins w:id="1" w:author="Redliner" w:date="2024-01-01T00:00:00Z">
        <w:r><w:t>{INSERTION}</w:t></w:r>
      </w:ins>
      <w:del w:id="2" w:author="Redliner" w:date="2024-01-01T00:00:00Z">
        <w:r><w:delText>{DELETION}</w:delText></w:r>
      </w:del>
      <w:r><w:t>{KEEP}</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:rPr>
        <w:del w:id="3" w:author="Redliner" w:date="2024-01-01T00:00:00Z"/>
      </w:rPr></w:pPr>
      <w:commentRangeStart w:id="0"/>
      <w:r><w:t>anchor</w:t></w:r>
      <w:commentRangeEnd w:id="0"/>
      <w:r><w:commentReference w:id="0"/></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    comments = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="{WML}">
  <w:comment w:id="0" w:author="Redliner" w:date="2024-01-01T00:00:00Z">
    <w:p><w:r><w:t>{COMMENT}</w:t></w:r></w:p>
  </w:comment>
</w:comments>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>
"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    doc_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>
"""
    import io

    buffer = io.BytesIO()
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_rels,
        "word/document.xml": document,
        "word/comments.xml": comments,
        "word/_rels/document.xml.rels": doc_rels,
    }
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            archive.writestr(info, payload)
    return buffer.getvalue()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


SHIM = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    """Stand-in soffice for accept_changes timeout tests.

    The product starts ``soffice`` and waits. This process is the wait target:
    profile init returns immediately, and the accept invocation follows
    ACCEPT_CHANGES_TEST_MODE (hang, noop, fail, strip). It does not reimplement
    LibreOffice's change-acceptance; strip mode only deletes the revision
    markup this test planted, so the success path has a real clean file.
    """
    import json
    import os
    import re
    import sys
    import time
    import zipfile
    from pathlib import Path

    SECRETS = (
        "REDLINE_INSERTION_DO_NOT_SHARE",
        "REDLINE_DELETION_DO_NOT_SHARE",
        "REDLINE_COMMENT_DO_NOT_SHARE",
    )

    def log_argv() -> None:
        path = os.environ["ACCEPT_CHANGES_TEST_ARGV_LOG"]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\n")

    def docx_arg() -> str:
        for arg in sys.argv[1:]:
            if arg.lower().endswith(".docx"):
                return arg
        sys.stderr.write("soffice shim: no docx argument\n")
        sys.exit(2)

    def strip_markup(path: str) -> None:
        source = Path(path)
        rewritten = source.with_suffix(".stripped.zip")
        with zipfile.ZipFile(source, "r") as incoming, zipfile.ZipFile(
            rewritten, "w", compression=zipfile.ZIP_DEFLATED
        ) as outgoing:
            for info in incoming.infolist():
                if info.filename.endswith("comments.xml"):
                    continue
                data = incoming.read(info.filename)
                if info.filename == "word/document.xml":
                    text = data.decode("utf-8")
                    text = re.sub(r"<w:ins\b[^>]*>|</w:ins>", "", text)
                    text = re.sub(r"<w:del\b[^>]*/>|<w:del\b[^>]*>|</w:del>", "", text)
                    text = re.sub(r"<w:delText\b[^>]*>.*?</w:delText>", "", text)
                    text = re.sub(
                        r"<w:commentRangeStart\b[^>]*/>|<w:commentRangeEnd\b[^>]*/>|<w:commentReference\b[^>]*/>",
                        "",
                        text,
                    )
                    for secret in SECRETS:
                        text = text.replace(secret, "")
                    data = text.encode("utf-8")
                outgoing.writestr(info, data)
        rewritten.replace(source)

    def main() -> None:
        log_argv()
        if any(arg == "--terminate_after_init" for arg in sys.argv[1:]):
            sys.exit(0)
        mode = os.environ.get("ACCEPT_CHANGES_TEST_MODE", "hang")
        if mode == "hang":
            Path(os.environ["ACCEPT_CHANGES_TEST_PID_FILE"]).write_text(str(os.getpid()))
            time.sleep(90)
            sys.exit(0)
        if mode == "noop":
            sys.exit(0)
        if mode == "fail":
            sys.stderr.write("libreoffice-failed\n")
            sys.exit(1)
        if mode == "strip":
            strip_markup(docx_arg())
            sys.exit(0)
        sys.stderr.write(f"unknown mode {mode}\n")
        sys.exit(2)

    if __name__ == "__main__":
        main()
    '''
).lstrip()


if __name__ == "__main__":
    unittest.main()
