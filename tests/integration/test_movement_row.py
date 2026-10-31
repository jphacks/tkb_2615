"""movement_v1 owned by Rust, with its rules still in Python (docs/rust-port/lanes/i-movement.md).

The row against the tree at every tick of the gate is benchmarks/rule_shadow/movement_shadow.py;
here, the shapes of its records, what taints it, and the steps that hold the tree.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain import movement
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.movement import MOVEMENT_STATE_KEY as KEY
from sheltersim.domain.movement import validate_movement_runtime_state
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases import update
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

ROOT = Path(__file__).resolve().parents[2]


def _position(point: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "zone_id": "hall",
        "point": point,
        "edge_id": None,
        "progress": 0.0,
        "position_kind": "physical" if point else "unavailable",
    }


def _journey(**more: Any) -> dict[str, Any]:
    return {
        "event_id": "e1",
        "task_id": "t1",
        "purpose": "walk",
        "facility_id": None,
        "destination_zone_id": "hall",
        "route_zone_ids": ["hall", "yard"],
        "route_edge_ids": ["edge"],
        "route_points": [],
        "started_minute": 1,
        "due_minute": 3,
        **more,
    }


def _state() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "policy_version": movement.MOVEMENT_POLICY_VERSION,
        "positions": {"b": _position({"x": 1.5, "y": -2.0}), "a": _position()},
        "staff_positions": {"s": _position()},
        "journeys": {
            "b": _journey(queue_id="q", route_points=[{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}]),
            "a": _journey(support_request_id="r", support_staff_id="s"),
            "c": _journey(facility_id="f"),
        },
        "edge_usage_minutes": {"edge": 3},
        "last_updated_minute": 0,
    }


def _world(state: dict[str, Any] | None = None) -> WorldState:
    return WorldState("s", 1, 100, model_state={KEY: state if state is not None else _state()})


def _mirror(world: WorldState) -> Any:
    return world._core.mirror(KEY)


def _run(scenario: Any, minutes: int, check: Any = None) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
        if check is not None:
            check(world)
    return world


# ---- The row -----------------------------------------------------------------------------------


def test_the_row_writes_the_bytes_of_the_tree_for_every_shape_of_a_journey() -> None:
    world = _world()
    assert _mirror(world).tainted is None
    assert _mirror(world).row_json() == native.canonical_json(_state())
    assert _mirror(world).verify()
    assert list(world.model_state[KEY]["positions"]) == ["b", "a"]


@pytest.mark.parametrize(
    "write",
    [
        lambda state: state["positions"]["a"].__setitem__("zone_id", "yard"),
        lambda state: state["positions"]["a"].__setitem__("point", {"x": 1.0, "y": 2.0}),
        lambda state: state["positions"]["b"].__setitem__("point", None),
        lambda state: state["positions"]["b"]["point"].__setitem__("x", 9.5),
        lambda state: state["positions"]["b"].update(edge_id="edge", progress=0.5),
        lambda state: state["positions"].__setitem__("n", _position({"x": 0.0, "y": 0.0})),
        lambda state: state["positions"].pop("a"),
        lambda state: state["staff_positions"].__setitem__("s", _position({"x": 1.0, "y": 1.0})),
        lambda state: state["journeys"].__setitem__("n", _journey(queue_id="q")),
        lambda state: state["journeys"].pop("b"),
        lambda state: state["journeys"]["a"].__setitem__("due_minute", 9),
        lambda state: state["journeys"]["c"].__setitem__("queue_id", "q"),
        lambda state: state["journeys"]["b"].pop("queue_id"),
        lambda state: state["journeys"]["a"]["route_zone_ids"].append("yard"),
        lambda state: state["journeys"]["b"]["route_points"][0].__setitem__("y", 4.0),
        lambda state: state["edge_usage_minutes"].__setitem__(
            "edge", state["edge_usage_minutes"].get("edge", 0) + 1
        ),
        lambda state: state.__setitem__("last_updated_minute", 7),
    ],
)
def test_a_forwarded_write_reaches_the_row(write: Any) -> None:
    world = _world()
    write(world.model_state[KEY])
    assert _mirror(world).tainted is None
    assert _mirror(world).verify()
    assert _mirror(world).row_json() == native.canonical_json(
        json.loads(json.dumps(world.model_state[KEY]))
    )


@pytest.mark.parametrize(
    "write",
    [
        lambda state: state["positions"]["a"].__setitem__("progress", 1),
        lambda state: state["positions"]["b"]["point"].__setitem__("x", 1),
        lambda state: state["positions"]["b"]["point"].__setitem__("z", 1.0),
        lambda state: state["positions"]["a"].__setitem__("other", 1),
        lambda state: state["positions"]["a"].pop("edge_id"),
        lambda state: state["positions"].__setitem__("n", {"zone_id": "hall"}),
        lambda state: state["journeys"]["a"].__setitem__("other", 1),
        lambda state: state["journeys"]["a"].pop("event_id"),
        lambda state: state["journeys"]["a"].__setitem__("queue_id", None),
        lambda state: state["journeys"].__setitem__("n", _journey(other=1)),
        lambda state: state["journeys"].__setitem__("n", _journey(queue_id=None)),
        lambda state: state["journeys"]["a"].__setitem__("started_minute", True),
        lambda state: state["edge_usage_minutes"].__setitem__("edge", 1.0),
        lambda state: state.__setitem__("other", 1),
    ],
)
def test_a_value_of_another_type_taints_the_row_and_the_tree_keeps_it(write: Any) -> None:
    world = _world()
    write(world.model_state[KEY])
    assert _mirror(world).tainted is not None
    # Nothing else depends on the row yet, and the tree is what the rules read.
    assert world.model_state[KEY] is not None
    assert native.canonical_json(world.model_state[KEY])


def test_a_bad_value_gets_the_python_rules_error() -> None:
    world = _world()
    world.model_state[KEY]["schema_version"] = "2"
    assert _mirror(world).tainted is not None
    with pytest.raises(ValueError, match="movement_v1 has an invalid exact schema"):
        validate_movement_runtime_state(world)


def test_the_int_progress_validation_accepts_runs_on_a_tainted_row() -> None:
    state = _state()
    state["positions"]["a"]["progress"] = 1
    world = _world(state)
    assert _mirror(world).tainted is not None
    assert world.model_state[KEY]["positions"]["a"]["progress"] == 1


# ---- With the rules ----------------------------------------------------------------------------


def test_the_row_follows_the_rules_with_only_single_records_written(
    generated_scenario: Any,
) -> None:
    def check(world: WorldState) -> None:
        assert _mirror(world).tainted is None
        assert _mirror(world).verify()

    tracked = _run(generated_scenario, 60, check)
    stats = _mirror(tracked).stats()
    # Written once when the namespace is made; everything after is a forwarded record.
    assert stats["whole_writes"] <= 1
    assert stats["forwards"] > 60
    assert tracked.model_state[KEY]["positions"]


def test_the_step_holds_the_tree_across_advance_movement_and_its_writes_reach_the_row(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _run(generated_scenario, 5)
    held = world.model_state[KEY]
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    agent_id = next(iter(held["journeys"]), None) or next(iter(world.agents))
    held["journeys"].setdefault(
        agent_id,
        _journey(
            route_zone_ids=[world.agents[agent_id].zone_id],
            route_edge_ids=[],
            destination_zone_id=world.agents[agent_id].zone_id,
        ),
    )
    # A replan: the step reads and pops through the name it took before advance_movement ran.
    monkeypatch.setattr(
        "sheltersim.experiments.phases.update.advance_movement", lambda *_: (agent_id,)
    )
    UpdatePhase._update_movement(controller, world.minute_index)
    assert world.model_state[KEY] is held
    assert _mirror(world).tainted is None
    assert _mirror(world).verify()
    assert world.model_state[KEY]["journeys"] is held["journeys"]


def test_restore_and_copy_read_the_row_again(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 10)
    expected = world.canonical_json()
    restored = WorldState.from_dict(json.loads(json.dumps(world.to_dict())))
    assert restored.canonical_json() == expected
    assert _mirror(restored).tainted is None
    assert _mirror(restored).verify()
    # As storage copies a restored world (_copy_restored): scheduled events are frozen and shared.
    copied = copy.deepcopy(restored, {id(event): event for event in restored.event_queue.pending()})
    assert copied.canonical_json() == expected
    assert _mirror(copied).tainted is None
    assert _mirror(copied).verify()


# ---- Order: what running advance_movement in parallel would have to keep ------------------------


def _reverse() -> Any:
    sys.path.insert(0, str(ROOT / "benchmarks" / "rule_shadow"))
    try:
        from movement_shadow import reverse_advance_movement
    finally:
        sys.path.pop(0)
    return reverse_advance_movement()


def test_reversed_order_reverses_the_walk(generated_scenario: Any) -> None:
    """The test below means nothing if the walk did not turn around."""
    world = _run(generated_scenario, 3)
    written: list[str] = []
    positions = world.model_state[KEY]["positions"]
    python_stationary = movement._set_stationary_position

    def stationary(_world: WorldState, agent_id: str, *rest: Any) -> None:
        written.append(agent_id)
        python_stationary(_world, agent_id, *rest)

    movement._set_stationary_position = stationary
    undo = _reverse()
    try:
        update.advance_movement(world, world.minute_index)
    finally:
        undo()
        movement._set_stationary_position = python_stationary
    assert len(written) > 2
    assert written == sorted(written, reverse=True)
    assert positions


def test_advance_movement_gives_the_same_world_in_reverse(generated_scenario: Any) -> None:
    plain = _run(generated_scenario, 240)
    undo = _reverse()
    try:
        reversed_world = _run(generated_scenario, 240)
    finally:
        undo()
    assert reversed_world.canonical_json() == plain.canonical_json()


def _two_queue_trips(scenario: Any) -> WorldState:
    """Two people walking to the two queues of one toilet, one per queue."""
    world = WorldState.from_scenario(scenario)
    speeds = world.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]
    for mobility in speeds:
        speeds[mobility] = 0.2
    routes = world.model_configs["facility_geometry"]["routes"]
    entrance = next(route for route in routes if route["edge_id"] == "route_corridor_toilet")
    for agent_id in ("A001", "A002"):
        world.agents[agent_id].zone_id = "toilet"
        world.agents[agent_id].next_decision_at = 100
        world.model_state[KEY]["positions"][agent_id].update(
            zone_id="toilet", point=dict(entrance["points"][-1]), position_kind="physical"
        )
    queues = sorted(
        queue_id
        for queue_id, row in world.model_state["facility_queues_v1"]["queues"].items()
        if row["facility_id"] == "toilet_01"
    )
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    service.request("A001", "toilet_01", queue_id=queues[0])
    service.request("A002", "toilet_01", queue_id=queues[1])
    service.arbitrate(0, CounterRNG(world.seed))
    assert set(world.model_state[KEY]["journeys"]) == {"A001", "A002"}
    world.minute_index = 1
    return world


def test_two_queue_replans_in_one_tick_depend_on_the_order_only_through_their_event_ids(
    generated_scenario_with_split_queues: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dependence on order this rule has: each replan takes the next event number.

    What running advance_movement in parallel must solve (id_counters["event"]). If this test
    starts to find no difference, or another one, the design's list of blockers has changed.
    """
    monkeypatch.setattr(movement, "_room_path_is_clear", lambda *_a, **_k: False)
    forward = _two_queue_trips(generated_scenario_with_split_queues)
    assert update.advance_movement(forward, 1) == ()
    backward = _two_queue_trips(generated_scenario_with_split_queues)
    undo = _reverse()
    try:
        assert update.advance_movement(backward, 1) == ()
    finally:
        undo()
    first = json.loads(json.dumps(forward.model_state[KEY]))
    second = json.loads(json.dumps(backward.model_state[KEY]))
    ids = {agent_id: first["journeys"][agent_id].pop("event_id") for agent_id in first["journeys"]}
    other = {
        agent_id: second["journeys"][agent_id].pop("event_id") for agent_id in second["journeys"]
    }
    assert ids != other
    assert ids == {"A001": other["A002"], "A002": other["A001"]}
    assert first == second
    assert forward.metric_state == backward.metric_state


