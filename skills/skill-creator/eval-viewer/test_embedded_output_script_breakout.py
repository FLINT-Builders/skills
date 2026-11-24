#!/usr/bin/env python3
"""Eval review HTML must not execute script planted in eval data.

generate_review.py embeds workspace JSON inside the inline <script> in
viewer.html. These tests load that page in Chrome through the real server
and the --static CLI. They fail while a closing script tag in eval data
runs on the review origin, and they pass once that data can no longer end
the script element and the page still renders the output text.

Run:
    python3 skills/skill-creator/eval-viewer/test_embedded_output_script_breakout.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import HTTPServer
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parent
SCRIPT = VIEWER_DIR / "generate_review.py"
CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable")

# Sequences Chrome treats as the end of the host script element when they
# appear literally in the HTML source. json.dumps leaves these intact.
SCRIPT_CLOSERS = (
    "</script>",
    "</SCRIPT>",
    "</ScRiPt>",
    "</script >",
    "</script/>",
)

MARKER = "XSS_FIRED"
BENIGN_OUTPUT = "benign output"
HUMAN_FEEDBACK = '{"reviews":[{"run_id":"keep","feedback":"human notes"}]}\n'


def _load_review():
    spec = importlib.util.spec_from_file_location("generate_review", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REVIEW = _load_review()


def breakout(closer: str = "</script>") -> str:
    """Markup that runs only if the HTML parser ends the host script element."""
    return f"{closer}<script>document.title='{MARKER}'</script>"


def feedback_breakout() -> str:
    """Same breakout, plus a same-origin POST that overwrites feedback.json.

    The script body uses only single quotes so json.dumps leaves it as
    valid JavaScript. The review server requires a JSON object body.
    """
    return (
        "</script><script>document.title='XSS_FIRED';"
        "var q=String.fromCharCode(34);"
        "var body='{'+q+'reviews'+q+':[{'+q+'run_id'+q+':'+q+'owned'+q+','"
        "+q+'feedback'+q+':'+q+'STEER_THE_SKILL'+q+'}]}';"
        "fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:body});"
        "</script>"
    )


def dump_dom(url: str) -> str:
    """Return Chrome's serialized DOM after it has parsed and run scripts."""
    if not CHROME:
        raise AssertionError("google-chrome is required to parse the review page")
    profile = tempfile.mkdtemp(prefix="review-xss-chrome-")
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--user-data-dir={profile}",
            "--dump-dom",
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    data = bytearray()
    deadline = time.time() + 15
    try:
        while time.time() < deadline:
            try:
                chunk = os.read(fd, 1 << 16)
            except BlockingIOError:
                if b"</html>" in data:
                    break
                time.sleep(0.02)
                continue
            if not chunk:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            data.extend(chunk)
            if b"</html>" in data:
                break
    finally:
        proc.kill()
        proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        shutil.rmtree(profile, ignore_errors=True)
    dom = data.decode("utf-8", "replace")
    if "</html>" not in dom:
        raise AssertionError(f"Chrome did not produce a document for {url}")
    return dom


def assert_rendered_safely(test: unittest.TestCase, dom: str) -> None:
    """The host page script ran, and the planted script did not."""
    title = re.search(r"<title>(.*?)</title>", dom)
    title_text = title.group(1) if title else "<missing>"
    test.assertEqual(
        title_text,
        "Eval Review",
        "planted script changed the document title",
    )
    # renderOutputs sets this via textContent. The raw JSON also contains
    # the words, but not this element, so a broken script cannot satisfy it.
    if f"<pre>{BENIGN_OUTPUT}</pre>" not in dom:
        test.fail("benign output was not rendered; the host script did not finish")


class ReviewServer:
    def __init__(
        self,
        workspace: Path,
        feedback: Path,
        previous: dict | None = None,
        benchmark: Path | None = None,
    ):
        handler = partial(
            REVIEW.ReviewHandler,
            workspace,
            "demo-skill",
            feedback,
            previous or {},
            benchmark,
        )
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_run(root: Path, *, prompt: str | None = "SAFE_PROMPT_RENDERED") -> Path:
    run = root / "eval-xss" / "with_skill"
    outputs = run / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "benign.txt").write_text(BENIGN_OUTPUT)
    if prompt is not None:
        (run / "eval_metadata.json").write_text(
            json.dumps({"eval_id": 0, "prompt": prompt, "assertions": []})
        )
    return run


class EmbeddedScriptBreakoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="review-xss-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _serve(self, workspace: Path, feedback: Path, **kwargs) -> ReviewServer:
        server = ReviewServer(workspace, feedback, **kwargs)
        self.addCleanup(server.close)
        return server

    def _feedback(self, workspace: Path) -> Path:
        path = workspace / "feedback.json"
        path.write_text(HUMAN_FEEDBACK)
        return path

    def test_text_outputs_cannot_end_the_script_element(self) -> None:
        # embed_file reads every TEXT_EXTENSIONS file verbatim. .html, .md,
        # .txt and .json are the extensions a copied page, note, or document
        # is likely to land in.
        for extension in (".html", ".md", ".txt", ".json"):
            for closer in SCRIPT_CLOSERS:
                with self.subTest(extension=extension, closer=closer):
                    workspace = self.tmp / f"out{extension}{abs(hash(closer)) % 10000}"
                    run = make_run(workspace)
                    planted = "copied from the page\n" + breakout(closer) + "\n"
                    (run / "outputs" / f"result{extension}").write_text(planted)
                    feedback = self._feedback(workspace)
                    dom = dump_dom(self._serve(workspace, feedback).url)
                    assert_rendered_safely(self, dom)
                    self.assertEqual(feedback.read_text(), HUMAN_FEEDBACK)

    def test_output_html_cannot_overwrite_feedback_on_page_load(self) -> None:
        workspace = self.tmp / "feedback"
        run = make_run(workspace)
        (run / "outputs" / "result.html").write_text(
            "copied from the page\n" + feedback_breakout() + "\n"
        )
        feedback = self._feedback(workspace)
        dom = dump_dom(self._serve(workspace, feedback).url)
        deadline = time.time() + 2
        while time.time() < deadline:
            if "STEER_THE_SKILL" in feedback.read_text():
                break
            time.sleep(0.05)
        self.assertNotIn(
            "STEER_THE_SKILL",
            feedback.read_text(),
            "loading the review page overwrote feedback.json",
        )
        self.assertIn("human notes", feedback.read_text())
        assert_rendered_safely(self, dom)

    def test_static_cli_cannot_end_the_script_element(self) -> None:
        workspace = self.tmp / "static-ws"
        run = make_run(workspace)
        (run / "outputs" / "result.html").write_text(
            "copied from the page\n" + breakout() + "\n"
        )
        html_path = self.tmp / "review.html"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(workspace),
                "--static",
                str(html_path),
                "--skill-name",
                "demo-skill",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        dom = dump_dom(html_path.resolve().as_uri())
        assert_rendered_safely(self, dom)

    def test_eval_metadata_prompt_cannot_end_the_script_element(self) -> None:
        workspace = self.tmp / "prompt"
        make_run(workspace, prompt="copied from the page\n" + breakout())
        feedback = self._feedback(workspace)
        dom = dump_dom(self._serve(workspace, feedback).url)
        assert_rendered_safely(self, dom)

    def test_transcript_prompt_cannot_end_the_script_element(self) -> None:
        workspace = self.tmp / "transcript"
        run = make_run(workspace, prompt=None)
        (run / "transcript.md").write_text(
            "## Eval Prompt\n\ncopied from the page\n" + breakout() + "\n\n## Notes\n\nnone\n"
        )
        feedback = self._feedback(workspace)
        dom = dump_dom(self._serve(workspace, feedback).url)
        assert_rendered_safely(self, dom)

    def test_grading_json_cannot_end_the_script_element(self) -> None:
        workspace = self.tmp / "grading"
        run = make_run(workspace)
        grading = {
            "summary": {"pass_rate": 0, "passed": 0, "failed": 1, "total": 1},
            "expectations": [
                {
                    "text": "copied from the page " + breakout(),
                    "passed": False,
                    "evidence": "quoted from the output",
                }
            ],
        }
        (run / "grading.json").write_text(json.dumps(grading))
        feedback = self._feedback(workspace)
        dom = dump_dom(self._serve(workspace, feedback).url)
        assert_rendered_safely(self, dom)

    def test_previous_output_cannot_end_the_script_element(self) -> None:
        current = self.tmp / "current-output"
        make_run(current)
        previous_root = self.tmp / "previous-output"
        previous_run = make_run(previous_root)
        (previous_run / "outputs" / "result.md").write_text(
            "copied from the page\n" + breakout() + "\n"
        )
        feedback = self._feedback(current)
        previous = REVIEW.load_previous_iteration(previous_root)
        dom = dump_dom(self._serve(current, feedback, previous=previous).url)
        assert_rendered_safely(self, dom)

    def test_previous_feedback_cannot_end_the_script_element(self) -> None:
        current = self.tmp / "current-feedback"
        make_run(current)
        previous_root = self.tmp / "previous-feedback"
        make_run(previous_root)
        (previous_root / "feedback.json").write_text(
            json.dumps(
                {
                    "reviews": [
                        {
                            "run_id": "eval-xss-with_skill",
                            "feedback": "copied from the page " + breakout(),
                        }
                    ]
                }
            )
        )
        feedback = self._feedback(current)
        previous = REVIEW.load_previous_iteration(previous_root)
        dom = dump_dom(self._serve(current, feedback, previous=previous).url)
        assert_rendered_safely(self, dom)

    def test_benchmark_json_cannot_end_the_script_element(self) -> None:
        workspace = self.tmp / "benchmark"
        make_run(workspace)
        benchmark = workspace / "benchmark.json"
        benchmark.write_text(
            json.dumps(
                {
                    "metadata": {"skill_name": "demo-skill"},
                    "run_summary": {},
                    "notes": ["copied from the page " + breakout()],
                    "runs": [
                        {
                            "eval_id": 0,
                            "eval_name": "copied",
                            "configuration": "with_skill",
                            "run_number": 1,
                            "expectations": [
                                {
                                    "text": "copied from the page " + breakout(),
                                    "passed": False,
                                    "evidence": "quoted",
                                }
                            ],
                            "result": {"pass_rate": 0, "passed": 0, "failed": 1, "total": 1},
                        }
                    ],
                }
            )
        )
        feedback = self._feedback(workspace)
        dom = dump_dom(self._serve(workspace, feedback, benchmark=benchmark).url)
        assert_rendered_safely(self, dom)


if __name__ == "__main__":
    unittest.main()
