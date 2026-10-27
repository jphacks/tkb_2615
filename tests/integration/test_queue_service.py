"""Followers moving up a physical queue do not hold back the head's service."""

from __future__ import annotations

import json
import statistics

import pytest

from sheltersim.contracts.extensions import Point2D
from sheltersim.core.world import WorldState
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    _geometry,
    _queue_slot_position,
    _zone_center,
    agent_is_at_queue_trip_destination,
    progress_physical_queue_head_to_service,
    progress_physical_queue_positions,
)
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario


def _generated(evacuees: int):
    return (
        InitialScenarioBuilder()
        .build(
            InitialScenarioRequest(
                municipality="Synthetic municipality",
                disaster="earthquake",
                evacuees=evacuees,
                duration_days=1,
                seed=7,
            )
        )
        .scenario
    )


def _positioned_meal_queue(length: int) -> tuple[WorldState, str, list[str]]:
    """Queue `length` people at the meal station, each standing on its slot."""
    world = WorldState.from_scenario(_generated(30))
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


def test_a_follower_walking_up_does_not_stop_the_head_from_being_served() -> None:
    world, facility_id, queue = _positioned_meal_queue(4)
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


def test_the_head_still_waits_for_its_own_walk() -> None:
    world, facility_id, queue = _positioned_meal_queue(3)
    _step_off_slot(world, queue[0])

    assert progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    assert _trips(world) == {queue[0]: "resource_queue_position"}
    assert progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    assert progress_physical_queue_head_to_service(world, facility_id, resource=True, minute=0)
    assert _trips(world) == {queue[0]: "resource_queue_position"}


def test_a_walk_ends_where_it_was_sent_even_if_the_slot_moved() -> None:
    world, _, queue = _positioned_meal_queue(2)
    point = world.model_state[MOVEMENT_STATE_KEY]["positions"][queue[1]]["point"]
    journey = {"route_points": [{"x": 0.0, "y": 0.0}, dict(point)]}
    assert agent_is_at_queue_trip_destination(world, queue[1], journey)
    elsewhere = Point2D(x=point["x"] + 1.0, y=point["y"])
    journey["route_points"][-1] = elsewhere.model_dump(mode="json")
    assert not agent_is_at_queue_trip_destination(world, queue[1], journey)
    assert not agent_is_at_queue_trip_destination(world, queue[1], {})


@pytest.mark.long_simulation
def test_one_meal_station_keeps_up_with_one_hundred_people(tmp_path) -> None:
    # Before the fix the meal queue grew to 72 people: 1,371 of about 1,500 service
    # attempts were held back by someone else moving up, and 65 meals were served.
    world = run_scenario(_generated(100), tmp_path)
    records = [
        json.loads(line)
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
    ]
    meals = sum(
        record["event"]["event_type"] == "resource_service_complete"
        and record["event"]["payload"]["resource_id"] == "food_meal_units"
        for record in records
    )
    assert meals >= len(world.agents)
    assert not any(record["status"] == "failed" for record in records)
    assert statistics.median(agent.needs.hunger for agent in world.agents.values()) < 0.7