def _two_trips_in_one_queue(scenario: Any) -> tuple[WorldState, dict[str, float]]:
    """The head walks to the service point and the follower to its slot, in one queue.

    The head stands on the slot the follower would take, and walks off it in the next tick.
    """
    world = WorldState.from_scenario(scenario)
    speeds = world.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]
    for mobility in speeds:
        speeds[mobility] = 0.2
    routes = world.model_configs["facility_geometry"]["routes"]
    entrance = next(route for route in routes if route["edge_id"] == "route_corridor_toilet")
    for agent_id in ("A001", "A002"):
        world.agents[agent_id].zone_id = "toilet"
        world.agents[agent_id].next_decision_at = 100
        world.model_state[KEY]["positions"][agent_id].update(
            zone_id="toilet", point=dict(entrance["points"][-1]), position_kind="physical"
        )
    queue_id = min(
        queue_id
        for queue_id, row in world.model_state["facility_queues_v1"]["queues"].items()
        if row["facility_id"] == "toilet_01"
    )
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    service.request("A001", "toilet_01", queue_id=queue_id)
    service.request("A002", "toilet_01", queue_id=queue_id)
    service.arbitrate(0, CounterRNG(world.seed))
    head = world.model_state[KEY]["journeys"]["A001"]
    assert head["purpose"].endswith("_position")
    head["purpose"] = head["purpose"].removesuffix("_position") + "_service"
    # What the next tick's arbitration does for the follower, with the head still walking.
    assert not movement.progress_physical_queue_positions(
        world, facility_id="toilet_01", resource=False, minute=0, queue_id=queue_id
    )
    assert set(world.model_state[KEY]["journeys"]) == {"A001", "A002"}
    slot = world.model_state[KEY]["journeys"]["A002"]["route_points"][-1]
    world.model_state[KEY]["positions"]["A001"]["point"] = dict(slot)
    world.minute_index = 1
    return world, dict(slot)


