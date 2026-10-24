"""Facility knowledge stays explicit and connects toilet choice to service completion."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.agent_dynamics import agent_knows_facility
from sheltersim.domain.simulator_state import simulator_signatures
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "scenario_100_agents.json"


def _single_agent_scenario(initial_known_facility_ids: list[str]) -> ScenarioSpec:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["id"] = "facility-awareness-toilet-integration"
    document["duration_minutes"] = 180
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:1]
    profile = document["profiles"][0]
    profile["initial_zone_id"] = "sleeping"
    profile["initial_needs"] = {
        "toilet_urgency": 0.65,
        "hunger": 0.45,
        "fatigue": 0.25,
    }
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
    }
    document["model_configs"]["facility_awareness"] = {
        "schema_version": 1,
        "initial_known_facility_ids": initial_known_facility_ids,
        "discover_co_located_facilities_on_arrival": True,
        "source_type": "assumption",
        "source_id": "ASM-FACILITY-ENTRY-ORIENTATION-V1",
    }
    return ScenarioSpec.model_validate(document)


def _run(scenario: ScenarioSpec, output: Path):
    world = run_scenario(scenario, output, trace_level=TraceLevel.SUMMARY)
    applied = Counter(
        record.event.event_type
        for record in world.event_log.records
        if record.status.value == "applied"
    )
    return world, applied


def test_entry_orientation_and_arrival_discovery_enable_toilet_service(tmp_path: Path) -> None:
    facilities = ["toilet_1", "toilet_2", "toilet_3", "toilet_4"]
    informed = _single_agent_scenario(["toilet_1"])
    uninformed = _single_agent_scenario([])

    informed_world, informed_events = _run(informed, tmp_path / "informed")
    uninformed_world, uninformed_events = _run(uninformed, tmp_path / "uninformed")

    assert informed_events["agent_travel_complete"] > 0
    assert informed_events["toilet_service_complete"] > 0
    assert uninformed_events["toilet_service_complete"] == 0
    dynamics = informed_world.model_state["agent_dynamics_v1"]["agents"]["A001"]
    assert set(dynamics["known_facility_ids"]) >= set(facilities)
    assert (
        informed_world.agents["A001"].needs.toilet_urgency
        < uninformed_world.agents["A001"].needs.toilet_urgency
    )
    hourly_trace = tmp_path / "informed" / "psychology_contributions_hourly.jsonl"
    rows = [json.loads(line) for line in hourly_trace.read_text(encoding="utf-8").splitlines()]
    first_hour = next(row for row in rows if row["agent_id"] == "A001")
    assert first_hour["sample_count"] == 60
    assert first_hour["component_totals"]["natural_recovery"] < 0.0
    manifest = json.loads((tmp_path / "informed" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_schema_version"] == 2
    assert manifest["psychology_hourly_agent_summary_count"] == len(rows)


def test_facility_knowledge_is_independent_of_psychology_selection() -> None:
    known_sets = []
    for psychology in ("mean-reverting-life-state-v1", "mean-reverting-distress-v1"):
        document = _single_agent_scenario(["toilet_1"]).model_dump(mode="json")
        document["model_configs"]["policy_registry"]["psychology"] = psychology
        world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
        known_sets.append(
            world.model_state["agent_dynamics_v1"]["agents"]["A001"]["known_facility_ids"]
        )
        assert agent_knows_facility(world, "A001", "toilet_1")
        assert not agent_knows_facility(world, "A001", "toilet_2")
    assert known_sets[0] == known_sets[1] == ["toilet_1"]


def test_awareness_config_is_versioned_and_checkpoint_safe() -> None:
    scenario = _single_agent_scenario(["toilet_1"])
    world = WorldState.from_scenario(scenario)
    restored = WorldState.from_dict(world.to_dict())
    assert restored.model_state["agent_dynamics_v1"] == world.model_state["agent_dynamics_v1"]
    assert agent_knows_facility(restored, "A001", "toilet_1")
    assert not agent_knows_facility(restored, "A001", "toilet_2")
    assert "operator:facility_awareness" in simulator_signatures(scenario.model_configs)


def test_unknown_initial_facility_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown facilities"):
        _single_agent_scenario(["facility-not-in-scenario"])


def test_legacy_scenario_without_awareness_keeps_prior_global_knowledge() -> None:
    document = _single_agent_scenario([]).model_dump(mode="json")
    document["model_configs"].pop("facility_awareness")
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    assert agent_knows_facility(world, "A001", "toilet_1")
