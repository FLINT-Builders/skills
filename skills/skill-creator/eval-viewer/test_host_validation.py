#!/usr/bin/env python3
"""Host-header checks for the eval review server.

A browser that DNS-rebinding rebind.attacker.example onto 127.0.0.1 sends
GET and POST to this process with Host: rebind.attacker.example:<port>.
The server must answer those requests with 403, must not put workspace
contents in the response, and must not create or modify any file.

Requests whose Host is exactly 127.0.0.1:<bound port> or
localhost:<bound port> are the pages the server itself opens and must
keep working.

These tests fail while that check is missing.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(VIEWER_DIR))

import generate_review  # noqa: E402


PROMPT = "PROMPT_MARKER_private_instruction_7c1fd121"
OUTPUT_TEXT = "OUTPUT_TEXT_MARKER_customer_table_7c1fd121"
PREVIOUS_OUTPUT = "PREVIOUS_OUTPUT_MARKER_prior_doc_7c1fd121"
PREVIOUS_FEEDBACK = "PREVIOUS_FEEDBACK_MARKER_do_not_share_7c1fd121"
CURRENT_FEEDBACK = "CURRENT_FEEDBACK_MARKER_reviewer_note_7c1fd121"
GRADING = "GRADING_EVIDENCE_MARKER_secret_quote_7c1fd121"
BENCHMARK = "BENCHMARK_MARKER_private_observation_7c1fd121"
OVERWRITE = "OVERWRITE_MARKER_attacker_feedback_7c1fd121"
IMAGE_BYTES = b"\x89PNG\r\nIMAGE_BYTES_MARKER_7c1fd121"
PDF_BYTES = b"%PDF-1.4\nPDF_BYTES_MARKER_7c1fd121\n"
XLSX_BYTES = b"PK\x03\x04XLSX_BYTES_MARKER_7c1fd121"


def _snapshot(root: Path) -> dict:
    snap = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            snap[rel] = ("symlink", str(path.readlink()))
        elif path.is_file():
            snap[rel] = ("file", path.read_bytes())
        elif path.is_dir():
            snap[rel] = ("dir",)
    return snap


def _write_run(root: Path, output_name: str, output_body: str, prompt: str) -> None:
    run = root / "eval-private" / "with_skill"
    outputs = run / "outputs"
    outputs.mkdir(parents=True)
    (run / "eval_metadata.json").write_text(
        json.dumps({"eval_id": 0, "eval_name": "private", "prompt": prompt})
    )
    (outputs / output_name).write_text(output_body)
    (run / "grading.json").write_text(
        json.dumps(
            {
                "expectations": [
                    {"text": "keeps private text", "passed": False, "evidence": GRADING}
                ]
            }
        )
    )


def _exchange(port: int, method: str, path: str, host: str | None,
              extra_headers: list[str] | None = None, body: bytes | None = None) -> bytes:
    extra = list(extra_headers or [])
    payload = body or b""
    if body is not None:
        extra.append(f"Content-Length: {len(payload)}")
        extra.append("Content-Type: application/json")
    lines = [f"{method} {path} HTTP/1.1", "Connection: close", *extra]
    if host is not None:
        lines.insert(1, f"Host: {host}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(raw)
        chunks = []
        while True:
            try:
                buf = sock.recv(65536)
            except socket.timeout:
                break
            if not buf:
                break
            chunks.append(buf)
    return b"".join(chunks)


def _status(raw: bytes) -> int:
    line = raw.split(b"\r\n", 1)[0]
    return int(line.split()[1])


class EvalViewerHostValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.workspace = root / "iteration-2"
        cls.previous_workspace = root / "iteration-1"
        _write_run(cls.workspace, "result.txt", OUTPUT_TEXT, PROMPT)
        outputs = cls.workspace / "eval-private" / "with_skill" / "outputs"
        (outputs / "scan.png").write_bytes(IMAGE_BYTES)
        (outputs / "scan.pdf").write_bytes(PDF_BYTES)
        (outputs / "sheet.xlsx").write_bytes(XLSX_BYTES)
        cls.feedback_path = cls.workspace / "feedback.json"
        cls.original_feedback = (
            json.dumps(
                {
                    "reviews": [
                        {
                            "run_id": "eval-private-with_skill",
                            "feedback": CURRENT_FEEDBACK,
                        }
                    ],
                    "status": "complete",
                },
                indent=2,
            )
            + "\n"
        ).encode()
        cls.feedback_path.write_bytes(cls.original_feedback)
        cls.benchmark_path = cls.workspace / "benchmark.json"
        cls.benchmark_path.write_text(
            json.dumps({"observations": BENCHMARK, "skill_name": "private-skill"})
        )

        _write_run(cls.previous_workspace, "old.txt", PREVIOUS_OUTPUT, "old prompt")
        (cls.previous_workspace / "feedback.json").write_text(
            json.dumps(
                {
                    "reviews": [
                        {
                            "run_id": "eval-private-with_skill",
                            "feedback": PREVIOUS_FEEDBACK,
                        }
                    ]
                }
            )
        )
        previous = generate_review.load_previous_iteration(cls.previous_workspace)
        handler = partial(
            generate_review.ReviewHandler,
            cls.workspace,
            "private-skill",
            cls.feedback_path,
            previous,
            cls.benchmark_path,
        )
        # Same wiring as generate_review.main: loopback HTTPServer, handler
        # closed over the workspace, previous iteration, and benchmark.
        cls.server = generate_review.HTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.page_markers = [
            PROMPT.encode(),
            OUTPUT_TEXT.encode(),
            PREVIOUS_OUTPUT.encode(),
            PREVIOUS_FEEDBACK.encode(),
            GRADING.encode(),
            BENCHMARK.encode(),
            base64.b64encode(IMAGE_BYTES),
            base64.b64encode(PDF_BYTES),
            base64.b64encode(XLSX_BYTES),
        ]
        cls.feedback_marker = CURRENT_FEEDBACK.encode()
        cls.attacker_host = f"rebind.attacker.example:{cls.port}"
        other = cls.port + 1 if cls.port < 65535 else cls.port - 1
        cls.rejected_hosts = [
            cls.attacker_host,
            f"localhost.attacker.example:{cls.port}",
            f"127.0.0.1.attacker.example:{cls.port}",
            f"127.0.0.1:{other}",
            f"localhost:{other}",
            "127.0.0.1",
            "localhost",
            f"[::1]:{cls.port}",
            f"0.0.0.0:{cls.port}",
            f"127.0.0.1:{cls.port}0",
            "",
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.feedback_path.write_bytes(self.original_feedback)
        self.workspace_before = _snapshot(self.workspace)
        self.previous_before = _snapshot(self.previous_workspace)

    def _assert_unchanged(self) -> None:
        self.assertEqual(self.workspace_before, _snapshot(self.workspace))
        self.assertEqual(self.previous_before, _snapshot(self.previous_workspace))

    def test_foreign_host_on_loopback_is_refused(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        raw = _exchange(self.port, "GET", "/", self.attacker_host)
        self.assertEqual(
            _status(raw),
            403,
            "a request that reached 127.0.0.1 with the rebinding Host "
            "must be refused before any workspace bytes are written",
        )
        for marker in self.page_markers:
            self.assertNotIn(marker, raw)
        self._assert_unchanged()

    def test_allowed_hosts_still_receive_embedded_workspace(self) -> None:
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host, path="/"):
                raw = _exchange(self.port, "GET", "/", host)
                self.assertEqual(_status(raw), 200)
                for marker in self.page_markers:
                    self.assertIn(marker, raw)
            with self.subTest(host=host, path="/index.html"):
                raw = _exchange(self.port, "GET", "/index.html", host)
                self.assertEqual(_status(raw), 200)
                for marker in self.page_markers:
                    self.assertIn(marker, raw)
            with self.subTest(host=host, path="/api/feedback"):
                raw = _exchange(self.port, "GET", "/api/feedback", host)
                self.assertEqual(_status(raw), 200)
                self.assertIn(self.feedback_marker, raw)
        self._assert_unchanged()

    def test_rebinding_hosts_do_not_receive_pages_or_feedback(self) -> None:
        cases = [(host, None) for host in self.rejected_hosts]
        cases.append((self.attacker_host, [
            f"Origin: http://{self.attacker_host}",
            f"X-Forwarded-Host: 127.0.0.1:{self.port}",
            f"Forwarded: host=localhost:{self.port}",
        ]))
        # A missing Host header is not one of the two allowed values.
        cases.append((None, None))
        for path in ("/", "/index.html", "/api/feedback"):
            for host, extra in cases:
                with self.subTest(path=path, host=host, extra=extra):
                    # An allowed request immediately before the attack must
                    # not authorize the next request.
                    allowed = _exchange(self.port, "GET", path, f"localhost:{self.port}")
                    self.assertEqual(_status(allowed), 200)
                    raw = _exchange(self.port, "GET", path, host, extra)
                    self.assertEqual(_status(raw), 403)
                    for marker in self.page_markers + [self.feedback_marker]:
                        self.assertNotIn(marker, raw)
                    self._assert_unchanged()

    def test_post_from_rebinding_host_does_not_touch_feedback(self) -> None:
        body = json.dumps(
            {
                "reviews": [
                    {"run_id": "eval-private-with_skill", "feedback": OVERWRITE}
                ],
                "status": "complete",
            }
        ).encode()
        extra = [f"Origin: http://{self.attacker_host}"]
        raw = _exchange(
            self.port, "POST", "/api/feedback", self.attacker_host, extra, body
        )
        self.assertEqual(_status(raw), 403)
        self.assertNotIn(OVERWRITE.encode(), raw)
        for marker in self.page_markers + [self.feedback_marker]:
            self.assertNotIn(marker, raw)
        self._assert_unchanged()

        self.feedback_path.unlink()
        raw = _exchange(
            self.port, "POST", "/api/feedback", self.attacker_host, extra, body
        )
        self.assertEqual(_status(raw), 403)
        self.assertFalse(
            self.feedback_path.exists(),
            "refusing the rebinding POST must not create feedback.json",
        )
        self.feedback_path.write_bytes(self.original_feedback)
        self.assertEqual(self.previous_before, _snapshot(self.previous_workspace))

    def test_allowed_post_still_saves_feedback(self) -> None:
        body = json.dumps(
            {
                "reviews": [
                    {
                        "run_id": "eval-private-with_skill",
                        "feedback": "reviewer kept this note",
                    }
                ],
                "status": "in_progress",
            }
        ).encode()
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host):
                self.feedback_path.write_bytes(self.original_feedback)
                raw = _exchange(
                    self.port, "POST", "/api/feedback", host, body=body
                )
                self.assertEqual(_status(raw), 200)
                saved = self.feedback_path.read_text()
                self.assertIn("reviewer kept this note", saved)
                self.assertNotIn(OVERWRITE, saved)

    def test_unimplemented_methods_do_not_return_workspace(self) -> None:
        # ReviewHandler does not implement these. BaseHTTPRequestHandler
        # returns 501 without calling do_GET. They must stay free of
        # workspace bytes for both the allowed host and the rebinding host.
        for method in ("HEAD", "OPTIONS", "PUT", "DELETE"):
            for host in (f"localhost:{self.port}", self.attacker_host):
                with self.subTest(method=method, host=host):
                    raw = _exchange(self.port, method, "/", host)
                    self.assertNotEqual(_status(raw), 200)
                    for marker in self.page_markers + [self.feedback_marker]:
                        self.assertNotIn(marker, raw)
                    self._assert_unchanged()


if __name__ == "__main__":
    unittest.main()
