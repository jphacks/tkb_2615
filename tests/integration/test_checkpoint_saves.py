"""Saved Runs: each changed boundary is saved once, cancel and restart keep the Run,
and a failed publish or an interrupted archive recovers.

Saving one Checkpoint (validation, privacy checks) is tested in
tests/unit/storage/test_projects.py.
"""

from __future__ import annotations

import json

import pytest

from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.projects import ProjectStore


def _same_as_public_archive(directory, checkpoint, public):
    """The saved manifest packs the document as a fresh archive does, and each of
    its chunks, wherever an earlier save put it, holds the same bytes."""
    manifest = json.loads(
        (directory / f"{checkpoint.checkpoint_hash}.json").read_text(encoding="utf-8")
    )
    fresh = CheckpointArchive(public)
    expected = fresh.encode(checkpoint)
    assert manifest["document"] == expected["document"]
    assert manifest["checkpoint_hash"] == expected["checkpoint_hash"]
    saved = CheckpointArchive(directory)
    assert saved.decode(manifest).to_json() == checkpoint.to_json()
    fresh._validate_manifest(expected)
    for key in CheckpointArchive.pack_locations(expected):
        assert saved._get(key) == fresh._get(key)


@pytest.mark.parametrize(("target", "expected"), [(30, [30]), (31, [30, 31])])
def test_advance_records_each_changed_boundary_once(
    tmp_path, monkeypatch, target, expected, saved_timeline
):
    _, timeline = saved_timeline(tmp_path)
    minutes = []
    original = timeline.record

    def record(runtime, **kwargs):
        minutes.append(runtime.world.minute_index)
        return original(runtime, **kwargs)

    monkeypatch.setattr(timeline, "record", record)
    timeline.advance("main", target)
    assert minutes == expected


@pytest.mark.parametrize("cancel_at", [0, 5, 30, 31])
def test_cancel_restart_and_noop_fork_keep_world_metrics_and_events(
    tmp_path, cancel_at, saved_timeline
):
    store, timeline = saved_timeline(tmp_path)
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks > cancel_at

    paused = timeline.advance("main", 40, cancelled=cancelled)
    assert paused.world.minute_index == cancel_at
    restarted = TimelineStore(ProjectStore(store.root), "p", "r")
    resumed = restarted.advance("main", 40)
    _, reference = saved_timeline(tmp_path / "reference")
    uninterrupted = reference.advance("main", 40)
    assert resumed.world.canonical_json() == uninterrupted.world.canonical_json()
    assert resumed.metrics.to_dict() == uninterrupted.metrics.to_dict()
    parent = store.load_branch("p", "r", "main")[1].to_json()
    restarted.fork("main", 13, "child")
    child = restarted.advance("child", 40)
    assert child.world.canonical_json() == resumed.world.canonical_json()
    assert child.metrics.to_dict() == resumed.metrics.to_dict()
    assert store.load_branch("p", "r", "main")[1].to_json() == parent
    # The exact non-checkpoint boundary still uses deterministic replay.
    assert (
        restarted.seek("main", 17).world.canonical_json()
        == reference.seek("main", 17).world.canonical_json()
    )


def test_failed_pointer_publish_keeps_previous_durable_head(tmp_path, monkeypatch, saved_timeline):
    store, timeline = saved_timeline(tmp_path)
    prior = store.load_branch("p", "r", "main")[1].to_json()
    original = store._write

    def fail_pointer(path, value, **kwargs):
        if path.name == "branch.json":
            raise OSError("injected interruption before pointer publication")
        return original(path, value, **kwargs)

    monkeypatch.setattr(store, "_write", fail_pointer)
    with pytest.raises(OSError, match="injected interruption"):
        timeline.advance("main", 5)
    restarted = ProjectStore(store.root)
    assert restarted.load_branch("p", "r", "main")[1].to_json() == prior
    result = TimelineStore(restarted, "p", "r").advance("main", 5)
    assert result.world.minute_index == 5
    assert restarted.checkpoint_write_bytes("p", "r") == restarted._checkpoint_archive_bytes(
        "p", "r"
    )


def test_interrupted_archive_recovers_orphan_chunk_accounting(
    tmp_path, monkeypatch, saved_timeline
):
    store, timeline = saved_timeline(tmp_path)
    prior = store.load_branch("p", "r", "main")[1].to_json()
    write_pack = CheckpointArchive._write_pack

    def interrupted_write(archive, name, data):
        write_pack(archive, name, data)
        if archive.new_bytes:
            raise OSError("injected interruption after a durable pack")

    with monkeypatch.context() as patch:
        patch.setattr(CheckpointArchive, "_write_pack", interrupted_write)
        with pytest.raises(OSError, match="injected interruption"):
            timeline.advance("main", 5)
    restarted = ProjectStore(store.root)
    assert restarted.load_branch("p", "r", "main")[1].to_json() == prior
    assert restarted.checkpoint_write_bytes("p", "r") == restarted._checkpoint_archive_bytes(
        "p", "r"
    )
    assert TimelineStore(restarted, "p", "r").advance("main", 5).world.minute_index == 5
    assert restarted.checkpoint_write_bytes("p", "r") == restarted._checkpoint_archive_bytes(
        "p", "r"
    )


