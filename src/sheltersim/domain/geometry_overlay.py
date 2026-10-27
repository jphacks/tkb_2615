"""Versioned runtime geometry changes; immutable Scenario geometry stays unchanged.

Idle facility moves and transfers, bounded obstacle partitions, and timed route
closures are supported. Physical temporary rooms require a separate explicit
geometry operator; zone-only capacity never pretends to be a surveyed room.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import (
    ExtensionModel,
    FacilityGeometry,
    Identifier,
    Point2D,
    point_in_polygon,
)
from sheltersim.contracts.operations import (
    EquipmentTransferParameters,
    TemporaryRoomGeometry,
)
from sheltersim.contracts.spatial import SpatialConstraints, SpatialObstacle

GEOMETRY_OVERLAY_KEY = "geometry_overlay_v1"

# Movement asks for the facility geometry hundreds of times per tick. Inside a
# config scope (one tick phase), each raw model_configs object is parsed once and
# the frozen model is shared. Outside a scope every call parses, so a config edited
# between phases (tests, scenario setup) is always seen. Entries hold the raw object,
# so its id cannot be reused within the scope.
_CONFIG_SCOPE: ContextVar[dict[tuple[str, int], tuple[Any, Any]] | None] = ContextVar(
    "geometry_config_scope", default=None
)
_EMPTY_SPATIAL_CONSTRAINTS: dict[str, Any] = {}


@contextmanager
def geometry_config_scope() -> Iterator[None]:
    """Reuse parsed facility geometry configs while model_configs cannot change."""
    if _CONFIG_SCOPE.get() is not None:
        yield
        return
    token = _CONFIG_SCOPE.set({})
    try:
        yield
    finally:
        _CONFIG_SCOPE.reset(token)


def _parsed_config(name: str, raw: Any, model: Any) -> Any:
    scope = _CONFIG_SCOPE.get()
    if scope is None:
        return model.model_validate(raw)
    key = (name, id(raw))
    entry = scope.get(key)
    if entry is not None and entry[0] is raw:
        return entry[1]
    parsed = model.model_validate(raw)
    scope[key] = (raw, parsed)
    return parsed
GEOMETRY_OVERLAY_POLICY = "bounded-runtime-geometry-v1"
TEMPORARY_ROOM_BED_POLICY_VERSION = "exclusive-physical-sleep-beds-v1"
TEMPORARY_ROOM_BED_SOURCE_ID = "ASM-TEMPORARY-ROOM-BEDS-V1"
TEMPORARY_ROOM_INTAKE_POLICY_VERSION = "physical-temporary-room-intake-v1"
TEMPORARY_ROOM_INTAKE_SOURCE_ID = "ASM-TEMPORARY-ROOM-INTAKE-V1"
# geometry_overlay schema_version=4 places facilities installed during a Run on
# the floor plan with the Scenario generator's own grid rule.
INSTALLED_FACILITY_PLACEMENT_POLICY_VERSION = "generator-grid-installed-facility-v1"
INSTALLED_FACILITY_PLACEMENT_SOURCE_ID = "ASM-INSTALLED-FACILITY-PLACEMENT-V1"
# The narrowest physical queue spacing (independent walkers, movement.py); the
# generator itself can place facilities this close in a narrow zone.
INSTALLED_FACILITY_MIN_SEPARATION_M = 0.6
MAX_INSTALLED_FACILITY_GRID_GROWTH = 16
MAX_BED_ROUTE_CANDIDATES = 256
MAX_ROOM_ADMISSION_POINTS = 256
GEOMETRY_OPERATORS = frozenset(
    {
        "move_facility",
        "transfer_equipment",
        "set_partition",
        "set_route_access",
        "temporary_room_geometry",
    }
)


class MoveFacilityParameters(ExtensionModel):
    point_m: Point2D


class PartitionParameters(ExtensionModel):
    polygon_m: tuple[Point2D, ...] = Field(min_length=3, max_length=64)


class RouteAccessParameters(ExtensionModel):
    accessible: bool = Field(strict=True)


class _FacilityPointOverride(ExtensionModel):
    point_m: Point2D
    event_id: Identifier
    source_ids: tuple[Identifier, ...]
    minute: int = Field(strict=True, ge=0)


class _PartitionOverride(ExtensionModel):
    zone_id: Identifier
    polygon_m: tuple[Point2D, ...] = Field(min_length=3, max_length=64)
    event_id: Identifier
    source_ids: tuple[Identifier, ...]
    minute: int = Field(strict=True, ge=0)
    end_minute: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def interval_is_ordered(self) -> _PartitionOverride:
        if self.end_minute <= self.minute:
            raise ValueError("partition end must follow its start")
        return self


class _RouteModifier(ExtensionModel):
    accessible: bool = Field(strict=True)
    event_id: Identifier
    source_ids: tuple[Identifier, ...]
    minute: int = Field(strict=True, ge=0)
    end_minute: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def interval_is_ordered(self) -> _RouteModifier:
        if self.end_minute <= self.minute:
            raise ValueError("route modifier end must follow its start")
        return self


class _RouteOverlay(ExtensionModel):
    base_closed: bool = Field(strict=True)
    modifiers: dict[Identifier, _RouteModifier]


class _TemporaryRoomOverlay(ExtensionModel):
    schema_version: Literal[1]
    room_id: Identifier
    zone_id: Identifier
    polygon_m: tuple[Point2D, ...]
    people_capacity: int = Field(strict=True, gt=0, le=1_000_000)
    sleeping_capacity: int = Field(strict=True, ge=0, le=1_000_000)
    bed_points_m: tuple[Point2D, ...]
    route_edge_ids: tuple[Identifier, ...]
    start_minute: int = Field(strict=True, ge=0)
    end_minute: int = Field(strict=True, gt=0)
    active: bool = Field(strict=True)
    start_event_id: Identifier
    end_event_id: Identifier | None = None
    program_id: Identifier
    effect_id: Identifier
    source_ids: tuple[Identifier, ...]

    @model_validator(mode="after")
    def room_shape_is_consistent(self) -> _TemporaryRoomOverlay:
        if self.end_minute <= self.start_minute:
            raise ValueError("temporary room end must follow its start")
        if self.sleeping_capacity > self.people_capacity:
            raise ValueError("temporary room sleeping capacity exceeds people capacity")
        if self.sleeping_capacity != len(self.bed_points_m):
            raise ValueError("temporary room bed points must match sleeping capacity")
        if self.active == (self.end_event_id is not None):
            raise ValueError("temporary room active status and end event disagree")
        if len(self.route_edge_ids) != len(set(self.route_edge_ids)):
            raise ValueError("temporary room route edges must be unique")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("temporary room sources must be unique")
        return self


class _TemporaryBedReservation(ExtensionModel):
    agent_id: Identifier
    room_id: Identifier
    bed_index: int = Field(strict=True, ge=0)
    target_id: Identifier
    reserved_minute: int = Field(strict=True, ge=0)


class _TemporaryRoomAdmission(ExtensionModel):
    agent_id: Identifier
    room_id: Identifier
    admitted_minute: int = Field(strict=True, ge=0)
    event_id: Identifier
    source_ids: tuple[Identifier, ...] = ()


class _GeometryHistory(ExtensionModel):
    transition_id: Identifier
    op: Literal[
        "move_facility",
        "transfer_equipment",
        "set_partition",
        "set_route_access",
        "temporary_room_geometry",
        "install_facility",
    ]
    target_id: Identifier
    status: Literal["applied", "expired"]
    causation_id: Identifier | None
    event_id: Identifier
    source_ids: tuple[Identifier, ...]
    minute: int = Field(strict=True, ge=0)


class _GeometryOverlayState(ExtensionModel):
    schema_version: int = Field(strict=True, ge=1, le=1)
    policy_version: Literal[GEOMETRY_OVERLAY_POLICY]
    facility_points: dict[Identifier, _FacilityPointOverride]
    partitions: dict[Identifier, _PartitionOverride]
    route_modifiers: dict[Identifier, _RouteOverlay]
    history: tuple[_GeometryHistory, ...] = Field(max_length=20000)
    temporary_rooms: dict[Identifier, _TemporaryRoomOverlay] = Field(default_factory=dict)
    # Optional when reading older Checkpoints; newly created overlays include it.
    bed_reservations: dict[Identifier, _TemporaryBedReservation] = Field(default_factory=dict)
    # Room assignments are separate from bed use and were added in schema v3.
    room_admissions: dict[Identifier, _TemporaryRoomAdmission] = Field(default_factory=dict)


def create_geometry_overlay_state(world: Any) -> dict[str, Any]:
    if "facility_geometry" not in world.model_configs:
        raise ValueError("geometry_overlay requires facility_geometry")
    return {
        "schema_version": 1,
        "policy_version": GEOMETRY_OVERLAY_POLICY,
        "facility_points": {},
        "partitions": {},
        "route_modifiers": {},
        "history": [],
        "temporary_rooms": {},
        "bed_reservations": {},
        "room_admissions": {},
    }


def reserve_sleep_bed(world: Any, agent_id: str) -> dict[str, Any] | None:
    """Reserve a free bed in the Agent's current zone and return a measured route."""
    from sheltersim.domain.movement import (
        MOVEMENT_STATE_KEY,
        in_room_travel_minutes,
        plan_in_room_route,
    )

    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    agent = world.agents.get(agent_id)
    movement = world.model_state.get(MOVEMENT_STATE_KEY, {})
    overlay_config = world.model_configs.get("geometry_overlay", {})
    if (
        not isinstance(state, dict)
        or agent is None
        or not isinstance(movement, dict)
        or not isinstance(overlay_config, dict)
        or overlay_config.get("schema_version") not in {2, 3, 4}
    ):
        return None
    reservations = state.setdefault("bed_reservations", {})
    geometry = effective_geometry(world)
    if geometry is None:
        return None
    raw_position = movement.get("positions", {}).get(agent_id, {})
    raw_start = raw_position.get("point") if isinstance(raw_position, dict) else None
    start = Point2D.model_validate(raw_start) if isinstance(raw_start, dict) else None
    points_by_cell: dict[tuple[int, int], list[tuple[str, Point2D]]] = {}
    for other_id, other in world.agents.items():
        if other_id == agent_id or other.zone_id != agent.zone_id:
            continue
        other_record = movement.get("positions", {}).get(other_id)
        other_raw_point = other_record.get("point") if isinstance(other_record, dict) else None
        if not isinstance(other_raw_point, dict):
            continue
        other_point = Point2D.model_validate(other_raw_point)
        cell = (math.floor(other_point.x / 0.75), math.floor(other_point.y / 0.75))
        points_by_cell.setdefault(cell, []).append((other_id, other_point))
    occupied = {
        (row["room_id"], row["bed_index"])
        for row in reservations.values()
        if isinstance(row, dict)
    }
    existing = reservations.get(agent_id)
    candidates = []
    route_attempts = 0
    for room_id, room in sorted(state.get("temporary_rooms", {}).items()):
        if route_attempts >= MAX_BED_ROUTE_CANDIDATES:
            break
        if room.get("zone_id") != agent.zone_id:
            continue
        if existing is None and not room.get("active"):
            continue
        if existing is not None and room_id != existing.get("room_id"):
            continue
        for index, raw_bed in enumerate(room.get("bed_points_m", [])):
            if route_attempts >= MAX_BED_ROUTE_CANDIDATES:
                break
            if existing is None and (room_id, index) in occupied:
                continue
            if existing is not None and index != existing.get("bed_index"):
                continue
            bed = Point2D.model_validate(raw_bed)
            cell = (math.floor(bed.x / 0.75), math.floor(bed.y / 0.75))
            neighboring_positions = (
                point
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for _, point in points_by_cell.get((cell[0] + dx, cell[1] + dy), ())
            )
            another_person_is_using_bed = any(
                (point.x - bed.x) ** 2 + (point.y - bed.y) ** 2 <= 0.75**2
                for point in neighboring_positions
            )
            if another_person_is_using_bed:
                continue
            route_attempts += 1
            path = plan_in_room_route(
                world,
                zone_id=room["zone_id"],
                start=start,
                destination=bed,
                mobility=agent.profile.mobility,
            )
            if path is None:
                continue
            distance = sum(
                ((second.x - first.x) ** 2 + (second.y - first.y) ** 2) ** 0.5
                for first, second in zip(path, path[1:])
            )
            travel_minutes = (
                0
                if distance <= 1e-8
                else in_room_travel_minutes(world, agent_id, path)
            )
            candidates.append((travel_minutes, room_id, index, bed, path))
            if route_attempts >= MAX_BED_ROUTE_CANDIDATES:
                break
    if not candidates:
        return None
    travel_minutes, room_id, bed_index, bed, path = min(
        candidates, key=lambda item: (item[0], item[1], item[2])
    )
    target_id = _bed_target_id(room_id, bed_index)
    if existing is None:
        reservations[agent_id] = {
            "agent_id": agent_id,
            "room_id": room_id,
            "bed_index": bed_index,
            "target_id": target_id,
            "reserved_minute": world.minute_index,
        }
    return {
        "agent_id": agent_id,
        "room_id": room_id,
        "bed_index": bed_index,
        "target_id": target_id,
        "reserved_minute": reservations[agent_id]["reserved_minute"],
        "point": bed,
        "route_points": path,
        "travel_minutes": travel_minutes,
    }


