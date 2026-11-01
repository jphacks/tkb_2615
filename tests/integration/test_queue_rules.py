"""The kernels of native/src/rules/queues.rs against the Python rules they stand beside.

Each case runs the kernel on a world and the Python rule on a copy made before it, and compares the
whole world in canonical JSON, which keeps a float's bits. benchmarks/rule_shadow/shadow_queues.py
does this on the runs of the gate; here, the values those runs don't decide: each coefficient at
the two sides of where it changes the result, and each branch the kernel refuses.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain import movement, queueing
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.domain.movement import MOVEMENT_STATE_KEY as KEY
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

MODULES = {
    "prepare_facility_queue": queueing,
    "progress_physical_queue_positions": movement,
    "progress_physical_queue_head_to_service": movement,
    "physical_service_capacity": movement,
    "shared_passage_edge_loads": movement,
}
QUEUE_POLICY = "need-weighted-physical-queue-v1"


def _kernel(name: str) -> Any:
    return getattr(native.rules("queues"), name)


def _rule(name: str) -> Any:
    return getattr(MODULES[name], name).__wrapped__


def _run(scenario: Any, minutes: int) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
    return world


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _same_after(world: WorldState, name: str, *arguments: Any, **options: Any) -> Any:
    """The kernel on the world, the rule on a copy: the same result and the same world."""
    twin = copy.deepcopy(world)
    result = _kernel(name)(world, *arguments, **options)
    expected = _rule(name)(twin, *arguments, **options)
    assert repr(result) == repr(expected)
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert [repr(event) for event in world.event_queue.pending()] == [
        repr(event) for event in twin.event_queue.pending()
    ]
    mirror = world._core.mirror(KEY)
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])
    return result


def _refuses(world: WorldState, name: str, *arguments: Any, **options: Any) -> str:
    """Unsupported, with nothing changed; then the name every caller has runs the Python rule."""
    before = _bytes(world)
    with pytest.raises(native.Unsupported) as refused:
        _kernel(name)(world, *arguments, **options)
    assert _bytes(world) == before
    return str(refused.value)


def _facility(world: WorldState, kind: str) -> str:
    return next(
        item.id
        for item in sorted(world.facilities.values(), key=lambda item: item.id)
        if kind in item.kind and item.id in movement._geometry(world).facility_points
    )


def _people(world: WorldState, count: int) -> list[str]:
    """Agents with nothing under way, so what a test gives them is all they have."""
    out = []
    for agent_id in sorted(world.agents):
        if agent_id in world.model_state[KEY]["journeys"]:
            continue
        if any(task.agent_id == agent_id for task in world.active_tasks.values()):
            continue
        if any(
            agent_id in item
            for facility in world.facilities.values()
            for item in (
                facility.queue_agent_ids,
                facility.active_agent_ids,
                facility.resource_queue_agent_ids,
                facility.resource_active_agent_ids,
            )
        ):
            continue
        out.append(agent_id)
    return out[:count]


def _line_up(
    world: WorldState,
    facility_id: str,
    people: list[str],
    *,
    resource: bool = False,
    starts: list[int] | None = None,
) -> None:
    """These people in this facility's queue, in the zone's middle, as `request` leaves them."""
    facility = world.facilities[facility_id]
    queue = facility.resource_queue_agent_ids if resource else facility.queue_agent_ids
    center = movement._zone_center(movement._geometry(world), facility.zone_id)
    for place, agent_id in enumerate(people):
        agent = world.agents[agent_id]
        agent.zone_id = facility.zone_id
        agent.activity = Activity.QUEUED
        agent.target_id = facility_id
        queue.append(agent_id)
        task_id = f"queue-task-{agent_id}"
        world.active_tasks[task_id] = ActiveTask(
            id=task_id,
            kind="resource_service_queue" if resource else "toilet_queue",
            agent_id=agent_id,
            resource_id=facility_id,
            start_minute=starts[place] if starts else 0,
            due_minute=starts[place] if starts else 0,
        )
        world.model_state[KEY]["positions"][agent_id].update(
            zone_id=facility.zone_id,
            point={"x": center.x, "y": center.y},
            edge_id=None,
            progress=0.0,
            position_kind="physical",
        )


def _world(scenario: Any) -> WorldState:
    return _run(scenario, 2)


# ---- prepare_facility_queue ----------------------------------------------------------------------


def _near_tie(generated_scenario: Any, waited: int, gap: float) -> list[str]:
    """Two people whose priorities part by `gap`: the first has waited, the second needs more."""
    world = _world(generated_scenario)
    world.model_configs["facility_queues"] = {"persistence_urgency_threshold": 0.0}
    toilet = _facility(world, "toilet")
    first, second = _people(world, 2)
    _line_up(world, toilet, [first, second], starts=[0, waited])
    base = 0.25
    aging = 0.1 * min(1.0, waited / 1440.0)
    world.agents[first].needs.toilet_urgency = base
    world.agents[second].needs.toilet_urgency = base + aging + gap
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=waited)
    return [
        "first" if item == first else "second" for item in world.facilities[toilet].queue_agent_ids
    ]


@pytest.mark.parametrize("waited", [100, 720, 1439, 1440, 3000])
def test_aging_decides_a_near_tie_at_its_exact_weight(generated_scenario: Any, waited: int) -> None:
    """A 1% change of the weight 0.1 or the day 1440 turns one of these two around."""
    assert _near_tie(generated_scenario, waited, 1e-6) == ["second", "first"]
    assert _near_tie(generated_scenario, waited, -1e-6) == ["first", "second"]


def test_a_tie_goes_to_the_earlier_start_then_the_earlier_place(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    world.model_configs["facility_queues"]["abandon_after_minutes_by_service"] = {"toilet": 10080}
    toilet = _facility(world, "toilet")
    people = _people(world, 3)
    # Needs at the top of the range, so waits make no difference past a day.
    _line_up(world, toilet, people, starts=[5, 5, 0])
    for agent_id in people:
        world.agents[agent_id].needs.toilet_urgency = 1.0
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=10000)
    assert world.facilities[toilet].queue_agent_ids == [people[2], people[0], people[1]]


@pytest.mark.parametrize(
    ("kind", "field"),
    [("water_station", "hydration_need"), ("meal_distribution", "hunger")],
)
def test_each_service_reads_its_own_need(generated_scenario: Any, kind: str, field: str) -> None:
    world = _world(generated_scenario)
    facility = _facility(world, kind)
    first, second = _people(world, 2)
    _line_up(world, facility, [first, second], resource=True)
    # Everything else says the other way round.
    for name in ("toilet_urgency", "hydration_need", "hunger"):
        setattr(world.agents[first].needs, name, 0.7)
        setattr(world.agents[second].needs, name, 0.2)
    setattr(world.agents[first].needs, field, 0.2)
    setattr(world.agents[second].needs, field, 0.7)
    _same_after(world, "prepare_facility_queue", facility, resource=True, minute=5)
    assert world.facilities[facility].resource_queue_agent_ids == [second, first]


def test_a_need_past_the_range_counts_as_its_end(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    first, second = _people(world, 2)
    _line_up(world, toilet, [first, second], starts=[0, 3])
    world.agents[first].needs.toilet_urgency = 1.0
    world.agents[second].needs.toilet_urgency = 1.5
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=3)
    assert world.facilities[toilet].queue_agent_ids == [first, second]


def test_someone_with_no_queue_task_leaves_the_list(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    first, second = _people(world, 2)
    _line_up(world, toilet, [first, second])
    del world.active_tasks[f"queue-task-{first}"]
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=3)
    assert world.facilities[toilet].queue_agent_ids == [second]


def test_the_list_is_left_alone_while_someone_walks_in_it(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    first, second = _people(world, 2)
    _line_up(world, toilet, [first, second])
    world.agents[first].needs.toilet_urgency = 0.1
    world.agents[second].needs.toilet_urgency = 0.9
    world.minute_index = 3
    assert _same_after(world, "progress_physical_queue_positions", toilet, resource=False, minute=3)
    assert first in world.model_state[KEY]["journeys"]
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=3)
    assert world.facilities[toilet].queue_agent_ids == [first, second]


@pytest.mark.parametrize(
    ("kind", "limit", "field"),
    [
        ("toilet", 120, "toilet_urgency"),
        ("water_station", 240, "hydration_need"),
        ("meal_distribution", 360, "hunger"),
    ],
)
def test_each_default_limit_and_the_threshold_are_where_someone_gives_up(
    generated_scenario: Any, kind: str, limit: int, field: str
) -> None:
    resource = kind != "toilet"

    def lined_up(need: float) -> tuple[WorldState, str, str]:
        world = _world(generated_scenario)
        world.duration_minutes = 2000
        world.model_configs["facility_queues"] = {"schema_version": 1}
        facility = _facility(world, kind)
        person = _people(world, 1)[0]
        _line_up(world, facility, [person], resource=resource)
        setattr(world.agents[person].needs, field, need)
        return world, facility, person

    below = math.nextafter(0.8, 0.0)
    world, facility, _ = lined_up(below)
    _same_after(world, "prepare_facility_queue", facility, resource=resource, minute=limit - 1)
    world, facility, person = lined_up(below)
    assert "gives up" in _refuses(
        world, "prepare_facility_queue", facility, resource=resource, minute=limit
    )
    twin = copy.deepcopy(world)
    queueing.prepare_facility_queue(world, facility, resource=resource, minute=limit)
    _rule("prepare_facility_queue")(twin, facility, resource=resource, minute=limit)
    assert _bytes(world) == _bytes(twin)
    assert world.metric_state["facility_queue_abandon_count"] == 1.0
    assert world.agents[person].next_decision_at == limit + 15
    world, facility, _ = lined_up(0.8)
    _same_after(world, "prepare_facility_queue", facility, resource=resource, minute=limit)


def test_a_config_s_limit_and_threshold_are_read(generated_scenario: Any) -> None:
    def lined_up(need: float, threshold: Any = 0.5) -> tuple[WorldState, str]:
        world = _world(generated_scenario)
        world.model_configs["facility_queues"] = {
            "schema_version": 1,
            "abandon_after_minutes_by_service": {"toilet": 50},
            "retry_delay_minutes": 3,
            "persistence_urgency_threshold": threshold,
        }
        toilet = _facility(world, "toilet")
        _line_up(world, toilet, _people(world, 1))
        world.agents[world.facilities[toilet].queue_agent_ids[0]].needs.toilet_urgency = need
        return world, toilet

    world, toilet = lined_up(0.4)
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=49)
    world, toilet = lined_up(0.4)
    assert "gives up" in _refuses(
        world, "prepare_facility_queue", toilet, resource=False, minute=50
    )
    world, toilet = lined_up(0.5)
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=50)
    world, toilet = lined_up(0.4, 1)
    assert "gives up" in _refuses(
        world, "prepare_facility_queue", toilet, resource=False, minute=50
    )
    world, toilet = lined_up(0.4, 0)
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=50)


def test_the_reference_fifo_keeps_the_order_of_starts_and_nobody_gives_up(
    generated_scenario_with: Any,
) -> None:
    from sheltersim.policies.queueing import create_queue_policy

    world = _world(generated_scenario_with(queue_policy_id="strict-fifo-queue-v1"))
    assert create_queue_policy(world.model_configs).model_id == "strict-fifo-queue-v1"
    toilet = _facility(world, "toilet")
    first, second = _people(world, 2)
    _line_up(world, toilet, [first, second], starts=[4, 1])
    world.agents[first].needs.toilet_urgency = 0.99
    _same_after(world, "prepare_facility_queue", toilet, resource=False, minute=9000)
    assert world.facilities[toilet].queue_agent_ids == [second, first]


@pytest.mark.parametrize(
    "config",
    [
        {"schema_version": 2},
        {"schema_version": 1, "other": 1},
        {"abandon_after_minutes_by_service": {"toilet": 0}},
        {"abandon_after_minutes_by_service": {"toilet": 10081}},
        {"abandon_after_minutes_by_service": {"toilet": 12.0}},
        {"abandon_after_minutes_by_service": {"shower": 5}},
        {"retry_delay_minutes": 0},
        {"retry_delay_minutes": 1441},
        {"persistence_urgency_threshold": 1.5},
        {"persistence_urgency_threshold": True},
        {"persistence_urgency_threshold": float("nan")},
    ],
)
def test_a_config_python_turns_down_goes_to_python(
    generated_scenario: Any, config: dict[str, Any]
) -> None:
    world = _world(generated_scenario)
    world.model_configs["facility_queues"] = config
    toilet = _facility(world, "toilet")
    _line_up(world, toilet, _people(world, 1))
    with pytest.raises(native.Unsupported):
        _kernel("prepare_facility_queue")(world, toilet, resource=False, minute=3)
    with pytest.raises(ValueError):
        queueing.prepare_facility_queue(world, toilet, resource=False, minute=3)
    assert len(world.facilities[toilet].queue_agent_ids) == 1


def _with_stall(world: WorldState, facility: str, agent_id: str) -> None:
    world.model_state["resource_service_v1"]["stall_state"][facility] = {
        "agent_id": agent_id,
        "reason": "stock",
        "minute": 1,
    }


def test_a_stall_stays_with_the_head_and_goes_when_the_head_changes(
    generated_scenario: Any,
) -> None:
    world = _world(generated_scenario)
    facility = _facility(world, "water_station")
    first, second = _people(world, 2)
    _line_up(world, facility, [first, second], resource=True)
    world.agents[first].needs.hydration_need = 0.9
    world.agents[second].needs.hydration_need = 0.1
    _with_stall(world, facility, first)
    _same_after(world, "prepare_facility_queue", facility, resource=True, minute=5)
    assert facility in world.model_state["resource_service_v1"]["stall_state"]
    world.agents[first].needs.hydration_need = 0.1
    world.agents[second].needs.hydration_need = 0.9
    _same_after(world, "prepare_facility_queue", facility, resource=True, minute=5)
    assert world.facilities[facility].resource_queue_agent_ids == [second, first]
    assert facility not in world.model_state["resource_service_v1"]["stall_state"]


def test_a_person_listed_twice_a_strange_kind_and_a_missing_facility(
    generated_scenario: Any,
) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    first = _people(world, 1)[0]
    _line_up(world, toilet, [first])
    world.facilities[toilet].queue_agent_ids.append(first)
    assert "twice" in _refuses(world, "prepare_facility_queue", toilet, resource=False, minute=3)
    world.facilities[toilet].queue_agent_ids.pop()
    world.facilities[toilet].kind = "İtoilet"
    assert "casefold" in _refuses(world, "prepare_facility_queue", toilet, resource=False, minute=3)
    world.facilities[toilet].kind = "toilet"
    _same_after(world, "prepare_facility_queue", "nowhere", resource=False, minute=3)
    other = _facility(world, "water_station")
    world.facilities[other].resource_queue_agent_ids.append(first)
    world.facilities[other].kind = "shelf"
    assert "kind" in _refuses(world, "prepare_facility_queue", other, resource=True, minute=3)


# ---- progress_physical_queue_positions and _head_to_service --------------------------------------


def test_the_first_who_is_not_in_their_place_walks_there(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    people = _people(world, 3)
    _line_up(world, toilet, people)
    minute = world.minute_index
    # The head walks to the slot, which holds service back.
    moved = _same_after(
        world, "progress_physical_queue_positions", toilet, resource=False, minute=minute
    )
    assert moved is True
    journey = world.model_state[KEY]["journeys"][people[0]]
    assert journey["purpose"] == "toilet_queue_position"
    event = next(
        item for item in world.event_queue.pending() if item.event_id == journey["event_id"]
    )
    assert event.provenance["fifo_position"] == 0
    assert event.payload["task_id"] == f"queue-task-{people[0]}"
    # While that walk lasts nobody else starts one, and service waits for it.
    assert (
        _same_after(
            world, "progress_physical_queue_positions", toilet, resource=False, minute=minute
        )
        is True
    )
    assert (
        _same_after(
            world, "progress_physical_queue_head_to_service", toilet, resource=False, minute=minute
        )
        is True
    )


def test_someone_who_is_moved_from_another_activity_is_queued_by_the_walk(
    generated_scenario: Any,
) -> None:
    world = _world(generated_scenario)
    facility = _facility(world, "water_station")
    person = _people(world, 1)[0]
    _line_up(world, facility, [person], resource=True)
    world.agents[person].activity = Activity.IDLE
    world.agents[person].target_id = None
    minute = world.minute_index
    assert _same_after(
        world, "progress_physical_queue_positions", facility, resource=True, minute=minute
    )
    agent = world.agents[person]
    assert agent.activity is Activity.QUEUED and agent.target_id == facility
    assert world.model_state[KEY]["journeys"][person]["purpose"] == "resource_queue_position"


@pytest.mark.parametrize(("away", "walks"), [(0.0499, False), (0.0501, True)])
def test_a_follower_within_the_tolerance_stands_in_their_slot(
    generated_scenario: Any, away: float, walks: bool
) -> None:
    """The head is at the facility, the follower 0.05 m from the slot, either side of it."""
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    head, follower = _people(world, 2)
    _line_up(world, toilet, [head, follower])
    positions = world.model_state[KEY]["positions"]
    geometry = movement._geometry(world)
    route = movement.facility_approach_route(world, head, toilet)
    assert route is not None
    last = route[1][-1]
    positions[head]["point"] = {"x": last.x, "y": last.y}
    slot = movement._queue_slot_position(world, geometry, follower, toilet)
    assert slot is not None
    positions[follower]["point"] = {"x": slot.x + away, "y": slot.y}
    # Standing somewhere else changes the first free slot's route, not the slot: check it holds.
    near = movement._queue_slot_position(world, geometry, follower, toilet)
    assert near == slot
    minute = world.minute_index
    assert not _same_after(
        world, "progress_physical_queue_positions", toilet, resource=False, minute=minute
    )
    assert (follower in world.model_state[KEY]["journeys"]) is walks


def _metric(world: WorldState) -> float:
    return world.metric_state.get("physical_queue_route_blocked_count", 0.0)


def test_a_head_with_no_route_holds_the_queue_and_a_follower_only_those_behind(
    generated_scenario: Any,
) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    first, second = _people(world, 2)
    _line_up(world, toilet, [first, second])
    # In another room there's no route in this one to any slot.
    other_zone = next(
        zone for zone in sorted(world.zones) if zone != world.facilities[toilet].zone_id
    )
    world.agents[first].zone_id = other_zone
    minute = world.minute_index
    moved = _same_after(
        world, "progress_physical_queue_head_to_service", toilet, resource=False, minute=minute
    )
    assert moved is True
    assert _metric(world) == 1.0


def test_a_walk_past_the_end_of_the_run_is_counted_and_not_started(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    person = _people(world, 1)[0]
    _line_up(world, toilet, [person])
    minute = world.duration_minutes - 1
    assert _same_after(
        world, "progress_physical_queue_positions", toilet, resource=False, minute=minute
    )
    assert _metric(world) == 1.0
    assert person not in world.model_state[KEY]["journeys"]


def test_the_head_walks_from_its_slot_to_the_service_point(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    person = _people(world, 1)[0]
    _line_up(world, toilet, [person])
    minute = world.minute_index
    assert (
        _same_after(
            world, "progress_physical_queue_head_to_service", toilet, resource=False, minute=minute
        )
        is True
    )
    journey = world.model_state[KEY]["journeys"][person]
    assert journey["purpose"] == "toilet_queue_service"
    # At the point, the walk is over and service may start.
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    person = _people(world, 1)[0]
    _line_up(world, toilet, [person])
    route = movement.facility_approach_route(world, person, toilet)
    world.model_state[KEY]["positions"][person]["point"] = {
        "x": route[1][-1].x,
        "y": route[1][-1].y,
    }
    assert (
        _same_after(
            world, "progress_physical_queue_head_to_service", toilet, resource=False, minute=minute
        )
        is False
    )
    assert (
        _same_after(
            world, "progress_physical_queue_positions", toilet, resource=False, minute=minute
        )
        is False
    )


def test_an_empty_queue_and_a_missing_facility(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    minute = world.minute_index
    for name in ("progress_physical_queue_positions", "progress_physical_queue_head_to_service"):
        assert _same_after(world, name, toilet, resource=False, minute=minute) is False
        assert _same_after(world, name, "nowhere", resource=False, minute=minute) is True


def test_without_a_task_python_raises_its_own_error(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    person = _people(world, 1)[0]
    _line_up(world, toilet, [person])
    del world.active_tasks[f"queue-task-{person}"]
    minute = world.minute_index
    assert "no matching task" in _refuses(
        world, "progress_physical_queue_positions", toilet, resource=False, minute=minute
    )
    with pytest.raises(ValueError, match="no matching active queue task"):
        movement.progress_physical_queue_positions(world, toilet, resource=False, minute=minute)


def test_an_independent_fifo_goes_to_python(generated_scenario_with_split_queues: Any) -> None:
    world = _world(generated_scenario_with_split_queues)
    queue_id, row = sorted(world.model_state["facility_queues_v1"]["queues"].items())[0]
    minute = world.minute_index
    for name in ("progress_physical_queue_positions", "progress_physical_queue_head_to_service"):
        assert "independent" in _refuses(
            world, name, row["facility_id"], resource=False, minute=minute, queue_id=queue_id
        )
    assert "independent" in _refuses(
        world, "physical_service_capacity", row["facility_id"], 1, queue_id
    )


def test_int_coordinates_taint_and_go_to_python(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    person = _people(world, 1)[0]
    _line_up(world, toilet, [person])
    world.model_state[KEY]["positions"][person]["point"] = {"x": 1, "y": 1}
    minute = world.minute_index
    with pytest.raises(native.Unsupported):
        _kernel("progress_physical_queue_positions")(world, toilet, resource=False, minute=minute)


def test_a_tick_of_a_queue_forwards_no_whole_namespace(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    _line_up(world, toilet, _people(world, 2))
    mirror = world._core.mirror(KEY)
    before = mirror.stats()
    assert _kernel("progress_physical_queue_positions")(
        world, toilet, resource=False, minute=world.minute_index
    )
    assert mirror.stats()["whole_writes"] == before["whole_writes"]
    assert mirror.stats()["forwards"] == before["forwards"]


def test_each_tick_of_a_run_is_python_s_at_every_facility(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    reached = 0
    for tick in range(240):
        engine.step()
        if tick % 20:
            continue
        minute = world.minute_index
        for facility in sorted(world.facilities):
            resource = "toilet" not in world.facilities[facility].kind
            reached += bool(
                world.facilities[facility].resource_queue_agent_ids
                if resource
                else world.facilities[facility].queue_agent_ids
            )
            options = {"resource": resource, "minute": minute}
            _same_after(world, "progress_physical_queue_positions", facility, **options)
            _same_after(world, "progress_physical_queue_head_to_service", facility, **options)
            _same_after(world, "prepare_facility_queue", facility, **options)
        _same_after(world, "shared_passage_edge_loads", minute)
    assert reached


# ---- physical_service_capacity -------------------------------------------------------------------


@pytest.mark.parametrize("configured", [0, 1, 5])
def test_a_surveyed_point_serves_one_at_a_time(generated_scenario: Any, configured: int) -> None:
    world = _world(generated_scenario)
    toilet = _facility(world, "toilet")
    assert _same_after(world, "physical_service_capacity", toilet, configured) == min(configured, 1)
    assert _same_after(world, "physical_service_capacity", "nowhere", configured) == configured
    del world.model_configs["facility_geometry"]
    assert _kernel("physical_service_capacity")(world, toilet, configured) == configured


def test_a_capacity_that_is_not_an_int_goes_to_python(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    assert "not an int" in _refuses(
        world, "physical_service_capacity", _facility(world, "toilet"), 2.0
    )


# ---- shared_passage_edge_loads -------------------------------------------------------------------


def _travel_event(
    world: WorldState,
    agent_id: str,
    edges: list[str],
    *,
    start: int,
    due: int,
    origin: str = "zone",
) -> None:
    task_id = f"travel-task-{agent_id}"
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="travel",
        agent_id=agent_id,
        resource_id=None,
        start_minute=start,
        due_minute=due,
    )
    sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=f"travel-{sequence}",
            kind=EventKind.COMPLETION,
            due_minute=due,
            priority=0,
            sequence=sequence,
            event_type="agent_travel_complete",
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "origin_zone_id": origin,
                "route_edge_ids": edges,
            },
            source_id=task_id,
            causation_id=task_id,
            provenance={"source_type": "assumption", "source_id": "ASM-TEST"},
        )
    )


def _edge_pair(world: WorldState) -> list[str]:
    return sorted(world.zone_edges)[:2]


def _no_walks(world: WorldState) -> None:
    world.model_state[KEY]["journeys"].clear()
    world.model_state["staff_operations_v1"]["staff"].clear()


def test_a_journey_s_edge_at_each_minute_of_it(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _no_walks(world)
    agent_id = _people(world, 1)[0]
    edges = _edge_pair(world)
    world.model_state[KEY]["journeys"][agent_id] = {
        "event_id": "e",
        "task_id": "t",
        "purpose": "walk",
        "facility_id": None,
        "destination_zone_id": "z",
        "route_zone_ids": ["a", "b", "c"],
        "route_edge_ids": edges,
        "route_points": [],
        "started_minute": 100,
        "due_minute": 113,
    }
    seen = set()
    for minute in range(98, 116):
        seen.add(tuple(_same_after_loads(world, minute)))
    assert () in seen and (edges[0],) in seen and (edges[1],) in seen


def _same_after_loads(world: WorldState, minute: int | None = None) -> dict[str, int]:
    result = _kernel("shared_passage_edge_loads")(world, minute)
    expected = _rule("shared_passage_edge_loads")(world, minute)
    assert repr(result) == repr(expected)
    return result


def test_a_pending_trip_counts_unless_a_journey_already_does(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _no_walks(world)
    first, second = _people(world, 2)
    edges = _edge_pair(world)
    _travel_event(world, first, edges, start=0, due=world.duration_minutes - 1)
    assert sum(_same_after_loads(world).values()) == 1
    _travel_event(world, second, edges[:1], start=0, due=world.duration_minutes - 2, origin="")
    assert sum(_same_after_loads(world).values()) == 1
    world.model_state[KEY]["journeys"][first] = {
        "event_id": "e",
        "task_id": "t",
        "purpose": "walk",
        "facility_id": None,
        "destination_zone_id": "z",
        "route_zone_ids": ["a", "b"],
        "route_edge_ids": edges[:1],
        "started_minute": 0,
        "due_minute": world.duration_minutes - 1,
        "route_points": [],
    }
    assert sum(_same_after_loads(world).values()) == 1


def test_staff_on_their_way_count_and_the_keys_are_sorted(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _no_walks(world)
    edges = _edge_pair(world)
    others = sum(_same_after_loads(world, 0).values())
    staff = world.model_state["staff_operations_v1"]["staff"]
    staff["S1"] = {
        "phase": "travel",
        "started_minute": 0,
        "travel_due_minute": 10,
        "route_edge_ids": list(reversed(edges)),
    }
    staff["S2"] = {
        "phase": "travel",
        "started_minute": 0,
        "travel_due_minute": 10,
        "route_edge_ids": edges,
    }
    staff["S3"] = {"phase": "work"}
    for minute in range(12):
        result = _same_after_loads(world, minute)
        assert list(result) == sorted(result)
    assert sum(_same_after_loads(world, 0).values()) == others + 2


def test_an_edge_nobody_knows_goes_to_python(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _no_walks(world)
    agent_id = _people(world, 1)[0]
    _travel_event(world, agent_id, ["no-such-edge"], start=0, due=world.duration_minutes - 1)
    assert "edge" in _refuses(world, "shared_passage_edge_loads", world.minute_index)
    with pytest.raises(KeyError):
        movement.shared_passage_edge_loads(world, world.minute_index)
