"""Persistence acceptance checks, authored for the final #122 gate."""

import json

import pytest

from sheltersim.contracts.api import BranchReference
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import ProjectStore, StorageError, digest


def seed_store(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "run", scenario.id, 1)
    checkpoint = Checkpoint.capture(WorldState.from_scenario(scenario))
    ref = BranchReference(run_id="run", branch_id="main", fork_minute=0, computed_until=0)
    store.save_branch("p", ref, checkpoint)
    return store, checkpoint, ref


def test_restart_keeps_exact_checkpoint_and_marks_interruption(tmp_path):
    store, checkpoint, _ = seed_store(tmp_path)
    store.set_status("p", "run", "running")
    restored = ProjectStore(tmp_path)
    assert restored.recover_interrupted() == [("p", "run")]
    assert restored.load_branch("p", "run", "main")[1].to_json() == checkpoint.to_json()


def test_stale_save_and_traversal_are_rejected(tmp_path):
    store, checkpoint, ref = seed_store(tmp_path)
    with pytest.raises(StorageError, match="branch changed"):
        store.save_branch("p", ref, checkpoint, expected_checkpoint_hash="stale")
    with pytest.raises(StorageError):
        store.project("../outside")


def test_edit_creates_new_revision_without_changing_prior_run(tmp_path):
    store, checkpoint, _ = seed_store(tmp_path)
    run = store.run("p", "run")
    scenario = store.scenario("p", run["scenario_id"], 1)
    document = scenario.model_dump(mode="json")
    document["seed"] += 1
    edited = ScenarioSpec.model_validate(document)
    revision = store.save_scenario("p", edited, approved_hash=digest(json.loads(edited.canonical_json())))
    assert revision["scenario_version"] == 2
    assert store.run("p", "run")["scenario_version"] == 1
    assert store.load_branch("p", "run", "main")[1].state_hash == checkpoint.state_hash
