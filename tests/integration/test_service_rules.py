"""The kernels of rules/proposals.rs, rules/service.rs and rules/travel.rs against the Python rules.

Each case runs the kernel on a world and the Python rule on a copy made before it, and compares the
whole world in canonical JSON. benchmarks/rule_shadow/shadow_service.py and shadow_proposals.py do
this on the runs of the gate; here, the branches those runs may not reach, the configured values
at what changes the result, and the state of resource_service_v1
(docs/rust-port/lanes/m4-service.md).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain import movement
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.domain.resource_service import SERVICE_STATE_KEY as KEY
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.behavior import ActionKind

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _run(scenario: Any, minutes: int) -> tuple[WorldState, OfflineSimulationController]:
    world = WorldState.from_scenario(scenario)
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
    return world, controller


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _same_world(world: WorldState, twin: WorldState) -> None:
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert [repr(event) for event in world.event_queue.pending()] == [
        repr(event) for event in twin.event_queue.pending()
    ]
    mirror = world._core.mirror(KEY)
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])
    assert world._core.entities().verify(world) is None


def _idle_people(world: WorldState, count: int) -> list[str]:
    out = []
    for agent_id in sorted(world.agents):
        if agent_id in world.model_state[MOVEMENT_STATE_KEY]["journeys"]:
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


def _proposal(
    action: ActionKind, agent_id: str, world: WorldState, target: str | None = None
) -> Any:
    return SimpleNamespace(
        action=action,
        agent_id=agent_id,
        next_decision_at=world.minute_index + 7,
        target_id=target,
    )


def _effect(world: WorldState, controller: Any, proposal: Any) -> Any:
    """The kernel on the world, the Python step on a copy: the same result and the same world."""
    twin = copy.deepcopy(world)
    python_controller = _controller(twin)
    result = native.rules("proposals").apply_effect(controller, proposal)
    expected = ProposePhase._apply_effect(python_controller, proposal)
    assert list(result) == list(expected)
    assert type(result[2]) is type(expected[2])
    _same_world(world, twin)
    return result


def _same_zone_pair(world: WorldState) -> tuple[str, str]:
    people = _idle_people(world, 100)
    for first in people:
        for second in people:
            if first != second and world.agents[first].zone_id == world.agents[second].zone_id:
                return first, second
    pytest.fail("no two people in one zone")


# ---- _apply_effect ------------------------------------------------------------------------------


@pytest.mark.parametrize("action", [ActionKind.WAIT, ActionKind.REST, ActionKind.SLEEP])
def test_a_still_action_is_python_s(generated_scenario: Any, action: ActionKind) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_people(world, 1)[0]
    world.agents[agent_id].activity = Activity.RESTING
    world.agents[agent_id].target_id = "kitchen"
    _effect(world, controller, _proposal(action, agent_id, world))


@pytest.mark.parametrize(
    "action", [ActionKind.SOCIALIZE, ActionKind.CARE_FAMILY, ActionKind.HELP_OTHERS]
)
def test_a_social_action_schedules_python_s_event(
    generated_scenario: Any, action: ActionKind
) -> None:
    world, controller = _run(generated_scenario, 5)
    first, second = _same_zone_pair(world)
    pending = len(world.event_queue.pending())
    target, next_decision, committed = _effect(
        world, controller, _proposal(action, first, world, second)
    )
    assert (target, committed) == (second, True)
    assert next_decision == world.minute_index + 5
    assert len(world.event_queue.pending()) == pending + 1


def test_the_config_s_minutes_and_their_end_of_the_run(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    first, second = _same_zone_pair(world)
    world.model_configs["body_dynamics"]["social_interaction_minutes"] = 11
    _, next_decision, _ = _effect(
        world, controller, _proposal(ActionKind.SOCIALIZE, first, world, second)
    )
    assert next_decision == world.minute_index + 11
    first, second = _same_zone_pair(world)
    # Past the last minute there is no event, and the planned decision stands.
    world.model_configs["body_dynamics"]["social_interaction_minutes"] = world.duration_minutes
    assert _effect(world, controller, _proposal(ActionKind.SOCIALIZE, first, world, second)) == (
        None,
        world.minute_index + 7,
        False,
    )


def test_without_a_config_the_default_minutes_are_python_s(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    first, second = _same_zone_pair(world)
    del world.model_configs["body_dynamics"]["social_interaction_minutes"]
    twin_minutes = _effect(
        world, controller, _proposal(ActionKind.SOCIALIZE, first, world, second)
    )[1]
    assert twin_minutes == world.minute_index + 5


def test_a_social_action_needs_someone_else_in_the_zone(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    first = _idle_people(world, 1)[0]
    other_zone = next(
        item for item in world.agents.values() if item.zone_id != world.agents[first].zone_id
    )
    for target in (None, first, "nobody", other_zone.id):
        assert _effect(
            world, controller, _proposal(ActionKind.HELP_OTHERS, first, world, target)
        ) == (
            None,
            world.minute_index + 7,
            False,
        )


def test_a_held_bed_is_given_up_and_sleep_keeps_it(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    first, second = _idle_people(world, 2)
    reservations = world.model_state["geometry_overlay_v1"]["bed_reservations"]
    for agent_id in (first, second):
        reservations[agent_id] = {
            "agent_id": agent_id,
            "room_id": "room",
            "bed_index": 0,
            "target_id": "bed-x",
            "reserved_minute": 0,
        }
    # Reservations of no real room: the world would not validate, so no comparison of worlds.
    kernel = native.rules("proposals").apply_effect
    kernel(controller, _proposal(ActionKind.WAIT, first, world))
    kernel(controller, _proposal(ActionKind.SLEEP, second, world))
    assert list(reservations) == [second]


def test_a_room_with_beds_is_python_s_to_reserve(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    world.model_configs.setdefault("geometry_overlay", {})["schema_version"] = 4
    world.model_state["geometry_overlay_v1"]["temporary_rooms"]["room"] = {}
    before = _bytes(world)
    agent_id = _idle_people(world, 1)[0]
    with pytest.raises(native.Unsupported, match="temporary room"):
        native.rules("proposals").apply_effect(
            controller, _proposal(ActionKind.SLEEP, agent_id, world)
        )
    assert _bytes(world) == before


def test_a_position_point2d_refuses_is_python_s_error(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    world.model_configs.setdefault("geometry_overlay", {})["schema_version"] = 4
    agent_id = _idle_people(world, 1)[0]
    world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id]["point"]["x"] = float("nan")
    with pytest.raises(native.Unsupported):
        native.rules("proposals").apply_effect(
            controller, _proposal(ActionKind.SLEEP, agent_id, world)
        )


@pytest.mark.parametrize(
    "action",
    [ActionKind.REQUEST_TOILET, ActionKind.FETCH_WATER, ActionKind.COLLECT_MEAL],
)
def test_each_trip_action_from_each_zone(generated_scenario: Any, action: ActionKind) -> None:
    world, controller = _run(generated_scenario, 5)
    ran = 0
    for agent_id in _idle_people(world, 100):
        for zone_id in sorted(world.zones):
            agent = world.agents[agent_id]
            agent.zone_id = zone_id
            world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id]["zone_id"] = zone_id
            # A person at their facility asks its queue at once, which Python does.
            try:
                _effect(world, controller, _proposal(action, agent_id, world))
            except native.Unsupported as refused:
                assert "at once" in str(refused)
            else:
                ran += 1
            if agent.activity is Activity.MOVING:
                break
        if ran >= 12:
            break
    assert ran


def test_a_trip_past_the_end_of_the_run_is_refused_as_python_refuses(
    generated_scenario: Any,
) -> None:
    world, controller = _run(generated_scenario, 5)
    world.duration_minutes = world.minute_index + 1
    for action in (ActionKind.FETCH_WATER, ActionKind.REQUEST_TOILET, ActionKind.COLLECT_MEAL):
        agent_id = _idle_people(world, 100)[0]
        try:
            result = _effect(world, controller, _proposal(action, agent_id, world))
        except native.Unsupported as refused:
            assert "at once" in str(refused)
        else:
            assert result[0] is None


def test_no_known_facility_is_recorded_as_python_records(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_people(world, 1)[0]
    known = world.model_state["agent_dynamics_v1"]["agents"][agent_id]["known_facility_ids"]
    known[:] = []
    for action in (ActionKind.FETCH_WATER, ActionKind.COLLECT_MEAL, ActionKind.REQUEST_TOILET):
        assert _effect(world, controller, _proposal(action, agent_id, world))[0] is None
    history = world.model_state[KEY]["request_history"]
    assert [record["reason"] for record in history[-2:]] == ["no_facility", "no_facility"]
    assert world.metric_state["resource_service_request_rejected_count"] >= 2.0


def test_seek_help_goes_to_python(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_people(world, 1)[0]
    for action in (ActionKind.SEEK_HELP, ActionKind.SEEK_INFORMATION):
        with pytest.raises(native.Unsupported, match="staff service"):
            native.rules("proposals").apply_effect(controller, _proposal(action, agent_id, world))


# ---- _route and _schedule_travel ----------------------------------------------------------------


def test_every_route_is_python_s_for_every_mobility(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    kernel = native.rules("travel").route
    seen = set()
    for agent_id in sorted(world.agents):
        mobility = world.agents[agent_id].profile.mobility
        if mobility in seen:
            continue
        seen.add(mobility)
        for origin in sorted(world.zones):
            for destination in sorted(world.zones):
                result = kernel(controller, origin, destination, agent_id)
                expected = ProposePhase._route(controller, origin, destination, agent_id)
                assert repr(result) == repr(expected)
    assert seen


def test_an_equal_time_goes_to_the_lower_edge_id(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    kernel = native.rules("travel").route
    agent_id = _idle_people(world, 1)[0]
    # The graph's own travel times equal: only the IDs can decide.
    from sheltersim.domain.zones import ZoneConnection, ZoneGraph
    from sheltersim.policies.space import SpaceRouter

    zones = list(controller.space.graph.zones)[:4]
    controller.space = SpaceRouter(
        ZoneGraph(
            zones={zone: controller.space.graph.zones[zone] for zone in zones},
            edges=(
                ZoneConnection("z-b", zones[0], zones[1], 1),
                ZoneConnection("a-c", zones[0], zones[2], 1),
                ZoneConnection("m-d", zones[1], zones[3], 1),
                ZoneConnection("n-d", zones[2], zones[3], 1),
            ),
        )
    )
    route = kernel(controller, zones[0], zones[3], agent_id)
    assert route.edge_ids == ("a-c", "n-d")
    assert repr(route) == repr(ProposePhase._route(controller, zones[0], zones[3], agent_id))


def test_an_edge_capacity_and_another_policy_go_to_python(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    kernel = native.rules("travel").route
    agent_id = _idle_people(world, 1)[0]
    zone = world.agents[agent_id].zone_id
    controller.route_capacities = {"any": 1}
    with pytest.raises(native.Unsupported, match="capacities"):
        kernel(controller, zone, zone, agent_id)
    controller.route_capacities = {}
    from sheltersim.policies.space import SpaceRouter

    class Other:
        def route(self, *arguments: Any, **options: Any) -> None:
            return None

    controller.space = SpaceRouter(controller.space.graph, Other())
    with pytest.raises(native.Unsupported, match="policy"):
        kernel(controller, zone, zone, agent_id)


def _travel_same(world: WorldState, controller: Any, **options: Any) -> Any:
    twin = copy.deepcopy(world)
    python_controller = _controller(twin)
    result = native.rules("travel").schedule_travel(controller, **options)
    expected = ProposePhase._schedule_travel(python_controller, **options)
    assert result is expected
    _same_world(world, twin)
    return result


def test_a_trip_and_each_limit_are_python_s(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    first, second = _idle_people(world, 2)
    origin = world.agents[first].zone_id
    destination = next(
        zone
        for zone in sorted(world.zones)
        if zone != origin and controller._route(origin, zone, first)
    )
    route = controller._route(origin, destination, first)
    common = {
        "agent_id": first,
        "route": route,
        "purpose": "return",
        "facility_id": None,
        "minute": world.minute_index,
        "validate_state": False,
    }
    # The zone is full, counting who walks there.
    capacity = world.zones[destination].capacity
    world.zones[destination].capacity = 0
    assert _travel_same(world, controller, **common) is False
    world.zones[destination].capacity = capacity
    # It ends past the run.
    saved = world.duration_minutes
    world.duration_minutes = world.minute_index + route.travel_minutes
    assert _travel_same(world, controller, **common) is False
    world.duration_minutes = saved
    assert _travel_same(world, controller, **common) is True
    assert world.agents[first].activity is Activity.MOVING


def test_a_trip_refused_writes_nothing(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    first = _idle_people(world, 1)[0]
    origin = world.agents[first].zone_id
    destination = next(
        zone
        for zone in sorted(world.zones)
        if zone != origin and controller._route(origin, zone, first)
    )
    route = controller._route(origin, destination, first)
    before = _bytes(world)
    kernel = native.rules("travel").schedule_travel
    options = {
        "agent_id": first,
        "route": route,
        "purpose": "return",
        "facility_id": None,
        "minute": world.minute_index,
    }
    with pytest.raises(native.Unsupported, match="whole validation"):
        kernel(controller, **options, validate_state=True)
    with pytest.raises(native.Unsupported, match="support trip"):
        kernel(controller, **{**options, "purpose": "staff_support_travel"}, validate_state=False)
    other = next(item for item in sorted(world.agents) if world.agents[item].zone_id != origin)
    with pytest.raises(native.Unsupported, match="starts elsewhere"):
        kernel(controller, **{**options, "agent_id": other}, validate_state=False)
    assert _bytes(world) == before


# ---- arbitrate ----------------------------------------------------------------------------------


def _service_facility(world: WorldState, kind: str) -> Any:
    return next(
        item
        for item in sorted(world.facilities.values(), key=lambda item: item.id)
        if item.kind == kind
    )


def _at_service(world: WorldState, kind: str = "water_station") -> tuple[Any, str]:
    """Someone first in line and standing at the service point, with a free person on staff."""
    facility = _service_facility(world, kind)
    facility.resource_queue_agent_ids.clear()
    agent_id = _idle_people(world, 1)[0]
    agent = world.agents[agent_id]
    agent.zone_id = facility.zone_id
    agent.activity = Activity.QUEUED
    agent.target_id = facility.id
    facility.resource_queue_agent_ids.append(agent_id)
    task_id = f"queue-task-{agent_id}"
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="resource_service_queue",
        agent_id=agent_id,
        resource_id=facility.id,
        start_minute=max(world.minute_index - 3, 0),
        due_minute=max(world.minute_index - 3, 0),
    )
    point = movement._geometry(world).facility_points[facility.id]
    world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id].update(
        zone_id=facility.zone_id,
        point={"x": point.x, "y": point.y},
        edge_id=None,
        progress=0.0,
        position_kind="physical",
    )
    return facility, agent_id


def _serve(world: WorldState, facility: Any, resource: str = "water_ml") -> Any:
    twin = copy.deepcopy(world)
    manager = ResourceServiceManager(world)
    python_manager = ResourceServiceManager(twin)
    result = native.rules("service").serve_head(manager, facility.id, resource, world.minute_index)
    expected = ResourceServiceManager._serve_head.__wrapped__(
        python_manager, facility.id, resource, world.minute_index
    )
    assert result == expected
    _same_world(world, twin)
    return result


def _free_all_staff(world: WorldState, facility: Any) -> None:
    for person in world.staff.values():
        person.zone_id = facility.zone_id
        person.active_task_id = None
        person.work_queue[:] = []


def test_a_head_at_the_point_starts_its_service(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_service(world)
    _free_all_staff(world, facility)
    event_id = _serve(world, facility)
    assert event_id is not None
    assert world.agents[agent_id].activity is Activity.IN_SERVICE
    assert world.event_queue.find(event_id).event_type == "resource_service_complete"
    assert world.metric_state["water_service_wait_count"] >= 1.0


@pytest.mark.parametrize("reason", ["capacity", "staff", "stock", "run_end", "water_outage"])
def test_each_wait_is_python_s_record(generated_scenario: Any, reason: str) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_service(world)
    _free_all_staff(world, facility)
    if reason == "capacity":
        facility.working_units = 0
    elif reason == "staff":
        for person in world.staff.values():
            person.zone_id = "elsewhere"
    elif reason == "stock":
        ledger = world.model_state["resource_ledger_v1"]["accounts"]["water_ml"]
        world.model_configs["resource_service"]["water_ml_per_service"] = ledger["remaining"] + 1
    elif reason == "run_end":
        world.duration_minutes = world.minute_index + 1
    else:
        world.model_state["operational_events_v1"]["water_outages"]["cut"] = {
            "active": True,
            "target_kind": "facility",
            "target_id": facility.id,
        }
    if reason == "stock":
        manager = ResourceServiceManager(world)
        twin = copy.deepcopy(world)
        python_manager = ResourceServiceManager(twin)
        assert (
            native.rules("service").serve_head(manager, facility.id, "water_ml", world.minute_index)
            is None
        )
        assert (
            ResourceServiceManager._serve_head.__wrapped__(
                python_manager, facility.id, "water_ml", world.minute_index
            )
            is None
        )
        _same_world(world, twin)
    else:
        assert _serve(world, facility) is None
    record = world.model_state[KEY]["stall_state"][facility.id]
    assert record == {"agent_id": agent_id, "reason": reason}
    history = list(world.model_state[KEY]["queue_stall_history"])
    # The same wait the next minute adds nothing.
    if reason != "stock":
        assert _serve(world, facility) is None
        assert list(world.model_state[KEY]["queue_stall_history"]) == history


def test_a_taken_reservation_id_is_python_s_error(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_service(world)
    world.model_state["resource_ledger_v1"]["used_reservation_ids"].append(f"queue-task-{agent_id}")
    before = _bytes(world)
    with pytest.raises(native.Unsupported, match="reservation"):
        native.rules("service").serve_head(
            ResourceServiceManager(world), facility.id, "water_ml", world.minute_index
        )
    assert _bytes(world) == before
    with pytest.raises(StateInvariantError, match="conflicts with an H01 reservation"):
        ResourceServiceManager(world)._serve_head(facility.id, "water_ml", world.minute_index)


def test_only_the_queues_with_people_are_served(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    for facility in world.facilities.values():
        facility.resource_queue_agent_ids.clear()
    manager = ResourceServiceManager(world)
    kernel = native.rules("service").facilities_to_serve
    assert kernel(manager) == []
    facility, _ = _at_service(world, "meal_distribution")
    assert kernel(manager) == [(facility, "food_meal_units")]


# ---- complete -----------------------------------------------------------------------------------


def _started(world: WorldState, kind: str, resource: str) -> tuple[Any, str, Any]:
    facility, agent_id = _at_service(world, kind)
    _free_all_staff(world, facility)
    event_id = _serve(world, facility, resource)
    return world.event_queue.find(event_id), agent_id, facility


@pytest.mark.parametrize(
    ("kind", "resource", "need"),
    [
        ("water_station", "water_ml", "hydration_need"),
        ("meal_distribution", "food_meal_units", "hunger"),
    ],
)
def test_a_completion_relieves_python_s_need(
    generated_scenario: Any, kind: str, resource: str, need: str
) -> None:
    world, _ = _run(generated_scenario, 5)
    event, agent_id, _ = _started(world, kind, resource)
    setattr(world.agents[agent_id].needs, need, 0.812345)
    twin = copy.deepcopy(world)
    python_manager = ResourceServiceManager(twin)
    native.rules("service").complete(ResourceServiceManager(world), event)
    ResourceServiceManager.complete.__wrapped__(python_manager, event)
    _same_world(world, twin)
    assert world.agents[agent_id].activity is Activity.IDLE


def test_the_relief_is_read_at_its_value_and_stops_at_zero(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    event, agent_id, _ = _started(world, "water_station", "water_ml")
    world.agents[agent_id].needs.hydration_need = 0.5
    manager = ResourceServiceManager(world)
    manager.config["hydration_relief"] *= 1.01
    relief = manager.config["hydration_relief"]
    native.rules("service").complete(manager, event)
    assert world.agents[agent_id].needs.hydration_need == max(0.0, 0.5 - relief)
    assert relief != ResourceServiceManager(world).config["hydration_relief"]


@pytest.mark.parametrize(
    "change",
    [
        lambda world, event, agent_id: world.staff[event.payload["staff_id"]].__setattr__(
            "busy_until", 0
        ),
        lambda world, event, agent_id: world.model_state[KEY]["active_reservation_ids"].clear(),
        lambda world, event, agent_id: world.facilities[
            event.payload["facility_id"]
        ].resource_active_agent_ids.clear(),
    ],
)
def test_a_completion_that_does_not_match_is_python_s_error(
    generated_scenario: Any, change: Any
) -> None:
    world, _ = _run(generated_scenario, 5)
    event, agent_id, _ = _started(world, "water_station", "water_ml")
    change(world, event, agent_id)
    before = _bytes(world)
    with pytest.raises(native.Unsupported):
        native.rules("service").complete(ResourceServiceManager(world), event)
    assert _bytes(world) == before


# ---- request and its record ---------------------------------------------------------------------


def _at_facility(world: WorldState, kind: str = "water_station") -> tuple[Any, str]:
    facility = _service_facility(world, kind)
    agent_id = _idle_people(world, 1)[0]
    agent = world.agents[agent_id]
    agent.zone_id = facility.zone_id
    agent.activity = Activity.IDLE
    agent.target_id = None
    return facility, agent_id


def test_a_request_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_facility(world)
    twin = copy.deepcopy(world)
    result = native.rules("service").request(
        ResourceServiceManager(world), agent_id, facility.id, validate_state=False
    )
    expected = ResourceServiceManager.request.__wrapped__(
        ResourceServiceManager(twin), agent_id, facility.id, validate_state=False
    )
    assert repr(result) == repr(expected)
    _same_world(world, twin)


@pytest.mark.parametrize(
    ("change", "text"),
    [
        (
            lambda world, agent_id: world.agents[agent_id].__setattr__("zone_id", "kitchen"),
            "must arrive",
        ),
        (
            lambda world, agent_id: world.agents[agent_id].__setattr__("activity", Activity.MOVING),
            "while active",
        ),
    ],
)
def test_a_request_refused_has_python_s_text(
    generated_scenario: Any, change: Any, text: str
) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_facility(world)
    change(world, agent_id)
    if world.agents[agent_id].zone_id == facility.zone_id and "arrive" in text:
        pytest.skip("the scenario's facility is in that zone")
    manager = ResourceServiceManager(world)
    before = _bytes(world)
    with pytest.raises(native.Unsupported):
        native.rules("service").request(manager, agent_id, facility.id, validate_state=False)
    assert _bytes(world) == before
    with pytest.raises(ValueError, match=text):
        manager.request(agent_id, facility.id, validate_state=False)


def test_a_full_validation_of_a_request_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_facility(world)
    with pytest.raises(native.Unsupported, match="whole validation"):
        native.rules("service").request(ResourceServiceManager(world), agent_id, facility.id)


def test_a_record_of_a_request_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    facility, agent_id = _at_facility(world)
    twin = copy.deepcopy(world)
    options = {
        "agent_id": agent_id,
        "action": "fetch_water",
        "outcome": "rejected",
        "reason": "travel_rejected",
        "facility_id": facility.id,
        "origin_zone_id": facility.zone_id,
        "detail": "text",
    }
    native.rules("service").record_request_outcome(ResourceServiceManager(world), **options)
    ResourceServiceManager.record_request_outcome.__wrapped__(
        ResourceServiceManager(twin), **options
    )
    _same_world(world, twin)


# ---- The state ----------------------------------------------------------------------------------


def test_the_row_is_the_tree_and_a_tick_writes_no_whole_namespace(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 1)
    mirror = world._core.mirror(KEY)
    before = mirror.stats()["whole_writes"]
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(120):
        engine.step()
    assert mirror.stats()["whole_writes"] == before
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])
    assert rust_validated(world, KEY)


def test_a_person_leaving_rewrites_the_histories_through_the_row(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    state = world.model_state[KEY]
    state["request_history"].append(
        {
            "minute": world.minute_index,
            "agent_id": "A001",
            "action": "fetch_water",
            "outcome": "rejected",
            "reason": "no_facility",
            "facility_id": None,
            "origin_zone_id": sorted(world.zones)[0],
            "detail": None,
        }
    )
    state["request_history"] = [
        item for item in state["request_history"] if item["agent_id"] != "A001"
    ]
    mirror = world._core.mirror(KEY)
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(state)


def test_a_bad_value_taints_and_goes_to_python(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    state = world.model_state[KEY]
    state["request_history"].append({"minute": "soon"})
    assert world._core.mirror(KEY).tainted
    assert not rust_validated(world, KEY)
    with pytest.raises(StateInvariantError, match="request history is invalid"):
        world.validate(validate_event_history=False)
    facility, agent_id = _at_facility(world)
    with pytest.raises(native.Unsupported):
        native.rules("service").request(
            ResourceServiceManager(world), agent_id, facility.id, validate_state=False
        )


@pytest.mark.parametrize(
    ("write", "text"),
    [
        (
            lambda state, world: state["stall_state"].__setitem__(
                _service_facility(world, "water_station").id,
                {"agent_id": _idle_people(world, 1)[0], "reason": "capacity"},
            ),
            "must describe the current facility queue head",
        ),
        (
            lambda state, world: state["stall_state"].__setitem__(
                _service_facility(world, "water_station").id,
                {"agent_id": sorted(world.agents)[0], "reason": "weather"},
            ),
            "stall state is invalid",
        ),
        (
            lambda state, world: state["cancelled_task_ids"].extend(["same", "same"]),
            "resource_service_v1 state is invalid",
        ),
        (
            lambda state, world: state["queue_stall_history"].append(
                {
                    "minute": world.minute_index + 1,
                    "facility_id": sorted(world.facilities)[0],
                    "agent_id": sorted(world.agents)[0],
                    "reason": "staff",
                }
            ),
            "stall history is invalid",
        ),
    ],
)
def test_the_tick_s_check_takes_nothing_python_refuses(
    generated_scenario: Any, write: Any, text: str
) -> None:
    world, _ = _run(generated_scenario, 5)
    world.validate(validate_event_history=False)
    write(world.model_state[KEY], world)
    assert not rust_validated(world, KEY)
    with pytest.raises(StateInvariantError, match=text):
        world.validate(validate_event_history=False)


def test_the_save_check_still_reads_the_whole_history(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    state = world.model_state[KEY]
    state["request_history"].insert(
        0,
        {
            "minute": 0,
            "agent_id": "nobody",
            "action": "fetch_water",
            "outcome": "rejected",
            "reason": "no_facility",
            "facility_id": None,
            "origin_zone_id": sorted(world.zones)[0],
            "detail": None,
        },
    )
    world.validate(validate_event_history=False)
    with pytest.raises(StateInvariantError, match="request history is invalid"):
        world.validate()


def test_a_deep_copy_has_its_own_row(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    twin = copy.deepcopy(world)
    twin.model_state[KEY]["cancelled_task_ids"].append("gone")
    assert world.model_state[KEY]["cancelled_task_ids"] == []
    assert world._core.mirror(KEY).verify()
    assert twin._core.mirror(KEY).verify()