def release_sleep_bed(world: Any, agent_id: str) -> bool:
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    reservations = state.get("bed_reservations") if isinstance(state, dict) else None
    return isinstance(reservations, dict) and reservations.pop(agent_id, None) is not None


def available_temporary_room(world: Any, zone_id: str) -> str | None:
    """Return the first active physical room with an available admission slot."""
    config = world.model_configs.get("geometry_overlay", {})
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    if (
        not isinstance(config, dict)
        or config.get("schema_version") not in {3, 4}
        or not isinstance(state, dict)
    ):
        return None
    counts: dict[str, int] = {}
    admissions = state.get("room_admissions", {})
    if isinstance(admissions, dict):
        for row in admissions.values():
            if isinstance(row, dict) and isinstance(row.get("room_id"), str):
                counts[row["room_id"]] = counts.get(row["room_id"], 0) + 1
    for room_id, room in sorted(state.get("temporary_rooms", {}).items()):
        if (
            room.get("active") is True
            and room.get("zone_id") == zone_id
            and counts.get(room_id, 0) < room.get("people_capacity", 0)
            and room_admission_point(world, room_id) is not None
        ):
            return room_id
    return None


def admit_to_temporary_room(
    world: Any,
    *,
    agent_id: str,
    room_id: str,
    minute: int,
    event_id: str,
) -> Point2D:
    """Persist a physical room assignment and return a clear in-room arrival point."""
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    room = state.get("temporary_rooms", {}).get(room_id) if isinstance(state, dict) else None
    admissions = state.get("room_admissions") if isinstance(state, dict) else None
    point = room_admission_point(world, room_id)
    if (
        not isinstance(admissions, dict)
        or room is None
        or room.get("active") is not True
        or agent_id not in world.agents
        or point is None
        or minute < 0
    ):
        raise ValueError("temporary room admission is no longer available")
    admissions[agent_id] = {
        "agent_id": agent_id,
        "room_id": room_id,
        "admitted_minute": minute,
        "event_id": event_id,
        "source_ids": sorted({TEMPORARY_ROOM_INTAKE_SOURCE_ID, *room["source_ids"]}),
    }
    return point


