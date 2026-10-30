"""A Run saved as Checkpoint v1 still loads, replays, resumes, forks and compares.

Its Checkpoints verify under the v1 hash rule (the SHA-256 of the whole document) and
what is saved after them is the current version, with the state a Run saved only in the
current version has at the same minute.
"""

from __future__ import annotations

import pytest

from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CanonicalEncoder
from sheltersim.experiments.timeline import TimelineError, TimelineStore
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.projects import StorageError
from sheltersim.web.projects import ProjectAPI

V1_UNTIL = 60
RESUMED_UNTIL = 90


def _saved_as_v1(saved_timeline, directory, scenario, monkeypatch):
    """A Run computed with the v1 hash rule, as the earlier code saved it."""
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "CURRENT_SCHEMA_VERSION", 1)
        patch.setattr(CanonicalEncoder, "world_tree_digest", CanonicalEncoder.digest)
        patch.setattr(CanonicalEncoder, "body_tree_digest", CanonicalEncoder.digest)
        store, timeline = saved_timeline(directory, scenario)
        timeline.advance("main", V1_UNTIL)
    return store, timeline


@pytest.fixture
def v1_run(tmp_path, saved_timeline, generated_scenario, monkeypatch):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    return _saved_as_v1(saved_timeline, tmp_path / "v1", generated_scenario, monkeypatch)


@pytest.fixture
def current_run(tmp_path, saved_timeline, generated_scenario):
    store, timeline = saved_timeline(tmp_path / "current", generated_scenario)
    timeline.advance("main", RESUMED_UNTIL)
    return store, timeline


def test_a_v1_head_is_read_and_its_hashes_are_checked(v1_run, tmp_path) -> None:
    store, _ = v1_run
    reference, head = store.load_branch("p", "r", "main")
    assert (head.schema_version, reference.computed_until) == (1, V1_UNTIL)

    document = store.checkpoint_document("p", "r", head.checkpoint_hash)
    assert document["schema_version"] == 1
    assert Checkpoint.document_hashes(document) == (head.state_hash, head.checkpoint_hash)
    fields = store.checkpoint_fields("p", "r", head.checkpoint_hash, ("state_hash",))
    assert fields["state_hash"] == head.state_hash
    assert Checkpoint.from_json(head.to_json()).checkpoint_hash == head.checkpoint_hash

    tampered = head.to_dict()
    tampered["world_state"]["resources"]["water"] = 500
    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(tampered)

    # The archive checks the hash under the version it holds.
    directory = store._path("p", "runs", "r", "checkpoints")
    manifest = store._read(directory / f"{head.checkpoint_hash}.json")
    assert CheckpointArchive(directory).document(manifest)["schema_version"] == 1
    forged = {**manifest, "checkpoint_hash": "0" * 64}
    with pytest.raises(CheckpointError):
        CheckpointArchive(directory).document(forged)


def test_a_v1_run_replays_and_resumes_as_a_run_saved_only_in_the_current_version(
    v1_run, current_run
) -> None:
    store, timeline = v1_run
    current_store, current_timeline = current_run

    # Seeks replay from the v1 Checkpoints; a state between them is the same state.
    for minute in (0, 30, 45, V1_UNTIL):
        assert (
            timeline.seek("main", minute).world.canonical_json()
            == current_timeline.seek("main", minute).world.canonical_json()
        )

    resumed = timeline.advance("main", RESUMED_UNTIL)
    reference, head = store.load_branch("p", "r", "main")
    assert head.schema_version == Checkpoint.CURRENT_SCHEMA_VERSION
    _, same = current_store.load_branch("p", "r", "main")
    assert head.state_hash == same.state_hash
    assert head.checkpoint_hash == same.checkpoint_hash
    assert resumed.world.canonical_json() == same.restore_world().canonical_json()
    assert reference.computed_until == RESUMED_UNTIL
    # The history before the first new save is still read under the v1 rule.
    assert timeline.seek("main", V1_UNTIL).world.minute_index == V1_UNTIL
    assert timeline.frame("main", V1_UNTIL)["minute"] == V1_UNTIL


def test_a_v1_checkpoint_forks_and_compares(v1_run, current_run) -> None:
    store, timeline = v1_run
    _, saved = store.load_branch("p", "r", "main")

    with pytest.raises(TimelineError, match="source state changed"):
        timeline.fork("main", V1_UNTIL, "stale", expected_state_hash="0" * 64)

    # A preview hashes the world under the current rule, not the v1 hash the head holds.
    source = timeline.seek("main", V1_UNTIL).world
    expected = source.state_digest()
    assert expected != saved.state_hash
    child = timeline.fork("main", V1_UNTIL, "child", expected_state_hash=expected)
    assert child.runtime_state["fork_origin"]["checkpoint_hash"] == saved.checkpoint_hash
    assert child.world.canonical_json() == source.canonical_json()
    _, child_head = store.load_branch("p", "r", "child")
    assert child_head.schema_version == Checkpoint.CURRENT_SCHEMA_VERSION
    assert child_head.state_hash == expected

    timeline.advance("child", V1_UNTIL + 10)
    assert timeline.seek("child", V1_UNTIL).world.canonical_json() == source.canonical_json()

    api = ProjectAPI(store, recover=False)
    try:
        comparison = api.workspace.comparison("p", "r", ["main", "child"], V1_UNTIL)
        assert comparison["shared_origin"] is True
        assert comparison["common_range"] == [V1_UNTIL, V1_UNTIL]
        replay = api.get(
            ["p", "runs", "r", "replay-frames"],
            {"branch_ids": ["main"], "start": [str(V1_UNTIL)], "end": [str(V1_UNTIL)]},
        )
        assert replay["samples"][0]["minute"] == V1_UNTIL
    finally:
        api.close()


def test_a_changed_chunk_of_a_v1_run_is_refused(v1_run) -> None:
    store, _ = v1_run
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    packs = store._path("p", "runs", "r", "checkpoints", "packs")
    for pack in packs.iterdir():
        data = bytearray(pack.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pack.write_bytes(bytes(data))
    with pytest.raises(StorageError):
        TimelineStore(store, "p", "r").store.load_checkpoint("p", "r", head)
