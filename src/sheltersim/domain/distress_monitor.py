"""Compact online duration indicators for mean-reverting psychology runs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from sheltersim.domain.models import validate_unit_interval

_PREFIX = "acute_distress_monitor"


@lru_cache(maxsize=1024)
def _metric_keys(agent_id: str) -> tuple[str, str, str, str, str]:
    key = f"{_PREFIX}:{agent_id}"
    return (
        f"{key}:current_run",
        f"{key}:longest_run",
        f"{key}:episodes",
        f"{key}:recovered_run_total",
        f"{key}:recovered_run_count",
    )


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
    if previous_by_agent.keys() != current_by_agent.keys():
        raise ValueError("distress monitor Agent sets must match")
    for agent_id in sorted(current_by_agent):
        previous = previous_by_agent[agent_id]
        current = current_by_agent[agent_id]
        # Floats within bounds pass, and NaN fails the comparison; the check decides the rest.
        if not (type(previous) is float and 0.0 <= previous <= 1.0):
            validate_unit_interval(f"previous distress for {agent_id}", previous)
        if not (type(current) is float and 0.0 <= current <= 1.0):
            validate_unit_interval(f"current distress for {agent_id}", current)
        run_key, longest_key, episodes_key, total_key, count_key = _metric_keys(agent_id)
        run = metrics.get(run_key, 0.0)
        longest = metrics.get(longest_key, 0.0)
        episodes = metrics.get(episodes_key, 0.0)
        recovered_total = metrics.get(total_key, 0.0)
        recovered_count = metrics.get(count_key, 0.0)
        for value in (run, longest, episodes, recovered_total, recovered_count):
            # Not finite or negative; NaN fails the comparison.
            if not 0.0 <= value < math.inf:
                raise ValueError("distress monitor state must be finite and non-negative")

        was_high = previous >= threshold
        is_high = current >= threshold
        if run > 0.0 and not (was_high and is_high):
            # Distress fell below the threshold in this update or, when the previous
            # value is already low, while events were handled before it (#159).
            recovered_total += run
            recovered_count += 1.0
            run = 0.0
        if is_high:
            if run == 0.0:
                run = 1.0
                episodes += 1.0
            else:
                run += 1.0
            longest = max(longest, run)

        metrics[run_key] = run
        metrics[longest_key] = longest
        metrics[episodes_key] = episodes
        metrics[total_key] = recovered_total
        metrics[count_key] = recovered_count


def distress_monitor_summary(metrics: Mapping[str, float]) -> dict[str, Any]:
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
    ongoing = [
        {
            "agent_id": key.removeprefix(f"{_PREFIX}:"),
            "elapsed_minutes": int(metrics.get(f"{key}:current_run", 0.0)),
        }
        for key in agent_keys
        if metrics.get(f"{key}:current_run", 0.0) > 0.0
    ]
    mean_completed = recovered_total / recovered_count if recovered_count else 0.0
    return {
        "monitored_agent_count": len(agent_keys),
        "high_distress_episode_count": int(episodes),
        "completed_high_distress_episode_count": int(recovered_count),
        "ongoing_high_distress_episode_count": len(ongoing),
        "ongoing_episode_elapsed_minutes_by_agent": ongoing,
        "ongoing_episode_elapsed_minutes_total": int(current),
        "mean_completed_episode_duration_minutes": mean_completed,
        "high_distress_reascent_count": int(
            sum(max(0.0, metrics.get(f"{key}:episodes", 0.0) - 1.0) for key in agent_keys)
        ),
        "longest_high_distress_run_minutes": int(max(longest, default=0.0)),
        "end_high_distress_agent_share": (len(ongoing) / len(agent_keys) if agent_keys else 0.0),
        "mean_per_agent_longest_high_distress_run_minutes": (
            sum(longest) / len(longest) if longest else 0.0
        ),
        "completed_recovery_count": int(recovered_count),
        # Retained for readers of earlier run artifacts; new reports should use the
        # explicitly episode-scoped mean above.
        "mean_completed_recovery_minutes": mean_completed,
        "currently_high_run_agent_minutes": int(current),
    }
