"""A world's entities as Rust rows: every Python write reaches its row, and nothing else changes.

The objects stay the dataclasses of domain/models.py, so these compare each row with the object it
mirrors (native/src/state/entities.rs, docs/rust-port/lanes/p6-entity-windows.md).
"""

from __future__ import annotations

import copy
import os
import pickle
import subprocess
import sys
from dataclasses import asdict, fields
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.state_mirror import IdMap, TrackedList
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.models import (
    ActiveTask,
    Activity,
    AgentState,
    FacilityState,
    InitialNeedProfile,
    NeedState,
    StaffState,
    SupportRequestState,
    SupportRequestStatus,
    ZoneEdgeState,
    ZoneState,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink

needs_kernels = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _valid_world() -> WorldState:
    return WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))


def _text(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _world() -> WorldState:
    """One of each entity. Its request has no timeout event, so this world does not validate."""
    world = _valid_world()
    world.support_requests["R1"] = _request("R1", next(iter(world.agents)))
    world.active_tasks["T1"] = _task("T1")
    return world


def _request(request_id: str, agent_id: str) -> SupportRequestState:
    return SupportRequestState(
        id=request_id, agent_id=agent_id, requested_minute=0, timeout_minute=30
    )


def _task(task_id: str) -> ActiveTask:
    return ActiveTask(
        id=task_id,
        kind="travel",
        agent_id=None,
        resource_id=None,
        start_minute=0,
        due_minute=10,
    )


def _store(world: WorldState) -> Any:
    return world._core.entities()


def _rules() -> Any:
    return native.state("entities")


def _agent(world: WorldState) -> AgentState:
    return next(iter(world.agents.values()))


def _facility(world: WorldState) -> FacilityState:
    return next(iter(world.facilities.values()))


def _staff(world: WorldState) -> StaffState:
    return next(iter(world.staff.values()))


def _in_step(world: WorldState) -> None:
    store = _store(world)
    assert store.tainted is None
    assert store.verify(world) is None


def _row_of(world: WorldState, field: str, key: str) -> dict[str, Any]:
    return _store(world).row(field, key)


@needs_kernels
def test_a_new_world_holds_id_maps_with_a_row_equal_to_each_object() -> None:
    world = _world()
    for name in ("agents", "facilities", "staff", "support_requests", "active_tasks"):
        assert type(getattr(world, name)) is IdMap
        assert isinstance(getattr(world, name), dict)
        assert _store(world).keys(name) == list(getattr(world, name))
    _in_step(world)
    agent = _agent(world)
    row = _row_of(world, "agents", agent.id)
    assert row["zone_id"] == agent.zone_id
    assert row["needs"]["hunger"] == agent.needs.hunger
    assert row["profile"]["id"] == agent.profile.id
    assert row["profile"]["provenance"] == agent.profile.provenance
    assert row["activity"] is agent.activity


@needs_kernels
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.25, 0.25), (0.0, 0.0), (1.0, 1.0)],
)
def test_a_write_to_a_float_reaches_the_row(value: float, expected: float) -> None:
    world = _world()
    agent = _agent(world)
    agent.stress = value
    agent.needs.hunger = value
    assert agent.stress == expected
    row = _row_of(world, "agents", agent.id)
    assert (row["stress"], row["needs"]["hunger"]) == (expected, expected)
    _in_step(world)


