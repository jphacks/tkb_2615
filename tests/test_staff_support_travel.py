from __future__ import annotations

import json
from pathlib import Path

from sheltersim.contracts.operations import StaffSupportContinuityReceipt
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_source_ids_for_agent,
)
from sheltersim.domain.common_operations import _event_payload_matches
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.domain.staff import StaffService, support_wait_reason_codes
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.metrics import MetricSnapshot
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import person_projection
from sheltersim.inspection.traces import RunArtifactWriter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"


def _world(
    *, geometry: bool = False, operational_event: bool = False, alternate_route: bool = False,
    duration_minutes: int = 45,
) -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = (
        [{
            "id": "staff-off-before-arrival",
            "kind": "exogenous",
            "due_minute": 1,
            "priority": 0,
            "sequence": 0,
            "event_type": "staff_unavailable",
            "payload": {"staff_id": "S01", "until_minute": 5},
            "source_id": "ASM-H03-OPERATIONAL-EVENTS-V1",
            "provenance": {
                "source_type": "assumption",
                "source_id": "ASM-H03-OPERATIONAL-EVENTS-V1",
            },
        }]
        if operational_event else []
    )
    if alternate_route:
        document["zone_edges"].append({
            "closed": False,
            "from_zone_id": "sleeping",
            "id": "sleeping_desk_alt",
            "to_zone_id": "information_desk",
            "travel_minutes": 5,
        })
    document["model_configs"]["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    if geometry:
        zone_ids = [row["id"] for row in document["zones"]]
        polygon = [
            {"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0},
            {"x": 10.0, "y": 10.0}, {"x": 0.0, "y": 10.0},
        ]
        document["model_configs"]["facility_geometry"] = {
            "schema_version": 1,
            "units": "m",
            "origin": "top_left",
            "width_m": 10.0,
            "height_m": 10.0,
            "source_kind": "synthetic",
            "zones": [
                {"zone_id": zone_id, "polygon": polygon, "usable_area_m2": 100.0}
                for zone_id in zone_ids
            ],
            "facility_points": {
                facility["id"]: {"x": 7.0, "y": 5.0}
                for facility in document["facilities"]
            },
            "service_points": [],
            "routes": [
                {
                    "edge_id": edge["id"],
                    "points": [{"x": 3.0, "y": 5.0}, {"x": 6.0, "y": 5.0}],
                    "width_m": 2.0,
                    "accessible": True,
                }
                for edge in document["zone_edges"]
            ],
        }
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def _controller(world: WorldState, tmp_path: Path) -> OfflineSimulationController:
    scenario = ScenarioSpec.load(FIXTURE)
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


def test_resident_travels_to_staff_before_support_starts_and_checkpoint_round_trips(
    tmp_path: Path,
) -> None:
    world = _world(geometry=True)
    controller = _controller(world, tmp_path)
    request_id = _request_and_assign(controller)

    resident_trip = next(
        task for task in world.active_tasks.values() if task.kind == "staff_support_travel"
    )
    support_task = world.active_tasks[world.support_requests[request_id].task_id]
    assert world.agents["A001"].activity.value == "moving"
    assert support_task.start_minute == resident_trip.due_minute
    assert support_task.due_minute == resident_trip.due_minute + 10
    assert world.model_state["movement_v1"]["journeys"]["A001"]["support_request_id"] == request_id
    snapshot = MetricSnapshot.from_world(world)
    assert snapshot.unresolved_support_request_count == 1
    metrics_sources = json.loads(snapshot.source_ids_json)
    assert metrics_sources["unresolved_support_agents"] == ["A001"]
    assert metrics_sources["support_wait_by_agent"]["A001"] == {
        "request_id": request_id,
        "waited_minutes": 0,
        "reason_codes": ["resident_travel"],
    }
    care_request = person_projection(world, "A001")["person"]["care_requests"][0]
    assert care_request["waited_minutes"] == 0
    assert care_request["wait_reason_codes"] == ["resident_travel"]
    world.validate()

    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    world.minute_index = resident_trip.due_minute
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, world.minute_index, CounterRNG(world.seed))
    controller(TickPhase.UPDATE_AGENT_STATE, world, world.minute_index, CounterRNG(world.seed))
    assert world.agents["A001"].zone_id == world.staff["S01"].zone_id
    assert world.agents["A001"].activity.value == "seeking_help"
    assert world.support_requests[request_id].status is SupportRequestStatus.ASSIGNED
    assert not any(task.kind == "staff_support_travel" for task in world.active_tasks.values())
    world.validate()


def test_closed_route_requeues_assigned_support_with_applied_history(tmp_path: Path) -> None:
    world = _world(geometry=True)
    controller = _controller(world, tmp_path)
    request_id = _request_and_assign(controller)
    world.zone_edges["sleeping_corridor"].closed = True
    controller._refresh_space()
    controller._update_movement(world.minute_index)

    request = world.support_requests[request_id]
    assert request.status is SupportRequestStatus.PENDING
    assert request.assigned_staff_id is None
    assert world.agents["A001"].activity.value == "seeking_help"
    continuity = next(
        row for row in world.model_state["shared_operations_v1"]["history"]
        if row["event_type"] == "staff_support_continuity"
    )
    event = world.event_log.find(continuity["event_id"])
    assert event is not None
    assert event.event.payload["cause_type"] == "route_unreachable"
    assert event.event.payload["status"] == "requeued"
    assert event.event.causation_id is not None
    assert world.staff["S01"].active_task_id is None
    world.validate()


def test_closed_route_reschedules_resident_trip_and_support_completion(tmp_path: Path) -> None:
    world = _world(geometry=True, alternate_route=True)
    controller = _controller(world, tmp_path)
    request_id = _request_and_assign(controller)
    original_travel = next(
        event for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete"
    )
    world.zone_edges["sleeping_corridor"].closed = True
    controller._refresh_space()

    controller._update_movement(world.minute_index)

    journey = world.model_state["movement_v1"]["journeys"]["A001"]
    assert journey["route_edge_ids"] == ["sleeping_desk_alt"]
    assert journey["event_id"] != original_travel.event_id
    assert world.agents["A001"].activity.value == "moving"
    task = world.active_tasks[world.support_requests[request_id].task_id]
    assert task.start_minute == journey["due_minute"]
    assert task.due_minute == journey["due_minute"] + 10
    assert any(
        record.event.event_id == original_travel.event_id
        and record.status.value == "suppressed"
        for record in world.event_log.records
    )
    world.validate()
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()


def test_staff_unavailability_cancels_resident_trip_and_requeues_request(
    tmp_path: Path,
) -> None:
    world = _world(geometry=True, operational_event=True)
    controller = _controller(world, tmp_path)
    request_id = _request_and_assign(controller)
    travel = next(
        event for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete"
    )
    world.minute_index = 1

    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))

    request = world.support_requests[request_id]
    assert request.status is SupportRequestStatus.PENDING
    assert request.assigned_staff_id is None
    assert not any(
        event.event_id == travel.event_id for event in world.event_queue.pending()
    )
    assert any(
        record.event.event_id == travel.event_id
        and record.status.value == "suppressed"
        for record in world.event_log.records
    )
    assert world.agents["A001"].activity.value == "seeking_help"
    world.validate()


