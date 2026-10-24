"""Offline, reproducible performance measurements for fixed synthetic scenarios."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.profiles import ProfileTemplate, SyntheticProfileGenerator
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

_TRACE_FILES = (
    "events_planned.json",
    "events_realized.jsonl",
    "decisions.jsonl",
    "social_decisions.jsonl",
    "social_contacts.jsonl",
)


def build_benchmark_scenario(
    base: ScenarioSpec,
    *,
    agent_count: int,
    seed: int,
    duration_minutes: int,
) -> ScenarioSpec:
    """Scale a baseline scenario with deterministic synthetic profiles."""
    if not isinstance(agent_count, int) or isinstance(agent_count, bool) or agent_count <= 0:
        raise ValueError("agent_count must be a positive integer")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if (
        not isinstance(duration_minutes, int)
        or isinstance(duration_minutes, bool)
        or duration_minutes <= 0
    ):
        raise ValueError("duration_minutes must be a positive integer")
    if any(event.due_minute >= duration_minutes for event in base.scheduled_events):
        raise ValueError("base scenario has planned events outside the benchmark duration")

    generation = base.provenance.get("profile_generation")
    if not isinstance(generation, dict):
        raise ValueError("base scenario must include a profile_generation manifest")
    try:
        template = ProfileTemplate.model_validate(generation["template"])
        zone_ids = tuple(generation["zone_ids"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("base profile-generation manifest is invalid") from exc

    generated = SyntheticProfileGenerator().generate(
        count=agent_count,
        seed=seed,
        zone_ids=zone_ids,
        template=template,
    )
    document = base.model_dump(mode="json")
    document["id"] = f"benchmark-{agent_count}-{duration_minutes}-{seed}"
    document["duration_minutes"] = duration_minutes
    document["seed"] = seed
    document["profiles"] = [profile.model_dump(mode="json") for profile in generated.profiles]
    document["provenance"] = {
        **document["provenance"],
        "profile_generation": generated.manifest.model_dump(mode="json"),
        "benchmark": {"source_scenario_id": base.id, "synthetic_load": True},
    }
    # All generated profiles start in this zone; expand its declared capacity to fit them.
    for zone in document["zones"]:
        if zone["id"] in {profile.initial_zone_id for profile in generated.profiles}:
            current_capacity = zone["capacity"]
            zone["capacity"] = max(agent_count, current_capacity or 0)
    return ScenarioSpec.model_validate(document)


def measure_scenario(
    scenario: ScenarioSpec,
    output_directory: str | Path,
    *,
    trace_level: TraceLevel = TraceLevel.SUMMARY,
) -> dict[str, Any]:
    """Run once offline and collect time, CPU, memory, events and artifact size."""
    destination = Path(output_directory)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f"benchmark output directory is not empty: {destination}")

    process_before = time.process_time()
    started = time.perf_counter()
    world = run_scenario(scenario, destination, trace_level=trace_level)
    wall_seconds = time.perf_counter() - started
    cpu_seconds = time.process_time() - process_before
    world.validate()

    rss_mib: float | None = None
    if resource is not None:
        maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux and the BSDs used in CI report KiB.
        rss_mib = maximum_rss / (1024**2 if sys.platform == "darwin" else 1024)
    artifact_bytes = sum(item.stat().st_size for item in destination.rglob("*") if item.is_file())
    trace_bytes = sum(
        (destination / filename).stat().st_size
        for filename in _TRACE_FILES
        if (destination / filename).is_file()
    )
    realized_event_count = len(world.event_log)
    scenario_hash = hashlib.sha256(scenario.canonical_json().encode("utf-8")).hexdigest()
    return {
        "scenario_id": scenario.id,
        "scenario_hash": scenario_hash,
        "agent_count": len(world.agents),
        "duration_minutes": world.duration_minutes,
        "seed": world.seed,
        "model_configs": scenario.model_configs,
        "trace_level": TraceLevel(trace_level).value,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "peak_rss_mib": rss_mib,
        "realized_event_count": realized_event_count,
        "events_per_tick": realized_event_count / world.duration_minutes,
        "artifact_bytes": artifact_bytes,
        "trace_bytes": trace_bytes,
        "final_state_hash": hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest(),
        "run_status": "completed",
    }


def environment_manifest() -> dict[str, Any]:
    """Describe the environment needed to compare benchmark results."""
    project_root = Path(__file__).resolve().parents[3]
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        git_sha = None
    try:
        git_status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
        git_dirty = bool(git_status)
    except (OSError, subprocess.SubprocessError):
        git_dirty = None
    return {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "git_sha": git_sha,
        "git_worktree_dirty": git_dirty,
        "source_tree_hash": _source_tree_hash(project_root),
        "execution_mode": "offline",
        "rng_algorithm": "sha256-keyed-v1",
        "model_versions": {
            "behavior": "utility-softmax-v1",
            "psychology": "causal-stress-v1",
            "space": "shortest-travel-time-v1",
            "toilet_service": "toilet-service-uniform-v1",
        },
    }


def _source_tree_hash(project_root: Path) -> str | None:
    sources = sorted((project_root / "src" / "sheltersim").rglob("*.py"))
    sources.extend(sorted((project_root / "benchmarks").rglob("*.py")))
    project_config = project_root / "pyproject.toml"
    if project_config.is_file():
        sources.append(project_config)
    if not sources:
        return None
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.relative_to(project_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def summarize_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate measurements by Agent count without treating seeds as replications of truth."""
    summaries: list[dict[str, Any]] = []
    for count in sorted({row["agent_count"] for row in runs}):
        rows = [row for row in runs if row["agent_count"] == count]
        summary: dict[str, Any] = {"agent_count": count, "seed_count": len(rows)}
        for metric in ("wall_seconds", "cpu_seconds", "peak_rss_mib", "events_per_tick"):
            values = [row[metric] for row in rows if row[metric] is not None]
            summary[f"{metric}_mean"] = statistics.fmean(values) if values else None
            summary[f"{metric}_stdev"] = statistics.stdev(values) if len(values) > 1 else None
        summary["artifact_bytes_mean"] = statistics.fmean(row["artifact_bytes"] for row in rows)
        trace_sizes = [row.get("trace_bytes", row["artifact_bytes"]) for row in rows]
        summary["trace_bytes_mean"] = statistics.fmean(trace_sizes)
        summaries.append(summary)
    return summaries


