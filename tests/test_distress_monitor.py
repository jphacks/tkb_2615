"""Boundary and censoring semantics for online high-distress episode counts."""

from __future__ import annotations

from sheltersim.domain.distress_monitor import (
    distress_monitor_summary,
    record_distress_monitor_tick,
)


def test_threshold_boundary_recovery_reascent_and_right_censoring_are_separate() -> None:
    metrics: dict[str, float] = {}
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.69, "A002": 0.2},
        current_by_agent={"A001": 0.70, "A002": 0.2},
        threshold=0.70,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.70, "A002": 0.2},
        current_by_agent={"A001": 0.70, "A002": 0.2},
        threshold=0.70,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.70, "A002": 0.2},
        current_by_agent={"A001": 0.699, "A002": 0.2},
        threshold=0.70,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.699, "A002": 0.2},
        current_by_agent={"A001": 0.71, "A002": 0.2},
        threshold=0.70,
    )

    summary = distress_monitor_summary(metrics)
    assert summary["high_distress_episode_count"] == 2
    assert summary["completed_high_distress_episode_count"] == 1
    assert summary["ongoing_high_distress_episode_count"] == 1
    assert summary["mean_completed_episode_duration_minutes"] == 2.0
    assert summary["ongoing_episode_elapsed_minutes_by_agent"] == [
        {"agent_id": "A001", "elapsed_minutes": 1}
    ]
    assert summary["ongoing_episode_elapsed_minutes_total"] == 1
    assert summary["longest_high_distress_run_minutes"] == 2
    assert summary["high_distress_reascent_count"] == 1
    assert summary["end_high_distress_agent_share"] == 0.5


def test_empty_monitor_has_defined_zero_summary() -> None:
    summary = distress_monitor_summary({})
    assert summary["high_distress_episode_count"] == 0
    assert summary["completed_high_distress_episode_count"] == 0
    assert summary["ongoing_high_distress_episode_count"] == 0
    assert summary["mean_completed_episode_duration_minutes"] == 0.0
    assert summary["end_high_distress_agent_share"] == 0.0
