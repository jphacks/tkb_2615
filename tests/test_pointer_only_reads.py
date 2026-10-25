"""Routes that need a pointer or one field must not decode whole checkpoints."""

import json
from types import SimpleNamespace

import pytest

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web.projects import ProjectAPI
from sheltersim.web.results_chat import ResultsChatService, WebQueryError


def saved_run(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return store, timeline


@pytest.fixture
def no_checkpoint_decode(monkeypatch):
    decoded = []
    original = ProjectStore.load_checkpoint

    def load(self, project_id, run_id, checkpoint_hash):
        decoded.append(checkpoint_hash)
        return original(self, project_id, run_id, checkpoint_hash)

    monkeypatch.setattr(ProjectStore, "load_checkpoint", load)
    return decoded


def test_comparison_reads_fork_receipts_without_decoding_history(tmp_path, no_checkpoint_decode):
    store, timeline = saved_run(tmp_path)
    timeline.advance("main", 30)
    timeline.fork("main", 13, "child")
    timeline.advance("child", 30)
    _, child = store.load_branch("p", "r", "child")
    expected = child.runtime_state["fork_origin"]
    api = ProjectAPI(store, recover=False)
    no_checkpoint_decode.clear()
    query = {"branch_ids": ["main,child"], "minute": ["30"]}
    try:
        # Both branches are at a saved boundary, so frames come from the cache.
        result = api.get(["p", "runs", "r", "compare"], query)
    finally:
        api.close()
    assert no_checkpoint_decode == []
    assert result["origin_receipts"] == [None, expected]
    assert result["shared_origin"] is True


def test_report_and_conversations_check_only_the_branch_pointer(tmp_path, no_checkpoint_decode):
    store, _ = saved_run(tmp_path)
    api = ProjectAPI(store, recover=False)
    try:
        assert api.get(["p", "runs", "r", "branches", "main", "report"], {}) == {
            "status": "not_generated"
        }
        assert api.get(["p", "runs", "r", "branches", "main", "conversations"], {})["turns"] == []
        assert no_checkpoint_decode == []
        for action in ("report", "conversations"):
            with pytest.raises(StorageError):
                api.get(["p", "runs", "r", "branches", "missing", action], {})
    finally:
        api.close()


def test_results_qa_completion_check_uses_the_pointer(tmp_path, no_checkpoint_decode):
    store, timeline = saved_run(tmp_path)
    timeline.advance("main", 5)
    no_checkpoint_decode.clear()
    service = SimpleNamespace(projects=SimpleNamespace(store=store))
    with pytest.raises(WebQueryError, match="completed project branch"):
        ResultsChatService._project_evidence(service, "p", "r", "main", None, None, None)
    assert no_checkpoint_decode == []
