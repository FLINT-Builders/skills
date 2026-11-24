#!/usr/bin/env python3
"""Grade-count XSS in the skill eval review page.

These tests fail while summary.passed, summary.failed, and summary.total from
grading.json (and the same counts copied into benchmark.json) are inserted
with innerHTML. They pass once those values are encoded and shown as text.

Run from the repository root:

    python3 skills/skill-creator/eval-viewer/test_grade_count_xss.py

Requires Google Chrome (google-chrome) on PATH. No third-party Python packages.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from functools import partial
from http.server import HTTPServer
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(VIEWER_DIR))

from generate_review import ReviewHandler  # noqa: E402

SKILL_CREATOR = VIEWER_DIR.parent
sys.path.insert(0, str(SKILL_CREATOR / "scripts"))
import aggregate_benchmark  # noqa: E402

generate_benchmark = aggregate_benchmark.generate_benchmark

SECRET_PROMPT = "SECRET_PROMPT_VALUE"
SECRET_OUTPUT = "SECRET_OUTPUT_VALUE"
CHROME = os.environ.get("CHROME", "google-chrome")


def grade_payload(token: str) -> str:
    """Image handler that posts every embedded prompt and output to feedback.

    The grades panel is not opened. fetch is asynchronous so it does not
    deadlock the review server's single request thread.
    """
    return (
        f"<img src=x alt={token} onerror=\""
        "var bits=[];"
        "EMBEDDED_DATA.runs.forEach(function(r){"
        "bits.push(r.prompt);"
        "(r.outputs||[]).forEach(function(f){if(f.content)bits.push(f.content)});"
        "});"
        "fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({reviews:[{run_id:'exfil',feedback:bits.join(String.fromCharCode(10))}]})});"
        "\">"
    )


def inner_html_by_id(document: str, element_id: str) -> str:
    marker = f'id="{element_id}"'
    start = document.find(marker)
    if start < 0:
        raise AssertionError(f"rendered page has no #{element_id}")
    open_end = document.find(">", start)
    tag_start = document.rfind("<", 0, start)
    opening = document[tag_start : open_end + 1]
    depth = 1
    i = open_end + 1
    while i < len(document) and depth:
        next_open = document.find("<div", i)
        next_close = document.find("</div>", i)
        if next_close < 0:
            break
        if 0 <= next_open < next_close:
            depth += 1
            i = next_open + 4
        else:
            depth -= 1
            if depth == 0:
                return opening + document[open_end + 1 : next_close]
            i = next_close + 6
    raise AssertionError(f"could not find the end of #{element_id}")


class ReviewServer:
    def __init__(self, workspace: Path, benchmark: dict | None = None):
        self.workspace = workspace
        self.feedback_path = workspace / "feedback.json"
        self.benchmark_path = None
        if benchmark is not None:
            self.benchmark_path = workspace / "benchmark.json"
            self.benchmark_path.write_text(json.dumps(benchmark))
        handler = partial(
            ReviewHandler,
            workspace,
            "demo-skill",
            self.feedback_path,
            {},
            self.benchmark_path,
        )
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


def load_in_chrome(url: str, dom_path: Path, feedback_path: Path) -> str:
    """Load the review page in headless Chrome and return the dumped DOM.

    Chrome's --dump-dom process has been observed to keep running after it
    has already written the DOM and the payload has posted feedback. The
    process is killed once both results are on disk, or after a short wait.
    """
    dom_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(dom_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--host-resolver-rules="
            "MAP fonts.googleapis.com 127.0.0.1, "
            "MAP fonts.gstatic.com 127.0.0.1, "
            "MAP cdn.sheetjs.com 127.0.0.1",
            "--virtual-time-budget=5000",
            "--timeout=8000",
            "--dump-dom",
            url,
        ],
        stdout=out,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 12
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            dom_ready = dom_path.exists() and "grades-content" in dom_path.read_text(
                errors="replace"
            )
            feedback_ready = feedback_path.exists()
            if dom_ready and (feedback_ready or time.time() + 3 > deadline):
                # Give an in-flight feedback POST a moment, then stop Chrome.
                if not feedback_ready:
                    time.sleep(1.5)
                break
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        out.close()
    return dom_path.read_text(errors="replace")


def write_run(root: Path, rel: str, prompt: str, output: str, grading: dict | None, eval_id: int):
    run = root / rel
    (run / "outputs").mkdir(parents=True, exist_ok=True)
    (run / "outputs" / "note.txt").write_text(output)
    (run / "eval_metadata.json").write_text(
        json.dumps(
            {
                "eval_id": eval_id,
                "eval_name": f"eval-{eval_id}",
                "prompt": prompt,
                "assertions": [],
            }
        )
    )
    if grading is not None:
        (run / "grading.json").write_text(json.dumps(grading))
    return run


def grading_with(**summary_fields) -> dict:
    summary = {"passed": 0, "failed": 0, "total": 1, "pass_rate": 0.0}
    summary.update(summary_fields)
    return {
        "expectations": [
            {"text": "safe assertion", "passed": True, "evidence": "safe evidence"}
        ],
        "summary": summary,
    }


class GradeCountXSSTests(unittest.TestCase):
    def assert_counts_are_text(self, dom: str, element_id: str, token: str, feedback: Path):
        element = inner_html_by_id(dom, element_id)
        self.assertIn("demo-skill", dom)
        self.assertIn(token, element)
        self.assertNotIn("<img", element)
        self.assertNotIn(SECRET_PROMPT, element)
        leaked = feedback.read_text() if feedback.exists() else ""
        self.assertNotIn(SECRET_PROMPT, leaked)
        self.assertNotIn(SECRET_OUTPUT, leaked)

    def _serve_and_load(self, workspace: Path, benchmark: dict | None = None) -> str:
        with ReviewServer(workspace, benchmark) as server:
            page = urllib.request.urlopen(server.url, timeout=5).read().decode()
            self.assertIn(SECRET_PROMPT, page)
            return load_in_chrome(
                server.url, workspace / "dom.html", workspace / "feedback.json"
            )

    def test_passed_count_is_text_while_grades_stay_collapsed(self):
        token = "GRADECOUNTXSS_PASSED"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "eval-0/with_skill",
                "prompt-for-payload-run",
                "output-of-payload-run",
                grading_with(passed=grade_payload(token)),
                0,
            )
            write_run(root, "eval-1/other", SECRET_PROMPT, SECRET_OUTPUT, None, 1)
            dom = self._serve_and_load(root)
            grades = inner_html_by_id(dom, "grades-content")
            self.assertNotIn("open", grades[: grades.find(">") + 1])
            self.assert_counts_are_text(dom, "grades-content", token, root / "feedback.json")

    def test_failed_count_is_text(self):
        token = "GRADECOUNTXSS_FAILED"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "eval-0/with_skill",
                "prompt-for-payload-run",
                "output-of-payload-run",
                grading_with(failed=grade_payload(token)),
                0,
            )
            write_run(root, "eval-1/other", SECRET_PROMPT, SECRET_OUTPUT, None, 1)
            dom = self._serve_and_load(root)
            self.assert_counts_are_text(dom, "grades-content", token, root / "feedback.json")

    def test_total_count_is_text(self):
        token = "GRADECOUNTXSS_TOTAL"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "eval-0/with_skill",
                "prompt-for-payload-run",
                "output-of-payload-run",
                grading_with(total=grade_payload(token)),
                0,
            )
            write_run(root, "eval-1/other", SECRET_PROMPT, SECRET_OUTPUT, None, 1)
            dom = self._serve_and_load(root)
            self.assert_counts_are_text(dom, "grades-content", token, root / "feedback.json")

    def test_parent_grading_json_counts_are_text(self):
        token = "GRADECOUNTXSS_PARENT"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = write_run(
                root,
                "eval-0/with_skill",
                "prompt-for-payload-run",
                "output-of-payload-run",
                None,
                0,
            )
            (run.parent / "grading.json").write_text(
                json.dumps(grading_with(passed=grade_payload(token)))
            )
            write_run(root, "eval-1/other", SECRET_PROMPT, SECRET_OUTPUT, None, 1)
            dom = self._serve_and_load(root)
            self.assert_counts_are_text(dom, "grades-content", token, root / "feedback.json")

    def test_benchmark_passed_and_total_from_grading_are_text(self):
        passed_token = "GRADECOUNTXSS_BENCH_PASSED"
        total_token = "GRADECOUNTXSS_BENCH_TOTAL"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Layout aggregate_benchmark scans: eval-*/<config>/run-*/grading.json.
            # The viewer also treats run-1 as a run because it contains outputs/.
            # Keep this run's own grading numeric so only the benchmark document
            # carries the strings aggregate copied out of the grading files.
            write_run(
                root,
                "eval-9/with_skill",
                "prompt-for-payload-run",
                "output-of-payload-run",
                grading_with(passed=2, failed=0, total=2, pass_rate=1.0),
                9,
            )
            for name, field, token in (
                ("run-1", "passed", passed_token),
                ("run-2", "total", total_token),
            ):
                run = root / "eval-0" / "with_skill" / name
                run.mkdir(parents=True)
                summary = {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0}
                summary[field] = grade_payload(token)
                (run / "grading.json").write_text(
                    json.dumps(
                        {
                            "expectations": [
                                {
                                    "text": "safe assertion",
                                    "passed": True,
                                    "evidence": "safe evidence",
                                }
                            ],
                            "summary": summary,
                            "timing": {"total_duration_seconds": 1.0},
                        }
                    )
                )
            (root / "eval-0" / "eval_metadata.json").write_text(
                json.dumps({"eval_id": 0, "eval_name": "bench", "prompt": "unused"})
            )
            benchmark = generate_benchmark(root, "demo-skill")
            encoded = json.dumps(benchmark)
            self.assertIn(passed_token, encoded)
            self.assertIn(total_token, encoded)
            write_run(root, "eval-1/other", SECRET_PROMPT, SECRET_OUTPUT, None, 1)
            dom = self._serve_and_load(root, benchmark)
            self.assert_counts_are_text(
                dom, "benchmark-content", passed_token, root / "feedback.json"
            )
            self.assert_counts_are_text(
                dom, "benchmark-content", total_token, root / "feedback.json"
            )

    def test_numeric_counts_render_and_do_not_write_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "eval-0/with_skill",
                "prompt-for-payload-run",
                "output-of-payload-run",
                grading_with(passed=2, failed=1, total=3, pass_rate=0.67),
                0,
            )
            write_run(root, "eval-1/other", SECRET_PROMPT, SECRET_OUTPUT, None, 1)
            dom = self._serve_and_load(root)
            grades = inner_html_by_id(dom, "grades-content")
            self.assertIn("2 passed, 1 failed of 3", grades)
            self.assertFalse((root / "feedback.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
