"""Reuse one writer's conversions of append-only history between Checkpoint saves.

A Checkpoint holds the whole WorldState and KPI history, and its hashes and archive
chunks cover the canonical JSON of all of it. Most of that is history that only
grows: realized EventLog records, KPI and resource samples, and model_state histories
such as the body-state window. A writer that saves every 30 minutes keeps, for each
item it saved before, the JSON document, its canonical bytes and (for EventLog
records) the record that the save's restore check made from that document. The next
save converts, encodes and restores only the new items, and still produces the bytes,
hashes and chunks of a full capture.

The hashes of a Checkpoint v2 are hash trees (docs/rust-port.md, B3). A list on a
history path is hashed as its count and the SHA-256 of each run of TREE_CHUNK_ITEMS
items, which are the archive's chunk keys, so a save hashes only new chunks. Any other
value is a leaf, the SHA-256 of its canonical JSON. An object on the path to a history
is hashed as the hashes of its fields. The rule reads nothing but the document, so a
reader without any of these caches gets the same hashes.

Items are matched by identity, and the cache holds a reference to each one so an
id is never reused while it is cached. EventLog records and KPI snapshots are
frozen. The other histories are plain dicts that are appended and never edited;
such an entry is reused only while it still equals its saved document. With the rust
kernels that comparison also tells 1 from True and a tuple from a list, and its result
also tells the save's validation which entries it need not read (core/save_validation.py).
"""

from __future__ import annotations

import hashlib
import json
import operator
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, fields
from typing import Any

from sheltersim import native
from sheltersim.contracts.policies import model_signatures
from sheltersim.core.save_validation import SaveValidationMemo
from sheltersim.core.saved_items import SavedItems, json_default
from sheltersim.core.state_mirror import owned_row_bytes
from sheltersim.core.world import WorldState
from sheltersim.events.models import EventLogRecord, ScheduledEvent
from sheltersim.events.queue import EventLog, keeps_saved_bytes
from sheltersim.experiments.metrics import MetricsRecorder


def canonical_bytes(value: Any) -> bytes:
    """The UTF-8 canonical JSON that Checkpoint hashes and archive chunks use."""
    encoded = native.canonical_json(value)
    if encoded is not None:
        return encoded
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=json_default,
    ).encode()


# Items of a spliced list joined at a time when only a hash of the bytes is needed.
_HASH_BATCH = 512

# How many items each hash of a history list covers (a Checkpoint v2 hash rule), and
# the archive's default for its chunks, so that the same bytes are hashed once.
TREE_CHUNK_ITEMS = 128

# The lists a Checkpoint v2 hash splits into chunks (None), by their path. Part of the
# hash rule: changing it needs a new Checkpoint version.
_WORLD_TREE: dict[str, Any] = {
    "event_log": None,
    "model_state": {
        "body_state_v1": {"history": None},
        "shared_operations_v1": {"history": None},
        "resource_ledger_v1": {"audit": None},
        "resource_service_v1": {"request_history": None, "queue_stall_history": None},
    },
}
_METRICS_TREE: dict[str, Any] = {"snapshots": None, "resource_snapshots": None}
_BODY_TREE: dict[str, Any] = {"world_state": _WORLD_TREE, "metrics_state": _METRICS_TREE}

# Inner nodes start with NUL, which canonical JSON (the bytes of a leaf) never does.
_TREE_LIST = b"\0sheltersim-tree-list\0"
_TREE_OBJECT = b"\0sheltersim-tree-object\0"