@needs_kernels
@pytest.mark.parametrize(
    ("pick", "field_name", "value", "table"),
    [
        ("agent", "zone_id", "elsewhere", "agents"),
        ("agent", "activity", Activity.SLEEPING, "agents"),
        ("agent", "target_id", "somewhere", "agents"),
        ("agent", "target_id", None, "agents"),
        ("agent", "next_decision_at", 77, "agents"),
        ("agent", "support_request_id", "R1", "agents"),
        ("agent", "support_cooldown_until", 5, "agents"),
        ("agent", "trust", 0.75, "agents"),
        ("facility", "capacity", 9, "facilities"),
        ("facility", "working_units", 1, "facilities"),
        ("facility", "kind", "other", "facilities"),
        ("staff", "skills", ("a", "b"), "staff"),
        ("staff", "busy_until", 12, "staff"),
        ("staff", "active_task_id", "T1", "staff"),
        ("request", "status", SupportRequestStatus.RESOLVED, "support_requests"),
        ("request", "resolved_minute", 3, "support_requests"),
        ("request", "assigned_staff_id", "S", "support_requests"),
        ("request", "last_assignment_wait_reason_code", "why", "support_requests"),
        ("request", "last_assignment_attempt_minute", 2, "support_requests"),
        ("task", "staff_id", "S", "active_tasks"),
        ("task", "due_minute", 11, "active_tasks"),
        ("task", "resource_id", "r", "active_tasks"),
    ],
)
def test_a_write_to_a_field_reaches_the_row(
    pick: str, field_name: str, value: Any, table: str
) -> None:
    world = _world()
    item = {
        "agent": _agent(world),
        "facility": _facility(world),
        "staff": _staff(world),
        "request": world.support_requests["R1"],
        "task": world.active_tasks["T1"],
    }[pick]
    setattr(item, field_name, value)
    assert getattr(item, field_name) == value
    key = item.profile.id if pick == "agent" else item.id
    assert _row_of(world, table, key)[field_name] == value
    _in_step(world)


@needs_kernels
def test_enums_and_ids_keep_their_identity() -> None:
    world = _world()
    agent = _agent(world)
    agent.activity = Activity.RESTING
    assert agent.activity is Activity.RESTING
    assert _row_of(world, "agents", agent.id)["activity"] is Activity.RESTING
    request = world.support_requests["R1"]
    assert request.status is SupportRequestStatus.PENDING
    zone = "a zone made here"
    agent.zone_id = zone
    assert agent.zone_id is zone
    _rules().add_agent_stress(world, agent.id, 0.0)
    assert agent.activity is Activity.RESTING
    assert agent.zone_id is zone


@needs_kernels
def test_a_property_setter_reaches_the_row_through_the_field() -> None:
    world = _world()
    agent = _agent(world)
    agent.acute_distress = 0.5
    assert agent.stress == 0.5
    assert _row_of(world, "agents", agent.id)["stress"] == 0.5
    _in_step(world)
    with pytest.raises(ValueError, match="acute_distress must be finite"):
        agent.acute_distress = 2.0
    with pytest.raises(AttributeError, match="has no setter"):
        agent.id = "new"
    _in_step(world)


@needs_kernels
def test_replacing_needs_moves_the_row_to_the_new_object() -> None:
    world = _world()
    agent = _agent(world)
    old = agent.needs
    new = NeedState(hunger=0.5)
    agent.needs = new
    assert _row_of(world, "agents", agent.id)["needs"]["hunger"] == 0.5
    new.fatigue = 0.25
    assert _row_of(world, "agents", agent.id)["needs"]["fatigue"] == 0.25
    # The old object is out of the world: its writes are its own.
    old.fatigue = 0.75
    assert _row_of(world, "agents", agent.id)["needs"]["fatigue"] == 0.25
    _in_step(world)


LIST_WRITES = [
    lambda ids: ids.append("x"),
    lambda ids: ids.extend(["x", "y"]),
    lambda ids: ids.insert(0, "x"),
    lambda ids: (ids.append("x"), ids.remove("x")),
    lambda ids: (ids.append("x"), ids.pop()),
    lambda ids: (ids.append("x"), ids.pop(0)),
    lambda ids: (ids.extend(["b", "a"]), ids.sort()),
    lambda ids: (ids.extend(["b", "a"]), ids.reverse()),
    lambda ids: (ids.extend(["b", "a"]), ids.clear()),
    lambda ids: ids.__setitem__(slice(None), ["task"]),
    lambda ids: (ids.append("x"), ids.__setitem__(0, "y")),
    lambda ids: (ids.extend(["a", "b"]), ids.__delitem__(0)),
    lambda ids: (ids.extend(["a", "b"]), ids.__delitem__(slice(0, 1))),
    lambda ids: ids.__iadd__(["x"]),
    lambda ids: (ids.append("x"), ids.__imul__(3)),
]


