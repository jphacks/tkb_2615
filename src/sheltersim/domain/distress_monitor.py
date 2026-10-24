"""Compact online duration indicators for mean-reverting psychology runs."""

from __future__ import annotations

import math
from collections.abc import Mapping

from sheltersim.domain.models import validate_unit_interval

_PREFIX = "acute_distress_monitor"


def record_distress_monitor_tick(
    metrics: dict[str, float],
    *,
    previous_by_agent: Mapping[str, float],
    current_by_agent: Mapping[str, float],
    threshold: float,
) -> None:
    """Update per-Agent longest high-load runs and completed recovery durations.

    The monitor stores only five numeric values per Agent in checkpointed metric state;
    it does not retain a tick-by-tick history or affect model state.
    """
    validate_unit_interval("distress monitor threshold", threshold)
    if set(previous_by_agent) != set(current_by_agent):
        raise ValueError("distress monitor Agent sets must match")
    for agent_id in sorted(current_by_agent):
        previous = previous_by_agent[agent_id]
        current = current_by_agent[agent_id]
        validate_unit_interval(f"previous distress for {agent_id}", previous)
        validate_unit_interval(f"current distress for {agent_id}", current)
        key = f"{_PREFIX}:{agent_id}"
        run = metrics.get(f"{key}:current_run", 0.0)
        longest = metrics.get(f"{key}:longest_run", 0.0)
        episodes = metrics.get(f"{key}:episodes", 0.0)
        recovered_total = metrics.get(f"{key}:recovered_run_total", 0.0)
        recovered_count = metrics.get(f"{key}:recovered_run_count", 0.0)
        if any(
            not math.isfinite(value) or value < 0.0
            for value in (run, longest, episodes, recovered_total, recovered_count)
        ):
            raise ValueError("distress monitor state must be finite and non-negative")

        was_high = previous >= threshold
        is_high = current >= threshold
        if is_high:
            if run == 0.0:
                run = 1.0
                episodes += 1.0
            elif was_high:
                run += 1.0
            else:
                run = 1.0
                episodes += 1.0
            longest = max(longest, run)
        elif was_high and run > 0.0:
            recovered_total += run
            recovered_count += 1.0
            run = 0.0

        metrics[f"{key}:current_run"] = run
        metrics[f"{key}:longest_run"] = longest
        metrics[f"{key}:episodes"] = episodes
        metrics[f"{key}:recovered_run_total"] = recovered_total
        metrics[f"{key}:recovered_run_count"] = recovered_count


def distress_monitor_summary(metrics: Mapping[str, float]) -> dict[str, float | int]:
    """Return aggregate online indicators without loading full psychological traces."""
    agent_keys = sorted(
        key.removesuffix(":current_run")
        for key in metrics
        if key.startswith(f"{_PREFIX}:") and key.endswith(":current_run")
    )
    longest = [metrics.get(f"{key}:longest_run", 0.0) for key in agent_keys]
    episodes = sum(metrics.get(f"{key}:episodes", 0.0) for key in agent_keys)
    recovered_total = sum(metrics.get(f"{key}:recovered_run_total", 0.0) for key in agent_keys)
    recovered_count = sum(metrics.get(f"{key}:recovered_run_count", 0.0) for key in agent_keys)
    current = sum(metrics.get(f"{key}:current_run", 0.0) for key in agent_keys)
    return {
        "monitored_agent_count": len(agent_keys),
        "high_distress_episode_count": int(episodes),
        "high_distress_reascent_count": int(
            sum(max(0.0, metrics.get(f"{key}:episodes", 0.0) - 1.0) for key in agent_keys)
        ),
        "longest_high_distress_run_minutes": int(max(longest, default=0.0)),
        "mean_per_agent_longest_high_distress_run_minutes": (
            sum(longest) / len(longest) if longest else 0.0
        ),
        "completed_recovery_count": int(recovered_count),
        "mean_completed_recovery_minutes": (
            recovered_total / recovered_count if recovered_count else 0.0
        ),
        "currently_high_run_agent_minutes": int(current),
    }
