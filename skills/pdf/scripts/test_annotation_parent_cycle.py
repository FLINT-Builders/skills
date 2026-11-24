"""Cycle checks for PDF form-field parent walks.

The cases that build a cyclic /Parent chain are expected to FAIL while
get_full_annotation_field_id follows /Parent with no visited set. A fix
must reject those chains with ValueError (message containing "cyclic" and
"parent"), finish well under the timeout, stay within the memory bound, and
must not create the output file. Acyclic parent chains must still extract.

Run from the repository root:

    pip install 'pypdf==7.1.1'
    python3 skills/pdf/scripts/test_annotation_parent_cycle.py
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from extract_form_field_info import get_field_info  # noqa: E402

# Interpreter and pypdf import climb ~30MB in the first ~100ms. Count growth
# only after that, so a cycle that keeps allocating is distinct from startup.
# Measured on pypdf 7.1.1: a named self-parent keeps climbing; an unnamed
# self-parent stays flat and only burns CPU; a normal form has already exited.
STARTUP_SECONDS = 0.15
MAX_GROWTH_KB = 8 * 1024
CYCLE_TIMEOUT_SECONDS = 2.0


def _widget(name=None):
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4),
        }
    )
    if name is not None:
        widget[NameObject("/T")] = TextStringObject(name)
    return widget


def _add(writer, obj):
    return writer._add_object(obj)


def _acro_form(writer, fields):
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(fields)}
    )


def _ordinary_text_field(writer):
    """A /Fields entry with no /Parent. get_fields returns this and the
    annotation loop still runs afterwards."""
    return _add(
        writer,
        DictionaryObject(
            {
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject("name"),
            }
        ),
    )


def write_attack_pdf(path, kind):
    """Build the PDF the form workflow actually opens.

    `kind` selects the cycle shape. Every kind except `empty_fields` keeps
    one ordinary text field in /AcroForm /Fields so check_fillable_fields
    reports fillable fields and the documented workflow continues into
    extraction. The cyclic widget is only on the page /Annots array.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    field = _ordinary_text_field(writer)
    annotations = []

    if kind == "beside_legitimate":
        good = _widget("name")
        good[NameObject("/FT")] = NameObject("/Tx")
        good[NameObject("/Rect")] = ArrayObject(
            [NumberObject(10), NumberObject(20), NumberObject(110), NumberObject(40)]
        )
        good_ref = _add(writer, good)
        annotations.append(good_ref)
        # The ordinary /Fields entry is this widget, so the field is real.
        field = good_ref

    widget = _widget(None if kind == "self_unnamed" else "x")
    widget_ref = _add(writer, widget)
    annotations.append(widget_ref)

    if kind in ("self_named", "self_unnamed", "empty_fields", "beside_legitimate"):
        widget[NameObject("/Parent")] = widget_ref
    elif kind == "two_node":
        mid = _widget("mid")
        mid_ref = _add(writer, mid)
        widget[NameObject("/Parent")] = mid_ref
        mid[NameObject("/Parent")] = widget_ref
    elif kind == "ancestor":
        # widget -> left -> right -> left. The repeat is an ancestor, not the widget.
        left = _widget("left")
        right = _widget("right")
        left_ref = _add(writer, left)
        right_ref = _add(writer, right)
        widget[NameObject("/Parent")] = left_ref
        left[NameObject("/Parent")] = right_ref
        right[NameObject("/Parent")] = left_ref
    else:
        raise AssertionError(f"unknown pdf kind {kind}")

    page[NameObject("/Annots")] = ArrayObject(annotations)
    if kind == "empty_fields":
        _acro_form(writer, [])
    else:
        _acro_form(writer, [field])
    writer.write(path)


def write_nested_pdf(path):
    """Acyclic /Parent chain: widget name, parent person. Field id person.name."""
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    parent = DictionaryObject({NameObject("/T"): TextStringObject("person")})
    parent_ref = _add(writer, parent)
    widget = _widget("name")
    widget[NameObject("/FT")] = NameObject("/Tx")
    widget[NameObject("/Rect")] = ArrayObject(
        [NumberObject(10), NumberObject(20), NumberObject(30), NumberObject(40)]
    )
    widget[NameObject("/Parent")] = parent_ref
    widget_ref = _add(writer, widget)
    parent[NameObject("/Kids")] = ArrayObject([widget_ref])
    page[NameObject("/Annots")] = ArrayObject([widget_ref])
    _acro_form(writer, [parent_ref])
    writer.write(path)