@needs_kernels
@pytest.mark.parametrize("write", LIST_WRITES)
@pytest.mark.parametrize(
    ("owner", "field_name"),
    [
        ("facility", "active_agent_ids"),
        ("facility", "queue_agent_ids"),
        ("facility", "resource_active_agent_ids"),
        ("facility", "resource_queue_agent_ids"),
        ("staff", "work_queue"),
    ],
)
def test_a_write_to_an_id_list_reaches_the_row(owner: str, field_name: str, write: Any) -> None:
    world = _world()
    item = _facility(world) if owner == "facility" else _staff(world)
    table = "facilities" if owner == "facility" else "staff"
    ids = getattr(item, field_name)
    assert type(ids) is TrackedList
    write(ids)
    assert _row_of(world, table, item.id)[field_name] == list(ids)
    _in_step(world)


@needs_kernels
def test_assigning_an_id_list_copies_it_and_frees_the_old_one() -> None:
    world = _world()
    facility = _facility(world)
    old = facility.queue_agent_ids
    given = ["a", "b"]
    facility.queue_agent_ids = given
    assert type(facility.queue_agent_ids) is TrackedList
    assert facility.queue_agent_ids is not given
    assert _row_of(world, "facilities", facility.id)["queue_agent_ids"] == ["a", "b"]
    # A write through the name it had before reaches neither, as with a tracked tree.
    given.append("c")
    assert list(facility.queue_agent_ids) == ["a", "b"]
    old.append("z")
    assert _row_of(world, "facilities", facility.id)["queue_agent_ids"] == ["a", "b"]
    facility.queue_agent_ids.append("d")
    assert _row_of(world, "facilities", facility.id)["queue_agent_ids"] == ["a", "b", "d"]
    _in_step(world)


@needs_kernels
def test_a_list_of_one_entity_assigned_to_another_is_copied() -> None:
    world = _world()
    first, second = list(world.facilities.values())[:2]
    first.queue_agent_ids.append("a")
    second.queue_agent_ids = first.queue_agent_ids
    assert second.queue_agent_ids is not first.queue_agent_ids
    first.queue_agent_ids.append("b")
    assert _row_of(world, "facilities", first.id)["queue_agent_ids"] == ["a", "b"]
    assert _row_of(world, "facilities", second.id)["queue_agent_ids"] == ["a"]
    _in_step(world)


@needs_kernels
def test_a_list_reassigned_as_itself_changes_nothing() -> None:
    world = _world()
    facility = _facility(world)
    ids = facility.queue_agent_ids
    facility.queue_agent_ids = ids
    assert facility.queue_agent_ids is ids
    ids.append("x")
    assert _row_of(world, "facilities", facility.id)["queue_agent_ids"] == ["x"]


@needs_kernels
def test_rust_reads_rows_and_writes_row_and_object_without_coming_back() -> None:
    world = _world()
    agent = _agent(world)
    before = _store(world).stats()["forwards"]
    agent.stress = 0.25
    assert _store(world).stats()["forwards"] == before + 1
    assert _rules().add_agent_stress(world, agent.id, 0.25) == 0.5
    assert agent.stress == 0.5
    assert _store(world).stats()["forwards"] == before + 1
    _in_step(world)
    assert _rules().hunger_of_agents(world) == [item.needs.hunger for item in world.agents.values()]
    with pytest.raises(native.Unsupported):
        _rules().add_agent_stress(world, "nobody", 0.1)