def render_markdown_report(document: dict[str, Any]) -> str:
    """Render machine-readable benchmark data as a concise, provenance-rich report."""
    versions = json.dumps(document["environment"]["model_versions"], sort_keys=True)
    lines = [
        "# Offline performance benchmark",
        "",
        "Results measure this implementation and synthetic workload only. They are not",
        "evidence about real-world shelter operations or psychological outcomes.",
        "",
        f"- Environment: {document['environment']['platform']}",
        f"- Python: {document['environment']['python_version']} "
        f"({document['environment']['implementation']})",
        f"- Git SHA: {document['environment']['git_sha'] or 'unavailable'}",
        f"- Git worktree dirty: {document['environment']['git_worktree_dirty']}",
        f"- Source tree hash: `{document['environment']['source_tree_hash'] or 'unavailable'}`",
        f"- Scenario source: `{document['scenario_source']}`",
        f"- Duration: {document['duration_minutes']} minutes per run",
        f"- Trace level: `{document['trace_level']}`",
        "- Execution mode: offline; no API calls",
        f"- Model versions: `{versions}`",
        "",
        "## Results by run",
        "",
        "| Agents | Seed | Wall (s) | CPU (s) | Peak RSS (MiB) | Events/tick | "
        "Trace (MiB) | Artifacts (MiB) | Scenario hash |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in document["runs"]:
        rss = "n/a" if row["peak_rss_mib"] is None else f"{row['peak_rss_mib']:.1f}"
        lines.append(
            f"| {row['agent_count']} | {row['seed']} | {row['wall_seconds']:.2f} "
            f"| {row['cpu_seconds']:.2f} | {rss} | {row['events_per_tick']:.4f} "
            f"| {row.get('trace_bytes', row['artifact_bytes']) / (1024**2):.2f} "
            f"| {row['artifact_bytes'] / (1024**2):.2f} | `{row['scenario_hash'][:12]}` |"
        )
    lines.extend(
        [
            "",
            "## Seed summaries",
            "",
            "| Agents | Seeds | Mean wall (s) | Wall stdev (s) | "
            "Mean peak RSS (MiB) | Mean events/tick | Mean trace (MiB) |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in document["summaries"]:
        wall_sd = "n/a" if row["wall_seconds_stdev"] is None else f"{row['wall_seconds_stdev']:.2f}"
        rss = "n/a" if row["peak_rss_mib_mean"] is None else f"{row['peak_rss_mib_mean']:.1f}"
        lines.append(
            f"| {row['agent_count']} | {row['seed_count']} | {row['wall_seconds_mean']:.2f} "
            f"| {wall_sd} | {rss} | {row['events_per_tick_mean']:.4f} "
            f"| {row['trace_bytes_mean'] / (1024**2):.2f} |"
        )
    lines.extend(
        [
            "",
            (
                "Peak RSS is process high-water RSS for an isolated run process; it includes "
                "Python startup."
            ),
            (
                "Artifacts include all files written by the run. Wall and CPU times include "
                "simulation and artifact writing."
            ),
            (
                "Trace volume is measured separately as planned/realized event files, decision "
                "records, social-decision records and contact traces."
            ),
            (
                "A single-seed result is descriptive only. No causal or statistical claim is "
                "made from these runs."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def benchmark_document(
    *,
    scenario_source: str,
    duration_minutes: int,
    trace_level: TraceLevel,
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a stable result document with its full execution provenance."""
    return {
        "benchmark_schema_version": 1,
        "environment": environment_manifest(),
        "scenario_source": scenario_source,
        "duration_minutes": duration_minutes,
        "trace_level": TraceLevel(trace_level).value,
        "runs": runs,
        "summaries": summarize_runs(runs),
    }


def write_benchmark_document(document: dict[str, Any], output_json: str | Path) -> None:
    """Atomically write JSON data and its adjacent Markdown report."""
    destination = Path(output_json)
    destination.parent.mkdir(parents=True, exist_ok=True)
    json_contents = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    _write_atomic(destination, json_contents + "\n")
    _write_atomic(destination.with_suffix(".md"), render_markdown_report(document))


def _write_atomic(path: Path, contents: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(contents, encoding="utf-8", newline="")
    temporary.replace(path)
