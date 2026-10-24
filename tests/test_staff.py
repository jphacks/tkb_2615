"""Staff assignment, support request, cooldown, and persistence invariants."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.models import StaffState, SupportRequestStatus
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import StaffService
from sheltersim.domain.staff_dynamics import canonical_staff_dynamics_config
from sheltersim.events.handlers import (
    EventDispatcher,
    StaffSupportCompletionHandler,
    SupportRequestTimeoutHandler,
)
from sheltersim.experiments.metrics import MetricSnapshot, MetricsRecorder
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def make_world() -> WorldState:
    return WorldState.from_scenario(ScenarioSpec.load(FIXTURE))


def test_staff_does_not_double_book_and_worldstate_v5_round_trips_assigned_work() -> None:
    world = make_world()
    service = StaffService(
        world,
        task_duration_minutes=4,
        unresolved_after_minutes=20,
        cooldown_minutes=6,
    )

    first = service.request("A001")
    request_event = next(
        item for item in world.event_queue.pending() if item.event_type == "support_request_timeout"
    )
    assert request_event.provenance["source_type"] == "assumption"
    assert request_event.provenance["source_id"] == "ASM-STAFF-SUPPORT-LIFECYCLE-V1"
    with pytest.raises(ValueError, match="already has an active support request"):
        service.request("A001")

    assert service.assign_pending(0) == (first.request_id,)
    completion_event = next(
        item for item in world.event_queue.pending() if item.event_type == "staff_support_complete"
    )
    assert completion_event.provenance["source_id"] == "ASM-STAFF-SUPPORT-LIFECYCLE-V1"
    assert service.assign_pending(0) == ()
    world.validate()

    restored = WorldState.from_json(world.canonical_json())
    assert restored.schema_version == 5
    assert restored.canonical_json() == world.canonical_json()
    assert restored.staff["S01"].busy_until == 4
    assert restored.staff["S01"].active_task_id is not None
    assert restored.support_requests[first.request_id].status is SupportRequestStatus.ASSIGNED


def test_completed_support_resolves_request_and_enforces_cooldown() -> None:
    world = make_world()
    service = StaffService(
        world,
        task_duration_minutes=4,
        unresolved_after_minutes=20,
        cooldown_minutes=6,
    )
    receipt = service.request("A001")
    service.assign_pending(0)
    world.minute_index = 4
    dispatcher = EventDispatcher(
        {"staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6)}
    )

    outcomes = dispatcher.dispatch_due(world, 4)

    assert len(outcomes) == 1
    request = world.support_requests[receipt.request_id]
    assert request.status is SupportRequestStatus.RESOLVED
    assert request.resolved_minute == 4
    assert world.agents["A001"].support_request_id is None
    assert world.agents["A001"].support_cooldown_until == 10
    assert world.staff["S01"].active_task_id is None
    world.validate()

    with pytest.raises(ValueError, match="cooldown"):
        service.request("A001")
    world.minute_index = 10
    assert service.request("A001").requested_minute == 10


def test_unassigned_request_times_out_and_cannot_be_reopened_during_cooldown() -> None:
    world = make_world()
    world.staff.clear()
    service = StaffService(world, unresolved_after_minutes=3, cooldown_minutes=5)
    receipt = service.request("A001")
    world.minute_index = 3
    dispatcher = EventDispatcher(
        {"support_request_timeout": SupportRequestTimeoutHandler(cooldown_minutes=5)}
    )

    dispatcher.dispatch_due(world, 3)

    assert world.support_requests[receipt.request_id].status is SupportRequestStatus.UNRESOLVED
    assert world.agents["A001"].support_request_id is None
    assert world.agents["A001"].support_cooldown_until == 8
    world.validate()
    with pytest.raises(ValueError, match="cooldown"):
        service.request("A001")


def test_request_at_end_of_scenario_is_recorded_unresolved_without_a_stranded_timeout() -> None:
    document = ScenarioSpec.load(FIXTURE).model_dump(mode="json")
    document["duration_minutes"] = 1
    document["scheduled_events"] = []
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    world.staff.clear()
    service = StaffService(world, unresolved_after_minutes=3, cooldown_minutes=5)

    receipt = service.request("A001")

    request = world.support_requests[receipt.request_id]
    assert request.status is SupportRequestStatus.UNRESOLVED
    assert request.resolved_minute == 0
    assert world.agents["A001"].support_request_id is None
    assert world.event_queue.pending() == ()
    world.validate()


def test_added_staff_increases_support_assignments_without_changing_toilet_capacity() -> None:
    world = make_world()
    world.staff = {"S01": world.staff["S01"]}
    original_capacity = {key: item.working_units for key, item in world.facilities.items()}
    service = StaffService(world, task_duration_minutes=8, unresolved_after_minutes=30)
    service.request("A001")
    second = service.request("A002")

    assert len(service.assign_pending(0)) == 1
    assert sum(person.active_task_id is not None for person in world.staff.values()) == 1
    world.staff["S02"] = StaffState(id="S02", zone_id="information_desk", skills=("support",))

    assert service.assign_pending(0) == (second.request_id,)
    assert sum(person.active_task_id is not None for person in world.staff.values()) == 2
    assert {key: item.working_units for key, item in world.facilities.items()} == original_capacity
    world.validate()


def test_fairness_aging_promotes_old_request_ahead_of_new_urgent_request() -> None:
    world = make_world()
    world.staff.clear()
    service = StaffService(
        world,
        task_duration_minutes=4,
        unresolved_after_minutes=90,
        fairness_after_minutes=30,
    )
    older = service.request("A001")
    world.minute_index = 20
    urgent = service.request("A002")
    world.model_state.setdefault("llm", {}).setdefault("support_priorities", {})[
        urgent.request_id
    ] = 1
    world.staff["S01"] = StaffState(id="S01", zone_id="information_desk", skills=("support",))
    world.minute_index = 30

    assert service.assign_pending(30) == (older.request_id,)
    completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_support_complete"
        and event.payload["request_id"] == older.request_id
    )
    assert completion.provenance["policy_version"] == "staff-support-fifo-aging-v2"
    assert completion.provenance["assignment_wait_minutes"] == 30
    assert completion.provenance["fairness_promoted"] is True
    assert world.metric_state["support_fairness_promoted_assignment_count"] == 1.0
    world.validate()
    assert WorldState.from_json(world.canonical_json()).canonical_json() == world.canonical_json()


def test_fairness_delay_must_precede_support_request_timeout() -> None:
    document = ScenarioSpec.load(FIXTURE).model_dump(mode="json")
    staff_config = document["model_configs"].setdefault("staff", {})
    staff_config["unresolved_after_minutes"] = 20
    staff_config["support_fairness_minutes"] = 21

    with pytest.raises(ValueError, match="support_fairness_minutes"):
        ScenarioSpec.model_validate(document)


def test_no_qualified_worker_is_counted_as_request_minutes_and_exported() -> None:
    world = make_world()
    world.staff.clear()
    service = StaffService(world, unresolved_after_minutes=20)
    service.request("A001")

    assert service.assign_pending(0) == ()
    assert service.assign_pending(0) == ()
    snapshot = MetricSnapshot.from_world(world)
    assert snapshot.support_no_qualified_worker_count == 1
    assert snapshot.support_no_qualified_worker_request_minutes == 1.0
    sources = json.loads(snapshot.source_ids_json)
    assert sources["support_requests_without_qualified_staff"] == ["A001"]
    assert sources["support_wait_by_agent"]["A001"]["reason_codes"] == [
        "no_qualified_staff"
    ]

    recorder = MetricsRecorder()
    recorder.capture(world)
    restored = MetricsRecorder.from_dict(recorder.to_dict())
    assert restored.snapshots[0].support_no_qualified_worker_count == 1
    assert restored.snapshots[0].support_no_qualified_worker_request_minutes == 1.0

    legacy_row = recorder.to_dict()["snapshots"][0]
    for key in (
        "support_no_qualified_worker_count",
        "support_no_qualified_worker_request_minutes",
        "support_fairness_promoted_assignment_count",
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
    ):
        legacy_row.pop(key)
    legacy = MetricsRecorder.from_dict({"schema_version": 2, "snapshots": [legacy_row]})
    assert legacy.snapshots[0].support_no_qualified_worker_count == 0
    assert legacy.snapshots[0].support_no_qualified_worker_request_minutes == 0.0


def test_busy_and_off_shift_support_delay_exposure_is_checkpointed_and_exported() -> None:
    world = make_world()
    world.staff = {
        staff_id: StaffState(id=staff_id, zone_id="sleeping", skills=("support",))
        for staff_id in ("S01", "S02")
    }
    world.model_configs["staff_dynamics"] = canonical_staff_dynamics_config({
        "shift_windows_by_staff": {
            "S02": [{"start_minute": 100, "end_minute": 200}],
        },
    })
    initialize_simulator_extensions(world)
    service = StaffService(world, task_duration_minutes=10, unresolved_after_minutes=30)

    first = service.request("A001")
    assert service.assign_pending(0) == (first.request_id,)
    world.minute_index = 4
    waiting = service.request("A002", minute=4)
    assert service.assign_pending(4) == ()
    assert service.assign_pending(4) == ()
    assert world.metric_state["support_wait_staff_busy_request_minutes"] == 1.0
    assert world.metric_state["support_wait_staff_off_shift_request_minutes"] == 1.0

    world.minute_index = 5
    assert service.assign_pending(5) == ()
    snapshot = MetricSnapshot.from_world(world)
    details = json.loads(snapshot.source_ids_json)["support_wait_by_agent"]["A002"]
    assert details["request_id"] == waiting.request_id
    assert details["reason_codes"] == ["staff_busy", "staff_off_shift"]
    assert snapshot.support_wait_staff_busy_request_minutes == 2.0
    assert snapshot.support_wait_staff_off_shift_request_minutes == 2.0

    recorder = MetricsRecorder()
    recorder.capture(world)
    serialized = recorder.to_dict()
    assert serialized["schema_version"] == 4
    restored = MetricsRecorder.from_dict(serialized)
    assert restored.snapshots[0].support_wait_staff_busy_request_minutes == 2.0
    assert restored.snapshots[0].support_wait_staff_off_shift_request_minutes == 2.0

    version_three = dict(serialized)
    version_three["schema_version"] = 3
    version_three["snapshots"] = [
        {key: value for key, value in serialized["snapshots"][0].items()
         if key not in {
             "support_wait_staff_busy_request_minutes",
             "support_wait_staff_off_shift_request_minutes",
         }}
    ]
    migrated = MetricsRecorder.from_dict(version_three)
    assert migrated.snapshots[0].support_wait_staff_busy_request_minutes == 0.0
    assert migrated.snapshots[0].support_wait_staff_off_shift_request_minutes == 0.0
