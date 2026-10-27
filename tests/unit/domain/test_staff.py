"""Support requests: residents walk to the worker, and waits record their reasons.

Most tests here drive the controller by hand. The resident's walk run through
the tick order is checked in tests/integration/test_staff_support_travel.py, and
a staff absence in tests/integration/test_intervention_outcomes.py
(test_a_staff_absence_stops_support_and_lets_requests_time_out).
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_source_ids_for_agent,
)
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.domain.staff import StaffService, support_wait_reason_codes
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import person_projection
from sheltersim.inspection.traces import RunArtifactWriter

# In a generated Scenario everyone starts in the sleeping area, and the only
# support worker, staff_001, works in the staff area behind the corridor.
WORKER = "staff_001"
WORKER_ZONE = "staff"
FIRST_LEG = "route_sleeping_corridor"


def test_waiting_for_an_absent_worker_counts_as_waiting_for_someone_not_working(
    generated_scenario_with,
) -> None:
    # Only Scenarios of four days or more hold staff absences (#65).
    world = WorldState.from_scenario(generated_scenario_with(duration_days=4))
    # staff_001 is the only support worker. Marking them away stands in for an
    # applied absence event, so the state checks that compare it with the event
    # history are skipped; test_a_staff_absence_shows_in_the_support_wait_reasons
    # runs the real intervention.
    world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["staff_001"]["available"] = False
    service = StaffService(world, task_duration_minutes=10, unresolved_after_minutes=30)

    request = service.request("A001", validate_state=False)

    assert service.assign_pending(0, validate_state=False) == ()
    assert support_wait_reason_codes(world, request.request_id) == ("staff_unavailable",)
    # Before #74 this wait was counted under no reason at all.
    assert world.metric_state["support_wait_staff_off_shift_request_minutes"] == 1.0
    assert "support_wait_staff_busy_request_minutes" not in world.metric_state


def _with_alternate_route(scenario: ScenarioSpec) -> ScenarioSpec:
    """Add a second, slower sleeping-to-staff route; generated plans only have one.

    It walks the same doors and corridor as the usual two legs, so it is physically
    clear, but it is a separate graph edge that a closed leg does not block.
    """
    document = scenario.model_dump(mode="json")
    geometry = document["model_configs"]["facility_geometry"]
    legs = {route["edge_id"]: route["points"] for route in geometry["routes"]}
    points = [*legs["route_sleeping_corridor"], *legs["route_corridor_staff"][1:]]
    document["zone_edges"].append(
        {
            "closed": False,
            "from_zone_id": "sleeping",
            "id": "route_sleeping_staff_alt",
            "to_zone_id": WORKER_ZONE,
            "travel_minutes": 5,
        }
    )
    geometry["routes"].append(
        {
            "edge_id": "route_sleeping_staff_alt",
            "points": points,
            "width_m": 1.8,
            "accessible": True,
        }
    )
    # Its doors are the ones the two legs pass through.
    for opening in document["model_configs"]["spatial_constraints"]["openings"]:
        if {"route_sleeping_corridor", "route_corridor_staff"} & set(opening["edge_ids"]):
            opening["edge_ids"].append("route_sleeping_staff_alt")
    return ScenarioSpec.model_validate(document)


def _controller(scenario: ScenarioSpec, tmp_path: Path) -> OfflineSimulationController:
    world = WorldState.from_scenario(scenario)
    artifacts = RunArtifactWriter(scenario, tmp_path / "run", trace_level=TraceLevel.OFF)
    return OfflineSimulationController(world, artifacts)


def _request_and_assign(controller: OfflineSimulationController) -> str:
    world = controller.world
    receipt = controller.staff_service.request("A001")
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
        agent_id=request.agent_id,
        household_id=current_household_id(world, "A001"),
        support_kind="emotional_support",
        status="assigned",
        staff_id=request.assigned_staff_id,
        causation_id=request.task_id,
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    world.validate()
    return receipt.request_id


def test_closed_route_requeues_assigned_support_with_applied_history(
    generated_scenario, tmp_path: Path
) -> None:
    controller = _controller(generated_scenario, tmp_path)
    world = controller.world
    request_id = _request_and_assign(controller)
    world.zone_edges[FIRST_LEG].closed = True
    controller._refresh_space()
    controller._update_movement(world.minute_index)

    request = world.support_requests[request_id]
    assert request.status is SupportRequestStatus.PENDING
    assert request.assigned_staff_id is None
    assert world.agents["A001"].activity.value == "seeking_help"
    continuity = next(
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["event_type"] == "staff_support_continuity"
    )
    event = world.event_log.find(continuity["event_id"])
    assert event is not None
    assert event.event.payload["cause_type"] == "route_unreachable"
    assert event.event.payload["status"] == "requeued"
    assert event.event.causation_id is not None
    assert world.staff[WORKER].active_task_id is None
    world.validate()


def test_closed_route_reschedules_resident_trip_and_support_completion(
    generated_scenario, tmp_path: Path
) -> None:
    controller = _controller(_with_alternate_route(generated_scenario), tmp_path)
    world = controller.world
    task_minutes = world.model_configs["staff"]["support_task_minutes"]
    request_id = _request_and_assign(controller)
    original_travel = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete"
    )
    world.zone_edges[FIRST_LEG].closed = True
    controller._refresh_space()

    controller._update_movement(world.minute_index)

    journey = world.model_state["movement_v1"]["journeys"]["A001"]
    assert journey["route_edge_ids"] == ["route_sleeping_staff_alt"]
    assert journey["event_id"] != original_travel.event_id
    assert world.agents["A001"].activity.value == "moving"
    task = world.active_tasks[world.support_requests[request_id].task_id]
    assert task.start_minute == journey["due_minute"]
    assert task.due_minute == journey["due_minute"] + task_minutes
    assert any(
        record.event.event_id == original_travel.event_id and record.status.value == "suppressed"
        for record in world.event_log.records
    )
    world.validate()
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()


def test_staff_unavailability_cancels_resident_trip_and_requeues_request(
    generated_scenario_with, tmp_path: Path
) -> None:
    scenario = generated_scenario_with(
        operational_conditions={
            "scheduled": [
                {
                    "kind": "staff_unavailable",
                    "staff_id": WORKER,
                    "start_minute": 1,
                    "end_minute": 5,
                }
            ]
        }
    )
    controller = _controller(scenario, tmp_path)
    world = controller.world
    request_id = _request_and_assign(controller)
    travel = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete"
    )
    world.minute_index = 1

    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))

    request = world.support_requests[request_id]
    assert request.status is SupportRequestStatus.PENDING
    assert request.assigned_staff_id is None
    assert not any(event.event_id == travel.event_id for event in world.event_queue.pending())
    assert any(
        record.event.event_id == travel.event_id and record.status.value == "suppressed"
        for record in world.event_log.records
    )
    assert world.agents["A001"].activity.value == "seeking_help"
    world.validate()


def test_support_wait_reasons_distinguish_unreachable_route_and_run_end(
    generated_scenario,
) -> None:
    unreachable = WorldState.from_scenario(generated_scenario)
    unreachable.zone_edges[FIRST_LEG].closed = True
    request_id = StaffService(unreachable).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(unreachable, request_id) == ("no_route_to_qualified_staff",)

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 10
    too_short = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    request_id = StaffService(too_short).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(too_short, request_id) == ("insufficient_run_time",)


def test_support_wait_reasons_report_route_capacity_and_full_destination(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_configs["layout_routing"] = {"max_agents_in_transit_by_edge": {FIRST_LEG: 1}}
    world.schedule_event(
        ScheduledEvent(
            event_id="other-traveler-on-support-route",
            kind=EventKind.COMPLETION,
            due_minute=20,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="agent_travel_complete",
            payload={"route_edge_ids": [FIRST_LEG]},
            source_id="test",
        )
    )
    request_id = StaffService(world).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(world, request_id) == ("route_capacity_full",)

    full = WorldState.from_scenario(generated_scenario)
    full.zones[WORKER_ZONE].capacity = 1
    full.agents["A002"].activity = Activity.MOVING
    full.agents["A002"].target_id = WORKER_ZONE
    request_id = StaffService(full).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(full, request_id) == ("staff_destination_full",)


def test_support_wait_reasons_distinguish_unattempted_and_rejected_assignment(
    generated_scenario, tmp_path: Path
) -> None:
    unattempted = WorldState.from_scenario(generated_scenario)
    request_id = StaffService(unattempted).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(unattempted, request_id) == (
        "assignment_not_attempted_at_sampled_minute",
    )

    controller = _controller(generated_scenario, tmp_path)
    world = controller.world
    request = controller.staff_service.request("A001", validate_state=False)
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=current_household_id(world, "A001"),
        support_kind="emotional_support",
        status="requested",
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    controller.staff_service.travel_scheduler = lambda *_args: False
    assert controller.staff_service.assign_pending(0, validate_state=False) == ()
    assert support_wait_reason_codes(world, request.request_id) == (
        "resident_travel_schedule_rejected",
    )
    snapshot = MetricSnapshot.from_world(world)
    assert json.loads(snapshot.source_ids_json)["support_wait_by_agent"]["A001"][
        "reason_codes"
    ] == ["resident_travel_schedule_rejected"]
    assert person_projection(world, "A001")["person"]["care_requests"][0]["wait_reason_codes"] == [
        "resident_travel_schedule_rejected"
    ]
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert support_wait_reason_codes(restored, request.request_id) == (
        "resident_travel_schedule_rejected",
    )
    assert restored.canonical_json() == world.canonical_json()
