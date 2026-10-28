"""The Project screen's history index: its status, and rebuilding it for a Run.

A generated Run with a main line and a Branch that adds toilets is indexed as it runs.
The operator can ask for the index status of each Branch and rebuild it ("既存Runの履歴
索引を再構築"); every history kind must read the same afterwards.

The history routes themselves are checked in tests/integration/test_project_evidence_routes.py.
"""

from __future__ import annotations

import copy

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import digest
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
    """Index statuses and every history before and after rebuilding both Branches."""
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
        try:
            status = {
                branch: api.get(["p", "runs", "r", "index"], {"branch_id": [branch]})
                for branch in BRANCHES
            }
            before = {(b, kind): _history(api, b, kind) for b in BRANCHES for kind in KINDS}
            results = {
                branch: api.post(["p", "runs", "r", "index"], {"branch_id": branch})
                for branch in BRANCHES
            }
            after = {(b, kind): _history(api, b, kind) for b in BRANCHES for kind in KINDS}
            yield status, results, before, after
        finally:
            api.close()


def test_the_index_of_each_branch_is_ready_at_its_head(rebuilt):
    status, results, _, _ = rebuilt

    for branch in BRANCHES:
        assert status[branch]["status"] == "ready"
        assert status[branch]["indexed"]["computed_until"] == END
        assert (
            status[branch]["indexed"]["checkpoint_hash"]
            == (status[branch]["source_checkpoint_hash"])
        )
        assert results[branch]["status"] == "indexed_not_verified"
        assert results[branch]["checkpoint_hash"] == status[branch]["source_checkpoint_hash"]


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(
            kind,
            marks=pytest.mark.xfail(
                strict=True, reason="#213: a rebuild drops the recorded action decisions"
            ),
        )
        if kind == "actions"
        else kind
        for kind in KINDS
    ],
)
def test_rebuilding_the_index_keeps_every_history(rebuilt, kind):
    _, _, before, after = rebuilt
    if not any(before[(branch, "events")] for branch in BRANCHES):
        pytest.fail("the Run must have recorded events to rebuild")

    for branch in BRANCHES:
        assert after[(branch, kind)] == before[(branch, kind)]