def _vm_hwm_kb(pid):
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None
    return None


def _run_bounded(command, timeout):
    """Run command. Return (returncode, stdout, stderr, growth_kb, failure).

    failure is "timeout" or "memory" when the walk does not stop, else None.
    The child is killed on either failure. growth_kb is the VmHWM increase
    after STARTUP_SECONDS, so import cost is not counted.
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    base = None
    growth = 0
    failure = None
    try:
        while proc.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed >= STARTUP_SECONDS:
                hwm = _vm_hwm_kb(proc.pid)
                if hwm is not None:
                    if base is None:
                        base = hwm
                    growth = max(growth, hwm - base)
                    if growth > MAX_GROWTH_KB:
                        failure = "memory"
                        break
            if elapsed > timeout:
                failure = "timeout"
                break
            time.sleep(0.05)
        if failure is not None and proc.poll() is None:
            proc.kill()
    finally:
        if proc.poll() is None:
            proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    return proc.returncode, stdout, stderr, growth, failure


class ParentCycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def path(self, name):
        return os.path.join(self._tmp.name, name)

    def attack_pdf(self, kind):
        pdf_path = self.path(f"{kind}.pdf")
        write_attack_pdf(pdf_path, kind)
        return pdf_path

    def assert_cycle_rejected(self, command, output_path):
        returncode, _stdout, stderr, growth, failure = _run_bounded(
            command, CYCLE_TIMEOUT_SECONDS
        )
        self.assertFalse(
            os.path.exists(output_path),
            f"output was created before the cycle was rejected: {output_path}",
        )
        self.assertIsNone(
            failure,
            f"parent walk did not stop ({failure}, growth_kb={growth}). "
            f"stderr={stderr[:500]!r}",
        )
        self.assertLessEqual(growth, MAX_GROWTH_KB)
        self.assertNotEqual(returncode, 0, stderr.decode("utf-8", "replace"))
        decoded = stderr.decode("utf-8", "replace").lower()
        self.assertIn("valueerror", decoded)
        self.assertIn("cyclic", decoded)
        self.assertIn("parent", decoded)

    def worker(self, entry, pdf_path, output_path, fields_json=None):
        command = [
            sys.executable,
            os.path.abspath(__file__),
            "--worker",
            entry,
            pdf_path,
            output_path,
        ]
        if fields_json is not None:
            command.append(fields_json)
        self.assert_cycle_rejected(command, output_path)

    def test_self_parent_with_name_rejected_by_field_id_walk(self):
        pdf_path = self.attack_pdf("self_named")
        self.worker("direct", pdf_path, self.path("unused.json"))

    def test_self_parent_with_name_rejected_by_get_field_info(self):
        pdf_path = self.attack_pdf("self_named")
        self.worker("info", pdf_path, self.path("unused.json"))

    def test_self_parent_with_name_extract_script_writes_nothing(self):
        pdf_path = self.attack_pdf("self_named")
        output_path = self.path("fields.json")
        self.assert_cycle_rejected(
            [
                sys.executable,
                os.path.join(SCRIPTS, "extract_form_field_info.py"),
                pdf_path,
                output_path,
            ],
            output_path,
        )

    def test_self_parent_with_name_fill_script_writes_nothing(self):
        pdf_path = self.attack_pdf("self_named")
        fields_json = self.path("values.json")
        output_path = self.path("filled.pdf")
        with open(fields_json, "w", encoding="utf-8") as handle:
            json.dump(
                [{"field_id": "name", "page": 1, "value": "Ada"}],
                handle,
            )
        self.assert_cycle_rejected(
            [
                sys.executable,
                os.path.join(SCRIPTS, "fill_fillable_fields.py"),
                pdf_path,
                fields_json,
                output_path,
            ],
            output_path,
        )

    def test_self_parent_without_name_still_rejected(self):
        # No /T, so the name list does not grow, but /Parent still never ends.
        pdf_path = self.attack_pdf("self_unnamed")
        self.worker("info", pdf_path, self.path("unused.json"))

    def test_two_node_parent_cycle_rejected(self):
        pdf_path = self.attack_pdf("two_node")
        self.worker("info", pdf_path, self.path("unused.json"))

    def test_ancestor_parent_cycle_rejected(self):
        pdf_path = self.attack_pdf("ancestor")
        self.worker("info", pdf_path, self.path("unused.json"))

    def test_empty_fields_array_still_reaches_annotation_walk(self):
        # get_fields returns {} (truthy .items(), falsy as a check result).
        # The annotation loop still runs, so the same cycle must be rejected.
        pdf_path = self.attack_pdf("empty_fields")
        self.worker("info", pdf_path, self.path("unused.json"))

    def test_cycle_beside_legitimate_widget_rejected_without_output(self):
        pdf_path = self.attack_pdf("beside_legitimate")
        output_path = self.path("fields.json")
        self.worker("write", pdf_path, output_path)

    def test_fill_library_rejects_cycle_without_output(self):
        pdf_path = self.attack_pdf("self_named")
        fields_json = self.path("values.json")
        output_path = self.path("filled.pdf")
        with open(fields_json, "w", encoding="utf-8") as handle:
            json.dump(
                [{"field_id": "name", "page": 1, "value": "Ada"}],
                handle,
            )
        self.worker("fill", pdf_path, output_path, fields_json)

    def test_check_fillable_fields_reports_fillable_for_attack_pdf(self):
        # The workflow in forms.md runs this first. It must return, and it must
        # report fillable fields so extraction is the next step. The hang is in
        # the extractor, not in this check (pypdf's own field walk is bounded).
        pdf_path = self.attack_pdf("self_named")
        returncode, stdout, stderr, growth, failure = _run_bounded(
            [
                sys.executable,
                os.path.join(SCRIPTS, "check_fillable_fields.py"),
                pdf_path,
            ],
            CYCLE_TIMEOUT_SECONDS,
        )
        self.assertIsNone(failure, stderr)
        self.assertLessEqual(growth, MAX_GROWTH_KB)
        self.assertEqual(returncode, 0, stderr.decode("utf-8", "replace"))
        self.assertIn(
            "This PDF has fillable form fields",
            stdout.decode("utf-8", "replace"),
        )

    def test_acyclic_parent_chain_still_extracts(self):
        pdf_path = self.path("nested.pdf")
        write_nested_pdf(pdf_path)
        captured = io.StringIO()
        with redirect_stdout(captured):
            fields = get_field_info(PdfReader(pdf_path))
        self.assertEqual(
            fields,
            [
                {
                    "field_id": "person.name",
                    "type": "text",
                    "page": 1,
                    "rect": [10, 20, 30, 40],
                }
            ],
        )
        output_path = self.path("nested.json")
        completed = subprocess.run(
            [
                sys.executable,
                os.path.join(SCRIPTS, "extract_form_field_info.py"),
                pdf_path,
                output_path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=CYCLE_TIMEOUT_SECONDS,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        with open(output_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)[0]["field_id"], "person.name")


def _worker(argv):
    entry, pdf_path, output_path = argv[0], argv[1], argv[2]
    fields_json = argv[3] if len(argv) > 3 else None
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    from extract_form_field_info import (  # noqa: E402
        get_full_annotation_field_id,
        write_field_info,
    )
    from fill_fillable_fields import fill_pdf_fields  # noqa: E402

    if entry == "direct":
        reader = PdfReader(pdf_path)
        annotation = list(reader.pages[0].get("/Annots", []))[0]
        get_full_annotation_field_id(annotation)
    elif entry == "info":
        get_field_info(PdfReader(pdf_path))
    elif entry == "write":
        write_field_info(pdf_path, output_path)
    elif entry == "fill":
        fill_pdf_fields(pdf_path, fields_json, output_path)
    else:
        raise SystemExit(f"unknown worker entry {entry}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker(sys.argv[2:])
    else:
        unittest.main()
