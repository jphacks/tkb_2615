"""A compressed Checkpoint keeps its hash and reuses its chunks, and a page of a Checkpoint
section reads only the chunks it returns.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.checkpoints import CheckpointArchive


def test_checkpoint_section_reads_only_selected_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sheltersim.storage.checkpoints import CheckpointArchive

    rows = [{"minute_index": minute, "value": "x" * 128} for minute in range(5000)]
    archive = CheckpointArchive(tmp_path, chunk_records=128)
    record = archive.encode(
        SimpleNamespace(
            checkpoint_hash="a" * 64, to_dict=lambda: {"metrics_state": {"snapshots": rows}}
        )
    )
    reads = []
    original = archive._get

    def read(key: str) -> object:
        reads.append(key)
        return original(key)

    monkeypatch.setattr(archive, "_get", read)
    result = archive.list_page(record, ("metrics_state", "snapshots"), offset=4097, limit=3)
    assert result["items"] == rows[4097:4100]
    assert result["next_offset"] == 4100
    assert result["total_items"] == 5000
    assert len(reads) <= 2
    assert result["full_checkpoint_verified_by_this_read"] is False


def test_compressed_checkpoint_retains_hash_and_reuses_chunks(tmp_path):
    checkpoint = Checkpoint.capture(
        WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    )
    archive = CheckpointArchive(tmp_path)
    manifest = archive.encode(checkpoint)
    first = sorted(tmp_path.rglob("*.json.gz"))
    assert archive.decode(manifest).to_json() == checkpoint.to_json()
    assert archive.encode(checkpoint) == manifest
    assert sorted(tmp_path.rglob("*.json.gz")) == first


@pytest.mark.parametrize("chunk_records", [20, 128])
def test_archive_of_a_cached_capture_is_byte_identical(
    tmp_path, live_checkpoint_world, chunk_records
):
    """Packing with a capture's encoder writes the manifest and chunks a plain
    archive writes (Issue #44). The EventLog is long enough to be split into chunks."""
    from sheltersim.core.checkpoint_cache import CheckpointHistoryCache
    from sheltersim.events.handlers import EventDispatcher
    from sheltersim.events.models import EventKind, ScheduledEvent
    from sheltersim.experiments.metrics import MetricsRecorder

    world = live_checkpoint_world
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    cache = CheckpointHistoryCache()
    Checkpoint.capture(world, metrics=metrics, history_cache=cache)
    for index in range(130):
        world.schedule_event(
            ScheduledEvent(
                event_id=f"archive-event-{index}",
                kind=EventKind.EXOGENOUS,
                due_minute=0,
                priority=0,
                sequence=world.allocate_event_sequence(),
                event_type="unknown_checkpoint_test_event",
                payload={"note": "x" * 600},
            )
        )
        EventDispatcher().dispatch_due(world, 0)
    assert len(world.event_log) == 131
    checkpoint = Checkpoint.capture(world, metrics=metrics, history_cache=cache)
    cached = CheckpointArchive(
        tmp_path / "cached", chunk_records=chunk_records, encoder=checkpoint._encoder
    )
    manifest = cached._encode_validated_document(
        checkpoint._document_view(), checkpoint_hash=checkpoint.checkpoint_hash
    )
    plain = CheckpointArchive(tmp_path / "plain", chunk_records=chunk_records)
    assert manifest == plain.encode(checkpoint)
    assert manifest["document"]["fields"]["world_state"]["fields"]["event_log"]["kind"] == (
        "list_v2"
    )
    chunks = sorted(path.name for path in (tmp_path / "plain" / "chunks").iterdir())
    assert sorted(path.name for path in (tmp_path / "cached" / "chunks").iterdir()) == chunks
    for name in chunks:
        assert (tmp_path / "cached" / "chunks" / name).read_bytes() == (
            tmp_path / "plain" / "chunks" / name
        ).read_bytes()


def test_a_loaded_checkpoint_is_restored_once_and_shares_nothing_with_its_restore(
    tmp_path, monkeypatch, saved_timeline
):
    """A load hands over the world its validation restored instead of restoring twice."""
    from sheltersim.experiments.branch import BranchRuntime

    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    reference = store.branch_reference("p", "r", "main")
    expected = store.load_checkpoint("p", "r", reference.checkpoint_hash)
    original = WorldState.from_dict.__func__
    restores = []

    def from_dict(cls, document, **kwargs):
        restores.append(document["minute_index"])
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(from_dict))
    # A new store, which has not restored this Checkpoint before, reads it from disk.
    fresh = type(store)(store.root)
    checkpoint, restored = fresh.load_restored_checkpoint("p", "r", reference.checkpoint_hash)
    runtime = BranchRuntime.from_checkpoint(checkpoint, branch_id="main", _restored=restored)
    assert restores == [30]
    assert checkpoint.to_json() == expected.to_json()
    world, metrics = expected.restore_state()
    assert runtime.world.canonical_json() == world.canonical_json()
    assert runtime.metrics.to_dict() == metrics.to_dict()
    runtime.world.metric_state["test_only"] = 1.0
    runtime.metrics.snapshots.clear()
    assert checkpoint.to_json() == expected.to_json()


def test_a_checkpoint_read_again_is_an_independent_copy_of_the_first_restore(
    tmp_path, saved_timeline
):
    """Draft for #46: a Checkpoint read again is decoded and checked anew, and its world is
    a copy of the one restored before."""
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    reference = store.branch_reference("p", "r", "main")
    checkpoint, (world, metrics) = store.load_restored_checkpoint(
        "p", "r", reference.checkpoint_hash
    )
    expected = world.canonical_json()
    world.metric_state["test_only"] = 1.0
    world.model_state.clear()
    metrics.snapshots.clear()
    again, (world_again, metrics_again) = store.load_restored_checkpoint(
        "p", "r", reference.checkpoint_hash
    )
    assert again is not checkpoint
    assert again.to_json() == checkpoint.to_json()
    assert world_again.canonical_json() == expected
    assert metrics_again.to_dict() == checkpoint.restore_metrics().to_dict()
    assert world_again is not world


def test_a_checkpoint_read_again_still_detects_a_corrupt_chunk(tmp_path, saved_timeline):
    """Draft for #46: reusing the restored world never skips checking the disk."""
    import gzip

    from sheltersim.storage.projects import StorageError

    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    reference = store.branch_reference("p", "r", "main")
    store.load_restored_checkpoint("p", "r", reference.checkpoint_hash)
    for chunk in (tmp_path / "p" / "runs" / "r" / "checkpoints" / "chunks").glob("*.json.gz"):
        chunk.write_bytes(gzip.compress(b'["tampered"]'))
    with pytest.raises(StorageError, match="checkpoint_incompatible_or_corrupt"):
        store.load_restored_checkpoint("p", "r", reference.checkpoint_hash)
