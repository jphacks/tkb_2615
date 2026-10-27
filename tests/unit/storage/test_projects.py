"""Project storage keeps a saved answer's proofs and lets one worker write a Run at a time.

Saving a Checkpoint validates and detaches it once and refuses mutated or sensitive
data before anything is written; saved Runs over time are checked in
tests/integration/test_checkpoint_saves.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.contracts.api import BranchReference
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.projects import ProjectStore, StorageError, safe_document

SMALL_REPLAY = Path(__file__).parents[3] / "fixtures" / "scenarios" / "h00_small_replay.json"


def test_saved_scalar_proofs_and_scope_survive_project_reload(
    project_store_at, tmp_path: Path
) -> None:
    import json
    from copy import deepcopy

    from sheltersim.contracts.api import BranchReference
    from sheltersim.core.checkpoint import Checkpoint
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = project_store_at(tmp_path)
    scenario = ScenarioSpec.load(SMALL_REPLAY)
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    checkpoint = Checkpoint.capture(WorldState.from_scenario(scenario))
    store.save_branch(
        "project",
        BranchReference(run_id="run", branch_id="main", fork_minute=0, computed_until=0),
        checkpoint,
    )
    reference = {
        "run_id": "run",
        "branch_id": "main",
        "minute": 0,
        "kind": "kpi",
        "reference_id": "boundary-kpis",
        "status": "recorded",
    }
    turn = {
        "question": "Recorded minute?",
        "answer": "Recorded minute = 0",
        "minute": 0,
        "evidence": [reference],
        "limitations": [],
        "usage": {},
        "model_version": "fake",
        "conversation_id": "conversation",
        "context": {"run_id": "run", "branch_id": "main", "captured_minute": 0},
        "sources": [{"alias": "S1", "reference": reference}],
        "claims": [
            {
                "text": "Recorded minute = 0",
                "evidence": [{"alias": "S1", "field": "minute", "value": 0}],
            }
        ],
    }
    store.save_turn("project", "run", "main", "turn", turn)
    restored = ProjectStore(tmp_path).conversations("project", "run", "main")[0]
    assert restored["schema_version"] == 2
    assert restored["claims"] == turn["claims"]
    assert restored["context"] == turn["context"]
    invalid = deepcopy(turn)
    invalid["claims"][0]["evidence"][0]["alias"] = "unknown"
    with pytest.raises(StorageError, match="invalid_conversation_proof"):
        store.save_turn("project", "run", "main", "invalid-turn", invalid)


def test_run_writer_lease_blocks_second_worker_and_preserves_live_status_on_recovery(
    project_store_at, tmp_path: Path
) -> None:
    import json

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.projects import digest

    store = project_store_at(tmp_path)
    scenario = ScenarioSpec.load(SMALL_REPLAY)
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    lease = store.claim_run("project", "run")
    try:
        store.set_status("project", "run", "running")
        other = ProjectStore(tmp_path)
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.claim_run("project", "run")
        assert other.recover_interrupted() == []
        assert other.run("project", "run")["status"] == "running"
        with pytest.raises(StorageError, match="run_worker_busy"):
            other.delete_run("project", "run", confirmation="run")
    finally:
        store.release_run(lease)
    assert ProjectStore(tmp_path).recover_interrupted() == [("project", "run")]
    assert store.run("project", "run")["status"] == "interrupted"


def files(store):
    return sorted((str(p), p.read_bytes()) for p in store.root.rglob("*") if p.is_file())


def test_save_validates_and_detaches_checkpoint_only_once(tmp_path, monkeypatch, saved_timeline):
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


def test_save_still_rejects_mutation_and_sensitive_runtime_data(tmp_path, saved_timeline):
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


def test_recorded_checkpoint_is_validated_only_by_capture(tmp_path, monkeypatch, saved_timeline):
    store, timeline = saved_timeline(tmp_path)
    _, head = store.load_branch("p", "r", "main")
    runtime = BranchRuntime.from_checkpoint(head, branch_id="main")
    calls = []
    original = Checkpoint._validate

    def validate(self, **kwargs):
        calls.append(self.checkpoint_hash)
        return original(self, **kwargs)

    monkeypatch.setattr(Checkpoint, "_validate", validate)
    recorded = timeline.record(runtime)
    assert calls == [recorded.checkpoint_hash]


@pytest.mark.parametrize("method", ["save_branch", "save_captured_branch"])
def test_sensitive_checkpoint_is_rejected_before_anything_is_written(
    tmp_path, method, saved_timeline
):
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


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 0.0, -0.0, 0.12345678901234567])
def test_privacy_scalar_fast_path_keeps_exact_json_values(value):
    assert json.dumps(safe_document(value), allow_nan=False) == json.dumps(value, allow_nan=False)


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_privacy_scalar_fast_path_still_rejects_nonfinite_numbers(value):
    with pytest.raises(ValueError):
        safe_document({"values": [value]})