BAD_WRITES = [
    lambda world: setattr(_agent(world), "stress", 1),
    lambda world: setattr(_agent(world), "stress", "high"),
    lambda world: setattr(_agent(world), "stress", True),
    lambda world: setattr(_agent(world), "zone_id", 3),
    lambda world: setattr(_agent(world), "activity", "idle"),
    lambda world: setattr(_agent(world), "needs", SimpleNamespace(hunger=0.0)),
    lambda world: setattr(_agent(world).needs, "hunger", 1),
    lambda world: setattr(_agent(world), "next_decision_at", 1.5),
    lambda world: setattr(_agent(world), "next_decision_at", 2**80),
    lambda world: setattr(_facility(world), "queue_agent_ids", ("a",)),
    lambda world: setattr(_facility(world), "queue_agent_ids", None),
    lambda world: _facility(world).queue_agent_ids.append(3),
    lambda world: setattr(_staff(world), "skills", ["a"]),
    lambda world: _staff(world).work_queue.extend([None]),
    lambda world: setattr(world.support_requests["R1"], "status", "pending"),
    lambda world: setattr(world.active_tasks["T1"], "due_minute", None),
    lambda world: world.active_tasks.__setitem__("T2", SimpleNamespace()),
    lambda world: world.active_tasks.__setitem__(2, _task("T2")),
    lambda world: world.active_tasks.__setitem__("T2", world.active_tasks["T1"]),
    lambda world: setattr(world, "active_tasks", None),
]


@needs_kernels
@pytest.mark.parametrize("write", BAD_WRITES)
def test_a_value_that_does_not_fit_taints_the_store_and_python_keeps_it(write: Any) -> None:
    world = _world()
    write(world)
    store = _store(world)
    assert store.tainted is not None
    assert store.verify(world) is None
    with pytest.raises(native.Unsupported):
        _rules().hunger_of_agents(world)
    with pytest.raises(native.Unsupported):
        _rules().add_agent_stress(world, _agent(world).id, 0.1)


@needs_kernels
def test_the_python_object_keeps_the_exact_value_that_tainted_the_store() -> None:
    world = _valid_world()
    agent = _agent(world)
    agent.stress = 1
    assert agent.stress == 1 and type(agent.stress) is int
    assert world.canonical_json().count('"stress":1,') == 1
    assert _store(world).tainted is not None
    agent.needs.hunger = 2.5
    with pytest.raises(StateInvariantError, match="hunger"):
        world.validate()


@needs_kernels
def test_a_tainted_store_takes_writes_and_fits_again() -> None:
    world = _world()
    agent = _agent(world)
    facility = _facility(world)
    agent.stress = 1
    store = _store(world)
    assert store.keys("agents") == []
    # Everything keeps working on the objects.
    world.active_tasks["T2"] = _task("T2")
    del world.active_tasks["T1"]
    facility.queue_agent_ids.append("x")
    facility.active_agent_ids = ["y"]
    assert store.refit(world) is False
    assert store.tainted is not None
    agent.stress = 0.5
    assert store.refit(world) is True
    assert store.tainted is None
    _in_step(world)
    assert store.keys("active_tasks") == ["T2"]
    assert _row_of(world, "facilities", facility.id)["active_agent_ids"] == ["y"]
    agent.stress = 0.75
    assert _row_of(world, "agents", agent.id)["stress"] == 0.75
    facility.active_agent_ids.append("z")
    assert _row_of(world, "facilities", facility.id)["active_agent_ids"] == ["y", "z"]
    _in_step(world)


@needs_kernels
def test_what_left_a_tainted_world_can_not_write_to_a_row_after_it_fits_again() -> None:
    world = _world()
    first, second = list(world.agents.values())[:2]
    facility = _facility(world)
    old_needs = first.needs
    old_list = facility.queue_agent_ids
    old_task = world.active_tasks["T1"]
    second.stress = 1
    first.needs = NeedState()
    facility.queue_agent_ids = []
    del world.active_tasks["T1"]
    second.stress = 0.5
    assert _store(world).refit(world) is True
    assert old_needs._store is None and old_task._store is None
    # In a new slot order these would name other rows.
    old_needs.hunger = 0.5
    old_list.append("x")
    old_task.due_minute = 1
    _in_step(world)


