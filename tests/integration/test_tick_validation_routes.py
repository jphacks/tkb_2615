"""A state broken by any way of writing stops the tick with the Python rule's error.

The per-tick check takes the word of a Rust row for psychological_distress_v1,
information_spread_v1 and staff_dynamics_v1, and the Python rules of those namespaces no longer
have Rust checks of their own in front of them. So each way of writing has to end in one of three
places: a row that cannot hold the value, a kernel that says no, or a row the kernels do not trust
any more; and then the Python rule speaks. docs/rust-port/lanes/t-tick-validation.md lists what
proves what. Without the kernels the rules always run, so all of this holds in both modes, apart
from the writes past a TrackedDict at the end.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core import state_mirror
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

PSYCHOLOGY = "psychological_distress_v1"
INFORMATION = "information_spread_v1"
STAFF = "staff_dynamics_v1"
OWNED = (PSYCHOLOGY, INFORMATION, STAFF)

PSYCHOLOGY_RANGE = (
    "invalid psychological distress state: long_term_distress must be finite and between 0 and 1"
)
PSYCHOLOGY_SCHEMA = (
    "invalid psychological distress state: unsupported psychological distress state schema"
)
PRESSURE_RANGE = (
    "invalid information-spread state: "
    "information query pressure for A001 must be finite and between 0 and 1"
)
EXTENSION = "invalid simulator extension state"
DECLARED = "declared model-state validation failed"


@pytest.fixture(scope="module")
def finished_world(tmp_path_factory, generated_scenario_preview) -> dict:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    return run_scenario(scenario, tmp_path_factory.mktemp("run")).to_dict()


def _checked_world(document: dict) -> WorldState:
    """A world whose end-of-tick check has run, with every kernel saying yes."""
    world = WorldState.from_dict(copy.deepcopy(document))
    world.validate_after_tick()
    world.validate_after_tick()
    if native.available():
        assert all(rust_validated(world, name) for name in OWNED)
    return world


def _error_of(check: Callable[[], None]) -> tuple[str, str | None] | None:
    try:
        check()
    except StateInvariantError as error:
        cause = error.__cause__
        return str(error), None if cause is None else str(cause)
    return None


def _python_error(world: WorldState, monkeypatch) -> tuple[str, str | None] | None:
    """What the Python rules alone say about this world."""
    with monkeypatch.context() as patch:
        patch.setattr(state_mirror, "_RUST_VALIDATORS", {})
        patch.setattr(native, "world_agents_valid", lambda *_: False)
        return _error_of(lambda: world.validate(validate_event_history=False))


def _first(mapping: dict) -> Any:
    return next(iter(mapping.values()))


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


# ---- The namespaces: where a broken value goes, and each way of putting it there ---------------


def _distress_record(namespace: dict) -> dict:
    return _first(namespace["agents"])


def _whole_namespace(namespace: dict) -> dict:
    return namespace


def _pressures(namespace: dict) -> dict:
    return namespace["query_pressure"]


def _staff_record(namespace: dict) -> dict:
    return _first(namespace["staff"])


# name -> namespace, the dict holding the value, its key, the value, the error, its cause or None.
BROKEN = {
    "distress out of range": (PSYCHOLOGY, _distress_record, "long_term_distress", 2.0),
    "distress of the wrong type": (PSYCHOLOGY, _distress_record, "long_term_distress", "x"),
    "distress schema": (PSYCHOLOGY, _whole_namespace, "schema_version", 99),
    "pressure out of range": (INFORMATION, _pressures, "A001", 2.0),
    "pressure of the wrong type": (INFORMATION, _pressures, "A001", "x"),
    "staff fatigue out of range": (STAFF, _staff_record, "fatigue", 2.0),
    "staff fatigue of the wrong type": (STAFF, _staff_record, "fatigue", "x"),
}
ERRORS = {
    "distress out of range": PSYCHOLOGY_RANGE,
    "distress of the wrong type": PSYCHOLOGY_RANGE,
    "distress schema": PSYCHOLOGY_SCHEMA,
    "pressure out of range": PRESSURE_RANGE,
    "pressure of the wrong type": PRESSURE_RANGE,
    "staff fatigue out of range": EXTENSION,
    "staff fatigue of the wrong type": EXTENSION,
}


def _tracked_write(world: WorldState, name: str, holder: Any, key: str, value: Any) -> None:
    holder(world.model_state[name])[key] = value


def _tracked_update(world: WorldState, name: str, holder: Any, key: str, value: Any) -> None:
    holder(world.model_state[name]).update({key: value})


def _broken_copy(world: WorldState, name: str, holder: Any, key: str, value: Any) -> dict:
    namespace = _plain(world.model_state[name])
    holder(namespace)[key] = value
    return namespace


def _namespace_written_whole(world, name, holder, key, value) -> None:
    world.model_state[name] = _broken_copy(world, name, holder, key, value)


def _table_written_whole(world, name, holder, key, value) -> None:
    table = {key_: _plain(item) for key_, item in world.model_state.items()}
    table[name] = _broken_copy(world, name, holder, key, value)
    world.model_state = table


def _table_stored_in_world_dict(world, name, holder, key, value) -> None:
    table = {key_: _plain(item) for key_, item in world.model_state.items()}
    table[name] = _broken_copy(world, name, holder, key, value)
    world.__dict__["model_state"] = table


def _namespace_stored_past_the_table(world, name, holder, key, value) -> None:
    dict.__setitem__(world.model_state, name, _broken_copy(world, name, holder, key, value))


ROUTES = {
    "TrackedDict item": _tracked_write,
    "TrackedDict update": _tracked_update,
    "whole namespace": _namespace_written_whole,
    "whole model_state": _table_written_whole,
    "__dict__ of the world": _table_stored_in_world_dict,
    "past the NamespaceTable": _namespace_stored_past_the_table,
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("broken", BROKEN)
def test_a_broken_namespace_stops_the_tick_whichever_way_it_was_written(
    finished_world, monkeypatch, broken, route
) -> None:
    name, holder, key, value = BROKEN[broken]
    world = _checked_world(finished_world)
    ROUTES[route](world, name, holder, key, value)
    expected = _python_error(world, monkeypatch)
    assert expected is not None and expected[0] == ERRORS[broken]
    assert not rust_validated(world, name)
    assert _error_of(world.validate_after_tick) == expected


@pytest.mark.parametrize("name", OWNED)
def test_a_good_namespace_written_whole_is_still_accepted(finished_world, name) -> None:
    world = _checked_world(finished_world)
    world.model_state[name] = _plain(world.model_state[name])
    assert rust_validated(world, name) is native.available()
    world.validate_after_tick()


@pytest.mark.parametrize("name", OWNED)
def test_a_namespace_taken_away_past_the_table_is_not_taken_from_its_row(
    finished_world, monkeypatch, name
) -> None:
    world = _checked_world(finished_world)
    dict.pop(world.model_state, name)
    assert not rust_validated(world, name)
    expected = _python_error(world, monkeypatch)
    assert expected is not None
    assert _error_of(world.validate_after_tick) == expected


def test_a_good_state_in_another_table_goes_to_the_python_rules(finished_world) -> None:
    world = _checked_world(finished_world)
    world.__dict__["model_state"] = {key: _plain(item) for key, item in world.model_state.items()}
    assert not any(rust_validated(world, name) for name in OWNED)
    world.validate_after_tick()


# ---- What the retired kernels judged: each value, against the Python rules ---------------------

_NUMBERS: list[Any] = [
    0,
    0.0,
    1,
    1.0,
    0.5,
    -0.0,
    -0.1,
    1.1,
    1.0000000000000002,
    True,
    None,
    "0.5",
    float("nan"),
    float("inf"),
    10**30,
]


def _same_as_python(world: WorldState, monkeypatch, name: str) -> None:
    expected = _python_error(world, monkeypatch)
    # A kernel may refuse what the rules pass; it may not take what they turn down.
    if expected is not None:
        assert not rust_validated(world, name)
    assert _error_of(lambda: world.validate(validate_event_history=False)) == expected


DISTRESS_RECORD = {
    "baseline_distress": 0.5,
    "long_term_distress": 0.5,
    "daily_acute_sum": 1.5,
    "daily_high_distress_minutes": 2,
    "daily_observed_minutes": 3,
    "completed_days": 1,
}
DISTRESS_CHANGES: list[dict[str, Any]] = [
    {},
    {"daily_observed_minutes": 1439},
    {"daily_observed_minutes": 1440},
    {"daily_high_distress_minutes": 3},
    {"daily_high_distress_minutes": 4},
    {"daily_acute_sum": 3},
    {"daily_acute_sum": 3.0000000001},
    {"daily_acute_sum": 3.1},
    *({name: value} for name in DISTRESS_RECORD for value in _NUMBERS),
]


@pytest.mark.parametrize("change", DISTRESS_CHANGES, ids=repr)
def test_the_distress_kernel_takes_only_what_the_python_rule_passes(
    finished_world, monkeypatch, change
) -> None:
    world = _checked_world(finished_world)
    agents = world.model_state[PSYCHOLOGY]["agents"]
    agents[next(iter(agents))] = {**DISTRESS_RECORD, **change}
    _same_as_python(world, monkeypatch, PSYCHOLOGY)
    if not change and native.available():
        assert rust_validated(world, PSYCHOLOGY)


@pytest.mark.parametrize("key", ["", 7])
def test_the_distress_kernel_turns_down_the_keys_the_python_rule_does(
    finished_world, monkeypatch, key
) -> None:
    world = _checked_world(finished_world)
    world.model_state[PSYCHOLOGY]["agents"][key] = dict(DISTRESS_RECORD)
    _same_as_python(world, monkeypatch, PSYCHOLOGY)


CONTACT_CHANGES: list[dict[str, Any]] = [
    {},
    *({"consecutive_minutes": value} for value in (0, 1, 4, 5, True, 1.0, 10**30)),
    *({"last_seen_minute": value} for value in (-1, 0, 10**9, 10**30, True)),
    {"unknown": 1},
]


@pytest.mark.parametrize("change", CONTACT_CHANGES, ids=repr)
def test_the_information_kernel_takes_only_the_contacts_the_python_rule_passes(
    finished_world, monkeypatch, change
) -> None:
    world = _checked_world(finished_world)
    first, second = sorted(world.agents)[:2]
    record = {"last_seen_minute": 3, "consecutive_minutes": 2, **change}
    world.model_state[INFORMATION]["contacts"][f'["{first}","{second}"]'] = record
    _same_as_python(world, monkeypatch, INFORMATION)
    if not change and native.available():
        assert rust_validated(world, INFORMATION)


def test_the_last_minute_of_a_contact_is_on_the_edge_of_the_scenario(
    finished_world, monkeypatch
) -> None:
    for offset, passes in ((-1, True), (0, False)):
        world = _checked_world(finished_world)
        first, second = sorted(world.agents)[:2]
        last = world.duration_minutes + offset
        world.model_state[INFORMATION]["contacts"][f'["{first}","{second}"]'] = {
            "last_seen_minute": last,
            "consecutive_minutes": 1,
        }
        _same_as_python(world, monkeypatch, INFORMATION)
        assert (_python_error(world, monkeypatch) is None) is passes


@pytest.mark.parametrize("key", ["[", '["A001","A001"]', '["A001", "A002"]', '["a","b"]', "7", 7])
def test_the_information_kernel_turns_down_the_contact_keys_the_python_rule_does(
    finished_world, monkeypatch, key
) -> None:
    world = _checked_world(finished_world)
    world.model_state[INFORMATION]["contacts"][key] = {
        "last_seen_minute": 3,
        "consecutive_minutes": 2,
    }
    _same_as_python(world, monkeypatch, INFORMATION)


@pytest.mark.parametrize("value", _NUMBERS, ids=repr)
def test_the_information_kernel_takes_only_the_pressures_the_python_rule_passes(
    finished_world, monkeypatch, value
) -> None:
    world = _checked_world(finished_world)
    world.model_state[INFORMATION]["query_pressure"]["A001"] = value
    _same_as_python(world, monkeypatch, INFORMATION)


# ---- The entities: rows are not trusted, because __dict__ does not reach them -------------------


def _agent(world: WorldState) -> Any:
    return _first(world.agents)


def _broken_agent(world: WorldState) -> Any:
    agent = copy.deepcopy(_agent(world))
    agent.stress = 1.5
    return agent


def _stress_attribute(world: WorldState) -> None:
    _agent(world).stress = 1.5


def _stress_in_dict(world: WorldState) -> None:
    _agent(world).__dict__["stress"] = 1.5


def _stress_past_setattr(world: WorldState) -> None:
    object.__setattr__(_agent(world), "stress", 1.5)


def _need_in_dict(world: WorldState) -> None:
    _agent(world).needs.__dict__["hunger"] = 1.5


def _profile_needs_past_frozen(world: WorldState) -> None:
    object.__setattr__(_agent(world).profile, "initial_needs", "unknown")


def _agents_written_whole(world: WorldState) -> None:
    agent = _broken_agent(world)
    world.agents = {**world.agents, agent.id: agent}


def _agents_in_world_dict(world: WorldState) -> None:
    agent = _broken_agent(world)
    world.__dict__["agents"] = {**world.agents, agent.id: agent}


def _agent_past_the_map(world: WorldState) -> None:
    agent = _broken_agent(world)
    dict.__setitem__(world.agents, agent.id, agent)


def _facility_capacity_in_dict(world: WorldState) -> None:
    _first(world.facilities).__dict__["capacity"] = -1


def _staff_busy_in_dict(world: WorldState) -> None:
    _first(world.staff).__dict__["busy_until"] = -1


def _staff_taken_away(world: WorldState) -> None:
    del world.staff[next(iter(world.staff))]


def _staff_past_the_map(world: WorldState) -> None:
    dict.pop(world.staff, next(iter(world.staff)))


def _staff_in_world_dict(world: WorldState) -> None:
    world.__dict__["staff"] = dict(list(world.staff.items())[1:])


def _agents_taken_away(world: WorldState) -> None:
    del world.agents[next(iter(world.agents))]


def _agent_gone_past_the_map(world: WorldState) -> None:
    dict.pop(world.agents, next(iter(world.agents)))


def _agents_short_in_world_dict(world: WorldState) -> None:
    world.__dict__["agents"] = dict(list(world.agents.items())[1:])


STRESS = "Agent 'A001' has invalid stress: stress must be finite and between 0 and 1"
ENTITY_ROUTES = {
    "attribute": (_stress_attribute, STRESS),
    "__dict__": (_stress_in_dict, STRESS),
    "object.__setattr__": (_stress_past_setattr, STRESS),
    "whole map": (_agents_written_whole, STRESS),
    "__dict__ of the world": (_agents_in_world_dict, STRESS),
    "past the IdMap": (_agent_past_the_map, STRESS),
    "need in __dict__": (
        _need_in_dict,
        "Agent 'A001' has invalid need levels: hunger must be finite and between 0 and 1",
    ),
    "frozen profile": (
        _profile_needs_past_frozen,
        "Agent 'A001' has invalid initial need profile",
    ),
    "staff taken away": (_staff_taken_away, EXTENSION),
    "staff past the IdMap": (_staff_past_the_map, EXTENSION),
    "staff in the __dict__ of the world": (_staff_in_world_dict, EXTENSION),
    "Agent taken away": (_agents_taken_away, DECLARED),
    "Agent past the IdMap": (_agent_gone_past_the_map, DECLARED),
    "Agents in the __dict__ of the world": (_agents_short_in_world_dict, DECLARED),
}


@pytest.mark.parametrize("route", ENTITY_ROUTES)
def test_a_broken_entity_stops_the_tick_whichever_way_it_was_written(
    finished_world, monkeypatch, route
) -> None:
    corrupt, error = ENTITY_ROUTES[route]
    world = _checked_world(finished_world)
    corrupt(world)
    expected = _python_error(world, monkeypatch)
    assert expected is not None and expected[0] == error
    assert _error_of(world.validate_after_tick) == expected


@pytest.mark.parametrize(
    "corrupt",
    [
        _facility_capacity_in_dict,
        _staff_busy_in_dict,
    ],
)
def test_the_entity_rules_read_the_objects_not_the_rows(
    finished_world, monkeypatch, corrupt
) -> None:
    world = _checked_world(finished_world)
    corrupt(world)
    expected = _python_error(world, monkeypatch)
    assert expected is not None
    assert _error_of(world.validate_after_tick) == expected


@pytest.mark.parametrize(
    "corrupt",
    [
        _staff_past_the_map,
        _staff_in_world_dict,
        _agent_gone_past_the_map,
        _agents_short_in_world_dict,
    ],
)
def test_rows_the_maps_have_left_behind_are_not_trusted(finished_world, corrupt) -> None:
    """The namespaces' kernels read the IDs from the rows, which these writes do not reach."""
    world = _checked_world(finished_world)
    corrupt(world)
    assert not any(rust_validated(world, name) for name in OWNED)


