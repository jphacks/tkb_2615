"""Deterministic future-event queue and separate realized event log."""

from __future__ import annotations

import heapq
import operator
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from sheltersim.events.models import EventLogRecord, ScheduledEvent


class DuplicateEventError(ValueError):
    """Raised when an event ID or sequence number is reused."""


@dataclass
class EventQueue:
    """Mutable future queue ordered by due minute, priority, and sequence."""

    _events: list[ScheduledEvent] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self.pending())

    def pending(self) -> tuple[ScheduledEvent, ...]:
        """Return a stable immutable view of pending events."""
        return tuple(
            sorted(
                self._events,
                key=lambda event: (event.due_minute, event.priority, event.sequence),
            )
        )

    def schedule(self, event: ScheduledEvent) -> None:
        """Add an event, rejecting ambiguous or repeated identities."""
        if any(item.event_id == event.event_id for item in self._events):
            raise DuplicateEventError(f"duplicate event ID {event.event_id!r}")
        if any(item.sequence == event.sequence for item in self._events):
            raise DuplicateEventError(f"duplicate event sequence {event.sequence}")
        self._events.append(event)

    def due(self, minute: int) -> tuple[ScheduledEvent, ...]:
        """Snapshot all events due through ``minute`` without removing them."""
        if minute < 0:
            raise ValueError("minute cannot be negative")
        return tuple(event for event in self.pending() if event.due_minute <= minute)

    def remove(self, event_id: str) -> ScheduledEvent:
        """Remove and return one event after its handler has finished."""
        for index, event in enumerate(self._events):
            if event.event_id == event_id:
                return self._events.pop(index)
        raise KeyError(event_id)

    def replace(self, event_id: str, event: ScheduledEvent) -> None:
        """Atomically replace one pending event while preserving its identity."""
        for index, current in enumerate(self._events):
            if current.event_id != event_id:
                continue
            if event.event_id != current.event_id or event.sequence != current.sequence:
                raise DuplicateEventError("replacement event must preserve ID and sequence")
            if any(
                item is not current
                and (item.event_id == event.event_id or item.sequence == event.sequence)
                for item in self._events
            ):
                raise DuplicateEventError("replacement event identity is already in use")
            self._events[index] = event
            return
        raise KeyError(event_id)

    def validate(self) -> None:
        identifiers = [event.event_id for event in self._events]
        sequences = [event.sequence for event in self._events]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("event queue contains duplicate event IDs")
        if len(sequences) != len(set(sequences)):
            raise ValueError("event queue contains duplicate event sequences")


