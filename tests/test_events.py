"""Typed event ordering, application, idempotency, and audit separation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.events.handlers import EventDispatcher, FacilityOutageHandler
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.events.queue import DuplicateEventError, EventLog
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def make_event(
    event_id: str,
    *,
    due_minute: int = 0,
    priority: int = 0,
    sequence: int = 0,
    event_type: str = "record",
    payload: dict[str, object] | None = None,
    kind: EventKind = EventKind.EXOGENOUS,
) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=event_id,
        kind=kind,
        due_minute=due_minute,
        priority=priority,
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
    )


class RecordHandler:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, world: WorldState, event: ScheduledEvent) -> None:
        self.applied.append(event.event_id)


def test_due_events_use_minute_priority_then_sequence_and_leave_future_events_pending() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    handler = RecordHandler()
    dispatcher = EventDispatcher({"record": handler})
    events = (
        make_event("later", due_minute=2, priority=-1, sequence=1),
        make_event("seq-2", priority=1, sequence=2),
        make_event("priority-0", priority=0, sequence=5),
        make_event("seq-0", priority=1, sequence=0),
    )
    for event in events:
        world.schedule_event(event)

    assert handler.applied == []
    outcomes = dispatcher.dispatch_due(world, 0)

    expected = ["priority-0", "seq-0", "seq-2"]
    assert handler.applied == expected
    assert [record.event.event_id for record in outcomes] == expected
    assert [event.event_id for event in world.event_queue.pending()] == ["later"]
    assert all(record.realized_minute == 0 for record in world.event_log.records)


def test_dispatch_is_reproducible_when_events_are_scheduled_in_different_input_order() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    event_inputs = (
        make_event("E3", priority=0, sequence=3),
        make_event("E1", priority=0, sequence=1),
        make_event("E2", priority=-1, sequence=2),
    )

    def run(events: tuple[ScheduledEvent, ...]) -> str:
        world = WorldState.from_scenario(scenario)
        for event in events:
            world.schedule_event(event)
        EventDispatcher({"record": RecordHandler()}).dispatch_due(world, 0)
        return world.canonical_json()

    assert run(event_inputs) == run(tuple(reversed(event_inputs)))


def test_duplicate_event_id_or_sequence_is_rejected_and_realized_event_is_idempotent() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    event = make_event("repeat", sequence=0)
    world.schedule_event(event)

    with pytest.raises(DuplicateEventError, match="duplicate event ID"):
        world.schedule_event(make_event("repeat", sequence=1))
    with pytest.raises(DuplicateEventError, match="duplicate event sequence"):
        world.schedule_event(make_event("other", sequence=0))

    dispatcher = EventDispatcher({"record": RecordHandler()})
    assert len(dispatcher.dispatch_due(world, 0)) == 1
    assert dispatcher.dispatch_due(world, 0) == ()
    with pytest.raises(DuplicateEventError, match="already realized"):
        world.schedule_event(event)
    assert len(world.event_log) == 1


def test_event_log_rebuilds_bounded_indexes_from_immutable_records() -> None:
    outage = EventLogRecord(
        event=make_event(
            "outage-history",
            sequence=2,
            event_type="facility_outage",
            payload={"facility_id": "toilet_1", "units_out": 1},
        ),
        realized_minute=3,
        status=EventStatus.APPLIED,
    )
    failed_recovery = EventLogRecord(
        event=make_event(
            "failed-recovery",
            sequence=3,
            event_type="facility_recovery",
            payload={"facility_id": "toilet_1", "units_restored": 1},
        ),
        realized_minute=4,
        status=EventStatus.FAILED,
    )
    care = EventLogRecord(
        event=make_event(
            "care-completed",
            sequence=5,
            event_type="care_support_transition",
            payload={
                "request_id": "support-1",
                "agent_id": "A001",
                "support_kind": "medication_support",
                "status": "completed",
            },
        ),
        realized_minute=5,
        status=EventStatus.APPLIED,
    )

    event_log = EventLog(_records=[outage, failed_recovery, care])

    assert event_log.max_sequence == 5
    assert event_log.applied_facility_event_ids("toilet_1") == frozenset(
        {"outage-history"}
    )
    assert event_log.completed_support_kinds("A001") == frozenset(
        {"medication_support"}
    )
    assert event_log.completed_support_kinds("A002") == frozenset()


def test_facility_outage_updates_working_capacity_once() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.schedule_event(
        make_event(
            "outage-1",
            event_type="facility_outage",
            payload={"facility_id": "toilet_1", "units_out": 1},
        )
    )
    dispatcher = EventDispatcher({"facility_outage": FacilityOutageHandler()})

    dispatcher.dispatch_due(world, 0)

    assert world.facilities["toilet_1"].working_units == 0
    assert world.event_log.records[0].status is EventStatus.APPLIED
    assert dispatcher.dispatch_due(world, 0) == ()
    assert world.facilities["toilet_1"].working_units == 0


@pytest.mark.parametrize(
    ("facility_id", "units_out", "expected_detail"),
    [
        ("missing", 1, "does not exist"),
        ("toilet_1", -1, "cannot be negative"),
        ("toilet_1", 2, "exceeds currently working capacity"),
    ],
)
def test_invalid_outage_is_logged_without_partial_facility_mutation(
    facility_id: str, units_out: int, expected_detail: str
) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.schedule_event(
        make_event(
            f"bad-{facility_id}-{units_out}",
            event_type="facility_outage",
            payload={"facility_id": facility_id, "units_out": units_out},
        )
    )
    dispatcher = EventDispatcher({"facility_outage": FacilityOutageHandler()})
    before = world.facilities["toilet_1"].working_units

    dispatcher.dispatch_due(world, 0)

    assert world.facilities["toilet_1"].working_units == before
    assert world.event_log.records[0].status is EventStatus.FAILED
    assert expected_detail in (world.event_log.records[0].detail or "")
    assert len(world.event_queue) == 0


def test_outage_below_active_occupancy_is_rejected_before_mutation() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    agent = world.agents["A001"]
    agent.activity = Activity.IN_SERVICE
    agent.target_id = "toilet_1"
    world.facilities["toilet_1"].active_agent_ids.append("A001")
    task = ActiveTask(
        id="toilet-service-current",
        kind="toilet_service",
        agent_id="A001",
        resource_id="toilet_1",
        start_minute=0,
        due_minute=5,
    )
    world.active_tasks[task.id] = task
    world.schedule_event(
        ScheduledEvent(
            event_id="service-complete-current",
            kind=EventKind.COMPLETION,
            due_minute=5,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="toilet_service_complete",
            payload={"facility_id": "toilet_1", "agent_id": "A001", "task_id": task.id},
            source_id=task.id,
        )
    )
    world.schedule_event(
        make_event(
            "occupied-outage",
            sequence=world.allocate_event_sequence(),
            event_type="facility_outage",
            payload={"facility_id": "toilet_1", "units_out": 1},
        )
    )
    dispatcher = EventDispatcher({"facility_outage": FacilityOutageHandler()})

    dispatcher.dispatch_due(world, 0)

    assert world.facilities["toilet_1"].working_units == 1
    assert world.event_log.records[0].status is EventStatus.FAILED
    assert "active occupancy" in (world.event_log.records[0].detail or "")
    world.validate()


def test_zero_outage_is_suppressed_and_unknown_handler_is_failed() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.schedule_event(
        make_event(
            "no-op-outage",
            event_type="facility_outage",
            payload={"facility_id": "toilet_1", "units_out": 0},
        )
    )
    world.schedule_event(make_event("unhandled", sequence=1, event_type="future_event"))
    dispatcher = EventDispatcher({"facility_outage": FacilityOutageHandler()})

    dispatcher.dispatch_due(world, 0)

    assert [record.status for record in world.event_log.records] == [
        EventStatus.SUPPRESSED,
        EventStatus.FAILED,
    ]
    assert world.facilities["toilet_1"].working_units == 1


def test_world_state_schema_two_serializes_pending_queue_and_realized_log_separately() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.schedule_event(make_event("pending", due_minute=3, sequence=0))
    world.schedule_event(make_event("realized", sequence=1))
    EventDispatcher({"record": RecordHandler()}).dispatch_due(world, 0)

    serialized = json.loads(world.canonical_json())

    assert world.schema_version == serialized["schema_version"] == 5
    assert [event["event_id"] for event in serialized["event_queue"]] == ["pending"]
    assert [record["event"]["event_id"] for record in serialized["event_log"]] == ["realized"]
    assert serialized["event_log"][0]["status"] == "applied"
    world.validate()


def test_scenario_rejects_reused_event_sequence() -> None:
    scenario_json = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenario_json["scheduled_events"] = [
        {
            "id": "planned-1",
            "due_minute": 1,
            "sequence": 0,
            "event_type": "facility_outage",
        },
        {
            "id": "planned-2",
            "due_minute": 2,
            "sequence": 0,
            "event_type": "facility_outage",
        },
    ]

    with pytest.raises(ValidationError, match="duplicate IDs in scheduled event sequences"):
        ScenarioSpec.model_validate(scenario_json)


def test_scheduled_event_payload_is_detached_immutable_and_json_compatible() -> None:
    caller_payload = {"labels": ["initial", {"source": "scenario"}]}
    event = make_event("immutable", payload=caller_payload)
    caller_payload["labels"][0] = "changed"
    caller_payload["labels"][1]["source"] = "changed"

    assert event.to_dict()["payload"] == {"labels": ["initial", {"source": "scenario"}]}
    with pytest.raises(TypeError):
        event.payload["new"] = "value"  # type: ignore[index]
    with pytest.raises(ValueError, match="finite JSON values"):
        make_event("not-json", payload={"value": float("nan")})
