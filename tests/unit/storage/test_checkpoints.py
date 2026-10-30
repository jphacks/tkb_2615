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
from sheltersim.experiments.timeline import TimelineStore
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
    first = sorted(tmp_path.rglob("*.pack"))
    assert len(first) == 1
    assert archive.decode(manifest).to_json() == checkpoint.to_json()
    assert archive.encode(checkpoint) == manifest
    assert CheckpointArchive(tmp_path).encode(checkpoint) == manifest
    assert sorted(tmp_path.rglob("*.pack")) == first


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
    packs = sorted(path.name for path in (tmp_path / "plain" / "packs").iterdir())
    assert sorted(path.name for path in (tmp_path / "cached" / "packs").iterdir()) == packs
    for name in packs:
        assert (tmp_path / "cached" / "packs" / name).read_bytes() == (
            tmp_path / "plain" / "packs" / name
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
    """#46: a Checkpoint read again is decoded and checked anew, and its world is
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
    """#46: reusing the restored world never skips checking the disk."""
    import gzip

    from sheltersim.storage.projects import StorageError

    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    reference = store.branch_reference("p", "r", "main")
    store.load_restored_checkpoint("p", "r", reference.checkpoint_hash)
    for pack in (tmp_path / "p" / "runs" / "r" / "checkpoints" / "packs").glob("*.pack"):
        pack.write_bytes(gzip.compress(b'["tampered"]').ljust(pack.stat().st_size, b"\0"))
    with pytest.raises(StorageError, match="checkpoint_incompatible_or_corrupt"):
        store.load_restored_checkpoint("p", "r", reference.checkpoint_hash)


def test_a_computation_keeps_no_copy_of_the_checkpoint_it_starts_from(
    tmp_path, monkeypatch, saved_timeline
):
    """#46: advancing reads its head without keeping a copy of the restored world, which
    would hold memory for the whole computation; reads still keep one."""
    from sheltersim.storage import projects

    store, timeline = saved_timeline(tmp_path)
    copies = []
    original = projects._copy_restored

    def copy_restored(*restored):
        copies.append(1)
        return original(*restored)

    monkeypatch.setattr(projects, "_copy_restored", copy_restored)
    timeline.advance("main", 30)
    assert copies == []
    assert store._restored == {}
    reference = store.branch_reference("p", "r", "main")
    store.load_restored_checkpoint("p", "r", reference.checkpoint_hash)
    assert list(store._restored) == [("p", "r", reference.checkpoint_hash)]


def test_kept_worlds_are_dropped_once_no_read_used_them_for_a_while(
    tmp_path, monkeypatch, saved_timeline
):
    """#46: the local server does not hold the kept worlds' memory after the reads."""
    from sheltersim.storage import projects

    store, _ = saved_timeline(tmp_path)
    store._restored_idle_seconds = 60.0
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    now = [1000.0]
    monkeypatch.setattr(projects.time, "monotonic", lambda: now[0])
    store.load_restored_checkpoint("p", "r", head)
    store._restored_timer.cancel()
    now[0] += 50
    store.load_restored_checkpoint("p", "r", head)
    # A read 50 seconds later keeps the world for another minute from then.
    now[0] += 50
    store._expire_restored()
    assert list(store._restored) == [("p", "r", head)]
    store._restored_timer.cancel()
    now[0] += 11
    store._expire_restored()
    assert store._restored == {}
    assert store._restored_timer is None


def test_the_timer_drops_the_kept_worlds(tmp_path, saved_timeline):
    import time

    store, _ = saved_timeline(tmp_path)
    store._restored_idle_seconds = 0.05
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    store.load_restored_checkpoint("p", "r", head)
    deadline = time.monotonic() + 10
    while store._restored and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store._restored == {}
    # A later read keeps the world again, from a new restore.
    store.load_restored_checkpoint("p", "r", head)
    assert list(store._restored) == [("p", "r", head)]


def _as_v2_archive(directory: Path) -> None:
    """Rewrite the v3 manifests in directory as v2 ones with one file per chunk, the
    layout saved before packs."""
    import hashlib
    import json

    from sheltersim.storage.checkpoints import _encoded

    (directory / "chunks").mkdir(exist_ok=True)
    for path in directory.glob("*.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for key, (pack, offset, length) in CheckpointArchive.pack_locations(manifest).items():
            data = (directory / "packs" / f"{pack}.pack").read_bytes()
            (directory / "chunks" / f"{key}.json.gz").write_bytes(data[offset : offset + length])
        body = {"checkpoint_hash": manifest["checkpoint_hash"], "document": manifest["document"]}
        v2 = {
            "schema_version": 1,
            "storage_type": "checkpoint_archive_v2",
            **body,
            "manifest_hash": hashlib.sha256(_encoded(body)).hexdigest(),
        }
        path.write_text(json.dumps(v2), encoding="utf-8")
    for pack in (directory / "packs").glob("*.pack"):
        pack.unlink()
    # The byte ledger counted the packs; a run without one counts its files once.
    (directory.parent / "checkpoint-write-ledger.json").unlink()


def _manifest(store, checkpoint_hash):
    import json

    path = store._path("p", "runs", "r", "checkpoints", f"{checkpoint_hash}.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_save_writes_one_pack_of_only_the_chunks_it_adds(tmp_path, saved_timeline):
    store, timeline = saved_timeline(tmp_path)
    packs = store._path("p", "runs", "r", "checkpoints") / "packs"
    timeline.advance("main", 30)
    before = {path.name for path in packs.glob("*.pack")}
    first = _manifest(store, store.branch_reference("p", "r", "main").checkpoint_hash)
    timeline.advance("main", 60)
    new = {path.name for path in packs.glob("*.pack")} - before
    assert len(new) == 1
    second = _manifest(store, store.branch_reference("p", "r", "main").checkpoint_hash)
    first, second = map(CheckpointArchive.pack_locations, (first, second))
    # Chunks the first save stored stay in its packs; the new pack has only new ones.
    kept = set(first) & set(second)
    assert kept
    assert all(second[key] == first[key] for key in kept)
    added = {key for key, location in second.items() if f"{location[0]}.pack" in new}
    assert added and not added & set(first)
    reference = store.branch_reference("p", "r", "main")
    fresh = type(store)(store.root)
    assert fresh.load_checkpoint("p", "r", reference.checkpoint_hash).simulation_minute == 60


def test_a_run_saved_with_one_file_per_chunk_is_read_and_continued(tmp_path, saved_timeline):
    """Runs saved before packs keep loading; a later save refers to their chunk files
    and packs only the chunks it adds."""
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    directory = store._path("p", "runs", "r", "checkpoints")
    reference = store.branch_reference("p", "r", "main")
    expected = store.load_checkpoint("p", "r", reference.checkpoint_hash).to_json()
    _as_v2_archive(directory)
    fresh = type(store)(store.root)
    assert fresh.load_checkpoint("p", "r", reference.checkpoint_hash).to_json() == expected
    chunk_files = {path.name.removesuffix(".json.gz") for path in (directory / "chunks").iterdir()}
    TimelineStore(fresh, "p", "r").advance("main", 60)
    head = fresh.branch_reference("p", "r", "main").checkpoint_hash
    assert fresh.load_checkpoint("p", "r", head).simulation_minute == 60
    assert len(list((directory / "packs").glob("*.pack"))) == 1
    # The new manifest refers to the chunk files it shares and packs none of them.
    manifest = _manifest(fresh, head)
    assert not set(CheckpointArchive.pack_locations(manifest)) & chunk_files
    archive = CheckpointArchive(directory)
    archive._validate_manifest(manifest)
    referenced = set()
    archive._get = lambda key, get=archive._get: referenced.add(key) or get(key)
    archive._unpack(manifest["document"])
    assert referenced & chunk_files
    assert fresh.checkpoint_write_bytes("p", "r") == fresh._checkpoint_archive_bytes("p", "r")


def test_a_v3_manifest_with_changed_locations_fails_closed(tmp_path):
    import hashlib

    from sheltersim.core.checkpoint import CheckpointError
    from sheltersim.storage.checkpoints import _encoded

    checkpoint = Checkpoint.capture(
        WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    )
    manifest = CheckpointArchive(tmp_path).encode(checkpoint)
    table = manifest["locations"]
    key = next(iter(table["chunks"]))
    moved = {
        **manifest,
        "locations": {"packs": table["packs"], "chunks": {**table["chunks"], key: [0, 0, 10]}},
    }
    with pytest.raises(CheckpointError, match="manifest integrity"):
        CheckpointArchive(tmp_path).decode(moved)
    # Even with a matching manifest hash, a chunk is checked against its key.
    body = {name: moved[name] for name in ("checkpoint_hash", "document", "locations")}
    moved["manifest_hash"] = hashlib.sha256(_encoded(body)).hexdigest()
    with pytest.raises(CheckpointError, match="missing or corrupt|integrity"):
        CheckpointArchive(tmp_path).decode(moved)
    malformed = {**manifest, "locations": {"packs": ["pack"], "chunks": {key: [0, -1, 0]}}}
    body = {name: malformed[name] for name in ("checkpoint_hash", "document", "locations")}
    malformed["manifest_hash"] = hashlib.sha256(_encoded(body)).hexdigest()
    with pytest.raises(CheckpointError, match="invalid checkpoint archive locations"):
        CheckpointArchive(tmp_path).decode(malformed)


def test_a_chunk_that_comes_back_in_a_later_save_is_not_written_again(tmp_path):
    """A writer remembers where all its saves put each chunk, not only its last save."""
    from sheltersim.core.checkpoint_cache import CanonicalEncoder

    encoder = CanonicalEncoder()
    first = [{"row": index, "text": "a" * 64} for index in range(300)]
    second = [{"row": index, "text": "b" * 64} for index in range(300)]
    previous = None
    for number, rows in enumerate((first, second, first)):
        archive = CheckpointArchive(
            tmp_path,
            encoder=encoder,
            known_locations=CheckpointArchive.pack_locations(previous) if previous else None,
        )
        previous = archive._encode_validated_document(
            {"rows": rows}, checkpoint_hash=f"{number}" * 64
        )
        assert CheckpointArchive(tmp_path).unpack_document(previous) == {"rows": rows}
    # The third save wrote nothing: its chunks are all in the first save's pack.
    assert archive.new_files == 0
    assert len(list((tmp_path / "packs").glob("*.pack"))) == 2
