"""The kernels of native/src/rules/movement.rs against the Python rules of domain/movement.py.

Each case runs the kernel on a world and the Python rule on a copy of that world made before it, and
compares the whole world in canonical JSON, which keeps a float's bits. The state of a tick the gate
reaches is benchmarks/rule_shadow/shadow_movement.py; here, the branches it doesn't reach, and the
branches the kernel refuses because they depend on the order of the walk.
"""

from __future__ import annotations

import copy
import math
import random
from dataclasses import replace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.contracts.extensions import FacilityGeometry, Point2D
from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain import movement
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.geometry_overlay import geometry_config_scope
from sheltersim.domain.models import Activity
from sheltersim.domain.movement import MOVEMENT_STATE_KEY as KEY
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.space import ZoneRoute

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _rules() -> Any:
    return native.rules("movement")


def _rule(name: str) -> Any:
    return getattr(movement, name).__wrapped__


def _run(scenario: Any, minutes: int) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
    return world


def _bytes(world: WorldState) -> str:
    """The whole world, floats by their bits. A hand-made state need not pass validation."""
    return world._canonical_text(world._canonical_data())


def _mirror(world: WorldState) -> Any:
    return world._core.mirror(KEY)


def _same_after(world: WorldState, name: str, *arguments: Any, **options: Any) -> Any:
    """The kernel on the world, the rule on a copy: the same result and the same world."""
    twin = copy.deepcopy(world)
    result = getattr(_rules(), name)(world, *arguments, **options)
    expected = _rule(name)(twin, *arguments, **options)
    assert repr(result) == repr(expected)
    assert _bytes(world) == _bytes(twin)
    assert _mirror(world).tainted is None
    assert _mirror(world).verify()
    assert _mirror(world).row_json() == native.canonical_json(world.model_state[KEY])
    return result


def _refuses(world: WorldState, name: str, *arguments: Any, **options: Any) -> str:
    """Unsupported, with nothing changed."""
    before = _bytes(world)
    forwards = _mirror(world).stats()
    with pytest.raises(native.Unsupported) as refused:
        getattr(_rules(), name)(world, *arguments, **options)
    assert _bytes(world) == before
    assert _mirror(world).stats() == forwards
    return str(refused.value)


def _walker(world: WorldState) -> str:
    """A person put on a two-edge walk that the queue and task of the world accept."""
    state = world.model_state[KEY]
    assert world.zone_edges
    edge = next(iter(world.zone_edges.values()))
    follow = next(
        (item for item in world.zone_edges.values() if item.from_zone_id == edge.to_zone_id),
        edge,
    )
    agent_id = sorted(world.agents)[0]
    agent = world.agents[agent_id]
    agent.activity = Activity.MOVING
    agent.zone_id = edge.from_zone_id
    world.minute_index = max(world.minute_index, 10)
    task_id, event_id = "task-walk", "event-walk"
    _add_task_and_event(world, agent_id, task_id, event_id, world.minute_index + 40)
    state["journeys"][agent_id] = {
        "event_id": event_id,
        "task_id": task_id,
        "purpose": "walk",
        "facility_id": None,
        "destination_zone_id": follow.to_zone_id,
        "route_zone_ids": [edge.from_zone_id, follow.from_zone_id, follow.to_zone_id],
        "route_edge_ids": [edge.id, follow.id],
        "route_points": [],
        "started_minute": world.minute_index - 3,
        "due_minute": world.minute_index + 7,
    }
    return agent_id


def _add_task_and_event(
    world: WorldState, agent_id: str, task_id: str, event_id: str, due: int
) -> None:
    from sheltersim.domain.models import ActiveTask
    from sheltersim.events.models import EventKind, ScheduledEvent

    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="travel",
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


# ---- Every tick ---------------------------------------------------------------------------------


