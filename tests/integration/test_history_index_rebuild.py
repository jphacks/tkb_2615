"""The Project screen's history index: its status, and rebuilding it for a Run.

A generated Run with a main line and a Branch that adds toilets is indexed as it runs.
The operator can ask for the index status of each Branch and rebuild it ("既存Runの履歴
索引を再構築"). An index that matches its Branch is not rebuilt, because the decisions
recorded while computing live only there (#213). A Run without an index, as one saved
before the index existed, is rebuilt from its Checkpoints.

The history routes themselves are checked in tests/integration/test_project_evidence_routes.py.
"""

from __future__ import annotations

import copy

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import StorageError, digest
from sheltersim.web.projects import ProjectAPI

END = 60
FORK_MINUTE = 10
BRANCHES = ("main", "capacity")
KINDS = (
    "events",
    "kpis",
    "conversations",
    "social",
    "staff",
    "actions",
    "transitions",
    "dialogue",
    "care",
    "operations",
)


def _history(api: ProjectAPI, branch_id: str, kind: str) -> list[dict]:
    """Every record of one kind from the fork minute on, page by page."""
    items: list[dict] = []
    cursor = None
    while True:
        query = {"kind": [kind], "start": [str(FORK_MINUTE)], "end": [str(END)], "limit": ["100"]}
        if cursor:
            query["cursor"] = [cursor]
        page = api.get(["p", "runs", "r", "branches", branch_id, "history"], query)
        items += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return items


@pytest.fixture(scope="module")
def rebuilt(tmp_path_factory, generated_previews, add_toilets_program, saved_timeline):
    """Index statuses and every history at each step of asking for rebuilds."""
    preview = generated_previews(duration_days=END / 1440)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        store, timeline = saved_timeline(tmp_path_factory.mktemp("index") / "projects", scenario)
        timeline.advance("main", END, checkpoint_interval=10)
        program = add_toilets_program(2, start_minute=FORK_MINUTE)
        timeline.fork(
            "main",
            FORK_MINUTE,
            "capacity",
            program=program,
            approved_program_hash=digest(program.model_dump(mode="json")),
        )
        timeline.advance("capacity", END)
        api = ProjectAPI(store)

        def status() -> dict:
            return {
                branch: api.get(["p", "runs", "r", "index"], {"branch_id": [branch]})
                for branch in BRANCHES
            }

        def histories() -> dict:
            return {(b, kind): _history(api, b, kind) for b in BRANCHES for kind in KINDS}

        def rebuild() -> dict:
            results = {}
            for branch in BRANCHES:
                try:
                    results[branch] = api.post(["p", "runs", "r", "index"], {"branch_id": branch})
                except StorageError as refused:
                    results[branch] = refused
            return results

        try:
            steps = {"current": (status(), histories())}
            refused = rebuild()
            steps["refused"] = (status(), histories())
            # A Run saved before the index existed has no index file.
            HistoryIndex(store, "p", "r").path.unlink()
            missing = status()
            rebuilt_results = rebuild()
            steps["rebuilt"] = (status(), histories())
            yield steps, refused, missing, rebuilt_results
        finally:
            api.close()


def test_the_index_of_each_branch_is_ready_at_its_head(rebuilt):
    steps, _, _, _ = rebuilt
    status, _ = steps["current"]

    for branch in BRANCHES:
        assert status[branch]["status"] == "ready"
        assert status[branch]["indexed"]["computed_until"] == END
        assert (
            status[branch]["indexed"]["checkpoint_hash"]
            == (status[branch]["source_checkpoint_hash"])
        )


@pytest.mark.parametrize("kind", KINDS)
def test_a_current_index_is_not_rebuilt_and_keeps_every_history(rebuilt, kind):
    steps, refused, _, _ = rebuilt
    current_status, current = steps["current"]
    refused_status, after = steps["refused"]
    if not any(current[(branch, "events")] for branch in BRANCHES):
        pytest.fail("the Run must have recorded events to rebuild")

    for branch in BRANCHES:
        assert isinstance(refused[branch], StorageError)
        assert refused[branch].code == "history_index_current"
        assert refused_status[branch] == current_status[branch]
        assert after[(branch, kind)] == current[(branch, kind)]


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind != "actions"])
def test_a_run_without_an_index_is_rebuilt_from_its_checkpoints(rebuilt, kind):
    steps, _, missing, results = rebuilt
    _, current = steps["current"]
    rebuilt_status, after = steps["rebuilt"]

    for branch in BRANCHES:
        assert missing[branch]["status"] == "required"
        assert results[branch]["status"] == "indexed_not_verified"
        assert rebuilt_status[branch]["status"] == "ready"
        assert after[(branch, kind)] == current[(branch, kind)]


def test_a_rebuilt_index_says_the_recorded_decisions_are_not_restored(rebuilt):
    steps, _, _, results = rebuilt
    _, current = steps["current"]
    _, after = steps["rebuilt"]

    for branch in BRANCHES:
        assert "decisions recorded while computing" in results[branch]["limits"]
        decisions = [
            row for row in current[(branch, "actions")] if row["status"] == "selected_proposal"
        ]
        assert decisions, "the Run recorded action decisions while computing"
        assert not [
            row for row in after[(branch, "actions")] if row["status"] == "selected_proposal"
        ]
