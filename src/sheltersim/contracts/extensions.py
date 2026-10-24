"""Opt-in scenario extensions, leaving legacy scenario/world encodings intact."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class ExtensionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Point2D(ExtensionModel):
    """Meters from the top-left building origin; x right, y down."""

    x: Finite
    y: Finite


class PhysicalZone(ExtensionModel):
    zone_id: Identifier
    polygon: tuple[Point2D, ...] = Field(min_length=3, max_length=128)
    usable_area_m2: Finite = Field(gt=0)
    source_ids: tuple[Identifier, ...] = ()


class PhysicalRoute(ExtensionModel):
    edge_id: Identifier
    points: tuple[Point2D, ...] = Field(min_length=2, max_length=128)
    width_m: Finite = Field(gt=0)
    accessible: bool = True


class ServiceAccessConditions(ExtensionModel):
    """Access facts/requirements for one service point; unknown is never inferred."""

    accessibility: Literal["accessible", "restricted", "unknown"] = "unknown"
    required_access_tags: tuple[Identifier, ...] = Field(default=(), max_length=64)
    required_staff_skills: tuple[Identifier, ...] = Field(default=(), max_length=64)


class FacilityServicePoint(ExtensionModel):
    """A stable physical service location with an independent FIFO reservation ID.

    `service_capacity` counts concurrent services at this point, not people per hour.
    Throughput remains a versioned simulator policy and must not be inferred from it.
    """

    schema_version: Literal[1] = 1
    id: Identifier
    facility_id: Identifier
    point_m: Point2D
    access: ServiceAccessConditions = Field(default_factory=ServiceAccessConditions)
    service_capacity: int = Field(strict=True, ge=0, le=1_000_000)
    queue_id: Identifier


class ArrivalApproachRoute(ExtensionModel):
    """Operator-supplied outdoor path into one physical admission zone."""

    schema_version: Literal[1] = 1
    route_id: Identifier
    destination_zone_id: Identifier
    points_m: tuple[Point2D, ...] = Field(min_length=2, max_length=128)
    walking_speed_m_per_min: Finite = Field(gt=0, le=120)
    source_kind: Literal["measured", "operator", "inferred"]
    source_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def route_has_distance_and_sources(self) -> ArrivalApproachRoute:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("arrival approach route source IDs must be unique")
        if self.distance_m <= 0:
            raise ValueError("arrival approach route must have positive length")
        return self

    @property
    def distance_m(self) -> float:
        return sum(
            math.hypot(right.x - left.x, right.y - left.y)
            for left, right in zip(self.points_m, self.points_m[1:], strict=False)
        )

    @property
    def travel_minutes(self) -> int:
        return max(1, math.ceil(self.distance_m / self.walking_speed_m_per_min))


class FacilityGeometry(ExtensionModel):
    """model_configs.facility_geometry; never infer meters from LayoutSpec pixels."""

    schema_version: Literal[1] = 1
    units: Literal["m"] = "m"
    origin: Literal["top_left"] = "top_left"
    width_m: Finite = Field(gt=0)
    height_m: Finite = Field(gt=0)
    source_kind: Literal["synthetic", "measured", "operator", "inferred"]
    source_ids: tuple[Identifier, ...] = ()
    zones: tuple[PhysicalZone, ...]
    facility_points: dict[Identifier, Point2D] = Field(default_factory=dict)
    service_points: tuple[FacilityServicePoint, ...] = Field(default=(), max_length=10_000)
    routes: tuple[PhysicalRoute, ...] = ()
    arrival_approach_routes: tuple[ArrivalApproachRoute, ...] = Field(
        default=(), max_length=128
    )

    @model_validator(mode="after")
    def geometry_is_bounded(self) -> FacilityGeometry:
        for values in (
            [z.zone_id for z in self.zones],
            [r.edge_id for r in self.routes],
            [r.route_id for r in self.arrival_approach_routes],
        ):
            if len(values) != len(set(values)):
                raise ValueError("geometry IDs must be unique")
        points = [p for z in self.zones for p in z.polygon]
        points += [p for r in self.routes for p in r.points]
        points += [p for r in self.arrival_approach_routes for p in r.points_m]
        points += list(self.facility_points.values())
        points += [service.point_m for service in self.service_points]
        if any(not (0 <= p.x <= self.width_m and 0 <= p.y <= self.height_m) for p in points):
            raise ValueError("physical geometry is outside the building bounds")
        zone_by_id = {zone.zone_id: zone for zone in self.zones}
        for route in self.arrival_approach_routes:
            zone = zone_by_id.get(route.destination_zone_id)
            if zone is None:
                raise ValueError("arrival approach route references an unknown destination zone")
            if not point_in_polygon(route.points_m[-1], zone.polygon):
                raise ValueError("arrival approach route must end inside its destination zone")
            if any(point_in_polygon(route.points_m[0], item.polygon) for item in self.zones):
                raise ValueError("arrival approach route must start outside modeled zones")
        service_ids = [service.id for service in self.service_points]
        queue_ids = [service.queue_id for service in self.service_points]
        if len(service_ids) != len(set(service_ids)):
            raise ValueError("facility service-point IDs must be unique")
        if len(queue_ids) != len(set(queue_ids)):
            raise ValueError("each service point must own a unique queue ID")
        for zone in self.zones:
            coordinates = [(p.x, p.y) for p in zone.polygon]
            area = abs(sum(
                a[0] * b[1] - b[0] * a[1]
                for a, b in zip(coordinates, coordinates[1:] + coordinates[:1])
            )) / 2
            if area <= 0 or zone.usable_area_m2 > area + 1e-8:
                raise ValueError("usable area must not exceed a nondegenerate polygon area")
        return self


class PersonTraits(ExtensionModel):
    """Synthetic demographic traits keyed by ProfileSpec.id, never real identity."""

    age_years: int = Field(ge=0, le=120)
    household_id: Identifier | None = None
    household_role: Literal["adult", "child", "dependent", "alone"] = "adult"
    languages: tuple[Identifier, ...] = ("ja",)
    accessibility_needs: tuple[Identifier, ...] = ()
    medical_support: bool = False
    source_ids: tuple[Identifier, ...] = ()


class PopulationSpec(ExtensionModel):
    """model_configs.population; immutable attributes survive every checkpoint."""

    schema_version: Literal[1] = 1
    people: dict[Identifier, PersonTraits]
    source_ids: tuple[Identifier, ...] = ()


def validate_scenario_extensions(scenario: Any) -> None:
    """Validate extension shapes and authoritative scenario references at input."""
    from sheltersim.contracts.population import validate_population_context
    from sheltersim.contracts.runtime import declared_states
    from sheltersim.contracts.spatial import validate_spatial_constraints
    declared_states(scenario.model_configs)
    validate_spatial_constraints(scenario)
    validate_population_context(scenario)
    configs = scenario.model_configs
    if "facility_geometry" in configs:
        geometry = FacilityGeometry.model_validate(configs["facility_geometry"])
        if {z.zone_id for z in geometry.zones} != {z.id for z in scenario.zones}:
            raise ValueError("physical geometry must map every scenario zone")
        if set(geometry.facility_points) != {f.id for f in scenario.facilities}:
            raise ValueError("physical geometry must position every scenario facility")
        if {r.edge_id for r in geometry.routes} != {e.id for e in scenario.zone_edges}:
            raise ValueError("physical routes must map every scenario edge")
        zone_polygons = {zone.zone_id: zone.polygon for zone in geometry.zones}
        for facility in scenario.facilities:
            if not point_in_polygon(geometry.facility_points[facility.id], zone_polygons[facility.zone_id]):
                raise ValueError("physical facility point must be inside its scenario zone")
        facility_by_id = {facility.id: facility for facility in scenario.facilities}
        for service_point in geometry.service_points:
            facility = facility_by_id.get(service_point.facility_id)
            if facility is None:
                raise ValueError("service point references an unknown facility")
            if not point_in_polygon(service_point.point_m, zone_polygons[facility.zone_id]):
                raise ValueError("service point must be inside its facility's physical zone")
        routes = {route.edge_id: route for route in geometry.routes}
        for edge in scenario.zone_edges:
            route = routes[edge.id]
            if not point_in_polygon(route.points[0], zone_polygons[edge.from_zone_id]) or not point_in_polygon(route.points[-1], zone_polygons[edge.to_zone_id]):
                raise ValueError("physical route endpoints must match the directed zone edge")
            if all(point == route.points[0] for point in route.points):
                raise ValueError("physical route must have a positive length")
    if "population" in configs:
        population = PopulationSpec.model_validate(configs["population"])
        if set(population.people) != {p.id for p in scenario.profiles}:
            raise ValueError("population traits must map every initial profile")


def polygon_area(polygon: tuple[Point2D, ...]) -> float:
    """Unsigned planar area in square metres; callers validate topology separately."""
    return abs(sum(a.x*b.y-b.x*a.y for a, b in zip(polygon, (*polygon[1:], polygon[0]))))/2


def point_in_polygon(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    """Boundary-inclusive containment; no pixel/metre conversions are implied."""
    inside = False
    previous = polygon[-1]
    for current in polygon:
        cross = (point.x-previous.x)*(current.y-previous.y) - (point.y-previous.y)*(current.x-previous.x)
        if abs(cross) < 1e-9 and min(previous.x, current.x)-1e-9 <= point.x <= max(previous.x, current.x)+1e-9 and min(previous.y, current.y)-1e-9 <= point.y <= max(previous.y, current.y)+1e-9:
            return True
        if (current.y > point.y) != (previous.y > point.y):
            crossing = (previous.x-current.x)*(point.y-current.y)/(previous.y-current.y)+current.x
            if point.x < crossing:
                inside = not inside
        previous = current
    return inside
