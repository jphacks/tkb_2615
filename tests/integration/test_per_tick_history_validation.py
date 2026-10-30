"""Per-tick validation checks new history records; the full validation checks all of them.

The window of records the per-tick check reads is checked in
tests/unit/core/test_history.py.
"""

from __future__ import annotations

import copy

import pytest

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


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


@pytest.mark.parametrize("recent", [True, False])
def test_appending_an_operation_parses_only_the_records_since_the_previous_tick(
    finished_world, recent
) -> None:
    """Before an append, the shared operations state is checked as the per-tick check
    does: a bad recent record stops it, an old one is left to the full check."""
    from sheltersim.contracts.operations import SharedOperationsState
    from sheltersim.domain.common_operations import record_shared_operation

    world = WorldState.from_dict(copy.deepcopy(finished_world))
    state = world.model_state["shared_operations_v1"]
    receipt = SharedOperationsState.model_validate(state).history[0]
    history = state["history"]
    if recent:
        newest = copy.deepcopy(history[-1])
        newest["minute"] = world.minute_index - 1
        _break_operation_record(newest)
        newest["event_type"] = "not-an-operation"
        history.append(newest)
    else:
        history[0]["event_type"] = "not-an-operation"
    expected = "invalid before operation append" if recent else "current simulation boundary minute"
    with pytest.raises(ValueError, match=expected):
        record_shared_operation(world, receipt)
