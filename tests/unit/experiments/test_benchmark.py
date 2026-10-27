"""A benchmark seed summary reports spread only when there are several seeds."""

from __future__ import annotations

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.benchmark import (
    benchmark_document,
)


def test_benchmark_seed_summary_reports_spread_only_with_multiple_seeds() -> None:
    runs = [
        {
            "agent_count": 30,
            "wall_seconds": wall,
            "cpu_seconds": wall,
            "peak_rss_mib": None,
            "events_per_tick": 0.25,
            "artifact_bytes": 1200,
            "trace_bytes": 400,
        }
        for wall in (1.0, 3.0)
    ]
    summary = benchmark_document(
        scenario_source="fixture.json",
        duration_minutes=10080,
        trace_level=TraceLevel.SUMMARY,
        runs=runs,
    )["summaries"][0]

    assert summary["seed_count"] == 2
    assert summary["wall_seconds_mean"] == 2.0
    assert summary["wall_seconds_stdev"] > 0
    assert summary["peak_rss_mib_mean"] is None
    assert summary["trace_bytes_mean"] == 400