def test_a_slot_replanned_in_a_tick_depends_on_whether_the_one_ahead_has_walked(
    generated_scenario_with_split_queues: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second dependence on order: _queue_slot_position reads the positions of those ahead.

    The one ahead walks off the slot in the walk. In order the follower then takes that slot, and
    in reverse it takes the next one, so this is another destination and not only another number.
    The state is made by hand: the follower's first route was planned before the head stood there.
    """
    python_clear = movement._room_path_is_clear

    def clear(world: WorldState, agent_id: str, *rest: Any) -> bool:
        return agent_id != "A002" and python_clear(world, agent_id, *rest)

    monkeypatch.setattr(movement, "_room_path_is_clear", clear)
    forward, slot = _two_trips_in_one_queue(generated_scenario_with_split_queues)
    assert update.advance_movement(forward, 1) == ()
    backward, _ = _two_trips_in_one_queue(generated_scenario_with_split_queues)
    undo = _reverse()
    try:
        assert update.advance_movement(backward, 1) == ()
    finally:
        undo()
    ahead = forward.model_state[KEY]["positions"]["A001"]
    assert ahead == backward.model_state[KEY]["positions"]["A001"]
    assert ahead["point"] != slot
    assert forward.model_state[KEY]["journeys"]["A002"]["route_points"][-1] == slot
    assert backward.model_state[KEY]["journeys"]["A002"]["route_points"][-1] != slot