@needs_kernels
def test_deleting_a_field_taints_the_store() -> None:
    world = _world()
    del _agent(world).needs.hunger
    assert "NeedState.hunger" in _store(world).tainted
    assert "hunger" not in vars(_agent(world).needs)


@needs_kernels
def test_an_id_map_attaches_what_comes_in_and_detaches_what_leaves() -> None:
    world = _world()
    tasks = world.active_tasks
    first = tasks["T1"]
    second = _task("T2")
    tasks["T2"] = second
    assert _store(world).keys("active_tasks") == ["T1", "T2"]
    second.due_minute = 99
    assert _row_of(world, "active_tasks", "T2")["due_minute"] == 99

    del tasks["T1"]
    assert _store(world).keys("active_tasks") == ["T2"]
    first.due_minute = 1
    assert _store(world).row("active_tasks", "T1") is None
    with pytest.raises(KeyError):
        del tasks["T1"]
    assert tasks.pop("T1", "gone") == "gone"
    with pytest.raises(KeyError):
        tasks.pop("T1")

    tasks["T1"] = first
    assert _row_of(world, "active_tasks", "T1")["due_minute"] == 1
    assert tasks.pop("T1") is first
    assert tasks.setdefault("T3", _task("T3")).id == "T3"
    assert tasks.setdefault("T3", first).id == "T3"
    tasks.update({"T4": _task("T4")}, T5=_task("T5"))
    tasks |= {"T6": _task("T6")}
    assert list(tasks) == ["T2", "T3", "T4", "T5", "T6"] == _store(world).keys("active_tasks")
    _in_step(world)
    key, value = tasks.popitem()
    assert (key, value.id) == ("T6", "T6")
    assert list(tasks) == _store(world).keys("active_tasks")
    kept = list(tasks.values())
    tasks.clear()
    assert _store(world).keys("active_tasks") == []
    kept[0].due_minute = 5
    _in_step(world)


@needs_kernels
def test_replacing_a_key_keeps_its_place_and_detaches_the_old_object() -> None:
    world = _world()
    tasks = world.active_tasks
    tasks["T2"] = _task("T2")
    old = tasks["T1"]
    new = _task("T1")
    new.due_minute = 50
    tasks["T1"] = new
    assert list(tasks) == ["T1", "T2"] == _store(world).keys("active_tasks")
    assert _row_of(world, "active_tasks", "T1")["due_minute"] == 50
    old.due_minute = 1
    assert _row_of(world, "active_tasks", "T1")["due_minute"] == 50
    tasks["T1"] = new
    _in_step(world)


@needs_kernels
def test_an_entity_in_two_worlds_taints_the_second_and_leaves_the_first() -> None:
    world = _world()
    other = _world()
    other.active_tasks["T9"] = world.active_tasks["T1"]
    assert _store(other).tainted is not None
    assert _store(world).tainted is None
    world.active_tasks["T1"].due_minute = 3
    assert _row_of(world, "active_tasks", "T1")["due_minute"] == 3
    assert other.active_tasks["T9"].due_minute == 3


