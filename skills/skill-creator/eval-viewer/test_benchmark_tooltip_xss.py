#!/usr/bin/env python3
"""Security tests for grader evidence rendered on the benchmark tab.

The product tells the grader to quote output text into grading.json, then
copies that evidence through benchmark.json into the review page. These tests
drive that code path and require the quoted text to stay tooltip text: opening
the Benchmark tab and hovering a pass/fail icon must not run it, and must not
change feedback.json.

They fail while evidence can break out of the title attribute.

Run from the repository root:

    python3 skills/skill-creator/eval-viewer/test_benchmark_tooltip_xss.py

Requires Python 3, Node.js (global WebSocket), and Google Chrome.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SKILL_CREATOR = REPO / "skills" / "skill-creator"
AGGREGATE = SKILL_CREATOR / "scripts" / "aggregate_benchmark.py"
GENERATE_REVIEW = SKILL_CREATOR / "eval-viewer" / "generate_review.py"

# Quoted from a skill output. The quote ends the title attribute; the rest is
# an event handler the benchmark table currently concatenates into innerHTML.
MOUSEOVER_EVIDENCE = (
    'a<b & c" onmouseover="window.__tooltipFired=1;fetch(\'/api/feedback\','
    "{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({reviews:[{run_id:'forged',feedback:'forged-by-tooltip'}],"
    "status:'complete'})})\""
)
CLICK_EVIDENCE = 'plain" onclick="window.__tooltipFired=1"'

ORIGINAL_FEEDBACK = {
    "reviews": [
        {
            "run_id": "eval-0-with_skill-run-1",
            "feedback": "the chart needs axis labels",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ],
    "status": "in_progress",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _chrome_bin() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    raise unittest.SkipTest("Chrome is not installed")


def _wait_http(url: str, proc: subprocess.Popen, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"review server exited {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {url}")


class BenchmarkEvidenceTooltipTest(unittest.TestCase):
    def test_quoted_output_evidence_cannot_run_or_rewrite_feedback(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.fail("node is required to drive Chrome")
        chrome = _chrome_bin()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            workspace = Path(tmp) / "workspace"
            run_dir = workspace / "eval-0" / "with_skill" / "run-1"
            outputs = run_dir / "outputs"
            outputs.mkdir(parents=True)
            # Skill output reproduces attacker-controlled source text.
            (outputs / "result.txt").write_text(
                "Extracted passage 1: " + MOUSEOVER_EVIDENCE + "\n"
                "Extracted passage 2: " + CLICK_EVIDENCE + "\n",
                encoding="utf-8",
            )
            (workspace / "eval-0" / "eval_metadata.json").write_text(
                json.dumps({"eval_id": 0, "prompt": "Extract the quoted passages."}) + "\n",
                encoding="utf-8",
            )
            # agents/grader.md tells the grader to quote the specific output text.
            source = (outputs / "result.txt").read_text(encoding="utf-8")
            grading = {
                "expectations": [
                    {
                        "text": "Passage 1 matches the source document",
                        "passed": False,
                        "evidence": source.split("Extracted passage 1: ", 1)[1].split("\n", 1)[0],
                    },
                    {
                        "text": "Passage 2 matches the source document",
                        "passed": True,
                        "evidence": source.split("Extracted passage 2: ", 1)[1].split("\n", 1)[0],
                    },
                ],
                "summary": {"passed": 1, "failed": 1, "total": 2, "pass_rate": 0.5},
            }
            (run_dir / "grading.json").write_text(json.dumps(grading, indent=2) + "\n", encoding="utf-8")
            feedback_path = workspace / "feedback.json"
            feedback_path.write_text(json.dumps(ORIGINAL_FEEDBACK, indent=2) + "\n", encoding="utf-8")
            original_bytes = feedback_path.read_bytes()

            aggregate = subprocess.run(
                [os.environ.get("PYTHON", "python3"), str(AGGREGATE), str(workspace), "--skill-name", "demo"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(aggregate.returncode, 0, aggregate.stderr + aggregate.stdout)
            benchmark = json.loads((workspace / "benchmark.json").read_text(encoding="utf-8"))
            expectations = benchmark["runs"][0]["expectations"]
            self.assertEqual([exp["evidence"] for exp in expectations], [MOUSEOVER_EVIDENCE, CLICK_EVIDENCE])

            port = _free_port()
            server = subprocess.Popen(
                [
                    os.environ.get("PYTHON", "python3"),
                    "-u",
                    str(GENERATE_REVIEW),
                    str(workspace),
                    "--skill-name",
                    "demo",
                    "--benchmark",
                    str(workspace / "benchmark.json"),
                    "--port",
                    str(port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "BROWSER": "true"},
                start_new_session=True,
            )
            try:
                _wait_http(f"http://127.0.0.1:{port}/", server)
                result = _hover_benchmark(chrome, node, f"http://127.0.0.1:{port}/", Path(tmp))
            finally:
                _stop_process_group(server)

            self.assertEqual(feedback_path.read_bytes(), original_bytes)
            self.assertFalse(result["fired"], result)
            self.assertFalse(result["handlerAttributes"], result)
            self.assertFalse(result["injectedElements"], result)
            self.assertEqual(
                result["titles"],
                [f"Run 1: {MOUSEOVER_EVIDENCE}", f"Run 1: {CLICK_EVIDENCE}"],
                result,
            )
            self.assertEqual(result["icons"], ["\u2717", "\u2713"], result)
            # The grades panel renders the same evidence as element text.
            self.assertIn(MOUSEOVER_EVIDENCE, result["gradesText"])
            self.assertFalse(result["gradesHandlerAttributes"], result)
            self.assertEqual(feedback_path.read_bytes(), original_bytes)


def _stop_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def _hover_benchmark(chrome: str, node: str, url: str, tmp: Path) -> dict:
    debug_port = _free_port()
    user_data = tmp / "chrome-profile"
    chrome_proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={user_data}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_http(f"http://127.0.0.1:{debug_port}/json/version", chrome_proc)
        script = tmp / "hover.mjs"
        script.write_text(_CDP_SCRIPT, encoding="utf-8")
        completed = subprocess.run(
            [node, str(script), str(debug_port), url],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout + completed.stderr)
        return json.loads(completed.stdout)
    finally:
        _stop_process_group(chrome_proc)


_CDP_SCRIPT = textwrap.dedent(
    r"""
    const debugPort = process.argv[2];
    const pageUrl = process.argv[3];
    const version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
    const browser = new WebSocket(version.webSocketDebuggerUrl);
    let nextId = 0;
    const pending = new Map();
    function send(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, {resolve, reject});
        browser.send(JSON.stringify({id, method, params}));
      });
    }
    browser.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
      else waiter.resolve(message.result);
    });
    await new Promise((resolve) => browser.addEventListener("open", resolve));
    const created = await send("Target.createTarget", {url: pageUrl});
    browser.close();

    const deadline = Date.now() + 10000;
    let page;
    while (Date.now() < deadline) {
      const list = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
      page = list.find((target) => target.id === created.targetId && target.webSocketDebuggerUrl);
      if (page) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!page) throw new Error("review page target missing");

    const pageSocket = new WebSocket(page.webSocketDebuggerUrl);
    nextId = 0;
    pending.clear();
    function sendPage(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, {resolve, reject});
        pageSocket.send(JSON.stringify({id, method, params}));
      });
    }
    pageSocket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
      else waiter.resolve(message.result);
    });
    await new Promise((resolve) => pageSocket.addEventListener("open", resolve));
    await sendPage("Runtime.enable");

    const expression = `(() => {
      const deadline = Date.now() + 10000;
      function ready() {
        const grades = document.getElementById("grades-content");
        return document.querySelectorAll("#benchmark-content span").length >= 2
          && grades && grades.textContent.includes("Passage 1");
      }
      return new Promise((resolve) => {
        const tick = () => {
          if (ready() || Date.now() > deadline) resolve(true);
          else setTimeout(tick, 50);
        };
        tick();
      });
    })()`;
    await sendPage("Runtime.evaluate", {expression, awaitPromise: true});
    const probed = await sendPage("Runtime.evaluate", {
      expression: `(() => {
        switchView("benchmark");
        const root = document.getElementById("benchmark-content");
        const spans = [...root.querySelectorAll("span")];
        for (const span of spans) {
          span.dispatchEvent(new MouseEvent("mouseover", {bubbles: true, cancelable: true, view: window}));
          span.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
        }
        const handlerAttributes = [...root.querySelectorAll("*")].flatMap((el) =>
          [...el.attributes].filter((attr) => attr.name.toLowerCase().startsWith("on")).map((attr) => attr.name)
        );
        const grades = document.getElementById("grades-content");
        const gradesHandlerAttributes = [...grades.querySelectorAll("*")].flatMap((el) =>
          [...el.attributes].filter((attr) => attr.name.toLowerCase().startsWith("on")).map((attr) => attr.name)
        );
        return {
          fired: Boolean(window.__tooltipFired),
          titles: spans.map((span) => span.getAttribute("title")),
          icons: spans.map((span) => span.textContent),
          handlerAttributes,
          injectedElements: [...root.querySelectorAll("img, svg, script, iframe, object")].map((el) => el.outerHTML),
          gradesText: grades.textContent,
          gradesHandlerAttributes,
        };
      })()`,
      returnByValue: true,
    });
    if (probed.exceptionDetails) throw new Error(JSON.stringify(probed.exceptionDetails));
    // Give an in-flight feedback POST time to hit disk.
    await new Promise((resolve) => setTimeout(resolve, 500));
    const firedLater = await sendPage("Runtime.evaluate", {
      expression: "Boolean(window.__tooltipFired)",
      returnByValue: true,
    });
    const value = probed.result.value;
    value.fired = Boolean(value.fired || firedLater.result.value);
    process.stdout.write(JSON.stringify(value));
    pageSocket.close();
    """
)


if __name__ == "__main__":
    unittest.main()
