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