def test_stored_chunks_are_not_checked_again(tmp_path, monkeypatch, saved_timeline):
    import sheltersim.storage.projects as projects

    store, timeline = saved_timeline(tmp_path)
    checked = []
    original = projects._reject_sensitive_checkpoint_content

    def check(chunks):
        for value, _ in chunks:
            if not (isinstance(value, dict) and all(item is None for item in value.values())):
                checked.append(value)
        original(chunks)

    monkeypatch.setattr(projects, "_reject_sensitive_checkpoint_content", check)
    reference, head = store.load_branch("p", "r", "main")
    store.save_captured_branch("p", reference, head, expected_checkpoint_hash=head.checkpoint_hash)
    assert checked == []
    timeline.advance("main", 1)
    assert checked


def test_saved_archive_matches_the_public_encoder(tmp_path, saved_timeline):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 31)
    _, head = store.load_branch("p", "r", "main")
    directory = store._path("p", "runs", "r", "checkpoints")
    _same_as_public_archive(directory, head, tmp_path / "public")


def test_every_save_matches_a_full_capture_and_the_public_encoder(
    tmp_path, monkeypatch, saved_timeline, generated_scenario
):
    """Each save of an advance stores what a full capture and a fresh archive would.

    The generated Scenario keeps every growing history a save handles (event log,
    body-state history, KPI and resource snapshots); fork and resume start new
    writers. Issue #44 made saves reuse the encodings of history saved earlier.
    """
    store, timeline = saved_timeline(tmp_path, generated_scenario)
    saved = []
    original = timeline.record

    def record(runtime, **kwargs):
        checkpoint = original(runtime, **kwargs)
        assert checkpoint.to_json() == runtime.checkpoint().to_json()
        saved.append(checkpoint)
        return checkpoint

    monkeypatch.setattr(timeline, "record", record)
    timeline.advance("main", 50, checkpoint_interval=10)
    timeline.advance("main", 70, checkpoint_interval=10)
    timeline.fork("main", 40, "child")
    timeline.advance("child", 60, checkpoint_interval=10)
    assert [item.simulation_minute for item in saved] == [10, 20, 30, 40, 50, 60, 70, 40, 50, 60]
    directory = store._path("p", "runs", "r", "checkpoints")
    for checkpoint in saved:
        _same_as_public_archive(
            directory, checkpoint, tmp_path / "public" / checkpoint.checkpoint_hash
        )
    # A save writes one pack of the chunks it adds; the others stay where they are.
    assert len(list((directory / "packs").glob("*.pack"))) <= len(saved) + 1


def _index_tables(path) -> dict:
    import sqlite3

    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return {
            table: sorted(db.execute(f"SELECT * FROM {table}").fetchall())
            for table in ("records", "entities", "tokens", "generations", "heads", "checkpoints")
        }


def test_a_long_computation_indexed_in_another_process_gets_the_same_index(
    tmp_path, monkeypatch, saved_timeline, generated_scenario
):
    """A computation with many saves indexes them in another process
    (storage/index_writer.py); the index equals the one indexing each save directly."""
    from sheltersim.experiments import timeline as timeline_module
    from sheltersim.storage.index_writer import BackgroundIndexer

    started = []
    original = BackgroundIndexer.__init__

    def counting(self, *args, **kwargs):
        started.append(1)
        original(self, *args, **kwargs)

    monkeypatch.setattr(BackgroundIndexer, "__init__", counting)
    store, timeline = saved_timeline(tmp_path / "background", generated_scenario)
    timeline.advance("main", 90, checkpoint_interval=10)
    assert started == [1]
    monkeypatch.setattr(timeline_module, "BACKGROUND_INDEX_MIN_SAVES", 10**6)
    direct_store, direct = saved_timeline(tmp_path / "direct", generated_scenario)
    direct.advance("main", 90, checkpoint_interval=10)
    assert started == [1]
    index_path = ("p", "runs", "r", "history.sqlite")
    assert _index_tables(store._path(*index_path)) == _index_tables(direct_store._path(*index_path))


def test_an_indexing_error_in_the_other_process_fails_the_computation(tmp_path, saved_timeline):
    """A send that skips records the index lacks is refused in the indexing process and
    reported by close(), never indexed as if it continued the index."""
    import json
    import sqlite3

    from sheltersim.storage.index_writer import BackgroundIndexer
    from sheltersim.storage.projects import StorageError

    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    _, head = store.load_branch("p", "r", "main")
    path = store._path("p", "runs", "r", "history.sqlite")
    with sqlite3.connect(path) as db:
        (counts,) = db.execute("SELECT counts FROM heads WHERE branch='main'").fetchone()
        counts = json.loads(counts)
        held = counts["events"]
        counts["events"] = held - 2  # The index now holds two records fewer.
        db.execute("UPDATE heads SET counts=? WHERE branch='main'", (json.dumps(counts),))
    indexer = BackgroundIndexer(store, "p", "r", "main")
    indexer.events_from = held  # This send starts after the records the index lacks.
    indexer.submit(head, [])
    with pytest.raises(StorageError, match="history_index_out_of_step"):
        indexer.close()


def test_a_long_computation_without_resource_samples_is_indexed_in_another_process(
    tmp_path, saved_timeline
):
    """scenario_100_agents keeps no resource samples; its sends carry none."""
    from sheltersim.storage.history_index import HistoryIndex

    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 90, checkpoint_interval=10)
    with HistoryIndex(store, "p", "r").connect() as db:
        head = db.execute("SELECT minute FROM heads WHERE branch='main'").fetchone()
        saved = db.execute("SELECT COUNT(*) FROM checkpoints WHERE branch='main'").fetchone()
    assert head[0] == 90 and saved[0] == 10