class CanonicalEncoder:
    """canonical_bytes() of one captured document, splicing cached history bytes.

    Spliced lists are encoded from their items' cached bytes. The objects that
    contain them are encoded key by key; everything else is encoded once by json and
    remembered. The document must not change while the encoder is in use.
    """

    def __init__(
        self,
        chunk_keys: dict[tuple[int, int, int], Any] | None = None,
        pack_locations: dict[str, tuple[str, int, int]] | None = None,
        run_inputs: dict[Any, Any] | None = None,
    ) -> None:
        # Archive chunk hashes by their first and last item bytes and length, shared by
        # the captures of one history cache: a writer reuses the item bytes of history
        # it saved before, so the same objects mean the same chunk.
        self.chunk_keys = {} if chunk_keys is None else chunk_keys
        # Where the archive saves of the same writer put each chunk in a pack, so a
        # chunk that comes back in a later save is not written again.
        self.pack_locations = {} if pack_locations is None else pack_locations
        # What the store read of a Run's immutable inputs, for the same writer's next save.
        self.run_inputs = {} if run_inputs is None else run_inputs
        self._lists: dict[int, tuple[list[Any], list[bytes]]] = {}
        self._containers: dict[int, dict[str, Any]] = {}
        self._encoded: dict[int, tuple[Any, bytes]] = {}
        self._tree_nodes: dict[int, tuple[Any, str]] = {}
        # The spliced lists whose items may change in place (entity rows), by id.
        self._rewritten: set[int] = set()

    def splice(self, value: list[Any], items: list[bytes], *containers: dict[str, Any]) -> None:
        """Encode value from items; containers are the objects on the path to it."""
        self._lists[id(value)] = (value, items)
        for container in containers:
            self._containers[id(container)] = container

    def splice_rows(self, value: Any, items: list[bytes]) -> None:
        """Encode value from items, which are not append-only: an equal first and last item
        do not mean an equal chunk, so chunk_key() says nothing about them."""
        self.splice(value, items)
        self._rewritten.add(id(value))

    def append_only(self, value: Any) -> bool:
        """Whether chunks of this spliced list are known by their first and last items."""
        return id(value) not in self._rewritten

    def contain(self, *containers: dict[str, Any]) -> None:
        """Encode containers key by key, as objects on the path to a spliced value."""
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
            items = self.list_items(value)
            if items is None:
                return canonical_bytes(value)
            entry = self._encoded.get(id(value))
            if entry is not None and entry[0] is value:
                return entry[1]
            raw = b"[" + b",".join(items) + b"]"
            self._encoded[id(value)] = (value, raw)
            return raw
        entry = self._encoded.get(id(value))
        if entry is not None and entry[0] is value:
            return entry[1]
        raw = canonical_bytes(value)
        self._encoded[id(value)] = (value, raw)
        return raw

    def parts(self, value: Any, *, joined: bool = True) -> Iterator[bytes]:
        """Fragments whose concatenation is canonical_bytes(value).

        Without joined, a spliced list comes in batches of items, so a hash need not
        build one copy of a whole history (about 45 MB for a day's EventLog).
        """
        items = self.list_items(value)
        if items is not None:
            if joined:
                yield (
                    self._leaf(value)
                    if id(value) in self._rewritten
                    else (b"[" + b",".join(items) + b"]")
                )
            else:
                yield b"["
                for start in range(0, len(items), _HASH_BATCH):
                    yield (b"," if start else b"") + b",".join(items[start : start + _HASH_BATCH])
                yield b"]"
        elif self._is_container(value):
            yield b"{"
            for index, key in enumerate(sorted(value)):
                yield (b"," if index else b"") + canonical_bytes(key) + b":"
                yield from self.parts(value[key], joined=joined)
            yield b"}"
        else:
            yield self._leaf(value)

    def encode(self, value: Any) -> bytes:
        return b"".join(self.parts(value))

    def exceeds(self, value: Any, limit: int) -> bool:
        """Whether len(canonical_bytes(value)) > limit, without reading all of a spliced list."""
        return self._size_up_to(value, limit) > limit

    def _size_up_to(self, value: Any, limit: int) -> int:
        """The size of value, or any size above limit once it is known to exceed it."""
        items = self.list_items(value)
        if items is not None:
            size = 2 + max(len(items) - 1, 0)
            for item in items:
                size += len(item)
                if size > limit:
                    break
            return size
        if not self._is_container(value):
            return len(self._leaf(value))
        size = 2 + max(len(value) - 1, 0)
        for key, item in value.items():
            size += len(canonical_bytes(key)) + 1 + self._size_up_to(item, limit - size)
            if size > limit:
                break
        return size

    def digest(self, value: Any) -> str:
        """The SHA-256 of canonical_bytes(value): the hash rule of a Checkpoint v1."""
        hasher = hashlib.sha256()
        for part in self.parts(value, joined=False):
            hasher.update(part)
        return hasher.hexdigest()

    def world_tree_digest(self, world_document: dict[str, Any]) -> str:
        """The state_hash of a Checkpoint v2 holding world_document."""
        return self._tree_node(world_document, _WORLD_TREE)

    def body_tree_digest(self, body: dict[str, Any]) -> str:
        """The checkpoint_hash of a Checkpoint v2 with this body."""
        return self._tree_node(body, _BODY_TREE)

    def _tree_node(self, value: Any, shape: dict[str, Any] | None) -> str:
        kept = self._tree_nodes.get(id(value))
        if kept is not None and kept[0] is value:
            return kept[1]
        if shape is None and isinstance(value, (list, SavedItems)):
            node = self._tree_list(value)
        elif (
            shape is not None
            and isinstance(value, dict)
            and all(isinstance(key, str) for key in value)
        ):
            hashes = {
                key: self._tree_node(item, shape[key]) if key in shape else self._tree_leaf(item)
                for key, item in value.items()
            }
            node = hashlib.sha256(_TREE_OBJECT + canonical_bytes(hashes)).hexdigest()
        else:
            node = self._tree_leaf(value)
        self._tree_nodes[id(value)] = (value, node)
        return node

    def _tree_leaf(self, value: Any) -> str:
        return hashlib.sha256(self._leaf(value)).hexdigest()

    def _tree_list(self, value: Sequence[Any]) -> str:
        items = value.raws if isinstance(value, SavedItems) else self.list_items(value)
        count = len(value)
        # The full chunks a history had at its last save, which only grows or drops from the front.
        known = [] if items is None else self._known_chunks(items)
        chunks = [
            (start, min(TREE_CHUNK_ITEMS, count - start))
            for start in range(len(known) * TREE_CHUNK_ITEMS, count, TREE_CHUNK_ITEMS)
        ]
        if items is None:
            keys = [
                hashlib.sha256(canonical_bytes(value[start : start + size])).hexdigest()
                for start, size in chunks
            ]
        else:
            keys = [self.chunk_key(items, start, size) for start, size in chunks]
            missing = [index for index, key in enumerate(keys) if key is None]
            if missing:
                wanted = [chunks[index] for index in missing]
                hashes = native.chunk_hashes(items, wanted) or [
                    hashlib.sha256(b"[" + b",".join(items[start : start + size]) + b"]").hexdigest()
                    for start, size in wanted
                ]
                for index, (start, size), key in zip(missing, wanted, hashes, strict=True):
                    keys[index] = key
                    self.remember_chunk(items, start, size, key)
            keys = known + keys
            self._keep_known_chunks(items, keys)
        return hashlib.sha256(_TREE_LIST + canonical_bytes([count, keys])).hexdigest()

    def leading_chunks(self, value: Any, items: list[bytes], chunk_records: int) -> list[str]:
        """The keys _known_chunks() knows of a list the archive splits in the same chunks."""
        if chunk_records != TREE_CHUNK_ITEMS or not self.append_only(value):
            return []
        return self._known_chunks(items)

    def _known_chunks(self, items: list[bytes]) -> list[str]:
        """The keys of the leading full chunks of items that are chunks of the same list before.

        As for chunk_key(), the same first and last item objects mean the same chunk.
        """
        if not items:
            return []
        kept = self.chunk_keys.get(("leading", id(items[0])))
        if kept is None or kept[0] is not items[0]:
            return []
        keys = kept[2]
        last = len(keys) * TREE_CHUNK_ITEMS - 1
        return list(keys) if last < len(items) and items[last] is kept[1] else []

    def _keep_known_chunks(self, items: list[bytes], keys: list[str]) -> None:
        full = len(items) // TREE_CHUNK_ITEMS
        if full:
            self.chunk_keys[("leading", id(items[0]))] = (
                items[0],
                items[full * TREE_CHUNK_ITEMS - 1],
                keys[:full],
            )


