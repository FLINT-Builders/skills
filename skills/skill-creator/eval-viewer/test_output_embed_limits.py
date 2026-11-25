"""Security spec: eval review must not inline unbounded output files.

The skill-creator eval viewer (generate_review.py) embeds every file in each
run's outputs/ directory into the review page. Text is stored as a string.
Images, PDFs, spreadsheets, and other binaries are base64-encoded. Nothing
caps the file size or the total payload, and the HTTP handler rebuilds that
page on every GET. A skill under evaluation writes those outputs; one large
file is enough to exhaust memory in the review process.

These tests are the contract for a fix. They fail while that behavior is
present.

Contract
--------
Raw file bytes, counted before encoding:

- MAX_EMBEDDED_FILE_BYTES: a single output file is inlined only when its
  size is less than or equal to this. A larger file must not be read in
  full and must not have its body copied into the page. Replace it with a
  short placeholder that still names the file. Leave the file on disk
  unchanged.
- MAX_TOTAL_EMBEDDED_BYTES: one generated page inlines at most this many
  raw output bytes, current workspace and previous workspace combined.
  Consume the budget in this order: current runs from find_runs, files in
  sorted name order within each run; then previous-workspace runs in that
  same order. A file that does not fit is a placeholder, not a partial
  body. The budget resets for each page (each --static generation and each
  HTTP GET), not for the life of the process.

Inlined means the file's leading token, or the base64 of that token, appears
in the HTML. Placeholder text must not include the file body.

Not covered here (separate uncapped reads, not output embedding):
transcript.md when no prompt is available (generate_review.py prompt
fallback), eval_metadata.json, grading.json, benchmark.json, and
feedback.json on GET /api/feedback.
"""

from __future__ import annotations

import base64
import gc
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import tracemalloc
import unittest
import urllib.request
from functools import partial
from http.server import HTTPServer
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(VIEWER_DIR))

import generate_review  # noqa: E402

# Raw bytes of one output file that may be copied into a review page.
MAX_EMBEDDED_FILE_BYTES = 256 * 1024
# Raw bytes inlined from all output files in one page, including previous.
MAX_TOTAL_EMBEDDED_BYTES = 1024 * 1024

# Traced allocations while building a page that must not read an oversized
# body. A full read of an 8MiB output is tens of MiB; a page with only a
# placeholder stays well under this.
TRACE_CEILING_BYTES = 2_000_000
# HTML that only placeholders an oversized file is the viewer template plus
# a short note, not the file body.
PLACEHOLDER_PAGE_CEILING_BYTES = 120_000
OVERSIZED_BYTES = 8 * 1024 * 1024
TOKEN_LEN = 48


def make_token(label: str) -> bytes:
    text = f"TOK:{label}:".encode("ascii")
    if len(text) >= TOKEN_LEN:
        raise AssertionError(f"token label too long: {label}")
    return text + b"Z" * (TOKEN_LEN - len(text))


def exact_bytes(token: bytes, size: int) -> bytes:
    if len(token) > size:
        raise AssertionError("token larger than file")
    return token + b"x" * (size - len(token))


def body_inlined(html: str, raw: bytes) -> bool:
    """True when the file body was copied into the page as text or base64."""
    token = raw[:TOKEN_LEN]
    if len(token) % 3 != 0:
        raise AssertionError("token length must be a multiple of 3")
    literal = token.decode("ascii")
    encoded = base64.b64encode(token).decode("ascii")
    return literal in html or encoded in html


def write_workspace(root: Path, files: dict[str, bytes], prompt: str = "Review the output") -> Path:
    """Create one eval run the viewer discovers, and return its outputs dir."""
    run = root / "eval-0" / "with_skill"
    outputs = run / "outputs"
    outputs.mkdir(parents=True)
    (run / "eval_metadata.json").write_text(
        json.dumps({"eval_id": 0, "prompt": prompt})
    )
    for name, data in files.items():
        path = outputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return outputs


def file_edges(path: Path) -> tuple[int, bytes, bytes]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(64)
        handle.seek(max(0, size - 64))
        tail = handle.read(64)
    return size, head, tail


def build_page(workspace: Path, previous_workspace: Path | None = None) -> str:
    """Same sequence as main() --static: current runs, then previous, then HTML."""
    runs = generate_review.find_runs(workspace)
    previous = None
    if previous_workspace is not None:
        previous = generate_review.load_previous_iteration(previous_workspace)
    return generate_review.generate_html(runs, "demo-skill", previous, None)