def release_temporary_room_admission(world: Any, agent_id: str) -> bool:
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    admissions = state.get("room_admissions") if isinstance(state, dict) else None
    return isinstance(admissions, dict) and admissions.pop(agent_id, None) is not None


def room_admission_point(world: Any, room_id: str) -> Point2D | None:
    """Find a deterministic walkable point in a room, avoiding assigned residents."""
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    room = state.get("temporary_rooms", {}).get(room_id) if isinstance(state, dict) else None
    if not isinstance(room, dict):
        return None
    polygon = tuple(Point2D.model_validate(point) for point in room.get("polygon_m", ()))
    if len(polygon) < 3:
        return None
    from sheltersim.contracts.extensions import point_in_polygon
    from sheltersim.domain.movement import MOVEMENT_STATE_KEY, plan_in_room_route

    min_x, max_x = min(point.x for point in polygon), max(point.x for point in polygon)
    min_y, max_y = min(point.y for point in polygon), max(point.y for point in polygon)
    admissions = state.get("room_admissions", {})
    occupied_points = []
    movement = world.model_state.get(MOVEMENT_STATE_KEY, {})
    positions = movement.get("positions", {}) if isinstance(movement, dict) else {}
    for agent_id, admission in admissions.items() if isinstance(admissions, dict) else ():
        if not isinstance(admission, dict) or admission.get("room_id") != room_id:
            continue
        record = positions.get(agent_id, {}) if isinstance(positions, dict) else {}
        raw_point = record.get("point") if isinstance(record, dict) else None
        if isinstance(raw_point, dict):
            occupied_points.append(Point2D.model_validate(raw_point))
    candidates = [Point2D.model_validate(point) for point in room.get("bed_points_m", ())]
    step = 0.75
    x = min_x + step / 2.0
    inspected = 0
    while x < max_x and inspected < MAX_ROOM_ADMISSION_POINTS:
        y = min_y + step / 2.0
        while y < max_y and inspected < MAX_ROOM_ADMISSION_POINTS:
            candidate = Point2D(x=x, y=y)
            if point_in_polygon(candidate, polygon):
                candidates.append(candidate)
            inspected += 1
            y += step
        x += step
    unique = {
        (round(point.x, 8), round(point.y, 8)): point for point in candidates
    }
    center_x = sum(point.x for point in polygon) / len(polygon)
    center_y = sum(point.y for point in polygon) / len(polygon)
    ordered = sorted(
        unique.values(),
        key=lambda point: (
            (point.x - center_x) ** 2 + (point.y - center_y) ** 2,
            point.x,
            point.y,
        ),
    )
    zone_id = room.get("zone_id")
    for candidate in ordered[:MAX_ROOM_ADMISSION_POINTS]:
        if any(
            (candidate.x - occupied.x) ** 2 + (candidate.y - occupied.y) ** 2 < 0.5**2
            for occupied in occupied_points
        ):
            continue
        if plan_in_room_route(
            world,
            zone_id=zone_id,
            start=candidate,
            destination=candidate,
        ) is not None:
            return candidate
    return None


def _bed_target_id(room_id: str, bed_index: int) -> str:
    import hashlib

    digest = hashlib.sha256(f"{room_id}:{bed_index}".encode()).hexdigest()[:24]
    return f"bed-{digest}"


def effective_geometry(world: Any) -> FacilityGeometry | None:
    raw = world.model_configs.get("facility_geometry")
    if raw is None:
        return None
    geometry = _parsed_config("facility_geometry", raw, FacilityGeometry)
    overrides = world.model_state.get(GEOMETRY_OVERLAY_KEY, {}).get("facility_points", {})
    if not overrides:
        return geometry
    points = {**geometry.facility_points, **{key: Point2D.model_validate(row["point_m"]) for key, row in overrides.items()}}
    shifted_service_points = []
    for service_point in geometry.service_points:
        override = overrides.get(service_point.facility_id)
        if override is None:
            shifted_service_points.append(service_point)
            continue
        old = geometry.facility_points[service_point.facility_id]
        new = Point2D.model_validate(override["point_m"])
        shifted_service_points.append(service_point.model_copy(update={
            "point_m": Point2D(x=service_point.point_m.x + new.x - old.x,
                                y=service_point.point_m.y + new.y - old.y),
        }))
    return geometry.model_copy(update={
        "facility_points": points, "service_points": tuple(shifted_service_points),
    })


def installed_facility_placement_enabled(world: Any) -> bool:
    """Whether this Scenario places installed facilities on its floor plan."""
    config = world.model_configs.get("geometry_overlay", {})
    return (
        isinstance(config, dict)
        and config.get("schema_version") == 4
        and isinstance(world.model_state.get(GEOMETRY_OVERLAY_KEY), dict)
    )


def installed_facility_point(world: Any, facility_id: str) -> Point2D | None:
    """Choose a free generator-grid slot in the facility's zone, or None if none fits.

    Existing facilities keep their points. The grid of the zone's facilities plus
    this one is searched in slot order, then grown, for a walkable, reachable
    point at least the narrowest queue spacing away from every other facility.
    """
    from sheltersim.domain.movement import _point_walkable, plan_in_room_route
    from sheltersim.region.facility import facility_grid_point

    facility = world.facilities.get(facility_id)
    geometry = effective_geometry(world)
    if facility is None or geometry is None:
        return None
    zone = next((row for row in geometry.zones if row.zone_id == facility.zone_id), None)
    if zone is None:
        return None
    others = [
        geometry.facility_points[key]
        for key, item in sorted(world.facilities.items())
        if key != facility_id and item.zone_id == facility.zone_id
        and key in geometry.facility_points
    ]
    constraints = effective_spatial_constraints(world)
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY, {})
    blocked = (
        *(item.polygon for item in constraints.obstacles),
        *(item.polygon for item in constraints.equipment_footprints),
        *(
            tuple(Point2D.model_validate(point) for point in room["polygon_m"])
            for room in state.get("temporary_rooms", {}).values()
            if room.get("active")
        ),
    )
    for count in range(len(others) + 1, len(others) + 1 + MAX_INSTALLED_FACILITY_GRID_GROWTH):
        for index in range(count):
            point = facility_grid_point(zone.polygon, index, count)
            if (
                _point_walkable(point, zone.polygon, blocked)
                and all(
                    math.hypot(point.x - other.x, point.y - other.y)
                    >= INSTALLED_FACILITY_MIN_SEPARATION_M
                    for other in others
                )
                and plan_in_room_route(
                    world, zone_id=facility.zone_id, start=None, destination=point
                ) is not None
            ):
                return point
    return None


