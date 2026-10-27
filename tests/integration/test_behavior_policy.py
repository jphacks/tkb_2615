"""A Run with a registered behavior policy variant replays the same from a Checkpoint
and records the variant in its decision trace.

The policies one decision at a time are checked in tests/unit/policies/test_behavior.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter

FIXTURE = Path(__file__).parents[2] / "fixtures" / "scenario_100_agents.json"


def test_priority_policy_checkpoint_noop_fork_replays_with_same_variant(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(FIXTURE).with_behavior_policy_variant("priority-rule-v1")
    root = BranchRuntime.from_world(WorldState.from_scenario(scenario), branch_id="source")
    checkpoint = root.checkpoint()
    uninterrupted = BranchRuntime.from_checkpoint(checkpoint, branch_id="continued")
    forked = BranchRuntime.from_checkpoint(checkpoint, branch_id="forked")
    outputs = []

    for index, branch in enumerate((uninterrupted, forked)):
        writer = RunArtifactWriter(
            scenario,
            tmp_path / f"branch-{index}",
            trace_level=TraceLevel.FULL,
        )
        writer.write_initial(branch.world, execution_mode="offline")
        controller = OfflineSimulationController(branch.world, writer)
        TickEngine(branch.world, phase_handler=controller).run(steps=75)
        writer.finalize(branch.world)
        outputs.append((branch, writer))

    assert outputs[0][0].world.canonical_json() == outputs[1][0].world.canonical_json()
    assert outputs[0][0].world.event_log.records == outputs[1][0].world.event_log.records
    assert outputs[0][1].metrics.to_dict() == outputs[1][1].metrics.to_dict()
    trace = json.loads((tmp_path / "branch-0" / "decisions.jsonl").read_text().splitlines()[0])
    assert trace["policy_version"] == "priority-rule-v1"
    assert trace["source_id"] == "ASM-PRIORITY-RULE-BEHAVIOR-V1"
