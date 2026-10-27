"""Runs with the research psychology models: a generated Scenario records the model,
high distress recovers, and social synchrony combines with natural recovery.

Recovery runs on a two-person fixture and, since #137, on the generated Scenario,
where staff support lowers distress during event handling (#159).

How the model is selected is checked in tests/unit/policies/test_registry.py, and
the policies one tick at a time in tests/unit/policies/test_psychology.py.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.distress_monitor import distress_monitor_summary
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter


def test_generated_research_scenario_runs_and_records_its_model(
    generated_scenario, tmp_path
) -> None:
    run_scenario(generated_scenario, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert manifest["policy_implementations"]["psychology"] == (
        INTEGRATED_PSYCHOLOGY_POLICY_VERSION
    )


def test_full_runner_recovers_initially_high_agents_and_records_online_recovery(
    tmp_path: Path,
) -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "integrated-high-distress-recovery"
    document["duration_minutes"] = 15
    document["profiles"] = document["profiles"][:2]
    document["scheduled_events"] = []
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.02,
        "baseline_distress_by_agent": {},
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
        "social": "expressiveness-channel-openness-v1",
    }
    document["model_configs"]["social_propagation"] = {
        "enabled": False,
        "max_contacts_per_agent": 0,
        "minimum_contact_minutes": 1,
        "stress_spread_per_minute": 0.0,
        "natural_recovery_per_minute": 0.0,
    }
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    for agent in world.agents.values():
        agent.stress = 0.8
    artifacts = RunArtifactWriter(
        scenario, tmp_path / "high-distress-recovery", trace_level=TraceLevel.OFF
    )
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)

    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    assert world.minute_index == scenario.duration_minutes
    assert all(0.0 <= agent.stress < 0.8 for agent in world.agents.values())
    assert all(math.isfinite(agent.stress) for agent in world.agents.values())
    monitor = distress_monitor_summary(world.metric_state)
    assert monitor["completed_recovery_count"] == len(world.agents)
    assert monitor["mean_completed_recovery_minutes"] > 0


# An hour lets every resident fall below the high-distress threshold.
RECOVERY_MINUTES = 60


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#159: a fall below the threshold during event handling is not counted",
)
def test_full_runner_records_every_recovery_of_a_generated_scenario(
    tmp_path: Path, generated_scenario
) -> None:
    # Unlike the fixture above, staff here walk to residents and their support lowers
    # distress while events are handled, outside the psychology update (#137).
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = RECOVERY_MINUTES
    document["model_configs"]["social_propagation"]["enabled"] = False
    scenario = ScenarioSpec.model_validate(document)
    threshold = scenario.model_configs["psychology"]["high_distress_threshold"]
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    for agent in world.agents.values():
        agent.stress = 0.8
    artifacts = RunArtifactWriter(
        scenario, tmp_path / "high-distress-recovery", trace_level=TraceLevel.OFF
    )
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)

    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    assert all(agent.stress < threshold for agent in world.agents.values())
    monitor = distress_monitor_summary(world.metric_state)
    assert monitor["ongoing_high_distress_episode_count"] == 0
    assert monitor["completed_recovery_count"] == len(world.agents)


def test_full_runner_combines_social_synchrony_with_natural_recovery(
    tmp_path: Path, research_psychology_scenario
) -> None:
    scenario = research_psychology_scenario(duration=4, social=True)
    document = scenario.model_dump(mode="json")
    document["model_configs"]["policy_registry"]["psychology"] = "mean-reverting-life-state-v1"
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    high_id, low_id = sorted(world.agents)
    world.agents[high_id].stress = 0.8
    world.agents[low_id].stress = 0.1
    run_dir = tmp_path / "social-natural-recovery"
    artifacts = RunArtifactWriter(scenario, run_dir, trace_level=TraceLevel.FULL)
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)

    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    traces = [
        json.loads(line)
        for line in (run_dir / "psychology_contributions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    first_tick = next(row for row in traces if row["minute_index"] == 0)
    first_agents = {row["agent_id"]: row for row in first_tick["agents"]}
    assert first_agents[high_id]["stress"]["social_delta"] < 0.0
    assert first_agents[low_id]["stress"]["social_delta"] > 0.0
    for row in first_agents.values():
        stress = row["stress"]
        assert stress["applied_delta"] == pytest.approx(
            stress["psychology_clipped_delta"] + stress["social_delta"]
        )
    assert world.agents[high_id].stress < 0.8
    assert world.agents[low_id].stress < 0.8
    assert world.agents[low_id].stress > 0.1
