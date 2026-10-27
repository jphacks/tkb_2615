"""The dispatcher applies due events in a stable order; the outage handler changes all or nothing.

The queue's refusal of reused ids and the event log's indexes are checked in
tests/unit/events/test_queue.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.events.handlers import EventDispatcher, FacilityOutageHandler
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"


def test_due_events_use_minute_priority_then_sequence_and_leave_future_events_pending(
    make_event, record_handler
) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    handler = record_handler()
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


def test_dispatch_is_reproducible_when_events_are_scheduled_in_different_input_order(
    make_event, record_handler
) -> None:
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
        EventDispatcher({"record": record_handler()}).dispatch_due(world, 0)
        return world.canonical_json()

    assert run(event_inputs) == run(tuple(reversed(event_inputs)))


def test_facility_outage_updates_working_capacity_once(make_event) -> None:
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
    facility_id: str, units_out: int, expected_detail: str, make_event
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


def test_outage_below_active_occupancy_is_rejected_before_mutation(make_event) -> None:
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


def test_zero_outage_is_suppressed_and_unknown_handler_is_failed(make_event) -> None:
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