def place_installed_facility(
    world: Any, facility_id: str, *, event_id: str, source_ids: tuple[str, ...]
) -> Point2D:
    """Record the floor-plan point of a facility installed during the Run."""
    point = installed_facility_point(world, facility_id)
    if point is None:
        raise ValueError(f"no free floor-plan point for installed facility {facility_id!r}")
    state = world.model_state[GEOMETRY_OVERLAY_KEY]
    common = {
        "event_id": event_id,
        "source_ids": sorted({*source_ids, INSTALLED_FACILITY_PLACEMENT_SOURCE_ID}),
        "minute": world.minute_index,
    }
    state["facility_points"][facility_id] = {"point_m": point.model_dump(mode="json"), **common}
    import hashlib

    digest = hashlib.sha256(f"{event_id}:{facility_id}".encode()).hexdigest()[:24]
    transition_id = f"install-{digest}"
    state["history"].append({"transition_id": transition_id, "op": "install_facility",
        "target_id": facility_id, "status": "applied", "causation_id": event_id, **common})
    del state["history"][:-20000]
    return point


def effective_spatial_constraints(world: Any) -> SpatialConstraints:
    raw = world.model_configs.get("spatial_constraints", _EMPTY_SPATIAL_CONSTRAINTS)
    constraints = _parsed_config("spatial_constraints", raw, SpatialConstraints)
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY, {})
    obstacles = (*constraints.obstacles, *(SpatialObstacle(id=key,
        polygon=tuple(Point2D.model_validate(p) for p in row["polygon_m"]),
        source_ids=tuple(row["source_ids"])) for key, row in sorted(state.get("partitions", {}).items())))
    # Runtime same-zone moves translate the footprint with its service point.
    original = world.model_configs.get("facility_geometry", {}).get("facility_points", {})
    footprints = []
    for item in constraints.equipment_footprints:
        override = state.get("facility_points", {}).get(item.facility_id)
        if override is None:
            footprints.append(item)
            continue
        old = Point2D.model_validate(original[item.facility_id])
        new = Point2D.model_validate(override["point_m"])
        footprints.append(item.model_copy(update={"polygon": tuple(Point2D(x=p.x+new.x-old.x,
            y=p.y+new.y-old.y) for p in item.polygon)}))
    return constraints.model_copy(update={"obstacles": obstacles, "equipment_footprints": tuple(footprints)})


def validate_geometry_overlay_state(world: Any) -> None:
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    if state is None:
        return
    try:
        contract = _GeometryOverlayState.model_validate(state)
    except (TypeError, ValueError) as exc:
        raise ValueError("geometry_overlay_v1 has an invalid schema") from exc
    nested_route_modifiers = sum(len(route.modifiers) for route in contract.route_modifiers.values())
    if (any(len(getattr(contract, name)) > 256 for name in ("facility_points", "partitions", "route_modifiers", "temporary_rooms"))
            or nested_route_modifiers > 256):
        raise ValueError("geometry_overlay_v1 exceeds its bounded override limit")
    if len(contract.bed_reservations) > 10_000:
        raise ValueError("geometry_overlay_v1 exceeds its bounded bed reservation limit")
    if len(contract.room_admissions) > 10_000:
        raise ValueError("geometry_overlay_v1 exceeds its bounded room admission limit")
    if (len({row.transition_id for row in contract.history}) != len(contract.history)
            or any(row.minute > world.minute_index for row in contract.history)
            or any(a.minute > b.minute for a, b in zip(contract.history, contract.history[1:]))):
        raise ValueError("geometry_overlay_v1 history identity/order/time mismatch")
    geometry = effective_geometry(world)
    if geometry is None:
        raise ValueError("geometry_overlay requires surveyed geometry")
    zones = {row.zone_id: row for row in geometry.zones}
    room_polygons: dict[str, tuple[Point2D, ...]] = {}
    for room_id, room in contract.temporary_rooms.items():
        if room.room_id != room_id or room.zone_id not in zones:
            raise ValueError("temporary room references an unknown room or zone")
        if room.start_minute > world.minute_index:
            raise ValueError("temporary room starts in the future")
        if room.active and world.minute_index > room.end_minute:
            raise ValueError("temporary room remained active past its expiry")
        _validate_room_polygon(zones[room.zone_id].polygon, room.polygon_m)
        room_polygons[room_id] = room.polygon_m
        if any(not point_in_polygon(point, room.polygon_m) for point in room.bed_points_m):
            raise ValueError("temporary room bed point lies outside its polygon")
        if any(edge_id not in world.zone_edges for edge_id in room.route_edge_ids):
            raise ValueError("temporary room references an unknown access route")
        if room.active:
            routes = {route.edge_id: route for route in geometry.routes}
            incoming = any(
                world.zone_edges[edge_id].to_zone_id == room.zone_id
                and not world.zone_edges[edge_id].closed
                and routes.get(edge_id) is not None
                and routes[edge_id].accessible
                for edge_id in room.route_edge_ids
            )
            outgoing = any(
                world.zone_edges[edge_id].from_zone_id == room.zone_id
                and not world.zone_edges[edge_id].closed
                and routes.get(edge_id) is not None
                and routes[edge_id].accessible
                for edge_id in room.route_edge_ids
            )
            if not incoming or not outgoing:
                raise ValueError("active temporary room lacks a directed ingress or egress route")
    active_rooms = [
        (room_id, room)
        for room_id, room in contract.temporary_rooms.items()
        if room.active
    ]
    for index, (left_id, left_room) in enumerate(active_rooms):
        for right_id, right_room in active_rooms[index + 1 :]:
            if (
                left_room.zone_id == right_room.zone_id
                and _polygons_overlap(room_polygons[left_id], room_polygons[right_id])
            ):
                raise ValueError("active temporary room polygons overlap")
        if any(
            point_in_polygon(point, left_room.polygon_m)
            for point in geometry.facility_points.values()
        ):
            raise ValueError("active temporary room covers a facility")
    for key, row in contract.facility_points.items():
        if key not in world.facilities or row.minute > world.minute_index:
            raise ValueError("geometry overlay references an unknown facility or invalid record")
        point = row.point_m
        if not point_in_polygon(point, zones[world.facilities[key].zone_id].polygon):
            raise ValueError("runtime facility point leaves its original zone")
    for service_point in geometry.service_points:
        facility = world.facilities.get(service_point.facility_id)
        if facility is None or not point_in_polygon(
            service_point.point_m, zones[facility.zone_id].polygon
        ):
            raise ValueError("runtime service point leaves its facility's physical zone")
    for row in contract.partitions.values():
        if row.zone_id not in zones or row.minute > world.minute_index:
            raise ValueError("invalid runtime partition record")
        _validate_partition_polygon(zones[row.zone_id].polygon, row.polygon_m)
    for key, row in contract.route_modifiers.items():
        if key not in world.zone_edges or not row.modifiers:
            raise ValueError("invalid runtime route-access record")
        for record in row.modifiers.values():
            if record.minute > world.minute_index:
                raise ValueError("invalid runtime route modifier")
        if world.zone_edges[key].closed != (row.base_closed or any(not r.accessible for r in row.modifiers.values())):
            raise ValueError("route geometry overlay and graph closure disagree")
    constraints = effective_spatial_constraints(world)
    for footprint in constraints.equipment_footprints:
        zone = zones[world.facilities[footprint.facility_id].zone_id]
        if any(not point_in_polygon(p, zone.polygon) for p in footprint.polygon):
            raise ValueError("runtime equipment footprint leaves its original zone")
        if any(
            room.active
            and room.zone_id == world.facilities[footprint.facility_id].zone_id
            and _polygons_overlap(room.polygon_m, footprint.polygon)
            for room in contract.temporary_rooms.values()
        ):
            raise ValueError("active temporary room overlaps an equipment footprint")
    reserved_beds: set[tuple[str, int]] = set()
    for agent_id, reservation in contract.bed_reservations.items():
        room = contract.temporary_rooms.get(reservation.room_id)
        bed_key = (reservation.room_id, reservation.bed_index)
        if (
            reservation.agent_id != agent_id
            or agent_id not in world.agents
            or room is None
            or reservation.bed_index >= room.sleeping_capacity
            or reservation.target_id != _bed_target_id(reservation.room_id, reservation.bed_index)
            or reservation.reserved_minute > world.minute_index
            or bed_key in reserved_beds
        ):
            raise ValueError("temporary room bed reservation references an invalid or duplicate bed")
        reserved_beds.add(bed_key)
    room_admission_counts: dict[str, int] = {}
    for agent_id, admission in contract.room_admissions.items():
        room = contract.temporary_rooms.get(admission.room_id)
        if (
            admission.agent_id != agent_id
            or agent_id not in world.agents
            or room is None
            or admission.admitted_minute > world.minute_index
        ):
            raise ValueError("temporary room admission references an invalid resident or room")
        room_admission_counts[admission.room_id] = (
            room_admission_counts.get(admission.room_id, 0) + 1
        )
    if any(
        count > contract.temporary_rooms[room_id].people_capacity
        for room_id, count in room_admission_counts.items()
    ):
        raise ValueError("temporary room admissions exceed physical room capacity")


