"""Shared utilities for skill-creator scripts."""

import re
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64
_SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def validate_skill_name(name: str) -> str:
    """Return name if it is a kebab-case skill name, else raise ValueError.

    The name is used to build a filename, so it must be a single path segment
    made of lowercase letters, digits, and single hyphens.
    """
    if (
        not isinstance(name, str)
        or len(name) > MAX_SKILL_NAME_LENGTH
        or not _SKILL_NAME_RE.fullmatch(name)
    ):
        raise ValueError(
            f"Invalid skill name {name!r}: must be kebab-case (lowercase letters, "
            f"digits, and single hyphens), at most {MAX_SKILL_NAME_LENGTH} characters"
        )
    return name


def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """Parse a SKILL.md file, returning (name, description, full_content)."""
    content = (skill_path / "SKILL.md").read_text()
    lines = content.split("\n")

    if lines[0].strip() != "---":
        raise ValueError("SKILL.md missing frontmatter (no opening ---)")

    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("SKILL.md missing frontmatter (no closing ---)")

    name = ""
    description = ""
    frontmatter_lines = lines[1:end_idx]
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            value = line[len("description:"):].strip()
            # Handle YAML multiline indicators (>, |, >-, |-)
            if value in (">", "|", ">-", "|-"):
                continuation_lines: list[str] = []
                i += 1
                while i < len(frontmatter_lines) and (frontmatter_lines[i].startswith("  ") or frontmatter_lines[i].startswith("\t")):
                    continuation_lines.append(frontmatter_lines[i].strip())
                    i += 1
                description = " ".join(continuation_lines)
                continue
            else:
                description = value.strip('"').strip("'")
        i += 1

    return name, description, content
