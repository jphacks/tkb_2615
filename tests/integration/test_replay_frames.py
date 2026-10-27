"""Batched observational playback must match individual Timeline/compare reads."""

import json
from types import SimpleNamespace

import pytest

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web.replay import replay_frames
from sheltersim.web.workspace import ProjectWorkspace


@pytest.fixture
def replay(tmp_path):
    scenario = ScenarioSpec.load("fixtures/scenarios/h00_small_replay.json")
    store = ProjectStore(tmp_path)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", 8)
    timeline.fork("main", 2, "child")
    timeline.advance("child", 8)
    return store, timeline, ProjectWorkspace(SimpleNamespace(store=store))


@pytest.mark.parametrize("ids", [["main"], ["main", "child"]])
def test_batch_matches_exact_reads_and_keeps_heads(replay, ids):
    store, timeline, workspace = replay
    heads = {key: store.load_branch("p", "r", key)[1].to_json() for key in ids}
    batch = replay_frames(store, workspace, "p", "r", ids, 2, 7)
    assert len(batch["samples"]) == 6
    for minute, frame in enumerate(batch["samples"], 2):
        expected = (
            timeline.frame("main", minute)
            if len(ids) == 1
            else workspace.comparison("p", "r", ids, minute)
        )
        assert frame == expected
    assert {key: store.load_branch("p", "r", key)[1].to_json() for key in ids} == heads


@pytest.mark.parametrize(
    ("ids", "start", "end"),
    [
        (["main"], 0, 8),
        (["main"], -1, 1),
        (["main"], 2, 1),
        (["main"], 7, 9),
        (["main", "child"], 1, 4),
        (["main", "main"], 2, 4),
        (["main", "child", "third"], 2, 4),
        (["main"], True, 3),
    ],
)
def test_batch_rejects_uncomputed_unbounded_or_invalid_intervals(replay, ids, start, end):
    store, _, workspace = replay
    with pytest.raises(StorageError):
        replay_frames(store, workspace, "p", "r", ids, start, end)


def test_batch_rejects_a_head_changed_during_read(replay, monkeypatch):
    store, _, workspace = replay
    original = store.branch_reference
    finished = False
    original_iter = TimelineStore.iter_worlds

    def changed_after_replay(*args, **kwargs):
        nonlocal finished
        yield from original_iter(*args, **kwargs)
        finished = True

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        if finished:
            result = result.model_copy(update={"checkpoint_hash": "0" * 64})
        return result

    monkeypatch.setattr(store, "branch_reference", changing)
    monkeypatch.setattr(TimelineStore, "iter_worlds", changed_after_replay)
    with pytest.raises(StorageError, match="heads changed"):
        replay_frames(store, workspace, "p", "r", ["main"], 2, 3)


def test_range_http_adapter_and_parameter_validation(replay):
    from sheltersim.web.projects import ProjectAPI

    store, _, _ = replay
    api = ProjectAPI(store, recover=False)
    parts = ["p", "runs", "r", "replay-frames"]
    try:
        result = api.get(parts, {"branch_ids": ["main"], "start": ["2"], "end": ["3"]})
        assert [frame["minute"] for frame in result["samples"]] == [2, 3]
        for query in (
            {"branch_ids": ["main"], "start": ["2", "3"], "end": ["4"]},
            {"branch_ids": ["main"], "start": ["2.5"], "end": ["4"]},
            {"branch_ids": ["main"], "start": ["2"], "end": ["4"], "extra": ["1"]},
        ):
            with pytest.raises(StorageError):
                api.get(parts, query)
    finally:
        api.close()


def test_checkpoint_only_batch_does_not_initialize_a_controller(replay, monkeypatch):
    import sheltersim.experiments.timeline as module

    store, timeline, workspace = replay
    expected = timeline.frame("main", 0)

    def unexpected(*args, **kwargs):
        raise AssertionError("an exact checkpoint read must not initialize policies")

    monkeypatch.setattr(module, "OfflineSimulationController", unexpected)
    batch = replay_frames(store, workspace, "p", "r", ["main"], 0, 0)
    assert batch["samples"] == [expected]
