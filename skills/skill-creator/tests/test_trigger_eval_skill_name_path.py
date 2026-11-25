"""Command-file writes during skill-creator trigger evaluation must stay
inside the project ``.claude/commands`` directory.

A skill author supplies ``name`` and ``description`` in ``SKILL.md``.
``scripts.run_loop`` and ``scripts.run_eval`` pass that name through
``parse_skill_md`` into ``run_single_query``, which joins it onto the
project commands directory and writes the description there before
starting ``claude``. These tests fail while that join can land outside
the commands directory. They pass when a query leaves no file outside
it — including a file that is created and then removed before ``claude``
returns, and including a destination whose lexical path sits inside the
commands directory but whose open follows a symlink out of it.

Run from the repository root:

    python skills/skill-creator/tests/test_trigger_eval_skill_name_path.py -v
"""

from __future__ import annotations

import json
import os
import select
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SKILL_CREATOR = Path(__file__).resolve().parents[1]
if str(SKILL_CREATOR) not in sys.path:
    sys.path.insert(0, str(SKILL_CREATOR))

from scripts.run_eval import find_project_root, run_single_query
from scripts.utils import parse_skill_md

QUERY = "attacker-supplied-trigger-query"
MARKER = "attacker-supplied-skill-description"
# linux/inotify.h — events that mean a file appeared in the watched directory.
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_Q_OVERFLOW = 0x00004000


class _CreateWatch:
    """Record filenames created in one directory while a call runs.

    Uses inotify (linux/inotify.h) so a file that is written and unlinked
    before this process can scan the directory is still observed.
    """

    def __init__(self, directory: Path):
        import ctypes

        self.directory = directory
        self.names: list[str] = []
        self.overflow = False
        self._stop = False
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._fd = self._libc.inotify_init()
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), "inotify_init")
        os.set_blocking(self._fd, False)
        mask = _IN_CREATE | _IN_CLOSE_WRITE | _IN_MOVED_TO
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(directory), mask)
        if wd < 0:
            err = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(err, os.strerror(err), str(directory))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            ready, _, _ = select.select([self._fd], [], [], 0.1)
            if not ready:
                continue
            self._drain()

    def _drain(self) -> None:
        try:
            data = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        offset = 0
        while offset + 16 <= len(data):
            _wd, mask, _cookie, name_len = struct.unpack_from("iIII", data, offset)
            offset += 16
            raw = data[offset : offset + name_len]
            offset += name_len
            if mask & _IN_Q_OVERFLOW:
                self.overflow = True
            name = raw.split(b"\x00", 1)[0]
            if name:
                self.names.append(os.fsdecode(name))

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._drain()
        os.close(self._fd)


class TriggerEvalSkillNamePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.commands = self.project / ".claude" / "commands"
        (self.project / ".claude").mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.report = self.root / "shim-report.json"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._write_shim()
        self.eval_path = self.root / "eval_set.json"
        self.eval_path.write_text(
            json.dumps([{"query": QUERY, "should_trigger": False}])
        )
        self._saved_path = os.environ.get("PATH")
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + self._saved_path
        self.addCleanup(self._restore_path)

    def _restore_path(self) -> None:
        if self._saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._saved_path

    def _write_shim(self) -> None:
        shim = self.bin_dir / "claude"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "report = Path(os.environ['TRIGGER_EVAL_SHIM_REPORT'])\n"
            "outside = Path(os.environ['TRIGGER_EVAL_OUTSIDE_DIR'])\n"
            "commands = Path(os.environ['TRIGGER_EVAL_COMMANDS_DIR'])\n"
            "\n"
            "def listing(root):\n"
            "    found = []\n"
            "    if not root.is_dir():\n"
            "        return found\n"
            "    for dirpath, dirnames, filenames in os.walk(root):\n"
            "        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]\n"
            "        for filename in filenames:\n"
            "            path = Path(dirpath) / filename\n"
            "            if path.is_symlink():\n"
            "                continue\n"
            "            try:\n"
            "                content = path.read_text(encoding='utf-8', errors='replace')\n"
            "            except OSError as exc:\n"
            "                content = '<unreadable %s>' % exc\n"
            "            found.append({'path': str(path), 'resolved': str(path.resolve()), 'content': content})\n"
            "    return found\n"
            "\n"
            "entries = []\n"
            "if report.exists():\n"
            "    entries = json.loads(report.read_text())\n"
            "entries.append({\n"
            "    'argv': sys.argv,\n"
            "    'cwd': os.getcwd(),\n"
            "    'commands_files': listing(commands),\n"
            "    'outside_files': listing(outside),\n"
            "})\n"
            "report.write_text(json.dumps(entries))\n"
            "sys.exit(0)\n"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def _write_skill(self, frontmatter_name: str) -> Path:
        skill = self.project / "supplied-skill"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\ndescription: {MARKER}\n---\n\n# Supplied skill\n"
        )
        return skill

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = str(SKILL_CREATOR) + os.pathsep + env.get("PYTHONPATH", "")
        env["TRIGGER_EVAL_SHIM_REPORT"] = str(self.report)
        env["TRIGGER_EVAL_COMMANDS_DIR"] = str(self.commands)
        env["TRIGGER_EVAL_OUTSIDE_DIR"] = str(self.outside)
        env.pop("CLAUDECODE", None)
        return env

    def _escaped(self, path: Path) -> bool:
        """True when opening this path does not stay inside the commands directory.

        ``resolve`` follows symlinks and parent-directory segments, which is
        the path the subsequent open uses. A lexical prefix check on the
        unresolved string is not enough for this to return false.
        """
        commands_root = self.commands.resolve()
        try:
            resolved = path.resolve()
        except OSError:
            return True
        return not resolved.is_relative_to(commands_root)

    def _assert_no_outside_writes(self, writes: list[tuple[Path, str]]) -> None:
        leaked = [
            {"path": str(path), "resolved": str(path.resolve()), "content": data}
            for path, data in writes
            if self._escaped(path)
        ]
        leftovers = [str(path) for path in self.outside.rglob("*") if path.is_file()]
        self.assertEqual(
            leaked,
            [],
            "trigger evaluation wrote a command file outside the project "
            f"commands directory: {leaked}",
        )
        self.assertEqual(
            leftovers,
            [],
            f"a file written outside the commands directory was left behind: {leftovers}",
        )

    def _run_single_query_from_skill(self, frontmatter_name: str) -> list[tuple[Path, str]]:
        skill = self._write_skill(frontmatter_name)
        try:
            name, description, _content = parse_skill_md(skill)
        except (Exception, SystemExit):
            # Refusing the frontmatter before any command file is built is safe.
            return []
        self.assertEqual(description, MARKER)
        writes: list[tuple[Path, str]] = []
        original = Path.write_text

        def spy(path_self, data, encoding=None, errors=None, newline=None):
            writes.append((Path(path_self), data))
            return original(
                path_self,
                data,
                encoding=encoding,
                errors=errors,
                newline=newline,
            )

        previous = Path.cwd()
        os.chdir(self.project)
        Path.write_text = spy
        try:
            project_root = find_project_root()
            self.assertEqual(project_root, self.project)
            try:
                # A refusal before the write is success. A write that already
                # escaped is recorded above and still fails the assertion.
                run_single_query(QUERY, name, description, 10, str(project_root))
            except (Exception, SystemExit):
                pass
        finally:
            Path.write_text = original
            os.chdir(previous)
        return writes

    def _run_cli(self, module: str, extra: list[str]) -> subprocess.CompletedProcess[str]:
        watch = _CreateWatch(self.outside)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", module, *extra],
                cwd=self.project,
                env=self._env(),
                text=True,
                capture_output=True,
                timeout=60,
            )
        finally:
            watch.close()
        self._cli_watch = watch
        return completed

    def _assert_cli_contained(self, completed: subprocess.CompletedProcess[str]) -> None:
        output = (completed.stdout or "") + (completed.stderr or "")
        self.assertNotIn(
            "No module named",
            output,
            f"evaluator did not start:\n{output}",
        )
        self.assertFalse(
            self._cli_watch.overflow,
            "inotify queue overflowed; the outside-directory watch is incomplete",
        )
        created = list(self._cli_watch.names)
        report_outside: list[dict] = []
        if self.report.exists():
            for entry in json.loads(self.report.read_text()):
                report_outside.extend(entry["outside_files"])
        leftovers = [str(path) for path in self.outside.rglob("*") if path.is_file()]
        self.assertEqual(
            created,
            [],
            "trigger evaluation created a file in a directory outside the "
            f"project commands directory while handling {QUERY!r}: {created}; "
            f"contents seen by claude: {report_outside}\n{output}",
        )
        self.assertEqual(
            report_outside,
            [],
            "claude was started while a skill command file existed outside "
            f"the project commands directory: {report_outside}",
        )
        self.assertEqual(leftovers, [], f"escaped file left on disk: {leftovers}")

    def _loop_args(self, skill: Path) -> list[str]:
        return [
            "--eval-set",
            str(self.eval_path),
            "--skill-path",
            str(skill),
            "--model",
            "test-model",
            "--max-iterations",
            "1",
            "--runs-per-query",
            "1",
            "--num-workers",
            "1",
            "--timeout",
            "10",
            "--holdout",
            "0",
            "--report",
            "none",
        ]

    def _eval_args(self, skill: Path) -> list[str]:
        return [
            "--eval-set",
            str(self.eval_path),
            "--skill-path",
            str(skill),
            "--runs-per-query",
            "1",
            "--num-workers",
            "1",
            "--timeout",
            "10",
        ]

    def test_run_single_query_rejects_absolute_frontmatter_name(self) -> None:
        name = str(self.outside / "payload")
        self.assertTrue(Path(name).is_absolute())
        writes = self._run_single_query_from_skill(name)
        self._assert_no_outside_writes(writes)

    def test_run_single_query_rejects_quoted_absolute_frontmatter_name(self) -> None:
        absolute = str(self.outside / "quoted-payload")
        writes = self._run_single_query_from_skill(f'"{absolute}"')
        self._assert_no_outside_writes(writes)

    def test_run_single_query_rejects_single_quoted_absolute_frontmatter_name(self) -> None:
        absolute = str(self.outside / "single-quoted-payload")
        writes = self._run_single_query_from_skill(f"'{absolute}'")
        self._assert_no_outside_writes(writes)

    def test_run_single_query_rejects_parent_directory_frontmatter_name(self) -> None:
        name = os.path.relpath(self.outside / "parent-payload", self.commands)
        self.assertTrue(name.split(os.sep)[0] == "..")
        self.assertFalse(Path(name).is_absolute())
        writes = self._run_single_query_from_skill(name)
        self._assert_no_outside_writes(writes)

    def test_run_single_query_rejects_embedded_parent_directory_segment(self) -> None:
        # A leading-only check misses '..' after a directory that already exists.
        self.commands.mkdir(parents=True, exist_ok=True)
        (self.commands / "real").mkdir()
        relative = os.path.relpath(self.outside / "embedded-payload", self.commands / "real")
        name = "real" + os.sep + relative
        self.assertIn("..", name.split(os.sep))
        self.assertFalse(name.startswith(".."))
        writes = self._run_single_query_from_skill(name)
        self._assert_no_outside_writes(writes)

    def test_run_single_query_rejects_symlink_component_under_commands(self) -> None:
        # The lexical path is `<commands>/linked-dir/<file>.md`. The directory
        # entry is a symlink, so the open lands outside the commands directory.
        self.commands.mkdir(parents=True, exist_ok=True)
        (self.commands / "linked-dir").symlink_to(self.outside, target_is_directory=True)
        writes = self._run_single_query_from_skill("linked-dir/payload")
        self._assert_no_outside_writes(writes)

    def test_optimization_loop_absolute_name_does_not_escape(self) -> None:
        name = str(self.outside / "loop-payload")
        skill = self._write_skill(name)
        completed = self._run_cli("scripts.run_loop", self._loop_args(skill))
        self._assert_cli_contained(completed)

    def test_optimization_loop_parent_directory_name_does_not_escape(self) -> None:
        name = os.path.relpath(self.outside / "loop-parent-payload", self.commands)
        self.assertIn("..", name.split(os.sep))
        skill = self._write_skill(name)
        completed = self._run_cli("scripts.run_loop", self._loop_args(skill))
        self._assert_cli_contained(completed)

    def test_run_eval_quoted_absolute_name_does_not_escape(self) -> None:
        absolute = str(self.outside / "eval-quoted-payload")
        skill = self._write_skill(f'"{absolute}"')
        completed = self._run_cli("scripts.run_eval", self._eval_args(skill))
        self._assert_cli_contained(completed)

    def test_run_eval_symlink_component_does_not_escape(self) -> None:
        self.commands.mkdir(parents=True, exist_ok=True)
        (self.commands / "linked-dir").symlink_to(self.outside, target_is_directory=True)
        skill = self._write_skill("linked-dir/eval-payload")
        completed = self._run_cli("scripts.run_eval", self._eval_args(skill))
        self._assert_cli_contained(completed)

    def test_kebab_case_name_is_written_only_inside_commands(self) -> None:
        skill = self._write_skill("supplied-skill")
        completed = self._run_cli("scripts.run_loop", self._loop_args(skill))
        self._assert_cli_contained(completed)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(QUERY, completed.stdout)
        self.assertTrue(self.report.exists(), "claude was not started for a safe skill name")
        entries = json.loads(self.report.read_text())
        command_files = [item for entry in entries for item in entry["commands_files"]]
        self.assertTrue(
            command_files,
            "a kebab-case skill name did not produce a command file inside "
            "the project commands directory",
        )
        commands_root = self.commands.resolve()
        for item in command_files:
            resolved = Path(item["resolved"])
            self.assertTrue(
                resolved.is_relative_to(commands_root),
                f"command file resolved outside the commands directory: {item}",
            )
            self.assertIn(MARKER, item["content"])


if __name__ == "__main__":
    unittest.main()
