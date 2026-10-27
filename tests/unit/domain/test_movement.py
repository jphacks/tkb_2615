"""Physical queues: the positioned head moves up and is served while others walk.

In-room routes keep their start point too. The day-long runs are checked in
tests/integration/test_physical_queues.py (a queue longer than its room),
tests/integration/test_queue_service.py (one meal station for a hundred people)
and tests/integration/test_entered_stock.py (entered stock).
"""

from __future__ import annotations

import math

from sheltersim.contracts.extensions import Point2D
from sheltersim.contracts.spatial import SpatialConstraints
from sheltersim.core.world import WorldState
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    _geometry,
    _point_walkable,
    _queue_slot_position,
    _spatial_constraints,
    _zone_center,
    agent_is_at_queue_trip_destination,
    plan_in_room_route,
    progress_physical_queue_head_to_service,
    progress_physical_queue_positions,
)
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.simulator_state import initialize_simulator_extensions


def _stock(evacuees: int) -> dict:
    """Water and meals entered by the operator, enough for a day of service."""
    return {
        "initial_resources": {
            "status": "entered",
            "water_ml": 9_000 * evacuees,
            "food_meal_units": 9 * evacuees,
            "source_kind": "assumption",
            "source_ids": ["ASM-TEST-ENTERED-STOCK"],
        }
    }


def _overflowing_water_queue(generated_scenario_with) -> tuple[WorldState, str, list[str]]:
    """Fill every reachable slot in the water room, then add one more person."""
    world = WorldState.from_scenario(
        generated_scenario_with(evacuees=40, operational_conditions=_stock(40))
    )
    initialize_simulator_extensions(world)
    geometry = _geometry(world)
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    center = _zone_center(geometry, station.zone_id)
    positions = world.model_state[MOVEMENT_STATE_KEY]["positions"]
    for agent_id in sorted(world.agents):
        station.resource_queue_agent_ids.append(agent_id)
        world.agents[agent_id].zone_id = station.zone_id
        positions[agent_id].update(
            {
                "zone_id": station.zone_id,
                "point": center.model_dump(mode="json"),
                "edge_id": None,
                "progress": 1.0,
            }
        )
        slot = _queue_slot_position(world, geometry, agent_id, station.id)
        if slot is None:
            break
        positions[agent_id]["point"] = slot.model_dump(mode="json")
    queue = list(station.resource_queue_agent_ids)
    assert 1 < len(queue) < len(world.agents)
    return world, station.id, queue


def test_follower_without_a_slot_does_not_hold_back_the_head(generated_scenario_with) -> None:
    world, facility_id, queue = _overflowing_water_queue(generated_scenario_with)

    assert not progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    # Nobody is sent ahead of the waiting follower and the FIFO order is unchanged.
    assert world.model_state[MOVEMENT_STATE_KEY]["journeys"] == {}
    assert world.facilities[facility_id].resource_queue_agent_ids == queue
    assert world.metric_state["physical_queue_route_blocked_count"] == 1.0


def test_head_without_a_reachable_slot_still_waits(generated_scenario_with) -> None:
    world = WorldState.from_scenario(
        generated_scenario_with(evacuees=10, operational_conditions=_stock(10))
    )
    initialize_simulator_extensions(world)
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    # The head still stands in its sleeping zone, outside the water room.
    station.resource_queue_agent_ids.append(sorted(world.agents)[0])

    assert progress_physical_queue_positions(world, station.id, resource=True, minute=0)


def _positioned_meal_queue(
    generated_scenario_with, length: int
) -> tuple[WorldState, str, list[str]]:
    """Queue `length` people at the meal station, each standing on its slot."""
    world = WorldState.from_scenario(generated_scenario_with(evacuees=30))
    initialize_simulator_extensions(world)
    geometry = _geometry(world)
    manager = ResourceServiceManager(world)
    station = next(item for item in world.facilities.values() if item.kind == "meal_distribution")
    center = _zone_center(geometry, station.zone_id)
    positions = world.model_state[MOVEMENT_STATE_KEY]["positions"]
    for agent_id in sorted(world.agents)[:length]:
        world.agents[agent_id].zone_id = station.zone_id
        positions[agent_id].update(
            {
                "zone_id": station.zone_id,
                "point": center.model_dump(mode="json"),
                "edge_id": None,
                "progress": 1.0,
            }
        )
        manager.request(agent_id, station.id, validate_state=False)
        slot = _queue_slot_position(world, geometry, agent_id, station.id)
        assert slot is not None
        positions[agent_id]["point"] = slot.model_dump(mode="json")
    return world, station.id, list(station.resource_queue_agent_ids)


def _trips(world: WorldState) -> dict[str, str]:
    journeys = world.model_state[MOVEMENT_STATE_KEY]["journeys"]
    return {agent_id: journey["purpose"] for agent_id, journey in journeys.items()}


def _step_off_slot(world: WorldState, agent_id: str) -> None:
    point = world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id]["point"]
    point["x"] = point["x"] + 0.3


def test_a_follower_walking_up_does_not_stop_the_head_from_being_served(
    generated_scenario_with,
) -> None:
    world, facility_id, queue = _positioned_meal_queue(generated_scenario_with, 4)
    _step_off_slot(world, queue[2])

    # The follower is sent back to its slot, and the positioned head stays servable.
    assert not progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    assert _trips(world) == {queue[2]: "resource_queue_position"}
    assert not progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    # The head now walks to the service point while the follower is still moving.
    assert progress_physical_queue_head_to_service(world, facility_id, resource=True, minute=0)
    assert _trips(world) == {
        queue[2]: "resource_queue_position",
        queue[0]: "resource_queue_service",
    }


def test_the_head_still_waits_for_its_own_walk(generated_scenario_with) -> None:
    world, facility_id, queue = _positioned_meal_queue(generated_scenario_with, 3)
    _step_off_slot(world, queue[0])

    assert progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    assert _trips(world) == {queue[0]: "resource_queue_position"}
    assert progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    assert progress_physical_queue_head_to_service(world, facility_id, resource=True, minute=0)
    assert _trips(world) == {queue[0]: "resource_queue_position"}


def test_a_walk_ends_where_it_was_sent_even_if_the_slot_moved(generated_scenario_with) -> None:
    world, _, queue = _positioned_meal_queue(generated_scenario_with, 2)
    point = world.model_state[MOVEMENT_STATE_KEY]["positions"][queue[1]]["point"]
    journey = {"route_points": [{"x": 0.0, "y": 0.0}, dict(point)]}
    assert agent_is_at_queue_trip_destination(world, queue[1], journey)
    elsewhere = Point2D(x=point["x"] + 1.0, y=point["y"])
    journey["route_points"][-1] = elsewhere.model_dump(mode="json")
    assert not agent_is_at_queue_trip_destination(world, queue[1], journey)
    assert not agent_is_at_queue_trip_destination(world, queue[1], {})


def test_in_room_route_keeps_a_start_that_rounds_onto_a_door_node(generated_scenario) -> None:
    # Waypoints are de-duplicated on coordinates rounded to 1e-8 m. A start within
    # that distance of a door-side waypoint used to be replaced by it and the
    # route search raised StopIteration while queueing at a water station.
    world = WorldState.from_scenario(generated_scenario)
    initialize_simulator_extensions(world)
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