def test_each_tick_of_a_run_is_python_s_bit_for_bit(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    ran = 0
    for _ in range(90):
        engine.step()
        state = world.model_state[KEY]
        ran += bool(state["journeys"])
        state["last_updated_minute"] = world.minute_index - 1
        _same_after(world, "advance_movement", world.minute_index)
    assert ran > 10


def test_a_kernel_forwards_nothing_and_writes_no_namespace(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 30)
    stats = _mirror(world).stats()
    assert _rules().advance_movement(world, world.minute_index) == ()
    assert _mirror(world).stats()["forwards"] == stats["forwards"]
    assert _mirror(world).stats()["whole_writes"] == stats["whole_writes"]


def test_a_person_who_stands_still_is_not_written_again(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 30)
    state = world.model_state[KEY]
    _rules().advance_movement(world, world.minute_index)
    still = next(
        agent_id
        for agent_id, agent in world.agents.items()
        if agent_id not in state["journeys"] and agent.activity is not Activity.QUEUED
    )
    record = state["positions"][still]
    _rules().advance_movement(world, world.minute_index + 1)
    assert state["positions"][still] is record


def test_a_walk_along_two_edges_both_ways(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    journey = world.model_state[KEY]["journeys"][agent_id]
    for minute in range(journey["started_minute"], journey["due_minute"] + 1):
        world.model_state[KEY]["last_updated_minute"] = minute - 1
        assert _same_after(world, "advance_movement", minute) == ()
    assert world.model_state[KEY]["edge_usage_minutes"]
    # The other way along the same edges: the polyline runs backwards.
    world.model_state[KEY]["journeys"][agent_id]["route_zone_ids"].reverse()
    world.model_state[KEY]["last_updated_minute"] = journey["started_minute"]
    _same_after(world, "advance_movement", journey["started_minute"] + 1)


def test_a_closed_edge_replans_what_is_walking_it(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    journey = world.model_state[KEY]["journeys"][agent_id]
    world.zone_edges[journey["route_edge_ids"][0]].closed = True
    world.zone_edges[journey["route_edge_ids"][1]].closed = True
    state = world.model_state[KEY]
    state["last_updated_minute"] = world.minute_index - 1
    assert _same_after(world, "advance_movement", world.minute_index) == (agent_id,)


def test_a_person_who_left_loses_their_journey_and_position(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    dict.pop(world.model_state[KEY], "nothing", None)
    state = world.model_state[KEY]
    extra = copy.deepcopy(dict(state["journeys"][agent_id]))
    state["journeys"]["gone"] = _plain(extra)
    state["positions"]["gone"] = _plain(state["positions"][agent_id])
    state["last_updated_minute"] = world.minute_index - 1
    twin = copy.deepcopy(world)
    assert _rules().advance_movement(world, world.minute_index) == ()
    assert _rule("advance_movement")(twin, world.minute_index) == ()
    assert _bytes(world) == _bytes(twin)
    assert "gone" not in state["journeys"] and "gone" not in state["positions"]
    assert _mirror(world).verify()


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def test_a_trip_whose_event_or_task_went_ends_there(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    state = world.model_state[KEY]
    state["last_updated_minute"] = world.minute_index - 1
    world.active_tasks.pop(state["journeys"][agent_id]["task_id"])
    _same_after(world, "advance_movement", world.minute_index)
    assert agent_id not in state["journeys"]


def test_someone_with_no_position_gets_one_first(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    state = world.model_state[KEY]
    agent_id = sorted(world.agents)[3]
    del state["positions"][agent_id]
    _same_after(world, "advance_movement", world.minute_index)
    assert agent_id in state["positions"]
    assert state["last_updated_minute"] >= world.minute_index


def test_a_minute_already_done_does_nothing_but_the_records(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    state = world.model_state[KEY]
    before = state["last_updated_minute"]
    assert _same_after(world, "advance_movement", before) == ()


def test_in_service_and_sleeping_stand_at_their_places(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    ids = [
        agent_id
        for agent_id in sorted(world.agents)
        if agent_id not in world.model_state[KEY]["journeys"]
    ]
    facility_id = sorted(world.facilities)[0]
    world.agents[ids[0]].activity = Activity.IN_SERVICE
    world.agents[ids[0]].target_id = facility_id
    world.agents[ids[1]].activity = Activity.IN_SERVICE
    world.agents[ids[1]].target_id = "nowhere"
    world.agents[ids[2]].activity = Activity.SLEEPING
    world.agents[ids[3]].activity = Activity.SLEEPING
    world.agents[ids[4]].activity = Activity.SLEEPING
    world.agents[ids[5]].activity = Activity.SLEEPING
    world.agents[ids[6]].activity = Activity.QUEUED
    world.agents[ids[6]].target_id = None
    overlay = world.model_state["geometry_overlay_v1"]
    overlay["bed_reservations"] = {
        ids[2]: {"room_id": "room", "bed_index": 1},
        ids[3]: {"room_id": "room", "bed_index": 7},
        ids[4]: {"room_id": "no-room", "bed_index": 0},
        ids[5]: {"room_id": "room", "bed_index": True},
    }
    overlay["temporary_rooms"] = {
        "room": {"bed_points_m": [{"x": 1.5, "y": 2.5}, {"x": 3.25, "y": 4.125}]},
    }
    state = world.model_state[KEY]
    state["last_updated_minute"] = world.minute_index - 1
    _same_after(world, "advance_movement", world.minute_index)
    assert state["positions"][ids[2]]["point"] == {"x": 3.25, "y": 4.125}
    # A bed that isn't two floats is the Python rule's to take or refuse.
    overlay["temporary_rooms"]["room"]["bed_points_m"][1] = {"x": 3, "y": 4}
    state["last_updated_minute"] = world.minute_index - 1
    _refuses(world, "advance_movement", world.minute_index)


def test_without_a_floor_plan_people_have_display_points(generated_scenario: Any) -> None:
    scenario = copy.deepcopy(generated_scenario.model_dump(mode="json"))
    world = WorldState.from_scenario(generated_scenario)
    del world.model_configs["facility_geometry"]
    world.model_state.pop("geometry_overlay_v1", None)
    world.model_configs.pop("spatial_constraints", None)
    zone_ids = sorted(world.zones)
    for index, zone_id in enumerate(zone_ids):
        world.zones[zone_id].display_x = None if index == 0 else float(index) + 0.5
        world.zones[zone_id].display_y = None if index == 0 else float(index) + 0.25
    assert scenario
    state = world.model_state[KEY]
    state["last_updated_minute"] = -1
    _same_after(world, "advance_movement", 0)
    kinds = {record["position_kind"] for record in state["positions"].values()}
    assert kinds <= {"display_only", "unavailable"}


def test_staff_on_their_way_are_placed_along_their_route(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    staff_id = sorted(world.staff)[0]
    edge = next(iter(world.zone_edges.values()))
    follow = next(
        (item for item in world.zone_edges.values() if item.from_zone_id == edge.to_zone_id), edge
    )
    operations = world.model_state["staff_operations_v1"]["staff"]
    record = operations[staff_id]
    state = world.model_state[KEY]
    minutes = edge.travel_minutes + follow.travel_minutes
    for elapsed in range(minutes + 2):
        record.update(
            phase="travel",
            travel_minutes=minutes,
            started_minute=world.minute_index + 1 - elapsed,
            route_edge_ids=[edge.id, follow.id],
            route_zone_ids=[edge.from_zone_id, follow.from_zone_id],
        )
        state["last_updated_minute"] = world.minute_index - 1
        _same_after(world, "advance_movement", world.minute_index)
    assert state["staff_positions"][staff_id]["progress"] == 1.0


def test_a_room_walk_follows_its_points_and_stops_at_what_blocks_them(
    generated_scenario: Any,
) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    state = world.model_state[KEY]
    journey = state["journeys"][agent_id]
    zone = world.agents[agent_id].zone_id
    geometry = movement._geometry(world)
    polygon = next(item for item in geometry.zones if item.zone_id == zone).polygon
    center = movement._zone_center(geometry, zone)
    corner = polygon[0]
    near = {"x": corner.x * 0.1 + center.x * 0.9, "y": corner.y * 0.1 + center.y * 0.9}
    journey["route_edge_ids"] = []
    journey["route_zone_ids"] = [zone, zone]
    journey["destination_zone_id"] = zone
    journey["route_points"] = [
        {"x": center.x, "y": center.y},
        near,
        {"x": center.x, "y": near["y"]},
    ]
    state["positions"][agent_id]["point"] = {"x": center.x, "y": center.y}
    world.model_configs["spatial_constraints"] = {}
    walked = 0
    for minute in range(journey["started_minute"], journey["due_minute"] + 1):
        state["last_updated_minute"] = minute - 1
        walked += _same_after(world, "advance_movement", minute) == ()
    assert walked
    world.model_configs["spatial_constraints"] = {
        "obstacles": [
            {
                "id": "block",
                "polygon": [
                    {"x": near["x"] - 0.01, "y": near["y"] - 0.01},
                    {"x": near["x"] + 0.01, "y": near["y"] - 0.01},
                    {"x": near["x"] + 0.01, "y": near["y"] + 0.01},
                    {"x": near["x"] - 0.01, "y": near["y"] + 0.01},
                ],
            }
        ]
    }
    state["positions"][agent_id]["progress"] = 0.0
    state["last_updated_minute"] = journey["started_minute"] - 1
    assert _same_after(world, "advance_movement", journey["started_minute"]) == (agent_id,)


# ---- What depends on the order of the walk ------------------------------------------------------


def _two_people_in_queue_trips(scenario: Any) -> WorldState:
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


def _move_the_toilet(world: WorldState) -> None:
    """What an equipment move leaves: a queue trip planned before it has to replan."""
    point = world.model_configs["facility_geometry"]["facility_points"]["toilet_01"]
    world.model_state["geometry_overlay_v1"]["facility_points"]["toilet_01"] = {
        "point_m": dict(point),
        "minute": 5,
        "source_ids": [],
        "event_id": "moved",
    }


def test_two_queue_replans_go_to_the_python_rule_and_keep_their_order(
    generated_scenario_with_split_queues: Any,
) -> None:
    """The kernel doesn't decide this tick: the event numbers depend on who replans first."""
    world = _two_people_in_queue_trips(generated_scenario_with_split_queues)
    _move_the_toilet(world)
    twin = copy.deepcopy(world)
    assert "serial order" in _refuses(world, "advance_movement", 1)
    # What runs instead, through the name every caller has.
    assert movement.advance_movement(world, 1) == _rule("advance_movement")(twin, 1) == ()
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert _mirror(world).verify()
    first, second = (
        world.model_state[KEY]["journeys"][key]["event_id"] for key in ("A001", "A002")
    )
    assert int(first.rsplit("-", 1)[1]) + 1 == int(second.rsplit("-", 1)[1])


def test_a_person_in_a_slot_in_the_walk_goes_to_the_python_rule(generated_scenario: Any) -> None:
    """Their slot reads the people ahead, who the walk may already have moved."""
    world = _run(generated_scenario, 5)
    facility = world.facilities[sorted(world.facilities)[0]]
    agent_id = _walker(world)
    state = world.model_state[KEY]
    agent = world.agents[agent_id]
    # Queued, on a trip that is not a queue trip: the step places them in their slot.
    agent.activity = Activity.QUEUED
    agent.target_id = facility.id
    facility.queue_agent_ids.append(agent_id)
    state["last_updated_minute"] = world.minute_index - 1
    twin = copy.deepcopy(world)
    assert "queue slot" in _refuses(world, "advance_movement", world.minute_index)
    movement.advance_movement(world, world.minute_index)
    _rule("advance_movement")(twin, world.minute_index)
    assert _bytes(world) == _bytes(twin)


def test_finishing_one_trip_may_read_the_slots(generated_scenario: Any) -> None:
    """One person in one call: nobody else moves during it, so the kernel decides it."""
    world = _run(generated_scenario, 5)
    facility = next(
        item
        for item in world.facilities.values()
        if item.id in movement._geometry(world).facility_points
    )
    ids = sorted(world.agents)[:4]
    state = world.model_state[KEY]
    for agent_id in ids:
        world.agents[agent_id].activity = Activity.QUEUED
        world.agents[agent_id].target_id = facility.id
        world.agents[agent_id].zone_id = facility.zone_id
        facility.queue_agent_ids.append(agent_id)
    for agent_id in ids:
        _same_after(world, "finish_movement", agent_id)
        assert state["positions"][agent_id]["position_kind"] == "physical"


# ---- What the kernels take ----------------------------------------------------------------------


def test_int_coordinates_taint_and_go_to_the_python_rule(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    state = world.model_state[KEY]
    state["positions"][sorted(world.agents)[0]]["progress"] = 0
    assert _mirror(world).tainted
    twin = copy.deepcopy(world)
    with pytest.raises(native.Unsupported):
        _rules().advance_movement(world, world.minute_index + 1)
    assert movement.advance_movement(world, world.minute_index + 1) == ()
    assert _rule("advance_movement")(twin, world.minute_index + 1) == ()
    assert _bytes(world) == _bytes(twin)


def test_a_state_past_the_table_is_not_the_row_s(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    dict.__setitem__(world.model_state, KEY, _plain(world.model_state[KEY]))
    with pytest.raises(native.Unsupported):
        _rules().advance_movement(world, world.minute_index + 1)


def test_an_edge_the_state_does_not_know_is_python_s_error(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    state = world.model_state[KEY]
    state["journeys"][agent_id]["route_edge_ids"][0] = "no-such-edge"
    state["last_updated_minute"] = world.minute_index - 1
    with pytest.raises(native.Unsupported):
        _rules().advance_movement(world, world.minute_index)
    with pytest.raises(KeyError, match="no-such-edge"):
        movement.advance_movement(world, world.minute_index)


def test_a_config_edited_outside_a_scope_is_seen_at_the_next_call(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 2)
    geometry = world.model_configs["facility_geometry"]
    geometry["arrival_approach_routes"] = []
    zone_id = sorted(world.zones)[0]
    first = _rules().zone_center(world, zone_id)
    assert first == _python_center(world, zone_id)
    zone = next(item for item in geometry["zones"] if item["zone_id"] == zone_id)
    zone["polygon"] = [{"x": p["x"] * 0.5, "y": p["y"] * 0.5} for p in zone["polygon"]]
    zone["usable_area_m2"] = 1e-6
    assert _rules().zone_center(world, zone_id) == _python_center(world, zone_id) != first


def test_a_moved_facility_is_seen_by_value(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 2)
    geometry = movement._geometry(world)
    agent_id = sorted(world.agents)[0]
    with geometry_config_scope():
        facility_id = next(iter(world.model_configs["facility_geometry"]["facility_points"]))
        zone_id = world.facilities[facility_id].zone_id
        center = movement._zone_center(geometry, zone_id)
        world.agents[agent_id].zone_id = zone_id
        world.model_state[KEY]["positions"][agent_id]["point"] = {"x": center.x, "y": center.y}
        before = _same_after(world, "facility_approach_route", agent_id, facility_id)
        original = world.model_configs["facility_geometry"]["facility_points"][facility_id]
        moved = {"x": (original["x"] * 3 + center.x) / 4, "y": (original["y"] * 3 + center.y) / 4}
        world.model_state["geometry_overlay_v1"]["facility_points"][facility_id] = {
            "point_m": moved,
            "minute": 1,
            "source_ids": [],
            "event_id": "moved",
        }
        after = _same_after(world, "facility_approach_route", agent_id, facility_id)
        assert before is not None and after is not None
        assert repr(before) != repr(after)


def _python_center(world: WorldState, zone_id: str) -> tuple[float, float] | None:
    point = movement._zone_center(movement._geometry(world), zone_id)
    return None if point is None else (point.x, point.y)


# ---- start_movement, finish_movement, routes ----------------------------------------------------


@pytest.mark.parametrize("started_offset", [0, 1, -1])
@pytest.mark.parametrize("kind", ["edges", "points", "nothing", "support", "queue"])
def test_start_movement_is_python_s(
    generated_scenario: Any, kind: str, started_offset: int
) -> None:
    world = _run(generated_scenario, 5)
    agent_id = sorted(world.agents)[2]
    state = world.model_state[KEY]
    edge = next(iter(world.zone_edges.values()))
    started = state["last_updated_minute"] + started_offset
    route = ZoneRoute(
        edge.from_zone_id,
        edge.to_zone_id,
        (edge.from_zone_id, edge.to_zone_id),
        (edge.id,) if kind in {"edges", "support"} else (),
        3,
    )
    options: dict[str, Any] = {}
    if kind == "points":
        options["route_points"] = (
            Point2D(x=1.0, y=2.0),
            Point2D(x=4.0, y=6.0),
            Point2D(x=4.0, y=9.5),
        )
    if kind == "support":
        options.update(support_request_id="request", support_staff_id="staff")
    if kind == "queue":
        options.update(queue_id="queue")
    _same_after(
        world,
        "start_movement",
        agent_id=agent_id,
        route=route,
        purpose="walk",
        facility_id=None,
        started_minute=started,
        due_minute=started + 7,
        event_id="e",
        task_id="t",
        **options,
    )


def test_start_movement_with_only_half_of_the_support_writes_neither(
    generated_scenario: Any,
) -> None:
    world = _run(generated_scenario, 5)
    edge = next(iter(world.zone_edges.values()))
    route = ZoneRoute(
        edge.from_zone_id, edge.to_zone_id, (edge.from_zone_id, edge.to_zone_id), (edge.id,), 3
    )
    _same_after(
        world,
        "start_movement",
        agent_id=sorted(world.agents)[2],
        route=route,
        purpose="walk",
        facility_id="f",
        started_minute=9,
        due_minute=11,
        event_id="e",
        task_id="t",
        support_request_id="request",
    )
    assert "support_request_id" not in world.model_state[KEY]["journeys"][sorted(world.agents)[2]]


@pytest.mark.parametrize(
    "change",
    [
        {"started_minute": 9, "due_minute": 9},
        {"purpose": 1},
        {"route": ZoneRoute("a", "b", ["a", "b"], (), 3)},  # type: ignore[arg-type]
    ],
)
def test_start_movement_refuses_what_it_does_not_decide(
    generated_scenario: Any, change: dict[str, Any]
) -> None:
    world = _run(generated_scenario, 5)
    arguments = {
        "agent_id": sorted(world.agents)[2],
        "route": ZoneRoute("a", "b", ("a", "b"), (), 3),
        "purpose": "walk",
        "facility_id": None,
        "started_minute": 9,
        "due_minute": 11,
        "event_id": "e",
        "task_id": "t",
        **change,
    }
    _refuses(world, "start_movement", **arguments)


def test_finish_movement_of_each_kind_is_python_s(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    state = world.model_state[KEY]
    state["journeys"][agent_id]["purpose"] = "toilet_queue_position"
    _same_after(world, "finish_movement", agent_id)
    assert state["positions"][agent_id]["edge_id"] is None
    # No journey now: the person stands where a person stands.
    world.agents[agent_id].activity = Activity.IDLE
    _same_after(world, "finish_movement", agent_id)
    # Nobody by that name: nothing to place.
    _same_after(world, "finish_movement", "nobody")


def _routes_of(world: WorldState) -> list[ZoneRoute]:
    edges = list(world.zone_edges.values())
    routes = [
        ZoneRoute(
            edge.from_zone_id,
            edge.to_zone_id,
            (edge.from_zone_id, edge.to_zone_id),
            (edge.id,),
            edge.travel_minutes,
        )
        for edge in edges
    ]
    # A long one, since a duration is rounded up to whole minutes: 1% shows only at this length.
    routes.append(replace(routes[0], travel_minutes=997))
    routes.append(
        ZoneRoute(edges[0].from_zone_id, edges[0].from_zone_id, (edges[0].from_zone_id,), (), 0)
    )
    return routes


# Areas that leave a route uncrowded, crowded, and at the most crowding counts for.
@pytest.mark.parametrize("area", [None, 5.0, 0.2])
@pytest.mark.parametrize(
    "model",
    [
        None,
        {
            "speed_m_per_minute_by_mobility": {
                "independent": 60.5,
                "assisted": 21.25,
                "wheelchair": 33.0,
            },
            "density_slowdown_per_person_m2": 0.3,
            "maximum_density_slowdown": 1.75,
        },
    ],
)
def test_a_route_s_minutes_are_python_s(
    generated_scenario: Any, model: dict[str, Any] | None, area: float | None
) -> None:
    world = _run(generated_scenario, 5)
    for zone in world.model_configs["facility_geometry"]["zones"] if area else ():
        zone["usable_area_m2"] = area
    for agent in world.agents.values() if area else ():
        agent.zone_id = next(iter(world.zone_edges.values())).from_zone_id
    if model is None:
        world.model_configs.pop("movement_model", None)
    else:
        world.model_configs["movement_model"] = model
    from sheltersim.domain.models import Mobility

    mobilities = [Mobility.INDEPENDENT, Mobility.ASSISTED, Mobility.WHEELCHAIR]
    for index, agent_id in enumerate(sorted(world.agents)[:3]):
        agent = world.agents[agent_id]
        agent.profile = _with_mobility(agent.profile, mobilities[index])
        for route in _routes_of(world):
            assert repr(_rules().route_with_movement_time(world, agent_id, route)) == repr(
                _rule("route_with_movement_time")(world, agent_id, route)
            )


def _with_mobility(profile: Any, mobility: Any) -> Any:
    return replace(profile, mobility=mobility)


def test_a_route_s_minutes_without_a_model_leave_the_route(generated_scenario: Any) -> None:
    world = _run(generated_scenario, 5)
    world.model_configs.pop("movement_model", None)
    del world.model_configs["facility_geometry"]
    route = _routes_of(world)[0]
    assert _rules().route_with_movement_time(world, sorted(world.agents)[0], route) is route


@pytest.mark.parametrize(
    "model",
    [
        {"extra": 1},
        {"speed_m_per_minute_by_mobility": {"independent": 60}},
        {"speed_m_per_minute_by_mobility": {"independent": 60, "assisted": 21, "wheelchair": 33}},
        {"maximum_density_slowdown": 0.5},
        {"density_slowdown_per_person_m2": 1},
        {"schema_version": 2},
    ],
)
def test_a_model_the_kernel_does_not_decide_goes_to_python(
    generated_scenario: Any, model: dict[str, Any]
) -> None:
    world = _run(generated_scenario, 5)
    world.model_configs["movement_model"] = model
    route = _routes_of(world)[0]
    with pytest.raises(native.Unsupported):
        _rules().route_with_movement_time(world, sorted(world.agents)[0], route)


# ---- Random floor plans -------------------------------------------------------------------------


def _random_polygon(
    rng: random.Random, cx: float, cy: float, radius: float
) -> list[dict[str, float]]:
    count = rng.randint(3, 9)
    angles = sorted(rng.uniform(0.0, math.tau) for _ in range(count))
    return [
        {
            "x": cx + rng.uniform(0.2, 1.0) * radius * math.cos(angle),
            "y": cy + rng.uniform(0.2, 1.0) * radius * math.sin(angle),
        }
        for angle in angles
    ]


def _random_plan(rng: random.Random) -> tuple[dict[str, Any], dict[str, Any]]:
    size = 100.0
    zones = []
    for index in range(4):
        cx, cy = rng.uniform(25, 75), rng.uniform(25, 75)
        polygon = _random_polygon(rng, cx, cy, rng.choice([0.5, 5.0, 20.0]))
        if rng.random() < 0.1:
            # No area at all: the mean of its corners.
            polygon = [{"x": cx, "y": cy}, {"x": cx + 1.0, "y": cy}, {"x": cx + 2.0, "y": cy}]
        zones.append(
            {
                "zone_id": f"z{index}",
                "polygon": polygon,
                "usable_area_m2": rng.uniform(1e-6, 1e-5),
            }
        )
    routes = [
        {
            "edge_id": f"e{index}",
            "points": [
                {"x": rng.uniform(0, size), "y": rng.uniform(0, size)}
                for _ in range(rng.randint(2, 4))
            ],
            "width_m": rng.choice([0.4, 0.85, 1.2, 3.0]),
            "accessible": rng.random() < 0.7,
        }
        for index in range(5)
    ]
    geometry = {
        "width_m": size,
        "height_m": size,
        "source_kind": "synthetic",
        "zones": zones,
        "routes": routes,
    }
    walls = [
        {
            "id": f"w{index}",
            "start": {"x": rng.uniform(0, size), "y": rng.uniform(0, size)},
            "end": {"x": rng.uniform(0, size), "y": rng.uniform(0, size)},
        }
        for index in range(rng.randint(0, 3))
    ]
    openings = []
    for index, wall in enumerate(walls):
        share = rng.random()
        openings.append(
            {
                "id": f"o{index}",
                "wall_id": wall["id"],
                "center": {
                    "x": wall["start"]["x"] + (wall["end"]["x"] - wall["start"]["x"]) * share,
                    "y": wall["start"]["y"] + (wall["end"]["y"] - wall["start"]["y"]) * share,
                },
                "width_m": rng.uniform(0.5, 30.0),
                "edge_ids": [f"e{rng.randrange(5)}"],
                "accessible": rng.random() < 0.7,
            }
        )
    constraints = {
        "walls": walls,
        "openings": openings,
        "obstacles": [
            {
                "id": f"b{index}",
                "polygon": _random_polygon(
                    rng, rng.uniform(10, 90), rng.uniform(10, 90), rng.uniform(0.5, 8.0)
                ),
            }
            for index in range(rng.randint(0, 4))
        ],
    }
    return geometry, constraints


def _inside(rng: random.Random, polygon: list[dict[str, float]]) -> Point2D:
    a, b, c = rng.sample(polygon, 3)
    x, y = sorted((rng.random(), rng.random()))
    return Point2D(
        x=a["x"] * x + b["x"] * (y - x) + c["x"] * (1 - y),
        y=a["y"] * x + b["y"] * (y - x) + c["y"] * (1 - y),
    )


def test_random_floor_plans_are_python_s_bit_for_bit() -> None:
    rng = random.Random(20261101)
    seen = {"centers": 0, "clear": 0, "blocked": 0, "routes": 0, "no_route": 0, "turns": 0}
    seen["turned_down"] = 1
    for _ in range(250):
        geometry, constraints = _random_plan(rng)
        try:
            FacilityGeometry.model_validate(geometry)
        except ValueError:
            seen["turned_down"] += 1
            continue
        world = WorldState(
            "s", 1, 100, model_state={}, model_configs={"facility_geometry": geometry}
        )
        if rng.random() < 0.8:
            world.model_configs["spatial_constraints"] = constraints
        for zone in geometry["zones"]:
            assert _rules().zone_center(world, zone["zone_id"]) == _python_center(
                world, zone["zone_id"]
            )
            seen["centers"] += 1
        for route in geometry["routes"]:
            for mobility in ("independent", "assisted", "wheelchair", "other"):
                clear = _rules().route_is_physically_clear(
                    world, route["edge_id"], mobility=mobility
                )
                assert clear == _rule("route_is_physically_clear")(
                    world, route["edge_id"], mobility=mobility
                )
                seen["clear" if clear else "blocked"] += 1
        for zone in geometry["zones"]:
            for mobility in ("independent", "assisted", "wheelchair"):
                start = _inside(rng, zone["polygon"]) if rng.random() < 0.9 else None
                destination = _inside(rng, zone["polygon"])
                options = {
                    "zone_id": zone["zone_id"],
                    "start": start,
                    "destination": destination,
                    "mobility": mobility,
                }
                path = _rules().plan_in_room_route(world, **options)
                assert repr(path) == repr(_rule("plan_in_room_route")(world, **options))
                seen["routes" if path else "no_route"] += 1
                seen["turns"] += bool(path) and len(path) > 2
    assert all(seen.values()), seen


def test_the_route_of_a_plan_is_found_again_the_same(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    geometry = movement._geometry(world)
    zone = geometry.zones[0]
    center = movement._zone_center(geometry, zone.zone_id)
    options = {
        "zone_id": zone.zone_id,
        "start": center,
        "destination": center,
        "mobility": "independent",
    }
    with geometry_config_scope():
        first = _rules().plan_in_room_route(world, **options)
        assert repr(first) == repr(_rules().plan_in_room_route(world, **options))


# ---- The validator ------------------------------------------------------------------------------


def _set(*path: Any) -> Any:
    def change(world: WorldState, agent_id: str) -> None:
        record: Any = world.model_state[KEY]
        for part in path[:-2]:
            record = record[agent_id if part is None else part]
        record[path[-2]] = path[-1]

    return change


BROKEN_JOURNEYS = {
    "unknown destination": _set("journeys", None, "destination_zone_id", "nowhere"),
    "unknown zone on the route": _set("journeys", None, "route_zone_ids", ["nowhere"]),
    "unknown edge": _set("journeys", None, "route_edge_ids", ["nowhere"]),
    "due before it starts": _set("journeys", None, "due_minute", -5),
    "support fields on another trip": _set("journeys", None, "support_request_id", "r"),
    "queue on another trip": _set("journeys", None, "queue_id", "q"),
    "one room point": _set("journeys", None, "route_points", [{"x": 1.0, "y": 1.0}]),
    "room points beyond the room": _set(
        "journeys", None, "route_points", [{"x": 1e6, "y": 1e6}, {"x": 2e6, "y": 2e6}]
    ),
    "position out of range": _set("positions", None, "progress", 1.5),
    "position on an unknown edge": _set("positions", None, "edge_id", "nowhere"),
    "another kind of position": _set("positions", None, "position_kind", "nowhere"),
    "a person nobody knows": _set(
        "positions",
        "nobody",
        {
            "zone_id": "hall",
            "point": None,
            "edge_id": None,
            "progress": 0.0,
            "position_kind": "unavailable",
        },
    ),
}


@pytest.mark.parametrize("broken", BROKEN_JOURNEYS)
def test_the_validator_takes_nothing_the_python_rule_refuses(
    generated_scenario: Any, broken: str
) -> None:
    world = _run(generated_scenario, 5)
    agent_id = _walker(world)
    state = world.model_state[KEY]
    state["journeys"][agent_id]["purpose"] = "walk"
    assert _rules().validate(world) is True
    movement.validate_movement_runtime_state(world)
    BROKEN_JOURNEYS[broken](world, agent_id)
    try:
        accepted = _rules().validate(world)
    except native.Unsupported:
        accepted = False
    assert accepted is False
    with pytest.raises(ValueError, match="movement_v"):
        movement.validate_movement_runtime_state(world)


# ---- The walk to a facility's footprint ---------------------------------------------------------


def test_the_walk_to_a_facility_with_a_footprint_is_python_s(generated_scenario: Any) -> None:
    """The points around a footprint, for each mobility: its clearances are in Rust too."""
    from sheltersim.domain.models import Mobility

    world = _run(generated_scenario, 2)
    geometry = movement._geometry(world)
    configs = world.model_configs
    ids = sorted(world.agents)[:3]
    mobilities = (Mobility.INDEPENDENT, Mobility.ASSISTED, Mobility.WHEELCHAIR)
    found = 0
    for facility_id, point in sorted(geometry.facility_points.items()):
        zone_id = world.facilities[facility_id].zone_id
        center = movement._zone_center(geometry, zone_id)
        half = 0.2
        polygon = [
            {"x": point.x + dx, "y": point.y + dy}
            for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half))
        ]
        configs["spatial_constraints"] = {
            "equipment_footprints": [
                {"facility_id": facility_id, "polygon": polygon, "area_m2": 0.16},
            ]
        }
        for agent_id, mobility in zip(ids, mobilities, strict=True):
            agent = world.agents[agent_id]
            agent.profile = _with_mobility(agent.profile, mobility)
            agent.zone_id = zone_id
            world.model_state[KEY]["positions"][agent_id]["point"] = {"x": center.x, "y": center.y}
            route = _same_after(world, "facility_approach_route", agent_id, facility_id)
            found += route is not None and len(route[1]) >= 2
    assert found
