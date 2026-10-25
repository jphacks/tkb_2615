"""Real saved-run regressions for duplicate save work, restart and recovery."""

import json

import pytest

from sheltersim.contracts.api import BranchReference
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.projects import ProjectStore, StorageError, digest, safe_document


def saved_timeline(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return store, timeline


def test_save_validates_and_detaches_checkpoint_only_once(tmp_path, monkeypatch):
    store, _ = saved_timeline(tmp_path)
    reference, checkpoint = store.load_branch("p", "r", "main")
    calls = []
    original = Checkpoint.to_dict

    def capture(self):
        calls.append(self.checkpoint_hash)
        return original(self)

    monkeypatch.setattr(Checkpoint, "to_dict", capture)
    store.save_branch(
        "p", reference, checkpoint, expected_checkpoint_hash=checkpoint.checkpoint_hash
    )
    assert calls == [checkpoint.checkpoint_hash]


@pytest.mark.parametrize(("target", "expected"), [(30, [30]), (31, [30, 31])])
def test_advance_records_each_changed_boundary_once(tmp_path, monkeypatch, target, expected):
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
def test_cancel_restart_and_noop_fork_keep_world_metrics_and_events(tmp_path, cancel_at):
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


def test_failed_pointer_publish_keeps_previous_durable_head(tmp_path, monkeypatch):
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


def test_interrupted_archive_recovers_orphan_chunk_accounting(tmp_path, monkeypatch):
    store, timeline = saved_timeline(tmp_path)
    prior = store.load_branch("p", "r", "main")[1].to_json()
    write_chunk = CheckpointArchive._write_chunk

    def interrupted_write(archive, key, compressed):
        write_chunk(archive, key, compressed)
        if archive.new_bytes:
            raise OSError("injected interruption after a durable chunk")

    with monkeypatch.context() as patch:
        patch.setattr(CheckpointArchive, "_write_chunk", interrupted_write)
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


def test_save_still_rejects_mutation_and_sensitive_runtime_data(tmp_path):
    store, _ = saved_timeline(tmp_path)
    reference, checkpoint = store.load_branch("p", "r", "main")
    checkpoint.world_state["seed"] += 1
    with pytest.raises(CheckpointError):
        store.save_branch(
            "p", reference, checkpoint, expected_checkpoint_hash=reference.checkpoint_hash
        )
    _, valid = store.load_branch("p", "r", "main")
    sensitive = Checkpoint.capture(valid.restore_world(), runtime_state={"secret": "test-value"})
    new = BranchReference(run_id="r", branch_id="sensitive", fork_minute=0, computed_until=0)
    with pytest.raises(StorageError, match="sensitive"):
        store.save_branch("p", new, sensitive)


def files(store):
    return sorted((str(p), p.read_bytes()) for p in store.root.rglob("*") if p.is_file())


def test_recorded_checkpoint_is_validated_only_by_capture(tmp_path, monkeypatch):
    store, timeline = saved_timeline(tmp_path)
    _, head = store.load_branch("p", "r", "main")
    runtime = BranchRuntime.from_checkpoint(head, branch_id="main")
    calls = []
    original = Checkpoint._validate

    def validate(self):
        calls.append(self.checkpoint_hash)
        return original(self)

    monkeypatch.setattr(Checkpoint, "_validate", validate)
    recorded = timeline.record(runtime)
    assert calls == [recorded.checkpoint_hash]


@pytest.mark.parametrize("method", ["save_branch", "save_captured_branch"])
def test_sensitive_checkpoint_is_rejected_before_anything_is_written(tmp_path, method):
    store, _ = saved_timeline(tmp_path)
    _, valid = store.load_branch("p", "r", "main")
    sensitive = Checkpoint.capture(
        valid.restore_world(), runtime_state={"note": "sk-ant-abcdefghijklmnop"}
    )
    new = BranchReference(run_id="r", branch_id="sensitive", fork_minute=0, computed_until=0)
    before = files(store)
    with pytest.raises(StorageError, match="sensitive"):
        getattr(store, method)("p", new, sensitive)
    assert files(store) == before


def test_split_object_field_names_are_privacy_checked(tmp_path):
    from sheltersim.storage.projects import _reject_sensitive_checkpoint_content

    archive = CheckpointArchive(tmp_path, content_check=_reject_sensitive_checkpoint_content)
    with pytest.raises(StorageError, match="sensitive"):
        archive._prepare_validated_document(
            {"model_state": {"phone_number": "x" * 70_000}}, checkpoint_hash="0" * 64
        )
    assert not any(tmp_path.rglob("*.gz"))


def test_stored_chunks_are_not_checked_again(tmp_path, monkeypatch):
    import sheltersim.storage.projects as projects

    store, timeline = saved_timeline(tmp_path)
    checked = []
    original = projects._reject_sensitive_checkpoint_content

    def check(value):
        if not (isinstance(value, dict) and all(item is None for item in value.values())):
            checked.append(value)
        original(value)

    monkeypatch.setattr(projects, "_reject_sensitive_checkpoint_content", check)
    reference, head = store.load_branch("p", "r", "main")
    store.save_captured_branch("p", reference, head, expected_checkpoint_hash=head.checkpoint_hash)
    assert checked == []
    timeline.advance("main", 1)
    assert checked


def test_saved_archive_matches_the_public_encoder_byte_for_byte(tmp_path):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 31)
    _, head = store.load_branch("p", "r", "main")
    directory = store._path("p", "runs", "r", "checkpoints")
    manifest = json.loads((directory / f"{head.checkpoint_hash}.json").read_text())
    other = tmp_path / "public"
    assert CheckpointArchive(other).encode(head) == manifest
    for chunk in (other / "chunks").iterdir():
        assert (directory / "chunks" / chunk.name).read_bytes() == chunk.read_bytes()


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 0.0, -0.0, 0.12345678901234567])
def test_privacy_scalar_fast_path_keeps_exact_json_values(value):
    assert json.dumps(safe_document(value), allow_nan=False) == json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_privacy_scalar_fast_path_still_rejects_nonfinite_numbers(value):
    with pytest.raises(ValueError):
        safe_document({"values": [value]})
