"""A writer's history cache gives the same Checkpoint as a full capture (Issue #44).

Saving reuses the documents, bytes and restored records of history captured
before, so these tests compare every cached result with a full conversion while
histories grow, drop old entries, are edited in place or belong to a new world.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from sheltersim import native
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CheckpointHistoryCache, canonical_bytes
from sheltersim.core.saved_items import SavedItems
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder


def _realize(world: WorldState, name: str) -> None:
    world.schedule_event(
        ScheduledEvent(
            event_id=name,
            kind=EventKind.EXOGENOUS,
            due_minute=world.minute_index,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="unknown_checkpoint_test_event",
            payload={"rows": [{"value": 1.5}], "label": "避難所"},
        )
    )
    EventDispatcher().dispatch_due(world, world.minute_index)


def _sample(metrics: MetricsRecorder, minute: int) -> None:
    metrics.snapshots.append(replace(metrics.snapshots[0], minute_index=minute))


def _restored_before(kwargs: dict) -> int:
    """How many records a restore check's from_dict had restored before."""
    log = kwargs.get("_restored_log")
    return len(log) if log is not None else len(kwargs["_restored_events"])


def _full_documents(world: WorldState, metrics: MetricsRecorder) -> tuple[dict, dict]:
    # world.to_dict() without its validation, which these hand-made histories fail.
    return json.loads(WorldState._canonical_text(world._canonical_data())), metrics.to_dict()


def test_documents_and_bytes_match_a_full_conversion_as_histories_change(
    live_checkpoint_world,
) -> None:
    world = live_checkpoint_world
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    metrics.resource_snapshots = [{"minute_index": 0, "water": 3}]
    history = [{"minute": 0, "deltas": {"stress": 0.25}, "tags": ["a"]}]
    world.model_state.update(
        {
            "body_state_v1": {"history": history, "agents": {}},
            "resource_service_v1": {"request_history": [{"id": 1}], "queue_stall_history": []},
            "shared_operations_v1": {"history": "not a list"},
        }
    )
    cache = CheckpointHistoryCache()
    earlier = None

    def changes():
        yield "first capture"
        _realize(world, "cache-event-1")
        _sample(metrics, 1)
        metrics.resource_snapshots.append({"minute_index": 1, "water": 2})
        history.append({"minute": 1, "deltas": {"stress": 0.5}, "tags": []})
        yield "appended"
        del history[0]
        history.append({"minute": 2, "deltas": {}, "tags": ("tuple",)})
        yield "window dropped its oldest entry"
        history[0]["deltas"]["stress"] = 0.75
        metrics.resource_snapshots[0]["water"] = 1
        yield "entries edited in place"
        world.model_state["body_state_v1"]["history"] = list(history)
        yield "same entries in a new list"
        world.model_state["resource_service_v1"]["request_history"].clear()
        yield "history emptied"
        world.model_state["with_int_keys"] = {"inner": {10: "a", 9: 1.5}}
        world.model_state["body_state_v1"]["agents"] = {2: {"a": (1, 2)}, 10: None}
        yield "object keys only json reads, inside and beside a history"

    for _ in changes():
        world_document, metric_document, encoder = cache.documents(world, metrics)
        assert (world_document, metric_document) == _full_documents(world, metrics)
        body = {"world_state": world_document, "metrics_state": metric_document, "minute": 0}
        for value in (world_document, metric_document, body, world_document["model_state"]):
            raw = canonical_bytes(value)
            assert encoder.encode(value) == raw
            assert not encoder.exceeds(value, len(raw))
            assert encoder.exceeds(value, len(raw) - 1)
            assert encoder.digest(value) == hashlib.sha256(raw).hexdigest()
        if earlier is not None:
            # A later capture never changes the documents of an earlier one.
            assert canonical_bytes(earlier[0]) == earlier[1]
        earlier = (world_document, canonical_bytes(world_document))


