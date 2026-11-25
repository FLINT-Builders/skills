#!/usr/bin/env python3
"""Remote-port process kill in the eval review launcher.

`generate_review.py` runs `lsof -ti :<port>` and sends SIGTERM to every PID
it prints before binding the review server. That lsof filter selects sockets
whose local or remote port equals the number, so a process with only an
outbound connection is signaled too.

These tests are the spec for that behavior. They fail while the launcher
still signals such a process, and they pass once it no longer does. The
client is already connected before the launcher starts and stays connected
until the launcher finishes the kill attempt, so looking the same port up
again with `lsof -ti` still sees the client.

Run:
    python3 skills/skill-creator/eval-viewer/test_remote_port_process_kill.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "generate_review.py"
REVIEW_PORT_DEFAULT = 3117

CLIENT_PROGRAM = r"""
import os
import signal
import socket
import sys
import time

port = int(sys.argv[1])
marker = sys.argv[2]
kind = sys.argv[3]


def on_term(signum, _frame):
    try:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("SIGTERM %s\n" % signum)
    finally:
        os._exit(143)


signal.signal(signal.SIGTERM, on_term)

if kind == "tcp4":
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port))
elif kind == "tcp6":
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.connect(("::1", port))
elif kind == "udp4":
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(("127.0.0.1", port))
else:
    raise SystemExit("unknown kind %s" % kind)

# Keep the socket object alive so the connection stays open.
print("ready", flush=True)
while True:
    time.sleep(0.5)
"""

SERVER_PROGRAM = r"""
import socket
import sys

kind = sys.argv[1]
port = int(sys.argv[2])
if kind == "tcp6":
    family = socket.AF_INET6
    host = "::1"
else:
    family = socket.AF_INET
    host = "127.0.0.1"
