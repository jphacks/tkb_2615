"""`_replan_journeys` and `_replan_staff_support_trip`, the kernel against the Python step.

A replan is rare: one call in the gate and none in the 7-day run, so these tests are what compares
most of its branches. Each runs the kernel on the world and the Python step on a copy with its own
controller, and the whole worlds must be the same, floats by their bits.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_source_ids_for_agent,
)
from sheltersim.domain.geometry_overlay import GEOMETRY_OVERLAY_KEY, reserve_sleep_bed
from sheltersim.domain.models import Activity, SupportRequestStatus, ZoneEdgeState
from sheltersim.domain.movement import MOVEMENT_STATE_KEY as KEY
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.events.effects import (
    EffectProgram,
    EffectProgramGeometryHandler,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventStatus
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.update import UpdatePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

ALTERNATE = "route_sleeping_toilet_alt"
FIRST_LEG = "route_sleeping_corridor"
SLOW = 0.2


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _records(world: WorldState) -> list[Any]:
    return [
        (
            record.event.event_id,
            record.event.sequence,
            record.event.causation_id,
            record.realized_minute,
            record.status,
            record.detail,
            sorted(record.event.payload.items(), key=lambda item: item[0]),
        )
        for record in world.event_log.records
    ]


def _pending(world: WorldState) -> list[Any]:
    return [
        (
            event.event_id,
            event.sequence,
            event.due_minute,
            event.source_id,
            event.causation_id,
            sorted(event.payload.items(), key=lambda item: item[0]),
            sorted(event.provenance.items(), key=lambda item: item[0]),
        )
        for event in world.event_queue.pending()
    ]


def _same_replan(world: WorldState, agent_ids: tuple[str, ...], minute: int) -> None:
    """The step on the world, the Python step on a copy: both must have done the same."""
    twin = copy.deepcopy(world)
    twin_controller = _controller(twin)
    controller = _controller(world)
    native.rules("replan").replan_journeys(controller, world.model_state[KEY], agent_ids, minute)
    UpdatePhase._replan_in_python(twin_controller, twin.model_state[KEY], agent_ids, minute)
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert world.metric_state == twin.metric_state
    assert _pending(world) == _pending(twin)
    assert _records(world) == _records(twin)
    assert list(world.model_state[KEY]["journeys"]) == list(twin.model_state[KEY]["journeys"])
    mirror = world._core.mirror(KEY)
    assert mirror.tainted is None
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(world.model_state[KEY])


def _world(scenario: Any, *, slow: bool = True) -> WorldState:
    world = WorldState.from_scenario(scenario)
    if slow:
        speeds = world.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]
        for mobility in speeds:
            speeds[mobility] = SLOW
    world.minute_index = 1
    return world


def _with_alternate_route(world: WorldState) -> None:
    """A second sleeping-to-toilet edge that closing the first leg does not block."""
    geometry = world.model_configs["facility_geometry"]
    legs = {route["edge_id"]: route["points"] for route in geometry["routes"]}
    world.zone_edges[ALTERNATE] = ZoneEdgeState(
        id=ALTERNATE, from_zone_id="sleeping", to_zone_id="toilet", travel_minutes=20
    )
    geometry["routes"].append(
        {
            "edge_id": ALTERNATE,
            "points": [*legs[FIRST_LEG], *legs["route_corridor_toilet"][1:]],
            "width_m": 1.8,
            "accessible": True,
        }
    )
    for opening in world.model_configs["spatial_constraints"]["openings"]:
        if {FIRST_LEG, "route_corridor_toilet"} & set(opening["edge_ids"]):
            opening["edge_ids"].append(ALTERNATE)


def _walk_to_the_toilet(world: WorldState, agent_id: str = "A001") -> dict[str, Any]:
    controller = _controller(world)
    route = controller._route(world.agents[agent_id].zone_id, "toilet", agent_id)
    assert route is not None
    assert controller._schedule_travel(
        agent_id=agent_id,
        route=route,
        purpose="toilet_request",
        facility_id="toilet_01",
        minute=world.minute_index,
        validate_state=False,
    )
    return world.model_state[KEY]["journeys"][agent_id]


def test_a_walk_takes_the_other_route(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _with_alternate_route(world)
    old = dict(_walk_to_the_toilet(world))
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A001",), 1)
    journey = world.model_state[KEY]["journeys"]["A001"]
    assert journey["route_edge_ids"] == [ALTERNATE]
    assert journey["event_id"] != old["event_id"]
    assert world.metric_state["movement_replan_count"] == 1.0
    assert next(
        record for record in world.event_log.records if record.event.event_id == old["event_id"]
    ).status is EventStatus.SUPPRESSED


def test_a_walk_with_no_route_stops_and_is_counted(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _walk_to_the_toilet(world)
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A001",), 1)
    assert "A001" not in world.model_state[KEY]["journeys"]
    assert world.metric_state["movement_replan_blocked_count"] == 1.0
    assert world.agents["A001"].activity is Activity.IDLE


def test_people_replan_in_the_order_given_and_take_their_event_numbers(
    generated_scenario: Any,
) -> None:
    world = _world(generated_scenario)
    _with_alternate_route(world)
    for agent_id in ("A001", "A002", "A003"):
        world.agents[agent_id].zone_id = "sleeping"
        _walk_to_the_toilet(world, agent_id)
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A003", "A001", "A002"), 1)
    journeys = world.model_state[KEY]["journeys"]
    numbers = [int(journeys[key]["event_id"].rsplit("-", 1)[1]) for key in ("A003", "A001", "A002")]
    assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]


def test_someone_with_no_journey_or_gone_is_passed_over(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _with_alternate_route(world)
    _walk_to_the_toilet(world)
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A002", "nobody", "A001"), 1)
    assert world.metric_state["movement_replan_count"] == 1.0


def test_a_walk_stopped_in_another_zone_is_where_its_position_says(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _with_alternate_route(world)
    _walk_to_the_toilet(world)
    world.model_state[KEY]["positions"]["A001"]["zone_id"] = "corridor"
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A001",), 1)
    assert world.agents["A001"].zone_id == "corridor"


def test_the_trip_past_the_end_of_the_run_is_not_made(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _with_alternate_route(world)
    _walk_to_the_toilet(world)
    world.zone_edges[FIRST_LEG].closed = True
    world.duration_minutes = 4
    _same_replan(world, ("A001",), 1)
    assert "A001" not in world.model_state[KEY]["journeys"]


def _in_the_toilet_area(world: WorldState, agent_id: str = "A001") -> dict[str, Any]:
    """Walking within the room of the toilet, as a trip to the facility is."""
    world.agents[agent_id].zone_id = "toilet"
    routes = world.model_configs["facility_geometry"]["routes"]
    entrance = next(route for route in routes if route["edge_id"] == "route_corridor_toilet")
    position = world.model_state[KEY]["positions"][agent_id]
    position.update(zone_id="toilet", point=dict(entrance["points"][-1]), position_kind="physical")
    controller = _controller(world)
    room = controller._facility_approach(agent_id, "toilet_01")
    assert room is not None
    assert controller._schedule_travel(
        agent_id=agent_id,
        route=room[0],
        route_points=room[1],
        purpose="toilet_request",
        facility_id="toilet_01",
        minute=world.minute_index,
        validate_state=False,
    )
    return world.model_state[KEY]["journeys"][agent_id]


def test_a_room_walk_has_a_new_path(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    old = dict(_in_the_toilet_area(world))
    world.model_state[KEY]["positions"]["A001"]["point"] = dict(old["route_points"][0])
    world.model_state[KEY]["positions"]["A001"]["point"]["x"] += 0.1
    _same_replan(world, ("A001",), 1)
    assert world.model_state[KEY]["journeys"]["A001"]["route_points"][0] != old["route_points"][0]


def test_a_room_walk_that_is_already_there_is_placed(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    old = _in_the_toilet_area(world)
    world.model_state[KEY]["positions"]["A001"]["point"] = dict(old["route_points"][-1])
    _same_replan(world, ("A001",), 1)
    assert "A001" not in world.model_state[KEY]["journeys"]
    assert world.model_state[KEY]["positions"]["A001"]["point"] == old["route_points"][-1]


def test_a_room_walk_with_no_path_is_counted(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _in_the_toilet_area(world)
    point = world.model_configs["facility_geometry"]["facility_points"]["toilet_01"]
    world.model_configs["spatial_constraints"]["obstacles"].append(
        {
            "id": "around-the-toilet",
            "polygon": [
                {"x": point["x"] - 0.5, "y": point["y"] - 0.5},
                {"x": point["x"] + 0.5, "y": point["y"] - 0.5},
                {"x": point["x"] + 0.5, "y": point["y"] + 0.5},
                {"x": point["x"] - 0.5, "y": point["y"] + 0.5},
            ],
            "source_ids": ["ASM-TEST"],
        }
    )
    _same_replan(world, ("A001",), 1)
    assert world.metric_state["movement_replan_blocked_count"] == 1.0


def test_without_a_floor_plan_a_room_walk_is_counted(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _in_the_toilet_area(world)
    world.model_state[KEY]["journeys"]["A001"]["route_points"] = []
    del world.model_configs["facility_geometry"]
    world.model_configs.pop("spatial_constraints", None)
    world.model_configs.pop("geometry_overlay", None)
    world.model_state.pop(GEOMETRY_OVERLAY_KEY, None)
    _same_replan(world, ("A001",), 1)
    assert world.metric_state["movement_replan_blocked_count"] == 1.0


# ---- sleep_bed ----------------------------------------------------------------------------------


def _room_with_a_bed(world: WorldState) -> dict[str, float]:
    """A temporary room in the sleeping area, by the effect a Run applies."""
    geometry = world.model_configs["facility_geometry"]
    sleeping = next(zone["polygon"] for zone in geometry["zones"] if zone["zone_id"] == "sleeping")
    left = min(point["x"] for point in sleeping)
    bottom = max(point["y"] for point in sleeping)
    bed = {"x": left + 1.5, "y": bottom - 2.4}
    action = TimedAction(
        effect_id="room",
        op="temporary_room_geometry",
        target_selector=TargetSelector(kind="zone", target_id="sleeping"),
        start_minute=1,
        end_minute=300,
        parameters={
            "schema_version": 1,
            "room_id": "room",
            "zone_id": "sleeping",
            "polygon_m": [
                {"x": left + 0.5, "y": bottom - 4.4},
                {"x": left + 2.5, "y": bottom - 4.4},
                {"x": left + 2.5, "y": bottom - 0.4},
                {"x": left + 0.5, "y": bottom - 0.4},
            ],
            "people_capacity": 2,
            "sleeping_capacity": 1,
            "bed_points_m": [bed],
            "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
            "source_ids": ["ASM-TEMP-ROOM-REPLAN-TEST-V1"],
        },
        units="metre_2d",
        source_id="ASM-TEMP-ROOM-REPLAN-TEST-V1",
        assumption_ids=[],
        policy_dependencies=["geometry_overlay_v1"],
        reversible=True,
    )
    program = EffectProgram(
        program_id="program-room",
        narrative="A room for the test",
        source_text="test source text",
        simulation_start_minute=1,
        effects=[action],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )
    schedule_effect_program(world, program)
    dispatcher = EventDispatcher({"effect_program_geometry": EffectProgramGeometryHandler()})
    assert dispatcher.dispatch_due(world, 1)[0].status is EventStatus.APPLIED
    return bed


def _walk_to_a_bed(world: WorldState) -> dict[str, Any]:
    """A001 on the walk that `sleep` starts."""
    controller = _controller(world)
    reservation = reserve_sleep_bed(world, "A001")
    assert reservation is not None and reservation["travel_minutes"] > 0
    from sheltersim.policies.space import ZoneRoute

    zone = world.agents["A001"].zone_id
    assert controller._schedule_travel(
        agent_id="A001",
        route=ZoneRoute(zone, zone, (zone, zone), (), reservation["travel_minutes"]),
        route_points=reservation["route_points"],
        purpose="sleep_bed",
        facility_id=None,
        minute=world.minute_index,
        validate_state=False,
    )
    return world.model_state[KEY]["journeys"]["A001"]


def test_a_walk_to_a_bed_has_a_new_path(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _room_with_a_bed(world)
    old = dict(_walk_to_a_bed(world))
    world.model_state[KEY]["positions"]["A001"]["point"]["x"] += 0.2
    _same_replan(world, ("A001",), 1)
    journey = world.model_state[KEY]["journeys"]["A001"]
    assert journey["purpose"] == "sleep_bed"
    assert journey["event_id"] != old["event_id"]


def test_a_walk_to_a_bed_that_arrived_sleeps_there(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    bed = _room_with_a_bed(world)
    _walk_to_a_bed(world)
    world.model_state[KEY]["positions"]["A001"]["point"] = dict(bed)
    _same_replan(world, ("A001",), 1)
    assert world.agents["A001"].activity is Activity.SLEEPING
    assert world.model_state[KEY]["positions"]["A001"]["progress"] == 1.0


def test_a_walk_to_a_bed_past_the_end_of_the_run_gives_it_up(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _room_with_a_bed(world)
    _walk_to_a_bed(world)
    world.duration_minutes = 3
    _same_replan(world, ("A001",), 1)
    assert world.agents["A001"].activity is Activity.SLEEPING
    assert "A001" not in world.model_state[GEOMETRY_OVERLAY_KEY]["bed_reservations"]
    assert world.metric_state["movement_replan_blocked_count"] == 1.0


def test_a_walk_to_a_room_that_went_gives_it_up(generated_scenario: Any) -> None:
    world = _world(generated_scenario)
    _room_with_a_bed(world)
    _walk_to_a_bed(world)
    overlay = world.model_state[GEOMETRY_OVERLAY_KEY]
    overlay["bed_reservations"].clear()
    overlay["temporary_rooms"].clear()
    _same_replan(world, ("A001",), 1)
    assert world.metric_state["movement_replan_blocked_count"] == 1.0
    assert world.agents["A001"].activity is Activity.SLEEPING


# ---- staff_support_travel -----------------------------------------------------------------------


def _assigned_support(scenario: Any) -> tuple[WorldState, str]:
    world = _world(scenario, slow=False)
    world.minute_index = 0
    controller = _controller(world)
    receipt = controller.staff_service.request("A001", validate_state=False)
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=current_household_id(world, "A001"),
        support_kind="emotional_support",
        status="requested",
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    assert controller.staff_service.assign_pending(0, validate_state=False) == (receipt.request_id,)
    request = world.support_requests[receipt.request_id]
    append_applied_care_transition(
        world,
        request_id=request.id,
        agent_id="A001",
        household_id=current_household_id(world, "A001"),
        support_kind="emotional_support",
        status="assigned",
        staff_id=request.assigned_staff_id,
        causation_id=request.task_id,
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    assert world.model_state[KEY]["journeys"]["A001"]["purpose"] == "staff_support_travel"
    world.minute_index = 1
    return world, receipt.request_id


def _alternate_to_the_staff(world: WorldState) -> None:
    geometry = world.model_configs["facility_geometry"]
    legs = {route["edge_id"]: route["points"] for route in geometry["routes"]}
    world.zone_edges["route_sleeping_staff_alt"] = ZoneEdgeState(
        id="route_sleeping_staff_alt", from_zone_id="sleeping", to_zone_id="staff", travel_minutes=5
    )
    geometry["routes"].append(
        {
            "edge_id": "route_sleeping_staff_alt",
            "points": [*legs[FIRST_LEG], *legs["route_corridor_staff"][1:]],
            "width_m": 1.8,
            "accessible": True,
        }
    )
    for opening in world.model_configs["spatial_constraints"]["openings"]:
        if {FIRST_LEG, "route_corridor_staff"} & set(opening["edge_ids"]):
            opening["edge_ids"].append("route_sleeping_staff_alt")


def test_support_with_another_route_is_rescheduled(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)
    _alternate_to_the_staff(world)
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A001",), 1)
    journey = world.model_state[KEY]["journeys"]["A001"]
    assert journey["route_edge_ids"] == ["route_sleeping_staff_alt"]
    assert world.support_requests[request_id].status is SupportRequestStatus.ASSIGNED
    assert world.metric_state["movement_replan_count"] == 1.0
    assert any(
        event.provenance.get("rescheduled_after_route_change")
        for event in world.event_queue.pending()
    )


def test_support_where_the_staff_already_are_starts_there(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)
    world.model_state[KEY]["positions"]["A001"]["zone_id"] = "staff"
    _same_replan(world, ("A001",), 1)
    assert world.agents["A001"].activity is Activity.SEEKING_HELP
    assert world.support_requests[request_id].status is SupportRequestStatus.ASSIGNED


def test_support_with_no_route_goes_back_to_the_queue(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)
    world.zone_edges[FIRST_LEG].closed = True
    _same_replan(world, ("A001",), 1)
    assert world.support_requests[request_id].status is SupportRequestStatus.PENDING
    assert world.metric_state["movement_replan_blocked_count"] == 1.0
    assert world.agents["A001"].support_request_id == request_id


def test_support_past_its_timeout_is_left_unresolved(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)
    world.zone_edges[FIRST_LEG].closed = True
    world.support_requests[request_id].timeout_minute = 1
    _same_replan(world, ("A001",), 1)
    assert world.support_requests[request_id].status is SupportRequestStatus.UNRESOLVED
    assert world.agents["A001"].activity is Activity.IDLE
    assert world.metric_state["support_unresolved_count"] == 1.0


def test_support_past_the_end_of_the_run_goes_back_to_the_queue(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)
    _alternate_to_the_staff(world)
    world.zone_edges[FIRST_LEG].closed = True
    world.duration_minutes = 3
    _same_replan(world, ("A001",), 1)
    assert world.support_requests[request_id].status is SupportRequestStatus.PENDING


def _python_says_inconsistent(world: WorldState, spoil: Any, match: str) -> None:
    """The Python step's error, from the kernel's fallback on the same world, on both."""
    twin = copy.deepcopy(world)
    twin_controller, controller = _controller(twin), _controller(world)
    spoil(twin)
    spoil(world)
    with pytest.raises(ValueError, match=match):
        UpdatePhase._replan_in_python(twin_controller, twin.model_state[KEY], ("A001",), 1)
    with pytest.raises(ValueError, match=match):
        native.rules("replan").replan_journeys(controller, world.model_state[KEY], ("A001",), 1)
    assert _bytes(world) == _bytes(twin)


def test_support_that_lost_its_task_is_python_s_error(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)

    def spoil(world: WorldState) -> None:
        del world.active_tasks[world.support_requests[request_id].task_id]

    _python_says_inconsistent(world, spoil, "lost its assigned support task")


def test_support_with_a_moved_completion_is_python_s_error(generated_scenario: Any) -> None:
    world, request_id = _assigned_support(generated_scenario)

    def spoil(world: WorldState) -> None:
        world.active_tasks[world.support_requests[request_id].task_id].due_minute += 1

    _python_says_inconsistent(world, spoil, "inconsistent support completion")


# ---- Beside the kernel ---------------------------------------------------------------------------


def test_edge_capacities_are_asked_of_the_python_route(generated_scenario: Any) -> None:
    """`_route` with capacities is in Python until it has a kernel: the rest here is the kernel's."""
    world = _world(generated_scenario)
    _with_alternate_route(world)
    _walk_to_the_toilet(world)
    world.zone_edges[FIRST_LEG].closed = True
    world.zone_edges[ALTERNATE].max_agents_in_transit = 5
    _same_replan(world, ("A001",), 1)
    assert "A001" in world.model_state[KEY]["journeys"]


def test_a_tick_with_a_closed_edge_replans_through_the_driver(generated_scenario: Any) -> None:
    from sheltersim.core.engine import TickEngine

    def run(with_native: bool) -> str:
        world = WorldState.from_scenario(generated_scenario)
        _with_alternate_route(world)
        controller = _controller(world)
        if not with_native:
            controller.run_tick = lambda *_arguments: False  # type: ignore[method-assign]
        engine = TickEngine(world, phase_handler=controller)
        for _ in range(40):
            engine.step()
        routes = [
            journey["route_edge_ids"]
            for journey in world.model_state[KEY]["journeys"].values()
        ]
        world.zone_edges[FIRST_LEG].closed = True
        controller._refresh_space()
        for _ in range(40):
            engine.step()
        assert world.metric_state.get("movement_replan_count", 0.0) >= 0.0
        assert routes is not None
        return _bytes(world)

    assert run(True) == run(False)
