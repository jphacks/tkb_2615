"""Additive spatial/intake contracts; legacy geometry and hard capacity stay unchanged."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import Field

from sheltersim.contracts.extensions import (
    ExtensionModel,
    FacilityGeometry,
    FacilityServicePoint,
    Finite,
    Identifier,
    Point2D,
    ServiceAccessConditions,
)


class Wall(ExtensionModel):
    id: Identifier
    start: Point2D
    end: Point2D
    source_ids: tuple[Identifier, ...] = ()


class Opening(ExtensionModel):
    id: Identifier
    wall_id: Identifier
    center: Point2D
    width_m: Finite = Field(gt=0)
    edge_ids: tuple[Identifier, ...]
    accessible: bool = True
    source_ids: tuple[Identifier, ...] = ()


class SpatialObstacle(ExtensionModel):
    id: Identifier
    polygon: tuple[Point2D, ...] = Field(min_length=3, max_length=128)
    source_ids: tuple[Identifier, ...] = ()


class EquipmentFootprint(ExtensionModel):
    facility_id: Identifier
    polygon: tuple[Point2D, ...] = Field(min_length=4, max_length=4)
    area_m2: Finite = Field(gt=0)
    source_ids: tuple[Identifier, ...] = ()


class SpatialConstraints(ExtensionModel):
    schema_version: Literal[1] = 1
    walls: tuple[Wall, ...] = ()
    openings: tuple[Opening, ...] = ()
    obstacles: tuple[SpatialObstacle, ...] = ()
    equipment_footprints: tuple[EquipmentFootprint, ...] = ()
    planning_capacity_by_zone: dict[Identifier, int] = Field(default_factory=dict)
    accessible_facility_ids: tuple[Identifier, ...] = ()


def service_points_for_scenario(scenario: Any) -> tuple[FacilityServicePoint, ...]:
    """Resolve explicit v1 service points; preserve the legacy one-queue-per-facility adapter.

    The adapter reuses the same physical target used by legacy movement, retains the
    Facility ID as FIFO queue ID, and marks access as unknown. It does not infer
    measured accessibility, throughput, or a facility-specific service time.
    """
    raw = scenario.model_configs.get("facility_geometry")
    if raw is None:
        return ()
    geometry = FacilityGeometry.model_validate(raw)
    if geometry.service_points:
        return geometry.service_points
    return legacy_service_points(geometry, scenario.facilities)


def legacy_service_points(geometry: FacilityGeometry, facilities: Any) -> tuple[FacilityServicePoint, ...]:
    """Build point records that preserve existing facility-target/FIFO semantics."""
    points = []
    for facility in sorted(facilities, key=lambda row: row.id):
        point = geometry.facility_points.get(facility.id)
        if point is None:
            continue
        capacity = facility.working_units if facility.working_units is not None else facility.capacity
        points.append(FacilityServicePoint(
            id=f"legacy-service:{hashlib.sha256(facility.id.encode()).hexdigest()[:24]}",
            facility_id=facility.id, point_m=point,
            access=ServiceAccessConditions(accessibility="unknown"),
            service_capacity=capacity, queue_id=facility.id,
        ))
    return tuple(points)


def install_legacy_service_points(scenario: Any) -> Any:
    """Return a ScenarioSpec copy with stable service-point IDs for a new preview only."""
    raw = scenario.model_configs.get("facility_geometry")
    if raw is None:
        return scenario
    geometry = FacilityGeometry.model_validate(raw)
    if geometry.service_points:
        return scenario
    points = service_points_for_scenario(scenario)
    geometry = geometry.model_copy(update={"service_points": points})
    configs = {**scenario.model_configs, "facility_geometry": geometry.model_dump(mode="json")}
    return scenario.model_copy(update={"model_configs": configs})


class PopulationTransition(ExtensionModel):
    schema_version: Literal[1] = 1
    transition_id: Identifier
    event_id: Identifier
    agent_id: Identifier
    household_id: Identifier | None = None
    minute: int = Field(ge=0)
    action: Literal["arrived", "waiting", "admitted", "departed", "temporary_exit",
                    "returned", "transferred", "reunited"]
    zone_id: Identifier | None = None
    source_ids: tuple[Identifier, ...] = ()


def validate_spatial_constraints(scenario: Any) -> None:
    raw = scenario.model_configs.get("spatial_constraints")
    if raw is None:
        return
    from sheltersim.contracts.extensions import FacilityGeometry
    spec = SpatialConstraints.model_validate(raw)
    geometry = FacilityGeometry.model_validate(scenario.model_configs["facility_geometry"])
    walls = {wall.id: wall for wall in spec.walls}
    if len(walls) != len(spec.walls) or len({o.id for o in spec.openings}) != len(spec.openings):
        raise ValueError("wall/opening IDs must be unique")
    if len({item.id for item in spec.obstacles}) != len(spec.obstacles):
        raise ValueError("obstacle IDs must be unique")
    if len({item.facility_id for item in spec.equipment_footprints}) != len(spec.equipment_footprints):
        raise ValueError("equipment footprint facility IDs must be unique")
    from sheltersim.contracts.extensions import polygon_area
    for item in (*spec.obstacles, *spec.equipment_footprints):
        if polygon_area(item.polygon) <= 0:
            raise ValueError("obstacle/equipment polygon must have positive area")
        if any(not 0 <= point.x <= geometry.width_m or not 0 <= point.y <= geometry.height_m
               for point in item.polygon):
            raise ValueError("obstacle/equipment polygon lies outside facility bounds")
    if not {item.facility_id for item in spec.equipment_footprints} <= {f.id for f in scenario.facilities}:
        raise ValueError("equipment footprint references an unknown facility")
    for wall in spec.walls:
        if wall.start == wall.end:
            raise ValueError("wall must have positive length")
        for point in (wall.start, wall.end):
            if not 0 <= point.x <= geometry.width_m or not 0 <= point.y <= geometry.height_m:
                raise ValueError("wall lies outside facility bounds")
    for opening in spec.openings:
        if opening.wall_id not in walls or not set(opening.edge_ids) <= {r.edge_id for r in geometry.routes}:
            raise ValueError("opening references missing wall or route")
        wall = walls[opening.wall_id]
        dx, dy = wall.end.x-wall.start.x, wall.end.y-wall.start.y
        length = (dx*dx+dy*dy)**0.5
        px, py = opening.center.x-wall.start.x, opening.center.y-wall.start.y
        distance = (px*dx+py*dy)/length
        if (abs(px*dy-py*dx) > 1e-7
                or distance - opening.width_m / 2 < -1e-9
                or distance + opening.width_m / 2 > length + 1e-9):
            raise ValueError("opening must lie on and fit inside its wall")
    if not set(spec.planning_capacity_by_zone) <= {z.id for z in scenario.zones}:
        raise ValueError("planning capacity references unknown zone")
    if any(type(v) is not int or v < 0 for v in spec.planning_capacity_by_zone.values()):
        raise ValueError("planning capacities must be nonnegative integers")
    if not set(spec.accessible_facility_ids) <= {f.id for f in scenario.facilities}:
        raise ValueError("accessibility references unknown facility")


def occupancy_snapshot(world: Any) -> dict[str, Any]:
    from sheltersim.domain.temporary_spaces import capacity_metrics

    planning = world.model_configs.get("spatial_constraints", {}).get("planning_capacity_by_zone", {})
    counts = {key: 0 for key in world.zones}
    for person in world.agents.values():
        counts[person.zone_id] += 1
    pending = world.model_state.get("population_dynamics_v1", {}).get("pending_arrivals", [])
    zones = []
    for zone_id, zone in sorted(world.zones.items()):
        capacity = capacity_metrics(world, zone_id, occupancy=counts[zone_id])
        planning_capacity = planning.get(zone_id, zone.capacity)
        zones.append({
            "zone_id": zone_id,
            "occupants": counts[zone_id],
            "hard_capacity": zone.capacity,
            "nominal_capacity": capacity["nominal_capacity"],
            "effective_capacity": capacity["effective_capacity"],
            "temporary_people_capacity": capacity["temporary_people_capacity"],
            "retained_expired_capacity": capacity["retained_expired_capacity"],
            "temporary_sleeping_capacity": capacity["temporary_sleeping_capacity"],
            "physical_room_capacity": capacity["physical_room_capacity"],
            "active_room_admissions": capacity["active_room_admissions"],
            "retained_expired_room_admissions": capacity["retained_expired_room_admissions"],
            "effective_total_capacity": capacity["effective_total_capacity"],
            "occupied_above_nominal": (
                max(0, counts[zone_id] - zone.capacity) if zone.capacity is not None else 0
            ),
            "planning_capacity": planning_capacity,
            "planning_excess": (
                max(0, counts[zone_id] - planning_capacity)
                if planning_capacity is not None else None
            ),
            "geometry_status": (
                "physical_room_and_zone_capacity"
                if capacity["physical_room_capacity"]
                else "zone_only_capacity"
                if capacity["temporary_people_capacity"] or capacity["retained_expired_capacity"]
                else "no_temporary_space"
            ),
        })
    return {
        "schema_version": 2,
        "minute": world.minute_index,
        "time_semantics": "before_tick",
        "admitted_count": len(world.agents),
        "waiting_outside_count": len(pending),
        "zones": zones,
    }