@needs_kernels
def test_assigning_a_whole_map_keeps_what_stays_and_detaches_the_rest() -> None:
    world = _world()
    world.support_requests["R2"] = _request("R2", _agent(world).id)
    world.support_requests["R3"] = _request("R3", _agent(world).id)
    old_map = world.support_requests
    kept, gone = old_map["R3"], old_map["R2"]
    world.support_requests = {key: value for key, value in reversed(old_map.items()) if key != "R2"}
    assert type(world.support_requests) is IdMap
    assert world.support_requests is not old_map
    assert list(world.support_requests) == ["R3", "R1"] == _store(world).keys("support_requests")
    kept.status = SupportRequestStatus.ASSIGNED
    assert _row_of(world, "support_requests", "R3")["status"] is SupportRequestStatus.ASSIGNED
    gone.status = SupportRequestStatus.ASSIGNED
    assert _store(world).row("support_requests", "R2") is None
    _in_step(world)
    # What was replaced is a plain holder now.
    old_map["R9"] = _request("R9", _agent(world).id)
    assert "R9" not in world.support_requests
    assert _store(world).row("support_requests", "R9") is None

    # New entries among old ones take the dict's order.
    world.support_requests = {
        "new": _request("new", _agent(world).id),
        "R3": kept,
        "new2": _request("new2", _agent(world).id),
    }
    assert _store(world).keys("support_requests") == ["new", "R3", "new2"]
    _in_step(world)
    world.support_requests = world.support_requests
    _in_step(world)


@needs_kernels
def test_assigning_a_map_with_one_object_under_two_keys_taints() -> None:
    world = _world()
    task = world.active_tasks["T1"]
    world.active_tasks = {"a": task, "b": task}
    assert _store(world).tainted is not None
    assert world.active_tasks["a"] is world.active_tasks["b"]


@needs_kernels
def test_copy_and_swap_moves_the_rows_with_the_state() -> None:
    world = _valid_world()
    agent = _agent(world)
    agent.stress = 0.25
    document = world.to_dict()
    candidate = WorldState.from_dict(document)
    old_store = _store(world)
    assert _store(candidate) is not old_store
    _in_step(candidate)
    assert candidate.same_state(world)
    assert candidate.canonical_json() == world.canonical_json()

    candidate.agents[agent.id].stress = 0.5
    assert not candidate.same_state(world)
    world.__dict__.clear()
    world.__dict__.update(candidate.__dict__)
    assert _store(world) is not old_store
    assert _row_of(world, "agents", agent.id)["stress"] == 0.5
    # The name of the old object writes to its old world only.
    agent.stress = 0.75
    assert _row_of(world, "agents", agent.id)["stress"] == 0.5
    assert world.agents[agent.id].stress == 0.5
    _in_step(world)


@needs_kernels
def test_a_deep_copy_has_its_own_rows() -> None:
    world = _world()
    _facility(world).queue_agent_ids.append("x")
    twin = copy.deepcopy(world)
    assert _store(twin) is not _store(world)
    assert type(twin.agents) is IdMap
    assert type(_facility(twin).queue_agent_ids) is TrackedList
    assert _text(twin) == _text(world)
    assert twin.same_state(world)
    _agent(twin).stress = 0.5
    _facility(twin).queue_agent_ids.append("y")
    assert _agent(world).stress != 0.5
    assert list(_facility(world).queue_agent_ids) == ["x"]
    _in_step(twin)
    _in_step(world)


@needs_kernels
@pytest.mark.parametrize(
    "copier", [copy.copy, copy.deepcopy, lambda item: pickle.loads(pickle.dumps(item))]
)
def test_a_copy_or_pickle_of_an_attached_entity_is_out_of_the_world(copier: Any) -> None:
    world = _world()
    for item in (
        _agent(world),
        _agent(world).needs,
        _facility(world),
        _staff(world),
        world.support_requests["R1"],
        world.active_tasks["T1"],
    ):
        twin = copier(item)
        assert twin is not item
        assert twin == item
        assert type(twin) is type(item)
        assert twin._store is None
        # A shallow copy shares its lists, like a plain one; nothing else is shared with the world.
        assert vars(twin).keys() == vars(item).keys()
    agent = copier(_agent(world))
    agent.stress = 0.5
    agent.needs.hunger = 0.5
    assert _row_of(world, "agents", agent.id)["stress"] != 0.5
    _in_step(world)
    if copier is not copy.copy:
        facility = copier(_facility(world))
        facility.queue_agent_ids.append("x")
        assert _row_of(world, "facilities", facility.id)["queue_agent_ids"] == []
        assert type(facility.queue_agent_ids) is list
    pickled = pickle.loads(pickle.dumps(world.active_tasks))
    assert type(pickled) is dict
    _in_step(world)


