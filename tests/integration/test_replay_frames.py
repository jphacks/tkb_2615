"""Batched observational playback must match individual Timeline/compare reads.

On a Run of the shared generated Scenario, replayed while people walk on the floor
plan, as the Project screen plays a Run back. Until #137 this replayed the first
minutes of h00_small_replay, which has neither a floor plan nor facility awareness.
"""

import copy
from types import SimpleNamespace

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.storage.projects import StorageError
from sheltersim.web.replay import replay_frames
from sheltersim.web.workspace import ProjectWorkspace

FORK = 22
# People walk on the floor plan in every minute from FORK to COMPUTED.
COMPUTED = 30


@pytest.fixture(scope="module")
def replay(tmp_path_factory, generated_scenario_preview, saved_timeline):
    """A main line and a child forked at FORK, both computed to COMPUTED.

    Shared by the module: replay_frames only reads, and the first test checks that.
    """
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    store, timeline = saved_timeline(tmp_path_factory.mktemp("replay"), scenario)
    timeline.advance("main", COMPUTED)
    timeline.fork("main", FORK, "child")
    timeline.advance("child", COMPUTED)
    return store, timeline, ProjectWorkspace(SimpleNamespace(store=store))


@pytest.mark.parametrize("ids", [["main"], ["main", "child"]])
def test_batch_matches_exact_reads_and_keeps_heads(replay, ids):
    store, timeline, workspace = replay
    heads = {key: store.load_branch("p", "r", key)[1].to_json() for key in ids}
    batch = replay_frames(store, workspace, "p", "r", ids, FORK, FORK + 5)
    assert len(batch["samples"]) == 6
    for minute, frame in enumerate(batch["samples"], FORK):
        expected = (
            timeline.frame("main", minute)
            if len(ids) == 1
            else workspace.comparison("p", "r", ids, minute)
        )
        assert frame == expected
        for branch_frame in [frame] if len(ids) == 1 else frame["frames"]:
            assert {agent["position_kind"] for agent in branch_frame["agents"]} == {"physical"}
            assert any(agent["edge_id"] for agent in branch_frame["agents"])
    assert {key: store.load_branch("p", "r", key)[1].to_json() for key in ids} == heads


@pytest.mark.parametrize(
    ("ids", "start", "end"),
    [
        (["main"], FORK - 2, FORK + 6),
        (["main"], -1, 1),
        (["main"], FORK, FORK - 1),
        (["main"], COMPUTED - 1, COMPUTED + 1),
        (["main", "child"], FORK - 1, FORK + 2),
        (["main", "main"], FORK, FORK + 2),
        (["main", "child", "third"], FORK, FORK + 2),
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
        replay_frames(store, workspace, "p", "r", ["main"], FORK, FORK + 1)


def test_range_http_adapter_and_parameter_validation(replay):
    from sheltersim.web.projects import ProjectAPI

    store, _, _ = replay
    api = ProjectAPI(store, recover=False)
    parts = ["p", "runs", "r", "replay-frames"]
    try:
        result = api.get(
            parts, {"branch_ids": ["main"], "start": [str(FORK)], "end": [str(FORK + 1)]}
        )
        assert [frame["minute"] for frame in result["samples"]] == [FORK, FORK + 1]
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
    expected = timeline.frame("main", COMPUTED)

    def unexpected(*args, **kwargs):
        raise AssertionError("an exact checkpoint read must not initialize policies")

    monkeypatch.setattr(module, "OfflineSimulationController", unexpected)
    batch = replay_frames(store, workspace, "p", "r", ["main"], COMPUTED, COMPUTED)
    assert batch["samples"] == [expected]
