"""Injectable deterministic route and travel-time policies."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Mapping, Protocol

from sheltersim.domain.zones import ZoneGraph


@dataclass(frozen=True)
class ZoneRoute:
    """One path through the directed zone graph and its declared duration."""

    origin_zone_id: str
    destination_zone_id: str
    zone_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    travel_minutes: int


class SpacePolicy(Protocol):
    """Route contract that can be replaced without modifying simulation core."""

    def route(
        self,
        graph: ZoneGraph,
        origin_zone_id: str,
        destination_zone_id: str,
        agent_id: str | None = None,
        edge_loads: Mapping[str, int] | None = None,
    ) -> ZoneRoute | None:
        """Return a route, or ``None`` when the destination is unreachable."""


@dataclass(frozen=True)
class ShortestTravelTimePolicy:
    """Choose the least-time route with stable edge-ID tie-breaking."""

    def route(
        self,
        graph: ZoneGraph,
        origin_zone_id: str,
        destination_zone_id: str,
        agent_id: str | None = None,
        edge_loads: Mapping[str, int] | None = None,
    ) -> ZoneRoute | None:
        del agent_id  # Available to alternative policies; shortest path is agent-neutral.
        if origin_zone_id not in graph.zones:
            raise ValueError(f"unknown origin zone {origin_zone_id!r}")
        if destination_zone_id not in graph.zones:
            raise ValueError(f"unknown destination zone {destination_zone_id!r}")
        if origin_zone_id == destination_zone_id:
            return ZoneRoute(
                origin_zone_id=origin_zone_id,
                destination_zone_id=destination_zone_id,
                zone_ids=(origin_zone_id,),
                edge_ids=(),
                travel_minutes=0,
            )

        loads = {} if edge_loads is None else dict(edge_loads)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in loads.values()
        ):
            raise ValueError("edge loads must be non-negative integers")
        adjacency: dict[str, list[tuple[str, str, int, int | None]]] = {
            zone_id: [] for zone_id in graph.zones
        }
        for edge in graph.edges:
            if not edge.closed:
                adjacency[edge.from_zone_id].append(
                    (
                        edge.to_zone_id,
                        edge.id,
                        edge.travel_minutes,
                        edge.max_agents_in_transit,
                    )
                )
        for outgoing in adjacency.values():
            outgoing.sort(key=lambda item: (item[1], item[0], item[2]))

        # The complete path tuple is part of the priority so equal-time choices
        # are independent of dict insertion order and runtime hash randomization.
        start_score = (0, (), (origin_zone_id,))
        best: dict[str, tuple[int, tuple[str, ...], tuple[str, ...]]] = {
            origin_zone_id: start_score
        }
        pending: list[tuple[int, tuple[str, ...], tuple[str, ...], str]] = [
            (*start_score, origin_zone_id)
        ]
        while pending:
            minutes, edge_ids, zone_ids, current_zone = heapq.heappop(pending)
            score = (minutes, edge_ids, zone_ids)
            if best.get(current_zone) != score:
                continue
            if current_zone == destination_zone_id:
                return ZoneRoute(
                    origin_zone_id=origin_zone_id,
                    destination_zone_id=destination_zone_id,
                    zone_ids=zone_ids,
                    edge_ids=edge_ids,
                    travel_minutes=minutes,
                )
            for next_zone, edge_id, edge_minutes, capacity in adjacency[current_zone]:
                if capacity is not None and loads.get(edge_id, 0) >= capacity:
                    continue
                candidate = (
                    minutes + edge_minutes,
                    (*edge_ids, edge_id),
                    (*zone_ids, next_zone),
                )
                if next_zone not in best or candidate < best[next_zone]:
                    best[next_zone] = candidate
                    heapq.heappush(pending, (*candidate, next_zone))
        return None


class SpaceRouter:
    """Small application boundary that accepts a replaceable SpacePolicy."""

    def __init__(
        self,
        graph: ZoneGraph,
        policy: SpacePolicy | None = None,
    ) -> None:
        self.graph = graph
        self.policy = policy if policy is not None else ShortestTravelTimePolicy()

    def route(
        self,
        origin_zone_id: str,
        destination_zone_id: str,
        agent_id: str | None = None,
        edge_loads: Mapping[str, int] | None = None,
    ) -> ZoneRoute | None:
        """Ask the injected policy for a route through this graph snapshot."""
        if edge_loads is None:
            return self.policy.route(self.graph, origin_zone_id, destination_zone_id, agent_id)
        return self.policy.route(
            self.graph,
            origin_zone_id,
            destination_zone_id,
            agent_id,
            edge_loads=edge_loads,
        )

    def travel_minutes(
        self,
        origin_zone_id: str,
        destination_zone_id: str,
        agent_id: str | None = None,
    ) -> int | None:
        """Return the sum of declared edge times along the selected route."""
        route = self.route(origin_zone_id, destination_zone_id, agent_id)
        return None if route is None else route.travel_minutes
