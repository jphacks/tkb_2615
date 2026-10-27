"""Seeking a minute between saved Checkpoints, and a no-op fork there, are exact."""

import json

import pytest

from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineError, TimelineStore, _MetricSink
from sheltersim.storage.projects import ProjectStore, digest


def test_non_checkpoint_seek_and_noop_fork_are_exact(tmp_path):
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store = ProjectStore(tmp_path)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    timeline.record(runtime)
    timeline.advance("main", 19, checkpoint_interval=10)
    reference = WorldState.from_scenario(scenario)
    TickEngine(
        reference,
        phase_handler=OfflineSimulationController(reference, _MetricSink(MetricsRecorder())),
    ).run(steps=13)
    assert timeline.seek("main", 13).world.canonical_json() == reference.canonical_json()
    parent_before = store.load_branch("p", "r", "main")[1].to_json()
    timeline.fork("main", 13, "child")
    child = timeline.advance("child", 19)
    assert child.world.canonical_json() == timeline.seek("main", 19).world.canonical_json()
    assert store.load_branch("p", "r", "main")[1].to_json() == parent_before
    with pytest.raises(TimelineError, match="computed range"):
        timeline.seek("main", 20)
