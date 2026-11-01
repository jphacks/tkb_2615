"""The Rust EventQueue and EventLog: the Python ones' order, bytes and errors, and a rule's API."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim import native
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.checkpoint_cache import CheckpointHistoryCache
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.events.queue import (
    DuplicateEventError,
    _LargeOrderEventQueue,
    _NativeEventLog,
    _NativeEventQueue,
    _PythonEventLog,
    _PythonEventQueue,
)
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _event(
    name: str, sequence: int, due: int = 3, priority: int = 0, **payload: Any
) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=name,
        kind=EventKind.ENDOGENOUS,
        due_minute=due,
        priority=priority,
        sequence=sequence,
        event_type="record",
        payload=payload,
        source_id="source-1",
        causation_id=None,
        provenance={"source_type": "assumption", "marker": {"effect": 1}},
    )


def _order(queue: Any) -> list[str]:
    return [event.event_id for event in queue.pending()]


def _state() -> Any:
    return native.state("events")


def _world(duration: int = 60) -> WorldState:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.duration_minutes = duration
    return world


def _python_bytes(record: EventLogRecord) -> bytes:
    return json.dumps(
        record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def test_random_writes_keep_the_order_and_the_errors_of_the_python_queue() -> None:
    chooser = random.Random(7)
    mine, theirs = _NativeEventQueue(), _PythonEventQueue()
    live: list[str] = []
    for step in range(3000):
        action = chooser.random()
        if action < 0.5 or not live:
            event = _event(
                f"e{step}", step, due=chooser.randrange(6), priority=chooser.randrange(-2, 3)
            )
            mine.schedule(event)
            theirs.schedule(event)
            live.append(event.event_id)
        elif action < 0.8:
            name = live.pop(chooser.randrange(len(live)))
            assert mine.remove(name) is theirs.remove(name)
        else:
            name = chooser.choice(live)
            current = theirs.find(name)
            event = _event(
                name, current.sequence, due=chooser.randrange(6), priority=chooser.randrange(-2, 3)
            )
            mine.replace(name, event)
            theirs.replace(name, event)
        assert mine.pending() == theirs.pending()
        assert len(mine) == len(theirs)
        minute = chooser.randrange(7)
        assert mine.due(minute) == theirs.due(minute)
        name = chooser.choice([*live, "gone"])
        assert mine.find(name) is theirs.find(name)
    assert mine._events == theirs._events
    assert mine == theirs
    assert repr(mine) == repr(theirs).replace("_PythonEventQueue", "EventQueue")


@pytest.mark.parametrize("queue_type", [_NativeEventQueue, _PythonEventQueue])
def test_the_errors_of_both_queues(queue_type: Any) -> None:
    queue = queue_type([_event("a", 1), _event("b", 2)])
    with pytest.raises(DuplicateEventError, match="duplicate event ID 'a'"):
        queue.schedule(_event("a", 9))
    with pytest.raises(DuplicateEventError, match="duplicate event sequence 1"):
        queue.schedule(_event("c", 1))
    with pytest.raises(KeyError):
        queue.remove("gone")
    with pytest.raises(KeyError):
        queue.replace("gone", _event("gone", 5))
    with pytest.raises(DuplicateEventError, match="must preserve ID and sequence"):
        queue.replace("a", _event("a", 5))
    with pytest.raises(ValueError, match="minute cannot be negative"):
        queue.due(-1)
    assert queue.find(1) is None
    queue.validate()


@pytest.mark.parametrize("queue_type", [_NativeEventQueue, _PythonEventQueue])
def test_a_queue_built_with_repeats_is_refused_by_validate(queue_type: Any) -> None:
    with pytest.raises(ValueError, match="duplicate event IDs"):
        queue_type([_event("a", 1), _event("a", 2)]).validate()
    with pytest.raises(ValueError, match="duplicate event sequences"):
        queue_type([_event("a", 1), _event("b", 1)]).validate()


def test_an_order_outside_i64_leaves_rust_and_keeps_working() -> None:
    huge = _event("huge", 3, due=10**30)
    queue = _NativeEventQueue([_event("a", 1, due=5)])
    queue.schedule(huge)
    assert type(queue) is _LargeOrderEventQueue
    assert isinstance(queue, _NativeEventQueue)
    assert _order(queue) == ["a", "huge"]
    assert [event.event_id for event in queue.due(5)] == ["a"]
    assert queue.remove("a").event_id == "a"
    assert _order(copy.deepcopy(queue)) == ["huge"]
    assert type(_NativeEventQueue([huge])) is _LargeOrderEventQueue
    world = _world()
    world.event_queue = queue
    with pytest.raises(native._native.Unsupported):
        _state().allocate_sequence(world)
    negative = _event("big", 4, due=1, priority=-(10**30))
    other = _NativeEventQueue([_event("a", 1)])
    other.replace("a", _event("a", 1, due=4))
    other.schedule(negative)
    assert _order(other) == ["big", "a"]
    assert other.due(10**40) == other.pending()


def test_due_with_a_minute_outside_i64() -> None:
    queue = _NativeEventQueue([_event("a", 1)])
    assert queue.due(10**30) == queue.pending()


def test_a_copied_queue_is_independent_and_shares_the_events() -> None:
    queue = _NativeEventQueue([_event("a", 1), _event("b", 2)])
    other = copy.deepcopy(queue)
    assert other.pending()[0] is queue.pending()[0]
    other.remove("a")
    assert _order(queue) == ["a", "b"]
    assert _order(other) == ["b"]


def test_pending_is_rebuilt_only_when_the_queue_changes() -> None:
    queue = _NativeEventQueue([_event("a", 1)])
    first = queue.pending()
    assert queue.pending() is first
    queue.schedule(_event("b", 2))
    assert queue.pending() is not first
    assert _order(queue) == ["a", "b"]


_VALUES: dict[str, Any] = {
    "text": 'é"\\\n',
    "whole": 3,
    "fraction": 0.1 + 0.2,
    "negative_zero": -0.0,
    "tiny": 1e-7,
    "large": 1e22,
    "big": 2**70,
    "flag": True,
    "none": None,
    "items": [1, 2.0, "x", [None], {"inner": False}],
    "object": {"b": 1, "a": {"c": [0.5]}},
}


def test_a_rows_bytes_and_object_are_those_of_the_python_event() -> None:
    event = _event("a", 1, **_VALUES)
    record = EventLogRecord(event, 4, EventStatus.APPLIED, "why")
    state = _state()
    raw = _python_bytes(record)
    assert state.row_record_json(event, 4, "applied", "why") == raw
    copied = state.row_copy(event)
    assert copied == event and copied is not event
    assert type(copied) is ScheduledEvent
    assert copied.kind is EventKind.ENDOGENOUS
    # A float stays a float and keeps its bits, and the key order stays.
    assert json.dumps(copied.to_dict()) == json.dumps(event.to_dict())
    assert native.same_saved_records(
        [EventLogRecord(copied, 4, EventStatus.APPLIED, "why")],
        [raw],
        EventLogRecord,
        ScheduledEvent,
    )
    read_back = state.saved_row_copy(raw)
    assert read_back == event
    assert list(read_back.payload) == sorted(event.payload)
    assert state.saved_row_copy(b"not json") is None


def test_an_event_with_another_type_of_value_has_no_row_and_no_bytes_from_rust() -> None:
    class Text(str):
        pass

    event = _event("a", 1, label=Text("x"))
    state = _state()
    assert state.row_copy(event) is None
    assert state.row_record_json(event, 4, "applied") is None
    log = _NativeEventLog()
    log.append(EventLogRecord(_event("b", 2), 4, EventStatus.APPLIED))
    assert log.all_encoded()
    log.append(EventLogRecord(event, 4, EventStatus.APPLIED))
    assert not log.all_encoded()
    # The Python encoder makes its bytes, as it always did.
    assert json.loads(log.items_bytes(1, 2)[0])["event"]["payload"] == {"label": "x"}


def test_appending_writes_the_bytes_that_python_writes() -> None:
    log = _NativeEventLog()
    records = [
        EventLogRecord(_event(f"e{index}", index, **_VALUES), index, EventStatus.APPLIED)
        for index in range(5)
    ]
    for record in records:
        log.append(record)
    assert log.all_encoded()
    raws = log.items_bytes(0, 5)
    assert raws == [_python_bytes(record) for record in records]
    # The same objects after that, which a capture relies on.
    assert log.items_bytes(0, 1)[0] is raws[0]
    assert log.records_from(0) == records
    assert log.find("e3") is records[3]
    copied = copy.deepcopy(log)
    assert copied.items_bytes(0, 1)[0] is raws[0]
    fresh = _NativeEventLog.from_saved(raws)
    assert fresh.all_encoded()
    assert fresh == log


def test_a_rule_schedules_with_the_python_methods_rules() -> None:
    state = _state()
    world, twin = _world(), _world()
    assert state.allocate_sequence(world) == twin.allocate_event_sequence()
    assert world.id_counters == twin.id_counters

    high = world.id_counters["event"] + 50
    state.schedule_row(world, _event("first", high, due=9))
    twin.schedule_event(_event("first", high, due=9))
    assert world.id_counters == twin.id_counters
    pending = world.event_queue.pending()
    assert type(pending[-1]) is ScheduledEvent
    assert [item.to_dict() for item in pending] == [
        item.to_dict() for item in twin.event_queue.pending()
    ]
    # The object made for Python stays the object.
    assert world.event_queue.find("first") is world.event_queue.find("first")
    assert world.event_queue.find("first") is world.event_queue.due(59)[-1]

    with pytest.raises(ValueError, match="'late' is outside the scenario"):
        state.schedule_row(world, _event("late", high + 1, due=60))
    with pytest.raises(DuplicateEventError, match="duplicate event ID 'first'"):
        state.schedule_row(world, _event("first", high + 1))
    with pytest.raises(DuplicateEventError, match=f"duplicate event sequence {high}"):
        state.schedule_row(world, _event("other", high))
    assert world.id_counters == twin.id_counters


def test_a_rule_records_an_outcome_python_reads_and_saves_as_its_own() -> None:
    state = _state()
    world = _world()
    event = _event("a", 500, due=0, agent_id="A1", transition="started")
    world.schedule_event(event)
    assert state.rows_due(world, 0)[-1][0] == "a"
    assert state.settle_row(world, "a", 0, "applied", "fine")
    assert world.event_queue.find("a") is None
    assert not state.settle_row(world, "a", 0, "applied")
    assert world.event_log.unread() == 1
    record = world.event_log.find("a")
    assert record.event == event
    assert record.status is EventStatus.APPLIED and record.detail == "fine"
    expected = EventLogRecord(event, 0, EventStatus.APPLIED, "fine")
    assert world.event_log.items_bytes(0, 1) == [_python_bytes(expected)]
    assert world.event_log.all_encoded()
    assert state.saved_rows(world, "record")[0][1:] == (0, "applied", "fine")
    assert state.saved_rows(world, "record")[0][0] == event
    with pytest.raises(DuplicateEventError, match="event 'a' already realized"):
        state.record_row(world, event, 1, "applied")
    with pytest.raises(DuplicateEventError, match="event 'a' already realized"):
        world.event_log.append(expected)
    with pytest.raises(DuplicateEventError, match="event 'a' already realized"):
        state.schedule_row(world, event)


@pytest.mark.parametrize(
    ("event_type", "payload", "status"),
    [
        (
            "care_support_transition",
            {"request_id": "r", "agent_id": "A", "support_kind": "k", "status": "completed"},
            "applied",
        ),
        (
            "care_support_transition",
            {"request_id": "r", "agent_id": "A", "support_kind": "k", "status": "completed"},
            "failed",
        ),
        ("facility_outage", {"facility_id": "f"}, "applied"),
        ("facility_recovery", {"facility_id": "f"}, "failed"),
        ("agent_action_lifecycle", {"agent_id": "A", "transition": "started"}, "applied"),
        ("agent_action_lifecycle", {"agent_id": "A", "transition": "completed"}, "applied"),
        ("other", {"agent_id": "A", "transition": "started", "facility_id": "f"}, "applied"),
    ],
)
def test_a_rules_record_fills_the_indexes_of_a_python_record(
    event_type: str, payload: dict[str, Any], status: str
) -> None:
    event = ScheduledEvent(
        event_id="a",
        kind=EventKind.COMPLETION,
        due_minute=0,
        priority=0,
        sequence=1,
        event_type=event_type,
        payload=payload,
        provenance={"marker": {"x": 1}, "plain": "y"},
    )
    state = _state()
    world = _world()
    state.record_row(world, event, 0, status)
    theirs = _PythonEventLog()
    record = EventLogRecord(event, 0, EventStatus(status))
    theirs.append(record)
    python_native = _NativeEventLog()
    python_native.append(record)
    for log in (world.event_log, python_native):
        assert log.event_types() == theirs.event_types()
        assert log.has_prior_care_request("r", "other") == theirs.has_prior_care_request(
            "r", "other"
        )
        assert log.completed_support_kinds("A") == theirs.completed_support_kinds("A")
        assert log.applied_facility_event_ids("f") == theirs.applied_facility_event_ids("f")
        assert log.with_provenance_object("marker") == theirs.with_provenance_object("marker")
        assert log.with_provenance_object("plain") == []
        assert log.at_minute(0) == theirs.at_minute(0)
    assert world.event_log.active_lifecycle_records() == python_native.active_lifecycle_records()


def test_a_handler_gets_a_rules_event_like_any_other() -> None:
    seen: list[Any] = []

    class Handler:
        def apply(self, world: WorldState, event: ScheduledEvent) -> None:
            seen.append(event)

    world, twin = _world(), _world()
    event = _event("a", 500, due=0, value=0.25)
    _state().schedule_row(world, event)
    twin.schedule_event(event)
    dispatcher = EventDispatcher({"record": Handler()})
    for item in (world, twin):
        dispatcher.dispatch_due(item, 0)
    assert seen[0] == seen[1]
    assert type(seen[0]) is ScheduledEvent
    assert world.event_log.items_bytes(0, 1) == twin.event_log.items_bytes(0, 1)


def test_a_writer_refuses_a_world_that_is_not_rusts_and_changes_nothing() -> None:
    state = _state()
    unsupported = native._native.Unsupported
    event = _event("a", 500)
    world = _world()
    before = dict(world.id_counters)
    world.id_counters = {**before, "event": "text"}
    with pytest.raises(unsupported):
        state.schedule_row(world, event)
    world.id_counters = before
    world.event_log = _PythonEventLog()
    with pytest.raises(unsupported):
        state.schedule_row(world, event)
    world = _world()
    world.event_queue = _PythonEventQueue()
    with pytest.raises(unsupported):
        state.schedule_row(world, event)
    assert world.event_queue.pending() == ()


def _advance_log(world: WorldState, start: int, count: int) -> None:
    for index in range(start, start + count):
        sequence = world.allocate_event_sequence()
        world.event_log.append(
            EventLogRecord(
                _event(f"made-{index}", sequence, due=0, value=index + 0.5),
                0,
                EventStatus.APPLIED,
            )
        )


def test_a_save_does_not_read_the_bytes_of_a_log_that_wrote_them(monkeypatch: Any) -> None:
    world = _world()
    cache = CheckpointHistoryCache()
    calls = {"same": 0, "extend": 0}
    same = native.same_saved_records
    extend = _NativeEventLog.extend_saved

    def counted_same(records: Any, *rest: Any) -> Any:
        calls["same"] += len(records)
        return same(records, *rest)

    def counted_extend(self: Any, items: Any) -> None:
        items = list(items)
        calls["extend"] += len(items)
        extend(self, items)

    monkeypatch.setattr(native, "same_saved_records", counted_same)
    monkeypatch.setattr(_NativeEventLog, "extend_saved", counted_extend)
    _advance_log(world, 0, 3)
    first = Checkpoint.capture(world, history_cache=cache)
    _advance_log(world, 3, 2)
    second = Checkpoint.capture(world, history_cache=cache)
    assert calls == {"same": 0, "extend": 0}
    # The same Checkpoint a capture without the cache makes, and they read back.
    assert second.checkpoint_hash == Checkpoint.capture(world).checkpoint_hash
    assert first.simulation_minute == second.simulation_minute
    restored = second.restore_world()
    assert len(restored.event_log) == 5
    assert restored.event_log.find("made-4").event.payload["value"] == 4.5


def test_a_log_with_a_record_it_could_not_write_gets_the_old_restore_check(
    monkeypatch: Any,
) -> None:
    class Text(str):
        pass

    world = _world()
    cache = CheckpointHistoryCache()
    extend = _NativeEventLog.extend_saved
    extended: list[int] = []

    def counted_extend(self: Any, items: Any) -> None:
        items = list(items)
        extended.append(len(items))
        extend(self, items)

    monkeypatch.setattr(_NativeEventLog, "extend_saved", counted_extend)
    _advance_log(world, 0, 2)
    Checkpoint.capture(world, history_cache=cache)
    assert extended in ([], [0])
    sequence = world.allocate_event_sequence()
    world.event_log.append(
        EventLogRecord(_event("text", sequence, due=0, label=Text("x")), 0, EventStatus.APPLIED)
    )
    _advance_log(world, 2, 1)
    captured = Checkpoint.capture(world, history_cache=cache)
    assert sum(extended) == 4
    assert captured.checkpoint_hash == Checkpoint.capture(world).checkpoint_hash
    Checkpoint.capture(world, history_cache=cache)
    assert len(captured.restore_world().event_log) == 4


def _random_value(chooser: random.Random, depth: int = 0) -> Any:
    kinds = 8 if depth < 4 else 6
    kind = chooser.randrange(kinds)
    if kind == 0:
        return None
    if kind == 1:
        return chooser.random() < 0.5
    if kind == 2:
        return chooser.choice([0, -1, 2**53, 2**63, -(2**63) - 1, chooser.randrange(-1000, 1000)])
    if kind == 3:
        scale = 10.0 ** chooser.randrange(-30, 30)
        return chooser.choice([0.0, -0.0, 1.0, 1e16, 1e-4, chooser.random() * scale])
    if kind in (4, 5):
        return "".join(chooser.choice('aé"\\\n\tあ\U0001f600\x7f\x00') for _ in range(4))
    if kind == 6:
        return [_random_value(chooser, depth + 1) for _ in range(chooser.randrange(4))]
    return {
        f"k{chooser.randrange(50)}é": _random_value(chooser, depth + 1)
        for _ in range(chooser.randrange(4))
    }


def test_random_values_written_on_appending_restore_to_the_record() -> None:
    """A save trusts this writer: its restore check does not read bytes the log wrote."""
    chooser = random.Random(11)
    state = _state()
    log = _NativeEventLog()
    records = []
    for index in range(2000):
        event = _event(f"e{index}", index, value=_random_value(chooser))
        record = EventLogRecord(event, index, chooser.choice(list(EventStatus)), None)
        log.append(record)
        records.append(record)
        raw = log.items_bytes(index, index + 1)[0]
        assert raw == _python_bytes(record)
        assert state.row_record_json(event, index, record.status.value) == raw
        assert native.same_saved_records([record], [raw], EventLogRecord, ScheduledEvent)
        assert json.dumps(state.row_copy(event).to_dict()) == json.dumps(event.to_dict())
        assert state.saved_row_copy(raw) == event
    assert log.all_encoded()
    assert _NativeEventLog.from_saved(log.items_bytes(0, 2000)).records_from(0) == records


def test_a_rule_cannot_record_a_minute_the_record_refuses() -> None:
    world = _world()
    event = _event("minus", 0)
    with pytest.raises(ValueError, match="realized_minute must be a non-negative integer"):
        native.state("events").record_row(world, event, -1, "applied")
    assert len(world.event_log) == 0


def test_a_rule_does_not_start_from_a_counter_it_could_overflow() -> None:
    world = _world()
    world.id_counters["event"] = 2**62
    assert native.state("events").allocate_sequence(world) == 2**62
    with pytest.raises(native.Unsupported):
        native.state("events").allocate_sequence(world)
    assert world.id_counters["event"] == 2**62 + 1


def test_a_sequence_whose_counter_would_not_fit_is_left_to_python() -> None:
    world = _world()
    event = _event("last", 2**63 - 1)
    with pytest.raises(native.Unsupported):
        native.state("events").schedule_row(world, event)
    assert len(world.event_queue) == 0
    assert "event" not in world.id_counters or world.id_counters["event"] == 0