class _SealedItems:
    """Documents and canonical bytes of the items of one growing list."""

    def __init__(self, convert: Callable[[Any], Any], *, still_equal: bool = False) -> None:
        self._convert = convert
        # Plain-dict items are reused only while they still equal their document.
        self._still_equal = still_equal
        self.items: list[Any] = []
        self.documents: list[Any] = []
        self.raws: list[bytes] = []
        # What a restore check made from the leading documents, if anything, and how
        # many of them are known to equal their items.
        self.restored: list[Any] = []
        self.restored_equal = 0
        # How many leading items the last update() found equal to their documents.
        self.known_equal = 0
        # The result of compare() for the update() of the same list that follows it.
        self._compared: tuple[Sequence[Any], int, int] | None = None
        # With the kernels, what an unchanged plain-dict item is known by, without reading its
        # values. Anything it can't say is compared with the documents as before.
        self._pins = native.history_pins() if still_equal else None
        self._pins_stale = False

    def compare(self, current: Sequence[Any]) -> int:
        """How many leading items of current are unchanged; update(current) reuses this."""
        self._compared = (current, *self._unchanged(current))
        return self._compared[2]

    def _unchanged(self, current: Sequence[Any]) -> tuple[int, int]:
        """The position in the cache of current's first item, and how many are unchanged.

        The lists only drop old items from the front and append new ones. Anything
        else, such as a replaced world or an edited entry, finds none unchanged.
        """
        start = self._position(current[0]) if current else len(self.items)
        kept = len(self.items) - start
        if (
            kept <= len(current)
            and all(map(operator.is_, self.items[start:], current[:kept]))
            and (not self._still_equal or self._same_as_documents(current, start, kept))
        ):
            return start, kept
        return len(self.items), 0

    def _same_as_documents(self, current: Sequence[Any], start: int, count: int) -> bool:
        pins = self._pins
        if pins is not None and pins.unchanged(current, start, count):
            return True
        same = _still_documents(current, self.documents, start, count)
        # Equal in another order, say: from now on known by what it holds now.
        self._pins_stale = same
        return same

    def update(self, current: Sequence[Any]) -> None:
        """Reuse the cached items still in current and convert the others."""
        compared, self._compared = self._compared, None
        if compared is not None and compared[0] is current:
            start, kept = compared[1:]
        else:
            start, kept = self._unchanged(current)
        # These lists are this cache's own: what it hands out is a copy (documents()). Each copy
        # of a list touches every item, scattered over memory.
        documents, raws = self.documents, self.raws
        del documents[:start], documents[kept:], raws[:start], raws[kept:]
        for item in current[kept:]:
            # What the kernel encoded reads back as the same bytes (see _encoded_document).
            raw = native.canonical_json(item) if self._convert is _world_json else None
            if raw is not None:
                documents.append(json.loads(raw))
                raws.append(raw)
                continue
            document = self._convert(item)
            documents.append(document)
            raws.append(canonical_bytes(document))
        del self.restored[:start], self.restored[kept:]
        self.restored_equal = max(0, min(self.restored_equal - start, kept))
        self.known_equal = kept if self._still_equal else 0
        self.items = list(current)
        pins = self._pins
        if pins is not None:
            if not kept or self._pins_stale:
                pins.clear()
                kept = 0
            else:
                pins.drop_front(start)
            self._pins_stale = False
            if isinstance(current, list):
                pins.extend(current, documents, kept)

    def _position(self, item: Any) -> int:
        # Only the items dropped since the last save are skipped.
        for index, cached in enumerate(self.items):
            if cached is item:
                return index
        return len(self.items)


