"""Offline deterministic Runs: a seven-day scenario, repeatable traces, a midpoint
Checkpoint, and the pinned small replay contract.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).parents[2]
FAILURE_SCENARIO = ROOT / "fixtures" / "scenarios" / "failure_day3.json"
H00_SMALL_REPLAY = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
REGRESSION_CONTRACT = ROOT / "fixtures" / "regression-contract.json"
EXPECTED_ARTIFACTS = {
    "manifest.json",
    "agents_initial.json",
    "events_planned.json",
    "events_realized.jsonl",
    "decisions.jsonl",
    "metrics.csv",
    "llm_calls.jsonl",
    "social_decisions.jsonl",
    "social_contacts.jsonl",
    "zones.json",
}


@pytest.mark.long_simulation
def test_100_agents_seven_day_failure_scenario_runs_offline(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(FAILURE_SCENARIO)
    output = tmp_path / "failure-day3-run"

    world = run_scenario(scenario, output, trace_level=TraceLevel.OFF)

    world.validate()
    assert world.minute_index == 10_080
    assert len(world.agents) == 100
    assert len(world.staff) == 5
    assert len(world.facilities) == 4
    assert {facility.working_units for facility in world.facilities.values()} == {0, 1}
    assert all(
        len(facility.active_agent_ids) <= facility.working_units
        and len(facility.queue_agent_ids) <= len(world.agents)
        for facility in world.facilities.values()
    )

    assert {path.name for path in output.iterdir()} == EXPECTED_ARTIFACTS
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_mode"] == "offline"
    assert manifest["run_status"] == "completed"
    assert manifest["final_minute"] == 10_080
    assert manifest["metric_snapshot_count"] == 10_080
    assert manifest["world_state_schema_version"] == world.schema_version
    assert manifest["scenario_schema_version"] == scenario.schema_version == 3
    assert (
        manifest["final_state_hash"]
        == hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()
    )
    assert "ASM-DAY3-TOILET-OUTAGE-V1" in manifest["scenario_provenance"]["model_assumption_ids"]

    profiles = json.loads((output / "agents_initial.json").read_text(encoding="utf-8"))
    assert len(profiles["profiles"]) == 100
    assert len(profiles["profiles"]) == len(world.agents)

    with (output / "metrics.csv").open(encoding="utf-8", newline="") as stream:
        metrics = list(csv.DictReader(stream))
    assert len(metrics) == 10_080
    assert metrics[0]["minute_index"] == "0"
    assert metrics[-1]["minute_index"] == "10079"
    assert metrics[0]["toilet_working_units"] == "4"
    assert metrics[2_880]["toilet_working_units"] == "2"

    realized = [
        json.loads(line)
        for line in (output / "events_realized.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    outages = {
        record["event"]["event_id"]: record
        for record in realized
        if record["event"]["event_id"].startswith("day3-toilet")
    }
    assert set(outages) == {"day3-toilet3-outage", "day3-toilet4-outage"}
    assert all(record["realized_minute"] == 2_880 for record in outages.values())
    assert all(record["status"] == "applied" for record in outages.values())


def test_short_offline_run_has_repeatable_state_and_event_trace(tmp_path: Path) -> None:
    document = json.loads(FAILURE_SCENARIO.read_text(encoding="utf-8"))
    document["id"] = "repeatability-check"
    document["duration_minutes"] = 30
    document["scheduled_events"] = []
    scenario = ScenarioSpec.model_validate(document)

    first = run_scenario(scenario, tmp_path / "first", trace_level=TraceLevel.OFF)
    second = run_scenario(scenario, tmp_path / "second", trace_level=TraceLevel.SUMMARY)

    assert first.canonical_json() == second.canonical_json()
    first_events = (tmp_path / "first" / "events_realized.jsonl").read_bytes()
    second_events = (tmp_path / "second" / "events_realized.jsonl").read_bytes()
    assert first_events == second_events
    assert (tmp_path / "first" / "metrics.csv").read_bytes() == (
        tmp_path / "second" / "metrics.csv"
    ).read_bytes()
    first_manifest = json.loads((tmp_path / "first" / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads(
        (tmp_path / "second" / "manifest.json").read_text(encoding="utf-8")
    )
    assert first_manifest["final_state_hash"] == second_manifest["final_state_hash"]
    assert (
        first_manifest["final_state_hash"]
        == hashlib.sha256(first.canonical_json().encode("utf-8")).hexdigest()
    )


def test_run_checkpoint_callback_captures_exact_midpoint_without_changing_run(
    tmp_path: Path,
) -> None:
    scenario = ScenarioSpec.load(H00_SMALL_REPLAY).model_copy(
        update={"id": "checkpoint-callback-check"}
    )
    checkpoint_path = tmp_path / "midpoint.json"
    callback_minutes: list[int] = []

    def save_checkpoint(world: Any, metrics: Any, router: Any) -> None:
        assert router is None
        assert hasattr(world, "minute_index")
        checkpoint = Checkpoint.capture(world, metrics=metrics)
        checkpoint.save(checkpoint_path)
        callback_minutes.append(checkpoint.simulation_minute)

    checkpointed = run_scenario(
        scenario,
        tmp_path / "checkpointed",
        trace_level=TraceLevel.OFF,
        checkpoint_at_minute=20,
        checkpoint_callback=save_checkpoint,
    )
    uninterrupted = run_scenario(
        scenario, tmp_path / "uninterrupted", trace_level=TraceLevel.OFF
    )

    assert callback_minutes == [20]
    checkpoint = Checkpoint.load(checkpoint_path)
    assert checkpoint.simulation_minute == 20
    assert len(checkpoint.restore_metrics().snapshots) == 20
    assert checkpoint.restore_world().minute_index == 20
    assert checkpointed.canonical_json() == uninterrupted.canonical_json()


def test_h00_small_replay_matches_pinned_state_events_and_provenance(tmp_path: Path) -> None:
    contract = json.loads(REGRESSION_CONTRACT.read_text(encoding="utf-8"))["small_replay"]
    scenario = ScenarioSpec.load(H00_SMALL_REPLAY)
    scenario_hash = hashlib.sha256(scenario.canonical_json().encode("utf-8")).hexdigest()

    assert scenario.id == contract["scenario_id"]
    assert scenario.schema_version == contract["scenario_schema_version"]
    assert scenario.seed == contract["seed"]
    assert scenario.duration_minutes == contract["duration_minutes"]
    assert len(scenario.profiles) == contract["profile_count"]
    assert scenario.provenance["profile_generation"]["profile_count"] == len(scenario.profiles)
    assert scenario.provenance["profile_generation"]["seed"] == scenario.seed
    assert scenario_hash == contract["scenario_sha256"]
    assert [event.id for event in scenario.scheduled_events] == contract["planned_event_ids"]

    output = tmp_path / "h00-small-replay"
    world = run_scenario(scenario, output, trace_level=TraceLevel.OFF)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    realized = [
        json.loads(line)
        for line in (output / "events_realized.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    final_hash = hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()

    assert world.schema_version == contract["world_state_schema_version"] == 5
    assert manifest["run_status"] == "completed"
    assert manifest["scenario_hash"] == scenario_hash
    assert manifest["final_state_hash"] == contract["expected_final_state_sha256"] == final_hash
    assert [record["event"]["event_id"] for record in realized] == contract["realized_event_ids"]
    assert (
        sorted({record["event"]["provenance"]["source_type"] for record in realized})
        == (contract["event_source_categories"])
    )
    assert (
        sorted({record["event"]["provenance"]["source_id"] for record in realized})
        == (contract["event_source_ids"])
    )
    assert all(record["status"] in {"applied", "suppressed"} for record in realized)
    policy_versions = {
        name: assumption.get("policy_version", assumption.get("generator"))
        for name, assumption in manifest["model_assumptions"].items()
        if "policy_version" in assumption or "generator" in assumption
    }
    baseline_contract = json.loads(REGRESSION_CONTRACT.read_text(encoding="utf-8"))[
        "baseline_100_7d"
    ]
    assert policy_versions == baseline_contract["model_policy_versions"]
