"""The event queue refuses reused ids and sequences; the event log rebuilds its indexes.

How the dispatcher orders and applies due events is checked in
tests/unit/events/test_handlers.py.
"""

from __future__ import annotations

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventLogRecord, EventStatus
from sheltersim.events.queue import DuplicateEventError, EventLog
from sheltersim.experiments.scenario import ScenarioSpec


def test_duplicate_event_id_or_sequence_is_rejected_and_realized_event_is_idempotent(
    make_event, record_handler
) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
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
    assert event_log.applied_facility_event_ids("toilet_1") == frozenset({"outage-history"})
    assert event_log.completed_support_kinds("A001") == frozenset({"medication_support"})
    assert event_log.completed_support_kinds("A002") == frozenset()


def _records(count: int, *, kinds=("record",)) -> list[EventLogRecord]:
    from sheltersim.events.models import EventKind, ScheduledEvent

    return [
        EventLogRecord(
            event=ScheduledEvent(
                event_id=f"e{index}",
                kind=EventKind.EXOGENOUS,
                due_minute=index,
                priority=0,
                sequence=index,
                event_type=kinds[index % len(kinds)],
                payload={},
                provenance={"marker": {"index": index}} if index % 2 else {},
            ),
            realized_minute=index,
            status=EventStatus.APPLIED,
        )
        for index in range(count)
    ]


def test_records_of_some_types_or_with_a_provenance_object_keep_append_order() -> None:
    records = _records(6, kinds=("a", "b", "c"))
    log = EventLog(_records=records)
    assert log.of_types({"c", "a"}) == [records[0], records[2], records[3], records[5]]
    assert log.of_types(["b"]) == [records[1], records[4]]
    assert log.of_types({"missing"}) == []
    assert log.event_types() == frozenset({"a", "b", "c"})
    assert log.with_provenance_object("marker") == records[1::2]
    assert log.with_provenance_object("missing") == []


def test_events_by_id_reads_realized_events_first_without_copying_the_log(make_event) -> None:
    from sheltersim.events.queue import EventsById

    records = _records(3)
    log = EventLog(_records=records)
    pending = make_event("pending", sequence=10)
    shadowed = make_event("e1", sequence=11)
    events = EventsById(log, [pending, shadowed])
    assert events["e1"] is records[1].event
    assert events.get("pending") is pending
    assert "e2" in events and "missing" not in events and None not in events
    assert events.get(None) is None
    assert sorted(events) == ["e0", "e1", "e2", "pending"]
    assert len(events) == 4
    with pytest.raises(KeyError):
        events["missing"]


def test_an_audit_checks_only_new_records_while_earlier_ones_are_unchanged(make_event) -> None:
    records = _records(4)
    log = EventLog(_records=records[:3])
    log.validate(minute_index=3, duration_minutes=10)
    assert log._audited == records[:3]
    log.append(records[3])
    log.validate(minute_index=4, duration_minutes=10)
    assert log._audited == records

    # A record placed without append, or a replaced record, is found.
    bypass = EventLogRecord(
        event=make_event("e9", sequence=9), realized_minute=4, status=EventStatus.APPLIED
    )
    log._records.append(bypass)
    with pytest.raises(ValueError, match="indexes do not match"):
        log.validate(minute_index=4, duration_minutes=10)
    log._records.pop()
    duplicate = EventLogRecord(
        event=make_event("e0", sequence=0), realized_minute=2, status=EventStatus.APPLIED
    )
    log._records[1] = duplicate
    with pytest.raises(ValueError, match="duplicate event IDs"):
        log.validate(minute_index=4, duration_minutes=10)
    log._records[1] = records[1]

    # An earlier minute or another duration audits every record again.
    with pytest.raises(ValueError, match="future realization"):
        log.validate(minute_index=2, duration_minutes=10)
    with pytest.raises(ValueError, match="outside the scenario duration"):
        log.validate(minute_index=4, duration_minutes=3)
    log.validate(minute_index=4, duration_minutes=10)
