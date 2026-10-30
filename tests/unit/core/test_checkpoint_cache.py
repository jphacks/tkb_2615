"""A writer's history cache gives the same Checkpoint as a full capture (Issue #44).

Saving reuses the documents, bytes and restored records of history captured
before, so these tests compare every cached result with a full conversion while
histories grow, drop old entries, are edited in place or belong to a new world.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.checkpoint_cache import CheckpointHistoryCache, canonical_bytes
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

    for _ in changes():
        world_document, metric_document, encoder = cache.documents(world, metrics)
        assert (world_document, metric_document) == _full_documents(world, metrics)
        body = {"world_state": world_document, "metrics_state": metric_document, "minute": 0}
        for value in (world_document, metric_document, body, world_document["model_state"]):
            raw = canonical_bytes(value)
            assert encoder.encode(value) == raw
            assert encoder.size(value) == len(raw)
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
            reused.append((len(kwargs["_restored_events"]), len(document["event_log"])))
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
    # Each cached capture restores only the records added since the previous one.
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


def test_a_cache_seeded_with_a_restored_event_log_captures_what_a_full_capture_does(
    live_checkpoint_world, monkeypatch
) -> None:
    """A load or a fork seeds the EventLog its restore made, so the first save reuses it."""
    world = live_checkpoint_world
    _realize(world, "cache-event-1")
    saved = Checkpoint.capture(world)
    restored = WorldState.from_dict(json.loads(json.dumps(saved.world_state)))
    documents = saved.world_state["event_log"]
    parsed = []
    original = WorldState.from_dict.__func__

    def from_dict(cls, document, **kwargs):
        if "_restored_events" in kwargs:
            parsed.append(len(document["event_log"]) - len(kwargs["_restored_events"]))
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(from_dict))
    cache = CheckpointHistoryCache()
    assert cache.seed_events(restored.event_log.records, documents)
    _realize(restored, "cache-event-2")
    cached = Checkpoint.capture(restored, history_cache=cache)
    assert cached.to_json() == Checkpoint.capture(restored).to_json()
    # Only the record added after the seed was restored from its document.
    assert parsed == [1]

    # A document that a record does not convert back to is never seeded.
    extra = [dict(documents[0], note="older field"), *documents[1:]]
    unseeded = CheckpointHistoryCache()
    assert not unseeded.seed_events(restored.event_log.records[: len(extra)], extra)
    assert not unseeded.seed_events(restored.event_log.records, documents)
    cached = Checkpoint.capture(restored, history_cache=unseeded)
    assert cached.to_json() == Checkpoint.capture(restored).to_json()


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
    assert cache.restored_events_equal() == 0

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
    assert seeded.seed_events(world.event_log.records, saved.world_state["event_log"])
    assert seeded.restored_log() is None
