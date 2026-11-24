"""Accept all tracked changes in a DOCX file using LibreOffice.

Requires LibreOffice (soffice) to be installed.
"""

import argparse
import io
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from office.soffice import get_soffice_env

logger = logging.getLogger(__name__)

LIBREOFFICE_PROFILE = "/tmp/libreoffice_docx_profile"
MACRO_DIR = f"{LIBREOFFICE_PROFILE}/user/basic/Standard"
ACCEPT_TIMEOUT = 30

WML_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "http://purl.oclc.org/ooxml/wordprocessingml/main",
    }
)

REVISION_ELEMENTS = frozenset(
    {
        "ins",
        "del",
        "delText",
        "delInstrText",
        "moveFrom",
        "moveTo",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "rPrChange",
        "pPrChange",
        "sectPrChange",
        "tblPrChange",
        "tblPrExChange",
        "tblGridChange",
        "trPrChange",
        "tcPrChange",
        "numberingChange",
        "cellIns",
        "cellDel",
        "cellMerge",
        "customXmlInsRangeStart",
        "customXmlInsRangeEnd",
        "customXmlDelRangeStart",
        "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart",
        "customXmlMoveToRangeEnd",
    }
)

ACCEPT_CHANGES_MACRO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" script:language="StarBasic">
    Sub AcceptAllTrackedChanges()
        Dim document As Object
        Dim dispatcher As Object

        document = ThisComponent.CurrentController.Frame
        dispatcher = createUnoService("com.sun.star.frame.DispatchHelper")

        dispatcher.executeDispatch(document, ".uno:AcceptAllTrackedChanges", "", 0, Array())
        ThisComponent.store()
        ThisComponent.close(True)
    End Sub
</script:module>"""


def accept_changes(
    input_file: str,
    output_file: str,
) -> tuple[bool, str]:
    input_path = Path(input_file)
    output_path = Path(output_file)

    if not input_path.exists():
        return False, f"Error: Input file not found: {input_file}"

    if not input_path.suffix.lower() == ".docx":
        return False, f"Error: Input file is not a DOCX file: {input_file}"

    if output_path.exists() and os.path.samefile(input_path, output_path):
        return False, f"Error: Output file must differ from the input file: {output_file}"

    if not _setup_libreoffice_macro():
        return False, "Error: Failed to setup LibreOffice macro"

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, output_path)
    except Exception as e:
        _remove(output_path)
        return False, f"Error: Failed to copy input file to output location: {e}"

    published = False
    try:
        error = _accept_in_place(output_path, input_path)
        if error:
            return False, f"Error: {error}; no clean copy was written to {output_file}"
        published = True
    finally:
        # The copy at output_path still holds every tracked change until
        # LibreOffice has rewritten it and the result has been verified.
        if not published:
            _remove(output_path)

    return (
        True,
        f"Successfully accepted all tracked changes: {input_file} -> {output_file}",
    )


def _accept_in_place(output_path: Path, input_path: Path) -> str | None:
    cmd = [
        "soffice",
        "--headless",
        f"-env:UserInstallation=file://{LIBREOFFICE_PROFILE}",
        "--norestore",
        "vnd.sun.star.script:Standard.Module1.AcceptAllTrackedChanges?language=Basic&location=application",
        str(output_path.absolute()),
    ]

    try:
        returncode, stderr = _run_soffice(cmd, ACCEPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"LibreOffice did not finish within {ACCEPT_TIMEOUT}s and was stopped"
    except OSError as e:
        return f"Could not start LibreOffice: {e}"

    if returncode != 0:
        return f"LibreOffice failed: {stderr.strip() or f'exit status {returncode}'}"

    try:
        accepted = output_path.read_bytes()
    except OSError as e:
        return f"Could not read LibreOffice output: {e}"

    try:
        remaining = _find_revision_markup(accepted)
    except Exception as e:
        return f"Could not verify LibreOffice output: {e}"
    if remaining:
        return f"LibreOffice left tracked changes in the document ({remaining})"

    # Publish exactly the bytes that were verified, so nothing that writes to
    # output_path after the check can change what the caller receives.
    try:
        _write_atomic(output_path, accepted, mode_from=input_path)
    except OSError as e:
        return f"Could not write output file: {e}"
    return None


def _find_revision_markup(docx: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".xml"):
                continue
            with archive.open(name) as part:
                for _, element in ET.iterparse(part, events=("start",)):
                    tag = element.tag
                    if not isinstance(tag, str) or not tag.startswith("{"):
                        continue
                    namespace, _, local = tag[1:].partition("}")
                    if namespace in WML_NAMESPACES and local in REVISION_ELEMENTS:
                        return f"<w:{local}> in {name}"
    return None


def _write_atomic(path: Path, data: bytes, mode_from: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        shutil.copymode(mode_from, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        _remove(tmp_path)
        raise


def _run_soffice(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run soffice in its own process group and kill the whole group on timeout.

    soffice forks helper processes (oosplash, soffice.bin) that keep running
    and hold the output pipes open after the direct child is killed.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=get_soffice_env(),
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        _kill_process_group(proc)
        raise
    _kill_process_group(proc)
    return proc.returncode, stderr or ""


def _kill_process_group(proc: subprocess.Popen) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
        proc.wait()


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Failed to remove {path}: {e}")


def _setup_libreoffice_macro() -> bool:
    macro_dir = Path(MACRO_DIR)
    macro_file = macro_dir / "Module1.xba"

    if macro_file.exists() and "AcceptAllTrackedChanges" in macro_file.read_text():
        return True

    if not macro_dir.exists():
        try:
            _run_soffice(
                [
                    "soffice",
                    "--headless",
                    f"-env:UserInstallation=file://{LIBREOFFICE_PROFILE}",
                    "--terminate_after_init",
                ],
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Failed to initialize LibreOffice profile: {e}")
        macro_dir.mkdir(parents=True, exist_ok=True)

    try:
        macro_file.write_text(ACCEPT_CHANGES_MACRO)
        return True
    except Exception as e:
        logger.warning(f"Failed to setup LibreOffice macro: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Accept all tracked changes in a DOCX file"
    )
    parser.add_argument("input_file", help="Input DOCX file with tracked changes")
    parser.add_argument(
        "output_file", help="Output DOCX file (clean, no tracked changes)"
    )
    args = parser.parse_args()

    ok, message = accept_changes(args.input_file, args.output_file)
    print(message)

    if not ok:
        raise SystemExit(1)