def test_the_rows_are_trusted_again_on_the_next_world(finished_world) -> None:
    world = _checked_world(finished_world)
    _staff_in_world_dict(world)
    restored = WorldState.from_dict(copy.deepcopy(finished_world))
    assert all(rust_validated(restored, name) for name in OWNED) is native.available()


# ---- What a row cannot see ----------------------------------------------------------------------


def test_a_write_past_a_tracked_dict_is_seen_only_by_the_python_rules(
    finished_world, monkeypatch
) -> None:
    """dict.__setitem__ on a node of the tree reaches neither the row nor any kernel.

    This is not new: the row kernels of P5 and the namespaces' lanes took over from the rules that
    would see it. src/ has no such write (test_tracked_write_lint.py), and verify() finds the
    difference. Kept here so that closing the gap, or its growing, shows as a change.
    """
    world = _checked_world(finished_world)
    record = _first(world.model_state[PSYCHOLOGY]["agents"])
    dict.__setitem__(record, "long_term_distress", 2.0)
    expected = _python_error(world, monkeypatch)
    assert expected is not None and expected[0] == PSYCHOLOGY_RANGE
    if not native.available():
        assert _error_of(world.validate_after_tick) == expected
        return
    assert rust_validated(world, PSYCHOLOGY)
    assert _error_of(world.validate_after_tick) is None
    assert not world.__dict__["_core"].mirror(PSYCHOLOGY).verify()
