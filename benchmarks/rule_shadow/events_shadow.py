"""Compare the Rust EventQueue and EventLog with the Python ones, at every write and every save.

Imported by sitecustomize.py of this directory, under the same SHELTERSIM_RULE_SHADOW. It compares:

- every queue write: a _PythonEventQueue twin, by the order of pending() and by due(). New events,
  whoever scheduled them, join the twin at each comparison through its own schedule();
- every append: the bytes the log holds against json.dumps of record.to_dict(), and an EventRow
  read from the event, written, read back from those bytes and made an object, against the event;
- every save whose restore check read the live log: the old check, on a log built from the bytes,
  incrementally. It must accept each new record, and hold the same indexes.

It counts the records with no bytes from Rust and the saves that took the old path, with the reason:
a log that always falls back passes every hash.
"""

from __future__ import annotations

import atexit
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


def _python_json(record: Any) -> bytes:
    return json.dumps(
        record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _order(queue: Any) -> list[Any]:
    return [
        (event.event_id, event.due_minute, event.priority, event.sequence)
        for event in queue.pending()
    ]


def install(output: str) -> None:
    from sheltersim import native
    from sheltersim.core.checkpoint_cache import CheckpointHistoryCache
    from sheltersim.events import queue as events
    from sheltersim.events.models import EventLogRecord, ScheduledEvent

    if not native.available():
        return
    state = native.state("events")
    seen: Counter[str] = Counter()
    refused: Counter[str] = Counter()
    differences: list[str] = []
    # Queues by id, keeping the queue alive so that an id is not reused.
    twins: dict[int, tuple[Any, Any]] = {}

    def twin_of(queue: Any) -> Any:
        found = twins.get(id(queue))
        if found is None:
            found = twins[id(queue)] = (queue, events._PythonEventQueue(list(queue._events)))
        return found[1]

    def compare_queue(queue: Any, what: str) -> None:
        if queue._store is None:
            refused["queue: an order outside i64"] += 1
            return
        twin = twin_of(queue)
        known = {event.event_id for event in twin._events}
        for event in queue._events:
            if event.event_id not in known:
                twin.schedule(event)
                seen["queue.joined_the_twin"] += 1
        mine, theirs = _order(queue), _order(twin)
        seen["queue.compared"] += 1
        seen["queue.values"] += len(mine) * 4
        if mine != theirs:
            differences.append(f"queue after {what}: {mine[:3]} and {theirs[:3]}")
            return
        if mine:
            minute = mine[len(mine) // 2][1]
            if _order_of(queue.due(minute)) != _order_of(twin.due(minute)):
                differences.append(f"queue due({minute}) after {what}")

    def _order_of(items: tuple[Any, ...]) -> list[Any]:
        return [item.event_id for item in items]

    native_queue = events._NativeEventQueue
    schedule, remove, replace = native_queue.schedule, native_queue.remove, native_queue.replace

    def checked_schedule(self: Any, event: Any) -> None:
        schedule(self, event)
        compare_queue(self, "schedule")

    def checked_remove(self: Any, event_id: str) -> Any:
        event = remove(self, event_id)
        if self._store is not None:
            twin = twin_of(self)
            if twin.find(event_id) is not None:
                twin.remove(event_id)
        compare_queue(self, "remove")
        return event

    def checked_replace(self: Any, event_id: str, event: Any) -> None:
        replace(self, event_id, event)
        if self._store is not None:
            twin_of(self).replace(event_id, event)
        compare_queue(self, "replace")

    native_queue.schedule = checked_schedule  # type: ignore[method-assign]
    native_queue.remove = checked_remove  # type: ignore[method-assign]
    native_queue.replace = checked_replace  # type: ignore[method-assign]

    native_log = events._NativeEventLog
    append = native_log.append

    def checked_append(self: Any, record: Any) -> None:
        append(self, record)
        at = len(self) - 1
        seen["append.records"] += 1
        expected = _python_json(record)
        raw = self._store.items_bytes(at, at + 1)[0]
        seen["append.bytes"] += len(raw)
        if raw != expected:
            differences.append(f"record {record.event.event_id}: bytes")
        written = native.record_json(record, EventLogRecord, ScheduledEvent)
        if written is None:
            refused[f"append: no bytes from Rust, {record.event.event_type}"] += 1
            return
        seen["append.encoded"] += 1
        status = record.status.value
        row_raw = state.row_record_json(record.event, record.realized_minute, status, record.detail)
        if row_raw != expected:
            differences.append(f"record {record.event.event_id}: the row's bytes")
        copy = state.row_copy(record.event)
        if copy != record.event or json.dumps(copy.to_dict()) != json.dumps(record.event.to_dict()):
            differences.append(f"record {record.event.event_id}: the row's event")
        saved = state.saved_row_copy(raw)
        if saved != record.event:
            differences.append(f"record {record.event.event_id}: the row read from bytes")
        if not native.same_saved_records([record], [raw], EventLogRecord, ScheduledEvent):
            differences.append(f"record {record.event.event_id}: same_saved_records")
        seen["append.rows_compared"] += 1

    native_log.append = checked_append  # type: ignore[method-assign]

    documents = CheckpointHistoryCache.documents
    restored_logs: dict[int, tuple[Any, Any]] = {}

    def checked_documents(self: Any, world: Any, metrics: Any) -> Any:
        result = documents(self, world, metrics)
        log = world.event_log
        compare_queue(world.event_queue, "a save")
        if not isinstance(log, native_log):
            return result
        seen["saves"] += 1
        if self._live_log is None:
            refused["save: the old restore check, a record has no bytes from Rust"] += 1
            return result
        seen["saves_on_the_live_log"] += 1
        raws = self._event_raws
        kept = restored_logs.get(id(self))
        restored = kept[1] if kept is not None and kept[0] is log else native_log()
        start = len(restored)
        if start > len(raws):
            restored, start = native_log(), 0
        new_raws = raws[start:]
        restored.extend_saved(new_raws)
        restored_logs[id(self)] = (log, restored)
        records = log.records_from(start)
        seen["save.records_rechecked"] += len(records)
        seen["save.bytes_rechecked"] += sum(map(len, new_raws))
        if not native.same_saved_records(records, new_raws, EventLogRecord, ScheduledEvent) and (
            restored.records_from(start) != records
        ):
            differences.append(f"minute {world.minute_index}: the old restore check refused")
        indexes = (
            lambda item: len(item),
            lambda item: item.max_sequence,
            lambda item: item.event_types(),
        )
        if any(index(restored) != index(log) for index in indexes):
            differences.append(f"minute {world.minute_index}: the indexes")
        minute = world.minute_index - 1
        if [item.to_dict() for item in restored.at_minute(minute)] != [
            item.to_dict() for item in log.at_minute(minute)
        ]:
            differences.append(f"minute {minute}: at_minute")
        return result

    CheckpointHistoryCache.documents = checked_documents  # type: ignore[method-assign]

    def report() -> None:
        if seen or refused or differences:
            directory = Path(output) / "events"
            directory.mkdir(parents=True, exist_ok=True)
            document = {"seen": seen, "unsupported": refused, "differences": differences[:20]}
            (directory / f"{os.getpid()}.json").write_text(json.dumps(document), encoding="utf-8")

    atexit.register(report)