def test_support_wait_reasons_distinguish_unreachable_route_and_run_end() -> None:
    unreachable = _world()
    unreachable.zone_edges["sleeping_corridor"].closed = True
    request_id = StaffService(unreachable).request(
        "A001", validate_state=False
    ).request_id
    assert support_wait_reason_codes(unreachable, request_id) == (
        "no_route_to_qualified_staff",
    )

    too_short = _world(duration_minutes=10)
    request_id = StaffService(too_short).request(
        "A001", validate_state=False
    ).request_id
    assert support_wait_reason_codes(too_short, request_id) == (
        "insufficient_run_time",
    )


def test_support_wait_reasons_report_route_capacity_and_full_destination() -> None:
    world = _world()
    world.model_configs["layout_routing"] = {
        "max_agents_in_transit_by_edge": {"sleeping_corridor": 1}
    }
    world.schedule_event(ScheduledEvent(
        event_id="other-traveler-on-support-route",
        kind=EventKind.COMPLETION,
        due_minute=20,
        priority=0,
        sequence=100,
        event_type="agent_travel_complete",
        payload={"route_edge_ids": ["sleeping_corridor"]},
        source_id="test",
    ))
    request_id = StaffService(world).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(world, request_id) == ("route_capacity_full",)

    full = _world()
    full.zones["information_desk"].capacity = 1
    full.agents["A002"].activity = Activity.MOVING
    full.agents["A002"].target_id = "information_desk"
    request_id = StaffService(full).request("A001", validate_state=False).request_id
    assert support_wait_reason_codes(full, request_id) == ("staff_destination_full",)


def test_support_wait_reasons_distinguish_unattempted_and_rejected_assignment(
    tmp_path: Path,
) -> None:
    unattempted = _world()
    request_id = StaffService(unattempted).request(
        "A001", validate_state=False
    ).request_id
    assert support_wait_reason_codes(unattempted, request_id) == (
        "assignment_not_attempted_at_sampled_minute",
    )

    world = _world(geometry=True)
    controller = _controller(world, tmp_path)
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
    assert person_projection(world, "A001")["person"]["care_requests"][0][
        "wait_reason_codes"
    ] == ["resident_travel_schedule_rejected"]
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert support_wait_reason_codes(restored, request.request_id) == (
        "resident_travel_schedule_rejected",
    )
    assert restored.canonical_json() == world.canonical_json()


def test_pre_cause_type_continuity_receipt_remains_valid() -> None:
    payload = {
        "record_type": "care",
        "schema_version": 1,
        "event_type": "staff_support_continuity",
        "event_id": "support-continuity-legacy",
        "request_id": "request-legacy",
        "agent_id": "A001",
        "task_id": "task-legacy",
        "previous_staff_id": "S01",
        "status": "requeued",
        "minute": 3,
        "causation_id": "staff-off-legacy",
        "source_ids": ["ASM-STAFF-SUPPORT-LIFECYCLE-V1"],
    }
    receipt = StaffSupportContinuityReceipt.model_validate(payload)
    event = ScheduledEvent(
        event_id=receipt.event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=receipt.minute,
        priority=0,
        sequence=8,
        event_type=receipt.event_type,
        payload=payload,
        source_id="ASM-STAFF-SUPPORT-LIFECYCLE-V1",
        causation_id=receipt.causation_id,
    )
    record = EventLogRecord(
        event=event,
        realized_minute=receipt.minute,
        status=EventStatus.APPLIED,
        detail="legacy continuity record",
    )

    assert receipt.cause_type == "staff_unavailable"
    assert _event_payload_matches(record, receipt)