def traced_page(workspace: Path, previous_workspace: Path | None = None) -> tuple[str, int]:
    tracemalloc.start()
    try:
        html = build_page(workspace, previous_workspace)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return html, peak


class OutputEmbedLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        gc.collect()

    def test_file_at_per_file_cap_is_inlined_for_every_output_kind(self) -> None:
        kinds = ("note.txt", "shot.png", "brief.pdf", "sheet.xlsx", "data.bin")
        for name in kinds:
            with self.subTest(name=name):
                token = make_token(name)
                raw = exact_bytes(token, MAX_EMBEDDED_FILE_BYTES)
                workspace = self.root / name
                write_workspace(workspace, {name: raw})
                html = build_page(workspace)
                self.assertIn(name, html)
                self.assertTrue(
                    body_inlined(html, raw),
                    f"{name} at the per-file cap must still be inlined",
                )

    def test_file_one_byte_over_per_file_cap_is_not_inlined(self) -> None:
        kinds = ("note.txt", "shot.png", "brief.pdf", "sheet.xlsx", "data.bin")
        for name in kinds:
            with self.subTest(name=name):
                token = make_token("over-" + name)
                raw = exact_bytes(token, MAX_EMBEDDED_FILE_BYTES + 1)
                workspace = self.root / name
                outputs = write_workspace(workspace, {name: raw})
                before = file_edges(outputs / name)
                html = build_page(workspace)
                self.assertIn(name, html)
                self.assertFalse(
                    body_inlined(html, raw),
                    f"{name} one byte over the cap was inlined",
                )
                self.assertLess(len(html), PLACEHOLDER_PAGE_CEILING_BYTES)
                self.assertEqual(file_edges(outputs / name), before)

    def test_oversized_output_is_not_read_into_the_page(self) -> None:
        """Each embed branch must skip the body before reading it."""
        kinds = ("note.txt", "shot.png", "brief.pdf", "sheet.xlsx", "data.bin")
        for name in kinds:
            with self.subTest(name=name):
                token = make_token("huge-" + name)
                raw = exact_bytes(token, OVERSIZED_BYTES)
                workspace = self.root / ("huge-" + name)
                outputs = write_workspace(workspace, {name: raw})
                target = outputs / name
                before = file_edges(target)
                html, peak = traced_page(workspace)
                self.assertLess(
                    peak,
                    TRACE_CEILING_BYTES,
                    f"building the page traced {peak} bytes; the {OVERSIZED_BYTES}-byte file was read",
                )
                self.assertIn(name, html)
                self.assertFalse(body_inlined(html, raw))
                self.assertLess(len(html), PLACEHOLDER_PAGE_CEILING_BYTES)
                self.assertEqual(file_edges(target), before)
                del html
                gc.collect()

    def test_symlink_to_oversized_output_is_not_read(self) -> None:
        token = make_token("symlink-target")
        raw = exact_bytes(token, OVERSIZED_BYTES)
        workspace = self.root / "linked"
        outputs = write_workspace(workspace, {})
        target = workspace / "outside-the-run.bin"
        target.write_bytes(raw)
        link = outputs / "linked.pdf"
        link.symlink_to(target)
        before = file_edges(target)
        html, peak = traced_page(workspace)
        self.assertLess(peak, TRACE_CEILING_BYTES)
        self.assertIn("linked.pdf", html)
        self.assertFalse(body_inlined(html, raw))
        self.assertTrue(link.is_symlink())
        self.assertEqual(file_edges(target), before)

    def test_total_budget_inlines_files_that_fit_in_sorted_order(self) -> None:
        # Five files at the per-file cap. Four fill the total budget exactly.
        names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
        files: dict[str, bytes] = {}
        tokens: dict[str, bytes] = {}
        for name in names:
            token = make_token(name)
            tokens[name] = token
            files[name] = exact_bytes(token, MAX_EMBEDDED_FILE_BYTES)
        workspace = self.root / "budget"
        outputs = write_workspace(workspace, files)
        before = {name: file_edges(outputs / name) for name in names}
        html = build_page(workspace)
        for name in names[:4]:
            self.assertTrue(
                body_inlined(html, files[name]),
                f"{name} fits in the total budget and must be inlined",
            )
        self.assertFalse(
            body_inlined(html, files["e.txt"]),
            "e.txt exceeds the total budget and must not be inlined",
        )
        self.assertIn("e.txt", html)
        for name in names:
            self.assertEqual(file_edges(outputs / name), before[name])

    def test_total_budget_includes_previous_workspace_outputs(self) -> None:
        current_files = {}
        for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
            current_files[name] = exact_bytes(make_token("cur-" + name), MAX_EMBEDDED_FILE_BYTES)
        previous_token = make_token("previous-output")
        previous_raw = exact_bytes(previous_token, MAX_EMBEDDED_FILE_BYTES)
        current = self.root / "current"
        previous = self.root / "previous"
        write_workspace(current, current_files)
        write_workspace(previous, {"earlier.txt": previous_raw})
        html = build_page(current, previous)
        for name, raw in current_files.items():
            self.assertTrue(body_inlined(html, raw), name)
        self.assertIn("earlier.txt", html)
        self.assertFalse(
            body_inlined(html, previous_raw),
            "previous output was inlined after the total budget was spent",
        )

    def test_previous_workspace_oversized_output_is_not_read(self) -> None:
        token = make_token("old-huge")
        raw = exact_bytes(token, OVERSIZED_BYTES)
        current = self.root / "current"
        previous = self.root / "previous"
        write_workspace(current, {"note.txt": b"current result\n"})
        outputs = write_workspace(previous, {"old.pdf": raw})
        before = file_edges(outputs / "old.pdf")
        html, peak = traced_page(current, previous)
        self.assertLess(peak, TRACE_CEILING_BYTES)
        self.assertIn("old.pdf", html)
        self.assertIn("current result", html)
        self.assertFalse(body_inlined(html, raw))
        self.assertEqual(file_edges(outputs / "old.pdf"), before)

    def test_budget_resets_for_each_page(self) -> None:
        first = self.root / "page-a"
        second = self.root / "page-b"
        raw_a = exact_bytes(make_token("page-a"), MAX_EMBEDDED_FILE_BYTES)
        raw_b = exact_bytes(make_token("page-b"), MAX_EMBEDDED_FILE_BYTES)
        write_workspace(first, {"a.txt": raw_a})
        write_workspace(second, {"b.txt": raw_b})
        html_a = build_page(first)
        html_b = build_page(second)
        self.assertTrue(body_inlined(html_a, raw_a))
        self.assertTrue(body_inlined(html_b, raw_b))

    def test_http_get_does_not_embed_oversized_output_and_rebuilds(self) -> None:
        token = make_token("http-huge")
        raw = exact_bytes(token, OVERSIZED_BYTES)
        workspace = self.root / "served"
        outputs = write_workspace(workspace, {"payload.bin": raw})
        before = file_edges(outputs / "payload.bin")
        listing_before = sorted(p.name for p in workspace.rglob("*"))
        server = _start_server(workspace, previous={})
        try:
            for _ in range(2):
                status, body, peak = _traced_get(server, "/")
                html = body.decode("utf-8")
                self.assertEqual(status, 200)
                self.assertLess(peak, TRACE_CEILING_BYTES)
                self.assertIn("payload.bin", html)
                self.assertFalse(body_inlined(html, raw))
                self.assertLess(len(body), PLACEHOLDER_PAGE_CEILING_BYTES)
        finally:
            server.server_close()
        self.assertEqual(file_edges(outputs / "payload.bin"), before)
        listing_after = sorted(p.name for p in workspace.rglob("*"))
        self.assertEqual(listing_after, listing_before)

    def test_http_get_inlines_a_file_at_the_cap_on_every_request(self) -> None:
        raw = exact_bytes(make_token("http-fit"), MAX_EMBEDDED_FILE_BYTES)
        workspace = self.root / "served-fit"
        write_workspace(workspace, {"fit.txt": raw})
        server = _start_server(workspace, previous={})
        try:
            for _ in range(2):
                status, body, _peak = _traced_get(server, "/")
                self.assertEqual(status, 200)
                self.assertTrue(body_inlined(body.decode("utf-8"), raw))
        finally:
            server.server_close()

    def test_http_get_shares_the_total_budget_with_previous_outputs(self) -> None:
        current_files = {
            name: exact_bytes(make_token("live-" + name), MAX_EMBEDDED_FILE_BYTES)
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }
        previous_raw = exact_bytes(make_token("stale"), MAX_EMBEDDED_FILE_BYTES)
        current = self.root / "live"
        previous_root = self.root / "stale"
        write_workspace(current, current_files)
        write_workspace(previous_root, {"earlier.txt": previous_raw})
        previous = generate_review.load_previous_iteration(previous_root)
        server = _start_server(current, previous=previous)
        try:
            status, body, _peak = _traced_get(server, "/")
        finally:
            server.server_close()
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        for raw in current_files.values():
            self.assertTrue(body_inlined(html, raw))
        self.assertIn("earlier.txt", html)
        self.assertFalse(body_inlined(html, previous_raw))

    def test_static_cli_does_not_embed_oversized_output(self) -> None:
        token = make_token("static-huge")
        raw = exact_bytes(token, OVERSIZED_BYTES)
        workspace = self.root / "static-ws"
        outputs = write_workspace(workspace, {"blob.bin": raw})
        before = file_edges(outputs / "blob.bin")
        html_path = self.root / "review.html"
        peak = _run_static_cli(workspace, html_path)
        html = html_path.read_text()
        self.assertLess(peak, TRACE_CEILING_BYTES)
        self.assertIn("blob.bin", html)
        self.assertFalse(body_inlined(html, raw))
        self.assertLess(html_path.stat().st_size, PLACEHOLDER_PAGE_CEILING_BYTES)
        self.assertEqual(file_edges(outputs / "blob.bin"), before)

    def test_static_cli_caps_previous_workspace_and_keeps_small_output(self) -> None:
        small = exact_bytes(make_token("static-small"), 64)
        huge_token = make_token("static-prev")
        huge = exact_bytes(huge_token, OVERSIZED_BYTES)
        current = self.root / "static-current"
        previous = self.root / "static-previous"
        write_workspace(current, {"now.txt": small})
        outputs = write_workspace(previous, {"then.pdf": huge})
        before = file_edges(outputs / "then.pdf")
        html_path = self.root / "review-prev.html"
        peak = _run_static_cli(current, html_path, previous)
        html = html_path.read_text()
        self.assertLess(peak, TRACE_CEILING_BYTES)
        self.assertTrue(body_inlined(html, small))
        self.assertIn("then.pdf", html)
        self.assertFalse(body_inlined(html, huge))
        self.assertEqual(file_edges(outputs / "then.pdf"), before)


