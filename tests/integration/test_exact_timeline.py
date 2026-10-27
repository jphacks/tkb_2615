"""Seeking a minute between saved Checkpoints, and a no-op fork there, are exact.

On the shared generated Scenario, so the replay from the saved Checkpoint walks
people on the floor plan, as the Project screen's seek does. Until #137 this ran
on scenario_100_agents, which has neither a floor plan nor facility awareness.
"""

import json

import pytest

from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import TimelineError, TimelineStore, _MetricSink
from sheltersim.storage.projects import ProjectStore, digest

CHECKPOINT_INTERVAL = 20
# Between the Checkpoints at 20 and 40, while people walk on the floor plan.
SEEK_MINUTE = 28
COMPUTED_UNTIL = 45


def test_non_checkpoint_seek_and_noop_fork_are_exact(tmp_path, generated_scenario):
    scenario = generated_scenario
    store = ProjectStore(tmp_path)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    timeline.record(runtime)
    timeline.advance("main", COMPUTED_UNTIL, checkpoint_interval=CHECKPOINT_INTERVAL)
    reference = WorldState.from_scenario(scenario)
    TickEngine(
        reference,
        phase_handler=OfflineSimulationController(reference, _MetricSink(MetricsRecorder())),
    ).run(steps=SEEK_MINUTE)
    assert reference.model_state[MOVEMENT_STATE_KEY]["journeys"]
    assert timeline.seek("main", SEEK_MINUTE).world.canonical_json() == reference.canonical_json()
    parent_before = store.load_branch("p", "r", "main")[1].to_json()
    timeline.fork("main", SEEK_MINUTE, "child")
    child = timeline.advance("child", COMPUTED_UNTIL)
    assert (
        child.world.canonical_json() == timeline.seek("main", COMPUTED_UNTIL).world.canonical_json()
    )
    assert store.load_branch("p", "r", "main")[1].to_json() == parent_before
    with pytest.raises(TimelineError, match="computed range"):
        timeline.seek("main", COMPUTED_UNTIL + 1)
