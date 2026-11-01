"""Rust owns the population, temporary space and overlay namespaces and model_configs.

native/src/state/population.rs holds them as `V` rows (docs/rust-port/lanes/m6-population.md). Their
rules stay in Python. What a tick gets is the mirror's generation: the end-of-tick check knows
they are unchanged without encoding them. So what these tests must show is that every way to change
a namespace moves the generation, and that the check then reads the state again.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.state_mirror import field_stamp, namespace_stamp, same_stamp
from sheltersim.core.tick_validation import TickValidationMemo
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.population import _append_presence_history, admit_pending_arrivals
from sheltersim.domain.simulator_state import validate_simulator_extensions
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

NAMESPACES = (
    "population_presence_v1",
    "population_lifecycle_v1",
    "population_dynamics_v1",
    "temporary_spaces_v1",
    "geometry_overlay_v1",
)


@pytest.fixture(scope="module")
def finished_world(tmp_path_factory, generated_scenario_preview) -> dict:
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    return run_scenario(scenario, tmp_path_factory.mktemp("run")).to_dict()


def _restored(document: dict) -> WorldState:
    return WorldState.from_dict(copy.deepcopy(document))


def _checked(document: dict) -> WorldState:
    world = _restored(document)
    world.validate_after_tick()
    world.validate_after_tick()
    return world


def _memo(world: WorldState) -> TickValidationMemo:
    return world.__dict__["_tick_memo"]


def _mirror(world: WorldState, name: str) -> Any:
    return world.__dict__["_core"].mirror(name)


def test_each_namespace_and_the_configs_have_a_row_with_the_bytes_of_the_tree(
    finished_world,
) -> None:
    world = _restored(finished_world)
    for name in NAMESPACES:
        mirror = _mirror(world, name)
        assert mirror.tainted is None
        assert mirror.row_json() == native.canonical_json(world.model_state[name])
        assert mirror.verify()
    configs = _mirror(world, "model_configs")
    assert configs.tainted is None
    assert configs.row_json() == native.canonical_json(world.model_configs)
    assert configs.holds(world.model_configs)


def test_the_saved_document_is_the_same_as_without_the_rows(finished_world) -> None:
    world = _restored(finished_world)
    assert json.loads(json.dumps(world.to_dict())) == json.loads(json.dumps(finished_world))
    assert world.same_state(_restored(finished_world))


def test_a_copy_owns_its_own_configs(finished_world) -> None:
    world = _restored(finished_world)
    copied = copy.deepcopy(world)
    copied.model_configs["staff"]["copied"] = 1
    assert "copied" not in world.model_configs["staff"]
    assert _mirror(copied, "model_configs").holds(copied.model_configs)
    assert _mirror(copied, "model_configs").verify()


def _writes(state: dict) -> list:
    """Each way to write to a tree. Each is done to a tree that may already have had the others."""

    def nested_set() -> None:
        state["nested"] = {"items": [1], "value": 1}

    def inner_set() -> None:
        state["nested"]["value"] = 2

    def append() -> None:
        state["nested"]["items"].append(2)

    def extend() -> None:
        state["nested"]["items"].extend([3, 4])

    def insert() -> None:
        state["nested"]["items"].insert(0, 0)

    def index_set() -> None:
        state["nested"]["items"][0] = 9

    def slice_set() -> None:
        state["nested"]["items"][:1] = [8]

    def sort() -> None:
        state["nested"]["items"].sort()

    def reverse() -> None:
        state["nested"]["items"].reverse()

    def delete_range() -> None:
        del state["nested"]["items"][:1]

    def list_pop() -> None:
        state["nested"]["items"].pop()

    def setdefault() -> None:
        state["nested"].setdefault("other", {})

    def update() -> None:
        state["nested"].update({"third": 3})

    def dict_pop() -> None:
        state["nested"].pop("third")

    def delete() -> None:
        del state["nested"]["other"]

    def clear() -> None:
        state["nested"].clear()

    def misfit() -> None:
        # Not JSON, so the row goes; the tree still tells.
        state["nested"][1] = object()

    return [
        nested_set,
        inner_set,
        append,
        extend,
        insert,
        index_set,
        slice_set,
        sort,
        reverse,
        delete_range,
        list_pop,
        setdefault,
        update,
        dict_pop,
        delete,
        clear,
        misfit,
    ]


@pytest.mark.parametrize("name", NAMESPACES)
def test_every_write_to_a_namespace_changes_its_stamp(finished_world, name) -> None:
    world = _restored(finished_world)
    for write in _writes(world.model_state[name]):
        before = namespace_stamp(world, name)
        assert before is not None
        assert same_stamp(before, namespace_stamp(world, name))
        write()
        assert not same_stamp(before, namespace_stamp(world, name)), write.__name__


def test_every_write_to_the_configs_changes_their_stamp(finished_world) -> None:
    world = _restored(finished_world)
    for write in _writes(world.model_configs):
        before = field_stamp(world, "model_configs")
        write()
        assert not same_stamp(before, field_stamp(world, "model_configs")), write.__name__


def test_a_read_does_not_change_a_stamp(finished_world) -> None:
    world = _restored(finished_world)
    name = "population_presence_v1"
    before = namespace_stamp(world, name), field_stamp(world, "model_configs")
    world.validate()
    world.to_dict()
    copy.deepcopy(world)
    json.dumps(world.model_state[name])
    native.canonical_json(world.model_configs)
    assert same_stamp(before[0], namespace_stamp(world, name))
    assert same_stamp(before[1], field_stamp(world, "model_configs"))


def test_a_namespace_replaced_or_stored_past_the_table_has_no_same_stamp(finished_world) -> None:
    world = _restored(finished_world)
    name = "population_lifecycle_v1"
    before = namespace_stamp(world, name)
    world.model_state[name] = copy.deepcopy(world.model_state[name])
    assert not same_stamp(before, namespace_stamp(world, name))
    dict.__setitem__(world.model_state, name, {})
    assert namespace_stamp(world, name) is None


def test_configs_assigned_whole_are_read_by_equality(finished_world) -> None:
    world = _checked(finished_world)
    assert field_stamp(world, "model_configs") is not None
    world.model_configs = copy.deepcopy(world.model_configs)
    assert field_stamp(world, "model_configs") is None
    world.validate_after_tick()
    assert _memo(world).configs_unchanged
    world.model_configs["resource_service"]["water_ml_per_service"] = -1
    with pytest.raises(StateInvariantError):
        world.validate_after_tick()


def test_the_tick_check_trusts_what_no_write_reached(finished_world) -> None:
    world = _checked(finished_world)
    memo = _memo(world)
    assert memo.configs_unchanged
    assert memo.presence_unchanged
    assert memo.lifecycle_unchanged
    assert memo.dynamics_unchanged
    assert memo.overlay_unchanged


@pytest.mark.parametrize(
    ("name", "flag"),
    [
        ("population_presence_v1", "presence_unchanged"),
        ("population_lifecycle_v1", "lifecycle_unchanged"),
        ("population_dynamics_v1", "dynamics_unchanged"),
        ("geometry_overlay_v1", "overlay_unchanged"),
    ],
)
def test_a_write_of_an_equal_value_reads_the_state_again(finished_world, name, flag) -> None:
    world = _checked(finished_world)
    state = world.model_state[name]
    part = "arrivals" if name == "population_dynamics_v1" else "history"
    state[part] = copy.deepcopy(state[part])
    world.validate_after_tick()
    assert not getattr(_memo(world), flag)
    world.validate_after_tick()
    assert getattr(_memo(world), flag)


def test_a_config_written_with_its_value_needs_no_new_checks(finished_world) -> None:
    world = _checked(finished_world)
    kept = world.model_configs["staff"]
    kept["written"] = 1
    del kept["written"]
    world.validate_after_tick()
    assert _memo(world).configs_unchanged


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda world: world.model_state["population_presence_v1"]["people"].pop(
            next(iter(world.agents))
        ),
        lambda world: world.model_state["population_lifecycle_v1"].update(schema_version=2),
        lambda world: world.model_state["population_dynamics_v1"].update(source_id="other"),
        lambda world: world.model_state["population_dynamics_v1"]["profile_traits"].clear()
        or world.model_state["population_dynamics_v1"]["initial_agent_ids"].clear(),
        lambda world: world.model_state["geometry_overlay_v1"].update(unknown=1),
        lambda world: world.model_configs["geometry_overlay"].update(schema_version=99),
    ],
)
def test_a_write_that_breaks_a_skipped_namespace_stops_the_next_tick(
    finished_world, corrupt
) -> None:
    world = _checked(finished_world)
    corrupt(world)
    with pytest.raises((StateInvariantError, ValueError)):
        world.validate_after_tick()


def test_a_namespace_written_past_the_tree_is_left_to_the_full_validation(finished_world) -> None:
    # The JSON compare used to see this. dict.__setitem__ on a node passes the mirror, as for the
    # checks that read a row (t-tick-validation.md), and no code in src/ does it.
    world = _checked(finished_world)
    dict.__setitem__(world.model_state["population_lifecycle_v1"], "schema_version", 2)
    world.validate_after_tick()
    with pytest.raises((StateInvariantError, ValueError)):
        world.validate()


def test_the_rule_a_memo_skips_passes_on_a_finished_run(finished_world) -> None:
    world = _checked(finished_world)
    validate_simulator_extensions(world)
    assert not world.model_state["population_dynamics_v1"]["pending_arrivals"]


def test_a_pending_arrival_is_read_at_every_check(finished_world) -> None:
    world = _checked(finished_world)
    state = world.model_state["population_dynamics_v1"]
    # A broken one: the rule reads its fields, so the skip must not apply.
    state["pending_arrivals"].append({"broken": True})
    with pytest.raises((StateInvariantError, ValueError, KeyError)):
        world.validate_after_tick()


def test_the_presence_history_grows_in_place_up_to_its_bound() -> None:
    class Transition:
        def __init__(self, number: int) -> None:
            self.number = number

        def model_dump(self, mode: str) -> dict[str, int]:
            assert mode == "json"
            return {"number": self.number}

    held = {"history": [{"number": number} for number in range(20_000)]}
    history = held["history"]
    _append_presence_history(held, Transition(20_000))  # type: ignore[arg-type]
    assert held["history"] is history
    assert len(history) == 20_000
    assert history[0] == {"number": 1}
    assert history[-1] == {"number": 20_000}


def test_a_waiting_list_that_stays_the_same_is_not_written(finished_world) -> None:
    world = _restored(finished_world)
    state = world.model_state["population_dynamics_v1"]
    waiting = {
        "profile": {"id": "z"},
        "traits": {},
        "zone_id": next(iter(world.zones)),
        "created_minute": 0,
        "event_id": "e",
        # Still walking, so it stays waiting without reading the zone.
        "approach_route_id": "r",
        "approach_started_minute": 0,
        "approach_due_minute": 10_000_000,
    }
    state["pending_arrivals"].append(waiting)
    kept = state["pending_arrivals"]
    stamp = namespace_stamp(world, "population_dynamics_v1")
    assert admit_pending_arrivals(world, world.minute_index) == ()
    assert state["pending_arrivals"] is kept
    # The summaries of arrivals are rewritten, but there are none here.
    assert same_stamp(stamp, namespace_stamp(world, "population_dynamics_v1"))
