"""Versioned staff shift and fatigue memory built on existing staff tasks.

Rust owns the namespace and each rule below runs in native/src/rules/staff_dynamics.rs when the
kernels are installed. The Python rule stays as the comparison base and for any value a kernel does
not decide, which is also where every error text comes from.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from sheltersim import native
from sheltersim.core.kernels import kernel_first
from sheltersim.core.state_mirror import register_rust_validator
from sheltersim.core.world import WorldState
from sheltersim.domain.operational import staff_is_available

STAFF_DYNAMICS_STATE_KEY = "staff_dynamics_v1"
STAFF_DYNAMICS_POLICY_VERSION = "continuous-duty-fatigue-v1"
STAFF_DYNAMICS_SOURCE_ID = "ASM-STAFF-DYNAMICS-V1"
SHIFT_STAFF_DYNAMICS_POLICY_VERSION = "continuous-duty-fatigue-v2"
SHIFT_STAFF_DYNAMICS_SOURCE_ID = "ASM-STAFF-DYNAMICS-V2"
_CONFIG_FIELDS = {
    "schema_version",
    "policy_version",
    "source_id",
    "max_continuous_work_minutes",
    "max_continuous_work_minutes_by_staff",
    "rest_minutes",
    "shift_windows_by_staff",
    "off_shift_fatigue_recovery_per_minute",
}


@kernel_first("staff_dynamics", "canonical_config")
def canonical_staff_dynamics_config(raw: Any) -> dict[str, Any]:
    """Validate the bounded, replaceable shift/rest assumptions."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - _CONFIG_FIELDS:
        raise ValueError("staff_dynamics config has unsupported fields")
    legacy = raw.get("policy_version") == STAFF_DYNAMICS_POLICY_VERSION or (
        "policy_version" not in raw
        and not {"shift_windows_by_staff", "off_shift_fatigue_recovery_per_minute"}.intersection(
            raw
        )
    )
    policy_version = (
        STAFF_DYNAMICS_POLICY_VERSION if legacy else SHIFT_STAFF_DYNAMICS_POLICY_VERSION
    )
    source_id = raw.get(
        "source_id",
        STAFF_DYNAMICS_SOURCE_ID if legacy else SHIFT_STAFF_DYNAMICS_SOURCE_ID,
    )
    config = {
        "schema_version": 1,
        "policy_version": policy_version,
        "source_id": source_id,
        "max_continuous_work_minutes": 240,
        "max_continuous_work_minutes_by_staff": {},
        "rest_minutes": 30,
        "shift_windows_by_staff": {},
        "off_shift_fatigue_recovery_per_minute": 0.0 if legacy else 0.005,
        **raw,
    }
    if (
        config["schema_version"] != 1
        or config["policy_version"]
        not in {
            STAFF_DYNAMICS_POLICY_VERSION,
            SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
        }
        or not isinstance(config["source_id"], str)
        or not config["source_id"]
    ):
        raise ValueError("unsupported staff_dynamics policy or source")
    if config["policy_version"] == STAFF_DYNAMICS_POLICY_VERSION and (
        config["shift_windows_by_staff"] or config["off_shift_fatigue_recovery_per_minute"] != 0.0
    ):
        raise ValueError("staff shift and off-shift recovery assumptions require policy v2")
    for name, lower, upper in (
        ("max_continuous_work_minutes", 1, 24 * 60),
        ("rest_minutes", 1, 8 * 60),
    ):
        value = config[name]
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise ValueError(f"staff_dynamics {name} is outside its supported range")
    per_staff_maxima = config["max_continuous_work_minutes_by_staff"]
    if not isinstance(per_staff_maxima, dict):
        raise ValueError("staff_dynamics per-staff work limits must be an object")
    for staff_id, value in per_staff_maxima.items():
        if (
            not isinstance(staff_id, str)
            or not staff_id
            or type(value) is not int
            or not 1 <= value <= 24 * 60
        ):
            raise ValueError(
                "staff_dynamics per-staff work limits must be integer minutes in 1..1440"
            )
    windows = config["shift_windows_by_staff"]
    if not isinstance(windows, dict):
        raise ValueError("staff_dynamics shift_windows_by_staff must be an object")
    for staff_id, rows in windows.items():
        if (
            not isinstance(staff_id, str)
            or not staff_id
            or not isinstance(rows, list)
            or len(rows) > 21
        ):
            raise ValueError(
                "staff_dynamics shift windows require a staff ID and at most 21 intervals"
            )
        intervals: list[tuple[int, int]] = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"start_minute", "end_minute"}
                or type(row["start_minute"]) is not int
                or type(row["end_minute"]) is not int
                or not 0 <= row["start_minute"] < row["end_minute"] <= 10_080
            ):
                raise ValueError(
                    "staff shift intervals must be non-empty [start,end) weekly minutes"
                )
            intervals.append((row["start_minute"], row["end_minute"]))
        ordered = sorted(intervals)
        if any(first[1] > second[0] for first, second in zip(ordered, ordered[1:])):
            raise ValueError("staff shift intervals for one person cannot overlap")
    recovery = config["off_shift_fatigue_recovery_per_minute"]
    if (
        not isinstance(recovery, (int, float))
        or isinstance(recovery, bool)
        or not math.isfinite(recovery)
        or not 0.0 <= recovery <= 1.0
    ):
        raise ValueError("staff_dynamics off-shift recovery must be within 0..1 per minute")
    return config


