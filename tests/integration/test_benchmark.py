"""Scaled benchmark Runs are reproducible and record their measurements.

The seed summary is checked in tests/unit/experiments/test_benchmark.py.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from support.paths import BASELINE_100_7D

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.benchmark import (
    benchmark_document,
    build_benchmark_scenario,
    measure_scenario,
    write_benchmark_document,
)
from sheltersim.experiments.scenario import ScenarioSpec


def test_scaled_benchmark_runs_are_reproducible_and_record_measurements(tmp_path: Path) -> None:
    base = ScenarioSpec.load(BASELINE_100_7D)
    first_scenario = build_benchmark_scenario(
        base,
        agent_count=30,
        seed=2030,
        duration_minutes=20,
    )
    repeated_scenario = build_benchmark_scenario(
        base,
        agent_count=30,
        seed=2030,
        duration_minutes=20,
    )
    assert first_scenario.canonical_json() == repeated_scenario.canonical_json()
    assert len(first_scenario.profiles) == 30
    sleeping_zone = next(zone for zone in first_scenario.zones if zone.id == "sleeping")
    assert sleeping_zone.capacity is not None and sleeping_zone.capacity >= 30

    result = measure_scenario(
        first_scenario,
        tmp_path / "run",
        trace_level=TraceLevel.OFF,
    )
    assert result["run_status"] == "completed"
    assert result["agent_count"] == 30
    assert result["duration_minutes"] == 20
    assert result["scenario_hash"]
    assert result["final_state_hash"]
    assert result["wall_seconds"] > 0
    assert result["cpu_seconds"] > 0
    assert result["artifact_bytes"] > 0
    assert result["trace_bytes"] > 0
    assert result["trace_bytes"] <= result["artifact_bytes"]
    assert math.isfinite(result["events_per_tick"])

    report = benchmark_document(
        scenario_source=str(BASELINE_100_7D),
        duration_minutes=20,
        trace_level=TraceLevel.OFF,
        runs=[result],
    )
    output_json = tmp_path / "results.json"
    write_benchmark_document(report, output_json)
    assert json.loads(output_json.read_text(encoding="utf-8")) == report
    markdown = output_json.with_suffix(".md").read_text(encoding="utf-8")
    assert "offline" in markdown
    assert "Trace (MiB)" in markdown
    assert "Trace volume is measured separately" in markdown
    assert "single-seed result is descriptive only" in markdown


def test_a_late_window_reaches_the_full_runs_hashes_and_a_mismatch_fails(tmp_path: Path) -> None:
    """benchmarks/investigate_performance.py can fork a saved Run and measure only its
    end; --expect compares the world, events and KPI hashes with an earlier result."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    script = root / "benchmarks" / "investigate_performance.py"

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), "--route", "project", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )

    full = tmp_path / "full"
    assert run("--duration", "40", "--output", str(full)).returncode == 0
    late = run(
        "--duration",
        "10",
        "--from-project",
        str(full / "projects"),
        "--fork-minute",
        "30",
        "--output",
        str(tmp_path / "late"),
        "--expect",
        str(full / "result.json"),
    )
    assert late.returncode == 0, late.stderr
    result = json.loads((tmp_path / "late" / "result.json").read_text(encoding="utf-8"))
    assert result["late_window"]["fork_minute"] == 30
    assert result["expected_hashes"]["mismatched"] == []

    shorter = run(
        "--duration",
        "30",
        "--output",
        str(tmp_path / "shorter"),
        "--expect",
        str(full / "result.json"),
    )
    assert shorter.returncode == 1
    assert "hashes differ" in shorter.stderr
