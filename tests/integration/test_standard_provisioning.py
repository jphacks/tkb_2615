"""Standard provisioning keeps a generated Scenario served while it runs.

How the generator chooses facilities, staff, stock and resupply is checked in
tests/unit/region/test_facility.py and tests/unit/region/test_conditions.py.
"""

from __future__ import annotations

import json
import statistics

import pytest

from sheltersim.experiments.runner import run_scenario


def test_a_pending_region_delivery_does_not_stop_the_run(generated_scenario_with, tmp_path) -> None:
    # Region-condition deliveries lacked a shipment ID, so no operational-event state
    # was created and the first validation with a pending delivery stopped the Run.
    scenario = generated_scenario_with(
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
    run_scenario(scenario, tmp_path)
    realized = [
        json.loads(line)["event"]
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
    ]
    delivery = next(event for event in realized if event["event_type"] == "resource_delivery")
    assert delivery["due_minute"] == 60
    assert delivery["payload"]["shipment_id"].startswith("region-shipment-")


@pytest.mark.long_simulation
def test_default_generated_scenario_serves_water_and_meals(
    generated_scenario_with, tmp_path
) -> None:
    world = run_scenario(generated_scenario_with(evacuees=30, duration_days=1), tmp_path)
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
