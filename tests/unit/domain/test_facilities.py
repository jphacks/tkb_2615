"""Toilet requests, FIFO service reservations, and typed completion events."""

from __future__ import annotations

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.events.handlers import (
    EventDispatcher,
    FacilityOutageHandler,
    FacilityRecoveryHandler,
    ToiletServiceCompletionHandler,
)
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec


def make_world() -> WorldState:
    return WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))


def test_toilet_service_is_fifo_capacity_bounded_and_wait_is_derived() -> None:
    world = make_world()
    manager = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    first = manager.request("A001", "toilet_1")
    second = manager.request("A002", "toilet_1")

    assert (first.queue_position, second.queue_position) == (1, 2)
    assert world.facilities["toilet_1"].queue_agent_ids == ["A001", "A002"]

    started = manager.arbitrate(world.minute_index, CounterRNG(world.seed))

    assert len(started) == 1
    assert started[0].payload["agent_id"] == "A001"
    assert started[0].kind is EventKind.COMPLETION
    assert started[0].due_minute == 2
    assert world.facilities["toilet_1"].active_agent_ids == ["A001"]
    assert world.facilities["toilet_1"].queue_agent_ids == ["A002"]
    assert world.agents["A001"].activity is Activity.IN_SERVICE
    assert world.agents["A002"].activity is Activity.QUEUED
    assert (
        len(world.facilities["toilet_1"].active_agent_ids)
        <= world.facilities["toilet_1"].working_units
    )
    assert world.metric_state["toilet_wait_count"] == 1.0
    assert world.metric_state["toilet_wait_total_minutes"] == 0.0
    world.validate()

    world.minute_index = 2
    dispatcher = EventDispatcher({"toilet_service_complete": ToiletServiceCompletionHandler()})
    completed = dispatcher.dispatch_due(world, minute=2)
    assert completed[0].status is EventStatus.APPLIED
    assert world.facilities["toilet_1"].active_agent_ids == []
    assert world.agents["A001"].activity is Activity.IDLE
    assert world.agents["A001"].needs.toilet_urgency == 0.0

    next_started = manager.arbitrate(2, CounterRNG(world.seed))
    assert [event.payload["agent_id"] for event in next_started] == ["A002"]
    assert world.metric_state["toilet_wait_count"] == 2.0
    assert world.metric_state["toilet_wait_total_minutes"] == 2.0
    assert world.metric_state["toilet_wait_max_minutes"] == 2.0
    world.validate()


def test_toilet_duration_and_event_trace_are_reproducible_for_seed_and_request() -> None:
    def start_once() -> str:
        world = make_world()
        manager = ToiletServiceManager(world)
        manager.request("A001", "toilet_1")
        manager.arbitrate(0, CounterRNG(world.seed))
        return world.canonical_json()

    assert start_once() == start_once()


def test_duplicate_request_is_rejected_without_partial_state_change() -> None:
    world = make_world()
    manager = ToiletServiceManager(world)
    manager.request("A001", "toilet_1")
    before = world.canonical_json()

    with pytest.raises(ValueError, match="cannot request.*while active"):
        manager.request("A001", "toilet_2")

    assert world.canonical_json() == before


def test_queue_waits_during_outage_then_starts_after_recovery_event() -> None:
    world = make_world()
    world.facilities["toilet_1"].working_units = 0
    manager = ToiletServiceManager(world, min_service_minutes=1, max_service_minutes=1)
    manager.request("A001", "toilet_1")
    recovery = ScheduledEvent(
        event_id="restore-toilet-1",
        kind=EventKind.EXOGENOUS,
        due_minute=1,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="facility_recovery",
        payload={"facility_id": "toilet_1", "units_restored": 1},
    )
    world.schedule_event(recovery)
    assert manager.arbitrate(0, CounterRNG(world.seed)) == ()
    assert world.facilities["toilet_1"].queue_agent_ids == ["A001"]

    world.minute_index = 1
    dispatcher = EventDispatcher({"facility_recovery": FacilityRecoveryHandler()})
    result = dispatcher.dispatch_due(world, minute=1)
    assert result[0].status is EventStatus.APPLIED
    started = manager.arbitrate(1, CounterRNG(world.seed))

    assert world.facilities["toilet_1"].working_units == 1
    assert started[0].payload["agent_id"] == "A001"
    assert started[0].provenance["queue_wait_minutes"] == 1
    assert started[0].provenance["source_type"] == "assumption"
    assert started[0].provenance["source_id"] == "ASM-TOILET-SERVICE-DURATION-V1"
    world.validate()


def test_recovery_above_installed_capacity_fails_without_mutation() -> None:
    world = make_world()
    world.schedule_event(
        ScheduledEvent(
            event_id="bad-restore",
            kind=EventKind.EXOGENOUS,
            due_minute=0,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="facility_recovery",
            payload={"facility_id": "toilet_1", "units_restored": 1},
        )
    )
    before = world.facilities["toilet_1"].working_units
    result = EventDispatcher({"facility_recovery": FacilityRecoveryHandler()}).dispatch_due(
        world, 0
    )

    assert world.facilities["toilet_1"].working_units == before
    assert result[0].status is EventStatus.FAILED
    assert "installed capacity" in (result[0].detail or "")


def test_stale_duplicate_completion_is_logged_as_failed_without_double_release() -> None:
    world = make_world()
    manager = ToiletServiceManager(world, min_service_minutes=1, max_service_minutes=1)
    manager.request("A001", "toilet_1")
    original = manager.arbitrate(0, CounterRNG(world.seed))[0]
    duplicate = ScheduledEvent(
        event_id="duplicate-completion",
        kind=EventKind.COMPLETION,
        due_minute=original.due_minute,
        priority=1,
        sequence=world.allocate_event_sequence(),
        event_type=original.event_type,
        payload=dict(original.payload),
    )
    world.schedule_event(duplicate)
    world.minute_index = original.due_minute
    dispatcher = EventDispatcher({"toilet_service_complete": ToiletServiceCompletionHandler()})

    result = dispatcher.dispatch_due(world, minute=world.minute_index)

    assert [record.status for record in result] == [EventStatus.APPLIED, EventStatus.FAILED]
    assert world.facilities["toilet_1"].active_agent_ids == []
    assert world.agents["A001"].activity is Activity.IDLE
    assert "active due task" in (result[1].detail or "")
    world.validate()


def test_outage_that_would_interrupt_active_service_is_failed_atomically() -> None:
    world = make_world()
    facility = world.facilities["toilet_1"]
    agent = world.agents["A001"]
    agent.activity = Activity.IN_SERVICE
    agent.target_id = facility.id
    facility.active_agent_ids.append(agent.id)
    task_id = "toilet-service-current"
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="toilet_service",
        agent_id=agent.id,
        resource_id=facility.id,
        start_minute=0,
        due_minute=5,
    )
    world.schedule_event(
        ScheduledEvent(
            event_id="service-complete-current",
            kind=EventKind.COMPLETION,
            due_minute=5,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="toilet_service_complete",
            payload={"facility_id": facility.id, "agent_id": agent.id, "task_id": task_id},
            source_id=task_id,
        )
    )
    outage = ScheduledEvent(
        event_id="outage-current",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="facility_outage",
        payload={"facility_id": facility.id, "units_out": 1},
    )
    world.schedule_event(outage)
    result = EventDispatcher({"facility_outage": FacilityOutageHandler()}).dispatch_due(world, 0)

    assert facility.working_units == 1
    assert result[0].status is EventStatus.FAILED
    assert "active occupancy" in (result[0].detail or "")
    world.validate()