def test_cached_capture_is_the_full_capture_and_restores_only_new_records(
    live_checkpoint_world, monkeypatch
) -> None:
    world = live_checkpoint_world
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    cache = CheckpointHistoryCache()
    reused = []
    original = WorldState.from_dict.__func__

    def from_dict(cls, document, **kwargs):
        if "_restored_events" in kwargs:
            reused.append((_restored_before(kwargs), len(document["event_log"])))
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(from_dict))
    for name in ("first", "cache-event-1", "cache-event-2"):
        if name != "first":
            _realize(world, name)
            _sample(metrics, len(metrics.snapshots))
        cached = Checkpoint.capture(world, metrics=metrics, history_cache=cache)
        full = Checkpoint.capture(world, metrics=metrics)
        assert cached.to_json() == full.to_json()
        assert cached.checkpoint_hash == full.checkpoint_hash
    # Each cached capture restores only the records added since the previous one. The Rust
    # EventLog wrote its records' bytes, so its restore check reads the log itself.
    if native.available():
        assert reused == [(1, 1), (2, 2), (3, 3)]
    else:
        assert reused == [(0, 1), (1, 2), (2, 3)]


def test_a_new_world_object_is_converted_and_restored_again(live_checkpoint_world) -> None:
    world = live_checkpoint_world
    cache = CheckpointHistoryCache()
    first = Checkpoint.capture(world, history_cache=cache)
    restored = WorldState.from_dict(world.to_dict())
    _realize(restored, "cache-event-1")
    cached = Checkpoint.capture(restored, history_cache=cache)
    assert cached.to_json() == Checkpoint.capture(restored).to_json()
    assert first.to_json() == Checkpoint.capture(world).to_json()


@pytest.mark.skipif(native.available(), reason="the Python EventLog only")
def test_a_cache_seeded_with_a_restored_event_log_captures_what_a_full_capture_does(
    live_checkpoint_world, monkeypatch
) -> None:
    """A load or a fork seeds the EventLog its restore made, so the first save reuses it.

    This is the Python EventLog, which follows records by identity.
    """
    world = live_checkpoint_world
    _realize(world, "cache-event-1")
    saved = Checkpoint.capture(world)
    restored = WorldState.from_dict(json.loads(json.dumps(saved.world_state)))
    documents = saved.world_state["event_log"]
    parsed = []
    original = WorldState.from_dict.__func__

    def from_dict(cls, document, **kwargs):
        if "_restored_events" in kwargs:
            parsed.append(len(document["event_log"]) - _restored_before(kwargs))
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(from_dict))
    cache = CheckpointHistoryCache()
    assert cache.seed_events(restored.event_log, documents)
    _realize(restored, "cache-event-2")
    cached = Checkpoint.capture(restored, history_cache=cache)
    assert cached.to_json() == Checkpoint.capture(restored).to_json()
    # Only the record added after the seed was restored from its document.
    assert parsed == [1]

    # A document that a record does not convert back to is never seeded.
    extra = [dict(documents[0], note="older field"), *documents[1:]]
    unseeded = CheckpointHistoryCache()
    assert not unseeded.seed_events(restored.event_log, extra)
    # Nor are more documents than the log has records.
    more = [*documents, *documents, *documents]
    assert len(more) > len(restored.event_log)
    assert not unseeded.seed_events(restored.event_log, more)
    cached = Checkpoint.capture(restored, history_cache=unseeded)
    assert cached.to_json() == Checkpoint.capture(restored).to_json()


@pytest.mark.skipif(not native.available(), reason="needs the Rust EventLog")
def test_a_cache_seeded_with_saved_event_bytes_reads_none_of_them(
    live_checkpoint_world, monkeypatch
) -> None:
    """A Rust EventLog restored from saved bytes is seeded by position: the first save
    reuses its bytes, and its restore check compares only the records added since."""
    world = live_checkpoint_world
    _realize(world, "cache-event-1")
    saved = Checkpoint.capture(world)
    documents = SavedItems([canonical_bytes(item) for item in saved.world_state["event_log"]])
    restored = WorldState.from_dict(
        {**json.loads(canonical_bytes(saved.world_state)), "event_log": documents},
        _saved_json=True,
    )
    assert restored.event_log.unread() == len(documents)
    compared = []
    same_state = WorldState.same_state

    def counting_same_state(self, other, **kwargs):
        compared.append(kwargs.get("known_equal_events"))
        return same_state(self, other, **kwargs)

    monkeypatch.setattr(WorldState, "same_state", counting_same_state)
    cache = CheckpointHistoryCache()
    assert cache.seed_events(restored.event_log, documents)
    _realize(restored, "cache-event-2")
    cached = Checkpoint.capture(restored, history_cache=cache)
    # The new record's bytes were written from it, so no record is left to compare.
    assert compared == [len(documents) + 1]
    # The seeded records were neither encoded nor read again.
    assert restored.event_log.unread() == len(documents)
    assert isinstance(cached.world_state["event_log"], SavedItems)
    assert cached.world_state["event_log"].raws[: len(documents)] == documents.raws
    assert cached.to_json() == Checkpoint.capture(restored).to_json()

    # Documents that are not saved bytes are never seeded into a Rust EventLog.
    assert not CheckpointHistoryCache().seed_events(
        restored.event_log, saved.world_state["event_log"]
    )