def _validate_partition_polygon(zone: tuple[Point2D, ...], polygon: tuple[Point2D, ...]) -> None:
    from sheltersim.domain.movement import _segment_inside_polygon
    from sheltersim.region.facility import _valid_simple_polygon
    if not _valid_simple_polygon(polygon) or any(not point_in_polygon(p, zone) for p in polygon):
        raise ValueError("area_invalid: partition requires a simple positive-area polygon inside its zone")
    if any(not _segment_inside_polygon(a, b, zone) for a, b in zip(polygon, (*polygon[1:], polygon[0]))):
        raise ValueError("area_invalid: partition leaves a concave zone")


def _validate_room_polygon(
    zone: tuple[Point2D, ...], polygon: tuple[Point2D, ...]
) -> None:
    from sheltersim.region.facility import _valid_simple_polygon

    if not _valid_simple_polygon(polygon):
        raise ValueError("area_invalid: temporary room polygon must be simple and nondegenerate")
    if any(not point_in_polygon(point, zone) for point in polygon):
        raise ValueError("area_invalid: temporary room polygon must lie inside its zone")
    from sheltersim.domain.movement import _segment_inside_polygon

    if any(
        not _segment_inside_polygon(first, second, zone)
        for first, second in zip(polygon, (*polygon[1:], polygon[0]))
    ):
        raise ValueError("area_invalid: temporary room polygon crosses the zone boundary")


def _polygons_overlap(
    left: tuple[Point2D, ...], right: tuple[Point2D, ...]
) -> bool:
    from sheltersim.region.facility import _segment_intersection_points

    if any(point_in_polygon(point, right) for point in left) or any(
        point_in_polygon(point, left) for point in right
    ):
        return True
    return any(
        _segment_intersection_points(a, b, c, d)
        for a, b in zip(left, (*left[1:], left[0]))
        for c, d in zip(right, (*right[1:], right[0]))
    )


def _validate_facility_room_clearance(
    world: Any,
    facility_id: str,
    zone_id: str,
    point: Point2D,
    geometry: FacilityGeometry,
    constraints: SpatialConstraints,
) -> None:
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY, {})
    rooms = state.get("temporary_rooms", {})
    old_point = geometry.facility_points[facility_id]
    dx, dy = point.x - old_point.x, point.y - old_point.y
    for room in rooms.values():
        if not room.get("active") or room.get("zone_id") != zone_id:
            continue
        polygon = tuple(Point2D.model_validate(item) for item in room["polygon_m"])
        if point_in_polygon(point, polygon):
            raise ValueError("facility_in_use: facility move would enter an active temporary room")
        if any(
            service_point.facility_id == facility_id
            and point_in_polygon(
                Point2D(
                    x=service_point.point_m.x + dx,
                    y=service_point.point_m.y + dy,
                ),
                polygon,
            )
            for service_point in geometry.service_points
        ):
            raise ValueError("facility_in_use: moved service point would enter an active temporary room")
        footprint = next(
            (item for item in constraints.equipment_footprints if item.facility_id == facility_id),
            None,
        )
        if footprint is not None:
            shifted = tuple(Point2D(x=item.x + dx, y=item.y + dy) for item in footprint.polygon)
            if _polygons_overlap(shifted, polygon):
                raise ValueError("facility_in_use: moved equipment footprint would enter an active temporary room")


