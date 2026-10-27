"""Water and meals served through the tick order: travel, queue, staffed service and
completion, with the same outputs at every trace level.

The service's requests, reservations, cancellations and checks are tested in
tests/unit/domain/test_resource_service.py.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.inspection.traces import RunArtifactWriter


@pytest.mark.parametrize(("agent_zone", "travel_expected"), [("sleeping", True), ("toilet", False)])
def test_behavior_fetch_water_runs_travel_queue_service_completion_and_artifacts(
    tmp_path: Path, agent_zone: str, travel_expected: bool, resource_service_scenario
) -> None:
    scenario = resource_service_scenario(duration=12, service_minutes=2)
    scenario.model_configs["behavior"]["temperature"] = 0.0001
    world = WorldState.from_scenario(scenario)
    for agent in world.agents.values():
        agent.next_decision_at = 100
        agent.needs.toilet_urgency = 0.0
        agent.needs.hunger = 0.1
        agent.needs.fatigue = 0.1
        agent.stress = 0.0
    agent = world.agents["A001"]
    agent.profile = replace(agent.profile, support_need=0.0)
    agent.zone_id = agent_zone
    agent.needs.hydration_need = 0.9
    agent.next_decision_at = 0

    output = tmp_path / f"resource-{agent_zone}"
    artifacts = RunArtifactWriter(scenario, output, trace_level=TraceLevel.FULL)
    artifacts.write_initial(world)
    controller = OfflineSimulationController(world, artifacts)
    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    resource_travel_events = [
        record
        for record in world.event_log.records
        if record.event.event_type == "agent_travel_complete"
        and record.event.payload.get("purpose") == "resource_request"
    ]
    assert bool(resource_travel_events) is travel_expected
    assert world.agents["A001"].needs.hydration_need == pytest.approx(0.79)
    water = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]
    assert water["served"] == 250
    assert water["reserved"] == 0
    decisions = [
        json.loads(line)
        for line in (output / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["agent_id"] == "A001" and record["action"] == "fetch_water" for record in decisions
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["world_state_schema_version"] == 6
    assert manifest["resource_service_summary"]["policy_version"] == "resource-service-v1"
    assert (
        manifest["model_assumptions"]["resource_service"]["source_id"]
        == "ASM-H02-RESOURCE-SERVICE-V1"
    )
    assert (
        json.loads((output / "resource_service.json").read_text(encoding="utf-8"))["schema_version"]
        == 1
    )


def test_behavior_collect_meal_consumes_food_and_reduces_hunger(
    tmp_path: Path, resource_service_scenario
) -> None:
    scenario = resource_service_scenario(duration=12, service_minutes=2)
    scenario.model_configs["behavior"]["temperature"] = 0.0001
    world = WorldState.from_scenario(scenario)
    for agent in world.agents.values():
        agent.next_decision_at = 100
        agent.needs.toilet_urgency = 0.0
        agent.needs.hunger = 0.1
        agent.needs.fatigue = 0.1
        agent.needs.hydration_need = 0.1
        agent.stress = 0.0
    agent = world.agents["A001"]
    agent.profile = replace(agent.profile, support_need=0.0)
    agent.needs.hunger = 0.95
    agent.next_decision_at = 0

    output = tmp_path / "meal-service"
    artifacts = RunArtifactWriter(scenario, output, trace_level=TraceLevel.FULL)
    artifacts.write_initial(world)
    controller = OfflineSimulationController(world, artifacts)
    TickEngine(world, phase_handler=controller).run()
    artifacts.finalize(world)

    food = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]
    assert food["served"] == 1
    assert food["remaining"] == 3
    assert food["reserved"] == 0
    assert world.agents["A001"].needs.hunger == pytest.approx(0.456)
    assert world.model_state[RESOURCE_LEDGER_KEY]["audit"][-1]["operation"] == "consume"
    decisions = [
        json.loads(line)
        for line in (output / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["agent_id"] == "A001" and record["action"] == "collect_meal" for record in decisions
    )


def test_resource_service_trace_off_and_summary_have_identical_outputs(
    tmp_path: Path, resource_service_scenario
) -> None:
    outputs: dict[TraceLevel, tuple[WorldState, Path]] = {}
    for trace_level in (TraceLevel.OFF, TraceLevel.SUMMARY):
        scenario = resource_service_scenario(duration=8, service_minutes=2)
        scenario.model_configs["behavior"]["temperature"] = 0.0001
        world = WorldState.from_scenario(scenario)
        for agent in world.agents.values():
            agent.next_decision_at = 100
            agent.needs.toilet_urgency = 0.0
            agent.needs.hunger = 0.1
            agent.needs.fatigue = 0.1
            agent.needs.hydration_need = 0.1
            agent.stress = 0.0
        agent = world.agents["A001"]
        agent.zone_id = world.facilities["water_station_1"].zone_id
        agent.profile = replace(agent.profile, support_need=0.0)
        agent.needs.hydration_need = 0.9
        agent.next_decision_at = 0

        output = tmp_path / trace_level.value
        artifacts = RunArtifactWriter(scenario, output, trace_level=trace_level)
        artifacts.write_initial(world)
        controller = OfflineSimulationController(world, artifacts)
        TickEngine(world, phase_handler=controller).run()
        artifacts.finalize(world)
        outputs[trace_level] = (world, output)

    off_world, off_path = outputs[TraceLevel.OFF]
    summary_world, summary_path = outputs[TraceLevel.SUMMARY]
    assert off_world.canonical_json() == summary_world.canonical_json()
    assert off_world.event_log.records == summary_world.event_log.records
    assert (off_path / "events_realized.jsonl").read_bytes() == (
        summary_path / "events_realized.jsonl"
    ).read_bytes()
    assert (off_path / "metrics.csv").read_bytes() == (summary_path / "metrics.csv").read_bytes()
    for artifact_name in ("resource_ledger.json", "resource_service.json"):
        assert (off_path / artifact_name).read_bytes() == (
            summary_path / artifact_name
        ).read_bytes()
