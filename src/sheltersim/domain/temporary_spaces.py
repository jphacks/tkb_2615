"""Versioned, operator-approved temporary capacity for dynamic population overflow.

This contract affects admission and capacity validation only. It carries no floorplan
coordinates, so consumers must not draw a fabricated room or walking path for a space.
"""
from __future__ import annotations

from typing import Any

from sheltersim.core.world import WorldState

TEMPORARY_SPACES_STATE_KEY = "temporary_spaces_v1"
TEMPORARY_SPACES_POLICY_VERSION = "typed-temporary-capacity-v1"
MAX_TEMPORARY_SPACES = 256
MAX_TEMPORARY_SPACE_HISTORY = 20_000

_STATE_FIELDS = {"schema_version", "policy_version", "spaces", "history"}
_SPACE_FIELDS = {
    "space_id", "zone_id", "people_capacity", "sleeping_capacity",
    "start_minute", "end_minute", "active", "start_event_id", "end_event_id",
    "program_id", "effect_id", "source_ids", "geometry_status",
}


def create_temporary_spaces_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_version": TEMPORARY_SPACES_POLICY_VERSION,
        "spaces": {},
        "history": [],
    }


def validate_temporary_spaces_state(world: WorldState) -> None:
    state = world.model_state.get(TEMPORARY_SPACES_STATE_KEY)
    if state is None:
        return
    if (
        not isinstance(state, dict)
        or set(state) != _STATE_FIELDS
        or state.get("schema_version") != 1
        or state.get("policy_version") != TEMPORARY_SPACES_POLICY_VERSION
        or not isinstance(state.get("spaces"), dict)
        or len(state["spaces"]) > MAX_TEMPORARY_SPACES
        or not isinstance(state.get("history"), list)
        or len(state["history"]) > MAX_TEMPORARY_SPACE_HISTORY
    ):
        raise ValueError("temporary_spaces_v1 has an invalid exact schema")
    for space_id, row in state["spaces"].items():
        if (
            not isinstance(row, dict)
            or set(row) != _SPACE_FIELDS
            or row.get("space_id") != space_id
            or row.get("zone_id") not in world.zones
            or type(row.get("people_capacity")) is not int
            or not 1 <= row["people_capacity"] <= 1000
            or type(row.get("sleeping_capacity")) is not int
            or not 0 <= row["sleeping_capacity"] <= row["people_capacity"]
            or type(row.get("start_minute")) is not int
            or type(row.get("end_minute")) is not int
            or row["start_minute"] < 0
            or row["end_minute"] <= row["start_minute"]
            or type(row.get("active")) is not bool
            or not isinstance(row.get("start_event_id"), str)
            or not row["start_event_id"]
            or (row["active"] and row.get("end_event_id") is not None)
            or (not row["active"] and (
                not isinstance(row.get("end_event_id"), str) or not row["end_event_id"]
            ))
            or not isinstance(row.get("program_id"), str)
            or not isinstance(row.get("effect_id"), str)
            or not isinstance(row.get("source_ids"), list)
            or any(not isinstance(item, str) or not item for item in row["source_ids"])
            or row.get("geometry_status") != "zone_only_capacity"
        ):
            raise ValueError(f"temporary space {space_id!r} is invalid")
    for row in state["history"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"event_id", "space_id", "zone_id", "minute", "transition", "causation_id"}
            or not isinstance(row["event_id"], str)
            or not isinstance(row["space_id"], str)
            or row["zone_id"] not in world.zones
            or type(row["minute"]) is not int
            or row["transition"] not in {"activated", "expired"}
            or (row["causation_id"] is not None and not isinstance(row["causation_id"], str))
        ):
            raise ValueError("temporary_spaces_v1 history record is invalid")