server = socket.socket(family, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if family == socket.AF_INET6:
    server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
server.bind((host, port))
server.listen(4)
print("listening", flush=True)
held = []
while True:
    server.settimeout(0.5)
    try:
        conn, _addr = server.accept()
    except socket.timeout:
        continue
    held.append(conn)
"""


def _pids_on_port(port: int) -> set[int]:
    """Return PIDs from the same lsof invocation the launcher uses."""
    result = subprocess.run(
        ["lsof", "-ti", ":%s" % port],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            pids.add(int(line))
    return pids


def _write_program(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def _spawn(program: Path, args: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(program), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_stdout_line(proc: subprocess.Popen[str], expected: str, timeout: float) -> None:
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.strip() == expected:
            return
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            raise AssertionError(
                "%s exited %s before printing %r\n%s" % (program_name(proc), proc.returncode, expected, stderr)
            )
    raise AssertionError("timed out waiting for %r" % expected)


def program_name(proc: subprocess.Popen[str]) -> str:
    return "pid %s" % proc.pid


def _make_workspace(root: Path) -> None:
    run = root / "eval-0"
    outputs = run / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "out.txt").write_text("ok\n", encoding="utf-8")
    (run / "eval_metadata.json").write_text(
        '{"prompt":"review","eval_id":0}\n',
        encoding="utf-8",
    )


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    reader = getattr(proc, "output_reader", None)
    if reader is not None:
        reader.join(timeout=2)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()


class RemotePortProcessKillTests(unittest.TestCase):
    def test_tcp4_outbound_client_is_not_signaled(self) -> None:
        self._assert_outbound_client_survives("tcp4")

    def test_tcp6_outbound_client_is_not_signaled(self) -> None:
        self._assert_outbound_client_survives("tcp6")

    def test_udp4_outbound_client_is_not_signaled(self) -> None:
        self._assert_outbound_client_survives("udp4")

    def test_default_port_outbound_client_is_not_signaled(self) -> None:
        # The skill launches the viewer without --port, so the kill uses 3117.
        strangers = _pids_on_port(REVIEW_PORT_DEFAULT)
        if strangers:
            self.skipTest("port %s is already held by %s" % (REVIEW_PORT_DEFAULT, sorted(strangers)))
        self._assert_outbound_client_survives(
            "tcp4",
            port=REVIEW_PORT_DEFAULT,
            pass_port_flag=False,
        )

    def test_client_on_a_different_port_is_not_signaled(self) -> None:
        """A client whose remote port is not the review port must survive.

        This holds both before and after the fix. It checks that the launcher
        does not signal every process it can see.
        """
        client_port = _reserve_port()
        review_port = _reserve_port()
        self._assert_outbound_client_survives(
            "tcp4",
            port=client_port,
            launch_port=review_port,
            expect_lsof_match=False,
        )

    def _assert_outbound_client_survives(
        self,
        kind: str,
        port: int | None = None,
        launch_port: int | None = None,
        pass_port_flag: bool = True,
        expect_lsof_match: bool = True,
    ) -> None:
        if port is None:
            port = _reserve_port()
        if launch_port is None:
            launch_port = port

        processes: list[subprocess.Popen[str]] = []
        viewer: subprocess.Popen[str] | None = None
        with tempfile.TemporaryDirectory(prefix="review-port-kill-") as temporary:
            root = Path(temporary)
            client_program = _write_program(root, "client.py", CLIENT_PROGRAM)
            server_program = _write_program(root, "server.py", SERVER_PROGRAM)
            marker = root / "signaled.txt"
            workspace = root / "workspace"
            _make_workspace(workspace)

            server: subprocess.Popen[str] | None = None
            if kind in {"tcp4", "tcp6"}:
                server = _spawn(server_program, [kind, str(port)])
                processes.append(server)
                _wait_stdout_line(server, "listening", 5)

            client = _spawn(client_program, [str(port), str(marker), kind])
            processes.append(client)
            try:
                _wait_stdout_line(client, "ready", 5)
                listed = _pids_on_port(port)
                if expect_lsof_match:
                    self.assertIn(
                        client.pid,
                        listed,
                        "precondition failed: lsof -ti :%s returned %s and did not include "
                        "the outbound client %s, so this run would not exercise the kill"
                        % (port, sorted(listed), client.pid),
                    )
                else:
                    self.assertNotIn(
                        client.pid,
                        _pids_on_port(launch_port),
                        "control client was already selected for the review port",
                    )

                viewer, output = _launch_viewer(
                    workspace,
                    None if not pass_port_flag else launch_port,
                )
                banner = "".join(output)
                self.assertIn(
                    "Eval Viewer",
                    banner,
                    "launcher exited before the post-kill banner, so the signal attempt "
                    "did not finish\n%s" % banner,
                )
                self.assertFalse(
                    marker.exists() or client.poll() is not None,
                    "launcher sent a fatal signal to outbound client pid %s "
                    "(remote port %s, review port %s, kind %s)\n"
                    "marker=%s returncode=%s\nlsof before launch=%s\nviewer:\n%s"
                    % (
                        client.pid,
                        port,
                        "default" if not pass_port_flag else launch_port,
                        kind,
                        marker.read_text(encoding="utf-8") if marker.exists() else None,
                        client.poll(),
                        sorted(listed),
                        banner,
                    ),
                )
            finally:
                _stop(viewer)
                for proc in processes:
                    _stop(proc)


def _launch_viewer(workspace: Path, port: int | None) -> tuple[subprocess.Popen[str], list[str]]:
    command = [sys.executable, "-u", str(SCRIPT), str(workspace)]
    if port is not None:
        command.extend(["--port", str(port)])
    env = os.environ.copy()
    # Child reads BROWSER when it imports webbrowser. `true` exits without a GUI.
    env["BROWSER"] = "/usr/bin/true"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    lines: list[str] = []

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    proc.output_reader = reader  # type: ignore[attr-defined]
    deadline = time.time() + 15
    while time.time() < deadline:
        if any("Eval Viewer" in line for line in lines):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    reader.join(timeout=0.2)
    return proc, lines


if __name__ == "__main__":
    unittest.main(verbosity=2)
