"""Versioned synthetic map geometry kept separate from simulation inputs."""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

StableLayoutId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Point = tuple[float, float]


class _LayoutContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LayoutZone(_LayoutContract):
    zone_id: StableLayoutId
    label: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]
    polygon: tuple[Point, ...] = Field(min_length=3, max_length=64)
    modelled: bool = True


class LayoutFacility(_LayoutContract):
    facility_id: StableLayoutId
    zone_id: StableLayoutId
    display_zone_id: StableLayoutId
    point: Point


class LayoutRouteEdge(_LayoutContract):
    edge_id: StableLayoutId
    from_zone_id: StableLayoutId
    to_zone_id: StableLayoutId


class LayoutSpec(_LayoutContract):
    """Display geometry plus IDs that bind it to a scenario's zone graph."""

    schema_version: int = Field(ge=1, le=1)
    id: StableLayoutId
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    source: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    note: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)
    zones: tuple[LayoutZone, ...] = Field(min_length=1, max_length=128)
    facilities: tuple[LayoutFacility, ...] = Field(max_length=256)
    route_edges: tuple[LayoutRouteEdge, ...] = Field(max_length=512)

    @model_validator(mode="after")
    def validate_geometry_and_ids(self) -> LayoutSpec:
        zone_by_id = {zone.zone_id: zone for zone in self.zones}
        if len(zone_by_id) != len(self.zones):
            raise ValueError("layout zone IDs must be unique")
        edge_ids = [edge.edge_id for edge in self.route_edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("layout route edge IDs must be unique")
        facility_ids = [facility.facility_id for facility in self.facilities]
        if len(facility_ids) != len(set(facility_ids)):
            raise ValueError("layout facility IDs must be unique")
        for zone in self.zones:
            for x, y in zone.polygon:
                if not math.isfinite(x) or not math.isfinite(y):
                    raise ValueError(f"layout zone {zone.zone_id!r} has a non-finite point")
                if not (0 <= x <= self.width and 0 <= y <= self.height):
                    raise ValueError(f"layout zone {zone.zone_id!r} is outside the canvas")
            if len(set(zone.polygon)) < 3:
                raise ValueError(f"layout zone {zone.zone_id!r} needs three distinct points")
        for edge in self.route_edges:
            if edge.from_zone_id not in zone_by_id or edge.to_zone_id not in zone_by_id:
                raise ValueError(f"layout route edge {edge.edge_id!r} references an unknown zone")
            if edge.from_zone_id == edge.to_zone_id:
                raise ValueError(f"layout route edge {edge.edge_id!r} is a self-loop")
        for facility in self.facilities:
            zone = zone_by_id.get(facility.zone_id)
            if zone is None:
                raise ValueError(f"layout facility {facility.facility_id!r} has an unknown zone")
            x, y = facility.point
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"layout facility {facility.facility_id!r} has a non-finite point")
            if not (0 <= x <= self.width and 0 <= y <= self.height):
                raise ValueError(f"layout facility {facility.facility_id!r} is outside the canvas")
            display_zone = zone_by_id.get(facility.display_zone_id)
            if display_zone is None or not _point_in_polygon(x, y, display_zone.polygon):
                raise ValueError(
                    f"layout facility {facility.facility_id!r} is outside display zone "
                    f"{facility.display_zone_id!r}"
                )
        return self

    def validate_scenario(self, scenario: Any) -> None:
        """Require an exact modeled zone/edge mapping and positioned facilities."""
        modeled_zone_ids = {zone.zone_id for zone in self.zones if zone.modelled}
        scenario_zone_ids = {zone.id for zone in scenario.zones}
        if modeled_zone_ids != scenario_zone_ids:
            raise ValueError("layout modeled zones must map one-to-one to the scenario zones")
        layout_edges = {
            edge.edge_id: (edge.from_zone_id, edge.to_zone_id) for edge in self.route_edges
        }
        scenario_edges = {
            edge.id: (edge.from_zone_id, edge.to_zone_id) for edge in scenario.zone_edges
        }
        if layout_edges != scenario_edges:
            raise ValueError("layout route edges must map one-to-one to the scenario zone graph")
        layout_facilities = {item.facility_id: item.zone_id for item in self.facilities}
        for facility in scenario.facilities:
            if layout_facilities.get(facility.id) != facility.zone_id:
                raise ValueError(f"layout facility {facility.id!r} must match its scenario zone")

    @classmethod
    def from_mapping(cls, document: dict[str, Any]) -> LayoutSpec:
        """Parse a JSON object at an external fixture or API boundary."""
        return cls.model_validate(document)


def _point_in_polygon(x: float, y: float, polygon: tuple[Point, ...]) -> bool:
    inside = False
    previous_x, previous_y = polygon[-1]
    for current_x, current_y in polygon:
        if (current_y > y) != (previous_y > y):
            crossing_x = (previous_x - current_x) * (y - current_y) / (
                previous_y - current_y
            ) + current_x
            if x < crossing_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside
