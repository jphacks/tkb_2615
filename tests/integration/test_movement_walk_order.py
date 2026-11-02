"""The branches of `advance_movement` that depend on the order of the walk, against the Python rule.

The gate and the 7-day run never reach them (docs/rust-port/lanes/l3-movement.md), so these tests are
all that compares them: a queue trip that replans, and a person placed in a slot during the walk.
Each one runs the kernel on the world and the Python rule on a copy, and the whole worlds must be the
same, floats by their bits, with the event numbers, the suppressed records and the metrics.

A case the Python rule raises on is refused by the kernel with nothing changed.
"""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain import movement
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.domain.movement import MOVEMENT_STATE_KEY as KEY
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

TOILET = "toilet_01"
SLOW = 0.2


def _python_rule() -> Any:
    return movement.advance_movement.__wrapped__


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _records(world: WorldState) -> list[str]:
    return [repr(record) for record in world.event_log.records]


def _pending(world: WorldState) -> list[str]:
    return [repr(event) for event in world.event_queue.pending()]


def _order_of_maps(world: WorldState) -> list[list[str]]:
    state = world.model_state[KEY]
    return [list(state[name]) for name in ("positions", "journeys", "edge_usage_minutes")]


def _same_after(world: WorldState, minute: int) -> tuple[str, ...]:
    """The kernel on the world, the Python rule on a copy. Both must have done the same."""
    twin = copy.deepcopy(world)
    result = movement.advance_movement(world, minute)
    expected = _python_rule()(twin, minute)
    assert result == expected
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert world.metric_state == twin.metric_state
    assert _pending(world) == _pending(twin)
    assert _records(world) == _records(twin)
    assert _order_of_maps(world) == _order_of_maps(twin)
    mirror = world._core.mirror(KEY)
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])
    for agent_id, agent in world.agents.items():
        other = twin.agents[agent_id]
        assert (agent.activity, agent.target_id) == (other.activity, other.target_id), agent_id
    return result


def _toilet_area(world: WorldState) -> Any:
    geometry = movement._geometry(world)
    return next(item for item in geometry.zones if item.zone_id == "toilet")


def _stand_in_the_toilet_area(world: WorldState, agent_ids: tuple[str, ...]) -> None:
    speeds = world.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]
    for mobility in speeds:
        speeds[mobility] = SLOW
    routes = world.model_configs["facility_geometry"]["routes"]
    entrance = next(route for route in routes if route["edge_id"] == "route_corridor_toilet")
    for agent_id in agent_ids:
        world.agents[agent_id].zone_id = "toilet"
        world.agents[agent_id].next_decision_at = 100
        world.model_state[KEY]["positions"][agent_id].update(
            zone_id="toilet", point=dict(entrance["points"][-1]), position_kind="physical"
        )


def _queues_of_the_toilet(world: WorldState) -> list[str]:
    return sorted(
        queue_id
        for queue_id, row in world.model_state["facility_queues_v1"]["queues"].items()
        if row["facility_id"] == TOILET
    )


def _two_trips_in_two_queues(scenario: Any) -> WorldState:
    world = WorldState.from_scenario(scenario)
    _stand_in_the_toilet_area(world, ("A001", "A002"))
    first, second, *_ = _queues_of_the_toilet(world)
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    service.request("A001", TOILET, queue_id=first)
    service.request("A002", TOILET, queue_id=second)
    service.arbitrate(0, CounterRNG(world.seed))
    assert set(world.model_state[KEY]["journeys"]) == {"A001", "A002"}
    world.minute_index = 1
    return world


