#!/usr/bin/env python3
"""
Aggregate individual run results into benchmark summary statistics.

Reads grading.json files from run directories and produces:
- run_summary with mean, stddev, min, max for each metric
- delta between with_skill and without_skill configurations

Usage:
    python aggregate_benchmark.py <benchmark_dir>

Example:
    python aggregate_benchmark.py benchmarks/2026-01-15T10-30-00/

The script supports two directory layouts:

    Workspace layout (from skill-creator iterations):
    <benchmark_dir>/
    └── eval-N/
        ├── with_skill/
        │   ├── run-1/grading.json
        │   └── run-2/grading.json
        └── without_skill/
            ├── run-1/grading.json
            └── run-2/grading.json

    Legacy layout (with runs/ subdirectory):
    <benchmark_dir>/
    └── runs/
        └── eval-N/
            ├── with_skill/
            │   └── run-1/grading.json
            └── without_skill/
                └── run-1/grading.json
"""

import argparse
import fnmatch
import json
import math
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run directories are untrusted: walk them through directory file descriptors
# and open every entry with O_NOFOLLOW so a symlink (or one swapped in
# mid-scan) cannot pull files from outside the benchmark directory into
# benchmark.json, or redirect its writes.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", None)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _require_fd_safe_io() -> None:
    if (
        _O_NOFOLLOW is None
        or _O_DIRECTORY is None
        or os.open not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
    ):
        raise OSError("aggregate_benchmark.py requires O_NOFOLLOW and dir_fd support (POSIX)")


def _open_dir_path(path: Path) -> int:
    _require_fd_safe_io()
    return os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)


def _open_subdir(dir_fd: int, name: str) -> int | None:
    try:
        return os.open(
            name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError:
        return None


def _subdir_names(dir_fd: int, pattern: str = "*") -> list[str]:
    with os.scandir(dir_fd) as it:
        return sorted(
            e.name for e in it
            if fnmatch.fnmatchcase(e.name, pattern) and e.is_dir(follow_symlinks=False)
        )


def _read_regular_file(dir_fd: int, name: str) -> bytes | None:
    """Read a regular file inside dir_fd; symlinks and special files yield None."""
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        raise
    with open(fd, "rb") as f:
        return f.read()


def _write_path_nofollow(path: Path, data: bytes) -> None:
    """Write path, refusing to follow a symlink or replace a non-regular file."""
    dir_fd = _open_dir_path(path.parent)
    try:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
            0o644,
            dir_fd=dir_fd,
        )
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        os.ftruncate(fd, 0)
    except OSError:
        os.close(fd)
        raise
    with open(fd, "wb") as f:
        f.write(data)


