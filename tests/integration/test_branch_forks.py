"""No-op Checkpoint forks run independently and replay the uninterrupted Run exactly.

On the shared generated Scenario, forked while people and staff walk on the floor
plan, as the Project screen forks a Run. Until #137 this forked baseline_100_7d at
minute 0, which has neither a floor plan nor facility awareness.

Intervention commands and Branch lineage are checked in
tests/unit/experiments/test_branch.py.
"""

from __future__ import annotations

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

# People walk to and queue at the toilets, and staff walk to a person, at this minute.
FORK_MINUTE = 40


def _engine(world: WorldState, metrics: MetricsRecorder) -> TickEngine:
    """The controller the Project screen runs, recording metrics into metrics."""
    return TickEngine(world, phase_handler=OfflineSimulationController(world, _MetricSink(metrics)))


def test_noop_checkpoint_forks_are_independent_and_replay_identically(generated_scenario) -> None:
    world, metrics = WorldState.from_scenario(generated_scenario), MetricsRecorder()
    uninterrupted = _engine(world, metrics)
    uninterrupted.run(steps=FORK_MINUTE)
    assert world.model_state[MOVEMENT_STATE_KEY]["journeys"]

    checkpoint = Checkpoint.capture(world, metrics=metrics)
    left = BranchRuntime.from_checkpoint(checkpoint, branch_id="left")
    right = BranchRuntime.from_checkpoint(checkpoint, branch_id="right")
    agent_id = min(world.agents)

    assert left.world is not right.world
    assert left.world.agents[agent_id] is not right.world.agents[agent_id]
    left_movement = left.world.model_state[MOVEMENT_STATE_KEY]
    assert left_movement is not right.world.model_state[MOVEMENT_STATE_KEY]
    assert left.pairing_key == right.pairing_key
    assert CounterRNG(left.world.seed).random("agent", agent_id, 12) == CounterRNG(
        right.world.seed
    ).random("agent", agent_id, 12)

    right_before = right.world.canonical_json()
    _engine(left.world, left.metrics).run(steps=30)
    assert right.world.canonical_json() == right_before
    _engine(right.world, right.metrics).run(steps=30)
    uninterrupted.run(steps=30)

    assert left.world.canonical_json() == right.world.canonical_json() == world.canonical_json()
    assert left.world.event_log.records == right.world.event_log.records
    assert left.world.event_log.records == world.event_log.records
    assert left.metrics.to_dict() == right.metrics.to_dict() == metrics.to_dict()


def test_a_fork_at_a_saved_minute_takes_the_saved_checkpoint(
    tmp_path, monkeypatch, saved_timeline
) -> None:
    """The source restored from a saved Checkpoint and not advanced would capture the
    same Checkpoint again, so the child takes it; a fork between saves captures."""
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 60)
    saved = store.load_checkpoint(
        "p",
        "r",
        timeline._nearest_checkpoint("main", 30, store.branch_reference("p", "r", "main"))[1],
    )
    _, recaptured = timeline.seek("main", 30).fork_with_checkpoint("probe")
    assert recaptured.checkpoint_hash == saved.checkpoint_hash

    captures = []
    capture_restored = Checkpoint.capture_restored.__func__

    def counting(cls, *args, **kwargs):
        captures.append(1)
        return capture_restored(cls, *args, **kwargs)

    monkeypatch.setattr(Checkpoint, "capture_restored", classmethod(counting))
    child = timeline.fork("main", 30, "at-save")
    assert captures == []
    assert child.world.canonical_json() == timeline.seek("main", 30).world.canonical_json()
    origin = child.runtime_state["fork_origin"]
    assert (origin["checkpoint_hash"], origin["state_hash"]) == (
        saved.checkpoint_hash,
        saved.state_hash,
    )
    timeline.fork("main", 45, "between-saves")
    assert captures == [1]


def test_a_fork_keeps_no_copy_of_the_world_it_read(tmp_path, saved_timeline) -> None:
    """A fork reads its source once and hands the world to the child, so the store
    keeps no copy of it for later reads (#257)."""
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 60)
    timeline.fork("main", 30, "child")
    assert store._restored == {}


def test_a_fork_whose_index_write_fails_leaves_no_branch_and_can_be_retried(
    tmp_path, monkeypatch, saved_timeline
) -> None:
    """#288: the child's pointer is saved before its index rows; when indexing fails
    the pointer is removed, so the branch list and a retry under the same ID work."""
    import pytest

    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.storage.projects import StorageError

    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 60)
    ingest = HistoryIndex.ingest

    def full_disk(self, branch, checkpoint, **kwargs):
        if branch == "child":
            raise StorageError("storage_capacity_exceeded")
        return ingest(self, branch, checkpoint, **kwargs)

    monkeypatch.setattr(HistoryIndex, "ingest", full_disk)
    with pytest.raises(StorageError, match="storage_capacity_exceeded"):
        timeline.fork("main", 30, "child")
    assert [branch["branch_id"] for branch in store.branches("p", "r")] == ["main"]
    monkeypatch.setattr(HistoryIndex, "ingest", ingest)
    child = timeline.fork("main", 30, "child")
    assert child.world.minute_index == 30
    assert store.branch_reference("p", "r", "child").computed_until == 30