def _still_documents(current: Sequence[Any], documents: list[Any], start: int, count: int) -> bool:
    """Whether current[:count] still are documents[start:start + count]."""
    same = native.same_json_items(current, documents, start, count)
    if same is None:
        return all(map(operator.eq, current[:count], documents[start : start + count]))
    return same


def _world_json(value: Any) -> Any:
    # The conversion WorldState.to_dict() applies to every value it serializes. A value
    # the kernel leaves to json (a frozen record's payload) takes the json path.
    encoded = native.canonical_json(value)
    return json.loads(WorldState._canonical_text(value) if encoded is None else encoded)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


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
        self._pack_locations: dict[str, tuple[str, int, int]] = {}
        self._run_inputs: dict[Any, Any] = {}
        # The EventLog of the last restore check (see restored_log()).
        self._log: EventLog | None = None
        # For a Rust EventLog, which keeps each record's bytes, records are followed
        # by position: the item bytes of the last documents() (the same objects
        # while the log only grows), and how many leading restored records are known
        # to equal the captured ones.
        self._event_raws: list[bytes] | None = None
        # The world's own EventLog, while it is what restoring its records gives.
        self._live_log: EventLog | None = None
        self._raws_equal = 0
        self._validation: SaveValidationMemo | None = None
        # The model signatures of the last configs, by their canonical JSON.
        self._signatures: tuple[bytes, dict[str, str]] | None = None
        # The canonical JSON and document of each field of the last documents(), by its path.
        # The same bytes read back as the same document, which nothing changes, so a field that
        # did not change is not parsed again.
        self._fields: dict[tuple[str, ...], tuple[bytes, Any]] = {}
        # What the last documents() took from Rust rows: each entity list's item bytes,
        # and the names of the owned namespaces and world fields.
        self._row_items: dict[str, list[bytes]] = {}
        self._owned_rows: frozenset[str] = frozenset()
        # The entities the last restore check showed equal, by field, then by id(raw).
        self._restored_rows: dict[str, dict[int, tuple[bytes, Any, str]]] = {}

    def model_signatures(self, configs: dict[str, Any]) -> dict[str, str]:
        """contracts.policies.model_signatures(configs); the same configs compute it once.

        It reads only the configs and the installed catalog, which do not change while a
        writer runs. Do not change the returned dict.
        """
        key = canonical_bytes(configs)
        if self._signatures is None or self._signatures[0] != key:
            self._signatures = (key, model_signatures(configs))
        return self._signatures[1]

    def validation_memo(self, world: WorldState) -> SaveValidationMemo | None:
        """The memo for validating world before this save, without the rust kernels None.

        Compares the histories with the documents of the last save, once for both the
        validation and the documents() that follows it.
        """
        if not native.available():
            return None
        if self._validation is None:
            self._validation = SaveValidationMemo()
        unchanged = {}
        for path, cache in self._model_histories.items():
            containers = _containers(world.model_state, path)
            if containers:
                unchanged[path] = cache.compare(containers[-1][path[-1]])
        self._validation.begin(world, unchanged)
        return self._validation

    def forget_validation(self) -> None:
        """Drop what passed validations proved, after a failed capture."""
        self._validation = None
        self._restored_rows = {}

    def documents(
        self, world: WorldState, metrics: MetricsRecorder | None
    ) -> tuple[dict[str, Any], dict[str, Any], CanonicalEncoder]:
        """world.to_dict() and metrics.to_dict() of a validated world, and their encoder."""
        encoder = CanonicalEncoder(self._chunk_keys, self._pack_locations, self._run_inputs)
        histories = [
            path for path in _MODEL_STATE_HISTORIES if _containers(world.model_state, path)
        ]
        for cache in self._model_histories.values():
            cache.known_equal = 0
        entity_rows, namespace_rows, field_rows = owned_row_bytes(world)
        self._row_items = entity_rows
        # A namespace on the hash tree's path has fields hashed apart: it stays on the Python path.
        namespace_rows = {
            name: raw
            for name, raw in namespace_rows.items()
            if name not in _WORLD_TREE["model_state"]
        }
        self._owned_rows = frozenset(namespace_rows) | frozenset(field_rows)
        data = world._canonical_data(include_event_log=False, without_entities=bool(entity_rows))
        data["model_state"] = _with_empty(world.model_state, histories)
        world_document = self._encoded_document(
            data, _WORLD_TREE, encoder, (), {(): field_rows, ("model_state",): namespace_rows}
        )
        for name, raws in entity_rows.items():
            world_document[name] = SavedItems(raws)
            encoder.splice_rows(world_document[name], raws)
        log = world.event_log
        if keeps_saved_bytes(log):
            raws = self._log_raws(log)
            self._event_raws = raws
            self._live_log = log if log.all_encoded() else None
            world_document["event_log"] = SavedItems(raws)
            encoder.splice(world_document["event_log"], raws, world_document)
        else:
            self._event_raws = None
            self._live_log = None
            self._events.update(log.records)
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

    def _document_field(
        self, path: tuple[str, ...], raw: bytes, document: dict[str, Any], key: str, encoder: Any
    ) -> None:
        kept = self._fields.get(path)
        if kept is not None and kept[0] == raw:
            value = kept[1]
        else:
            value = json.loads(raw)
            self._fields[path] = (raw, value)
        document[key] = value
        if isinstance(value, (dict, list)):
            encoder.remember(value, raw)

    def _encoded_document(
        self,
        data: dict[str, Any],
        shape: dict[str, Any],
        encoder: Any,
        path: tuple[str, ...],
        rows: dict[tuple[str, ...], dict[str, bytes]],
    ) -> dict[str, Any]:
        """_world_json(data), remembering the JSON of each field the hash tree hashes as one.

        Each field is encoded once, for its document, its hash and its archive chunks. What
        the kernel encoded reads back as the same bytes: its keys are str, in sorted order.
        rows holds the bytes of fields a Rust row wrote, by path, and those are not encoded.
        """
        document: dict[str, Any] = {}
        written = rows.get(path, {})
        for key in sorted(data):
            value = data[key]
            inner = shape.get(key)
            if inner is not None and isinstance(value, dict) and all(map(_is_str, value)):
                document[key] = self._encoded_document(value, inner, encoder, (*path, key), rows)
                continue
            encoded = written.get(key) or native.canonical_json(value)
            if encoded is None:
                document[key] = json.loads(WorldState._canonical_text(value))
                continue
            self._document_field((*path, key), encoded, document, key, encoder)
        return document

    def _log_raws(self, log: EventLog) -> list[bytes]:
        # A Rust EventLog that only grew returns the same bytes objects; a replaced
        # one makes its bytes anew, and then nothing known about the old one holds.
        known = self._event_raws
        count = len(log)
        if known is not None and len(known) <= count:
            if not known:
                return log.items_bytes(0, count)
            if (
                log.items_bytes(0, 1)[0] is known[0]
                and log.items_bytes(len(known) - 1, len(known))[0] is known[-1]
            ):
                return [*known, *log.items_bytes(len(known), count)]
        self._log, self._raws_equal = None, 0
        return log.items_bytes(0, count)

    def seed_events(self, log: EventLog, documents: Sequence[Any]) -> bool:
        """Start from a log a Checkpoint restore made from its event_log documents.

        The first len(documents) records of log must be the records restored from
        documents, and the documents must never change. A restored record converts
        back to its document when the document has exactly the record's fields, so
        the first capture then reuses both instead of converting and restoring the
        whole EventLog. Returns whether the documents were used.
        """
        if keeps_saved_bytes(log):
            if not isinstance(documents, SavedItems) or len(log) < len(documents):
                return False
            # The log was built from these bytes, so its leading records are the ones a
            # restore check builds from them.
            self._event_raws = log.items_bytes(0, len(documents))
            self._raws_equal = len(documents)
            self._log = None
            return True
        records = log.records[: len(documents)]
        if len(records) != len(documents) or any(
            set(document) != _RECORD_FIELDS or set(document["event"]) != _EVENT_FIELDS
            for document in documents
        ):
            return False
        events = self._events
        events.items, events.documents = list(records), list(documents)
        events.raws = [canonical_bytes(document) for document in documents]
        events.restored = list(records)
        # The items are the restored records themselves.
        events.restored_equal = len(records)
        # They belong to a live world's EventLog, which a restore check never extends.
        self._log = None
        return True

    def known_equal_histories(self) -> dict[tuple[str, str], int]:
        """How many leading entries of each model_state history equal their documents.

        The last documents() compared them, so a restore check need not compare them
        again: the restored world holds those documents.
        """
        return {
            path: cache.known_equal
            for path, cache in self._model_histories.items()
            if cache.known_equal
        }

    def restored_row_objects(self) -> dict[str, dict[int, tuple[bytes, Any, str]]] | None:
        """What a restore check takes as restored, by entity field; None without any rows."""
        return self._restored_rows if self._row_items else None

    def known_equal_rows(self) -> dict[str, set[str]]:
        """The IDs of the entities of the last documents() whose rows a check showed equal."""
        known = {}
        for name, raws in self._row_items.items():
            kept = self._restored_rows.get(name, {})
            known[name] = {entry[2] for entry in map(kept.get, map(id, raws)) if entry is not None}
        return known

    def remember_restored_rows(self, restored: WorldState, *, equal: bool) -> None:
        """Keep the entities of a restore check for the rows that do not change until the next."""
        self._restored_rows = {}
        if not equal:
            return
        for name, raws in self._row_items.items():
            rows = getattr(restored, name)
            if len(rows) == len(raws):
                self._restored_rows[name] = {
                    id(raw): (raw, row, key)
                    for raw, (key, row) in zip(raws, rows.items(), strict=True)
                }

    def restored_metrics(self) -> tuple[list[Any], list[dict[str, int]]]:
        """What the last restore check made of the leading samples of the last documents()."""
        return list(self._snapshots.restored), list(self._resource_snapshots.restored)

    def remember_restored_metrics(self, metrics: MetricsRecorder) -> None:
        """Keep the samples a restore check made from the last documents()."""
        self._snapshots.restored = list(metrics.snapshots)
        self._resource_snapshots.restored = list(metrics.resource_snapshots or [])

    def restored_events(self) -> list[EventLogRecord]:
        """Records restored from the leading event_log documents of the last documents().

        Empty for a Rust EventLog, whose restore check extends restored_log() instead.
        """
        return [] if self._event_raws is not None else list(self._events.restored)

    def restored_events_equal(self) -> int:
        """How many leading restored records are known to equal the captured records."""
        if self._live_log is not None:
            return len(self._event_raws)
        if self._event_raws is not None:
            return self._raws_equal
        return self._events.restored_equal

    def restored_log(self) -> EventLog | None:
        """The EventLog of the last restore check, if it holds exactly restored_events().

        That restored world was discarded, so the next restore check may append the
        new records to its EventLog instead of indexing every record again.

        A log that wrote each of its records' bytes is what restoring them gives: reading
        those bytes gives the frozen values they were written from, by the writer's exact
        types (native/src/checkpoint.rs, tried at every save by benchmarks/rule_shadow). The
        restore check then reads the live log, and only reads it. No restored log is built.
        """
        if self._live_log is not None:
            return self._live_log
        log = self._log
        if log is None:
            return None
        if self._event_raws is not None:
            # It was built from the leading item bytes of the last documents().
            count = len(log)
            if count > len(self._event_raws) or (
                count and log.items_bytes(count - 1, count)[0] is not self._event_raws[count - 1]
            ):
                return None
            return log
        restored = self._events.restored
        if len(log) != len(restored):
            return None
        if restored and log.last() is not restored[-1]:
            return None
        return log

    def remember_restored(self, log: EventLog, *, equal: bool) -> None:
        """Keep the EventLog a restore check made from the last documents().

        equal says whether the check showed its records equal to the captured records.
        """
        if log is self._live_log:
            # The live log is not kept as a restored one. Its records are all known equal, if a
            # later capture has to build a restored log after all.
            self._raws_equal, self._log = len(log), None
            return
        if self._event_raws is not None:
            if len(log) != len(self._event_raws):
                raise ValueError("restored EventLog does not match the captured documents")
            self._raws_equal = len(log) if equal else 0
            self._log = log
            return
        records = log.records
        if len(records) != len(self._events.items):
            raise ValueError("restored EventLog does not match the captured documents")
        self._events.restored = list(records)
        self._events.restored_equal = len(records) if equal else 0
        self._log = log
