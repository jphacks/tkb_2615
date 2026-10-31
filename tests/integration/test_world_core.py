"""A world's core: the sample namespace owned end to end, and real rules on tracked trees.

These own more namespaces for one test. The sample's typed row is
native/src/state/sample_namespace.rs; the row that holds any JSON can own a real namespace, which
runs its rules, the kernels and the saves on a tree whose every write reaches Rust. The namespaces
owned in production are in test_psychology_row.py.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core import state_mirror
from sheltersim.core.engine import TickEngine
from sheltersim.core.state_mirror import (
    DetachedNodeError,
    NamespaceTable,
    TrackedDict,
    TrackedList,
    detach,
    register_rust_validator,
    rust_validated,
)
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.psychological_state import PSYCHOLOGICAL_STATE_KEY
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.segments import install_segments
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

SAMPLE = "sample_v1"


def _sample_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "total": 1.0,
        "people": {
            "a": {"level": 0.25, "visits": 1, "note": None},
            "b": {"level": 0.75, "visits": 2, "note": "n"},
        },
        "notes": [{"k": [1, 2.5]}],
    }


@pytest.fixture
def owned(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(state_mirror.OWNED_NAMESPACES, SAMPLE, ("sample_namespace", "ROW"))
    yield


@pytest.fixture
def world(owned: None) -> WorldState:
    world = WorldState("s", 1, 100)
    world.model_state[SAMPLE] = _sample_state()
    world.model_state["other_v1"] = {"plain": True}
    return world


def _kernels() -> Any:
    return native.state("sample_namespace")


def _mirror(world: WorldState) -> Any:
    return world._core.mirror(SAMPLE)


def _level(world: WorldState, person: str = "a") -> float:
    return world.model_state[SAMPLE]["people"][person]["level"]


def test_with_nothing_owned_model_state_is_a_plain_dict_and_there_is_a_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state_mirror, "OWNED_NAMESPACES", {})
    monkeypatch.setattr(state_mirror, "OWNED_FIELDS", {})
    world = WorldState("s", 1, 100)
    assert type(world.model_state) is dict
    assert world._core.owned_names() == []
    world.model_state["x"] = {"a": 1}
    assert type(world.model_state["x"]) is dict
    assert type(copy.deepcopy(world).model_state) is dict


def test_an_owned_namespace_is_a_tree_and_the_others_stay_as_they_were(world: WorldState) -> None:
    assert type(world.model_state) is NamespaceTable
    assert type(world.model_state[SAMPLE]) is TrackedDict
    assert type(world.model_state[SAMPLE]["notes"]) is TrackedList
    assert type(world.model_state["other_v1"]) is dict
    assert _mirror(world).has_row and _mirror(world).verify()


def test_a_python_write_reaches_the_row(world: WorldState) -> None:
    state = world.model_state[SAMPLE]
    state["people"]["a"]["level"] = 0.5
    state["people"]["a"]["note"] = "x"
    state["people"]["c"] = {"level": 0.1, "visits": 0, "note": None}
    del state["people"]["b"]
    state["notes"][0]["k"].append(3)
    state["total"] = 0.6
    assert json.loads(_kernels().row_json(world)) == state
    assert _mirror(world).verify() and _mirror(world).tainted is None


def test_a_rust_write_reaches_the_tree_without_coming_back(world: WorldState) -> None:
    forwards = _mirror(world).stats()["forwards"]
    _kernels().set_level(world, "a", 0.5)
    state = world.model_state[SAMPLE]
    assert state["people"]["a"]["level"] == 0.5
    assert state["total"] == 1.25
    assert _mirror(world).stats()["forwards"] == forwards
    assert _mirror(world).verify()
    assert type(state["people"]["a"]) is TrackedDict


def test_a_rust_write_bypasses_an_override_of_the_node(world: WorldState) -> None:
    # The sink would see any call of __setitem__.
    class Counting:
        calls = 0

        def __getattr__(self, name: str) -> Any:
            Counting.calls += 1
            raise AssertionError(f"the override reached the sink: {name}")

    world.model_state[SAMPLE]["people"]["a"]._sink, kept = Counting(), _mirror(world)
    try:
        _kernels().set_level(world, "a", 0.5)
    except AssertionError:  # pragma: no cover
        pytest.fail("a rust write ran the override")
    assert Counting.calls == 0
    world.model_state[SAMPLE]["people"]["a"]._sink = kept
    assert _level(world) == 0.5


@pytest.mark.parametrize(
    ("write", "reason"),
    [
        (lambda state: state["people"]["a"].__setitem__("level", 1), "not a float"),
        (lambda state: state["people"]["a"].__setitem__("visits", 1.0), "not an int"),
        (lambda state: state["people"]["a"].__setitem__("visits", True), "not an int"),
        (lambda state: state["people"]["a"].__setitem__("extra", 1), "no field"),
        (lambda state: state["people"].__setitem__("c", {"level": 1.0}), "other fields"),
        (lambda state: state.__setitem__("new", 1), "no field"),
        (lambda state: state.__delitem__("total"), "which fields"),
        (lambda state: state["people"].__setitem__(1, {}), "not a str"),
        (lambda state: state["notes"].append((1, 2)), "plain JSON"),
    ],
)
def test_a_write_that_does_not_fit_taints_the_namespace(
    world: WorldState, write: Any, reason: str
) -> None:
    expected = copy.deepcopy(world.model_state[SAMPLE])
    write(expected)
    write(world.model_state[SAMPLE])
    mirror = _mirror(world)
    assert reason in mirror.tainted
    assert not mirror.has_row
    # The tree keeps what Python wrote, and kernels step aside.
    assert world.model_state[SAMPLE] == expected
    with pytest.raises(native.Unsupported):
        _kernels().set_level(world, "a", 0.5)
    with pytest.raises(native.Unsupported):
        _kernels().row_json(world)


def test_a_tainted_namespace_keeps_taking_writes_and_can_fit_again(world: WorldState) -> None:
    state = world.model_state[SAMPLE]
    state["people"]["a"]["level"] = 1
    state["people"]["a"]["visits"] = 5
    state["notes"].append({"k": 1})
    assert _mirror(world).refit() is False
    state["people"]["a"]["level"] = 1.0
    assert _mirror(world).refit() is True
    assert _mirror(world).verify() and _mirror(world).tainted is None
    state["people"]["a"]["visits"] = 6
    assert _mirror(world).verify()


def test_a_document_that_does_not_fit_starts_tainted_and_is_stored_as_it_is(owned: None) -> None:
    world = WorldState("s", 1, 100)
    world.model_state[SAMPLE] = {"schema_version": "one"}
    assert "not an int" in _mirror(world).tainted or "other fields" in _mirror(world).tainted
    assert world.model_state[SAMPLE] == {"schema_version": "one"}
    world.model_state[SAMPLE] = None
    assert world.model_state[SAMPLE] is None and _mirror(world).tainted is not None
    world.model_state[SAMPLE] = _sample_state()
    assert _mirror(world).tainted is None and _mirror(world).verify()


def test_writing_a_namespace_back_as_itself_costs_nothing(world: WorldState) -> None:
    state = world.model_state[SAMPLE]
    writes = _mirror(world).stats()["whole_writes"]
    world.model_state[SAMPLE] = state
    assert world.model_state[SAMPLE] is state
    assert _mirror(world).stats()["whole_writes"] == writes


def test_a_whole_namespace_write_makes_a_new_tree_and_row(world: WorldState) -> None:
    old = world.model_state[SAMPLE]
    new = _sample_state()
    new["total"] = 9.0
    world.model_state[SAMPLE] = new
    assert world.model_state[SAMPLE] is not new
    assert json.loads(_kernels().row_json(world))["total"] == 9.0
    # The old tree is out of it: a kept name writes to nobody.
    old["total"] = 7.0
    assert json.loads(_kernels().row_json(world))["total"] == 9.0


def test_removing_a_namespace_leaves_no_row(world: WorldState) -> None:
    del world.model_state[SAMPLE]
    assert not _mirror(world).has_row and _mirror(world).tainted is None
    with pytest.raises(native.Unsupported):
        _kernels().row_json(world)
    world.model_state[SAMPLE] = _sample_state()
    assert _mirror(world).has_row
    assert world.model_state.pop(SAMPLE)["total"] == 1.0
    assert not _mirror(world).has_row


def test_detach_keeps_keys_and_row_and_a_read_rebuilds(world: WorldState) -> None:
    expected = copy.deepcopy(world.model_state[SAMPLE])
    assert detach(world, SAMPLE) is True
    mirror = _mirror(world)
    assert mirror.is_detached
    table = world.model_state
    # Keys need no tree.
    assert SAMPLE in table and list(table) == [SAMPLE, "other_v1"] and len(table) == 2
    assert mirror.is_detached and mirror.stats()["rebuilds"] == 0
    # Rust goes on without a tree.
    _kernels().set_level(world, "a", 0.5)
    assert mirror.is_detached
    expected["people"]["a"]["level"] = 0.5
    expected["total"] = 1.25
    assert world.model_state.get(SAMPLE) == expected
    assert not mirror.is_detached and mirror.stats()["rebuilds"] == 1
    assert type(world.model_state) is NamespaceTable
    assert type(world.model_state[SAMPLE]["people"]) is TrackedDict
    world.model_state[SAMPLE]["total"] = 2.0
    assert mirror.verify()


@pytest.mark.parametrize(
    "read",
    [
        lambda table: table[SAMPLE],
        lambda table: table.get(SAMPLE),
        lambda table: dict(table)[SAMPLE],
        lambda table: dict(table.items())[SAMPLE],
        lambda table: next(iter(table.values())),
        lambda table: table.copy()[SAMPLE],
        lambda table: copy.copy(table)[SAMPLE],
        lambda table: copy.deepcopy(table)[SAMPLE],
        lambda table: json.loads(json.dumps(table))[SAMPLE],
        lambda table: table.pop(SAMPLE),
        lambda table: table.setdefault(SAMPLE),
        lambda table: {**table}[SAMPLE],
        lambda table: (table | {})[SAMPLE],
    ],
)
def test_no_read_of_a_detached_namespace_sees_a_placeholder(world: WorldState, read: Any) -> None:
    detach(world, SAMPLE)
    assert read(world.model_state)["total"] == 1.0


def test_comparing_a_detached_table_builds_it(world: WorldState) -> None:
    expected = {SAMPLE: _sample_state(), "other_v1": {"plain": True}}
    detach(world, SAMPLE)
    assert world.model_state == expected
    detach(world, SAMPLE)
    assert expected == world.model_state
    detach(world, SAMPLE)
    assert not world.model_state != expected


def test_a_kernel_turns_a_detached_table_down(world: WorldState) -> None:
    detach(world, SAMPLE)
    assert native.canonical_json(world.model_state) is None
    assert _mirror(world).is_detached


def test_a_node_kept_across_a_detach_raises_on_a_write_and_reads_the_past(
    world: WorldState,
) -> None:
    kept = world.model_state[SAMPLE]["people"]["a"]
    detach(world, SAMPLE)
    with pytest.raises(DetachedNodeError):
        kept["level"] = 0.5
    with pytest.raises(DetachedNodeError):
        kept.pop("level")
    _kernels().set_level(world, "a", 0.5)
    assert kept["level"] == 0.25
    assert _level(world) == 0.5


def test_a_tainted_namespace_does_not_detach(world: WorldState) -> None:
    world.model_state[SAMPLE]["total"] = 1
    assert detach(world, SAMPLE) is False
    assert world.model_state[SAMPLE]["total"] == 1


def test_a_callback_can_swap_the_state_under_a_kernel(world: WorldState) -> None:
    other = WorldState("s", 1, 100)
    other.model_state[SAMPLE] = _sample_state()
    other.model_state["other_v1"] = {"plain": True}
    old_core = world._core

    def swap() -> None:
        world.__dict__.clear()
        world.__dict__.update(other.__dict__)

    _kernels().set_level_after(world, swap, "a", 0.5)
    assert world._core is not old_core
    assert _level(world) == 0.5
    assert json.loads(_kernels().row_json(world))["people"]["a"]["level"] == 0.5


def test_to_dict_and_from_dict_round_trip(world: WorldState) -> None:
    _kernels().set_level(world, "a", 0.5)
    document = world.to_dict()
    assert type(document["model_state"][SAMPLE]) is dict
    restored = WorldState.from_dict(document)
    assert restored._core is not world._core
    assert type(restored.model_state[SAMPLE]) is TrackedDict
    assert restored.same_state(world) and world.same_state(restored)
    assert _kernels().row_json(restored) == _kernels().row_json(world)
    restored.model_state[SAMPLE]["total"] = 8.0
    assert world.model_state[SAMPLE]["total"] != 8.0
    assert not restored.same_state(world)


def test_a_borrowing_from_dict_leaves_the_document_alone(world: WorldState) -> None:
    document = world.to_dict()
    before = json.dumps(document, sort_keys=True)
    restored = WorldState.from_dict(document, _borrow=True, _validate=False)
    assert type(document["model_state"]) is dict
    assert type(document["model_state"][SAMPLE]) is dict
    assert json.dumps(document, sort_keys=True) == before
    assert restored.same_state(world)


def test_copy_and_swap_moves_the_core_with_the_state(world: WorldState) -> None:
    held = world
    candidate = WorldState.from_dict(world.to_dict())
    candidate.model_state[SAMPLE]["total"] = 3.0
    held.__dict__.clear()
    held.__dict__.update(candidate.__dict__)
    assert held._core is candidate._core
    assert json.loads(_kernels().row_json(held))["total"] == 3.0
    held.model_state[SAMPLE]["total"] = 4.0
    assert _mirror(held).verify()


def test_a_deep_copy_is_owned_by_its_own_core(world: WorldState) -> None:
    _kernels().set_level(world, "a", 0.5)
    other = copy.deepcopy(world)
    assert other._core is not world._core
    assert type(other.model_state) is NamespaceTable
    assert type(other.model_state[SAMPLE]) is TrackedDict
    assert other.same_state(world)
    other.model_state[SAMPLE]["total"] = 9.0
    assert world.model_state[SAMPLE]["total"] == 1.25
    assert _mirror(other).verify() and _mirror(world).verify()


def test_assigning_model_state_owns_the_new_document(world: WorldState) -> None:
    old = world.model_state
    world.model_state = {SAMPLE: _sample_state(), "other_v1": {}}
    assert type(world.model_state) is NamespaceTable
    assert world.model_state is not old
    assert type(world.model_state[SAMPLE]) is TrackedDict
    world.model_state[SAMPLE]["total"] = 2.0
    assert json.loads(_kernels().row_json(world))["total"] == 2.0
    # The old table holds plain containers now.
    old[SAMPLE]["total"] = 7.0
    old[SAMPLE] = {}
    assert json.loads(_kernels().row_json(world))["total"] == 2.0
    world.model_state = {"other_v1": {}}
    assert not _mirror(world).has_row


def test_canonical_json_of_tracked_nodes_is_the_json_of_the_plain_state(world: WorldState) -> None:
    state = world.model_state[SAMPLE]
    plain = json.loads(json.dumps(state))
    expected = json.dumps(
        plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    assert native.canonical_json(state) == expected
    assert _kernels().row_json(world) == expected
    assert (
        native.canonical_json(world.model_state)
        == json.dumps(
            {SAMPLE: plain, "other_v1": {"plain": True}},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )


def test_worlds_on_threads_keep_their_own_state(owned: None) -> None:
    def run(number: int) -> float:
        world = WorldState("s", 1, 100)
        world.model_state[SAMPLE] = _sample_state()
        for step in range(2000):
            world.model_state[SAMPLE]["people"]["a"]["visits"] = step + number
            _kernels().set_level(world, "b", step / 4000)
        assert _mirror(world).verify()
        return world.model_state[SAMPLE]["people"]["a"]["visits"]

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(run, range(4))) == [1999, 2000, 2001, 2002]


# ---- The registries -------------------------------------------------------------------------


@pytest.fixture
def validator() -> Iterator[Any]:
    yield register_rust_validator
    register_rust_validator(SAMPLE, None)
    register_rust_validator(
        PSYCHOLOGICAL_STATE_KEY, native.state("psychological_distress").validate
    )


def test_a_rust_validator_accepts_or_leaves_it_to_python(world: WorldState, validator: Any) -> None:
    assert not rust_validated(world, SAMPLE)
    validator(SAMPLE, _kernels().validate)
    assert rust_validated(world, SAMPLE)
    world.model_state[SAMPLE]["people"]["a"]["level"] = 2.0
    assert not rust_validated(world, SAMPLE)
    world.model_state[SAMPLE]["people"]["a"]["level"] = 1
    assert _mirror(world).tainted is not None
    # Unsupported from a tainted namespace: the Python rules decide.
    assert not rust_validated(world, SAMPLE)


def _bad_psychology_world(generated_scenario: Any) -> tuple[WorldState, str]:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[PSYCHOLOGICAL_STATE_KEY]["schema_version"] = 99
    with pytest.raises(StateInvariantError) as raised:
        world.validate(validate_event_history=False)
    return world, str(raised.value)


def test_a_namespace_rust_accepts_skips_its_python_validation(
    generated_scenario: Any, validator: Any
) -> None:
    world, _ = _bad_psychology_world(generated_scenario)
    validator(PSYCHOLOGICAL_STATE_KEY, lambda _world: True)
    world.validate(validate_event_history=False)


def test_a_namespace_rust_turns_down_keeps_the_exact_error(
    generated_scenario: Any, validator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, message = _bad_psychology_world(generated_scenario)

    def unsupported(_world: Any) -> bool:
        raise native.Unsupported("tainted")

    validator(PSYCHOLOGICAL_STATE_KEY, unsupported)
    with pytest.raises(StateInvariantError) as raised:
        world.validate(validate_event_history=False)
    assert str(raised.value) == message

    validator(PSYCHOLOGICAL_STATE_KEY, lambda _world: False)
    with pytest.raises(StateInvariantError) as raised:
        world.validate(validate_event_history=False)
    assert str(raised.value) == message


def test_corruption_of_an_owned_namespace_keeps_the_exact_error(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt(world: WorldState) -> None:
        agents = world.model_state[PSYCHOLOGICAL_STATE_KEY]["agents"]
        record = agents[next(iter(agents))]
        record[next(iter(record))] = "not a number"

    plain = WorldState.from_scenario(generated_scenario)
    corrupt(plain)
    with pytest.raises(StateInvariantError) as expected:
        plain.validate(validate_event_history=False)

    monkeypatch.setitem(
        state_mirror.OWNED_NAMESPACES,
        PSYCHOLOGICAL_STATE_KEY,
        ("sample_namespace", "ROW"),
    )
    owned = WorldState.from_scenario(generated_scenario)
    mirror = owned._core.mirror(PSYCHOLOGICAL_STATE_KEY)
    # The sample's row can't read this namespace: it is tainted from the start, on purpose.
    assert mirror.tainted is not None
    corrupt(owned)
    with pytest.raises(StateInvariantError) as raised:
        owned.validate(validate_event_history=False)
    assert str(raised.value) == str(expected.value)


def test_a_segment_replaces_a_step_and_unsupported_runs_the_python_step() -> None:
    class Base:
        def step(self, value: int) -> str:
            return f"python {value}"

    class Controller(Base):
        pass

    answers: list[Any] = ["rust"]

    def kernel(_controller: Any, value: int) -> str:
        answer = answers[0]
        if answer is None:
            raise native.Unsupported("not this one")
        return f"{answer} {value}"

    install_segments(Controller, {"step": kernel})
    assert Controller().step(1) == "rust 1"
    answers[0] = None
    assert Controller().step(2) == "python 2"
    Base.step = lambda _self, value: f"patched {value}"  # type: ignore[method-assign]
    assert Controller().step(3) == "patched 3"
    with pytest.raises(ValueError, match="no step"):
        install_segments(Controller, {"nope": kernel})


# ---- Real rules on a tree -------------------------------------------------------------------


def _run_world(scenario: Any, minutes: int, check: Any = None) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
        if check is not None:
            check(world)
    return world


# Every namespace of the first ticks that the ledger's size doesn't make slow here.
REAL_NAMESPACES = (
    "agent_dynamics_v1",
    "body_state_v1",
    "information_spread_v1",
    "movement_v1",
    "psychological_distress_v1",
    "shared_operations_v1",
    "social_network_v1",
    "social_propagation_v1",
    "staff_operations_v1",
)


def test_real_rules_on_owned_trees_give_the_same_world_and_the_row_follows(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    minutes = 25
    plain = _run_world(generated_scenario, minutes)
    for name in REAL_NAMESPACES:
        monkeypatch.setitem(state_mirror.OWNED_NAMESPACES, name, ("sample_namespace", "VALUE_ROW"))

    def check(world: WorldState) -> None:
        assert type(world.model_state) is NamespaceTable
        for name in world._core.owned_names():
            mirror = world._core.mirror(name)
            if name in world.model_state:
                assert mirror.tainted is None, name
                assert mirror.verify(), name

    tracked = _run_world(generated_scenario, minutes, check)
    assert tracked.canonical_json() == plain.canonical_json()
    assert tracked.state_digest() == plain.state_digest()
    assert sum(tracked._core.mirror(name).stats()["forwards"] for name in REAL_NAMESPACES) > 100
