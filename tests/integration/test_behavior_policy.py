"""A Run with a registered behavior policy variant replays the same from a Checkpoint
and records the variant in its decision trace.

The first test swaps the variant of scenario_100_agents. The second is what an
operator gets by choosing the priority rule on the Project screen: a generated
Scenario whose people walk on the floor plan and decide with the autonomy-aware
version of the rule (#137).

The policies one decision at a time are checked in tests/unit/policies/test_behavior.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.inspection.traces import RunArtifactWriter


def test_priority_policy_checkpoint_noop_fork_replays_with_same_variant(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS).with_behavior_policy_variant(
        "priority-rule-v1"
    )
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


# People walk on the floor plan at this minute, so the fork carries their trips.
GENERATED_FORK_MINUTE = 30


def test_priority_policy_chosen_on_the_project_screen_replays_from_a_mid_trip_fork(
    tmp_path: Path, generated_previews
) -> None:
    preview = generated_previews(behavior_policy_id="priority-rule-v1")
    assert preview["can_start"] is True
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    assert scenario.model_configs["behavior"]["policy"] == "priority-rule-v1"
    root = BranchRuntime.from_world(WorldState.from_scenario(scenario), branch_id="source")
    writer = RunArtifactWriter(scenario, tmp_path / "source", trace_level=TraceLevel.FULL)
    writer.write_initial(root.world, execution_mode="offline")
    TickEngine(root.world, phase_handler=OfflineSimulationController(root.world, writer)).run(
        steps=GENERATED_FORK_MINUTE
    )
    writer.finalize(root.world)
    assert root.world.model_state[MOVEMENT_STATE_KEY]["journeys"]
    checkpoint = root.checkpoint()

    branches = [
        BranchRuntime.from_checkpoint(checkpoint, branch_id=branch_id)
        for branch_id in ("continued", "forked")
    ]
    for branch in branches:
        controller = OfflineSimulationController(branch.world, _MetricSink(branch.metrics))
        TickEngine(branch.world, phase_handler=controller).run(steps=45)

    continued, forked = branches
    assert continued.world.canonical_json() == forked.world.canonical_json()
    assert continued.world.event_log.records == forked.world.event_log.records
    assert continued.metrics.to_dict() == forked.metrics.to_dict()
    decisions = [
        json.loads(line)
        for line in (tmp_path / "source" / "decisions.jsonl").read_text().splitlines()
    ]
    assert decisions
    # Unlike the first test's priority-rule-v1, the generated Scenario's people
    # have autonomy, so the rule records its autonomy-aware version.
    assert {decision["policy_version"] for decision in decisions} == {
        "priority-rule-autonomy-v2-resource-service-v1"
    }
    assert {decision["source_id"] for decision in decisions} == {"ASM-PRIORITY-RULE-BEHAVIOR-V1"}