@dataclass
class EventLog:
    """Append-only outcomes for events that were attempted at a due minute."""

    _records: list[EventLogRecord] = field(default_factory=list, repr=False)
    _ids: set[str] = field(default_factory=set, init=False, repr=False, compare=False)
    _by_id: dict[str, EventLogRecord] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _first_care_request_event: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _completed_support_kinds_by_agent: dict[str, set[str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _applied_facility_event_ids: dict[str, set[str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _sequences: set[int] = field(default_factory=set, init=False, repr=False, compare=False)
    _by_minute: dict[int, list[EventLogRecord]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Each event type's records with their append positions, for validators that
    # check a few types and need not read the whole log.
    _by_type: dict[str, list[tuple[int, EventLogRecord]]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Records by each provenance key that holds an object, such as an EffectProgram
    # marker, which most events lack.
    _by_provenance: dict[str, list[EventLogRecord]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # The records an audit (validate) checked and the time bounds it checked them
    # against; they are not checked again while they are still the leading records.
    _audited: list[EventLogRecord] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    _audit_bounds: tuple[int | None, int | None] = field(
        default=(None, None), init=False, repr=False, compare=False
    )
    _max_sequence: int = field(default=-1, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        records = list(self._records)
        self._records = []
        for record in records:
            self.append(record)

    def contains_id(self, event_id: str) -> bool:
        return event_id in self._ids

    def find(self, event_id: str) -> EventLogRecord | None:
        """Return one immutable realized event in constant time from the derived index."""
        return self._by_id.get(event_id)

    def has_prior_care_request(self, request_id: str, current_event_id: str) -> bool:
        """Detect request-ID reuse across full EventLog history, including expired state tails."""
        first = self._first_care_request_event.get(request_id)
        return first is not None and first != current_event_id

    def completed_support_kinds(self, agent_id: str) -> frozenset[str]:
        """Return support types with applied completion records for one Agent."""
        return frozenset(self._completed_support_kinds_by_agent.get(agent_id, ()))

    def applied_facility_event_ids(self, facility_id: str) -> frozenset[str]:
        """Return applied outage/recovery IDs associated with one facility."""
        return frozenset(self._applied_facility_event_ids.get(facility_id, ()))

    def contains_sequence(self, sequence: int) -> bool:
        return sequence in self._sequences

    @property
    def max_sequence(self) -> int:
        """Highest realized sequence, maintained as outcomes are appended."""
        return self._max_sequence

    def at_minute(self, minute: int) -> tuple[EventLogRecord, ...]:
        return tuple(self._by_minute.get(minute, ()))

    def event_types(self) -> frozenset[str]:
        """The event types of the realized records."""
        return frozenset(self._by_type)

    def with_provenance_object(self, key: str) -> list[EventLogRecord]:
        """The records whose event provenance holds an object at key, in append order."""
        return list(self._by_provenance.get(key, ()))

    def of_types(self, event_types: Collection[str]) -> list[EventLogRecord]:
        """The records whose event has one of event_types, in append order."""
        lists = [self._by_type[kind] for kind in event_types if kind in self._by_type]
        if len(lists) == 1:
            return [record for _, record in lists[0]]
        return [record for _, record in heapq.merge(*lists, key=operator.itemgetter(0))]

    @property
    def records(self) -> tuple[EventLogRecord, ...]:
        """Read-only view of realized outcomes in append order."""
        return tuple(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def recent(self, limit: int = 100) -> tuple[EventLogRecord, ...]:
        """Bounded observational tail; avoids copying the complete realized log."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid event tail limit")
        return tuple(self._records[-limit:])

    def recent_matching(
        self,
        event_types: frozenset[str],
        *,
        limit: int = 200,
        scan_limit: int = 5000,
    ) -> tuple[tuple[EventLogRecord, ...], bool]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(scan_limit) is not int
            or not 1 <= scan_limit <= 10000
        ):
            raise ValueError("invalid event tail search limit")
        result = []
        scanned = 0
        for record in reversed(self._records):
            if scanned == scan_limit:
                break
            scanned += 1
            if record.event.event_type in event_types and record.status.value == "applied":
                result.append(record)
                if len(result) == limit:
                    break
        return tuple(reversed(result)), scanned < len(self._records)

    def append(self, record: EventLogRecord) -> None:
        if record.event.event_id in self._ids:
            raise DuplicateEventError(f"event {record.event.event_id!r} already realized")
        if record.event.sequence in self._sequences:
            raise DuplicateEventError(f"event sequence {record.event.sequence} already realized")
        self._records.append(record)
        self._ids.add(record.event.event_id)
        self._by_id[record.event.event_id] = record
        if record.event.event_type == "care_support_transition":
            request_id = record.event.payload.get("request_id")
            if isinstance(request_id, str):
                self._first_care_request_event.setdefault(request_id, record.event.event_id)
            agent_id = record.event.payload.get("agent_id")
            support_kind = record.event.payload.get("support_kind")
            if (
                record.status.value == "applied"
                and record.event.payload.get("status") == "completed"
                and isinstance(agent_id, str)
                and isinstance(support_kind, str)
            ):
                self._completed_support_kinds_by_agent.setdefault(agent_id, set()).add(support_kind)
        if record.status.value == "applied" and record.event.event_type in {
            "facility_outage",
            "facility_recovery",
        }:
            facility_id = record.event.payload.get("facility_id")
            if isinstance(facility_id, str):
                self._applied_facility_event_ids.setdefault(facility_id, set()).add(
                    record.event.event_id
                )
        self._sequences.add(record.event.sequence)
        self._max_sequence = max(self._max_sequence, record.event.sequence)
        self._by_minute.setdefault(record.realized_minute, []).append(record)
        self._by_type.setdefault(record.event.event_type, []).append(
            (len(self._records) - 1, record)
        )
        for key, value in record.event.provenance.items():
            if isinstance(value, Mapping):
                self._by_provenance.setdefault(key, []).append(record)

    def validate(
        self, *, minute_index: int | None = None, duration_minutes: int | None = None
    ) -> None:
        """Audit unique IDs and sequences, and realization times before minute_index
        and within duration_minutes when given.

        Records are frozen and only appended. Records an earlier audit checked, at
        an earlier or the same minute, are not checked again while they are still the
        leading records, the same objects; a new record must be the one its ID and
        sequence indexes hold. Anything else, such as a replaced record, audits the
        whole log again.
        """
        audited = self._audited
        records = self._records
        audited_minute, audited_duration = self._audit_bounds
        start = len(audited)
        if (
            start > len(records)
            or any(map(operator.is_not, audited, records))
            or (
                minute_index is not None
                and (audited_minute is None or audited_minute > minute_index)
            )
            or (duration_minutes is not None and audited_duration != duration_minutes)
        ):
            start = 0
        if start == 0:
            identifiers = [record.event.event_id for record in records]
            sequences = [record.event.sequence for record in records]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("event log contains duplicate event IDs")
            if len(sequences) != len(set(sequences)):
                raise ValueError("event log contains duplicate event sequences")
        if not len(self._ids) == len(self._by_id) == len(self._sequences) == len(records):
            raise ValueError("event log indexes do not match its records")
        for record in records[start:]:
            if self._by_id.get(record.event.event_id) is not record:
                raise ValueError("event log contains duplicate event IDs")
            if record.event.sequence not in self._sequences:
                raise ValueError("event log contains duplicate event sequences")
            if minute_index is not None and record.realized_minute > minute_index:
                raise ValueError("event log cannot contain future realization times")
            if duration_minutes is not None and record.realized_minute >= duration_minutes:
                raise ValueError("event log is outside the scenario duration")
        self._audited = list(records)
        self._audit_bounds = (minute_index, duration_minutes)


class EventsById(Mapping[str, ScheduledEvent]):
    """Pending and realized events by ID, read through the EventLog index.

    Like a dict built from the pending events followed by every realized one, a
    realized event wins, but nothing is copied from the log.
    """

    def __init__(self, log: EventLog, pending: Iterable[ScheduledEvent]) -> None:
        self._log = log
        self._pending = {event.event_id: event for event in pending}

    def __getitem__(self, event_id: str) -> ScheduledEvent:
        record = self._log.find(event_id) if isinstance(event_id, str) else None
        return record.event if record is not None else self._pending[event_id]

    def __iter__(self) -> Iterator[str]:
        yield from (event_id for event_id in self._pending if not self._log.contains_id(event_id))
        yield from (record.event.event_id for record in self._log._records)

    def __len__(self) -> int:
        return len(self._log) + sum(
            not self._log.contains_id(event_id) for event_id in self._pending
        )
