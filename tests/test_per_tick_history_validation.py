"""Per-tick validation checks new history records; the full validation checks all of them."""

from __future__ import annotations

import copy

import pytest

from sheltersim.core.history import records_since_previous_tick
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _rows(*minutes: object) -> list[object]:
    return [{"minute": minute, "n": index} for index, minute in enumerate(minutes)]


def test_the_window_holds_the_last_tick_and_the_record_before_it() -> None:
    history = _rows(0, 1, 3, 5, 5)

    assert records_since_previous_tick(history, 6) == history[2:]


def test_the_window_keeps_the_last_record_when_the_tick_added_none() -> None:
    history = _rows(0, 1)

    assert records_since_previous_tick(history, 10) == history[1:]
    assert records_since_previous_tick([], 10) == []


def test_malformed_records_do_not_end_the_scan() -> None:
    history = [{"minute": 0}, "not a record", {"minute": None}, {"minute": 2.0}]

    assert records_since_previous_tick(history, 10) == history


@pytest.fixture(scope="module")
def finished_world(tmp_path_factory, generated_scenario_preview) -> dict:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    world = run_scenario(scenario, tmp_path_factory.mktemp("run"))
    for namespace, key in (
        ("body_state_v1", "history"),
        ("shared_operations_v1", "history"),
        ("resource_service_v1", "request_history"),
    ):
        assert len(world.model_state[namespace][key]) >= 3, (namespace, key)
    return world.to_dict()


def _break_body_record(record: dict) -> None:
    record["actor_ids"] = ["unknown-person"]


def _break_request_record(record: dict) -> None:
    record["agent_id"] = "unknown-person"


def _break_operation_record(record: dict) -> None:
    record["event_id"] = "unknown-event"


@pytest.mark.parametrize(
    ("namespace", "key", "breaker"),
    [
        ("body_state_v1", "history", _break_body_record),
        ("resource_service_v1", "request_history", _break_request_record),
        ("shared_operations_v1", "history", _break_operation_record),
    ],
)
def test_a_bad_record_from_the_last_tick_stops_the_per_tick_check(
    finished_world, namespace, key, breaker
) -> None:
    world = WorldState.from_dict(copy.deepcopy(finished_world))
    history = world.model_state[namespace][key]
    newest = copy.deepcopy(history[-1])
    newest["minute"] = world.minute_index - 1
    breaker(newest)
    history.append(newest)

    with pytest.raises(StateInvariantError):
        world.validate(validate_event_history=False)


@pytest.mark.parametrize(
    ("namespace", "key", "breaker"),
    [
        ("body_state_v1", "history", _break_body_record),
        ("resource_service_v1", "request_history", _break_request_record),
        ("shared_operations_v1", "history", _break_operation_record),
    ],
)
def test_an_old_bad_record_is_left_to_the_full_check(
    finished_world, namespace, key, breaker
) -> None:
    world = WorldState.from_dict(copy.deepcopy(finished_world))
    history = world.model_state[namespace][key]
    assert history[0]["minute"] < world.minute_index - 2
    breaker(history[0])

    world.validate(validate_event_history=False)
    with pytest.raises(StateInvariantError):
        world.validate()