def _start_server(workspace: Path, previous: dict) -> HTTPServer:
    feedback = workspace / "feedback.json"
    handler = partial(
        generate_review.ReviewHandler,
        workspace,
        "demo-skill",
        feedback,
        previous,
        None,
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = 10
    return server


def _traced_get(server: HTTPServer, path: str) -> tuple[int, bytes, int]:
    port = server.server_address[1]
    box: dict = {}

    def client() -> None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=10
            ) as response:
                box["status"] = response.status
                box["body"] = response.read()
        except Exception as exc:  # noqa: BLE001 - surface client failures in the assertion
            box["error"] = exc

    tracemalloc.start()
    try:
        thread = threading.Thread(target=client)
        thread.start()
        server.handle_request()
        thread.join(timeout=10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if thread.is_alive():
        raise RuntimeError("review request did not finish")
    if "error" in box:
        raise box["error"]
    return box["status"], box["body"], peak


def _run_static_cli(
    workspace: Path, html_path: Path, previous: Path | None = None
) -> int:
    """Run generate_review.py --static in a child so startup and main() are covered."""
    script = VIEWER_DIR / "generate_review.py"
    argv = [
        str(script),
        str(workspace),
        "--static",
        str(html_path),
        "--skill-name",
        "demo-skill",
    ]
    if previous is not None:
        argv.extend(["--previous-workspace", str(previous)])
    # The wrapper lives outside the workspace so find_runs does not treat it as a run.
    wrapper = html_path.parent / f".trace-{html_path.name}.py"
    wrapper.write_text(
        textwrap.dedent(
            f"""
            import runpy
            import sys
            import tracemalloc

            tracemalloc.start()
            sys.argv = {argv!r}
            try:
                runpy.run_path({str(script)!r}, run_name="__main__")
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
            _current, peak = tracemalloc.get_traced_memory()
            print("TRACE_PEAK", peak)
            print("EXIT", 0 if code in (None, 0) else code)
            """
        )
    )
    completed = subprocess.run(
        [sys.executable, str(wrapper)],
        cwd=str(html_path.parent),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    peak = None
    exit_code = None
    for line in completed.stdout.splitlines():
        if line.startswith("TRACE_PEAK "):
            peak = int(line.split()[1])
        elif line.startswith("EXIT "):
            exit_code = int(line.split()[1])
    if completed.returncode != 0 or exit_code not in (0, None) or peak is None:
        raise AssertionError(
            "static viewer failed\n"
            f"returncode={completed.returncode} exit={exit_code}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return peak


if __name__ == "__main__":
    unittest.main()
