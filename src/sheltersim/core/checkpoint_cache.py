"""Reuse one writer's conversions of append-only history between Checkpoint saves.

A Checkpoint v1 holds the whole WorldState and KPI history, and its hashes and
archive chunks cover the canonical JSON of all of it. Most of that is history that
only grows: realized EventLog records, KPI and resource samples, and model_state
histories such as the body-state window. A writer that saves every 30 minutes
keeps, for each item it saved before, the JSON document, its canonical bytes and
(for EventLog records) the record that the save's restore check made from that
document. The next save converts, encodes and restores only the new items, and
still produces the bytes, hashes and chunks of a full capture.

Items are matched by identity, and the cache holds a reference to each one so an
id is never reused while it is cached. EventLog records and KPI snapshots are
frozen. The other histories are plain dicts that are appended and never edited;
such an entry is reused only while it still equals its saved document.
"""

from __future__ import annotations

import hashlib
import json
import operator
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, fields
from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.events.models import EventLogRecord, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder


def canonical_bytes(value: Any) -> bytes:
    """The UTF-8 canonical JSON that Checkpoint hashes and archive chunks use."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class CanonicalEncoder:
    """canonical_bytes() of one captured document, splicing cached history bytes.

    Spliced lists are encoded from their items' cached bytes. The objects that
    contain them are encoded key by key; everything else is encoded once by json and
    remembered. The document must not change while the encoder is in use.
    """

    def __init__(self, chunk_keys: dict[tuple[int, int, int], Any] | None = None) -> None:
        # Archive chunk hashes by their first and last item bytes and length, shared by
        # the captures of one history cache: a writer reuses the item bytes of history
        # it saved before, so the same objects mean the same chunk.
        self.chunk_keys = {} if chunk_keys is None else chunk_keys
        self._lists: dict[int, tuple[list[Any], list[bytes]]] = {}
        self._containers: dict[int, dict[str, Any]] = {}
        self._encoded: dict[int, tuple[Any, bytes]] = {}

    def splice(self, value: list[Any], items: list[bytes], *containers: dict[str, Any]) -> None:
        """Encode value from items; containers are the objects on the path to it."""
        self._lists[id(value)] = (value, items)
        for container in containers:
            self._containers[id(container)] = container

    def remember(self, value: Any, raw: bytes, *containers: dict[str, Any]) -> None:
        """Use raw, the canonical_bytes() of value, for value inside containers."""
        self._encoded[id(value)] = (value, raw)
        for container in containers:
            self._containers[id(container)] = container

    def chunk_key(self, items: list[bytes], start: int, count: int) -> str | None:
        """The archive key saved before for items[start:start + count], if any."""
        kept = self.chunk_keys.get((id(items[start]), id(items[start + count - 1]), count))
        if kept is None or kept[0] is not items[start] or kept[1] is not items[start + count - 1]:
            return None
        return kept[2]

    def remember_chunk(self, items: list[bytes], start: int, count: int, key: str) -> None:
        first, last = items[start], items[start + count - 1]
        self.chunk_keys[(id(first), id(last), count)] = (first, last, key)

    def spliced_lists(self) -> list[list[Any]]:
        """The spliced lists; their item documents are never changed."""
        return [value for value, _ in self._lists.values()]

    def list_items(self, value: Any) -> list[bytes] | None:
        """The cached item bytes of a spliced list, or None for any other value."""
        entry = self._lists.get(id(value))
        return entry[1] if entry is not None and entry[0] is value else None

    def _spliced(self, value: Any) -> bool:
        if self.list_items(value) is not None:
            return True
        container = self._containers.get(id(value))
        return container is not None and container is value

    def _is_container(self, value: Any) -> bool:
        # A new wrapper around a container (a Checkpoint body) is a container too.
        return (
            isinstance(value, dict)
            and all(isinstance(key, str) for key in value)
            and (self._spliced(value) or any(self._spliced(item) for item in value.values()))
        )

    def _leaf(self, value: Any) -> bytes:
        if not isinstance(value, (dict, list)):
            return canonical_bytes(value)
        entry = self._encoded.get(id(value))
        if entry is not None and entry[0] is value:
            return entry[1]
        raw = canonical_bytes(value)
        self._encoded[id(value)] = (value, raw)
        return raw

    def parts(self, value: Any) -> Iterator[bytes]:
        """Fragments whose concatenation is canonical_bytes(value)."""
        items = self.list_items(value)
        if items is not None:
            yield b"[" + b",".join(items) + b"]"
        elif self._is_container(value):
            yield b"{"
            for index, key in enumerate(sorted(value)):
                yield (b"," if index else b"") + canonical_bytes(key) + b":"
                yield from self.parts(value[key])
            yield b"}"
        else:
            yield self._leaf(value)

    def encode(self, value: Any) -> bytes:
        return b"".join(self.parts(value))

    def size(self, value: Any) -> int:
        """len(canonical_bytes(value)) without joining spliced lists."""
        items = self.list_items(value)
        if items is not None:
            return 2 + sum(map(len, items)) + max(len(items) - 1, 0)
        if self._is_container(value):
            fields = sum(
                len(canonical_bytes(key)) + 1 + self.size(item) for key, item in value.items()
            )
            return 2 + max(len(value) - 1, 0) + fields
        return len(self._leaf(value))

    def digest(self, value: Any) -> str:
        hasher = hashlib.sha256()
        for part in self.parts(value):
            hasher.update(part)
        return hasher.hexdigest()


class _SealedItems:
    """Documents and canonical bytes of the items of one growing list."""

    def __init__(self, convert: Callable[[Any], Any], *, still_equal: bool = False) -> None:
        self._convert = convert
        # Plain-dict items are reused only while they still equal their document.
        self._still_equal = still_equal
        self.items: list[Any] = []
        self.documents: list[Any] = []
        self.raws: list[bytes] = []
        # What a restore check made from the leading documents, if anything.
        self.restored: list[Any] = []

    def update(self, current: Sequence[Any]) -> None:
        """Reuse the cached items still in current and convert the others.

        The lists only drop old items from the front and append new ones. Anything
        else, such as a replaced world or an edited entry, converts them all again.
        """
        start = self._position(current[0]) if current else len(self.items)
        kept = len(self.items) - start
        if not (
            kept <= len(current)
            and all(map(operator.is_, self.items[start:], current[:kept]))
            and (
                not self._still_equal
                or all(map(operator.eq, current[:kept], self.documents[start:]))
            )
        ):
            start, kept = len(self.items), 0
        documents = self.documents[start:]
        raws = self.raws[start:]
        for item in current[kept:]:
            document = self._convert(item)
            documents.append(document)
            raws.append(canonical_bytes(document))
        self.restored = self.restored[start : start + kept]
        self.items, self.documents, self.raws = list(current), documents, raws

    def _position(self, item: Any) -> int:
        # Only the items dropped since the last save are skipped.
        for index, cached in enumerate(self.items):
            if cached is item:
                return index
        return len(self.items)


def _world_json(value: Any) -> Any:
    # The conversion WorldState.to_dict() applies to every value it serializes.
    return json.loads(WorldState._canonical_text(value))


def _event_document(record: EventLogRecord) -> Any:
    return _world_json(record.to_dict())


_RECORD_FIELDS = {item.name for item in fields(EventLogRecord)}
_EVENT_FIELDS = {item.name for item in fields(ScheduledEvent)}


# Append-only model_state histories (they may drop their oldest entries).
_MODEL_STATE_HISTORIES = (
    ("body_state_v1", "history"),
    ("shared_operations_v1", "history"),
    ("resource_ledger_v1", "audit"),
    ("resource_service_v1", "request_history"),
    ("resource_service_v1", "queue_stall_history"),
)


def _containers(value: Any, path: tuple[str, ...]) -> list[dict[str, Any]] | None:
    """The objects from value down to the list at path, if there is one."""
    containers = []
    for key in path:
        if not isinstance(value, dict):
            return None
        containers.append(value)
        value = value.get(key)
    return containers if isinstance(value, list) else None


def _with_empty(value: dict[str, Any], paths: list[tuple[str, ...]]) -> dict[str, Any]:
    """A copy of value, sharing everything except an empty list at each path."""
    copy = dict(value)
    for key in {path[0] for path in paths}:
        rests = [path[1:] for path in paths if path[0] == key]
        copy[key] = [] if rests == [()] else _with_empty(value[key], rests)
    return copy


class CheckpointHistoryCache:
    """History conversions one writer reuses across its Checkpoint captures.

    Owned by the code that advances one BranchRuntime and saves it; never share it
    between runtimes or threads.
    """

    def __init__(self) -> None:
        self._events = _SealedItems(_event_document)
        self._model_histories = {
            path: _SealedItems(_world_json, still_equal=True) for path in _MODEL_STATE_HISTORIES
        }
        self._snapshots = _SealedItems(asdict)
        self._resource_snapshots = _SealedItems(dict, still_equal=True)
        self._chunk_keys: dict[tuple[int, int, int], Any] = {}

    def documents(
        self, world: WorldState, metrics: MetricsRecorder | None
    ) -> tuple[dict[str, Any], dict[str, Any], CanonicalEncoder]:
        """world.to_dict() and metrics.to_dict() of a validated world, and their encoder."""
        encoder = CanonicalEncoder(self._chunk_keys)
        histories = [
            path for path in _MODEL_STATE_HISTORIES if _containers(world.model_state, path)
        ]
        data = world._canonical_data(include_event_log=False)
        data["model_state"] = _with_empty(world.model_state, histories)
        world_document = json.loads(WorldState._canonical_text(data))
        self._events.update(world.event_log.records)
        world_document["event_log"] = list(self._events.documents)
        encoder.splice(world_document["event_log"], list(self._events.raws), world_document)
        for path in histories:
            cache = self._model_histories[path]
            cache.update(_containers(world.model_state, path)[-1][path[-1]])
            containers = [world_document, *_containers(world_document["model_state"], path)]
            containers[-1][path[-1]] = list(cache.documents)
            encoder.splice(containers[-1][path[-1]], list(cache.raws), *containers)
        if metrics is None:
            metric_document: dict[str, Any] = {"schema_version": 3, "snapshots": []}
        else:
            self._snapshots.update(metrics.snapshots)
            resource_snapshots = None
            if metrics.resource_snapshots is not None:
                self._resource_snapshots.update(metrics.resource_snapshots)
                resource_snapshots = list(self._resource_snapshots.documents)
            metric_document = metrics._document(list(self._snapshots.documents), resource_snapshots)
            encoder.splice(
                metric_document["snapshots"], list(self._snapshots.raws), metric_document
            )
            if resource_snapshots is not None:
                encoder.splice(resource_snapshots, list(self._resource_snapshots.raws))
        return world_document, metric_document, encoder

    def seed_events(self, records: Sequence[EventLogRecord], documents: Sequence[Any]) -> bool:
        """Start from records a Checkpoint restore made from its event_log documents.

        records[i] must be the record restored from documents[i], and the documents
        must never change. A restored record converts back to its document when the
        document has exactly the record's fields, so the first capture then reuses
        both instead of converting and restoring the whole EventLog. Returns whether
        the documents were used.
        """
        if len(records) != len(documents) or any(
            set(document) != _RECORD_FIELDS or set(document["event"]) != _EVENT_FIELDS
            for document in documents
        ):
            return False
        events = self._events
        events.items, events.documents = list(records), list(documents)
        events.raws = [canonical_bytes(document) for document in documents]
        events.restored = list(records)
        return True

    def restored_events(self) -> list[EventLogRecord]:
        """Records restored from the leading event_log documents of the last documents()."""
        return list(self._events.restored)

    def remember_restored(self, records: Sequence[EventLogRecord]) -> None:
        """Keep the records a restore check made from the last documents()."""
        if len(records) != len(self._events.items):
            raise ValueError("restored EventLog does not match the captured documents")
        self._events.restored = list(records)