@kernel_first("staff_dynamics", "validate_override")
def validate_staff_shift_windows_override(
    value: Any,
    *,
    known_staff_ids: set[str] | None = None,
) -> dict[str, list[dict[str, int]]]:
    """Validate a bounded per-staff map of non-overlapping weekly shift intervals."""
    if not isinstance(value, dict) or not 1 <= len(value) <= 100:
        raise ValueError("shift-window overrides require 1..100 staff IDs")
    if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > 16_384:
        raise ValueError("shift-window overrides exceed the 16 KiB limit")
    if any(
        not isinstance(staff_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", staff_id) is None
        for staff_id in value
    ):
        raise ValueError("shift-window overrides contain an invalid staff ID")
    if known_staff_ids is not None and set(value) - known_staff_ids:
        raise ValueError("shift-window overrides reference unknown staff IDs")
    normalized: dict[str, list[dict[str, int]]] = {}
    for staff_id, rows in sorted(value.items()):
        if not isinstance(rows, list) or len(rows) > 21:
            raise ValueError("each staff member may have at most 21 weekly shift intervals")
        if any(
            not isinstance(row, dict)
            or set(row) != {"start_minute", "end_minute"}
            or type(row["start_minute"]) is not int
            or type(row["end_minute"]) is not int
            or not 0 <= row["start_minute"] < row["end_minute"] <= 10_080
            for row in rows
        ):
            raise ValueError("shift intervals must be non-empty [start,end) weekly minutes")
        normalized[staff_id] = sorted(
            (dict(row) for row in rows),
            key=lambda row: (row["start_minute"], row["end_minute"]),
        )
    canonical_staff_dynamics_config(
        {
            "policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
            "shift_windows_by_staff": normalized,
        }
    )
    return normalized


@kernel_first("staff_dynamics", "create_state")
def create_staff_dynamics_state(world: WorldState) -> dict[str, Any]:
    """Create an independent runtime namespace for each configured staff member."""
    config = canonical_staff_dynamics_config(world.model_configs.get("staff_dynamics"))
    persisted_config = dict(config)
    if not persisted_config["max_continuous_work_minutes_by_staff"]:
        persisted_config.pop("max_continuous_work_minutes_by_staff")
    return {
        "schema_version": 1,
        "policy_version": config["policy_version"],
        "source_id": config["source_id"],
        "config": persisted_config,
        "staff": {
            staff_id: {
                "consecutive_work_minutes": 0,
                "total_work_minutes": 0,
                "total_rest_minutes": 0,
                "fatigue": 0.0,
                "rest_until_minute": 0,
                "last_activity": "off_duty",
            }
            for staff_id in sorted(world.staff)
        },
    }


def validate_staff_dynamics_state(world: WorldState) -> None:
    """Validate exact persisted staff values and all staff references."""
    state = world.model_state.get(STAFF_DYNAMICS_STATE_KEY)
    if state is None:
        return
    if (
        not isinstance(state, dict)
        or set(state) != {"schema_version", "policy_version", "source_id", "config", "staff"}
        or state.get("schema_version") != 1
        or not isinstance(state.get("staff"), dict)
        or set(state["staff"]) != set(world.staff)
    ):
        raise ValueError("staff_dynamics_v1 has an invalid exact schema")
    config = canonical_staff_dynamics_config(state["config"])
    if (
        state.get("policy_version") != config["policy_version"]
        or state.get("source_id") != config["source_id"]
    ):
        raise ValueError("staff_dynamics_v1 model identity does not match its config")
    legacy_config = {
        key: value
        for key, value in config.items()
        if key
        not in {
            "shift_windows_by_staff",
            "off_shift_fatigue_recovery_per_minute",
            "max_continuous_work_minutes_by_staff",
        }
    }
    config_without_per_staff_limits = dict(config)
    config_without_per_staff_limits.pop("max_continuous_work_minutes_by_staff")
    if config != state["config"] and not (
        config_without_per_staff_limits == state["config"]
        or config["policy_version"] == STAFF_DYNAMICS_POLICY_VERSION
        and legacy_config == state["config"]
    ):
        raise ValueError("staff_dynamics_v1 config is not canonical")
    if set(config["shift_windows_by_staff"]) - set(world.staff):
        raise ValueError("staff_dynamics shift roster references unknown staff")
    if set(config["max_continuous_work_minutes_by_staff"]) - set(world.staff):
        raise ValueError("staff_dynamics per-staff work limits reference unknown staff")
    for staff_id, record in state["staff"].items():
        expected = {
            "consecutive_work_minutes",
            "total_work_minutes",
            "total_rest_minutes",
            "fatigue",
            "rest_until_minute",
            "last_activity",
        }
        if not isinstance(record, dict) or set(record) != expected:
            raise ValueError(f"staff_dynamics_v1 record for {staff_id!r} is invalid")
        for name in (
            "consecutive_work_minutes",
            "total_work_minutes",
            "total_rest_minutes",
            "rest_until_minute",
        ):
            value = record[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"staff_dynamics_v1 {name} for {staff_id!r} is invalid")
        fatigue = record["fatigue"]
        if (
            not isinstance(fatigue, (int, float))
            or isinstance(fatigue, bool)
            or not math.isfinite(fatigue)
            or not 0.0 <= fatigue <= 1.0
        ):
            raise ValueError(f"staff_dynamics_v1 fatigue for {staff_id!r} is invalid")
        if record["last_activity"] not in {"working", "resting", "off_duty"}:
            raise ValueError(f"staff_dynamics_v1 activity for {staff_id!r} is invalid")


@kernel_first("staff_dynamics", "update")
def update_staff_dynamics(world: WorldState, minute: int) -> None:
    """Account for work and automatic recovery without cancelling assigned tasks."""
    state = world.model_state.get(STAFF_DYNAMICS_STATE_KEY)
    if state is None:
        return
    from sheltersim.policies.registry import create_staff_fatigue_formula

    fatigue_formula = create_staff_fatigue_formula(world.model_configs)
    config = state["config"]
    for staff_id, staff in sorted(world.staff.items()):
        record = state["staff"][staff_id]
        maximum = fatigue_formula.maximum_minutes(config["max_continuous_work_minutes"], staff_id)
        if staff.active_task_id is not None:
            record["consecutive_work_minutes"] += 1
            record["total_work_minutes"] += 1
            record["fatigue"] = fatigue_formula.fatigue(record["consecutive_work_minutes"], maximum)
            record["last_activity"] = "working"
            continue
        if not staff_is_available(world, staff_id) or not _on_shift(state, staff_id, minute):
            record["last_activity"] = "off_duty"
            record["consecutive_work_minutes"] = 0
            record["total_rest_minutes"] += 1
            record["fatigue"] = max(
                0.0, record["fatigue"] - config["off_shift_fatigue_recovery_per_minute"]
            )
            continue
        if minute < record["rest_until_minute"]:
            record["total_rest_minutes"] += 1
            record["last_activity"] = "resting"
            continue
        if record["rest_until_minute"]:
            record["consecutive_work_minutes"] = 0
            record["fatigue"] = 0.0
            record["rest_until_minute"] = 0
        if record["consecutive_work_minutes"] >= maximum:
            record["rest_until_minute"] = min(
                world.duration_minutes, minute + config["rest_minutes"]
            )
            record["last_activity"] = "resting"
        else:
            record["last_activity"] = "off_duty"


@kernel_first("staff_dynamics", "is_available")
def staff_dynamics_is_available(world: WorldState, staff_id: str, minute: int) -> bool:
    """Exclude staff members who are off shift or in a required recovery break."""
    state = world.model_state.get(STAFF_DYNAMICS_STATE_KEY)
    if state is None:
        return True
    record = state["staff"].get(staff_id)
    operations = world.model_state.get("staff_operations_v1", {})
    operation = operations.get("staff", {}).get(staff_id) if isinstance(operations, dict) else None
    return (
        record is not None
        and _on_shift(state, staff_id, minute)
        and minute >= record["rest_until_minute"]
        and staff_is_available(world, staff_id)
        and (operation is None or operation.get("phase") == "idle")
    )


@kernel_first("staff_dynamics", "on_shift")
def staff_dynamics_on_shift(world: WorldState, staff_id: str, minute: int) -> bool:
    """Report shift/rest eligibility without treating an active assignment as a block."""
    state = world.model_state.get(STAFF_DYNAMICS_STATE_KEY)
    if state is None:
        return True
    record = state["staff"].get(staff_id)
    return (
        record is not None
        and _on_shift(state, staff_id, minute)
        and minute >= record["rest_until_minute"]
    )


def _on_shift(state: dict[str, Any], staff_id: str, minute: int) -> bool:
    """Use recurring week-minute intervals; unspecified staff remain continuously rostered."""
    rows = state["config"]["shift_windows_by_staff"].get(staff_id)
    if rows is None:
        return True
    week_minute = minute % 10_080
    return any(row["start_minute"] <= week_minute < row["end_minute"] for row in rows)


@kernel_first("staff_dynamics", "add_staff")
def add_staff(world: WorldState, staff_id: str) -> None:
    """Initialize the runtime record after a typed event adds a staff member."""
    state = world.model_state.get(STAFF_DYNAMICS_STATE_KEY)
    if state is not None:
        state["staff"][staff_id] = {
            "consecutive_work_minutes": 0,
            "total_work_minutes": 0,
            "total_rest_minutes": 0,
            "fatigue": 0.0,
            "rest_until_minute": 0,
            "last_activity": "off_duty",
        }


if native.available():
    register_rust_validator(STAFF_DYNAMICS_STATE_KEY, native.state("staff_dynamics").validate)
