"""No-op Checkpoint forks run independently and replay the uninterrupted Run exactly.

Intervention commands and Branch lineage are checked in
tests/unit/experiments/test_branch.py.
"""

from __future__ import annotations

from pathlib import Path

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TickPhase
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[2] / "fixtures" / "scenarios" / "baseline_100_7d.json"


def test_noop_checkpoint_forks_are_independent_and_replay_identically() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    checkpoint = Checkpoint.capture(world, metrics=MetricsRecorder())
    left = BranchRuntime.from_checkpoint(checkpoint, branch_id="left")
    right = BranchRuntime.from_checkpoint(checkpoint, branch_id="right")

    assert left.world is not right.world
    assert left.world.agents["A001"] is not right.world.agents["A001"]
    assert left.pairing_key == right.pairing_key
    assert CounterRNG(left.world.seed).random("agent", "A001", 12) == CounterRNG(
        right.world.seed
    ).random("agent", "A001", 12)

    for branch in (left, right):

        def capture_metrics(phase, state, minute, rng):
            del rng
            if phase is TickPhase.COLLECT_METRICS:
                branch.metrics.capture(state, minute=minute)

        TickEngine(branch.world, phase_handler=capture_metrics).run(steps=10)

    assert left.world.canonical_json() == right.world.canonical_json()
    assert left.world.event_log.records == right.world.event_log.records
    assert left.metrics.to_dict() == right.metrics.to_dict()


