"""WorldState.validate checks records against people and model state the Run holds.

That a Scenario with a scheduled resource loss runs to the end is checked in
tests/integration/test_resource_loss.py.
"""

from __future__ import annotations

import pytest

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.resource_service import SERVICE_STATE_KEY, ResourceServiceManager
from sheltersim.domain.simulator_state import initialize_simulator_extensions


def _water_request(agent_id: str) -> dict[str, object]:
    return {
        "minute": 0,
        "agent_id": agent_id,
        "action": "fetch_water",
        "outcome": "rejected",
        "reason": "no_facility",
        "facility_id": None,
        "origin_zone_id": "sleeping",
        "detail": None,
    }


@pytest.mark.parametrize(("agent_id", "valid"), [("A001", True), ("A999", False)])
def test_service_history_must_name_a_known_person(generated_scenario, agent_id, valid) -> None:
    # People away on a temporary exit are also known (#79); the intervention
    # test test_someone_who_has_used_water_or_meals_can_go_out_and_come_back runs that.
    world = WorldState.from_scenario(generated_scenario)
    world.model_state["resource_service_v1"]["request_history"].append(_water_request(agent_id))

    if valid:
        world.validate()
    else:
        with pytest.raises(StateInvariantError, match="request history is invalid"):
            world.validate()


def _scheduled(generated_scenario_with, *scheduled: dict[str, object]):
    return generated_scenario_with(
        operational_conditions={
            "initial_resources": {"status": "entered", "water_ml": 300_000, "food_meal_units": 100},
            "scheduled": list(scheduled),
        }
    )


def test_a_pending_loss_passes_the_per_tick_check_without_operational_state(
    generated_scenario_with,
) -> None:
    loss = {"kind": "resource_loss", "resource_id": "water_ml", "quantity": 1_000, "due_minute": 20}
    scenario = _scheduled(generated_scenario_with, loss)
    # Generated Scenarios opt into the state since #65; one saved before that did not.
    del scenario.model_configs["operational_events"]
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)

    assert OPERATIONAL_EVENTS_KEY not in world.model_state
    world.validate(validate_event_history=False)
    world.validate()


def test_the_per_tick_check_still_requires_state_for_operational_events(
    generated_scenario_with,
) -> None:
    absence = {
        "kind": "staff_unavailable",
        "staff_id": "staff_001",
        "start_minute": 30,
        "end_minute": 90,
    }
    world = WorldState.from_scenario(_scheduled(generated_scenario_with, absence))
    initialize_simulator_extensions(world)
    del world.model_state[OPERATIONAL_EVENTS_KEY]

    with pytest.raises(StateInvariantError, match="operational_events_v1"):
        world.validate(validate_event_history=False)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("travel_scheduled", "in_room_route_available"), ("rejected", "room_route_blocked")],
)
def test_in_room_request_outcomes_pass_state_validation(
    generated_scenario, outcome: str, reason: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    initialize_simulator_extensions(world)
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    agent_id = sorted(world.agents)[0]
    world.model_state[SERVICE_STATE_KEY]["request_history"].append(
        {
            "minute": 0,
            "agent_id": agent_id,
            "action": "fetch_water",
            "outcome": outcome,
            "reason": reason,
            "facility_id": station.id,
            "origin_zone_id": station.zone_id,
            "detail": None,
        }
    )
    world.validate()


def test_in_room_reasons_still_require_their_own_outcome(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    initialize_simulator_extensions(world)
    station = next(item for item in world.facilities.values() if item.kind == "water_station")
    world.model_state[SERVICE_STATE_KEY]["request_history"].append(
        {
            "minute": 0,
            "agent_id": sorted(world.agents)[0],
            "action": "fetch_water",
            "outcome": "queued",
            "reason": "in_room_route_available",
            "facility_id": station.id,
            "origin_zone_id": station.zone_id,
            "detail": None,
        }
    )
    with pytest.raises(StateInvariantError, match="outcome is inconsistent"):
        world.validate()


@pytest.mark.parametrize(("agent_id", "valid"), [("A003", True), ("A002", False)])
def test_a_resource_stall_may_name_the_head_of_any_independent_queue(
    generated_scenario_with_split_queues, place_three_at_facility, agent_id, valid
) -> None:
    # The station runs on a TickEngine in tests/integration/test_facility_queues.py
    # (test_independent_water_queues_wait_for_room_when_the_station_serves_one, #118).
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    place_three_at_facility(world, "water_station_main")
    service = ResourceServiceManager(world)
    for person, queue_id in (
        ("A001", "water_station_main-left-fifo"),
        ("A002", "water_station_main-left-fifo"),
        ("A003", "water_station_main-right-fifo"),
    ):
        service.request(person, "water_station_main", queue_id=queue_id)
    world.model_state[SERVICE_STATE_KEY]["stall_state"]["water_station_main"] = {
        "agent_id": agent_id,
        "reason": "capacity",
    }

    if valid:
        world.validate()
    else:
        with pytest.raises(StateInvariantError, match="current facility queue head"):
            world.validate()


def test_a_scenario_that_opts_into_operational_events_keeps_their_state(
    generated_scenario,
) -> None:
    # Generated Scenarios opt in without planning a delivery (#65).
    assert generated_scenario.model_configs["operational_events"] == {"enabled": True}
    world = WorldState.from_scenario(generated_scenario)
    assert set(world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]) == set(world.staff)
    del world.model_state[OPERATIONAL_EVENTS_KEY]

    with pytest.raises(StateInvariantError, match="requires operational_events_v1 state"):
        world.validate()
