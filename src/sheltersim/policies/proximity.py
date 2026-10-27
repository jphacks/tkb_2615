"""Allowlisted physical proximity samplers; these record proximity, never speech."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.domain.social_network import (
    PROXIMITY_CONTACT_POLICY_VERSION,
    PROXIMITY_DISABLED_POLICY_VERSION,
    canonical_proximity_contact_config,
)

DISABLED_PROXIMITY_POLICY_ID = PROXIMITY_DISABLED_POLICY_VERSION
DISTANCE_PROXIMITY_POLICY_ID = PROXIMITY_CONTACT_POLICY_VERSION


@dataclass(frozen=True)
class ProximityPair:
    distance_m: float
    first_agent_id: str
    second_agent_id: str
    zone_id: str
    first_point: dict[str, float]
    second_point: dict[str, float]


@dataclass(frozen=True)
class DisabledProximityPolicy:
    model_id: str = DISABLED_PROXIMITY_POLICY_ID

    def sample(self, world: WorldState) -> list[ProximityPair]:
        del world
        return []


@dataclass(frozen=True)
class DistanceSampledProximityPolicy:
    """Deterministic same-zone Euclidean sampling with a bounded pair count."""

    config: dict[str, Any]
    model_id: str = DISTANCE_PROXIMITY_POLICY_ID

    def sample(self, world: WorldState) -> list[ProximityPair]:
        movement = world.model_state.get(MOVEMENT_STATE_KEY)
        positions = movement.get("positions", {}) if isinstance(movement, dict) else {}
        radius = self.config["max_distance_m"]
        cells: dict[tuple[str, int, int], list[str]] = {}
        points: dict[str, tuple[str, dict[str, float]]] = {}
        for agent_id, position in sorted(positions.items()):
            if (
                agent_id not in world.agents
                or not isinstance(position, dict)
                or position.get("position_kind") != "physical"
                or position.get("zone_id") not in world.zones
            ):
                continue
            point = position.get("point")
            if (
                not isinstance(point, dict)
                or set(point) != {"x", "y"}
                or any(
                    isinstance(point[key], bool)
                    or not isinstance(point[key], (int, float))
                    or not math.isfinite(float(point[key]))
                    for key in ("x", "y")
                )
            ):
                continue
            zone_id = position["zone_id"]
            normalized = {"x": float(point["x"]), "y": float(point["y"])}
            points[agent_id] = (zone_id, normalized)
            cell = (
                zone_id,
                math.floor(normalized["x"] / radius),
                math.floor(normalized["y"] / radius),
            )
            cells.setdefault(cell, []).append(agent_id)

        candidates: list[ProximityPair] = []
        visited: set[tuple[str, str]] = set()
        for agent_id in sorted(points):
            zone_id, first_point = points[agent_id]
            cell_x = math.floor(first_point["x"] / radius)
            cell_y = math.floor(first_point["y"] / radius)
            for offset_x in (-1, 0, 1):
                for offset_y in (-1, 0, 1):
                    for other_id in cells.get((zone_id, cell_x + offset_x, cell_y + offset_y), ()):
                        if other_id == agent_id:
                            continue
                        first_id, second_id = sorted((agent_id, other_id))
                        pair = (first_id, second_id)
                        if pair in visited:
                            continue
                        visited.add(pair)
                        second_zone, second_point = points[second_id]
                        if zone_id != second_zone:
                            continue
                        distance = math.hypot(
                            second_point["x"] - points[first_id][1]["x"],
                            second_point["y"] - points[first_id][1]["y"],
                        )
                        if distance <= radius:
                            candidates.append(
                                ProximityPair(
                                    distance,
                                    first_id,
                                    second_id,
                                    zone_id,
                                    points[first_id][1],
                                    second_point,
                                )
                            )
        candidates.sort(key=lambda row: (row.distance_m, row.first_agent_id, row.second_agent_id))
        return candidates[: self.config["max_pairs_per_sample"]]


def create_proximity_policy(
    model_id: str, configs: dict[str, Any]
) -> DisabledProximityPolicy | DistanceSampledProximityPolicy:
    if model_id == DISABLED_PROXIMITY_POLICY_ID:
        return DisabledProximityPolicy()
    if model_id != DISTANCE_PROXIMITY_POLICY_ID:
        raise ValueError("unsupported installed social_proximity model")
    raw = configs.get("social_proximity") or {
        "schema_version": 1,
        "enabled": True,
        "policy_version": PROXIMITY_CONTACT_POLICY_VERSION,
        "sample_interval_minutes": 15,
        "max_distance_m": 1.5,
        "max_pairs_per_sample": 500,
    }
    config = canonical_proximity_contact_config(raw)
    if config is None:
        raise ValueError("selected distance proximity policy requires an enabled config")
    return DistanceSampledProximityPolicy(config)
