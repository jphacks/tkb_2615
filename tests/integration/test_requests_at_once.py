"""What `_apply_effect` does with no trip to make, and the checks of having arrived.

A person who asks a queue at once and a request closed at once, on a world with a floor plan and on
one without, and `agent_is_at_*`: each kernel on the world, the Python rule on a copy.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.domain import movement
from sheltersim.domain.agent_dynamics import learn_facility
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.movement import MOVEMENT_STATE_KEY as KEY
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.behavior import ActionKind

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

SEEKS = (ActionKind.SEEK_HELP, ActionKind.SEEK_INFORMATION)


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _proposal(action: ActionKind, agent_id: str, world: WorldState) -> Any:
    return SimpleNamespace(
        action=action, agent_id=agent_id, next_decision_at=world.minute_index + 7, target_id=None
    )


def _pending(world: WorldState) -> list[Any]:
    return [
        (event.event_id, event.sequence, event.due_minute, sorted(event.payload.items()))
        for event in world.event_queue.pending()
    ]


def _effect(world: WorldState, action: ActionKind, agent_id: str, setup: Any = None) -> Any:
    """The kernel on the world, the Python step on a copy: the same result and the same world."""
    twin = copy.deepcopy(world)
    controller, python_controller = _controller(world), _controller(twin)
    if setup is not None:
        setup(controller)
        setup(python_controller)
    proposal = _proposal(action, agent_id, world)
    result = native.rules("proposals").apply_effect(controller, proposal)
    expected = ProposePhase._apply_effect(python_controller, proposal)
    assert list(result) == list(expected)
    assert type(result[2]) is type(expected[2])
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert world.metric_state == twin.metric_state
    assert _pending(world) == _pending(twin)
    assert len(world.event_log.records) == len(twin.event_log.records)
    assert list(world.active_tasks) == list(twin.active_tasks)
    return result


def _place_at(world: WorldState, agent_id: str, facility_id: str) -> None:
    facility = world.facilities[facility_id]
    world.agents[agent_id].zone_id = facility.zone_id
    state = world.model_state.get(KEY)
    if state is not None:
        state["positions"][agent_id]["zone_id"] = facility.zone_id
    if "agent_dynamics_v1" in world.model_state:
        learn_facility(world, agent_id, facility_id)


def _without_a_floor_plan() -> WorldState:
    scenario = ScenarioSpec.model_validate_json(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    world = WorldState.from_scenario(scenario)
    assert "facility_geometry" not in world.model_configs
    return world


def _facility_of(world: WorldState, word: str) -> str:
    return next(key for key, item in world.facilities.items() if word in item.kind.casefold())


# ---- A queue at once -----------------------------------------------------------------------------


def test_a_toilet_at_an_independent_queue(generated_scenario_with_split_queues: Any) -> None:
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    _place_at(world, "A001", "toilet_01")
    target, _, committed = _effect(world, ActionKind.REQUEST_TOILET, "A001")
    assert world.facilities[target].zone_id == world.facilities["toilet_01"].zone_id
    assert committed is True
    assert world.agents["A001"].activity is Activity.QUEUED
    assert "A001" in world.facilities[target].queue_agent_ids


def test_a_service_at_an_independent_queue(generated_scenario_with_split_queues: Any) -> None:
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    facility_id = _facility_of(world, "water")
    _place_at(world, "A001", facility_id)
    target, _, committed = _effect(world, ActionKind.FETCH_WATER, "A001")
    assert target == facility_id
    assert committed is True
    assert "A001" in world.facilities[facility_id].resource_queue_agent_ids


def _own_queues(scenario: Any) -> WorldState:
    document = scenario.model_dump(mode="json")
    document["model_configs"].pop("facility_queues", None)
    document["model_configs"]["facility_geometry"]["service_points"] = []
    return WorldState.from_scenario(type(scenario).model_validate(document))


@pytest.mark.parametrize(
    ("action", "word"),
    [
        (ActionKind.REQUEST_TOILET, "toilet"),
        (ActionKind.FETCH_WATER, "water"),
        (ActionKind.COLLECT_MEAL, "meal"),
    ],
)
def test_someone_at_the_point_of_the_facility_asks_at_once(
    generated_scenario: Any, action: ActionKind, word: str
) -> None:
    world = _own_queues(generated_scenario)
    facility_id = _facility_of(world, word)
    _place_at(world, "A001", facility_id)
    point = world.model_configs["facility_geometry"]["facility_points"][facility_id]
    world.model_state[KEY]["positions"]["A001"]["point"] = dict(point)
    target, _, _ = _effect(world, action, "A001")
    assert target == facility_id
    assert world.agents["A001"].activity is Activity.QUEUED


@pytest.mark.parametrize(
    ("action", "word"),
    [
        (ActionKind.REQUEST_TOILET, "toilet"),
        (ActionKind.FETCH_WATER, "water"),
        (ActionKind.COLLECT_MEAL, "meal"),
    ],
)
def test_without_a_floor_plan_someone_in_the_zone_asks_at_once(
    action: ActionKind, word: str
) -> None:
    world = _without_a_floor_plan()
    facility_id = next(
        (key for key, item in world.facilities.items() if word in item.kind.casefold()), None
    )
    if facility_id is None:
        pytest.skip(f"this Scenario has no {word}")
    agent_id = sorted(world.agents)[0]
    _place_at(world, agent_id, facility_id)
    target, _, committed = _effect(world, action, agent_id)
    assert target == facility_id
    assert committed is True


def test_a_person_the_service_turns_down_is_recorded_by_the_python_step(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    facility_id = _facility_of(world, "water")
    _place_at(world, "A001", facility_id)
    world.agents["A001"].activity = Activity.MOVING
    before = _bytes(world)
    controller = _controller(world)
    with pytest.raises(native.Unsupported):
        native.rules("proposals").apply_effect(
            controller, _proposal(ActionKind.FETCH_WATER, "A001", world)
        )
    assert _bytes(world) == before
    target, _, committed = controller._apply_effect(
        _proposal(ActionKind.FETCH_WATER, "A001", world)
    )
    assert target is None
    assert committed is False
    records = world.model_state["resource_service_v1"]["request_history"]
    assert records[-1]["reason"] == "request_rejected"


def test_a_toilet_request_a_test_patched_is_the_python_step_s(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    _place_at(world, "A001", "toilet_01")
    controller = _controller(world)
    controller.toilet_service.request = lambda *_arguments, **_options: None
    with pytest.raises(native.Unsupported, match="another function"):
        native.rules("proposals").apply_effect(
            controller, _proposal(ActionKind.REQUEST_TOILET, "A001", world)
        )


def test_a_service_action_with_no_service_is_rejected(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)

    def no_service(controller: Any) -> None:
        controller.resource_service = None

    target, _, committed = _effect(world, ActionKind.FETCH_WATER, "A001", no_service)
    assert target is None
    assert committed is False
    assert world.metric_state["resource_service_request_rejected_count"] == 1.0


# ---- A request closed at once --------------------------------------------------------------------


@pytest.mark.parametrize("action", SEEKS)
def test_a_request_of_the_last_minute_is_closed_at_once(
    generated_scenario: Any, action: ActionKind
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.minute_index = world.duration_minutes - 1
    before = world.id_counters.get("event", 0)
    activity = world.agents["A001"].activity
    target, _, committed = _effect(world, action, "A001")
    assert committed is False
    request = world.support_requests[target]
    assert request.status is SupportRequestStatus.UNRESOLVED
    assert request.resolved_minute == world.minute_index
    assert world.agents["A001"].support_cooldown_until == request.cooldown_until
    assert world.agents["A001"].support_request_id is None
    assert world.agents["A001"].activity is activity
    assert world.metric_state["support_unresolved_count"] == 1.0
    assert not world.event_queue.pending() or all(
        event.event_type != "support_request_timeout" for event in world.event_queue.pending()
    )
    # `seek_help` records two care receipts: asked, and interrupted.
    assert world.id_counters.get("event", 0) == before + (
        2 if action is ActionKind.SEEK_HELP else 0
    )


def test_a_person_in_a_cooldown_is_turned_down_in_the_last_minute_too(
    generated_scenario: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.minute_index = world.duration_minutes - 1
    world.agents["A001"].support_cooldown_until = world.duration_minutes + 5
    target, _, committed = _effect(world, ActionKind.SEEK_HELP, "A001")
    assert target is None
    assert committed is False
    assert world.metric_state["support_request_rejected_count"] == 1.0


# ---- Having arrived ------------------------------------------------------------------------------


def _same_answer(world: WorldState, name: str, *arguments: Any) -> bool:
    answer = getattr(native.rules("movement"), name)(world, *arguments)
    assert answer == getattr(movement, name).__wrapped__(world, *arguments)
    return answer


def test_having_arrived_at_a_slot_or_the_service_point(generated_scenario: Any) -> None:
    world = _own_queues(generated_scenario)
    controller = _controller(world)
    people = ("A001", "A002", "A003")
    for agent_id in people:
        _place_at(world, agent_id, "toilet_01")
        controller.toilet_service.request(agent_id, "toilet_01", validate_state=False)
    point = world.model_configs["facility_geometry"]["facility_points"]["toilet_01"]
    state = world.model_state[KEY]
    answers = []
    for agent_id in people:
        for where in (None, point):
            if where is not None:
                state["positions"][agent_id]["point"] = dict(where)
            answers.append(_same_answer(world, "agent_is_at_queue_slot", agent_id, "toilet_01"))
            answers.append(
                _same_answer(world, "agent_is_at_facility_service_point", agent_id, "toilet_01")
            )
    assert True in answers
    assert False in answers
    assert not _same_answer(world, "agent_is_at_queue_slot", "A001", "no_such_facility")


def test_having_arrived_at_the_end_of_a_trip(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    state = world.model_state[KEY]
    target = {"x": 1.5, "y": 2.5}
    journey = {"route_points": [{"x": 0.5, "y": 0.5}, target]}
    state["positions"]["A001"]["point"] = dict(target)
    assert _same_answer(world, "agent_is_at_queue_trip_destination", "A001", journey)
    state["positions"]["A001"]["point"] = {"x": 1.5, "y": 2.5502}
    assert not _same_answer(world, "agent_is_at_queue_trip_destination", "A001", journey)
    state["positions"]["A001"]["point"] = {"x": 1.5, "y": 2.5499}
    assert _same_answer(world, "agent_is_at_queue_trip_destination", "A001", journey)
    assert not _same_answer(world, "agent_is_at_queue_trip_destination", "A001", {})
    assert not _same_answer(world, "agent_is_at_queue_trip_destination", "A001", None)
    assert not _same_answer(
        world, "agent_is_at_queue_trip_destination", "A001", {"route_points": []}
    )


def test_a_journey_in_a_dict_of_another_kind_is_the_python_rule_s(generated_scenario: Any) -> None:
    class Other(dict):
        pass

    world = WorldState.from_scenario(generated_scenario)
    with pytest.raises(native.Unsupported):
        native.rules("movement").agent_is_at_queue_trip_destination(world, "A001", Other())


def test_having_arrived_with_an_independent_queue(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    controller = _controller(world)
    _place_at(world, "A001", "toilet_01")
    queues = world.model_state["facility_queues_v1"]["queues"]
    queue_id = min(key for key, row in queues.items() if row["facility_id"] == "toilet_01")
    point_id = queues[queue_id]["service_point_id"]
    controller.toilet_service.request("A001", "toilet_01", queue_id=queue_id, validate_state=False)
    _same_answer(world, "agent_is_at_queue_slot", "A001", "toilet_01", queue_id)
    _same_answer(world, "agent_is_at_facility_service_point", "A001", "toilet_01", point_id)
    _same_answer(world, "agent_is_at_facility_service_point", "A001", "toilet_01", "no_such_point")


@pytest.mark.parametrize("boundary", [None, 0, 7])
def test_the_staff_positions_asked_from_outside_the_walk(
    generated_scenario: Any, boundary: int | None
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    twin = copy.deepcopy(world)
    answer = native.rules("movement").staff_position_snapshots(world, boundary_minute=boundary)
    expected = movement.staff_position_snapshots.__wrapped__(twin, boundary_minute=boundary)
    assert repr(answer) == repr(expected)
    assert list(answer) == sorted(world.staff)
    assert _bytes(world) == _bytes(twin)


def test_without_the_movement_state_or_a_floor_plan() -> None:
    world = _without_a_floor_plan()
    agent_id = sorted(world.agents)[0]
    facility_id = _facility_of(world, "toilet")
    if KEY in world.model_state:
        del world.model_state[KEY]
    _same_answer(world, "agent_is_at_queue_slot", agent_id, facility_id)
    assert _same_answer(world, "agent_is_at_facility_service_point", agent_id, facility_id)
    kernel = native.rules("movement").facility_approach_route(world, agent_id, facility_id)
    rule = movement.facility_approach_route.__wrapped__(world, agent_id, facility_id)
    assert repr(kernel) == repr(rule)
