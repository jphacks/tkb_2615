"""A staffed water station serves a queue longer than its room.

How the positioned queue moves up is checked in tests/unit/domain/test_movement.py.
"""

from __future__ import annotations

import json
import statistics

import pytest

from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _stock(evacuees: int) -> dict:
    """Water and meals entered by the operator, enough for a day of service."""
    return {
        "initial_resources": {
            "status": "entered",
            "water_ml": 9_000 * evacuees,
            "food_meal_units": 9 * evacuees,
            "source_kind": "assumption",
            "source_ids": ["ASM-TEST-ENTERED-STOCK"],
        }
    }


@pytest.mark.long_simulation
def test_staffed_water_station_serves_a_queue_longer_than_its_room(
    generated_scenario_with, tmp_path
) -> None:
    # Before the fix the 17th person in a 1.8 m wide room had no slot, the head was
    # never served and everyone ended the day at hydration need 1.0 (found with seed 7).
    scenario = generated_scenario_with(
        evacuees=30, duration_days=1, operational_conditions=_stock(30)
    )
    document = scenario.model_dump(mode="json")
    station = next(item for item in document["facilities"] if item["kind"] == "water_station")
    station["capacity"] = 3
    document["staff"].extend(
        {
            "id": f"water_staff_{index:02d}",
            "zone_id": station["zone_id"],
            "skills": ["water_service", "meal_service"],
        }
        for index in range(1, 4)
    )
    world = run_scenario(ScenarioSpec.model_validate(document), tmp_path)
    served = sum(
        1
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
        if json.loads(line).get("event", {}).get("event_type") == "resource_service_complete"
        and json.loads(line)["event"]["payload"]["resource_id"] == "water_ml"
    )
    assert served >= len(world.agents)
    assert statistics.median(agent.needs.hydration_need for agent in world.agents.values()) < 1.0
