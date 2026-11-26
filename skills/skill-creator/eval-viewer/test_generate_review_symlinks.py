#!/usr/bin/env python3
"""Symlink disclosure tests for eval-viewer/generate_review.py.

These tests fail while the review builder follows symbolic links out of the
workspace. They pass once a link is not walked or read, including when the
link replaces a regular file or directory after that path has already been
looked up and before it is opened.

Run:
    python3 skills/skill-creator/eval-viewer/test_generate_review_symlinks.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import importlib.util

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "generate_review.py"
HARNESS_SRC = ROOT / "toctou_swap.c"
LEGIT = "LEGIT_OUTPUT_MARKER"
OUTSIDE_ORIGINAL = b"ORIGINAL_OUTSIDE_CONTENT\n"

_SPEC = importlib.util.spec_from_file_location("generate_review", SCRIPT)
gr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gr)

HARNESS_SO: Path | None = None


def _compile_harness() -> Path:
    if shutil.which("gcc") is None:
        raise RuntimeError("gcc is required to build the symlink race harness")
    work = Path(tempfile.mkdtemp(prefix="toctou-harness-"))
    so = work / "toctou_swap.so"
    result = subprocess.run(
        ["gcc", "-shared", "-fPIC", "-Wall", "-Werror", "-o", str(so), str(HARNESS_SRC), "-ldl"],
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"harness compile failed\n{result.stderr}")
    return so


def _run_python(code: str, env: dict, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def _harness_env(entry: Path, spare: Path) -> dict:
    env = os.environ.copy()
    env["LD_PRELOAD"] = str(HARNESS_SO)
    env["TOCTOU_ENTRY"] = str(entry)
    env["TOCTOU_SPARE"] = str(spare)
    env.pop("SYMLINK_REVIEW_DRIVER", None)
    return env


def setUpModule() -> None:
    global HARNESS_SO
    HARNESS_SO = _compile_harness()
    root = Path(tempfile.mkdtemp(prefix="toctou-selfcheck-"))
    try:
        outside = root / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("OUTSIDE_HARNESS_MARKER")
        entry = root / "entry.txt"
        entry.write_text("SAFE_HARNESS")
        spare = root / "spare"
        os.symlink(secret, spare)
        env = _harness_env(entry, spare)
        followed = _run_python(
            "from pathlib import Path\n"
            "import os\n"
            "p = Path(os.environ['TOCTOU_ENTRY'])\n"
            "seen = p.is_symlink() or p.is_symlink()\n"
            "data = '' if seen else p.read_text()\n"
            "print('SECRET' if 'OUTSIDE_HARNESS_MARKER' in data else 'NOSECRET')\n",
            env,
        )
        if followed.returncode != 0 or followed.stdout.strip() != "SECRET":
            raise RuntimeError(
                "harness did not disclose through a checked-then-opened path\n"
                f"stdout={followed.stdout!r}\nstderr={followed.stderr!r}"
            )
        entry2 = root / "entry2.txt"
        entry2.write_text("SAFE_HARNESS")
        spare2 = root / "spare2"
        os.symlink(secret, spare2)
        env2 = _harness_env(entry2, spare2)
        guarded = _run_python(
            "import os\n"
            "fd = os.open(os.environ['TOCTOU_ENTRY'], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)\n"
            "data = os.read(fd, 1000).decode()\n"
            "os.close(fd)\n"
            "print('SECRET' if 'OUTSIDE_HARNESS_MARKER' in data else 'NOSECRET')\n",
            env2,
        )
        if guarded.returncode != 0 or guarded.stdout.strip() != "NOSECRET":
            raise RuntimeError(
                "harness exchanged a path opened with O_NOFOLLOW\n"
                f"stdout={guarded.stdout!r}\nstderr={guarded.stderr!r}"
            )
    finally:
        shutil.rmtree(root)


def _hidden(blob: str, marker: str) -> bool:
    if marker in blob:
        return False
    encoded = base64.b64encode(marker.encode()).decode("ascii")
    return encoded not in blob


class ReviewSymlinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        legit_outputs = self.ws / "legit-run" / "outputs"
        legit_outputs.mkdir(parents=True)
        (legit_outputs / "hello.txt").write_text(LEGIT)
        (self.ws / "legit-run" / "eval_metadata.json").write_text(
            json.dumps({"prompt": "legit prompt", "eval_id": 0})
        )

    def _outside_file(self, name: str, content: str | bytes) -> Path:
        path = self.outside / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_bytes(content)
        return path

    def _link(self, link: Path, target: Path, relative: bool) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if relative:
            link.symlink_to(os.path.relpath(target, start=link.parent))
        else:
            link.symlink_to(target)

    def _tag(self, run: Path, eval_id: int) -> None:
        """Give a run an int eval_id so find_runs can sort it with the legit run."""
        run.mkdir(parents=True, exist_ok=True)
        meta = run / "eval_metadata.json"
        if not meta.exists() and not meta.is_symlink():
            meta.write_text(json.dumps({"prompt": "tagged", "eval_id": eval_id}))

    def _render(self, workspace: Path | None = None, previous: dict | None = None) -> str:
        workspace = self.ws if workspace is None else workspace
        runs = gr.find_runs(workspace)
        html = gr.generate_html(runs, "skill", previous)
        return json.dumps(runs) + "\n" + html

    def _assert_hidden(self, blob: str, marker: str) -> None:
        self.assertTrue(_hidden(blob, marker), f"{marker} was embedded in the review")

    def _assert_legit(self, blob: str) -> None:
        self.assertIn(LEGIT, blob)

    def _assert_outside_unchanged(self, before: dict[Path, bytes]) -> None:
        for path, content in before.items():
            self.assertTrue(path.is_file(), f"{path} was removed")
            self.assertEqual(path.read_bytes(), content)

    def test_outputs_directory_symlink_is_not_read(self) -> None:
        for relative in (False, True):
            with self.subTest(relative=relative):
                marker = f"OUTSIDE_OUTPUTS_DIR_{'REL' if relative else 'ABS'}"
                target = self._outside_file(f"dir-{relative}/secret.txt", marker)
                run = self.ws / f"dir-link-{'rel' if relative else 'abs'}"
                self._tag(run, 10 + int(relative))
                self._link(run / "outputs", target.parent, relative)
                before = {target: target.read_bytes()}
                blob = self._render()
                self._assert_legit(blob)
                self._assert_hidden(blob, marker)
                self._assert_outside_unchanged(before)

    def test_walker_does_not_follow_directory_symlink_to_later_outputs(self) -> None:
        marker = "OUTSIDE_WALK_MARKER"
        secret = self._outside_file("nested/deeper/outputs/secret.txt", marker)
        self._tag(secret.parent.parent, 11)
        planted = self.ws / "planted"
        self._link(planted, self.outside, relative=False)
        before = {secret: secret.read_bytes()}
        blob = self._render()
        self._assert_legit(blob)
        self._assert_hidden(blob, marker)
        self._assert_outside_unchanged(before)

    def test_output_file_symlinks_are_not_read(self) -> None:
        outputs = self.ws / "file-links" / "outputs"
        outputs.mkdir(parents=True)
        self._tag(outputs.parent, 12)
        (outputs / "hello.txt").write_text(LEGIT)
        cases = {
            "absolute": False,
            "relative": True,
        }
        files = []
        for name, relative in cases.items():
            marker = f"OUTSIDE_FILE_{name.upper()}"
            target = self._outside_file(f"{name}.txt", marker)
            self._link(outputs / f"{name}.txt", target, relative)
            files.append(target)
        chain_target = self._outside_file("chain-target.txt", "OUTSIDE_FILE_CHAIN")
        mid = self.outside / "chain-mid"
        mid.symlink_to(chain_target)
        (outputs / "chain.txt").symlink_to(mid)
        files.extend([chain_target, mid])
        before = {path: path.read_bytes() for path in files if path.is_file()}
        blob = self._render()
        self._assert_legit(blob)
        for marker in ("OUTSIDE_FILE_ABSOLUTE", "OUTSIDE_FILE_RELATIVE", "OUTSIDE_FILE_CHAIN"):
            self._assert_hidden(blob, marker)
        self._assert_outside_unchanged(before)

    def test_each_embed_kind_skips_symlink(self) -> None:
        outputs = self.ws / "kinds" / "outputs"
        outputs.mkdir(parents=True)
        self._tag(outputs.parent, 13)
        (outputs / "hello.txt").write_text(LEGIT)
        kinds = {
            "note.txt": "OUTSIDE_KIND_TEXT",
            "note.md": "OUTSIDE_KIND_MD",
            "pic.png": b"OUTSIDE_KIND_PNG",
            "doc.pdf": b"OUTSIDE_KIND_PDF",
            "sheet.xlsx": b"OUTSIDE_KIND_XLSX",
            "blob.bin": b"OUTSIDE_KIND_BIN",
        }
        for name, payload in kinds.items():
            target = self._outside_file(name, payload if isinstance(payload, str) else payload)
            (outputs / name).symlink_to(target)
        blob = self._render()
        self._assert_legit(blob)
        for payload in kinds.values():
            marker = payload if isinstance(payload, str) else payload.decode()
            self._assert_hidden(blob, marker)

    def test_metadata_transcript_and_grading_symlinks_are_not_read(self) -> None:
        run = self.ws / "meta-run"
        (run / "outputs").mkdir(parents=True)
        (run / "outputs" / "hello.txt").write_text(LEGIT)
        meta = self._outside_file(
            "meta.json",
            json.dumps({"prompt": "OUTSIDE_METADATA_MARKER", "eval_id": 4}),
        )
        transcript = self._outside_file(
            "transcript.md",
            "## Eval Prompt\n\nOUTSIDE_TRANSCRIPT_MARKER\n\n## End\n",
        )
        grading = self._outside_file(
            "grading.json",
            json.dumps(
                {
                    "expectations": [
                        {"text": "OUTSIDE_GRADING_MARKER", "passed": False, "evidence": "x"}
                    ]
                }
            ),
        )
        (run / "eval_metadata.json").symlink_to(meta)
        (run / "grading.json").symlink_to(grading)
        transcript_run = self.ws / "transcript-run"
        (transcript_run / "outputs").mkdir(parents=True)
        (transcript_run / "outputs" / "hello.txt").write_text(LEGIT)
        (transcript_run / "eval_metadata.json").write_text(
            json.dumps({"prompt": "", "eval_id": 21})
        )
        (transcript_run / "transcript.md").symlink_to(transcript)
        parent_meta = self._outside_file(
            "parent-meta.json",
            json.dumps({"prompt": "OUTSIDE_PARENT_METADATA_MARKER", "eval_id": 5}),
        )
        parent_run = self.ws / "parent-meta" / "child"
        (parent_run / "outputs").mkdir(parents=True)
        (parent_run / "outputs" / "hello.txt").write_text(LEGIT)
        (parent_run.parent / "eval_metadata.json").symlink_to(parent_meta)
        blob = self._render()
        self._assert_legit(blob)
        for marker in (
            "OUTSIDE_METADATA_MARKER",
            "OUTSIDE_TRANSCRIPT_MARKER",
            "OUTSIDE_GRADING_MARKER",
            "OUTSIDE_PARENT_METADATA_MARKER",
        ):
            self._assert_hidden(blob, marker)

    def test_previous_workspace_symlinks_are_not_embedded(self) -> None:
        prev = self.root / "prev"
        (prev / "old-run" / "outputs").mkdir(parents=True)
        target = self._outside_file("prev-secret.txt", "OUTSIDE_PREV_OUTPUT")
        (prev / "old-run" / "outputs").rmdir()
        (prev / "old-run" / "outputs").symlink_to(target.parent)
        feedback_target = self._outside_file(
            "prev-feedback.json",
            json.dumps(
                {"reviews": [{"run_id": "old-run", "feedback": "OUTSIDE_PREV_FEEDBACK"}]}
            ),
        )
        (prev / "feedback.json").symlink_to(feedback_target)
        previous = gr.load_previous_iteration(prev)
        blob = self._render(previous=previous)
        self._assert_legit(blob)
        self._assert_hidden(blob, "OUTSIDE_PREV_OUTPUT")
        self._assert_hidden(blob, "OUTSIDE_PREV_FEEDBACK")

    def test_static_review_and_http_server_do_not_disclose(self) -> None:
        marker = "OUTSIDE_SERVER_MARKER"
        secret = self._outside_file("server/secret.txt", marker)
        run = self.ws / "server-run"
        (run / "outputs").mkdir(parents=True)
        self._tag(run, 14)
        (run / "outputs" / "hello.txt").write_text(LEGIT)
        (run / "outputs" / "secret.txt").symlink_to(secret)
        walked = self._outside_file("walked/nested/outputs/secret.txt", "OUTSIDE_SERVER_WALK")
        self._tag(walked.parent.parent, 15)
        (self.ws / "walked").symlink_to(self.outside / "walked")
        prev = self.root / "server-prev"
        (prev / "old" / "outputs").mkdir(parents=True)
        prev_secret = self._outside_file("server-prev.txt", "OUTSIDE_SERVER_PREV")
        (prev / "old" / "outputs").rmdir()
        (prev / "old" / "outputs").symlink_to(prev_secret.parent)
        out = self.root / "review.html"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.ws),
                "--static",
                str(out),
                "--skill-name",
                "demo",
                "--previous-workspace",
                str(prev),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        html = out.read_text()
        self._assert_legit(html)
        for item in (marker, "OUTSIDE_SERVER_WALK", "OUTSIDE_SERVER_PREV"):
            self._assert_hidden(html, item)

        feedback_target = self._outside_file("feedback-api.txt", "OUTSIDE_FEEDBACK_API")
        (self.ws / "feedback.json").symlink_to(feedback_target)
        status, body = _http_request(self.ws, "/")
        self.assertEqual(status, 200)
        page = body.decode()
        self._assert_legit(page)
        for item in (marker, "OUTSIDE_SERVER_WALK", "OUTSIDE_FEEDBACK_API"):
            self._assert_hidden(page, item)
        status, feedback_body = _http_request(self.ws, "/api/feedback")
        self.assertEqual(status, 200)
        self._assert_hidden(feedback_body.decode(errors="replace"), "OUTSIDE_FEEDBACK_API")
        self.assertEqual(feedback_target.read_text(), "OUTSIDE_FEEDBACK_API")
        self.assertEqual(secret.read_text(), marker)
        self.assertEqual(walked.read_text(), "OUTSIDE_SERVER_WALK")

    def test_feedback_post_does_not_write_through_symlink(self) -> None:
        target = self._outside_file("post-target.txt", OUTSIDE_ORIGINAL)
        (self.ws / "feedback.json").symlink_to(target)
        status, _body = _http_request(
            self.ws,
            "/api/feedback",
            post=b'{"reviews":[{"run_id":"legit-run","feedback":"ok"}]}',
        )
        self.assertEqual(target.read_bytes(), OUTSIDE_ORIGINAL)
        self.assertIn(status, (200, 400, 500))

    def test_toctou_output_file_and_each_embed_kind(self) -> None:
        kinds = {
            "data.txt": "OUTSIDE_RACE_TEXT",
            "data.md": "OUTSIDE_RACE_MD",
            "data.png": "OUTSIDE_RACE_PNG",
            "data.pdf": "OUTSIDE_RACE_PDF",
            "data.xlsx": "OUTSIDE_RACE_XLSX",
            "data.bin": "OUTSIDE_RACE_BIN",
        }
        for name, marker in kinds.items():
            with self.subTest(name=name):
                self._assert_toctou_find_runs(
                    relative=Path("kinds") / "outputs" / name,
                    benign=b"SAFE_INSIDE",
                    outside_name=name,
                    outside_content=marker.encode(),
                    marker=marker,
                )

    def test_toctou_outputs_directory_and_walk(self) -> None:
        with self.subTest("outputs directory"):
            secret = "OUTSIDE_RACE_OUTPUTS_DIR"
            outside = self.outside / "race-outputs"
            outside.mkdir()
            (outside / "secret.txt").write_text(secret)
            self._tag(self.ws / "race-run", 17)
            real = self.root / "real-outputs"
            real.mkdir()
            (real / "x.txt").write_text("plain")
            self._assert_toctou_tree(
                entry_rel=Path("race-run") / "outputs",
                real=real,
                outside=outside,
                marker=secret,
            )
        with self.subTest("ancestor directory"):
            secret = "OUTSIDE_RACE_WALK"
            outside = self.outside / "race-walk"
            (outside / "nested" / "outputs").mkdir(parents=True)
            (outside / "nested" / "outputs" / "secret.txt").write_text(secret)
            self._tag(outside / "nested", 18)
            real = self.root / "real-tree"
            (real / "note").mkdir(parents=True)
            (real / "note" / "a.txt").write_text("plain")
            self._assert_toctou_tree(
                entry_rel=Path("planted"),
                real=real,
                outside=outside,
                marker=secret,
            )

    def test_toctou_metadata_transcript_grading_and_feedback(self) -> None:
        cases = {
            "eval_metadata.json": (
                b'{"prompt":"benign","eval_id":1}',
                json.dumps({"prompt": "OUTSIDE_RACE_METADATA", "eval_id": 9}).encode(),
                "OUTSIDE_RACE_METADATA",
                Path("meta-race") / "eval_metadata.json",
            ),
            "transcript.md": (
                b"## Eval Prompt\n\nbenign\n",
                b"## Eval Prompt\n\nOUTSIDE_RACE_TRANSCRIPT\n",
                "OUTSIDE_RACE_TRANSCRIPT",
                Path("transcript-race") / "transcript.md",
            ),
            "grading.json": (
                b'{"expectations":[]}',
                json.dumps(
                    {"expectations": [{"text": "OUTSIDE_RACE_GRADING", "passed": False}]}
                ).encode(),
                "OUTSIDE_RACE_GRADING",
                Path("grading-race") / "grading.json",
            ),
        }
        for label, (benign, outside_content, marker, relative) in cases.items():
            with self.subTest(label):
                run = self.ws / relative.parent
                (run / "outputs").mkdir(parents=True)
                (run / "outputs" / "hello.txt").write_text(LEGIT)
                self._assert_toctou_find_runs(
                    relative=relative,
                    benign=benign,
                    outside_name=label,
                    outside_content=outside_content,
                    marker=marker,
                )
        with self.subTest("previous feedback"):
            prev = self.root / "prev-race"
            (prev / "old" / "outputs").mkdir(parents=True)
            (prev / "old" / "outputs" / "hello.txt").write_text(LEGIT)
            self._assert_toctou_command(
                workspace=prev,
                relative=Path("feedback.json"),
                benign=b'{"reviews":[{"run_id":"old","feedback":"benign"}]}',
                outside_name="race-feedback.json",
                outside_content=json.dumps(
                    {"reviews": [{"run_id": "old", "feedback": "OUTSIDE_RACE_FEEDBACK"}]}
                ).encode(),
                marker="OUTSIDE_RACE_FEEDBACK",
                code=(
                    "import importlib.util, json, os\n"
                    "from pathlib import Path\n"
                    "spec = importlib.util.spec_from_file_location('g', os.environ['SCRIPT'])\n"
                    "mod = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(mod)\n"
                    "prev = mod.load_previous_iteration(Path(os.environ['WS']))\n"
                    "html = mod.generate_html([], 'skill', prev)\n"
                    "blob = json.dumps(prev) + html\n"
                    "marker = os.environ['MARKER']\n"
                    "print('disclosure=SECRET' if marker in blob else 'disclosure=NOSECRET')\n"
                    "print('legit=LEGIT' if os.environ['LEGIT'] in blob else 'legit=NOLEGIT')\n"
                ),
            )

    def test_toctou_static_cli_and_feedback_post(self) -> None:
        with self.subTest("static cli"):
            secret = self._outside_file("cli-secret.txt", "OUTSIDE_RACE_CLI")
            run = self.ws / "cli-race" / "outputs"
            run.mkdir(parents=True)
            self._tag(run.parent, 16)
            (run / "hello.txt").write_text(LEGIT)
            entry = run / "data.txt"
            entry.write_text("SAFE_INSIDE")
            spare = self.root / "cli-spare"
            os.symlink(secret, spare)
            out = self.root / "race-review.html"
            before = secret.read_bytes()
            env = _harness_env(entry, spare)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(self.ws),
                    "--static",
                    str(out),
                    "--skill-name",
                    "demo",
                ],
                text=True,
                capture_output=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            html = out.read_text()
            self._assert_legit(html)
            self._assert_hidden(html, "OUTSIDE_RACE_CLI")
            self.assertEqual(secret.read_bytes(), before)

        with self.subTest("feedback post"):
            target = self._outside_file("post-race.txt", OUTSIDE_ORIGINAL)
            entry = self.ws / "feedback.json"
            entry.write_text('{"reviews":[]}\n')
            spare = self.root / "post-spare"
            os.symlink(target, spare)
            code = (
                "import importlib.util, os\n"
                "from functools import partial\n"
                "from http.server import HTTPServer\n"
                "from pathlib import Path\n"
                "from urllib.request import Request, urlopen\n"
                "spec = importlib.util.spec_from_file_location('g', os.environ['SCRIPT'])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "ws = Path(os.environ['WS'])\n"
                "handler = partial(mod.ReviewHandler, ws, 'skill', ws / 'feedback.json', {}, None)\n"
                "server = HTTPServer(('127.0.0.1', 0), handler)\n"
                "port = server.server_address[1]\n"
                "import threading\n"
                "thread = threading.Thread(target=server.serve_forever, daemon=True)\n"
                "thread.start()\n"
                "try:\n"
                "    req = Request(\n"
                "        f'http://127.0.0.1:{port}/api/feedback',\n"
                "        data=b'{\"reviews\":[{\"run_id\":\"legit-run\",\"feedback\":\"changed\"}]}',\n"
                "        method='POST',\n"
                "    )\n"
                "    req.add_header('Content-Type', 'application/json')\n"
                "    urlopen(req, timeout=5).read()\n"
                "finally:\n"
                "    server.shutdown()\n"
                "    server.server_close()\n"
                "print('done')\n"
            )
            env = _harness_env(entry, spare)
            env["SCRIPT"] = str(SCRIPT)
            env["WS"] = str(self.ws)
            result = _run_python(code, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), OUTSIDE_ORIGINAL)

    def _assert_toctou_find_runs(
        self,
        relative: Path,
        benign: bytes,
        outside_name: str,
        outside_content: bytes,
        marker: str,
    ) -> None:
        code = (
            "import importlib.util, json, os\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location('g', os.environ['SCRIPT'])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "blob = json.dumps(mod.find_runs(Path(os.environ['WS'])))\n"
            "html = mod.generate_html(mod.find_runs(Path(os.environ['WS'])), 'skill')\n"
            "marker = os.environ['MARKER']\n"
            "import base64\n"
            "encoded = base64.b64encode(marker.encode()).decode()\n"
            "leaked = marker in blob or marker in html or encoded in blob or encoded in html\n"
            "print('disclosure=SECRET' if leaked else 'disclosure=NOSECRET')\n"
            "print('legit=LEGIT' if os.environ['LEGIT'] in blob + html else 'legit=NOLEGIT')\n"
        )
        self._assert_toctou_command(
            workspace=self.ws,
            relative=relative,
            benign=benign,
            outside_name=outside_name,
            outside_content=outside_content,
            marker=marker,
            code=code,
        )

    def _assert_toctou_tree(self, entry_rel: Path, real: Path, outside: Path, marker: str) -> None:
        entry = self.ws / entry_rel
        entry.parent.mkdir(parents=True, exist_ok=True)
        if entry.exists() or entry.is_symlink():
            raise AssertionError(f"{entry} already exists")
        os.rename(real, entry)
        spare = self.root / f"spare-{entry_rel.name}"
        os.symlink(outside, spare)
        secret_files = [path for path in outside.rglob("*") if path.is_file()]
        before = {path: path.read_bytes() for path in secret_files}
        code = (
            "import importlib.util, json, os\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location('g', os.environ['SCRIPT'])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "runs = mod.find_runs(Path(os.environ['WS']))\n"
            "blob = json.dumps(runs) + mod.generate_html(runs, 'skill')\n"
            "marker = os.environ['MARKER']\n"
            "print('disclosure=SECRET' if marker in blob else 'disclosure=NOSECRET')\n"
            "print('legit=LEGIT' if os.environ['LEGIT'] in blob else 'legit=NOLEGIT')\n"
        )
        result = self._preload_product(entry, spare, code, self.ws, marker)
        self._assert_driver_clean(result, marker)
        self._assert_outside_unchanged(before)

    def _assert_toctou_command(
        self,
        workspace: Path,
        relative: Path,
        benign: bytes,
        outside_name: str,
        outside_content: bytes,
        marker: str,
        code: str,
    ) -> None:
        target = self._outside_file(outside_name, outside_content)
        entry = workspace / relative
        entry.parent.mkdir(parents=True, exist_ok=True)
        run_dir = entry.parent.parent if entry.parent.name == "outputs" else entry.parent
        if entry.name != "eval_metadata.json":
            self._tag(run_dir, 19)
        if entry.name == "transcript.md":
            (run_dir / "eval_metadata.json").write_text(json.dumps({"prompt": "", "eval_id": 19}))
        entry.write_bytes(benign)
        spare = self.root / f"spare-{relative.name}-{marker}"
        os.symlink(target, spare)
        before = target.read_bytes()
        result = self._preload_product(entry, spare, code, workspace, marker)
        self._assert_driver_clean(result, marker)
        self.assertEqual(target.read_bytes(), before)

    def _preload_product(
        self,
        entry: Path,
        spare: Path,
        code: str,
        workspace: Path,
        marker: str,
    ) -> subprocess.CompletedProcess[str]:
        env = _harness_env(entry, spare)
        env["SCRIPT"] = str(SCRIPT)
        env["WS"] = str(workspace)
        env["MARKER"] = marker
        env["LEGIT"] = LEGIT
        return _run_python(code, env)

    def _assert_driver_clean(self, result: subprocess.CompletedProcess[str], marker: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = set(result.stdout.splitlines())
        self.assertIn("disclosure=NOSECRET", lines, f"{marker} leaked\n{result.stdout}\n{result.stderr}")
        self.assertIn("legit=LEGIT", lines, result.stdout)


def _http_request(workspace: Path, path: str, post: bytes | None = None) -> tuple[int, bytes]:
    feedback = workspace / "feedback.json"
    handler = partial(gr.ReviewHandler, workspace, "skill", feedback, {}, None)
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}{path}"
        if post is None:
            with urlopen(url, timeout=5) as response:
                return response.status, response.read()
        request = Request(url, data=post, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except Exception as exc:
            status = getattr(getattr(exc, "code", None), "real", None)
            if getattr(exc, "code", None) is not None:
                return exc.code, getattr(exc, "read", lambda: b"")()
            raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
