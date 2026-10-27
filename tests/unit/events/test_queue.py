"""The event queue refuses reused ids and sequences; the event log rebuilds its indexes.

How the dispatcher orders and applies due events is checked in
tests/unit/events/test_handlers.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventLogRecord, EventStatus
from sheltersim.events.queue import DuplicateEventError, EventLog
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"


def test_duplicate_event_id_or_sequence_is_rejected_and_realized_event_is_idempotent(
    make_event, record_handler
) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    event = make_event("repeat", sequence=0)
    world.schedule_event(event)

    with pytest.raises(DuplicateEventError, match="duplicate event ID"):
        world.schedule_event(make_event("repeat", sequence=1))
    with pytest.raises(DuplicateEventError, match="duplicate event sequence"):
        world.schedule_event(make_event("other", sequence=0))

    dispatcher = EventDispatcher({"record": record_handler()})
    assert len(dispatcher.dispatch_due(world, 0)) == 1
    assert dispatcher.dispatch_due(world, 0) == ()
    with pytest.raises(DuplicateEventError, match="already realized"):
        world.schedule_event(event)
    assert len(world.event_log) == 1


def test_event_log_rebuilds_bounded_indexes_from_immutable_records(make_event) -> None:
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