def validate_geometry_change(world: Any, op: str, target: str, parameters: dict[str, Any], end_minute: int | None) -> None:
    if GEOMETRY_OVERLAY_KEY not in world.model_state:
        raise ValueError("geometry_overlay_required: declare the versioned geometry overlay before approving effects")
    geometry = effective_geometry(world)
    if geometry is None:
        raise ValueError("geometry_unavailable")
    from sheltersim.domain.movement import _segment_clear_of_polygon
    constraints = effective_spatial_constraints(world)
    if op == "set_route_access":
        data = RouteAccessParameters.model_validate(parameters)
        if target not in world.zone_edges:
            raise ValueError("target_missing: route does not exist")
        if end_minute is None:
            raise ValueError("expiry_required: route-access changes require a bounded end")
        # The approved geometry event is dispatched before the movement phase. The
        # controller atomically suppresses active completions, stops evacuee journeys,
        # and reassigns staff against the refreshed graph in that same simulation tick.
        if data.accessible and world.zone_edges[target].closed:
            raise ValueError("unsupported_reopening: this operation cannot override a prior graph closure")
        if not data.accessible:
            rooms = world.model_state.get(GEOMETRY_OVERLAY_KEY, {}).get(
                "temporary_rooms", {}
            )
            for room in rooms.values():
                if not room.get("active") or target not in room.get("route_edge_ids", ()):
                    continue
                room_zone = room.get("zone_id")
                remaining_incoming = any(
                    edge_id != target
                    and edge_id in world.zone_edges
                    and not world.zone_edges[edge_id].closed
                    and world.zone_edges[edge_id].to_zone_id == room_zone
                    and any(route.edge_id == edge_id and route.accessible for route in geometry.routes)
                    for edge_id in room.get("route_edge_ids", ())
                )
                remaining_outgoing = any(
                    edge_id != target
                    and edge_id in world.zone_edges
                    and not world.zone_edges[edge_id].closed
                    and world.zone_edges[edge_id].from_zone_id == room_zone
                    and any(route.edge_id == edge_id and route.accessible for route in geometry.routes)
                    for edge_id in room.get("route_edge_ids", ())
                )
                if not remaining_incoming or not remaining_outgoing:
                    raise ValueError("route_in_use: closure would disconnect an active temporary room")
    elif op == "transfer_equipment":
        data = EquipmentTransferParameters.model_validate(parameters)
        facility = world.facilities.get(target)
        if facility is None or target not in geometry.facility_points:
            raise ValueError("target_missing: equipment must identify a positioned facility")
        if data.equipment_id != target:
            raise ValueError("target_mismatch: equipment_id must match the selected facility")
        if data.from_zone_id != facility.zone_id or data.to_zone_id not in world.zones:
            raise ValueError("precondition_failed: equipment source/destination zone does not match")
        if end_minute is not None:
            raise ValueError("unsupported_transfer_expiry: equipment transfers are permanent")
        if any((facility.queue_agent_ids, facility.active_agent_ids,
                facility.resource_queue_agent_ids, facility.resource_active_agent_ids)):
            raise ValueError("facility_in_use: equipment queues and services must be empty")
        if any(
            row.get("target_id") == target
            for row in world.model_state.get("staff_operations_v1", {}).get("staff", {}).values()
            if isinstance(row, dict)
        ):
            raise ValueError("facility_in_use: an active staff task targets this equipment")
        source_zone = next((row for row in geometry.zones if row.zone_id == data.from_zone_id), None)
        destination_zone = next((row for row in geometry.zones if row.zone_id == data.to_zone_id), None)
        if source_zone is None or destination_zone is None:
            raise ValueError("target_missing: equipment zone has no surveyed polygon")
        if not point_in_polygon(data.point_m, destination_zone.polygon):
            raise ValueError("area_invalid: transfer point must lie inside the destination zone")
        _validate_facility_room_clearance(
            world, target, data.to_zone_id, data.point_m, geometry, constraints
        )
        if not _zones_are_connected(world, data.from_zone_id, data.to_zone_id):
            raise ValueError("route_unavailable: equipment zones have no open directed route")
        from sheltersim.domain.movement import _point_segment_distance, plan_in_room_route

        if any(
            _point_segment_distance(data.point_m, wall.start, wall.end) < 0.5
            for wall in constraints.walls
        ):
            raise ValueError("clear_width_insufficient: transferred equipment must remain half a metre from walls")
        if plan_in_room_route(
            world, zone_id=data.to_zone_id, start=data.point_m, destination=data.point_m
        ) is None:
            raise ValueError("clear_width_insufficient: transfer point is not walkable")
        old_point = geometry.facility_points[target]
        dx, dy = data.point_m.x - old_point.x, data.point_m.y - old_point.y
        for service_point in geometry.service_points:
            if service_point.facility_id != target:
                continue
            shifted = Point2D(
                x=service_point.point_m.x + dx,
                y=service_point.point_m.y + dy,
            )
            if not point_in_polygon(shifted, destination_zone.polygon):
                raise ValueError("area_invalid: transferred service point leaves its destination zone")
            if any(
                _point_segment_distance(shifted, wall.start, wall.end) < 0.5
                for wall in constraints.walls
            ) or plan_in_room_route(
                world, zone_id=data.to_zone_id, start=shifted, destination=shifted
            ) is None:
                raise ValueError("clear_width_insufficient: transferred service point is not walkable")
        footprint = next(
            (item for item in constraints.equipment_footprints if item.facility_id == target),
            None,
        )
        if footprint is not None:
            shifted_polygon = tuple(Point2D(x=point.x + dx, y=point.y + dy) for point in footprint.polygon)
            if any(not point_in_polygon(point, destination_zone.polygon) for point in shifted_polygon):
                raise ValueError("area_invalid: transferred equipment footprint leaves its destination zone")
            from sheltersim.region.facility import (
                _segment_distance_to_polygon,
                _segment_intersection_points,
            )

            if any(
                _segment_intersection_points(a, b, wall.start, wall.end)
                for a, b in zip(shifted_polygon, (*shifted_polygon[1:], shifted_polygon[0]))
                for wall in constraints.walls
            ):
                raise ValueError("clear_width_insufficient: transferred equipment footprint crosses a wall")
            blocked = [
                item.polygon
                for item in (*constraints.obstacles, *constraints.equipment_footprints)
                if item.facility_id != target
            ]
            if any(
                point_in_polygon(point, shape) or point_in_polygon(shape[0], shifted_polygon)
                for shape in blocked for point in shifted_polygon
            ) or any(
                _segment_distance_to_polygon(a, b, shape) < 1e-8
                for shape in blocked
                for a, b in zip(shifted_polygon, (*shifted_polygon[1:], shifted_polygon[0]))
            ):
                raise ValueError("area_invalid: transferred equipment overlaps another obstacle")
    elif op == "move_facility":
        point = MoveFacilityParameters.model_validate(parameters).point_m
        facility = world.facilities.get(target)
        if facility is None or target not in geometry.facility_points:
            raise ValueError("target_missing: facility has no surveyed position")
        if end_minute is not None:
            raise ValueError("unsupported_move_expiry: same-zone moves are permanent; approve an explicit later move")
        if any((facility.queue_agent_ids, facility.active_agent_ids, facility.resource_queue_agent_ids, facility.resource_active_agent_ids)):
            raise ValueError("facility_in_use: queues and services must be empty")
        if any(row.get("target_id") == target for row in world.model_state.get("staff_operations_v1", {}).get("staff", {}).values() if isinstance(row, dict)):
            raise ValueError("facility_in_use: an active staff task targets this facility")
        zone = next(z for z in geometry.zones if z.zone_id == facility.zone_id)
        if not point_in_polygon(point, zone.polygon):
            raise ValueError("area_invalid: cross-zone facility moves are unsupported")
        _validate_facility_room_clearance(
            world, target, facility.zone_id, point, geometry, constraints
        )
        from sheltersim.domain.movement import _point_segment_distance
        if any(_point_segment_distance(point, wall.start, wall.end) < 0.5 for wall in constraints.walls):
            raise ValueError("clear_width_insufficient: moved service point requires half a metre from walls")
        old_point = geometry.facility_points[target]
        for service_point in geometry.service_points:
            if service_point.facility_id != target:
                continue
            shifted = Point2D(x=service_point.point_m.x + point.x - old_point.x,
                              y=service_point.point_m.y + point.y - old_point.y)
            if not point_in_polygon(shifted, zone.polygon):
                raise ValueError("area_invalid: moved facility would move a service point outside its zone")
            if any(_point_segment_distance(shifted, wall.start, wall.end) < 0.5 for wall in constraints.walls):
                raise ValueError("clear_width_insufficient: moved service point is too close to a wall")
            from sheltersim.domain.movement import plan_in_room_route
            if plan_in_room_route(world, zone_id=facility.zone_id, start=shifted, destination=shifted) is None:
                raise ValueError("clear_width_insufficient: moved service point is not walkable")
        # Reuse the exact room/wall/obstacle planner for the moved service point.
        from sheltersim.domain.movement import plan_in_room_route
        if plan_in_room_route(world, zone_id=facility.zone_id, start=point, destination=point) is None:
            raise ValueError("clear_width_insufficient: moved service point is not walkable")
        existing = next((r for r in constraints.equipment_footprints if r.facility_id == target), None)
        if existing is not None:
            old = geometry.facility_points[target]
            shifted = tuple(Point2D(x=p.x+point.x-old.x, y=p.y+point.y-old.y) for p in existing.polygon)
            if any(not point_in_polygon(p, zone.polygon) for p in shifted):
                raise ValueError("area_invalid: moved footprint leaves its zone")
            from sheltersim.region.facility import (
                _segment_distance_to_polygon,
                _segment_intersection_points,
            )
            if any(_segment_intersection_points(a, b, wall.start, wall.end)
                   for a, b in zip(shifted, (*shifted[1:], shifted[0])) for wall in constraints.walls):
                raise ValueError("clear_width_insufficient: moved equipment crosses a wall")
            blocked = [r.polygon for r in constraints.obstacles] + [r.polygon for r in constraints.equipment_footprints if r.facility_id != target]
            if any(point_in_polygon(p, shape) or point_in_polygon(shape[0], shifted) for shape in blocked for p in shifted):
                raise ValueError("area_invalid: moved footprint overlaps existing obstacles or equipment")
            if any(_segment_distance_to_polygon(a, b, shape) < 1e-8 for a, b in zip(shifted, (*shifted[1:], shifted[0])) for shape in blocked):
                raise ValueError("clear_width_insufficient: moved equipment intersects an obstacle")
    elif op == "set_partition":
        data = PartitionParameters.model_validate(parameters)
        zone = next((z for z in geometry.zones if z.zone_id == target), None)
        if zone is None:
            raise ValueError("target_missing: zone does not exist")
        if end_minute is None:
            raise ValueError("expiry_required: partitions require an explicit bounded end")
        _validate_partition_polygon(zone.polygon, data.polygon_m)
        if any(point_in_polygon(point, data.polygon_m) for point in geometry.facility_points.values()):
            raise ValueError("area_invalid: partition covers a facility")
        from sheltersim.region.facility import _segment_distance_to_polygon
        if any(point_in_polygon(p, shape) or point_in_polygon(shape[0], data.polygon_m)
               or any(_segment_distance_to_polygon(a, b, shape) < 1e-8
                   for a, b in zip(data.polygon_m, (*data.polygon_m[1:], data.polygon_m[0])))
               for shape in (item.polygon for item in (*constraints.obstacles, *constraints.equipment_footprints))
               for p in data.polygon_m):
            raise ValueError("area_invalid: partition overlaps equipment or another obstacle")
        positions = world.model_state.get("movement_v1", {}).get("positions", {})
        if any(row.get("point") and point_in_polygon(Point2D.model_validate(row["point"]), data.polygon_m) for row in positions.values()):
            raise ValueError("route_in_use: partition covers a current person")
        for staff in world.staff.values():
            staff_zone = next((z for z in geometry.zones if z.zone_id == staff.zone_id), None)
            if staff_zone is not None:
                from sheltersim.domain.movement import _zone_center
                centre = _zone_center(geometry, staff.zone_id)
                if centre is not None and point_in_polygon(centre, data.polygon_m):
                    raise ValueError("route_in_use: partition covers a staff standing point")
        # Reject all intersections with declared transit paths; do not leave staff
        # graph-only planners believing an unsafe narrowed passage is available.
        if any(not _segment_clear_of_polygon(a, b, data.polygon_m, route.width_m / 2)
               for route in geometry.routes for a, b in zip(route.points, route.points[1:])):
            raise ValueError("clear_width_insufficient: partition obstructs a declared passage")
        for journey in world.model_state.get("movement_v1", {}).get("journeys", {}).values():
            points = tuple(Point2D.model_validate(p) for p in journey.get("route_points", ()))
            if any(not _segment_clear_of_polygon(a, b, data.polygon_m, 0.5) for a, b in zip(points, points[1:])):
                if journey.get("purpose") not in {
                    "toilet_queue_position", "resource_queue_position",
                    "toilet_queue_service", "resource_queue_service",
                }:
                    raise ValueError("route_in_use: partition intersects current room travel")
    elif op == "temporary_room_geometry":
        data = TemporaryRoomGeometry.model_validate(parameters)
        if data.zone_id != target:
            raise ValueError("target_mismatch: temporary room zone must match its selected zone")
        if end_minute is None or end_minute >= world.duration_minutes:
            raise ValueError("expiry_required: temporary rooms need an in-run expiry")
        zone = next((row for row in geometry.zones if row.zone_id == target), None)
        if zone is None:
            raise ValueError("target_missing: temporary room zone has no surveyed polygon")
        _validate_room_polygon(zone.polygon, data.polygon_m)
        for edge_id in data.route_edge_ids:
            edge = world.zone_edges.get(edge_id)
            route = next((row for row in geometry.routes if row.edge_id == edge_id), None)
            if edge is None or route is None or target not in {
                edge.from_zone_id,
                edge.to_zone_id,
            }:
                raise ValueError("route_unavailable: temporary room access route must connect to its zone")
            if edge.closed or not route.accessible:
                raise ValueError("route_unavailable: temporary room access route is closed or inaccessible")
        incoming = any(world.zone_edges[item].to_zone_id == target for item in data.route_edge_ids)
        outgoing = any(world.zone_edges[item].from_zone_id == target for item in data.route_edge_ids)
        if not incoming or not outgoing:
            raise ValueError("route_unavailable: temporary room requires directed ingress and egress routes")
        overlay_state = world.model_state.get(GEOMETRY_OVERLAY_KEY, {})
        existing_rooms = overlay_state.get("temporary_rooms", {})
        if data.room_id in existing_rooms:
            raise ValueError("room_exists: temporary room ID already exists")
        for row in existing_rooms.values():
            if row.get("active") and row.get("zone_id") == target and _polygons_overlap(
                data.polygon_m, tuple(Point2D.model_validate(p) for p in row["polygon_m"])
            ):
                raise ValueError("area_invalid: temporary room overlaps another active room")
        if any(point_in_polygon(point, data.polygon_m) for point in geometry.facility_points.values()):
            raise ValueError("area_invalid: temporary room covers a facility")
        reservations = overlay_state.get("bed_reservations", {})
        existing_rooms_by_id = overlay_state.get("temporary_rooms", {})
        for reservation in reservations.values():
            existing_room = existing_rooms_by_id.get(reservation.get("room_id"), {})
            index = reservation.get("bed_index")
            bed_points = existing_room.get("bed_points_m", [])
            if (
                type(index) is int
                and 0 <= index < len(bed_points)
                and point_in_polygon(
                    Point2D.model_validate(bed_points[index]), data.polygon_m
                )
            ):
                raise ValueError("room_in_use: temporary room would cover a reserved sleep bed")
        if any(
            _polygons_overlap(data.polygon_m, item.polygon)
            for item in (*constraints.obstacles, *constraints.equipment_footprints)
        ):
            raise ValueError("area_invalid: temporary room overlaps an obstacle or equipment footprint")
        from sheltersim.region.facility import _segment_intersection_points

        if any(
            _segment_intersection_points(first, second, wall.start, wall.end)
            for first, second in zip(data.polygon_m, (*data.polygon_m[1:], data.polygon_m[0]))
            for wall in constraints.walls
        ):
            raise ValueError("area_invalid: temporary room boundary crosses a declared wall")
        movement = world.model_state.get("movement_v1", {})
        if any(
            row.get("point")
            and point_in_polygon(Point2D.model_validate(row["point"]), data.polygon_m)
            for row in movement.get("positions", {}).values()
        ):
            raise ValueError("room_in_use: temporary room would cover a current person")
        if any(
            row.get("point")
            and point_in_polygon(Point2D.model_validate(row["point"]), data.polygon_m)
            for row in movement.get("staff_positions", {}).values()
        ):
            raise ValueError("room_in_use: temporary room would cover a current staff member")
        from sheltersim.domain.movement import _zone_center, plan_in_room_route

        entry_points = []
        for edge_id in data.route_edge_ids:
            edge = world.zone_edges[edge_id]
            route = next(row for row in geometry.routes if row.edge_id == edge_id)
            entry_points.append(
                route.points[-1] if edge.to_zone_id == target else route.points[0]
            )
        center = _zone_center(geometry, target)
        access_points = [*entry_points, *([center] if center is not None else [])]
        if data.bed_points_m and not all(
            any(
                plan_in_room_route(
                    world, zone_id=target, start=entry, destination=bed
                ) is not None
                for entry in access_points
            )
            for bed in data.bed_points_m
        ):
            raise ValueError("route_unavailable: every bed must be reachable from a room access point")
    else:
        raise ValueError("unsupported_geometry_operator")


