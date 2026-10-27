"""What an Agent knows about facilities decides whether its toilet need is served.

The knowledge itself (configuration, Checkpoint, older Scenarios) is checked in
tests/unit/domain/test_agent_dynamics.py.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _run(scenario: ScenarioSpec, output: Path):
    world = run_scenario(scenario, output, trace_level=TraceLevel.SUMMARY)
    applied = Counter(
        record.event.event_type
        for record in world.event_log.records
        if record.status.value == "applied"
    )
    return world, applied


def test_entry_orientation_and_arrival_discovery_enable_toilet_service(
    tmp_path: Path, single_agent_awareness_scenario
) -> None:
    facilities = ["toilet_1", "toilet_2", "toilet_3", "toilet_4"]
    informed = single_agent_awareness_scenario(["toilet_1"])
    uninformed = single_agent_awareness_scenario([])

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
