"""Index a branch's saves in another process while its computation goes on.

A long computation saves every 30 minutes and indexed each save before computing
on. BackgroundIndexer sends each save's new records to one process that indexes
them in order with HistoryIndex.ingest, so the computation waits for the index
only at its end (and when the index falls more than a few saves behind).

Only what ingest reads is sent: the EventLog records, KPI samples and resource
samples added since the last save, the model_state history entries from the
minute before the last save on (body-state entries from the first new event's
minute on), the model_state names and the model configs. ingest reads them at the
offsets the index records; a list that does not start there fails, so a send that
does not continue the index is never indexed as if it did.
"""

from __future__ import annotations

import json
import multiprocessing
from bisect import bisect_left
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sheltersim.core.saved_items import SavedItems
from sheltersim.events.queue import saved_realized_minute
from sheltersim.storage.projects import StorageError

# The model_state histories ingest indexes, with the field holding each entry's minute.
_HISTORIES = (
    ("body_state_v1", "minute"),
    ("staff_operations_v1", "complete_minute"),
    ("population_transitions_v1", "minute"),
    ("population_presence_v1", "minute"),
    ("population_lifecycle_v1", "minute"),
)


class _Tail(Sequence[Any]):
    """The items of a list from start on, standing for the whole list of length total."""

    def __init__(self, total: int, start: int, items: list[Any]) -> None:
        if start + len(items) != total:
            raise ValueError("tail does not end the list")
        self.total, self.start, self.items = total, start, items

    def __len__(self) -> int:
        return self.total

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            begin, end, step = index.indices(self.total)
            if step != 1 or begin < self.start:
                raise StorageError("history_index_out_of_step: rebuild from the saved Run")
            return self.items[begin - self.start : end - self.start]
        position = index + self.total if index < 0 else index
        if not self.start <= position < self.total:
            raise StorageError("history_index_out_of_step: rebuild from the saved Run")
        return self.items[position - self.start]

    def __iter__(self) -> Iterator[Any]:
        if self.start:
            raise StorageError("history_index_out_of_step: rebuild from the saved Run")
        return iter(self.items)


def _realized_minute(record: Any) -> int:
    return saved_realized_minute(record) if isinstance(record, bytes) else record["realized_minute"]


def _send_document(
    checkpoint: Any, events_from: int, kpis_from: int, previous_minute: int
) -> dict[str, Any]:
    world, metrics = checkpoint.world_state, checkpoint.metrics_state
    events = world["event_log"]
    # Saved records go as their bytes, and the indexing process parses them.
    new_events = (
        events.raws[events_from:] if isinstance(events, SavedItems) else events[events_from:]
    )
    model_state: dict[str, Any] = {name: {} for name in world["model_state"]}
    for name, field in _HISTORIES:
        state = world["model_state"].get(name)
        history = state.get("history") if isinstance(state, dict) else None
        if not isinstance(history, list):
            continue
        since = max(0, previous_minute - 1)
        if name == "body_state_v1" and new_events:
            # Dialogue rows of the new events read their body-state effects.
            since = min(since, min(map(_realized_minute, new_events)))
        first = bisect_left(history, since, key=lambda row: row[field])
        model_state[name] = {"history": history[first:]}
    snapshots = metrics["snapshots"]
    resources = metrics.get("resource_snapshots") or []
    return {
        "simulation_minute": checkpoint.simulation_minute,
        "checkpoint_hash": checkpoint.checkpoint_hash,
        "state_hash": checkpoint.state_hash,
        "events": (len(events), events_from, list(new_events)),
        "snapshots": (len(snapshots), kpis_from, list(snapshots[kpis_from:])),
        # Resource samples, when kept, are one per KPI sample.
        "resources": (len(resources), kpis_from, list(resources[kpis_from:]))
        if resources
        else (0, 0, []),
        "model_state": model_state,
        "model_configs": world["model_configs"],
    }