@needs_kernels
def test_the_links_are_not_in_the_objects_state() -> None:
    world = _world()
    agent = _agent(world)
    names = {item.name for item in fields(AgentState)}
    assert set(vars(agent)) == names
    assert set(vars(agent.needs)) == {item.name for item in fields(NeedState)}
    assert asdict(agent.needs) == vars(agent.needs)
    assert "_store" not in repr(agent)
    assert agent == copy.deepcopy(agent)


def _bare_entities() -> list[Any]:
    world = _world()
    return [
        _agent(world).needs,
        _staff(world),
        world.active_tasks["T1"],
        ZoneState(id="z", name="Z", capacity=None),
        ZoneEdgeState(id="e", from_zone_id="a", to_zone_id="b", travel_minutes=1),
    ]


@pytest.mark.parametrize("position", range(5))
def test_to_dict_is_what_asdict_gives(position: int) -> None:
    item = _bare_entities()[position]
    assert item.to_dict() == asdict(item)
    assert list(item.to_dict()) == list(asdict(item))


def test_to_dict_copies_lists_and_keeps_a_value_that_is_not_a_list() -> None:
    staff = StaffState(id="s", zone_id="z", skills=("a",), work_queue=["t"])
    document = staff.to_dict()
    assert document["skills"] == ("a",) and type(document["skills"]) is tuple
    document["work_queue"].append("other")
    assert staff.work_queue == ["t"]
    bad = StaffState(id="s", zone_id="z", skills=("a",), work_queue="text")
    assert bad.to_dict() == asdict(bad)


@needs_kernels
def test_a_settled_request_still_reports_its_writes() -> None:
    world = _world()
    request = world.support_requests["R1"]
    reported: list[Any] = []
    request.report_writes_to(reported)
    request.status = SupportRequestStatus.RESOLVED
    assert reported == [request]
    assert _row_of(world, "support_requests", "R1")["status"] is SupportRequestStatus.RESOLVED
    assert "report" not in _store(world).stats()
    request.report_writes_to(None)
    request.status = SupportRequestStatus.PENDING
    assert reported == [request]
    _in_step(world)


@needs_kernels
def test_kernels_read_an_id_map_as_a_plain_dict() -> None:
    world = _world()
    assert native.world_agents_valid(
        world.agents, world.zones, Activity, InitialNeedProfile, NeedState
    )
    assert native.canonical_json(IdMap({"a": 1})) == b'{"a":1}'
    assert native.canonical_json(_facility(world).queue_agent_ids) == b"[]"


@needs_kernels
def test_rules_over_real_ticks_keep_every_row_equal_to_its_object(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    forwards = 0
    for _ in range(40):
        engine.step()
        _in_step(world)
        forwards = max(forwards, _store(world).stats()["forwards"])
    assert forwards > 100
    assert world.same_state(WorldState.from_dict(world.to_dict()))


def test_without_kernels_entities_are_plain_dataclasses() -> None:
    script = """
from dataclasses import asdict
from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask, AgentState, FacilityState, NeedState
from sheltersim.domain.models import StaffState, SupportRequestState
assert not native.available()
for kind in (AgentState, NeedState, FacilityState, StaffState, ActiveTask):
    assert kind.__setattr__ is object.__setattr__, kind
    assert kind.__new__ is object.__new__, kind
needs = NeedState()
needs.hunger = 1
assert vars(needs) == asdict(needs) and needs.hunger == 1
world = WorldState(scenario_id="s", seed=1, duration_minutes=1)
assert type(world.agents) is dict and "_core" not in world.__dict__
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "SHELTERSIM_NATIVE": "0",
            "PYTHONPATH": os.pathsep.join(sys.path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