def test_a_restored_world_equal_to_the_captured_one_is_not_validated_again(
    live_checkpoint_world, monkeypatch
) -> None:
    """The restore check compares the restored world with the world validated just
    before, and compares only the EventLog records no earlier check compared."""
    world = live_checkpoint_world
    cache = CheckpointHistoryCache()
    validated = []
    compared = []
    validate = WorldState.validate
    same_state = WorldState.same_state

    def counting_validate(self, **kwargs):
        validated.append(self is world)
        return validate(self, **kwargs)

    def counting_same_state(self, other, **kwargs):
        result = same_state(self, other, **kwargs)
        compared.append((kwargs["known_equal_events"], len(other.event_log), result))
        return result

    monkeypatch.setattr(WorldState, "validate", counting_validate)
    monkeypatch.setattr(WorldState, "same_state", counting_same_state)
    for name in ("first", "cache-event-1", "cache-event-2"):
        if name != "first":
            _realize(world, name)
        validated.clear()
        cached = Checkpoint.capture(world, history_cache=cache)
        assert validated == [True]
        monkeypatch.setattr(WorldState, "validate", validate)
        assert cached.to_json() == Checkpoint.capture(world).to_json()
        monkeypatch.setattr(WorldState, "validate", counting_validate)
    if native.available():
        assert compared == [(1, 1, True), (2, 2, True), (3, 3, True)]
    else:
        assert compared == [(0, 1, True), (1, 2, True), (2, 3, True)]


def test_a_restored_world_that_differs_is_validated_and_compared_in_full_next_time(
    live_checkpoint_world, monkeypatch
) -> None:
    from sheltersim.core.checkpoint import CheckpointError

    world = live_checkpoint_world
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, history_cache=cache)
    restore = Checkpoint._world_document_for_restore
    validated = []
    validate = WorldState.validate

    def counting_validate(self, **kwargs):
        validated.append(self is world)
        return validate(self, **kwargs)

    def changed(self, *args, **kwargs):
        document = restore(self, *args, **kwargs)
        document["metric_state"] = {**document["metric_state"], "test_only": 1.0}
        return document

    monkeypatch.setattr(WorldState, "validate", counting_validate)
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "_world_document_for_restore", changed)
        _realize(world, "cache-event-1")
        Checkpoint.capture(world, history_cache=cache)
    # A different (still valid) restored world is validated as a whole.
    assert validated == [True, False]
    # What differs is not the EventLog, which the Rust log's restore check does not build.
    assert cache.restored_events_equal() == (len(world.event_log) if native.available() else 0)

    def broken(self, *args, **kwargs):
        document = restore(self, *args, **kwargs)
        document["minute_index"] = -1
        return document

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "_world_document_for_restore", broken)
        _realize(world, "cache-event-2")
        try:
            Checkpoint.capture(world, history_cache=cache)
        except CheckpointError as exc:
            assert "checkpoint payload is invalid" in str(exc)
        else:
            raise AssertionError("an invalid restored world was accepted")


def test_a_restore_check_extends_the_last_checks_eventlog(
    live_checkpoint_world, monkeypatch
) -> None:
    """The world a restore check builds is discarded, so the next check appends the new
    records to its EventLog; the log's indexes equal those of a log built anew."""
    from sheltersim.events.queue import EventLog

    world = live_checkpoint_world
    cache = CheckpointHistoryCache()
    received = []
    original = WorldState.from_dict.__func__

    def from_dict(cls, document, **kwargs):
        received.append(kwargs.get("_restored_log"))
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(from_dict))
    logs = []
    for name in ("first", "cache-event-1", "cache-event-2"):
        if name != "first":
            _realize(world, name)
        Checkpoint.capture(world, history_cache=cache)
        logs.append(cache.restored_log())
    if native.available():
        # The log wrote its records' bytes, so restoring them gives the log itself.
        assert received == [world.event_log] * 3
        assert all(received[index] is world.event_log for index in range(3))
        assert logs == [world.event_log] * 3
    else:
        assert received[0] is None
        assert received[1] is logs[0] and received[2] is logs[0] and logs[2] is logs[0]
    fresh = EventLog(list(world.event_log.records))
    log = logs[2]
    assert len(log) == len(world.event_log)
    for record in world.event_log.records:
        assert log.find(record.event.event_id) == record
    assert log.max_sequence == fresh.max_sequence
    assert log.event_types() == fresh.event_types()
    monkeypatch.setattr(WorldState, "from_dict", classmethod(original))
    assert Checkpoint.capture(world, history_cache=cache).to_json() == (
        Checkpoint.capture(world).to_json()
    )

    # A cache seeded with a live world's records never extends that world's log.
    seeded = CheckpointHistoryCache()
    saved = Checkpoint.capture(world)
    seeded.seed_events(world.event_log, saved.world_state["event_log"])
    assert seeded.restored_log() is None


