"""Resource bounds for SKILL.md YAML frontmatter parsing.

These tests fail while skill validation expands YAML aliases without a cap.
They pass once a supplied skill folder cannot force the validator or the
packager to allocate a large object graph from a small frontmatter.

`scripts/utils.py` `parse_skill_md` is not covered. It never loads YAML; it
only scans lines for name and description, so it does not share this bug.
"""

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_CREATOR = REPO_ROOT / "skills" / "skill-creator"
# Wider than a fixed parser of this payload, far below the expansion measured
# for the bomb below (about 18MB and 2.5s at seven merge levels).
PEAK_BYTES = 2_000_000
TIME_SECONDS = 1.0


def legitimate_skill_md() -> str:
    """Frontmatter a real skill is allowed to contain, larger than the bomb."""
    description = (
        "Handles ordinary skill packaging checks for documents that only use "
        "plain strings and a short metadata map. "
        + ("detail " * 80)
    ).strip()
    if len(description) > 1024:
        raise RuntimeError("control description exceeds the validator limit")
    return (
        "---\n"
        "name: ordinary-skill\n"
        f"description: {description}\n"
        "metadata:\n"
        "  author: example\n"
        "---\n"
        "\n"
        "# Ordinary\n"
    )


def alias_expansion_bomb_md() -> str:
    """Small frontmatter whose merge aliases expand by 10x at each level.

    Seven levels of ten aliases copy about a million mapping entries inside
    PyYAML's safe loader. Name and description stay within the validator's
    own limits so rejection has to come from bounding the load, not from the
    name or description checks.
    """
    lines = [
        "---",
        "name: compact-skill",
        "description: short",
        "metadata:",
        "  a: &a {k: v}",
    ]
    previous = "a"
    for letter in "bcdefg":
        refs = ",".join([f"*{previous}"] * 10)
        lines.append(f"  {letter}: &{letter} {{<<: [{refs}]}}")
        previous = letter
    lines.extend(["---", "", "# Compact", ""])
    return "\n".join(lines)


HARNESS = textwrap.dedent(
    """\
    import json
    import sys
    import time
    import tracemalloc
    from pathlib import Path

    skill_creator, mode, skill_dir, output_dir, result_path = sys.argv[1:6]
    sys.path.insert(0, skill_creator)
    sys.argv = ["harness", skill_dir] if mode != "package-cli" else [
        "harness", skill_dir, output_dir
    ]

    tracemalloc.start()
    started = time.perf_counter()
    exit_code = 0
    ok = None
    message = ""
    try:
        if mode == "validate":
            from scripts.quick_validate import validate_skill
            ok, message = validate_skill(skill_dir)
        elif mode == "package":
            from scripts.package_skill import package_skill
            produced = package_skill(skill_dir, output_dir)
            ok = produced is not None
            message = "" if produced is None else str(produced)
        elif mode == "validate-cli":
            import runpy
            sys.argv = ["quick_validate.py", skill_dir]
            try:
                runpy.run_path(
                    str(Path(skill_creator) / "scripts" / "quick_validate.py"),
                    run_name="__main__",
                )
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
            ok = exit_code == 0
        elif mode == "package-cli":
            import runpy
            sys.argv = ["package_skill.py", skill_dir, output_dir]
            try:
                runpy.run_module("scripts.package_skill", run_name="__main__")
            except SystemExit as exc:
                exit_code = exc.code if isinstance(exc.code, int) else 1
            ok = exit_code == 0
        else:
            raise SystemExit("unknown mode")
    except Exception as exc:
        exit_code = 2
        message = f"{type(exc).__name__}: {exc}"
        ok = None
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    Path(result_path).write_text(json.dumps({
        "ok": ok,
        "message": message,
        "seconds": elapsed,
        "peak": peak,
        "exit_code": exit_code,
    }))
    """
)


class FrontmatterAliasExpansionTests(unittest.TestCase):
    def setUp(self):
        bomb = alias_expansion_bomb_md()
        legit = legitimate_skill_md()
        self.assertLess(
            len(bomb.encode()),
            len(legit.encode()),
            "the bomb must be smaller than a frontmatter the validator accepts",
        )
        self.bomb = bomb
        self.legit = legit

    def _write_skill(self, root: Path, body: str) -> Path:
        skill = root / "sample-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(body)
        return skill

    def _run(self, mode: str, skill: Path, output_dir: Path) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            result_path = Path(raw) / "result.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    HARNESS,
                    str(SKILL_CREATOR),
                    mode,
                    str(skill),
                    str(output_dir),
                    str(result_path),
                ],
                cwd=str(SKILL_CREATOR),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertTrue(
                result_path.exists(),
                "harness produced no result\n"
                f"returncode={completed.returncode}\n"
                f"stdout={completed.stdout}\n"
                f"stderr={completed.stderr}",
            )
            return json.loads(result_path.read_text())

    def _assert_bounded_rejection(self, mode: str):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = self._write_skill(root, self.bomb)
            output_dir = root / "dist"
            result = self._run(mode, skill, output_dir)
            self.assertLess(
                result["seconds"],
                TIME_SECONDS,
                f"{mode} spent {result['seconds']:.3f}s parsing a "
                f"{len(self.bomb)}-byte skill file (peak={result['peak']} bytes, "
                f"ok={result['ok']}, exit={result['exit_code']}, "
                f"message={result['message']!r})",
            )
            self.assertLess(
                result["peak"],
                PEAK_BYTES,
                f"{mode} allocated {result['peak']} bytes from a "
                f"{len(self.bomb)}-byte skill file",
            )
            self.assertIs(result["ok"], False, result)
            self.assertFalse(
                output_dir.exists(),
                f"{mode} created {output_dir} while refusing the skill",
            )
            self.assertEqual(list(root.rglob("*.skill")), [])

    def test_validate_skill_bounds_alias_expansion(self):
        self._assert_bounded_rejection("validate")

    def test_package_skill_bounds_alias_expansion_and_writes_nothing(self):
        self._assert_bounded_rejection("package")

    def test_quick_validate_cli_bounds_alias_expansion(self):
        self._assert_bounded_rejection("validate-cli")

    def test_package_skill_cli_bounds_alias_expansion_and_writes_nothing(self):
        self._assert_bounded_rejection("package-cli")

    def test_larger_legitimate_frontmatter_is_accepted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = self._write_skill(root, self.legit)
            output_dir = root / "dist"
            validated = self._run("validate", skill, output_dir)
            self.assertIs(validated["ok"], True, validated)
            self.assertLess(validated["seconds"], TIME_SECONDS, validated)
            self.assertLess(validated["peak"], PEAK_BYTES, validated)
            packaged = self._run("package", skill, output_dir)
            self.assertIs(packaged["ok"], True, packaged)
            self.assertTrue((output_dir / "sample-skill.skill").is_file(), packaged)


if __name__ == "__main__":
    unittest.main()
