"""The event queue refuses reused ids and sequences; the event log rebuilds its indexes.

How the dispatcher orders and applies due events is checked in
tests/unit/events/test_handlers.py.
"""

from __future__ import annotations

import random

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.events.queue import DuplicateEventError, EventLog, EventQueue
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
    """The Python EventLog, whose record list code could edit directly."""
    from sheltersim.events.queue import _PythonEventLog as EventLog

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


@pytest.mark.parametrize("implementation", ["python", "native"])
def test_both_event_logs_answer_alike(make_event, implementation) -> None:
    """The EventLog held in Rust (feature/rust-port) answers every method as the Python
    one does, refuses the same duplicates, and audits realization times."""
    from sheltersim import native
    from sheltersim.events.models import EventKind, ScheduledEvent
    from sheltersim.events.queue import _NativeEventLog, _PythonEventLog

    if implementation == "native" and not native.available():
        pytest.skip("the sheltersim_native extension is not installed")
    log_type = _NativeEventLog if implementation == "native" else _PythonEventLog

    def record(index, event_type, status="applied", payload=None, provenance=None):
        return EventLogRecord(
            event=ScheduledEvent(
                event_id=f"e{index}",
                kind=EventKind.EXOGENOUS,
                due_minute=index,
                priority=0,
                sequence=index,
                event_type=event_type,
                payload=payload or {},
                provenance=provenance or {},
            ),
            realized_minute=index // 2,
            status=status,
        )

    records = [
        record(0, "a", provenance={"marker": {"k": 1}, "plain": "x"}),
        record(
            1,
            "care_support_transition",
            payload={
                "request_id": "req",
                "agent_id": "A1",
                "support_kind": "meal",
                "status": "completed",
            },
        ),
        record(2, "facility_outage", payload={"facility_id": "f1"}),
        record(3, "facility_recovery", "failed", payload={"facility_id": "f1"}),
        record(4, "b", provenance={"marker": {"k": 2}}),
        record(5, "a", "failed"),
        record(6, "care_support_transition", payload={"request_id": "req", "status": "x"}),
    ]
    log = log_type(_records=records)
    expected = _PythonEventLog(_records=records)
    assert len(log) == len(expected) == 7
    assert log.records == expected.records and log == expected
    assert log.find("e3") is records[3] and log.find("missing") is None
    assert log.contains_id("e2") and not log.contains_id("e9")
    assert log.contains_sequence(4) and not log.contains_sequence(9)
    assert log.max_sequence == 6
    assert log.at_minute(1) == expected.at_minute(1) == (records[2], records[3])
    assert log.event_types() == expected.event_types()
    assert log.of_types({"b", "a"}) == expected.of_types({"b", "a"})
    assert log.of_types(["facility_outage"]) == [records[2]]
    assert log.with_provenance_object("marker") == expected.with_provenance_object("marker")
    assert log.with_provenance_object("plain") == []
    assert log.has_prior_care_request("req", "e6") and not log.has_prior_care_request("req", "e1")
    assert log.completed_support_kinds("A1") == frozenset({"meal"})
    assert log.applied_facility_event_ids("f1") == frozenset({"e2"})
    assert log.recent(3) == expected.recent(3)
    assert log.recent_matching(frozenset({"a"}), limit=5, scan_limit=3) == (
        expected.recent_matching(frozenset({"a"}), limit=5, scan_limit=3)
    )
    assert log.records_from(5) == records[5:] and log.last() is records[6]
    assert log.event_ids() == [f"e{index}" for index in range(7)]
    with pytest.raises(DuplicateEventError, match="already realized"):
        log.append(record(0, "a"))
    duplicate_sequence = EventLogRecord(
        event=ScheduledEvent(
            event_id="other",
            kind=EventKind.EXOGENOUS,
            due_minute=0,
            priority=0,
            sequence=3,
            event_type="a",
        ),
        realized_minute=0,
        status="applied",
    )
    with pytest.raises(DuplicateEventError, match="sequence 3 already realized"):
        log.append(duplicate_sequence)
    log.validate(minute_index=3, duration_minutes=10)
    with pytest.raises(ValueError, match="future realization"):
        log.validate(minute_index=2, duration_minutes=10)
    with pytest.raises(ValueError, match="outside the scenario duration"):
        log.validate(minute_index=3, duration_minutes=3)
    import copy

    # As storage.projects._copy_restored copies a world: frozen records are shared.
    copied = copy.deepcopy(log, {id(item): item for item in records})
    assert copied == log and copied is not log and copied.find("e1") is records[1]
    copied.append(record(7, "c"))
    assert len(copied) == 8 and len(log) == 7


