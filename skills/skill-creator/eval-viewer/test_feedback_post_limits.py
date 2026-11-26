#!/usr/bin/env python3
"""Limits for the eval review server's feedback POST.

The review server must not buffer a client-declared body larger than 1 MiB,
and it must not treat a CORS-safelisted content type as a feedback save.
These tests exercise ReviewHandler on HTTPServer. They fail while the handler
reads the full Content-Length on its only thread before it checks the body.

Run from the repository root:

    python3 skills/skill-creator/eval-viewer/test_feedback_post_limits.py -v
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import HTTPServer
from pathlib import Path

from generate_review import ReviewHandler

# Largest feedback document the server may accept.
MAX_FEEDBACK_BODY = 1024 * 1024

# A declared length used to show the handler allocates and then blocks.
DECLARED_OVERSIZE = 32 * 1024 * 1024

RESPONSE_TIMEOUT_S = 2.0

SENTINEL = '{"reviews":[{"run_id":"kept","feedback":"sentinel"}]}\n'

# Status codes that refuse the request. 5xx means the body was already read
# and then failed later, which is not a refusal.
REFUSED = range(400, 500)

SMALL_REVIEW = {
    "reviews": [{"run_id": "run1", "feedback": "looks good", "timestamp": "t"}],
    "status": "in_progress",
}


def make_workspace(root: Path) -> Path:
    run = root / "run1"
    (run / "outputs").mkdir(parents=True)
    (run / "eval_metadata.json").write_text(
        json.dumps({"prompt": "prompt", "eval_id": 1})
    )
    (run / "outputs" / "out.txt").write_text("output")
    (root / "feedback.json").write_text(SENTINEL)
    return root


def status_of(response: bytes) -> int | None:
    line = response.split(b"\r\n", 1)[0]
    parts = line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def header_value(response: bytes, name: str) -> str | None:
    head, _, _ = response.partition(b"\r\n\r\n")
    prefix = name.lower().encode() + b":"
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip().decode("iso-8859-1")
    return None


def read_http_response(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    return data


def open_post(
    port: int,
    content_length: int,
    content_type: str,
    body_prefix: bytes,
    extra_headers: dict[str, str] | None = None,
) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port))
    headers = [
        "POST /api/feedback HTTP/1.1",
        "Host: 127.0.0.1",
        f"Content-Type: {content_type}",
        f"Content-Length: {content_length}",
        "Connection: close",
        "Origin: http://172.30.0.2:8902",
    ]
    if extra_headers:
        for key, value in extra_headers.items():
            headers.append(f"{key}: {value}")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body_prefix
    sock.sendall(raw)
    return sock


def request(
    port: int,
    method: str,
    path: str,
    content_type: str | None = None,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    timeout: float = RESPONSE_TIMEOUT_S,
) -> bytes:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        headers = [
            f"{method} {path} HTTP/1.1",
            "Host: 127.0.0.1",
            "Connection: close",
        ]
        if content_type is not None:
            headers.append(f"Content-Type: {content_type}")
        if method in {"POST", "PUT", "OPTIONS"} or body:
            headers.append(f"Content-Length: {len(body)}")
        if extra_headers:
            for key, value in extra_headers.items():
                headers.append(f"{key}: {value}")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
        return read_http_response(sock, timeout)
    finally:
        sock.close()


@contextmanager
def running_server(workspace: Path):
    feedback = workspace / "feedback.json"
    handler = partial(ReviewHandler, workspace, "skill", feedback, {}, None)
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
    if proc.stdout is not None:
        proc.stdout.close()
    proc.wait(timeout=5)


def vm_peak_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmPeak:"):
            return int(line.split()[1])
    raise AssertionError(f"VmPeak missing for pid {pid}")


class FeedbackPostLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = make_workspace(Path(self._tmp.name))
        self.feedback = self.workspace / "feedback.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assert_sentinel_intact(self) -> None:
        self.assertEqual(
            self.feedback.read_text(),
            SENTINEL,
            "feedback.json changed before the request was refused",
        )

    def test_declared_length_above_one_mibibyte_is_refused_without_allocating(self) -> None:
        """A declared body over 1 MiB must be refused before it is read.

        The client keeps the unread bytes back. A handler that allocates the
        declared length, or that blocks in read() until that length arrives,
        fails this test. The same request must not keep the only request
        thread from serving a later GET.
        """
        proc = subprocess.Popen(
            [sys.executable, __file__, "--serve", str(self.workspace)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        post = None
        try:
            assert proc.stdout is not None
            port = int(proc.stdout.readline().strip())
            peak_before = vm_peak_kb(proc.pid)
            post = open_post(
                port,
                DECLARED_OVERSIZE,
                "text/plain",
                b"x" * 1024,
            )
            getter: dict[str, bytes | BaseException] = {}

            def fetch_saved() -> None:
                try:
                    getter["response"] = request(port, "GET", "/api/feedback")
                except BaseException as exc:  # noqa: BLE001 - report the hang
                    getter["response"] = exc

            # Let the POST reach do_POST before the GET is queued.
            threading.Event().wait(0.2)
            worker = threading.Thread(target=fetch_saved, daemon=True)
            worker.start()
            try:
                posted = read_http_response(post, RESPONSE_TIMEOUT_S)
            except TimeoutError:
                posted = b""
            worker.join(RESPONSE_TIMEOUT_S + 0.5)
            peak_after = vm_peak_kb(proc.pid)
        finally:
            if post is not None:
                post.close()
            _stop_process(proc)

        problems: list[str] = []
        code = status_of(posted)
        if code not in REFUSED:
            problems.append(
                f"POST was not refused before the unread body arrived "
                f"(status={code!r}, first bytes={posted[:120]!r})"
            )
        growth_kb = peak_after - peak_before
        # The declared buffer is 32 MiB. Header handling is a few hundred KB.
        if growth_kb > 8 * 1024:
            problems.append(
                f"process virtual memory grew by {growth_kb} KB after a "
                f"{DECLARED_OVERSIZE}-byte Content-Length whose body was not sent"
            )
        got = getter.get("response")
        if isinstance(got, BaseException) or not isinstance(got, bytes) or status_of(got) != 200:
            problems.append(
                "GET /api/feedback did not complete while the oversized POST "
                f"was outstanding ({got!r})"
            )
        if self.feedback.read_text() != SENTINEL:
            problems.append("feedback.json was replaced by the partial POST")
        if problems:
            self.fail("\n".join(problems))

    def test_completed_oversized_json_does_not_replace_feedback(self) -> None:
        body = json.dumps(
            {
                "reviews": [
                    {
                        "run_id": "run1",
                        "feedback": "x" * (MAX_FEEDBACK_BODY + 100),
                        "timestamp": "t",
                    }
                ],
                "status": "in_progress",
            }
        ).encode()
        self.assertGreater(len(body), MAX_FEEDBACK_BODY)
        with running_server(self.workspace) as port:
            response = request(port, "POST", "/api/feedback", "application/json", body)
        code = status_of(response)
        self.assertIn(code, REFUSED, response[:300])
        self.assert_sentinel_intact()

    def test_cors_safelisted_content_types_cannot_save_feedback(self) -> None:
        """Browsers send these types cross-site with no preflight.

        A private-network page can POST them to 127.0.0.1. Valid JSON in a
        safelisted type must not be stored. application/json stays allowed.
        """
        valid = json.dumps(SMALL_REVIEW).encode()
        cases = [
            ("text/plain", valid),
            ("text/plain;charset=UTF-8", valid),
            ("application/x-www-form-urlencoded", b"reviews=hi"),
            (
                "multipart/form-data; boundary=----bound",
                b"------bound\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\nhi\r\n------bound--\r\n",
            ),
        ]
        with running_server(self.workspace) as port:
            for content_type, body in cases:
                with self.subTest(content_type=content_type):
                    response = request(
                        port, "POST", "/api/feedback", content_type, body
                    )
                    code = status_of(response)
                    self.assertIn(code, REFUSED, response[:300])
                    self.assert_sentinel_intact()

    def test_negative_content_length_is_refused_without_waiting_for_close(self) -> None:
        """Content-Length: -1 must not become read-until-EOF on the only thread."""
        with running_server(self.workspace) as port:
            sock = socket.create_connection(("127.0.0.1", port))
            try:
                sock.sendall(
                    b"POST /api/feedback HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: -1\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"{"
                )
                try:
                    response = read_http_response(sock, RESPONSE_TIMEOUT_S)
                except TimeoutError:
                    response = b""
            finally:
                sock.close()
            follow = request(
                port,
                "POST",
                "/api/feedback",
                "application/json",
                json.dumps(SMALL_REVIEW).encode(),
            )
        self.assertIn(status_of(response), REFUSED, response[:200])
        self.assertEqual(status_of(follow), 200, follow[:300])
        saved = json.loads(self.feedback.read_text())
        self.assertEqual(saved["reviews"][0]["feedback"], "looks good")

    def test_content_length_above_ssize_is_refused(self) -> None:
        huge = str(sys.maxsize + 1)
        with running_server(self.workspace) as port:
            sock = socket.create_connection(("127.0.0.1", port))
            try:
                sock.sendall(
                    (
                        "POST /api/feedback HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {huge}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("ascii")
                    + b"{"
                )
                try:
                    response = read_http_response(sock, RESPONSE_TIMEOUT_S)
                except TimeoutError:
                    response = b""
            finally:
                sock.close()
            follow = request(port, "GET", "/api/feedback")
        self.assertIn(status_of(response), REFUSED, response[:200])
        self.assertEqual(status_of(follow), 200, follow[:200])
        self.assert_sentinel_intact()

    def test_small_application_json_feedback_is_saved(self) -> None:
        body = json.dumps(SMALL_REVIEW).encode()
        self.assertLess(len(body), MAX_FEEDBACK_BODY)
        with running_server(self.workspace) as port:
            response = request(
                port,
                "POST",
                "/api/feedback",
                "application/json",
                body,
            )
        self.assertEqual(status_of(response), 200, response[:300])
        saved = json.loads(self.feedback.read_text())
        self.assertEqual(saved["reviews"][0]["run_id"], "run1")
        self.assertEqual(saved["reviews"][0]["feedback"], "looks good")

    def test_cross_origin_preflight_is_not_permitted(self) -> None:
        with running_server(self.workspace) as port:
            response = request(
                port,
                "OPTIONS",
                "/api/feedback",
                extra_headers={
                    "Origin": "https://203.0.113.10:8443",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                    "Access-Control-Request-Private-Network": "true",
                },
            )
        self.assertNotEqual(status_of(response), 200, response[:400])
        allow_origin = header_value(response, "Access-Control-Allow-Origin")
        allow_private = header_value(response, "Access-Control-Allow-Private-Network")
        self.assertIsNone(allow_origin, response[:400])
        self.assertIsNone(allow_private, response[:400])

    def test_post_to_other_path_does_not_buffer_a_declared_body(self) -> None:
        """POST outside /api/feedback does not read a body today.

        Kept so a later change cannot share the unbounded read.
        """
        proc = subprocess.Popen(
            [sys.executable, __file__, "--serve", str(self.workspace)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        sock = None
        try:
            assert proc.stdout is not None
            port = int(proc.stdout.readline().strip())
            peak_before = vm_peak_kb(proc.pid)
            sock = socket.create_connection(("127.0.0.1", port))
            sock.sendall(
                (
                    "POST / HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\n"
                    "Content-Type: text/plain\r\n"
                    f"Content-Length: {DECLARED_OVERSIZE}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
                + b"x" * 64
            )
            try:
                response = read_http_response(sock, RESPONSE_TIMEOUT_S)
            except TimeoutError:
                response = b""
            peak_after = vm_peak_kb(proc.pid)
        finally:
            if sock is not None:
                sock.close()
            _stop_process(proc)

        self.assertIn(status_of(response), REFUSED, response[:200])
        self.assertLess(peak_after - peak_before, 8 * 1024)
        self.assert_sentinel_intact()


def serve(workspace: Path) -> None:
    feedback = workspace / "feedback.json"
    handler = partial(ReviewHandler, workspace, "skill", feedback, {}, None)
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    print(httpd.server_address[1], flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]))
    else:
        unittest.main()