def _view(document: dict[str, Any]) -> SimpleNamespace:
    """A checkpoint as ingest reads it, from a sent document."""
    # A history sent from its minute on is read from its start (see _send_document).
    resources = _Tail(*document["resources"])
    total, start, new_events = document["events"]
    new_events = [json.loads(item) if isinstance(item, bytes) else item for item in new_events]
    return SimpleNamespace(
        simulation_minute=document["simulation_minute"],
        checkpoint_hash=document["checkpoint_hash"],
        state_hash=document["state_hash"],
        world_state={
            "event_log": _Tail(total, start, new_events),
            "model_state": document["model_state"],
            "model_configs": document["model_configs"],
        },
        metrics_state={
            "snapshots": _Tail(*document["snapshots"]),
            **({"resource_snapshots": resources} if len(resources) else {}),
        },
    )


def _index_saves(connection: Any, root: str, max_bytes: int, project_id: str, run_id: str) -> None:
    """The indexing process: ingest each sent save in order and answer each one."""
    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.storage.projects import ProjectStore

    index = HistoryIndex(ProjectStore(Path(root), max_bytes=max_bytes), project_id, run_id)
    while True:
        message = connection.recv()
        if message is None:
            connection.send(None)
            return
        branch, document, observations = message
        try:
            index.ingest(branch, _view(document), observations=observations)
        except Exception as exc:  # The computation reports it and stops.
            connection.send(f"{type(exc).__name__}: {exc}")
            return
        connection.send(None)


class BackgroundIndexer:
    """Ingests one branch's saves in another process, in the order they are saved.

    The caller holds the branch's writer lease while the indexer is open; close()
    waits until every save is indexed and raises the first indexing error.
    """

    def __init__(
        self,
        store: Any,
        project_id: str,
        run_id: str,
        branch_id: str,
        *,
        max_pending: int = 2,
    ) -> None:
        from sheltersim.storage.history_index import HistoryIndex

        self.branch_id = branch_id
        self.max_pending = max_pending
        # Continue from what the index holds for the branch.
        with HistoryIndex(store, project_id, run_id).connect() as db:
            head = db.execute(
                "SELECT minute,counts FROM heads WHERE branch=?", (branch_id,)
            ).fetchone()
        if head is None:
            raise StorageError("history_index_stale: explicitly resume index rebuild")
        counts = json.loads(head["counts"])
        self.events_from = counts.get("events", 0)
        self.kpis_from = counts.get("kpis", 0)
        self.previous_minute = head["minute"]
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(
            target=_index_saves,
            args=(child, str(store.root), store.max_bytes, project_id, run_id),
            daemon=True,
        )
        self._process.start()
        child.close()
        self._pending = 0
        self._error: str | None = None

    def submit(self, checkpoint: Any, observations: list[dict[str, Any]]) -> None:
        """Send one saved Checkpoint and the observations recorded since the last save."""
        while self._pending >= self.max_pending:
            self._receive()
        self._raise()
        document = _send_document(
            checkpoint, self.events_from, self.kpis_from, self.previous_minute
        )
        self._connection.send((self.branch_id, document, list(observations)))
        self._pending += 1
        self.events_from = document["events"][0]
        self.kpis_from = document["snapshots"][0]
        self.previous_minute = checkpoint.simulation_minute

    def close(self) -> None:
        """Wait until every sent save is indexed, stop the process, raise any error."""
        try:
            while self._pending:
                self._receive()
            if self._error is None and self._process.is_alive():
                self._connection.send(None)
                self._connection.recv()
        finally:
            self._connection.close()
            self._process.join(timeout=30)
            if self._process.is_alive():
                self._process.terminate()
        self._raise()

    def _receive(self) -> None:
        try:
            answer = self._connection.recv()
        except EOFError:
            answer = "the indexing process ended without answering"
        self._pending -= 1
        if answer is not None and self._error is None:
            self._error = answer
            self._pending = 0

    def _raise(self) -> None:
        if self._error is not None:
            raise StorageError(f"history_index_unavailable: {self._error}")
