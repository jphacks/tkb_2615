"""A queue longer than its room still serves the positioned head."""

from __future__ import annotations

import json
import statistics

from sheltersim.core.world import WorldState
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    _geometry,
    _queue_slot_position,
    _zone_center,
    progress_physical_queue_positions,
)
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _stocked(evacuees: int) -> ScenarioSpec:
    return (
        InitialScenarioBuilder()
        .build(
            InitialScenarioRequest(
                municipality="Synthetic municipality",
                disaster="earthquake",
                evacuees=evacuees,
                duration_days=1,
                seed=7,
                operational_conditions={
                    "initial_resources": {
                        "status": "entered",
                        "water_ml": 9_000 * evacuees,
                        "food_meal_units": 9 * evacuees,
                        "source_kind": "assumption",
                        "source_ids": ["ASM-TEST-ENTERED-STOCK"],
                    }
                },
            )
        )
        .scenario
    )


def _overflowing_water_queue() -> tuple[WorldState, str, list[str]]:
    """Fill every reachable slot in the water room, then add one more person."""
    world = WorldState.from_scenario(_stocked(40))
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


def test_follower_without_a_slot_does_not_hold_back_the_head() -> None:
    world, facility_id, queue = _overflowing_water_queue()

    assert not progress_physical_queue_positions(world, facility_id, resource=True, minute=0)
    # Nobody is sent ahead of the waiting follower and the FIFO order is unchanged.
    assert world.model_state[MOVEMENT_STATE_KEY]["journeys"] == {}
    assert world.facilities[facility_id].resource_queue_agent_ids == queue
    assert world.metric_state["physical_queue_route_blocked_count"] == 1.0


def test_head_without_a_reachable_slot_still_waits() -> None:
    world = WorldState.from_scenario(_stocked(10))
    initialize_simulator_extensions(world)
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    # The head still stands in its sleeping zone, outside the water room.
    station.resource_queue_agent_ids.append(sorted(world.agents)[0])

    assert progress_physical_queue_positions(world, station.id, resource=True, minute=0)


def test_staffed_water_station_serves_a_queue_longer_than_its_room(tmp_path) -> None:
    # Before the fix the 17th person in this 1.8 m wide room had no slot, the head
    # was never served and everyone ended the day at hydration need 1.0.
    document = _stocked(30).model_dump(mode="json")
    station = next(item for item in document["facilities"] if item["kind"] == "water_station")
    station["capacity"] = 3
    document["staff"].extend(
        {
            "id": f"water_staff_{index:02d}",
            "zone_id": station["zone_id"],
            "skills": ["water_service", "meal_service"],
        }
        for index in range(1, 4)
    )
    world = run_scenario(ScenarioSpec.model_validate(document), tmp_path)
    served = sum(
        1
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
        if json.loads(line).get("event", {}).get("event_type") == "resource_service_complete"
        and json.loads(line)["event"]["payload"]["resource_id"] == "water_ml"
    )
    assert served >= len(world.agents)
    assert statistics.median(agent.needs.hydration_need for agent in world.agents.values()) < 1.0
