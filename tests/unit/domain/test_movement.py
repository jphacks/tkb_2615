"""A queue longer than its room still lets the positioned head move up.

That a staffed station then serves the whole queue over a day is checked in
tests/integration/test_physical_queues.py.
"""

from __future__ import annotations

from sheltersim.core.world import WorldState
from sheltersim.domain.movement import (
    MOVEMENT_STATE_KEY,
    _geometry,
    _queue_slot_position,
    _zone_center,
    progress_physical_queue_positions,
)
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
