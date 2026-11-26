#!/usr/bin/env python3
"""Generate and serve a review page for eval results.

Reads the workspace directory, discovers runs (directories with outputs/),
embeds all output data into a self-contained HTML page, and serves it via
a tiny HTTP server. Feedback auto-saves to feedback.json in the workspace.

Usage:
    python generate_review.py <workspace-path> [--port PORT] [--skill-name NAME]
    python generate_review.py <workspace-path> --previous-feedback /path/to/old/feedback.json

No dependencies beyond the Python stdlib are required.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import signal
import stat
import subprocess
import sys
import time
import webbrowser
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Files to exclude from output listings
METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}

# Extensions we render as inline text
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}

# Extensions we render as inline images
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# MIME type overrides for common types
MIME_OVERRIDES = {
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


# Workspace contents are untrusted: every read and write below goes through a
# directory file descriptor and opens the final component with O_NOFOLLOW, so
# a symlink (or one swapped in mid-scan) can never point the viewer outside
# the workspace.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", None)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
FD_SAFE_IO_SUPPORTED = (
    _O_NOFOLLOW is not None
    and _O_DIRECTORY is not None
    and os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def _require_fd_safe_io() -> None:
    if not FD_SAFE_IO_SUPPORTED:
        raise OSError("generate_review.py requires O_NOFOLLOW and dir_fd support (POSIX)")


def _open_dir_path(path: Path) -> int:
    """Open a caller-chosen directory (symlinks in the path itself are trusted)."""
    _require_fd_safe_io()
    return os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)


def _open_subdir(dir_fd: int, name: str) -> int | None:
    """Open a real subdirectory of dir_fd; symlinks and non-directories yield None."""
    try:
        return os.open(
            name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError:
        return None


def _read_regular_file(dir_fd: int, name: str) -> bytes | None:
    """Read a regular file inside dir_fd; symlinks and special files yield None.

    Raises OSError only when the file was opened but could not be read.
    """
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        raise
    with open(fd, "rb") as f:
        return f.read()


def _read_path_nofollow(path: Path) -> bytes | None:
    """Read path if its final component is a regular file, not a symlink."""
    try:
        dir_fd = _open_dir_path(path.parent)
    except OSError:
        return None
    try:
        return _read_regular_file(dir_fd, path.name)
    except OSError:
        return None
    finally:
        os.close(dir_fd)


def _write_path_nofollow(path: Path, data: bytes) -> None:
    """Write path, refusing to follow a symlink or replace a non-regular file."""
    dir_fd = _open_dir_path(path.parent)
    try:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
            0o644,
            dir_fd=dir_fd,
        )
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        os.ftruncate(fd, 0)
    except OSError:
        os.close(fd)
        raise
    with open(fd, "wb") as f:
        f.write(data)


def _load_json_bytes(raw: bytes | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def find_runs(workspace: Path) -> list[dict]:
    """Recursively find directories that contain an outputs/ subdirectory."""
    runs: list[dict] = []
    try:
        root_fd = _open_dir_path(workspace)
    except OSError:
        return runs
    try:
        _find_runs_recursive(root_fd, None, (), runs)
    finally:
        os.close(root_fd)
    runs.sort(key=lambda r: (
        float("inf") if r.get("eval_id") is None else r["eval_id"],
        r["id"],
    ))
    return runs


def _find_runs_recursive(
    dir_fd: int, parent_fd: int | None, rel_parts: tuple[str, ...], runs: list[dict]
) -> None:
    outputs_fd = _open_subdir(dir_fd, "outputs")
    if outputs_fd is not None:
        try:
            run = build_run(rel_parts, dir_fd, parent_fd, outputs_fd)
        finally:
            os.close(outputs_fd)
        if run:
            runs.append(run)
        return

    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs"}
    try:
        with os.scandir(dir_fd) as it:
            names = sorted(
                e.name for e in it if e.name not in skip and e.is_dir(follow_symlinks=False)
            )
    except OSError:
        return
    for name in names:
        child_fd = _open_subdir(dir_fd, name)
        if child_fd is None:
            continue
        try:
            _find_runs_recursive(child_fd, dir_fd, rel_parts + (name,), runs)
        finally:
            os.close(child_fd)


def build_run(
    rel_parts: tuple[str, ...], run_fd: int, parent_fd: int | None, outputs_fd: int
) -> dict | None:
    """Build a run dict with prompt, outputs, and grading data.

    parent_fd is None for a run at the workspace root, so nothing outside the
    workspace is read.
    """
    prompt = ""
    eval_id = None
    run_and_parent = [fd for fd in (run_fd, parent_fd) if fd is not None]

    # Try eval_metadata.json
    for dir_fd in run_and_parent:
        try:
            raw = _read_regular_file(dir_fd, "eval_metadata.json")
        except OSError:
            continue
        if raw is None:
            continue
        metadata = _load_json_bytes(raw)
        if isinstance(metadata, dict):
            prompt = metadata.get("prompt", "")
            eval_id = metadata.get("eval_id")
        if prompt:
            break

    # Fall back to transcript.md
    if not prompt:
        for dir_fd in (run_fd, outputs_fd):
            try:
                raw = _read_regular_file(dir_fd, "transcript.md")
            except OSError:
                continue
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace")
            match = re.search(r"## Eval Prompt\n\n([\s\S]*?)(?=\n##|$)", text)
            if match:
                prompt = match.group(1).strip()
            if prompt:
                break

    if not prompt:
        prompt = "(No prompt found)"

    run_id = "-".join(rel_parts) or "."

    # Collect output files
    output_files: list[dict] = []
    try:
        with os.scandir(outputs_fd) as it:
            names = sorted(
                e.name for e in it
                if e.name not in METADATA_FILES and e.is_file(follow_symlinks=False)
            )
    except OSError:
        names = []
    for name in names:
        try:
            raw = _read_regular_file(outputs_fd, name)
        except OSError:
            output_files.append({"name": name, "type": "error", "content": "(Error reading file)"})
            continue
        if raw is not None:
            output_files.append(embed_file(name, raw))

    # Load grading if present
    grading = None
    for dir_fd in run_and_parent:
        try:
            grading = _load_json_bytes(_read_regular_file(dir_fd, "grading.json"))
        except OSError:
            grading = None
        if grading:
            break

    return {
        "id": run_id,
        "prompt": prompt,
        "eval_id": eval_id,
        "outputs": output_files,
        "grading": grading,
    }


def embed_file(name: str, raw: bytes) -> dict:
    """Return an embedded representation of a file's contents."""
    path = Path(name)
    ext = path.suffix.lower()
    mime = get_mime_type(path)

    if ext in TEXT_EXTENSIONS:
        return {
            "name": name,
            "type": "text",
            "content": raw.decode("utf-8", errors="replace"),
        }

    b64 = base64.b64encode(raw).decode("ascii")
    if ext in IMAGE_EXTENSIONS:
        return {
            "name": name,
            "type": "image",
            "mime": mime,
            "data_uri": f"data:{mime};base64,{b64}",
        }
    elif ext == ".pdf":
        return {
            "name": name,
            "type": "pdf",
            "data_uri": f"data:{mime};base64,{b64}",
        }
    elif ext == ".xlsx":
        return {
            "name": name,
            "type": "xlsx",
            "data_b64": b64,
        }
    else:
        # Binary / unknown — base64 download link
        return {
            "name": name,
            "type": "binary",
            "mime": mime,
            "data_uri": f"data:{mime};base64,{b64}",
        }


