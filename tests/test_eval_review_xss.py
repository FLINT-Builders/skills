"""Failing tests for raw HTML injection in skill-creator review pages.

These tests are the spec for the fix. They fail while the vulnerability is
present and are expected to pass once review pages stop treating skill text
as markup.

What is vulnerable today
------------------------
``skills/skill-creator/SKILL.md`` tells the agent to copy
``assets/eval_review.html`` and replace ``__SKILL_NAME_PLACEHOLDER__``,
``__SKILL_DESCRIPTION_PLACEHOLDER__``, and ``__EVAL_DATA_PLACEHOLDER__`` with
the skill name, description, and eval JSON. The template places the name and
description in the document body and assigns the JSON inside a ``<script>``
block. ``quick_validate.py`` rejects angle brackets, but the review steps
never call it; only packaging does.

``eval-viewer/generate_review.py`` has the same script-breakout sink.
``generate_html`` embeds the skill name, prompts, output files, grading, and
previous feedback with ``json.dumps`` inside ``viewer.html``'s script tag.
``json.dumps`` does not escape ``<``, so a value containing ``</script>``
closes the script element.

Required fix
------------
1. Add ``skills/skill-creator/scripts/render_eval_review.py`` with
   ``render_eval_review_html(skill_name: str, description: str, eval_items: list) -> str``.
   HTML-encode the name and description. Embed the eval JSON so angle
   brackets cannot close the script (U+003C as ``\\u003c``, and the same for
   U+003E and U+0026).
2. Stop telling the agent to paste those placeholders. ``SKILL.md`` must not
   contain ``__SKILL_NAME_PLACEHOLDER__``, ``__SKILL_DESCRIPTION_PLACEHOLDER__``,
   or ``__EVAL_DATA_PLACEHOLDER__``, and must tell the agent to run
   ``scripts/render_eval_review.py``.
3. Escape those same characters in the JSON ``generate_review.generate_html``
   injects into ``viewer.html``.

Run from the repository root (Google Chrome must be on ``PATH``):

    python3 -m unittest discover -s tests -p 'test_eval_review_xss.py' -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skills" / "skill-creator"
SKILL_MD = SKILL_DIR / "SKILL.md"
TEMPLATE = SKILL_DIR / "assets" / "eval_review.html"
RENDERER = SKILL_DIR / "scripts" / "render_eval_review.py"
VIEWER_DIR = SKILL_DIR / "eval-viewer"

RAW_PLACEHOLDERS = (
    "__SKILL_NAME_PLACEHOLDER__",
    "__SKILL_DESCRIPTION_PLACEHOLDER__",
    "__EVAL_DATA_PLACEHOLDER__",
)

# Attribute written by a payload that managed to run. The payload source uses
# a single-quoted argument, so this double-quoted form appears only after the
# browser has executed it.
EXECUTED = 'data-pwned="1"'
MARKER_JS = "document.documentElement.setAttribute('data-pwned','1')"


def _instructions_paste_raw(skill_text: str) -> bool:
    return any(token in skill_text for token in RAW_PLACEHOLDERS)


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_eval_review", RENDERER)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot import {RENDERER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_template_page(skill_name: str, description: str, eval_items: list) -> str:
    """Do the substitution SKILL.md currently tells the agent to do."""
    template = TEMPLATE.read_text(encoding="utf-8")
    # The eval JSON is a JS assignment, so it is inserted without quotes.
    page = template.replace("__EVAL_DATA_PLACEHOLDER__", json.dumps(eval_items))
    page = page.replace("__SKILL_NAME_PLACEHOLDER__", skill_name)
    page = page.replace("__SKILL_DESCRIPTION_PLACEHOLDER__", description)
    return page


def build_eval_review_page(skill_name: str, description: str, eval_items: list) -> str:
    """Build the page the skill-creator review step tells the agent to open.

    While SKILL.md still names the raw placeholders, this follows that
    procedure against ``assets/eval_review.html``. Once those instructions are
    gone, the page must come from ``render_eval_review_html``.
    """
    skill_text = SKILL_MD.read_text(encoding="utf-8")
    if _instructions_paste_raw(skill_text):
        return _raw_template_page(skill_name, description, eval_items)
    if not RENDERER.is_file():
        raise AssertionError(
            "SKILL.md no longer pastes the review placeholders, but "
            f"{RENDERER.relative_to(REPO)} does not exist"
        )
    module = _load_renderer()
    render = getattr(module, "render_eval_review_html", None)
    if render is None:
        raise AssertionError("render_eval_review.py must define render_eval_review_html")
    return render(skill_name, description, eval_items)


def _load_generate_review():
    path = VIEWER_DIR / "generate_review.py"
    spec = importlib.util.spec_from_file_location("generate_review", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def open_dom(html: str) -> str:
    """Open a generated review file in Chrome and return the serialized DOM.

    Chrome's headless dump finishes and then does not exit in this environment,
    so the process group is killed once the DOM is on stdout. The file URL is
    the same origin the skill uses when it tells the agent to ``open`` the
    review file.
    """
    tmp = Path(tempfile.mkdtemp(prefix="eval-review-xss-"))
    try:
        page = tmp / "review.html"
        page.write_text(html, encoding="utf-8")
        profile = tmp / "profile"
        cmd = [
            "google-chrome",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=2000",
            "--dump-dom",
            page.as_uri(),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
        dom = out.decode("utf-8", errors="replace")
        if "</html>" not in dom.lower():
            raise AssertionError(
                "Chrome did not return a document.\n"
                + err.decode("utf-8", errors="replace")[-1000:]
            )
        return dom
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _assert_inert(dom: str, token: str) -> None:
    if EXECUTED in dom:
        raise AssertionError(
            "Skill-controlled text ran as script. The DOM contains "
            f"{EXECUTED!r}, set by the payload around {token!r}."
        )
    if token not in dom:
        raise AssertionError(
            f"Review page dropped the skill text {token!r}. "
            "Encode it; do not strip it."
        )


class EvalSetReviewTests(unittest.TestCase):
    """Review page built from the skill name, description, and eval queries."""

    def test_skill_instructions_do_not_paste_raw_placeholders(self) -> None:
        text = SKILL_MD.read_text(encoding="utf-8")
        for token in RAW_PLACEHOLDERS:
            if token in text:
                self.fail(
                    f"SKILL.md still tells the agent to paste {token} into the HTML template"
                )
        if "render_eval_review.py" not in text:
            self.fail(
                "SKILL.md must tell the agent to generate the review page with "
                "scripts/render_eval_review.py"
            )

    def test_skill_description_does_not_execute_in_review_page(self) -> None:
        description = (
            f"<img src=x onerror={MARKER_JS}>XSSMARKER_DESC"
        )
        page = build_eval_review_page(
            "ordinary-skill",
            description,
            [{"query": "benign query", "should_trigger": True}],
        )
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_DESC")
        self.assertIn("ordinary-skill", dom)
        self.assertIn("Export Eval Set", dom)

    def test_skill_name_does_not_execute_in_review_page(self) -> None:
        # The name is written into <title> and into the body. Closing the
        # title is enough to run markup from the head; a script element in
        # the heading runs from the body as well.
        skill_name = f"</title><script>{MARKER_JS}</script>XSSMARKER_NAME"
        page = build_eval_review_page(
            skill_name,
            "ordinary description",
            [{"query": "benign query", "should_trigger": True}],
        )
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_NAME")
        self.assertIn("ordinary description", dom)
        self.assertIn("Export Eval Set", dom)

    def test_eval_query_cannot_break_out_of_review_script(self) -> None:
        query = f"</script><script>{MARKER_JS}</script>XSSMARKER_QUERY"
        page = build_eval_review_page(
            "ordinary-skill",
            "ordinary description",
            [{"query": query, "should_trigger": False}],
        )
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_QUERY")
        self.assertIn("ordinary-skill", dom)
        self.assertIn("Export Eval Set", dom)
        # The query has to survive as the textarea's text. A page that stays
        # inert by never rendering the eval rows does not count.
        self.assertRegex(dom, r"<textarea[^>]*>[\s\S]*XSSMARKER_QUERY[\s\S]*</textarea>")


class EvalViewerEmbedTests(unittest.TestCase):
    """generate_review.py embeds workspace text into viewer.html's script."""

    def _viewer_html(
        self,
        *,
        skill_name: str = "safe-skill",
        prompt: str = "benign prompt",
        output: str = "benign output",
        grading_evidence: str | None = None,
        feedback: str | None = None,
    ) -> str:
        gr = _load_generate_review()
        tmp = Path(tempfile.mkdtemp(prefix="eval-viewer-xss-"))
        try:
            workspace = tmp / "workspace"
            run = workspace / "case-1"
            outputs = run / "outputs"
            outputs.mkdir(parents=True)
            (outputs / "note.txt").write_text(output, encoding="utf-8")
            (run / "eval_metadata.json").write_text(
                json.dumps({"prompt": prompt, "eval_id": 1}),
                encoding="utf-8",
            )
            if grading_evidence is not None:
                (run / "grading.json").write_text(
                    json.dumps(
                        {
                            "expectations": [
                                {
                                    "text": "check",
                                    "passed": True,
                                    "evidence": grading_evidence,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
            previous = None
            if feedback is not None:
                prev = tmp / "previous"
                prev_run = prev / "case-1"
                (prev_run / "outputs").mkdir(parents=True)
                (prev_run / "outputs" / "note.txt").write_text(
                    "previous benign output", encoding="utf-8"
                )
                (prev_run / "eval_metadata.json").write_text(
                    json.dumps({"prompt": "previous benign prompt", "eval_id": 1}),
                    encoding="utf-8",
                )
                (prev / "feedback.json").write_text(
                    json.dumps(
                        {"reviews": [{"run_id": "case-1", "feedback": feedback}]}
                    ),
                    encoding="utf-8",
                )
                previous = gr.load_previous_iteration(prev)
            runs = gr.find_runs(workspace)
            self.assertTrue(runs, "workspace produced no runs for the viewer")
            return gr.generate_html(runs, skill_name, previous)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_output_file_cannot_break_out_of_viewer_script(self) -> None:
        payload = f"</script><script>{MARKER_JS}</script>XSSMARKER_OUTPUT"
        page = self._viewer_html(output=payload)
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_OUTPUT")
        self.assertIn("safe-skill", dom)

    def test_skill_name_cannot_break_out_of_viewer_script(self) -> None:
        skill_name = f"</script><script>{MARKER_JS}</script>XSSMARKER_VIEWER_NAME"
        page = self._viewer_html(skill_name=skill_name)
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_VIEWER_NAME")

    def test_prompt_cannot_break_out_of_viewer_script(self) -> None:
        prompt = f"</script><script>{MARKER_JS}</script>XSSMARKER_PROMPT"
        page = self._viewer_html(prompt=prompt)
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_PROMPT")
        self.assertIn("safe-skill", dom)

    def test_grading_evidence_cannot_break_out_of_viewer_script(self) -> None:
        evidence = f"</script><script>{MARKER_JS}</script>XSSMARKER_GRADE"
        page = self._viewer_html(grading_evidence=evidence)
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_GRADE")
        self.assertIn("safe-skill", dom)

    def test_previous_feedback_cannot_break_out_of_viewer_script(self) -> None:
        feedback = f"</script><script>{MARKER_JS}</script>XSSMARKER_FEEDBACK"
        page = self._viewer_html(feedback=feedback)
        dom = open_dom(page)
        _assert_inert(dom, "XSSMARKER_FEEDBACK")
        self.assertIn("safe-skill", dom)


if __name__ == "__main__":
    unittest.main()
