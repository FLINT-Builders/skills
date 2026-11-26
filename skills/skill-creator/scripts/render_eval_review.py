#!/usr/bin/env python3
"""Render the trigger eval set review page from assets/eval_review.html.

The skill name, description, and queries come from the skill being reviewed,
so they are treated as untrusted text: the name and description are
HTML-encoded, and the eval items are embedded as JSON with every character
that could end the inline <script> escaped.

Usage:
    python -m scripts.render_eval_review --eval-set <eval.json> --skill-path <skill> --output <out.html>
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "eval_review.html"

_PLACEHOLDER_RE = re.compile(
    r"__(SKILL_NAME|SKILL_DESCRIPTION|EVAL_DATA)_PLACEHOLDER__"
)


def json_for_script(value: object) -> str:
    """Serialize value as JSON that is safe to place inside an HTML <script>."""
    return (
        json.dumps(value)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _normalize_eval_items(eval_items: list) -> list[dict]:
    if not isinstance(eval_items, list):
        raise ValueError("Eval set must be a JSON array")
    items: list[dict] = []
    for i, item in enumerate(eval_items):
        if not isinstance(item, dict):
            raise ValueError(f"Eval item {i} must be an object")
        query = item.get("query")
        if not isinstance(query, str):
            raise ValueError(f"Eval item {i} must have a string 'query'")
        items.append({"query": query, "should_trigger": bool(item.get("should_trigger"))})
    return items


def render_eval_review_html(skill_name: str, description: str, eval_items: list) -> str:
    """Return the eval review page with the given values safely embedded."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "SKILL_NAME": html.escape(str(skill_name), quote=True),
        "SKILL_DESCRIPTION": html.escape(str(description), quote=True),
        "EVAL_DATA": json_for_script(_normalize_eval_items(eval_items)),
    }
    # One pass, so text substituted for one placeholder is never re-scanned
    # for another.
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def main() -> None:
    from scripts.utils import parse_skill_md

    parser = argparse.ArgumentParser(description="Render the trigger eval set review page")
    parser.add_argument("--eval-set", required=True, type=Path, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, type=Path, help="Path to skill directory")
    parser.add_argument("--output", "-o", required=True, type=Path, help="Where to write the HTML page")
    args = parser.parse_args()

    name, description, _ = parse_skill_md(args.skill_path)
    eval_items = json.loads(args.eval_set.read_text(encoding="utf-8"))

    page = render_eval_review_html(name, description, eval_items)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding="utf-8")
    print(f"Eval review written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
