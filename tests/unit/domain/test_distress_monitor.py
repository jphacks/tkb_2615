"""Boundary and censoring semantics for online high-distress episode counts."""

from __future__ import annotations

import pytest

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


def test_online_distress_monitor_tracks_high_runs_recovery_and_reascent() -> None:
    metrics: dict[str, float] = {}
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.8},
        current_by_agent={"A001": 0.85},
        threshold=0.7,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.85},
        current_by_agent={"A001": 0.9},
        threshold=0.7,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.9},
        current_by_agent={"A001": 0.3},
        threshold=0.7,
    )
    record_distress_monitor_tick(
        metrics,
        previous_by_agent={"A001": 0.3},
        current_by_agent={"A001": 0.8},
        threshold=0.7,
    )
    summary = distress_monitor_summary(metrics)
    assert summary["high_distress_episode_count"] == 2
    assert summary["high_distress_reascent_count"] == 1
    assert summary["longest_high_distress_run_minutes"] == 2
    assert summary["completed_recovery_count"] == 1
    assert summary["mean_completed_recovery_minutes"] == 2.0


def test_a_fall_below_the_threshold_during_event_handling_closes_the_run() -> None:
    """Staff support can lower distress before the psychology update (#159)."""
    metrics: dict[str, float] = {}
    record_distress_monitor_tick(
        metrics, previous_by_agent={"A001": 0.8}, current_by_agent={"A001": 0.75}, threshold=0.7
    )
    record_distress_monitor_tick(
        metrics, previous_by_agent={"A001": 0.75}, current_by_agent={"A001": 0.72}, threshold=0.7
    )
    # An event lowered distress to 0.69 between the two updates.
    record_distress_monitor_tick(
        metrics, previous_by_agent={"A001": 0.69}, current_by_agent={"A001": 0.68}, threshold=0.7
    )

    summary = distress_monitor_summary(metrics)
    assert summary["completed_recovery_count"] == 1
    assert summary["mean_completed_episode_duration_minutes"] == 2.0
    assert summary["ongoing_high_distress_episode_count"] == 0


def test_a_fall_during_event_handling_and_a_rise_in_the_same_update_are_two_episodes() -> None:
    """The fall is observed before the update, so the earlier run ends there (#159)."""
    metrics: dict[str, float] = {}
    record_distress_monitor_tick(
        metrics, previous_by_agent={"A001": 0.8}, current_by_agent={"A001": 0.75}, threshold=0.7
    )
    # An event lowered distress to 0.69; the update raises it above the threshold again.
    record_distress_monitor_tick(
        metrics, previous_by_agent={"A001": 0.69}, current_by_agent={"A001": 0.71}, threshold=0.7
    )

    summary = distress_monitor_summary(metrics)
    assert summary["high_distress_episode_count"] == 2
    assert summary["completed_recovery_count"] == 1
    assert summary["mean_completed_episode_duration_minutes"] == 1.0
    assert summary["ongoing_episode_elapsed_minutes_by_agent"] == [
        {"agent_id": "A001", "elapsed_minutes": 1}
    ]
    assert summary["high_distress_reascent_count"] == 1


@pytest.mark.parametrize("value", [float("nan"), 1.5, -0.1, True, "0.5"])
def test_values_a_float_check_does_not_accept_still_go_through_the_rule(value) -> None:
    with pytest.raises((ValueError, TypeError)):
        record_distress_monitor_tick(
            {}, previous_by_agent={"A": value}, current_by_agent={"A": 0.5}, threshold=0.5
        )
    with pytest.raises((ValueError, TypeError)):
        record_distress_monitor_tick(
            {}, previous_by_agent={"A": 0.5}, current_by_agent={"A": value}, threshold=0.5
        )


def test_int_distress_within_bounds_is_accepted_and_bad_state_is_refused() -> None:
    metrics: dict[str, float] = {}
    record_distress_monitor_tick(
        metrics, previous_by_agent={"A": 0}, current_by_agent={"A": 1}, threshold=0.5
    )
    assert metrics["acute_distress_monitor:A:current_run"] == 1.0
    metrics["acute_distress_monitor:A:episodes"] = float("inf")
    with pytest.raises(ValueError):
        record_distress_monitor_tick(
            metrics, previous_by_agent={"A": 0.0}, current_by_agent={"A": 1.0}, threshold=0.5
        )
