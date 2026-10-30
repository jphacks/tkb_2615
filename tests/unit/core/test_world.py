"""WorldState from a Scenario, its serialization, and what WorldState.validate refuses.

That a Scenario with a scheduled resource loss runs to the end is checked in
tests/integration/test_resource_loss.py.
"""

from __future__ import annotations

import json

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.models import Activity
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.resource_service import SERVICE_STATE_KEY, ResourceServiceManager
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.events.handlers import EventDispatcher
from sheltersim.experiments.scenario import ScenarioSpec


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


def test_world_rejects_duplicate_queue_membership() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.agents["A001"].activity = Activity.QUEUED
    world.facilities["toilet_1"].queue_agent_ids.append("A001")
    world.facilities["toilet_2"].queue_agent_ids.append("A001")

    with pytest.raises(StateInvariantError, match="multiple facility queues"):
        world.validate()


def test_world_rejects_duplicate_entries_in_one_queue() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.agents["A001"].activity = Activity.QUEUED
    world.facilities["toilet_1"].queue_agent_ids.extend(("A001", "A001"))

    with pytest.raises(StateInvariantError, match="duplicate queue entries"):
        world.validate()


def test_world_rejects_agent_with_unknown_zone() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.agents["A001"].zone_id = "missing-zone"

    with pytest.raises(StateInvariantError, match="unknown zone"):
        world.validate()


def test_world_canonical_json_is_stable() -> None:
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    world_a = WorldState.from_scenario(scenario)
    world_b = WorldState.from_scenario(scenario)

    assert world_a.canonical_json() == world_b.canonical_json()


def test_initial_world_preserves_scenario_graph_and_model_config() -> None:
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)

    world = WorldState.from_scenario(scenario)

    assert len(world.zone_edges) == len(scenario.zone_edges)
    assert world.model_configs == scenario.model_configs


def test_initial_world_owns_its_mutable_model_config_copy() -> None:
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    world = WorldState.from_scenario(scenario)

    world.model_configs["behavior"]["mode"] = "changed-in-world"

    assert scenario.model_configs["behavior"]["mode"] == "offline"


def test_initial_needs_copy_into_mutable_agent_state_and_serialize_separately() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    agent = world.agents["A001"]
    initial_hunger = agent.profile.initial_needs.hunger

    agent.needs.hunger = 0.9
    serialized = json.loads(world.canonical_json())
    serialized_agent = next(
        item for item in serialized["agents"] if item["profile"]["id"] == "A001"
    )
    assert agent.profile.initial_needs.hunger == initial_hunger
    assert agent.needs.hunger == 0.9
    assert serialized_agent["profile"]["initial_needs"]["hunger"] == initial_hunger
    assert serialized_agent["needs"]["hunger"] == 0.9


def test_world_rejects_mutated_need_state_outside_the_normalized_range() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.agents["A001"].needs.fatigue = -0.01

    with pytest.raises(StateInvariantError, match="invalid need levels"):
        world.validate()


def test_world_state_v5_rejects_old_versions_and_unknown_fields() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    document = world.to_dict()

    old_version = dict(document, schema_version=4)
    with pytest.raises(StateInvariantError, match="unsupported world-state schema version"):
        WorldState.from_dict(old_version)

    extended = dict(document, unexpected_contract_field=True)
    with pytest.raises(StateInvariantError, match="fields do not match"):
        WorldState.from_dict(extended)


def test_world_state_schema_two_serializes_pending_queue_and_realized_log_separately(
    make_event, record_handler
) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.schedule_event(make_event("pending", due_minute=3, sequence=0))
    world.schedule_event(make_event("realized", sequence=1))
    EventDispatcher({"record": record_handler()}).dispatch_due(world, 0)

    serialized = json.loads(world.canonical_json())

    assert world.schema_version == serialized["schema_version"] == 5
    assert [event["event_id"] for event in serialized["event_queue"]] == ["pending"]
    assert [record["event"]["event_id"] for record in serialized["event_log"]] == ["realized"]
    assert serialized["event_log"][0]["status"] == "applied"
    world.validate()


def test_world_state_v5_serializes_bounded_psychology_and_decision_time() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    agent = world.agents["A001"]
    agent.stress = 0.35
    agent.trust = 0.72
    agent.next_decision_at = 23

    serialized = world.canonical_json()

    assert world.schema_version == 5
    assert '"schema_version":5' in serialized
    assert '"stress":0.35' in serialized
    assert '"trust":0.72' in serialized
    assert '"next_decision_at":23' in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [("stress", -0.1), ("stress", 1.1), ("trust", float("nan"))],
)
def test_world_rejects_psychology_values_outside_zero_to_one(field: str, value: float) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    setattr(world.agents["A001"], field, value)

    with pytest.raises(StateInvariantError, match=f"invalid {field}"):
        world.validate()