def test_a_new_kpi_sample_is_still_checked_when_earlier_ones_are_reused(
    live_checkpoint_world,
) -> None:
    """A restore check reads only the new samples, and still refuses a bad one."""
    world = live_checkpoint_world
    metrics = MetricsRecorder()
    cache = CheckpointHistoryCache()
    metrics.capture(world)
    first = metrics.snapshots[0]

    def add_sample(minute: int) -> None:
        metrics.snapshots.append(replace(first, minute_index=minute))

    add_sample(first.minute_index + 1)
    Checkpoint.capture(world, metrics=metrics, history_cache=cache)
    assert len(cache.restored_metrics()[0]) == 2

    add_sample(first.minute_index + 2)
    checkpoint = Checkpoint.capture(world, metrics=metrics, history_cache=cache)
    assert checkpoint.checkpoint_hash == Checkpoint.capture(world, metrics=metrics).checkpoint_hash
    assert len(cache.restored_metrics()[0]) == 3
    assert metrics.to_dict() == MetricsRecorder.from_dict(checkpoint.metrics_state).to_dict()

    add_sample(first.minute_index + 2)
    with pytest.raises(CheckpointError, match="unique and increasing"):
        Checkpoint.capture(world, metrics=metrics, history_cache=cache)


def test_a_history_entry_changed_in_place_is_compared_again_by_the_restore_check(
    live_checkpoint_world,
) -> None:
    """Known-equal counts come from comparing with the documents at this very save."""
    world = live_checkpoint_world
    world.model_state.setdefault("body_state_v1", {"history": []})
    path = ("body_state_v1", "history")
    history = world.model_state[path[0]][path[1]]
    history.extend({"minute": index} for index in range(3))
    cache = CheckpointHistoryCache()
    cache.documents(world, None)
    assert cache.known_equal_histories().get(path, 0) == 0
    cache.documents(world, None)
    assert cache.known_equal_histories()[path] == len(history)
    history[0]["minute"] = 99
    cache.documents(world, None)
    assert cache.known_equal_histories().get(path, 0) == 0


def test_the_saved_bytes_of_a_replaced_event_log_are_read_anew(live_checkpoint_world) -> None:
    if not native.available():
        pytest.skip("the sheltersim_native extension is not installed")
    world = live_checkpoint_world
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, history_cache=cache)
    _realize(world, "grown")
    grown = Checkpoint.capture(world, history_cache=cache)
    assert grown.checkpoint_hash == Checkpoint.capture(world).checkpoint_hash

    restored = WorldState.from_dict(world.to_dict())
    world.event_log = restored.event_log
    _realize(world, "replaced")
    again = Checkpoint.capture(world, history_cache=cache)
    assert again.checkpoint_hash == Checkpoint.capture(world).checkpoint_hash


def test_a_writer_computes_model_signatures_again_only_for_other_configs(
    live_checkpoint_world, monkeypatch
) -> None:
    import sheltersim.core.checkpoint_cache as cache_module

    calls = []
    signatures = cache_module.model_signatures

    def counted(configs):
        calls.append(configs)
        return signatures(configs)

    monkeypatch.setattr(cache_module, "model_signatures", counted)
    world = live_checkpoint_world
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, history_cache=cache)
    saved = Checkpoint.capture(world, history_cache=cache)
    assert len(calls) == 1
    assert saved.model_versions == Checkpoint.capture(world).model_versions

    world.model_configs["added_by_the_test"] = {"version": 1}
    saved = Checkpoint.capture(world, history_cache=cache)
    assert len(calls) == 2
    assert saved.model_versions == Checkpoint.capture(world).model_versions
