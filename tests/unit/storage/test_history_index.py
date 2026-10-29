"""The history index stores each new record once with its entity and search rows,
whether a write holds a few records or more than one batch of them (#47).

A record already stored, or put earlier in the same write, adds nothing again, and a
different payload under its key is refused. A refused write and a write over the
storage capacity leave the index as it was. That a saved Run's index answers every
history kind is checked in tests/integration/test_project_evidence_routes.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from sheltersim.storage.history_index import HistoryIndex, _RecordBatch
from sheltersim.storage.projects import ProjectStore, StorageError


@pytest.fixture
def index(tmp_path) -> HistoryIndex:
    store = ProjectStore(tmp_path)
    (tmp_path / "p" / "runs" / "r").mkdir(parents=True)
    index = HistoryIndex(store, "p", "r")
    with index.connect(write=True):  # An empty index with its tables.
        pass
    return index


def _record(number: int) -> dict:
    return {
        "minute": number,
        "event_id": f"event-{number}",
        "agent_id": f"agent-{number % 7}",
        "event_type": "social_interaction",
        "status": "applied",
    }


def _put(index: HistoryIndex, *rows: tuple[str, dict, str | None]) -> None:
    with index.connect(write=True) as db, _RecordBatch(db, "main") as records:
        for kind, row, key in rows:
            records.put(kind, row, key=key)


def _counts(index: HistoryIndex) -> dict:
    with sqlite3.connect(index.path.as_uri() + "?mode=ro", uri=True) as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("records", "entities", "tokens")
        } | {"generations": dict(db.execute("SELECT kind,value FROM generations").fetchall())}


def test_records_beyond_one_batch_are_all_stored_with_entities_and_tokens(index):
    count = 2 * _RecordBatch._FLUSH_RECORDS + 3
    _put(index, *(("events", _record(number), None) for number in range(count)))
    # Each record has two identities (event_id, agent_id) and three tokens
    # (event_type, status and its own event link).
    assert _counts(index) == {
        "records": count,
        "entities": 2 * count,
        "tokens": 3 * count,
        "generations": {"events": count},
    }
    with sqlite3.connect(index.path.as_uri() + "?mode=ro", uri=True) as db:
        keys = db.execute(
            "SELECT key FROM tokens WHERE token='@event:event-1000' AND kind='events'"
        ).fetchall()
        stored = db.execute(
            "SELECT key FROM records WHERE kind='events' AND event_id='event-1000'"
        ).fetchall()
    assert keys == stored and len(keys) == 1


def test_a_repeated_record_is_stored_once_within_and_across_writes(index):
    _put(
        index, ("events", _record(1), "k"), ("events", _record(1), "k"), ("kpis", _record(2), None)
    )
    _put(index, ("events", _record(1), "k"), ("kpis", _record(2), None))
    assert _counts(index) == {
        "records": 2,
        "entities": 4,
        "tokens": 6,
        "generations": {"events": 1, "kpis": 1},
    }


@pytest.mark.parametrize("first_put", ["in_an_earlier_write", "earlier_in_this_batch", "flushed"])
def test_a_changed_payload_under_a_kept_key_is_refused_and_nothing_is_written(index, first_put):
    if first_put == "in_an_earlier_write":
        _put(index, ("events", _record(1), "k"))
    before = _counts(index)
    rows = [("events", _record(number), None) for number in range(2, 600)]
    changed = ("events", {**_record(1), "status": "rejected"}, "k")
    if first_put == "earlier_in_this_batch":
        rows[:0] = [("events", _record(1), "k"), changed]
    elif first_put == "flushed":
        rows = [("events", _record(1), "k"), *rows, changed]
    else:
        rows.append(changed)
    with pytest.raises(StorageError, match="immutable_history_record_conflict"):
        _put(index, *rows)
    assert _counts(index) == before


def test_an_index_write_over_the_storage_capacity_is_refused_and_rolled_back(index):
    _put(index, ("events", _record(1), None))
    before = _counts(index)
    index.store.max_bytes = index.store.usage()["bytes"] + 1_000
    with pytest.raises(StorageError, match="storage_capacity_exceeded"):
        _put(index, *(("events", _record(number), None) for number in range(2, 2_000)))
    assert _counts(index) == before