def _head_and_follower_in_one_queue(scenario: Any) -> tuple[WorldState, dict[str, float]]:
    """The head walks to the service point and the follower to its slot; the head stands on it."""
    world = WorldState.from_scenario(scenario)
    _stand_in_the_toilet_area(world, ("A001", "A002"))
    queue_id = _queues_of_the_toilet(world)[0]
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    service.request("A001", TOILET, queue_id=queue_id)
    service.request("A002", TOILET, queue_id=queue_id)
    service.arbitrate(0, CounterRNG(world.seed))
    head = world.model_state[KEY]["journeys"]["A001"]
    head["purpose"] = head["purpose"].removesuffix("_position") + "_service"
    assert not movement.progress_physical_queue_positions(
        world, facility_id=TOILET, resource=False, minute=0, queue_id=queue_id
    )
    assert set(world.model_state[KEY]["journeys"]) == {"A001", "A002"}
    slot = world.model_state[KEY]["journeys"]["A002"]["route_points"][-1]
    world.model_state[KEY]["positions"]["A001"]["point"] = dict(slot)
    world.minute_index = 1
    return world, dict(slot)


def _without_independent_queues(scenario: Any) -> Any:
    """The generated Scenario with no `facility_queues`: each facility has its own queue."""
    document = scenario.model_dump(mode="json")
    document["model_configs"].pop("facility_queues", None)
    document["model_configs"]["facility_geometry"]["service_points"] = []
    return type(scenario).model_validate(document)


def _trips_of_the_facility_s_own_queue(scenario: Any, people: tuple[str, ...]) -> WorldState:
    """People in a queue without independent FIFOs, as arbitration leaves them."""
    world = WorldState.from_scenario(_without_independent_queues(scenario))
    assert "facility_queues_v1" not in world.model_state
    _stand_in_the_toilet_area(world, people)
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    for agent_id in people:
        service.request(agent_id, TOILET)
    service.arbitrate(0, CounterRNG(world.seed))
    assert world.model_state[KEY]["journeys"]
    world.minute_index = 1
    return world


def _move_the_toilet(world: WorldState, point: dict[str, float] | None = None) -> None:
    """What an equipment move leaves: a trip planned before it must replan."""
    where = point or world.model_configs["facility_geometry"]["facility_points"][TOILET]
    world.model_state["geometry_overlay_v1"]["facility_points"][TOILET] = {
        "point_m": dict(where),
        "minute": 5,
        "source_ids": [],
        "event_id": "moved",
    }


def _obstacle_on_the_walk(world: WorldState, agent_id: str) -> None:
    """A small square across the first leg of this person's room walk."""
    points = world.model_state[KEY]["journeys"][agent_id]["route_points"]
    x = (points[0]["x"] + points[1]["x"]) / 2
    y = (points[0]["y"] + points[1]["y"]) / 2
    constraints = world.model_configs.setdefault("spatial_constraints", {})
    constraints.setdefault("obstacles", []).append(
        {
            "id": f"block-{agent_id}",
            "polygon": [
                {"x": x - 0.3, "y": y - 0.3},
                {"x": x + 0.3, "y": y - 0.3},
                {"x": x + 0.3, "y": y + 0.3},
                {"x": x - 0.3, "y": y + 0.3},
            ],
            "source_ids": ["ASM-TEST"],
        }
    )


def _schedule(world: WorldState, agent_id: str, task_id: str, event_id: str, due: int) -> None:
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="travel_return",
        agent_id=agent_id,
        resource_id=None,
        start_minute=0,
        due_minute=due,
    )
    sequence = world.allocate_event_sequence()
    world.schedule_event(
        ScheduledEvent(
            event_id=event_id,
            kind=EventKind.COMPLETION,
            due_minute=due,
            priority=0,
            sequence=sequence,
            event_type="agent_travel_complete",
            payload={"agent_id": agent_id, "task_id": task_id},
            source_id=task_id,
            causation_id=task_id,
            provenance={"source_type": "assumption", "source_id": "ASM-TEST"},
        )
    )


# ---- A queue trip that replans -------------------------------------------------------------------


def test_two_replans_take_their_event_numbers_in_the_order_of_the_walk(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = _two_trips_in_two_queues(generated_scenario_with_split_queues)
    before = {key: value["event_id"] for key, value in world.model_state[KEY]["journeys"].items()}
    _move_the_toilet(world)
    assert _same_after(world, 1) == ()
    journeys = world.model_state[KEY]["journeys"]
    first, second = (int(journeys[key]["event_id"].rsplit("-", 1)[1]) for key in ("A001", "A002"))
    assert first + 1 == second
    assert world.metric_state["physical_queue_replan_count"] == 2.0
    assert world.metric_state["movement_replan_count"] == 2.0
    assert list(journeys) == ["A001", "A002"]
    suppressed = [row for row in world.event_log.records if row.status is EventStatus.SUPPRESSED]
    assert [row.event.event_id for row in suppressed] == [before["A001"], before["A002"]]


def test_a_replan_for_a_wall_on_the_walk(generated_scenario_with_split_queues: Any) -> None:
    world = _two_trips_in_two_queues(generated_scenario_with_split_queues)
    _obstacle_on_the_walk(world, "A002")
    before = dict(world.model_state[KEY]["journeys"]["A002"])
    assert _same_after(world, 1) == ()
    after = world.model_state[KEY]["journeys"]["A002"]
    assert after["event_id"] != before["event_id"]
    assert after["route_points"] != before["route_points"]


def test_a_replan_for_a_facility_that_changed_zone(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = _two_trips_in_two_queues(generated_scenario_with_split_queues)
    world.facilities[TOILET].zone_id = "corridor"
    assert _same_after(world, 1) == ()


def _queue_trip(
    world: WorldState,
    agent_id: str,
    purpose: str = "toilet_queue_position",
    *,
    started: int = 0,
    due: int = 300,
    points: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    """A walk of this person in the queue, by hand, with its event pending and their queue's task."""
    state = world.model_state[KEY]
    task_id = next(
        key
        for key, task in world.active_tasks.items()
        if task.agent_id == agent_id and task.kind == "toilet_queue"
    )
    event_id = f"trip-of-{agent_id}"
    if world.event_queue.find(event_id) is None:
        _schedule(world, agent_id, f"holder-{agent_id}", event_id, due)
        del world.active_tasks[f"holder-{agent_id}"]
    start = state["positions"][agent_id]["point"]
    target = world.model_configs["facility_geometry"]["facility_points"][TOILET]
    state["journeys"].pop(agent_id, None)
    state["journeys"][agent_id] = {
        "event_id": event_id,
        "task_id": task_id,
        "purpose": purpose,
        "facility_id": TOILET,
        "destination_zone_id": "toilet",
        "route_zone_ids": ["toilet", "toilet"],
        "route_edge_ids": [],
        "route_points": points or [dict(start), dict(target)],
        "started_minute": started,
        "due_minute": due,
    }
    return state["journeys"][agent_id]


def _own_queue_with_trips(scenario: Any, people: tuple[str, ...]) -> WorldState:
    """Everyone in the facility's own queue on a queue trip that must replan once it has moved."""
    world = _trips_of_the_facility_s_own_queue(scenario, people)
    world.model_state[KEY]["journeys"].clear()
    for key in [event.event_id for event in world.event_queue.pending()]:
        if key.startswith("physical-queue-trip-"):
            world.event_queue.remove(key)
    for agent_id in people:
        _queue_trip(world, agent_id)
    return world


def test_a_replan_with_no_room_route_is_blocked_and_counted(generated_scenario: Any) -> None:
    world = _own_queue_with_trips(generated_scenario, ("A001", "A002"))
    _move_the_toilet(world, {"x": -1000.0, "y": -1000.0})
    assert _same_after(world, 1) == ()
    assert world.model_state[KEY]["journeys"] == {}
    assert world.metric_state["physical_queue_replan_blocked_count"] == 2.0
    assert world.metric_state["movement_replan_blocked_count"] == 2.0


def test_a_replan_past_the_end_of_the_run_is_blocked_in_its_schedule(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = _two_trips_in_two_queues(generated_scenario_with_split_queues)
    _obstacle_on_the_walk(world, "A001")
    _obstacle_on_the_walk(world, "A002")
    world.duration_minutes = 3
    assert _same_after(world, 1) == ()
    assert world.metric_state["physical_queue_route_blocked_count"] == 2.0
    # The replan's own count goes up too: the Python rule counts it when the schedule gave up.
    assert world.metric_state["physical_queue_replan_count"] == 2.0


def test_a_replan_to_where_the_person_stands_takes_no_trip(generated_scenario: Any) -> None:
    world = _own_queue_with_trips(generated_scenario, ("A001", "A002"))
    state = world.model_state[KEY]
    for agent_id in ("A001", "A002"):
        state["journeys"][agent_id]["purpose"] = "toilet_queue_service"
    _move_the_toilet(world, dict(state["positions"]["A001"]["point"]))
    assert _same_after(world, 1) == ()
    assert state["journeys"] == {}
    assert world.metric_state["physical_queue_replan_count"] == 2.0


def test_a_replan_when_the_facility_is_gone_only_ends_the_trip(generated_scenario: Any) -> None:
    world = _own_queue_with_trips(generated_scenario, ("A001",))
    journey = world.model_state[KEY]["journeys"]["A001"]
    journey["facility_id"] = "another_facility"
    # The move is of the name on the journey, which is no facility of the world.
    world.model_state["geometry_overlay_v1"]["facility_points"]["another_facility"] = {
        "point_m": {"x": 0.0, "y": 0.0},
        "minute": 5,
        "source_ids": [],
        "event_id": "moved",
    }
    assert _same_after(world, 1) == ()
    assert world.model_state[KEY]["journeys"] == {}
    assert "movement_replan_count" not in world.metric_state


def test_a_replan_of_the_facility_s_own_queue(generated_scenario: Any) -> None:
    world = _own_queue_with_trips(generated_scenario, ("A001", "A002"))
    _move_the_toilet(world)
    before = world.model_state[KEY]["journeys"]["A001"]["event_id"]
    assert _same_after(world, 1) == ()
    assert world.model_state[KEY]["journeys"]["A001"]["event_id"] != before


@pytest.mark.parametrize("people", [("A001", "A002"), ("A002", "A001")])
def test_a_slot_read_with_the_one_ahead_before_or_after_their_walk(
    generated_scenario: Any, people: tuple[str, str]
) -> None:
    """Whoever is ahead in the queue, walked already or not when the follower replans."""
    world = _own_queue_with_trips(generated_scenario, people)
    state = world.model_state[KEY]
    head, follower = people
    state["journeys"][head]["purpose"] = "toilet_queue_service"
    _obstacle_on_the_walk(world, follower)
    state["positions"][head]["progress"] = 0.9
    state["positions"][follower]["progress"] = 0.0
    assert _same_after(world, 1) == ()


def test_the_one_ahead_moving_changes_the_follower_s_destination(
    generated_scenario_with_split_queues: Any,
) -> None:
    """The dependence with a coordinate in it: the follower takes the slot the head walked off."""
    world, slot = _head_and_follower_in_one_queue(generated_scenario_with_split_queues)
    state = world.model_state[KEY]
    assert state["journeys"]["A002"]["route_points"][-1] == slot
    world.model_state["geometry_overlay_v1"]["facility_points"][TOILET] = {
        "point_m": dict(world.model_configs["facility_geometry"]["facility_points"][TOILET]),
        "minute": 5,
        "source_ids": [],
        "event_id": "moved",
    }
    assert _same_after(world, 1) == ()
    assert set(state["journeys"]) == {"A001", "A002"}
    assert state["journeys"]["A002"]["route_points"][-1] != slot


def test_a_replan_seen_by_the_slot_of_someone_later_in_the_walk(
    generated_scenario: Any,
) -> None:
    """A001 replans and so stands elsewhere; A002, later in the walk, is placed in a slot."""
    world = _own_queue_with_trips(generated_scenario, ("A001", "A002"))
    state = world.model_state[KEY]
    _move_the_toilet(world)
    # A002 is queued, with a walk of another purpose: the step places them in their slot.
    assert world.agents["A002"].activity is Activity.QUEUED
    state["journeys"]["A002"]["purpose"] = "return"
    state["journeys"]["A002"]["facility_id"] = None
    state["journeys"]["A002"]["started_minute"] = 0
    assert _same_after(world, 1) == ()
    assert "A002" not in state["journeys"]
    assert state["positions"]["A002"]["position_kind"] == "physical"


# ---- A person placed in a slot during the walk -----------------------------------------------------


@pytest.mark.parametrize("queue_order", [("A001", "A002", "A003"), ("A003", "A002", "A001")])
def test_people_placed_in_slots_read_who_has_walked(
    generated_scenario: Any, queue_order: tuple[str, str, str]
) -> None:
    """Each is queued with a trip whose task went: the walk places them, one after another."""
    world = _own_queue_with_trips(generated_scenario, queue_order)
    state = world.model_state[KEY]
    base = state["positions"][queue_order[0]]["point"]
    for offset, agent_id in enumerate(queue_order):
        state["positions"][agent_id]["point"] = {"x": base["x"], "y": base["y"] + offset * 0.1}
        state["journeys"][agent_id]["task_id"] = f"gone-{agent_id}"
    assert _same_after(world, 1) == ()
    assert state["journeys"] == {}


def test_a_queued_person_past_their_trip_keeps_their_arrival(generated_scenario: Any) -> None:
    """The completion was applied: `finish_movement` of a queue trip, in the walk."""
    world = _own_queue_with_trips(generated_scenario, ("A001", "A002"))
    state = world.model_state[KEY]
    world.event_queue.remove(state["journeys"]["A001"]["event_id"])
    assert _same_after(world, 1) == ()
    assert "A001" not in state["journeys"]


# ---- Walks that replan twice in a tick -----------------------------------------------------------


def test_one_person_replans_in_two_ticks_running(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = _two_trips_in_two_queues(generated_scenario_with_split_queues)
    _move_the_toilet(world)
    assert _same_after(world, 1) == ()
    first = dict(world.model_state[KEY]["journeys"]["A001"])
    world.minute_index = 2
    world.model_state["geometry_overlay_v1"]["facility_points"][TOILET]["minute"] = 7
    assert _same_after(world, 2) == ()
    second = world.model_state[KEY]["journeys"]["A001"]
    assert first["event_id"] != second["event_id"]
    # And a minute already done does nothing, whatever it would replan.
    assert _same_after(world, 2) == ()


def test_a_queue_replan_and_a_walker_who_must_replan_in_one_walk(
    generated_scenario_with_split_queues: Any,
) -> None:
    """One replans in the walk and takes an event; the other is returned to the controller."""
    world = _two_trips_in_two_queues(generated_scenario_with_split_queues)
    state = world.model_state[KEY]
    edge = world.zone_edges["route_sleeping_corridor"]
    edge.closed = True
    _schedule(world, "A003", "walk-3", "walk-event-3", 40)
    world.agents["A003"].activity = Activity.MOVING
    world.agents["A003"].zone_id = "sleeping"
    state["journeys"]["A003"] = {
        "event_id": "walk-event-3",
        "task_id": "walk-3",
        "purpose": "return",
        "facility_id": None,
        "destination_zone_id": "corridor",
        "route_zone_ids": ["sleeping", "corridor"],
        "route_edge_ids": ["route_sleeping_corridor"],
        "route_points": [],
        "started_minute": 0,
        "due_minute": 40,
    }
    _move_the_toilet(world)
    assert _same_after(world, 1) == ("A003",)
    assert world.metric_state["physical_queue_replan_count"] == 2.0


# ---- Refusals ------------------------------------------------------------------------------------


def test_a_trip_with_no_queue_task_is_python_s_error_before_any_change(
    generated_scenario: Any,
) -> None:
    world = _own_queue_with_trips(generated_scenario, ("A001", "A002"))
    state = world.model_state[KEY]
    task_id = state["journeys"]["A001"]["task_id"]
    world.active_tasks[task_id].resource_id = "another"
    _move_the_toilet(world)
    twin = copy.deepcopy(world)
    with pytest.raises(ValueError, match="no matching active queue task"):
        _python_rule()(twin, 1)
    before = _bytes(world)
    with pytest.raises(native.Unsupported):
        native.rules("movement").advance_movement(world, 1)
    assert _bytes(world) == before


# ---- At random -----------------------------------------------------------------------------------


def _random_trips(world: WorldState, rng: random.Random, people: tuple[str, ...]) -> None:
    """Everyone in a queue, each with a trip of a kind drawn at random, some of them stale."""
    state = world.model_state[KEY]
    for key in [event.event_id for event in world.event_queue.pending()]:
        if key.startswith("physical-queue-trip-"):
            world.event_queue.remove(key)
    area = _toilet_area(world)
    xs = [point.x for point in area.polygon]
    ys = [point.y for point in area.polygon]

    def somewhere() -> dict[str, float]:
        return {
            "x": rng.uniform(min(xs) + 0.2, max(xs) - 0.2),
            "y": rng.uniform(min(ys) + 0.2, max(ys) - 0.2),
        }

    state["journeys"].clear()
    tasks = {
        task.agent_id: key
        for key, task in world.active_tasks.items()
        if task.kind == "toilet_queue"
    }
    for agent_id in rng.sample(people, len(people)):
        kind = rng.choice(("position", "service", "other", "none", "stale", "gone"))
        state["positions"][agent_id]["point"] = somewhere()
        state["positions"][agent_id]["progress"] = rng.choice((0.0, 0.25, 1.0))
        if kind == "none":
            continue
        event_id = f"trip-{agent_id}"
        purpose = {
            "position": "toilet_queue_position",
            "service": "toilet_queue_service",
            "stale": "toilet_queue_position",
            "gone": "toilet_queue_position",
            "other": "return",
        }[kind]
        task_id = tasks[agent_id]
        if kind == "other":
            task_id = f"other-{agent_id}"
        _schedule(world, agent_id, f"holder-{agent_id}", event_id, 200 + rng.randrange(50))
        del world.active_tasks[f"holder-{agent_id}"]
        if kind == "other":
            world.active_tasks[task_id] = ActiveTask(
                id=task_id,
                kind="travel_return",
                agent_id=agent_id,
                resource_id=None,
                start_minute=0,
                due_minute=300,
            )
        if kind == "stale":
            world.event_queue.remove(event_id)
        if kind == "gone":
            task_id = f"task-that-went-{agent_id}"
        state["journeys"][agent_id] = {
            "event_id": event_id,
            "task_id": task_id,
            "purpose": purpose,
            "facility_id": TOILET if kind != "other" else None,
            "destination_zone_id": "toilet",
            "route_zone_ids": ["toilet", "toilet"],
            "route_edge_ids": [],
            "route_points": [somewhere(), somewhere(), somewhere()],
            "started_minute": rng.choice((0, 7)),
            "due_minute": 300,
        }


@pytest.mark.parametrize("seed", range(40))
def test_random_walks_are_python_s_bit_for_bit(generated_scenario: Any, seed: int) -> None:
    rng = random.Random(seed)
    people = tuple(rng.sample(("A001", "A002", "A003", "A004", "A005", "A006"), 6))
    world = _trips_of_the_facility_s_own_queue(generated_scenario, people)
    _random_trips(world, rng, people)
    point = world.model_configs["facility_geometry"]["facility_points"][TOILET]
    _move_the_toilet(world, {"x": point["x"] - rng.uniform(0.0, 0.4), "y": point["y"]})
    if rng.random() < 0.5:
        area = _toilet_area(world)
        xs = [item.x for item in area.polygon]
        ys = [item.y for item in area.polygon]
        x, y = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
        world.model_configs.setdefault("spatial_constraints", {}).setdefault(
            "obstacles", []
        ).append(
            {
                "id": "random-block",
                "polygon": [
                    {"x": x - 0.2, "y": y - 0.2},
                    {"x": x + 0.2, "y": y - 0.2},
                    {"x": x + 0.2, "y": y + 0.2},
                    {"x": x - 0.2, "y": y + 0.2},
                ],
                "source_ids": ["ASM-TEST"],
            }
        )
    for minute in (1, 2, 3):
        world.minute_index = minute
        _same_after(world, minute)