@pytest.mark.parametrize("implementation", ["python", "native"])
def test_an_event_log_read_back_from_saved_bytes_makes_records_when_read(implementation) -> None:
    """from_saved builds the indexes from the saved canonical JSON; the native store
    makes a record object only when one is read, equal to the record saved."""
    from sheltersim import native
    from sheltersim.events.queue import (
        _NativeEventLog,
        _PythonEventLog,
        record_bytes,
        record_from_bytes,
    )

    if implementation == "native" and not native.available():
        pytest.skip("the sheltersim_native extension is not installed")
    log_type = _NativeEventLog if implementation == "native" else _PythonEventLog
    records = _records(6, kinds=("a", "b"))
    saved = [record_bytes(record) for record in records]
    assert [record_from_bytes(raw) for raw in saved] == records
    log = log_type.from_saved(saved)
    if implementation == "native":
        assert log._store.unread() == 6
        assert log.find("e3") == records[3]
        assert log._store.unread() == 5
        assert log.of_types({"b"}) == [records[1], records[3], records[5]]
        assert log._store.unread() == 3
    assert log.records == tuple(records)
    assert log.items_bytes(0, 6) == saved
    assert log.with_provenance_object("marker") == records[1::2]
    with pytest.raises(DuplicateEventError):
        log_type.from_saved([saved[0], saved[0]])


def test_the_realized_minute_of_saved_bytes_is_the_records_own() -> None:
    """A payload key of the same name never shadows the record's realized_minute."""
    from sheltersim.events.models import EventKind, ScheduledEvent
    from sheltersim.events.queue import record_bytes, saved_realized_minute

    event = ScheduledEvent(
        event_id="nested-minute",
        kind=EventKind.EXOGENOUS,
        due_minute=3,
        priority=0,
        sequence=1,
        event_type="unknown_test_event",
        payload={"realized_minute": 99, "rows": [{"realized_minute": 98}]},
    )
    record = EventLogRecord(event=event, realized_minute=7, status=EventStatus.APPLIED)
    assert saved_realized_minute(record_bytes(record)) == 7


def test_the_rust_saved_record_checks_name_the_model_fields() -> None:
    import dataclasses

    sheltersim_native = pytest.importorskip("sheltersim_native")

    from sheltersim.events.models import _EVENT_FIELDS

    record_fields, event_fields = sheltersim_native.saved_record_fields()
    assert set(record_fields) == {item.name for item in dataclasses.fields(EventLogRecord)}
    assert set(event_fields) == _EVENT_FIELDS


def test_of_types_from_gives_the_records_of_of_types_from_a_position(make_event) -> None:
    log = EventLog()
    for sequence, kind in enumerate(["a", "b", "c", "a", "b"]):
        log.append(
            EventLogRecord(
                event=make_event(f"event-{sequence}", sequence=sequence, event_type=kind),
                realized_minute=sequence,
                status=EventStatus.APPLIED,
            )
        )
    wanted = {"a", "b", "missing"}
    records = log.of_types(wanted)
    positions = [0, 1, 3, 4]
    for start in range(7):
        expected = [record for record, at in zip(records, positions, strict=True) if at >= start]
        assert log.of_types_from(wanted, start) == expected
    assert [record.event.sequence for record in log.of_types_from({"a"}, 1)] == [3]


def _event(event_id: str, sequence: int, due_minute: int = 1, priority: int = 0) -> ScheduledEvent:
    return ScheduledEvent(event_id, EventKind.ENDOGENOUS, due_minute, priority, sequence, "tick")


def test_the_indexes_answer_as_a_scan_of_the_events_does() -> None:
    """find, due and pending read indexes; the list stays what they are compared with."""
    rng = random.Random(20261031)
    queue = EventQueue()
    live: list[ScheduledEvent] = []
    for sequence in range(1500):
        action = rng.random()
        if action < 0.55 or not live:
            event = _event(f"e-{sequence}", sequence, rng.randrange(40), rng.randrange(3))
            queue.schedule(event)
            live.append(event)
        elif action < 0.85:
            event = live.pop(rng.randrange(len(live)))
            assert queue.remove(event.event_id) is event
        else:
            index = rng.randrange(len(live))
            current = live[index]
            event = _event(current.event_id, current.sequence, rng.randrange(40), current.priority)
            queue.replace(current.event_id, event)
            live[index] = event
        expected = sorted(live, key=lambda item: (item.due_minute, item.priority, item.sequence))
        assert list(queue.pending()) == expected
        minute = rng.randrange(40)
        assert list(queue.due(minute)) == [item for item in expected if item.due_minute <= minute]
        probe = rng.choice(live) if live else None
        if probe is not None:
            assert queue.find(probe.event_id) is probe
        assert queue.find(f"e-{sequence + 1}") is None
        assert len(queue) == len(live)


def test_the_indexes_still_refuse_repeated_identities_and_unknown_events() -> None:
    event = _event("e-1", 1)
    queue = EventQueue([event])
    assert queue.find("e-1") is event
    assert queue.find(["not", "hashable"]) is None
    with pytest.raises(DuplicateEventError):
        queue.schedule(_event("e-1", 2))
    with pytest.raises(DuplicateEventError):
        queue.schedule(_event("e-2", 1))
    queue.remove("e-1")
    with pytest.raises(KeyError):
        queue.remove("e-1")
    queue.schedule(_event("e-1", 1, due_minute=5))
    assert queue.pending()[0].due_minute == 5
