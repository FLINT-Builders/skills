"""Accept all tracked changes in a DOCX file using LibreOffice.

Requires LibreOffice (soffice) to be installed.
"""

import argparse
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from office.soffice import (
    UnsafeDocumentError,
    get_soffice_env,
    harden_profile,
    run_soffice,
    sanitized_copy,
)

logger = logging.getLogger(__name__)

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
) -> tuple[None, str]:
    input_path = Path(input_file)
    output_path = Path(output_file)

    if not input_path.exists():
        return None, f"Error: Input file not found: {input_file}"

    if not input_path.suffix.lower() == ".docx":
        return None, f"Error: Input file is not a DOCX file: {input_file}"

    with tempfile.TemporaryDirectory(
        prefix="accept_changes_", ignore_cleanup_errors=True
    ) as work_dir:
        work = Path(work_dir)
        source_dir = work / "source"
        source_dir.mkdir()
        try:
            document = sanitized_copy(input_path, source_dir, family="docx")
        except UnsafeDocumentError as e:
            return None, f"Error: {e}"

        profile = work / "profile"
        if not _setup_libreoffice_macro(profile):
            return None, "Error: Failed to setup LibreOffice macro"
        harden_profile(profile)

        cmd = [
            "soffice",
            "--headless",
            f"-env:UserInstallation={profile.as_uri()}",
            "--norestore",
            "vnd.sun.star.script:Standard.Module1.AcceptAllTrackedChanges?language=Basic&location=application",
            str(document),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=get_soffice_env(),
            )
        except subprocess.TimeoutExpired:
            result = None

        if result is not None and result.returncode != 0:
            return None, f"Error: LibreOffice failed: {result.stderr}"

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(document, output_path)
        except Exception as e:
            return None, f"Error: Failed to write output file: {e}"

    return (
        None,
        f"Successfully accepted all tracked changes: {input_file} -> {output_file}",
    )


def _setup_libreoffice_macro(profile: Path) -> bool:
    macro_dir = profile / "user" / "basic" / "Standard"

    try:
        run_soffice(
            [
                "--headless",
                f"-env:UserInstallation={profile.as_uri()}",
                "--terminate_after_init",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Failed to create LibreOffice profile: {e}")
    macro_dir.mkdir(parents=True, exist_ok=True)

    try:
        (macro_dir / "Module1.xba").write_text(ACCEPT_CHANGES_MACRO)
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

    _, message = accept_changes(args.input_file, args.output_file)
    print(message)

    if "Error" in message:
        raise SystemExit(1)
