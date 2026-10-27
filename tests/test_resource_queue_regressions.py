"""Generated Scenarios with entered food and water stock run without invariant failures."""

from __future__ import annotations

import json
import math

import pytest

from sheltersim.contracts.extensions import Point2D
from sheltersim.contracts.spatial import SpatialConstraints
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.movement import (
    _geometry,
    _point_walkable,
    _spatial_constraints,
    _zone_center,
    plan_in_room_route,
)
from sheltersim.domain.queueing import _drop_stale_resource_stall
from sheltersim.domain.resource_service import SERVICE_STATE_KEY
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _stocked(evacuees: int = 10, duration_days: float = 1) -> ScenarioSpec:
    draft = InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality", disaster="earthquake",
        evacuees=evacuees, duration_days=duration_days, seed=7,
        operational_conditions={"initial_resources": {
            "status": "entered", "water_ml": 9_000 * evacuees,
            "food_meal_units": 9 * evacuees, "source_kind": "assumption",
            "source_ids": ["ASM-TEST-ENTERED-STOCK"],
        }},
    ))
    assert not draft.approval_blockers
    return draft.scenario


def _world(scenario: ScenarioSpec) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


def _meal_station(world: WorldState):
    return next(item for item in world.facilities.values() if item.kind == "meal_distribution")


def test_stall_is_kept_while_its_agent_heads_the_queue() -> None:
    world = _world(_stocked())
    facility = _meal_station(world)
    first, second = sorted(world.agents)[:2]
    facility.resource_queue_agent_ids[:] = [first, second]
    stalls = world.model_state[SERVICE_STATE_KEY]["stall_state"]
    stalls[facility.id] = {"agent_id": first, "reason": "staff"}

    _drop_stale_resource_stall(world, facility.id)

    assert stalls[facility.id] == {"agent_id": first, "reason": "staff"}


@pytest.mark.parametrize("remaining", ["reordered", "left", "empty"])
def test_stall_is_dropped_once_its_agent_no_longer_heads_the_queue(remaining: str) -> None:
    # Need-weighted reordering or a stalled head leaving used to leave the stall
    # pointing at another Agent, and the next state validation stopped the Run.
    world = _world(_stocked())
    facility = _meal_station(world)
    first, second = sorted(world.agents)[:2]
    facility.resource_queue_agent_ids[:] = {
        "reordered": [second, first], "left": [second], "empty": [],
    }[remaining]
    state = world.model_state[SERVICE_STATE_KEY]
    state["stall_state"][facility.id] = {"agent_id": first, "reason": "staff"}
    history = list(state["queue_stall_history"])

    _drop_stale_resource_stall(world, facility.id)

    assert facility.id not in state["stall_state"]
    assert state["queue_stall_history"] == history


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("travel_scheduled", "in_room_route_available"), ("rejected", "room_route_blocked")],
)
def test_in_room_request_outcomes_pass_state_validation(outcome: str, reason: str) -> None:
    world = _world(_stocked())
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    agent_id = sorted(world.agents)[0]
    world.model_state[SERVICE_STATE_KEY]["request_history"].append({
        "minute": 0, "agent_id": agent_id, "action": "fetch_water", "outcome": outcome,
        "reason": reason, "facility_id": station.id,
        "origin_zone_id": station.zone_id, "detail": None,
    })
    world.validate()


def test_in_room_reasons_still_require_their_own_outcome() -> None:
    world = _world(_stocked())
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    world.model_state[SERVICE_STATE_KEY]["request_history"].append({
        "minute": 0, "agent_id": sorted(world.agents)[0], "action": "fetch_water",
        "outcome": "queued", "reason": "in_room_route_available", "facility_id": station.id,
        "origin_zone_id": station.zone_id, "detail": None,
    })
    with pytest.raises(StateInvariantError, match="outcome is inconsistent"):
        world.validate()


def test_in_room_route_keeps_a_start_that_rounds_onto_a_door_node() -> None:
    # Waypoints are de-duplicated on coordinates rounded to 1e-8 m. A start within
    # that distance of a door-side waypoint used to be replaced by it and the
    # route search raised StopIteration while queueing at a water station.
    world = _world(_stocked())
    geometry = _geometry(world)
    constraints = SpatialConstraints.model_validate(_spatial_constraints(world))
    blocked = tuple(item.polygon for item in constraints.obstacles) + tuple(
        item.polygon for item in constraints.equipment_footprints
    )
    clearance = 0.25 + 0.05  # independent mobility plus the planner's fixed margin
    checked = 0
    for zone in geometry.zones:
        destination = _zone_center(geometry, zone.zone_id)
        for wall in constraints.walls:
            dx, dy = wall.end.x - wall.start.x, wall.end.y - wall.start.y
            length = math.hypot(dx, dy)
            for opening in (item for item in constraints.openings if item.wall_id == wall.id):
                for side in (-1.0, 1.0):
                    node = Point2D(
                        x=opening.center.x - dy / length * clearance * side,
                        y=opening.center.y + dx / length * clearance * side,
                    )
                    if not _point_walkable(node, zone.polygon, blocked):
                        continue
                    start = Point2D(x=node.x + 1e-12, y=node.y)
                    path = plan_in_room_route(
                        world, zone_id=zone.zone_id, start=start, destination=destination
                    )
                    assert path is not None
                    assert path[0] == start
                    assert path[-1] == destination
                    checked += 1
    assert checked


@pytest.mark.long_simulation
def test_generated_scenario_with_entered_stock_completes_a_day(tmp_path) -> None:
    # Before the fixes this Run stopped at minute 495 (stale stall) and, once that
    # was fixed, at minute 512 (in-room request reason rejected by validation).
    run_scenario(_stocked(evacuees=30), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert manifest["final_minute"] == 1440
