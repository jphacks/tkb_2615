"""Checkpointable day-scale latent distress state for the research psychology model."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from sheltersim.domain.models import validate_unit_interval

PSYCHOLOGICAL_STATE_KEY = "psychological_distress_v1"
PSYCHOLOGICAL_STATE_SCHEMA_VERSION = 1
PSYCHOLOGICAL_STATE_POLICY_VERSION = "mean-reverting-distress-v1"


def create_psychological_state(
    baselines: dict[str, float],
) -> dict[str, Any]:
    """Create one latent state and daily acute-load accumulator per resident."""
    agents: dict[str, dict[str, float | int]] = {}
    for agent_id, baseline in sorted(baselines.items()):
        validate_unit_interval(f"baseline distress for {agent_id}", baseline)
        agents[agent_id] = {
            "baseline_distress": float(baseline),
            "long_term_distress": float(baseline),
            "daily_acute_sum": 0.0,
            "daily_high_distress_minutes": 0,
            "daily_observed_minutes": 0,
            "completed_days": 0,
        }
    return {
        "schema_version": PSYCHOLOGICAL_STATE_SCHEMA_VERSION,
        "last_updated_minute": -1,
        "agents": agents,
    }


def update_psychological_state(
    state: dict[str, Any],
    *,
    minute: int,
    acute_by_agent: dict[str, float],
    retention: float,
    acute_weight: float,
    high_distress_weight: float,
    high_distress_threshold: float,
    baseline_by_agent: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Accumulate post-social acute load and update D only at day boundaries.

    This function receives the final acute value for the tick, so social contagion
    enters the daily integral once. It does not change the acute state.
    """
    validate_psychological_state(state)
    if type(minute) is not int or minute < 0:
        raise ValueError("psychology minute must be a non-negative integer")
    if state["last_updated_minute"] != minute - 1:
        raise ValueError("psychology state must advance by exactly one minute")
    for name, value in (
        ("retention", retention),
        ("acute_weight", acute_weight),
        ("high_distress_weight", high_distress_weight),
        ("high_distress_threshold", high_distress_threshold),
    ):
        validate_unit_interval(name, value)
    result = deepcopy(state)
    agents = result["agents"]
    for agent_id, acute in sorted(acute_by_agent.items()):
        validate_unit_interval(f"acute distress for {agent_id}", acute)
        record = agents.get(agent_id)
        if record is None:
            baseline = (baseline_by_agent or {}).get(agent_id, 0.0)
            validate_unit_interval(f"baseline distress for {agent_id}", baseline)
            record = {
                "baseline_distress": float(baseline),
                "long_term_distress": float(baseline),
                "daily_acute_sum": 0.0,
                "daily_high_distress_minutes": 0,
                "daily_observed_minutes": 0,
                "completed_days": 0,
            }
            agents[agent_id] = record
        record["daily_acute_sum"] += acute
        record["daily_observed_minutes"] += 1
        if acute >= high_distress_threshold:
            record["daily_high_distress_minutes"] += 1

    if (minute + 1) % 1440 == 0:
        for record in agents.values():
            observed = record["daily_observed_minutes"]
            if observed == 0:
                continue
            mean_acute = record["daily_acute_sum"] / observed
            high_fraction = record["daily_high_distress_minutes"] / observed
            baseline = record["baseline_distress"]
            next_value = (
                baseline
                + retention * (record["long_term_distress"] - baseline)
                + acute_weight * (mean_acute - baseline)
                + high_distress_weight * high_fraction
            )
            record["long_term_distress"] = min(1.0, max(0.0, next_value))
            record["daily_acute_sum"] = 0.0
            record["daily_high_distress_minutes"] = 0
            record["daily_observed_minutes"] = 0
            record["completed_days"] += 1
    result["last_updated_minute"] = minute
    validate_psychological_state(result)
    return result


def validate_psychological_state(state: Any) -> None:
    """Validate the bounded, versioned latent state used by checkpoints."""
    if not isinstance(state, dict) or set(state) != {
        "schema_version",
        "last_updated_minute",
        "agents",
    }:
        raise ValueError("psychological distress state fields do not match schema v1")
    if state["schema_version"] != PSYCHOLOGICAL_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported psychological distress state schema")
    minute = state["last_updated_minute"]
    if type(minute) is not int or minute < -1:
        raise ValueError("psychological distress last minute is invalid")
    agents = state["agents"]
    if not isinstance(agents, dict) or len(agents) > 100_000:
        raise ValueError("psychological distress agents must be a bounded object")
    fields = {
        "baseline_distress",
        "long_term_distress",
        "daily_acute_sum",
        "daily_high_distress_minutes",
        "daily_observed_minutes",
        "completed_days",
    }
    for agent_id, record in agents.items():
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or not isinstance(record, dict)
            or set(record) != fields
        ):
            raise ValueError("psychological distress agent record is invalid")
        validate_unit_interval("baseline_distress", record["baseline_distress"])
        validate_unit_interval("long_term_distress", record["long_term_distress"])
        total = record["daily_acute_sum"]
        if (
            not isinstance(total, (int, float))
            or isinstance(total, bool)
            or not math.isfinite(total)
        ):
            raise ValueError("psychological distress daily integral must be finite")
        observed = record["daily_observed_minutes"]
        high = record["daily_high_distress_minutes"]
        days = record["completed_days"]
        if type(observed) is not int or not 0 <= observed < 1440:
            raise ValueError("psychological distress daily observed minutes are invalid")
        if type(high) is not int or not 0 <= high <= observed:
            raise ValueError("psychological distress high minutes are invalid")
        if type(days) is not int or days < 0 or total < 0 or total > observed + 1e-9:
            raise ValueError("psychological distress daily accumulation is invalid")