def load_previous_iteration(workspace: Path) -> dict[str, dict]:
    """Load previous iteration's feedback and outputs.

    Returns a map of run_id -> {"feedback": str, "outputs": list[dict]}.
    """
    result: dict[str, dict] = {}

    # Load feedback
    feedback_map: dict[str, str] = {}
    data = _load_json_bytes(_read_path_nofollow(workspace / "feedback.json"))
    if isinstance(data, dict):
        try:
            feedback_map = {
                r["run_id"]: r["feedback"]
                for r in data.get("reviews", [])
                if r.get("feedback", "").strip()
            }
        except (AttributeError, KeyError, TypeError):
            pass

    # Load runs (to get outputs)
    prev_runs = find_runs(workspace)
    for run in prev_runs:
        result[run["id"]] = {
            "feedback": feedback_map.get(run["id"], ""),
            "outputs": run.get("outputs", []),
        }

    # Also add feedback for run_ids that had feedback but no matching run
    for run_id, fb in feedback_map.items():
        if run_id not in result:
            result[run_id] = {"feedback": fb, "outputs": []}

    return result


def generate_html(
    runs: list[dict],
    skill_name: str,
    previous: dict[str, dict] | None = None,
    benchmark: dict | None = None,
) -> str:
    """Generate the complete standalone HTML page with embedded data."""
    template_path = Path(__file__).parent / "viewer.html"
    template = template_path.read_text()

    # Build previous_feedback and previous_outputs maps for the template
    previous_feedback: dict[str, str] = {}
    previous_outputs: dict[str, list[dict]] = {}
    if previous:
        for run_id, data in previous.items():
            if data.get("feedback"):
                previous_feedback[run_id] = data["feedback"]
            if data.get("outputs"):
                previous_outputs[run_id] = data["outputs"]

    embedded = {
        "skill_name": skill_name,
        "runs": runs,
        "previous_feedback": previous_feedback,
        "previous_outputs": previous_outputs,
    }
    if benchmark:
        embedded["benchmark"] = benchmark

    data_json = json.dumps(embedded)

    return template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {data_json};")