def test_state_digest_is_the_state_hash_of_a_checkpoint_of_the_world(
    live_checkpoint_world,
) -> None:
    """state_digest() converts only what a save converts; it gives the hash of to_dict()
    and of a Checkpoint, including escaped and non-ASCII strings and floats."""
    from sheltersim.core.checkpoint import Checkpoint
    from sheltersim.core.checkpoint_cache import CanonicalEncoder
    from sheltersim.events.handlers import EventDispatcher
    from sheltersim.events.models import EventKind, ScheduledEvent

    world = live_checkpoint_world
    world.schedule_event(
        ScheduledEvent(
            event_id="digest-event",
            kind=EventKind.EXOGENOUS,
            due_minute=world.minute_index,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="unknown_digest_test_event",
            payload={"text": 'quote " back \\\\ line\\n tab\\t 避難所 \\u2028', "x": 0.1 + 0.2},
        )
    )
    EventDispatcher().dispatch_due(world, world.minute_index)
    assert world.state_digest() == CanonicalEncoder().world_tree_digest(world.to_dict())
    assert world.state_digest() == Checkpoint.capture(world).state_hash


def test_same_state_compares_every_field_and_skips_only_known_equal_records() -> None:
    from sheltersim.events.models import EventKind, ScheduledEvent

    def event(name: str, minute: int, sequence: int) -> ScheduledEvent:
        return ScheduledEvent(
            event_id=name,
            kind=EventKind.EXOGENOUS,
            due_minute=minute,
            priority=0,
            sequence=sequence,
            event_type="unknown_same_state_test_event",
            payload={},
        )

    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    for index in range(2):
        world.schedule_event(event(f"done-{index}", 0, world.allocate_event_sequence()))
    EventDispatcher().dispatch_due(world, 0)
    first = event("later-a", 5, world.allocate_event_sequence())
    second = event("later-b", 3, world.allocate_event_sequence())
    world.schedule_event(first)
    world.schedule_event(second)
    copy = WorldState.from_dict(world.to_dict())
    assert copy.same_state(world)
    # Pending events compare in queue order, whatever order they were added in.
    assert copy.event_queue._events != world.event_queue._events
    copy.agents[next(iter(copy.agents))].stress += 0.25
    assert not copy.same_state(world)
    copy = WorldState.from_dict(world.to_dict())
    copy.model_state["test_only"] = {"value": 1}
    assert not copy.same_state(world)
    document = world.to_dict()
    document["event_log"][1]["detail"] = "a different outcome note"
    copy = WorldState.from_dict(document)
    assert not copy.same_state(world)
    assert not copy.same_state(world, known_equal_events=1)
    # Records known to be equal already are not compared again.
    assert copy.same_state(world, known_equal_events=2)


def test_same_state_skips_only_the_known_equal_entries_of_a_model_history() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.model_state["test_only"] = {"history": [{"i": 0}, {"i": 1}], "other": 1}
    known = {("test_only", "history"): 1}
    copy = WorldState.from_dict(world.to_dict())
    assert copy.same_state(world, known_equal_histories=known)

    copy.model_state["test_only"]["history"][1]["i"] = 5
    assert not copy.same_state(world, known_equal_histories=known)
    copy = WorldState.from_dict(world.to_dict())
    copy.model_state["test_only"]["history"].append({"i": 2})
    assert not copy.same_state(world, known_equal_histories=known)
    copy = WorldState.from_dict(world.to_dict())
    copy.model_state["test_only"]["other"] = 2
    assert not copy.same_state(world, known_equal_histories=known)
    copy = WorldState.from_dict(world.to_dict())
    del copy.model_state["test_only"]["other"]
    assert not copy.same_state(world, known_equal_histories=known)
    copy = WorldState.from_dict(world.to_dict())
    copy.model_state["added"] = {}
    assert not copy.same_state(world, known_equal_histories=known)
    # A leading entry is what the caller vouches for, and is not compared.
    copy = WorldState.from_dict(world.to_dict())
    copy.model_state["test_only"]["history"][0]["i"] = 5
    assert copy.same_state(world, known_equal_histories=known)
    assert not copy.same_state(world)
