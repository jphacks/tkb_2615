"""Immutable routing and display projection of the mutable zone state."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sheltersim.domain.models import ZoneEdgeState, ZoneState

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@dataclass(frozen=True)
class ZoneNode:
    """A stable zone identity and optional display metadata."""

    id: str
    name: str
    capacity: int | None = None
    display_x: float | None = None
    display_y: float | None = None


@dataclass(frozen=True)
class ZoneConnection:
    """Directed edge with explicit simulation travel duration."""

    id: str
    from_zone_id: str
    to_zone_id: str
    travel_minutes: int
    closed: bool = False
    max_agents_in_transit: int | None = None


@dataclass(frozen=True)
class ZoneGraph:
    """Validated immutable graph snapshot, independent of mutable WorldState."""

    zones: Mapping[str, ZoneNode]
    edges: tuple[ZoneConnection, ...] = ()

    def __post_init__(self) -> None:
        zones = dict(self.zones)
        if not zones:
            raise ValueError("zone graph must contain at least one zone")
        for key, zone in zones.items():
            if key != zone.id or not _STABLE_ID.fullmatch(zone.id):
                raise ValueError("zone graph mapping keys must match stable zone IDs")
            if zone.capacity is not None and zone.capacity < 0:
                raise ValueError(f"zone {zone.id!r} capacity cannot be negative")
            if any(
                coordinate is not None and not math.isfinite(coordinate)
                for coordinate in (zone.display_x, zone.display_y)
            ):
                raise ValueError(f"zone {zone.id!r} has non-finite display coordinates")

        edges = tuple(sorted(self.edges, key=lambda edge: edge.id))
        edge_ids: set[str] = set()
        for edge in edges:
            if not _STABLE_ID.fullmatch(edge.id) or edge.id in edge_ids:
                raise ValueError("zone edge IDs must be stable and unique")
            edge_ids.add(edge.id)
            if edge.from_zone_id not in zones or edge.to_zone_id not in zones:
                raise ValueError(f"zone edge {edge.id!r} references an unknown zone")
            if edge.from_zone_id == edge.to_zone_id:
                raise ValueError(f"zone edge {edge.id!r} must connect different zones")
            if (
                not isinstance(edge.travel_minutes, int)
                or isinstance(edge.travel_minutes, bool)
                or edge.travel_minutes <= 0
            ):
                raise ValueError(f"zone edge {edge.id!r} travel time must be a positive integer")
            if edge.max_agents_in_transit is not None and (
                not isinstance(edge.max_agents_in_transit, int)
                or isinstance(edge.max_agents_in_transit, bool)
                or not 1 <= edge.max_agents_in_transit <= 100
            ):
                raise ValueError(f"zone edge {edge.id!r} transit capacity must be from 1 to 100")
        object.__setattr__(self, "zones", MappingProxyType(dict(sorted(zones.items()))))
        object.__setattr__(self, "edges", edges)

    @classmethod
    def from_states(
        cls,
        zones: Mapping[str, ZoneState],
        edges: Mapping[str, ZoneEdgeState],
        *,
        edge_capacities: Mapping[str, int] | None = None,
    ) -> ZoneGraph:
        """Copy the routing-relevant fields from state into an immutable graph."""
        capacities = {} if edge_capacities is None else dict(edge_capacities)
        if set(capacities) - set(edges):
            raise ValueError("route capacity references an unknown zone edge")
        return cls(
            zones={
                zone_id: ZoneNode(
                    id=zone.id,
                    name=zone.name,
                    capacity=zone.capacity,
                    display_x=zone.display_x,
                    display_y=zone.display_y,
                )
                for zone_id, zone in zones.items()
            },
            edges=tuple(
                ZoneConnection(
                    id=edge.id,
                    from_zone_id=edge.from_zone_id,
                    to_zone_id=edge.to_zone_id,
                    travel_minutes=edge.travel_minutes,
                    closed=edge.closed,
                    max_agents_in_transit=capacities.get(edge.id),
                )
                for edge in edges.values()
            ),
        )
