"""The trace level changes how much a Run writes, not the simulation state hash or the
metrics and events a Run saves.
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.core.engine import TickEngine, TickPhase, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import BehaviorDecisionRunner, WorldView

FIXTURE = Path(__file__).parents[2] / "fixtures" / "scenario_100_agents.json"
SMALL_REPLAY = Path(__file__).parents[2] / "fixtures" / "scenarios" / "h00_small_replay.json"


def test_trace_level_changes_artifact_detail_not_simulation_hash(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(FIXTURE)

    def run(level: TraceLevel) -> tuple[str, list[str]]:
        world = WorldState.from_scenario(scenario)
        output_directory = tmp_path / level.value
        artifacts = RunArtifactWriter(scenario, output_directory, trace_level=level)
        artifacts.write_initial(world)
        policy_runner = BehaviorDecisionRunner()

        def observe(phase, state, minute, rng) -> None:
            if phase is TickPhase.PROPOSE_ACTIONS:
                for proposal in policy_runner.propose_due(WorldView.from_world(state), rng):
                    artifacts.record_decision(proposal)
            if phase is TickPhase.COLLECT_METRICS:
                artifacts.capture_metric(state, minute=minute)

        traces: list[object] = []
        engine = TickEngine(
            world,
            phase_handler=observe,
            trace_level=level,
            trace_sink=traces.append if level is not TraceLevel.OFF else None,
        )
        engine.run(steps=2)
        final_state = world.canonical_json()
        artifacts.finalize(world)
        decisions = (output_directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        return final_state, decisions

    off_state, off_decisions = run(TraceLevel.OFF)
    summary_state, summary_decisions = run(TraceLevel.SUMMARY)
    full_state, full_decisions = run(TraceLevel.FULL)

    assert off_state == summary_state == full_state
    assert off_decisions == []
    assert summary_decisions
    assert "candidates" not in json.loads(summary_decisions[0])
    assert "candidates" in json.loads(full_decisions[0])


def test_same_seed_small_scenario_has_identical_canonical_outputs(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(SMALL_REPLAY)
    first = run_scenario(scenario, tmp_path / "first", trace_level=TraceLevel.OFF)
    second = run_scenario(scenario, tmp_path / "second", trace_level=TraceLevel.SUMMARY)

    assert first.canonical_json() == second.canonical_json()
    for filename in ("metrics.csv", "events_realized.jsonl"):
        assert (tmp_path / "first" / filename).read_bytes() == (
            tmp_path / "second" / filename
        ).read_bytes()
    manifests = [
        json.loads((tmp_path / name / "manifest.json").read_text(encoding="utf-8"))
        for name in ("first", "second")
    ]
    assert manifests[0]["final_state_hash"] == manifests[1]["final_state_hash"]
    assert manifests[0]["scenario_hash"] == manifests[1]["scenario_hash"]
