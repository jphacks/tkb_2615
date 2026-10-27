"""Generated Scenarios get standard facilities, station staff, stock and resupply."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

import pytest

from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.region.conditions import STANDARD_PROVISIONING_SOURCE_ID
from sheltersim.region.facility import (
    SERVICE_STATION_SOURCE_ID,
    SYNTHETIC_STAFFING_SOURCE_ID,
    SYNTHETIC_TOILET_SOURCE_ID,
    standard_service_counts,
)

ROOT = Path(__file__).parents[1]


def _draft(evacuees: int, duration_days: float = 1, **overrides: object):
    return InitialScenarioBuilder().build(
        InitialScenarioRequest(
            municipality="Synthetic municipality",
            disaster="earthquake",
            evacuees=evacuees,
            duration_days=duration_days,
            seed=7,
            **overrides,
        )
    )


def _kinds(scenario) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for facility in scenario.facilities:
        grouped.setdefault(facility.kind, []).append(facility)
    return grouped


def _deliveries(scenario) -> list:
    return sorted(
        (event for event in scenario.scheduled_events if event.event_type == "resource_delivery"),
        key=lambda event: (event.due_minute, event.payload["resource_id"]),
    )


def test_standard_counts_take_the_larger_of_the_standard_and_the_demand() -> None:
    assert standard_service_counts(10) == {
        "toilet_standard": 1,
        "toilet_demand": 1,
        "water_station": 1,
        "meal_distribution": 1,
    }
    # 100 people: 1 per 50 gives 2 toilets, but 500 uses x 6 minutes needs 4 at 0.8 load.
    assert standard_service_counts(100) == {
        "toilet_standard": 2,
        "toilet_demand": 4,
        "water_station": 4,
        "meal_distribution": 1,
    }


def test_generated_facilities_follow_the_standard_counts() -> None:
    scenario = _draft(100).scenario
    kinds = _kinds(scenario)
    assert len(kinds["toilet"]) == 4
    assert {item.id for item in kinds["water_station"]} == {
        "water_station_main",
        "water_station_02",
        "water_station_03",
        "water_station_04",
    }
    assert {item.id for item in kinds["meal_distribution"]} == {"meal_distribution_main"}
    assert {item.zone_id for item in kinds["water_station"] + kinds["meal_distribution"]} == {
        "food"
    }
    # Entered accessible toilets still count on top of the planning standard.
    assert len(_kinds(_draft(100, accessible_toilet_count=3).scenario)["toilet"]) == 5


def test_each_station_gets_a_station_staff_member_in_its_zone() -> None:
    scenario = _draft(100).scenario
    general = [member for member in scenario.staff if member.zone_id == "staff"]
    station = [member for member in scenario.staff if member.zone_id == "food"]
    assert len(general) == 1
    assert "support" in general[0].skills
    assert len(station) == 5
    assert all(set(member.skills) == {"water_service", "meal_service"} for member in station)


@pytest.mark.parametrize(
    ("entered", "general", "station"), [(3, 1, 2), (1, 1, 0), (0, 0, 0), (20, 15, 5)]
)
def test_an_entered_staff_total_keeps_one_general_staff_member(
    entered: int, general: int, station: int
) -> None:
    draft = _draft(100, staff_count=entered)
    staff = draft.scenario.staff
    assert sum(member.zone_id == "staff" for member in staff) == general
    assert sum(member.zone_id == "food" for member in staff) == station
    record = next(
        item
        for item in draft.scenario.provenance["assumptions"]
        if item["source_id"] == SYNTHETIC_STAFFING_SOURCE_ID
    )
    assert record["value"]["entered_total"] == entered


def test_unknown_inventory_gets_three_days_of_standard_stock() -> None:
    scenario = _draft(30).scenario
    assert scenario.initial_stock.water_ml == 3_000 * 30 * 3
    assert scenario.initial_stock.food_meal_units == 3 * 30 * 3
    assert scenario.initial_stock.source_ids == (STANDARD_PROVISIONING_SOURCE_ID,)
    assert "resource_service" in scenario.model_configs
    # A one-day Run ends before supply starts on day 4.
    assert _deliveries(scenario) == []
    record = next(
        item
        for item in scenario.provenance["assumptions"]
        if item["id"] == STANDARD_PROVISIONING_SOURCE_ID
    )
    assert record["value"]["supply_scheduled"] is False


def test_daily_supply_starts_on_day_four_by_default() -> None:
    deliveries = _deliveries(_draft(30, duration_days=7).scenario)
    assert sorted({event.due_minute for event in deliveries}) == [4320, 5760, 7200, 8640]
    assert {(event.payload["resource_id"], event.payload["quantity"]) for event in deliveries} == {
        ("water_ml", 90_000),
        ("food_meal_units", 90),
    }
    assert all(event.payload["shipment_id"].startswith("region-shipment-") for event in deliveries)


def test_supply_start_day_can_be_changed() -> None:
    deliveries = _deliveries(_draft(30, duration_days=3, default_supply_start_day=2).scenario)
    assert sorted({event.due_minute for event in deliveries}) == [1440, 2880]


def test_long_runs_deliver_every_few_days_within_the_event_limit() -> None:
    draft = _draft(100, duration_days=90)
    deliveries = _deliveries(draft.scenario)
    minutes = sorted({event.due_minute for event in deliveries})
    interval_days = (minutes[1] - minutes[0]) // 1440
    assert interval_days > 1
    assert 2 * math.ceil(87 / interval_days) == len(deliveries) <= 64
    water = next(event for event in deliveries if event.payload["resource_id"] == "water_ml")
    assert water.payload["quantity"] == 3_000 * 100 * interval_days
    assert not draft.approval_blockers


def test_entered_inventory_is_never_replaced() -> None:
    draft = _draft(
        30,
        duration_days=7,
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "water_ml": 1_000,
                "food_meal_units": 2,
                "source_kind": "operator",
                "source_ids": ["SRC-OPERATOR-INITIAL-CONDITIONS"],
            }
        },
    )
    assert draft.scenario.initial_stock.water_ml == 1_000
    assert _deliveries(draft.scenario) == []
    assert not any(
        item["id"] == STANDARD_PROVISIONING_SOURCE_ID
        for item in draft.scenario.provenance["assumptions"]
    )


def test_provisioning_assumptions_and_sources_are_registered() -> None:
    manifest = json.loads((ROOT / "fixtures" / "source-manifest.json").read_text("utf-8"))
    assumptions = {item["id"]: item for item in manifest["assumptions"]}
    sources = {item["id"] for item in manifest["sources"]}
    for source_id in (
        STANDARD_PROVISIONING_SOURCE_ID,
        SYNTHETIC_STAFFING_SOURCE_ID,
        SYNTHETIC_TOILET_SOURCE_ID,
        SERVICE_STATION_SOURCE_ID,
    ):
        assert source_id in assumptions
        assert set(assumptions[source_id]["context_sources"]) <= sources
    values = assumptions[STANDARD_PROVISIONING_SOURCE_ID]["values"]
    assert values["drinking_water_ml_per_person_day"] == 3_000
    assert values["stock_days"] == 3
    assert values["supply_start_day_default"] == 4


def test_a_pending_region_delivery_does_not_stop_the_run(tmp_path) -> None:
    # Region-condition deliveries lacked a shipment ID, so no operational-event state
    # was created and the first validation with a pending delivery stopped the Run.
    draft = _draft(
        10,
        duration_days=0.1,
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "water_ml": 90_000,
                "food_meal_units": 90,
                "source_kind": "operator",
                "source_ids": ["SRC-OPERATOR-INITIAL-CONDITIONS"],
            },
            "scheduled": [
                {
                    "kind": "resource_delivery",
                    "resource_id": "water_ml",
                    "quantity": 1_000,
                    "due_minute": 60,
                },
            ],
        },
    )
    run_scenario(draft.scenario, tmp_path)
    realized = [
        json.loads(line)["event"]
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
    ]
    delivery = next(event for event in realized if event["event_type"] == "resource_delivery")
    assert delivery["due_minute"] == 60
    assert delivery["payload"]["shipment_id"].startswith("region-shipment-")


@pytest.mark.long_simulation
def test_default_generated_scenario_serves_water_and_meals(tmp_path) -> None:
    world = run_scenario(_draft(30).scenario, tmp_path)
    events = [
        json.loads(line).get("event", {})
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
    ]
    completed = [
        event["payload"]["resource_id"]
        for event in events
        if event.get("event_type") == "resource_service_complete"
    ]
    assert completed.count("water_ml") >= 2 * len(world.agents)
    assert completed.count("food_meal_units") >= len(world.agents)
    assert statistics.median(agent.needs.hydration_need for agent in world.agents.values()) < 0.5