# ---------------------------------------------------------------------------
# HTTP server (stdlib only, zero dependencies)
# ---------------------------------------------------------------------------

def _kill_port(port: int) -> None:
    """Kill any process listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split("\n"):
            if pid_str.strip():
                try:
                    os.kill(int(pid_str.strip()), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
        if result.stdout.strip():
            time.sleep(0.5)
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        print("Note: lsof not found, cannot check if port is in use", file=sys.stderr)

class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review HTML and handles feedback saves.

    Regenerates the HTML on each page load so that refreshing the browser
    picks up new eval outputs without restarting the server.
    """

    def __init__(
        self,
        workspace: Path,
        skill_name: str,
        feedback_path: Path,
        previous: dict[str, dict],
        benchmark_path: Path | None,
        *args,
        **kwargs,
    ):
        self.workspace = workspace
        self.skill_name = skill_name
        self.feedback_path = feedback_path
        self.previous = previous
        self.benchmark_path = benchmark_path
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            # Regenerate HTML on each request (re-scans workspace for new outputs)
            runs = find_runs(self.workspace)
            benchmark = None
            if self.benchmark_path:
                benchmark = _load_json_bytes(_read_path_nofollow(self.benchmark_path))
            html = generate_html(runs, self.skill_name, self.previous, benchmark)
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/feedback":
            data = _read_path_nofollow(self.feedback_path)
            if data is None:
                data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/feedback":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or "reviews" not in data:
                    raise ValueError("Expected JSON object with 'reviews' key")
                _write_path_nofollow(
                    self.feedback_path, (json.dumps(data, indent=2) + "\n").encode("utf-8")
                )
                resp = b'{"ok":true}'
                self.send_response(200)
            except (json.JSONDecodeError, OSError, ValueError) as e:
                resp = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress request logging to keep terminal clean
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and serve eval review")
    parser.add_argument("workspace", type=Path, help="Path to workspace directory")
    parser.add_argument("--port", "-p", type=int, default=3117, help="Server port (default: 3117)")
    parser.add_argument("--skill-name", "-n", type=str, default=None, help="Skill name for header")
    parser.add_argument(
        "--previous-workspace", type=Path, default=None,
        help="Path to previous iteration's workspace (shows old outputs and feedback as context)",
    )
    parser.add_argument(
        "--benchmark", type=Path, default=None,
        help="Path to benchmark.json to show in the Benchmark tab",
    )
    parser.add_argument(
        "--static", "-s", type=Path, default=None,
        help="Write standalone HTML to this path instead of starting a server",
    )
    args = parser.parse_args()

    if not FD_SAFE_IO_SUPPORTED:
        print("Error: this platform lacks O_NOFOLLOW/dir_fd support required to read the "
              "workspace safely", file=sys.stderr)
        sys.exit(1)

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"Error: {workspace} is not a directory", file=sys.stderr)
        sys.exit(1)

    runs = find_runs(workspace)
    if not runs:
        print(f"No runs found in {workspace}", file=sys.stderr)
        sys.exit(1)

    skill_name = args.skill_name or workspace.name.replace("-workspace", "")
    feedback_path = workspace / "feedback.json"

    previous: dict[str, dict] = {}
    if args.previous_workspace:
        previous = load_previous_iteration(args.previous_workspace.resolve())

    # absolute(), not resolve(): resolving would turn a symlinked benchmark.json
    # into its target and bypass the O_NOFOLLOW read.
    benchmark_path = args.benchmark.absolute() if args.benchmark else None
    benchmark = None
    if benchmark_path:
        benchmark = _load_json_bytes(_read_path_nofollow(benchmark_path))

    if args.static:
        html = generate_html(runs, skill_name, previous, benchmark)
        args.static.parent.mkdir(parents=True, exist_ok=True)
        args.static.write_text(html)
        print(f"\n  Static viewer written to: {args.static}\n")
        sys.exit(0)

    # Kill any existing process on the target port
    port = args.port
    _kill_port(port)
    handler = partial(ReviewHandler, workspace, skill_name, feedback_path, previous, benchmark_path)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Port still in use after kill attempt — find a free one
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]

    url = f"http://localhost:{port}"
    print(f"\n  Eval Viewer")
    print(f"  ─────────────────────────────────")
    print(f"  URL:       {url}")
    print(f"  Workspace: {workspace}")
    print(f"  Feedback:  {feedback_path}")
    if previous:
        print(f"  Previous:  {args.previous_workspace} ({len(previous)} runs)")
    if benchmark_path:
        print(f"  Benchmark: {benchmark_path}")
    print(f"\n  Press Ctrl+C to stop.\n")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
