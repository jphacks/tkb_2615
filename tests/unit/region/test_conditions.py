"""Operational conditions: standard stock and resupply, and entered water outages.

These check the generator itself, so they build drafts directly. The runs are
checked in tests/integration/test_standard_provisioning.py and
tests/integration/test_water_outage_conditions.py.
"""

from __future__ import annotations

import json
import math

from support.paths import SOURCE_MANIFEST

from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.region.conditions import STANDARD_PROVISIONING_SOURCE_ID
from sheltersim.region.facility import (
    SERVICE_STATION_SOURCE_ID,
    SYNTHETIC_STAFFING_SOURCE_ID,
    SYNTHETIC_TOILET_SOURCE_ID,
)


def _request(**scheduled: object) -> InitialScenarioRequest:
    return InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=12,
        duration_days=0.5,
        seed=7,
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "water_ml": 300_000,
                "food_meal_units": 100,
            },
            "scheduled": [
                {
                    "kind": "water_outage",
                    "facility_id": "water_station_main",
                    "start_minute": 60,
                    "end_minute": 240,
                    **scheduled,
                }
            ],
        },
    )


def test_each_restoration_names_its_outage_as_the_cause() -> None:
    draft = InitialScenarioBuilder().build(_request(repeat_every_minutes=360))

    assert draft.can_start and not draft.approval_blockers
    water = {
        event.id: event
        for event in draft.scenario.scheduled_events
        if event.event_type in {"water_outage", "water_restored"}
    }
    restorations = [event for event in water.values() if event.event_type == "water_restored"]
    assert [event.due_minute for event in restorations] == [240, 600]
    for restore in restorations:
        outage = water[restore.causation_id]
        assert outage.event_type == "water_outage"
        assert outage.payload["outage_id"] == restore.payload["outage_id"]
        assert outage.due_minute < restore.due_minute


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


def _deliveries(scenario) -> list:
    return sorted(
        (event for event in scenario.scheduled_events if event.event_type == "resource_delivery"),
        key=lambda event: (event.due_minute, event.payload["resource_id"]),
    )


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
    manifest = json.loads(SOURCE_MANIFEST.read_text("utf-8"))
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
