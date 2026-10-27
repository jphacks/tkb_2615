"""Project storage keeps a saved answer's proofs and lets one worker write a Run at a time."""

from __future__ import annotations

from pathlib import Path

import pytest

from sheltersim.storage.projects import ProjectStore, StorageError

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
