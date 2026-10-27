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
