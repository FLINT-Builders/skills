#!/usr/bin/env python3
"""Benchmark fields must not execute in the eval review page.

These tests fail while the vulnerability is present. They pass once every
benchmark string is escaped before it is placed in HTML, the benchmark table
is built with text assignment rather than HTML concatenation, and the JSON
embedded in the page cannot close the surrounding script element.

Run from the repository root:

    python3 skills/skill-creator/eval-viewer/test_benchmark_html_escaping.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import HTTPServer
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = VIEWER_DIR.parent / "scripts"
sys.path.insert(0, str(VIEWER_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import aggregate_benchmark  # noqa: E402
import generate_review  # noqa: E402

# Word-start letters are written as semicolon-less numeric character references.
# The benchmark renderer capitalizes every word start before assigning innerHTML,
# and a directory name cannot contain "/". Both forms still execute today.
CONFIG_PAYLOAD = (
    "<img src=x onerror=\"&#100ocument.&#98ody.&#115etAttribute('&#100ata-&#99onfig','1');"
    "&#102etch('&#47&#97pi&#47&#102eedback',{&#109ethod:'POST',"
    "&#98ody:'{&#34&#114eviews&#34:[{&#34&#102eedback&#34:&#34&#112wned&#34}]}'})\">"
)
SCRIPT_BREAKOUT = (
    "</script><img src=x onerror=document.body.setAttribute(&#39;data-break&#39;,&#39;1&#39;)>"
)
EVIDENCE_BREAKOUT = "\" onmouseover=\"document.body.setAttribute('data-ev','1')\" x=\""


def _marker(name: str) -> str:
    return f"<img src=x onerror=\"document.body.setAttribute('data-{name}','1')\">"


def _chrome() -> str:
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    raise unittest.SkipTest("headless Chrome is not installed")


def _workspace(root: Path) -> Path:
    outputs = root / "ws" / "eval-0" / "with_skill" / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "out.txt").write_text("secret-eval-output")
    (outputs.parent / "eval_metadata.json").write_text(
        json.dumps({"eval_id": 0, "prompt": "Say hello"})
    )
    return root / "ws"


def _body_attrs(dom: str) -> str:
    rendered = dom.split("const EMBEDDED_DATA", 1)[0]
    match = re.search(r"<body\b([^>]*)>", rendered)
    return match.group(1) if match else ""


def _open_review(workspace: Path, benchmark: dict) -> tuple[str, str]:
    benchmark_path = workspace / "benchmark.json"
    benchmark_path.write_text(json.dumps(benchmark))
    feedback_path = workspace / "feedback.json"
    feedback_path.write_text(
        json.dumps({"reviews": [{"run_id": "eval-0-with_skill", "feedback": "user wrote this"}]})
        + "\n"
    )
    handler = partial(
        generate_review.ReviewHandler,
        workspace,
        "demo-skill",
        feedback_path,
        {},
        benchmark_path,
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = workspace.parent / "chrome-profile"
    proc = subprocess.Popen(
        [
            _chrome(),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=8000",
            "--timeout=12000",
            "--dump-dom",
            f"http://127.0.0.1:{server.server_address[1]}/",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        stdout, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
    finally:
        server.shutdown()
    return stdout.decode("utf-8", "replace"), feedback_path.read_text()


def _grading(pass_rate: float = 1.0) -> dict:
    return {
        "summary": {"pass_rate": pass_rate, "passed": 1, "failed": 0, "total": 1},
        "expectations": [{"text": "checked", "passed": True, "evidence": "plain"}],
    }


class BenchmarkHtmlEscapingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_configuration_directory_name_cannot_run_script_or_overwrite_feedback(self) -> None:
        """Attack path: config directory name -> aggregate_benchmark -> review page."""
        bench = self.root / "bench"
        run = bench / "eval-0" / CONFIG_PAYLOAD / "run-1"
        run.mkdir(parents=True)
        (run / "grading.json").write_text(json.dumps(_grading()))
        plain = bench / "eval-0" / "without_skill" / "run-1"
        plain.mkdir(parents=True)
        (plain / "grading.json").write_text(json.dumps(_grading(0.0)))
        (bench / "eval-0" / "eval_metadata.json").write_text(
            json.dumps({"eval_id": 0, "eval_name": "demo", "prompt": "Say hello"})
        )
        benchmark = aggregate_benchmark.generate_benchmark(bench, skill_name="demo-skill")
        self.assertIn(CONFIG_PAYLOAD, [row["configuration"] for row in benchmark["runs"]])

        workspace = _workspace(self.root)
        dom, feedback = _open_review(workspace, benchmark)
        rendered = dom.split("const EMBEDDED_DATA", 1)[0]
        self.assertIn("Without Skill", rendered)
        self.assertIn("secret-eval-output", rendered)
        self.assertNotIn("data-config", _body_attrs(dom))
        self.assertNotIn("pwned", feedback)
        self.assertIn("user wrote this", feedback)

    def test_benchmark_fields_cannot_execute_when_the_file_is_supplied(self) -> None:
        """timestamp, eval list, run count, deltas, run number, and evidence."""
        workspace = _workspace(self.root)
        benchmark = {
            "metadata": {
                "skill_name": "demo-skill",
                "timestamp": _marker("timestamp"),
                "evals_run": [_marker("evals")],
                "runs_per_configuration": _marker("runs"),
            },
            "runs": [
                {
                    "eval_id": 0,
                    "eval_name": "Demo",
                    "configuration": "with_skill",
                    "run_number": _marker("run"),
                    "result": {
                        "pass_rate": 1,
                        "passed": 1,
                        "failed": 0,
                        "total": 1,
                        "time_seconds": 1.0,
                        "errors": 0,
                    },
                    "expectations": [
                        {"text": "assertion", "passed": True, "evidence": EVIDENCE_BREAKOUT}
                    ],
                }
            ],
            "run_summary": {
                "with_skill": {
                    "pass_rate": {"mean": 1, "stddev": 0},
                    "time_seconds": {"mean": 1, "stddev": 0},
                    "tokens": {"mean": 1, "stddev": 0},
                },
                "delta": {
                    "pass_rate": _marker("dpass"),
                    "time_seconds": _marker("dtime"),
                    "tokens": _marker("dtok"),
                },
            },
            "notes": ["safe note"],
        }
        dom, _feedback = _open_review(workspace, benchmark)
        rendered = dom.split("const EMBEDDED_DATA", 1)[0]
        attrs = _body_attrs(dom)
        self.assertIn("With Skill", rendered)
        for name in ("timestamp", "evals", "runs", "run", "dpass", "dtime", "dtok"):
            self.assertNotIn(f"data-{name}", attrs, name)
        self.assertNotIn(
            "onmouseover=\"document.body.setAttribute('data-ev','1')\"",
            rendered,
        )

    def test_eval_id_from_metadata_cannot_execute(self) -> None:
        bench = self.root / "bench"
        run = bench / "eval-0" / "with_skill" / "run-1"
        run.mkdir(parents=True)
        (run / "grading.json").write_text(json.dumps(_grading()))
        (bench / "eval-0" / "eval_metadata.json").write_text(
            json.dumps({"eval_id": _marker("evalid"), "eval_name": "demo", "prompt": "Say hello"})
        )
        benchmark = aggregate_benchmark.generate_benchmark(bench, skill_name="demo-skill")
        self.assertEqual(benchmark["metadata"]["evals_run"], [_marker("evalid")])
        workspace = _workspace(self.root)
        dom, _feedback = _open_review(workspace, benchmark)
        self.assertNotIn("data-evalid", _body_attrs(dom))

    def test_benchmark_text_cannot_close_the_embedding_script(self) -> None:
        workspace = _workspace(self.root)
        benchmark = {
            "metadata": {
                "skill_name": "demo-skill",
                "timestamp": SCRIPT_BREAKOUT,
                "evals_run": [0],
                "runs_per_configuration": 1,
            },
            "runs": [
                {
                    "eval_id": 0,
                    "configuration": "with_skill",
                    "run_number": 1,
                    "result": {"pass_rate": 1, "passed": 1, "total": 1},
                    "expectations": [],
                }
            ],
            "run_summary": {"with_skill": {"pass_rate": {"mean": 1, "stddev": 0}}},
            "notes": [],
        }
        dom, _feedback = _open_review(workspace, benchmark)
        self.assertNotIn("data-break", _body_attrs(dom))


if __name__ == "__main__":
    unittest.main()
