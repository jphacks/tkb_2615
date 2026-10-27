"""Starting a computation decodes and validates the head checkpoint only once."""

import json

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web.projects import ProjectAPI


def saved_run(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    initial = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    TimelineStore(store, "p", "r").record(initial)
    return store, scenario


def test_resume_loads_the_head_checkpoint_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_run(tmp_path)
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    loads, validations = [], []
    load, validate = ProjectStore.load_checkpoint, Checkpoint._validate

    def counted_load(self, project_id, run_id, checkpoint_hash):
        loads.append(checkpoint_hash)
        return load(self, project_id, run_id, checkpoint_hash)

    def counted_validate(self, *args, **kwargs):
        if self.checkpoint_hash == head:
            validations.append(1)
        return validate(self, *args, **kwargs)

    monkeypatch.setattr(ProjectStore, "load_checkpoint", counted_load)
    monkeypatch.setattr(Checkpoint, "_validate", counted_validate)
    api = ProjectAPI(store)
    try:
        api.submit("p", "r", "main", 5)
        assert loads == []
        api._pool.submit(lambda: None).result(timeout=60)
    finally:
        api.close()
    assert store.branch_reference("p", "r", "main").computed_until == 5
    # One decode with its validation, then one restore into the runtime.
    assert loads == [head]
    assert len(validations) == 2


def test_resume_range_is_still_checked_without_loading_history(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, scenario = saved_run(tmp_path)
    api = ProjectAPI(store)
    status = store.run("p", "r")["status"]
    try:
        for target in (0, scenario.duration_minutes + 1, "5"):
            with pytest.raises(StorageError, match="uncomputed remaining range"):
                api.submit("p", "r", "main", target)
        with pytest.raises(StorageError):
            api.submit("p", "r", "missing", 5)
        assert store.run("p", "r")["status"] == status
    finally:
        api.close()


def test_corrupt_head_still_fails_closed_in_the_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_run(tmp_path)
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    for chunk in (store._path("p", "runs", "r", "checkpoints") / "chunks").iterdir():
        chunk.write_bytes(b"corrupt")
    api = ProjectAPI(store)
    try:
        api.submit("p", "r", "main", 5)
        api._pool.submit(lambda: None).result(timeout=60)
    finally:
        api.close()
    assert store.run("p", "r")["status"] == "failed"
    assert store.branch_reference("p", "r", "main").checkpoint_hash == head
