#!/usr/bin/env python3
"""Spec for with_server.py server readiness.

The helper must not run the user command unless the process tree it spawned is
the listener on --port. These tests fail while any successful TCP connection to
localhost on that port is treated as ready.

The race test observes the port closed after the child has started, then binds
it from this process. Polling the port again still reaches this process, so a
second connect is not a fix. The pre-bound tests cover a listener that is
already present and a spawned server that exits with the address in use.
"""

import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "with_server.py"
MARKER = "placeholder-submission"
HELPER_TIMEOUT = "8"

# Child waits longer than the helper timeout, so it is still alive when a
# connect-only check would already have succeeded.
CHILD_HOLD_SECONDS = "20"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _quote(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


class ForeignListener:
    """Accepts on 127.0.0.1 only, matching a process that bound that address."""

    def __init__(self, port):
        self.port = port
        self.received = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen()
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1)
                data = bytearray()
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    data.extend(chunk)
                    if b"\r\n\r\n" in data:
                        head, _, rest = bytes(data).partition(b"\r\n\r\n")
                        length = 0
                        for line in head.split(b"\r\n"):
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":", 1)[1].strip())
                        if len(rest) >= length:
                            break
                with self._lock:
                    self.received.extend(data)
                try:
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                        b"Connection: close\r\n\r\nok"
                    )
                except OSError:
                    pass
            finally:
                conn.close()

    def snapshot(self):
        with self._lock:
            return bytes(self.received)

    def close(self):
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


def _write(path, text):
    path.write_text(text)
    path.chmod(0o755)


def _client_script():
    return """\
import sys
import urllib.request

sentinel, url, body = sys.argv[1:]
with open(sentinel, "w", encoding="utf-8") as handle:
    handle.write("ran\\n")
request = urllib.request.Request(url, data=body.encode(), method="POST")
with urllib.request.urlopen(request, timeout=3) as response:
    response.read()
"""


def _exiting_server_script():
    return """\
import socket
import sys
import time

port = int(sys.argv[1])
fate = sys.argv[2]
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen()
except OSError as exc:
    with open(fate, "w", encoding="utf-8") as handle:
        handle.write(f"error {exc.errno}\\n")
    sys.exit(1)
with open(fate, "w", encoding="utf-8") as handle:
    handle.write("bound\\n")
time.sleep(30)
"""


def _holding_server_script():
    return """\
import os
import socket
import sys
import time

port = int(sys.argv[1])
flag = sys.argv[2]
hold = float(sys.argv[3])
with open(flag, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
    handle.flush()
    os.fsync(handle.fileno())
time.sleep(hold)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", port))
except OSError as exc:
    with open(flag + ".bound", "w", encoding="utf-8") as handle:
        handle.write(f"error {exc.errno}\\n")
    sock.close()
    fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fallback.bind(("127.0.0.1", port + 1))
    fallback.listen()
    with open(flag + ".bound", "a", encoding="utf-8") as handle:
        handle.write("fallback\\n")
    time.sleep(30)
else:
    sock.listen()
    with open(flag + ".bound", "w", encoding="utf-8") as handle:
        handle.write("bound\\n")
    time.sleep(30)
"""


def _owned_server_script():
    return """\
import socket
import sys

port = int(sys.argv[1])
log_path = sys.argv[2]
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", port))
sock.listen()
while True:
    conn, _addr = sock.accept()
    data = bytearray()
    conn.settimeout(1)
    try:
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data.extend(chunk)
            if b"\\r\\n\\r\\n" in data:
                head, _, rest = bytes(data).partition(b"\\r\\n\\r\\n")
                length = 0
                for line in head.split(b"\\r\\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if len(rest) >= length:
                    break
    finally:
        with open(log_path, "ab") as handle:
            handle.write(data)
        try:
            conn.sendall(
                b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n"
                b"Connection: close\\r\\n\\r\\nok"
            )
        except OSError:
            pass
        conn.close()
"""


class WithServerPortHijackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.client = self.root / "client.py"
        _write(self.client, _client_script())
        self._groups = []

    def _run_helper(self, args):
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._groups.append(proc.pid)
        try:
            out, _ = proc.communicate(timeout=float(HELPER_TIMEOUT) + 10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            self.fail(f"helper did not return\n{out}")
        return proc.returncode, out

    def tearDown(self):
        for pid in self._groups:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _command(self, port, sentinel):
        url = f"http://localhost:{port}/submit"
        return [
            sys.executable,
            str(HELPER),
            "--timeout",
            HELPER_TIMEOUT,
            "--",
            sys.executable,
            str(self.client),
            str(sentinel),
            url,
            MARKER,
        ]

    def _assert_command_not_used(self, code, output, sentinel, received):
        problems = []
        if code == 0:
            problems.append("helper exited 0 after a foreign listener accepted")
        if sentinel.exists():
            problems.append("user command started and created the sentinel file")
        if MARKER.encode() in received:
            problems.append("foreign listener received the submission body")
        self.assertFalse(
            problems,
            "readiness accepted a listener the helper did not spawn:\n"
            + "\n".join(problems)
            + f"\n--- helper output ---\n{output}",
        )

    def test_prebound_listener_and_exited_server_does_not_run_command(self):
        port = _free_port()
        listener = ForeignListener(port)
        self.addCleanup(listener.close)
        fate = self.root / "fate.txt"
        script = self.root / "exit_server.py"
        _write(script, _exiting_server_script())
        sentinel = self.root / "sentinel.txt"
        command = self._command(port, sentinel)
        command[2:2] = [
            "--server",
            _quote([sys.executable, script, port, fate]),
            "--port",
            str(port),
        ]
        code, output = self._run_helper(command)
        self._assert_command_not_used(code, output, sentinel, listener.snapshot())
        # A fix may refuse before spawning. If the server did start, it must
        # not have been the listener the command would have used.
        if fate.exists():
            self.assertNotEqual(
                fate.read_text(encoding="utf-8").strip(),
                "bound",
                "spawned server bound the port the foreign listener was supposed to hold",
            )

    def test_listener_bound_after_spawn_while_server_alive_does_not_run_command(self):
        port = _free_port()
        flag = self.root / "started.txt"
        script = self.root / "hold_server.py"
        _write(script, _holding_server_script())
        sentinel = self.root / "sentinel.txt"
        command = self._command(port, sentinel)
        command[2:2] = [
            "--server",
            _quote([sys.executable, script, port, flag, CHILD_HOLD_SECONDS]),
            "--port",
            str(port),
        ]
        started = time.monotonic()
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._groups.append(proc.pid)
        listener = None
        try:
            deadline = time.monotonic() + 5
            while not flag.exists():
                if proc.poll() is not None:
                    out, _ = proc.communicate()
                    self.fail(f"helper exited before the child started\n{out}")
                if time.monotonic() > deadline:
                    os.killpg(proc.pid, signal.SIGKILL)
                    out, _ = proc.communicate()
                    self.fail(f"child did not signal startup\n{out}")
                time.sleep(0.01)
            child_pid = int(flag.read_text(encoding="utf-8").strip())
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                with self.assertRaises(ConnectionRefusedError):
                    probe.connect(("127.0.0.1", port))
            finally:
                probe.close()
            listener = ForeignListener(port)
            self.assertLess(
                time.monotonic() - started,
                3.0,
                "foreign bind missed the helper's readiness window",
            )
            os.kill(child_pid, 0)
            out, _ = proc.communicate(timeout=float(HELPER_TIMEOUT) + 10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            self.fail(f"helper did not return\n{out}")
        finally:
            if listener is not None:
                received = listener.snapshot()
                listener.close()
            else:
                received = b""
            try:
                os.kill(child_pid, signal.SIGKILL)
            except (ProcessLookupError, UnboundLocalError, PermissionError):
                pass
        self._assert_command_not_used(proc.returncode, out, sentinel, received)

    def test_second_server_foreign_listener_does_not_run_command(self):
        self._assert_multiserver_hijack(hijack="second")

    def test_first_server_foreign_listener_does_not_run_command(self):
        self._assert_multiserver_hijack(hijack="first")

    def _assert_multiserver_hijack(self, hijack):
        real_port = _free_port()
        foreign_port = _free_port()
        listener = ForeignListener(foreign_port)
        self.addCleanup(listener.close)
        owned = self.root / "owned_server.py"
        exiting = self.root / "exit_server.py"
        _write(owned, _owned_server_script())
        _write(exiting, _exiting_server_script())
        real_log = self.root / "real.log"
        fate = self.root / "fate.txt"
        sentinel = self.root / "sentinel.txt"
        real_cmd = _quote([sys.executable, owned, real_port, real_log])
        foreign_cmd = _quote([sys.executable, exiting, foreign_port, fate])
        if hijack == "first":
            servers = [
                ("--server", foreign_cmd, "--port", str(foreign_port)),
                ("--server", real_cmd, "--port", str(real_port)),
            ]
            target = foreign_port
        else:
            servers = [
                ("--server", real_cmd, "--port", str(real_port)),
                ("--server", foreign_cmd, "--port", str(foreign_port)),
            ]
            target = foreign_port
        command = self._command(target, sentinel)
        inserted = []
        for server in servers:
            inserted.extend(server)
        command[2:2] = inserted
        code, output = self._run_helper(command)
        self._assert_command_not_used(code, output, sentinel, listener.snapshot())
        if real_log.exists():
            self.assertNotIn(
                MARKER.encode(),
                real_log.read_bytes(),
                "submission reached a server after a foreign listener passed readiness",
            )

    def test_spawned_server_that_owns_the_port_runs_command(self):
        port = _free_port()
        script = self.root / "owned_server.py"
        log_path = self.root / "owned.log"
        _write(script, _owned_server_script())
        sentinel = self.root / "sentinel.txt"
        command = self._command(port, sentinel)
        command[2:2] = [
            "--server",
            _quote([sys.executable, script, port, log_path]),
            "--port",
            str(port),
        ]
        code, output = self._run_helper(command)
        self.assertEqual(code, 0, output)
        self.assertTrue(sentinel.exists(), output)
        self.assertIn(MARKER.encode(), log_path.read_bytes(), output)


if __name__ == "__main__":
    unittest.main()