def apply_geometry_change(world: Any, event: Any, *, expire: bool = False) -> None:
    payload = event.payload
    op, target, modifier = payload["op"], payload["target_id"], payload["modifier_id"]
    state = world.model_state.get(GEOMETRY_OVERLAY_KEY)
    if state is None:
        raise ValueError("geometry_overlay_required")
    if not expire:
        validate_geometry_change(world, op, target, payload["parameters"], payload["end_minute"])
    marker = event.provenance.get("effect_program_v1", {})
    parameter_sources = payload.get("parameters", {}).get("source_ids", ())
    assumption_sources = marker.get("assumption_ids", ()) if isinstance(marker, Mapping) else ()
    source_ids = {
        source_id
        for source_id in (event.source_id, *parameter_sources, *assumption_sources)
        if isinstance(source_id, str) and source_id
    }
    common = {
        "event_id": event.event_id,
        "source_ids": sorted(source_ids),
        "minute": world.minute_index,
    }
    if op in {"move_facility", "transfer_equipment"}:
        if op == "transfer_equipment":
            transfer = EquipmentTransferParameters.model_validate(payload["parameters"])
            world.facilities[target].zone_id = transfer.to_zone_id
            point = transfer.point_m
        else:
            point = MoveFacilityParameters.model_validate(payload["parameters"]).point_m
        state["facility_points"][target] = {"point_m": point.model_dump(mode="json"), **common}
    elif op == "set_partition":
        if expire:
            state["partitions"].pop(modifier, None)
        else:
            partition = PartitionParameters.model_validate(payload["parameters"])
            polygon = partition.model_dump(mode="json")["polygon_m"]
            state["partitions"][modifier] = {
                "zone_id": target,
                "polygon_m": polygon,
                **common,
                "end_minute": payload["end_minute"],
            }
    elif op == "set_route_access":
        record = state["route_modifiers"].setdefault(target, {"base_closed": world.zone_edges[target].closed, "modifiers": {}})
        if expire:
            record["modifiers"].pop(modifier, None)
        else:
            record["modifiers"][modifier] = {"accessible": payload["parameters"]["accessible"],
                **common, "end_minute": payload["end_minute"]}
        world.zone_edges[target].closed = record["base_closed"] or any(not r["accessible"] for r in record["modifiers"].values())
        if not record["modifiers"]:
            del state["route_modifiers"][target]
    elif op == "temporary_room_geometry":
        parameters = TemporaryRoomGeometry.model_validate(payload["parameters"])
        rooms = state.setdefault("temporary_rooms", {})
        if expire:
            record = rooms[parameters.room_id]
            record["active"] = False
            record["end_event_id"] = event.event_id
        else:
            rooms[parameters.room_id] = {
                **parameters.model_dump(mode="json"),
                "start_minute": world.minute_index,
                "end_minute": payload["end_minute"],
                "active": True,
                "start_event_id": event.event_id,
                "end_event_id": None,
                "program_id": payload["program_id"],
                "effect_id": payload["effect_id"],
                "source_ids": common["source_ids"],
            }
    else:
        raise ValueError("unsupported_geometry_operator")
    history_target = (
        TemporaryRoomGeometry.model_validate(payload["parameters"]).room_id
        if op == "temporary_room_geometry"
        else target
    )
    state["history"].append({"transition_id": event.event_id, "op": op, "target_id": history_target,
        "status": "expired" if expire else "applied", "causation_id": event.causation_id, **common})
    del state["history"][:-20000]


def _zones_are_connected(world: Any, source_zone_id: str, destination_zone_id: str) -> bool:
    """Require an existing directed, currently open route for transferred equipment."""
    if source_zone_id == destination_zone_id:
        return False
    edges: dict[str, list[str]] = {}
    for edge in world.zone_edges.values():
        if not edge.closed:
            edges.setdefault(edge.from_zone_id, []).append(edge.to_zone_id)
    pending = [source_zone_id]
    visited = {source_zone_id}
    while pending:
        zone_id = pending.pop(0)
        for next_zone in sorted(edges.get(zone_id, ())):
            if next_zone == destination_zone_id:
                return True
            if next_zone not in visited:
                visited.add(next_zone)
                pending.append(next_zone)
    return False