def calculate_stats(values: list[float]) -> dict:
    """Calculate mean, stddev, min, max for a list of values."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}

    n = len(values)
    mean = sum(values) / n

    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4)
    }


def load_run_results(benchmark_dir: Path) -> dict:
    """
    Load all run results from a benchmark directory.

    Returns dict keyed by config name (e.g. "with_skill"/"without_skill",
    or "new_skill"/"old_skill"), each containing a list of run results.
    """
    root_fd = _open_dir_path(benchmark_dir)
    try:
        return _load_run_results(benchmark_dir, root_fd)
    finally:
        os.close(root_fd)


def _load_run_results(benchmark_dir: Path, root_fd: int) -> dict:
    # Support both layouts: eval dirs directly under benchmark_dir, or under runs/
    runs_fd = _open_subdir(root_fd, "runs")
    if runs_fd is not None:
        try:
            return _load_eval_dirs(benchmark_dir / "runs", runs_fd)
        finally:
            os.close(runs_fd)
    if _subdir_names(root_fd, "eval-*"):
        return _load_eval_dirs(benchmark_dir, root_fd)
    print(f"No eval directories found in {benchmark_dir} or {benchmark_dir / 'runs'}")
    return {}


def _load_eval_dirs(search_dir: Path, search_fd: int) -> dict:
    results: dict[str, list] = {}

    for eval_idx, eval_name in enumerate(_subdir_names(search_fd, "eval-*")):
        eval_fd = _open_subdir(search_fd, eval_name)
        if eval_fd is None:
            continue
        try:
            _load_eval_dir(search_dir / eval_name, eval_fd, eval_idx, results)
        finally:
            os.close(eval_fd)

    return results


def _load_eval_dir(eval_dir: Path, eval_fd: int, eval_idx: int, results: dict) -> None:
    try:
        metadata_raw = _read_regular_file(eval_fd, "eval_metadata.json")
    except OSError:
        metadata_raw = b""
    if metadata_raw is not None:
        try:
            eval_id = json.loads(metadata_raw).get("eval_id", eval_idx)
        except ValueError:
            eval_id = eval_idx
    else:
        try:
            eval_id = int(eval_dir.name.split("-")[1])
        except ValueError:
            eval_id = eval_idx

    # Discover config directories dynamically rather than hardcoding names
    for config in _subdir_names(eval_fd):
        config_fd = _open_subdir(eval_fd, config)
        if config_fd is None:
            continue
        try:
            run_names = _subdir_names(config_fd, "run-*")
            # Skip non-config directories (inputs, outputs, etc.)
            if not run_names:
                continue
            if config not in results:
                results[config] = []
            for run_name in run_names:
                run_fd = _open_subdir(config_fd, run_name)
                if run_fd is None:
                    continue
                try:
                    result = _load_run(eval_dir / config / run_name, run_fd, eval_id)
                finally:
                    os.close(run_fd)
                if result is not None:
                    results[config].append(result)
        finally:
            os.close(config_fd)


def _load_run(run_dir: Path, run_fd: int, eval_id) -> dict | None:
    run_number = int(run_dir.name.split("-")[1])
    grading_file = run_dir / "grading.json"

    grading_raw = _read_regular_file(run_fd, "grading.json")
    if grading_raw is None:
        print(f"Warning: grading.json not found in {run_dir}")
        return None

    try:
        grading = json.loads(grading_raw)
    except ValueError as e:
        print(f"Warning: Invalid JSON in {grading_file}: {e}")
        return None

    # Extract metrics
    result = {
        "eval_id": eval_id,
        "run_number": run_number,
        "pass_rate": grading.get("summary", {}).get("pass_rate", 0.0),
        "passed": grading.get("summary", {}).get("passed", 0),
        "failed": grading.get("summary", {}).get("failed", 0),
        "total": grading.get("summary", {}).get("total", 0),
    }

    # Extract timing — check grading.json first, then sibling timing.json
    timing = grading.get("timing", {})
    result["time_seconds"] = timing.get("total_duration_seconds", 0.0)
    timing_raw = None
    if result["time_seconds"] == 0.0:
        timing_raw = _read_regular_file(run_fd, "timing.json")
    if timing_raw is not None:
        try:
            timing_data = json.loads(timing_raw)
            result["time_seconds"] = timing_data.get("total_duration_seconds", 0.0)
            result["tokens"] = timing_data.get("total_tokens", 0)
        except ValueError:
            pass

    # Extract metrics if available
    metrics = grading.get("execution_metrics", {})
    result["tool_calls"] = metrics.get("total_tool_calls", 0)
    if not result.get("tokens"):
        result["tokens"] = metrics.get("output_chars", 0)
    result["errors"] = metrics.get("errors_encountered", 0)

    # Extract expectations — viewer requires fields: text, passed, evidence
    raw_expectations = grading.get("expectations", [])
    for exp in raw_expectations:
        if "text" not in exp or "passed" not in exp:
            print(f"Warning: expectation in {grading_file} missing required fields (text, passed, evidence): {exp}")
    result["expectations"] = raw_expectations

    # Extract notes from user_notes_summary
    notes_summary = grading.get("user_notes_summary", {})
    notes = []
    notes.extend(notes_summary.get("uncertainties", []))
    notes.extend(notes_summary.get("needs_review", []))
    notes.extend(notes_summary.get("workarounds", []))
    result["notes"] = notes

    return result


def aggregate_results(results: dict) -> dict:
    """
    Aggregate run results into summary statistics.

    Returns run_summary with stats for each configuration and delta.
    """
    run_summary = {}
    configs = list(results.keys())

    for config in configs:
        runs = results.get(config, [])

        if not runs:
            run_summary[config] = {
                "pass_rate": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "time_seconds": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "tokens": {"mean": 0, "stddev": 0, "min": 0, "max": 0}
            }
            continue

        pass_rates = [r["pass_rate"] for r in runs]
        times = [r["time_seconds"] for r in runs]
        tokens = [r.get("tokens", 0) for r in runs]

        run_summary[config] = {
            "pass_rate": calculate_stats(pass_rates),
            "time_seconds": calculate_stats(times),
            "tokens": calculate_stats(tokens)
        }

    # Calculate delta between the first two configs (if two exist)
    if len(configs) >= 2:
        primary = run_summary.get(configs[0], {})
        baseline = run_summary.get(configs[1], {})
    else:
        primary = run_summary.get(configs[0], {}) if configs else {}
        baseline = {}

    delta_pass_rate = primary.get("pass_rate", {}).get("mean", 0) - baseline.get("pass_rate", {}).get("mean", 0)
    delta_time = primary.get("time_seconds", {}).get("mean", 0) - baseline.get("time_seconds", {}).get("mean", 0)
    delta_tokens = primary.get("tokens", {}).get("mean", 0) - baseline.get("tokens", {}).get("mean", 0)

    run_summary["delta"] = {
        "pass_rate": f"{delta_pass_rate:+.2f}",
        "time_seconds": f"{delta_time:+.1f}",
        "tokens": f"{delta_tokens:+.0f}"
    }

    return run_summary


def generate_benchmark(benchmark_dir: Path, skill_name: str = "", skill_path: str = "") -> dict:
    """
    Generate complete benchmark.json from run results.
    """
    results = load_run_results(benchmark_dir)
    run_summary = aggregate_results(results)

    # Build runs array for benchmark.json
    runs = []
    for config in results:
        for result in results[config]:
            runs.append({
                "eval_id": result["eval_id"],
                "configuration": config,
                "run_number": result["run_number"],
                "result": {
                    "pass_rate": result["pass_rate"],
                    "passed": result["passed"],
                    "failed": result["failed"],
                    "total": result["total"],
                    "time_seconds": result["time_seconds"],
                    "tokens": result.get("tokens", 0),
                    "tool_calls": result.get("tool_calls", 0),
                    "errors": result.get("errors", 0)
                },
                "expectations": result["expectations"],
                "notes": result["notes"]
            })

    # Determine eval IDs from results
    eval_ids = sorted(set(
        r["eval_id"]
        for config in results.values()
        for r in config
    ))

    benchmark = {
        "metadata": {
            "skill_name": skill_name or "<skill-name>",
            "skill_path": skill_path or "<path/to/skill>",
            "executor_model": "<model-name>",
            "analyzer_model": "<model-name>",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": eval_ids,
            "runs_per_configuration": 3
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": []  # To be filled by analyzer
    }

    return benchmark


def generate_markdown(benchmark: dict) -> str:
    """Generate human-readable benchmark.md from benchmark data."""
    metadata = benchmark["metadata"]
    run_summary = benchmark["run_summary"]

    # Determine config names (excluding "delta")
    configs = [k for k in run_summary if k != "delta"]
    config_a = configs[0] if len(configs) >= 1 else "config_a"
    config_b = configs[1] if len(configs) >= 2 else "config_b"
    label_a = config_a.replace("_", " ").title()
    label_b = config_b.replace("_", " ").title()

    lines = [
        f"# Skill Benchmark: {metadata['skill_name']}",
        "",
        f"**Model**: {metadata['executor_model']}",
        f"**Date**: {metadata['timestamp']}",
        f"**Evals**: {', '.join(map(str, metadata['evals_run']))} ({metadata['runs_per_configuration']} runs each per configuration)",
        "",
        "## Summary",
        "",
        f"| Metric | {label_a} | {label_b} | Delta |",
        "|--------|------------|---------------|-------|",
    ]

    a_summary = run_summary.get(config_a, {})
    b_summary = run_summary.get(config_b, {})
    delta = run_summary.get("delta", {})

    # Format pass rate
    a_pr = a_summary.get("pass_rate", {})
    b_pr = b_summary.get("pass_rate", {})
    lines.append(f"| Pass Rate | {a_pr.get('mean', 0)*100:.0f}% ± {a_pr.get('stddev', 0)*100:.0f}% | {b_pr.get('mean', 0)*100:.0f}% ± {b_pr.get('stddev', 0)*100:.0f}% | {delta.get('pass_rate', '—')} |")

    # Format time
    a_time = a_summary.get("time_seconds", {})
    b_time = b_summary.get("time_seconds", {})
    lines.append(f"| Time | {a_time.get('mean', 0):.1f}s ± {a_time.get('stddev', 0):.1f}s | {b_time.get('mean', 0):.1f}s ± {b_time.get('stddev', 0):.1f}s | {delta.get('time_seconds', '—')}s |")

    # Format tokens
    a_tokens = a_summary.get("tokens", {})
    b_tokens = b_summary.get("tokens", {})
    lines.append(f"| Tokens | {a_tokens.get('mean', 0):.0f} ± {a_tokens.get('stddev', 0):.0f} | {b_tokens.get('mean', 0):.0f} ± {b_tokens.get('stddev', 0):.0f} | {delta.get('tokens', '—')} |")

    # Notes section
    if benchmark.get("notes"):
        lines.extend([
            "",
            "## Notes",
            ""
        ])
        for note in benchmark["notes"]:
            lines.append(f"- {note}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate benchmark run results into summary statistics"
    )
    parser.add_argument(
        "benchmark_dir",
        type=Path,
        help="Path to the benchmark directory"
    )
    parser.add_argument(
        "--skill-name",
        default="",
        help="Name of the skill being benchmarked"
    )
    parser.add_argument(
        "--skill-path",
        default="",
        help="Path to the skill being benchmarked"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Output path for benchmark.json (default: <benchmark_dir>/benchmark.json)"
    )

    args = parser.parse_args()

    if not args.benchmark_dir.exists():
        print(f"Directory not found: {args.benchmark_dir}")
        sys.exit(1)

    # Generate benchmark
    benchmark = generate_benchmark(args.benchmark_dir, args.skill_name, args.skill_path)

    # Determine output paths
    output_json = args.output or (args.benchmark_dir / "benchmark.json")
    output_md = output_json.with_suffix(".md")

    # Write benchmark.json
    _write_path_nofollow(output_json, json.dumps(benchmark, indent=2).encode("utf-8"))
    print(f"Generated: {output_json}")

    # Write benchmark.md
    markdown = generate_markdown(benchmark)
    _write_path_nofollow(output_md, markdown.encode("utf-8"))
    print(f"Generated: {output_md}")

    # Print summary
    run_summary = benchmark["run_summary"]
    configs = [k for k in run_summary if k != "delta"]
    delta = run_summary.get("delta", {})

    print(f"\nSummary:")
    for config in configs:
        pr = run_summary[config]["pass_rate"]["mean"]
        label = config.replace("_", " ").title()
        print(f"  {label}: {pr*100:.1f}% pass rate")
    print(f"  Delta:         {delta.get('pass_rate', '—')}")


if __name__ == "__main__":
    main()
