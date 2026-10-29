"""H05 operational scenarios: a seven-day combined Run keeps its invariants, and
paired comparisons extract event windows and refuse changed profiles or models.

That the six scenario files stay frozen is checked in
tests/unit/repo/test_scenario_fixtures.py.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from support.paths import SCENARIOS

from sheltersim.core.engine import TraceLevel
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY, validate_resource_ledger
from sheltersim.experiments.comparison import (
    compare_run_artifacts,
    render_comparison_markdown,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _load(name: str) -> ScenarioSpec:
    return ScenarioSpec.load(SCENARIOS / f"{name}.json")


def _short_scenario(name: str, *, initial_food: int | None = None) -> ScenarioSpec:
    document = _load(name).model_dump(mode="json")
    document["duration_minutes"] = 180
    if initial_food is not None:
        document["initial_stock"]["food_meal_units"] = initial_food
    for event in document["scheduled_events"]:
        if event["event_type"] == "resource_delivery":
            event["due_minute"] = 20
        elif event["event_type"] == "delivery_delayed":
            event["due_minute"] = 10
            event["payload"]["to_minute"] = 100
        elif event["event_type"] == "water_outage":
            event["due_minute"] = 60
        elif event["event_type"] == "water_restored":
            event["due_minute"] = 80
    return ScenarioSpec.model_validate(document)


@pytest.mark.long_simulation
def test_100_agent_seven_day_combined_scenario_preserves_operational_invariants(
    tmp_path: Path,
) -> None:
    scenario = _load("multi_operational")
    output = tmp_path / "multi-operational"

    world = run_scenario(scenario, output, trace_level=TraceLevel.SUMMARY)

    assert world.minute_index == 10080
    assert len(world.agents) == 100
    world.validate()
    ledger = world.model_state[RESOURCE_LEDGER_KEY]
    validate_resource_ledger(ledger, world.resources, set(world.agents), set(world.zones))
    assert all(account["remaining"] >= 0 for account in ledger["accounts"].values())
    assert all(
        account["reserved"] <= account["remaining"] for account in ledger["accounts"].values()
    )

    event_records = {record.event.event_id: record for record in world.event_log.records}
    assert event_records["delivery-delay-day1"].status.value == "applied"
    assert event_records["water-outage-day4"].status.value == "applied"
    delayed_delivery = event_records["shipment-meals-day2"]
    assert delayed_delivery.realized_minute == 5760
    assert delayed_delivery.event.causation_id == "delivery-delay-day1"
    stalls = world.model_state["resource_service_v1"]["queue_stall_history"]
    assert any(item["reason"] == "water_outage" for item in stalls)
    assert any(item["reason"] == "stock" for item in stalls)

    with (output / "operational_kpis.csv").open(encoding="utf-8", newline="") as stream:
        kpis = list(csv.DictReader(stream))
    assert len(kpis) == 10080
    assert {
        "minute_index",
        "hunger_need_mean",
        "hydration_need_mean",
        "water_queue_length",
        "meal_queue_length",
        "water_service_wait_mean_minutes",
        "food_meal_units_remaining",
        "staff_load_ratio",
        "stress_mean",
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
    } <= set(kpis[0])

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_mode"] == "offline"
    assert manifest["scenario_input_source_ids"]["facility_capacity"] == [
        "ASM-H05-OPERATIONAL-CAPACITY-V1"
    ]
    assert manifest["scheduled_event_source_ids"]
    assert any(
        facility["id"] == "water_station_main" and facility["capacity"] == 1
        for facility in manifest["facilities"]
    )
    initial_profiles = json.loads((output / "agents_initial.json").read_text(encoding="utf-8"))[
        "profiles"
    ]
    planned_events = json.loads((output / "events_planned.json").read_text(encoding="utf-8"))[
        "events"
    ]
    assert len(initial_profiles) == 100
    assert {event["id"] for event in planned_events} >= {
        "delivery-delay-day1",
        "water-outage-day4",
    }


def test_comparison_pairs_event_schedule_variants_and_extracts_event_windows(
    tmp_path: Path,
) -> None:
    baseline = _short_scenario("baseline_multi_operational", initial_food=5)
    candidate = _short_scenario("delivery_delay", initial_food=5)
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"

    run_scenario(baseline, baseline_dir, trace_level=TraceLevel.SUMMARY)
    run_scenario(candidate, candidate_dir, trace_level=TraceLevel.SUMMARY)

    report = compare_run_artifacts(baseline_dir, candidate_dir)

    assert report["seed"] == 2026
    assert report["scenario_family_hash"]
    assert report["scenario_definition_hash"] != report["candidate_scenario_definition_hash"]
    assert "not statistical or causal proof" in report["interpretation"]
    event = next(
        window
        for window in report["event_windows"]["candidate"]
        if window["event_id"] == "delivery-delay-day1"
    )
    assert event["source_id"] == "ASM-H05-OPERATIONAL-SCHEDULE-V1"
    assert event["realized_status"] == "applied"
    assert event["kpi_before"] is not None and event["kpi_after"] is not None
    assert event["representative_agent_logs"]["after"]
    markdown = render_comparison_markdown(report)
    assert "Scenario family hash" in markdown
    assert "delivery-delay-day1" in markdown


def test_comparison_rejects_changed_profile_or_model_family(tmp_path: Path) -> None:
    baseline = _short_scenario("baseline_multi_operational")
    changed_document = baseline.model_dump(mode="json")
    changed_document["initial_stock"]["food_meal_units"] += 1
    changed = ScenarioSpec.model_validate(changed_document)
    paths = []
    for scenario, name in ((baseline, "baseline"), (changed, "changed")):
        destination = tmp_path / name
        run_scenario(scenario, destination, trace_level=TraceLevel.OFF)
        paths.append(destination)

    try:
        compare_run_artifacts(*paths)
    except ValueError as exc:
        assert "scenario family" in str(exc)
    else:
        raise AssertionError("comparison accepted a different initial inventory")