def capacity_metrics(world: WorldState, zone_id: str, *, occupancy: int | None = None) -> dict[str, int | None]:
    """Return zone-only capacity and separately measured physical room intake."""
    zone = world.zones[zone_id]
    nominal = zone.capacity
    if nominal is None:
        return {
            "nominal_capacity": None,
            "effective_capacity": None,
            "temporary_people_capacity": 0,
            "retained_expired_capacity": 0,
            "temporary_sleeping_capacity": 0,
            "physical_room_capacity": 0,
            "active_room_admissions": 0,
            "retained_expired_room_admissions": 0,
            "effective_total_capacity": None,
        }
    current = (
        sum(agent.zone_id == zone_id for agent in world.agents.values())
        if occupancy is None else occupancy
    )
    state = world.model_state.get(TEMPORARY_SPACES_STATE_KEY, {})
    records = (
        [row for row in state.get("spaces", {}).values() if row.get("zone_id") == zone_id]
        if isinstance(state, dict) else []
    )
    active_people = sum(row["people_capacity"] for row in records if row["active"])
    expired_people = sum(row["people_capacity"] for row in records if not row["active"])
    retained = min(expired_people, max(0, current - nominal - active_people))
    effective = nominal + active_people + retained
    overlay_config = world.model_configs.get("geometry_overlay", {})
    geometry_overlay = world.model_state.get("geometry_overlay_v1", {})
    physical_rooms = (
        geometry_overlay.get("temporary_rooms", {})
        if isinstance(geometry_overlay, dict) else {}
    )
    room_admissions = (
        geometry_overlay.get("room_admissions", {})
        if isinstance(geometry_overlay, dict) else {}
    )
    room_rows = [
        row for row in physical_rooms.values()
        if isinstance(row, dict) and row.get("zone_id") == zone_id
    ] if isinstance(physical_rooms, dict) else []
    room_capacity = (
        sum(row["people_capacity"] for row in room_rows if row.get("active"))
        if isinstance(overlay_config, dict) and overlay_config.get("schema_version") == 3
        else 0
    )
    active_room_ids = {row["room_id"] for row in room_rows if row.get("active")}
    room_counts: dict[str, int] = {}
    if isinstance(room_admissions, dict):
        for row in room_admissions.values():
            if isinstance(row, dict) and isinstance(row.get("room_id"), str):
                room_counts[row["room_id"]] = room_counts.get(row["room_id"], 0) + 1
    active_admissions = sum(room_counts.get(room_id, 0) for room_id in active_room_ids)
    expired_admissions = sum(
        room_counts.get(row["room_id"], 0)
        for row in room_rows
        if not row.get("active")
    )
    retained_room_capacity = min(
        sum(row["people_capacity"] for row in room_rows if not row.get("active")),
        expired_admissions,
    )
    return {
        "nominal_capacity": nominal,
        "effective_capacity": effective,
        "temporary_people_capacity": active_people,
        "retained_expired_capacity": retained,
        "temporary_sleeping_capacity": sum(
            row["sleeping_capacity"] for row in records if row["active"]
        ),
        "physical_room_capacity": room_capacity,
        "active_room_admissions": active_admissions,
        "retained_expired_room_admissions": retained_room_capacity,
        "effective_total_capacity": effective + room_capacity + retained_room_capacity,
    }


def effective_zone_capacity(world: WorldState, zone_id: str, *, occupancy: int | None = None) -> int | None:
    return capacity_metrics(world, zone_id, occupancy=occupancy)["effective_capacity"]


def effective_total_zone_capacity(
    world: WorldState, zone_id: str, *, occupancy: int | None = None
) -> int | None:
    """Capacity bound for WorldState validation, including signed physical rooms."""
    return capacity_metrics(world, zone_id, occupancy=occupancy)["effective_total_capacity"]


def register_temporary_space(world: WorldState, record: dict[str, Any], *, event_id: str) -> None:
    state = world.model_state.get(TEMPORARY_SPACES_STATE_KEY)
    if not isinstance(state, dict):
        raise ValueError("temporary_spaces_v1 is not installed")
    if record["space_id"] in state["spaces"]:
        raise ValueError("temporary space ID already exists")
    if len(state["spaces"]) >= MAX_TEMPORARY_SPACES:
        raise ValueError("temporary space runtime limit reached")
    state["spaces"][record["space_id"]] = record
    _append_history(
        state, event_id=event_id, record=record, minute=world.minute_index,
        transition="activated", causation_id=None,
    )


def expire_temporary_space(
    world: WorldState, space_id: str, *, event_id: str, causation_id: str | None
) -> bool:
    state = world.model_state.get(TEMPORARY_SPACES_STATE_KEY)
    if not isinstance(state, dict):
        raise ValueError("temporary_spaces_v1 is not installed")
    record = state["spaces"].get(space_id)
    if record is None or not record["active"]:
        return False
    record["active"] = False
    record["end_event_id"] = event_id
    _append_history(
        state, event_id=event_id, record=record, minute=world.minute_index,
        transition="expired", causation_id=causation_id,
    )
    return True


def _append_history(
    state: dict[str, Any],
    *,
    event_id: str,
    record: dict[str, Any],
    minute: int,
    transition: str,
    causation_id: str | None,
) -> None:
    state["history"].append({
        "event_id": event_id,
        "space_id": record["space_id"],
        "zone_id": record["zone_id"],
        "minute": minute,
        "transition": transition,
        "causation_id": causation_id,
    })
    if len(state["history"]) > MAX_TEMPORARY_SPACE_HISTORY:
        del state["history"][:len(state["history"]) - MAX_TEMPORARY_SPACE_HISTORY]
