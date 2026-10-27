"""One meal station keeps up with a hundred people over a day.

How followers and the head move up the queue is checked in
tests/unit/domain/test_movement.py.
"""

from __future__ import annotations

import json
import statistics

import pytest

from sheltersim.experiments.runner import run_scenario


@pytest.mark.long_simulation
def test_one_meal_station_keeps_up_with_one_hundred_people(
    generated_scenario_with, tmp_path
) -> None:
    # Before the fix the meal queue grew to 72 people: 1,371 of about 1,500 service
    # attempts were held back by someone else moving up, and 65 meals were served
    # (found with seed 7).
    world = run_scenario(generated_scenario_with(evacuees=100, duration_days=1), tmp_path)
    records = [
        json.loads(line)
        for line in (tmp_path / "events_realized.jsonl").read_text("utf-8").splitlines()
    ]
    meals = sum(
        record["event"]["event_type"] == "resource_service_complete"
        and record["event"]["payload"]["resource_id"] == "food_meal_units"
        for record in records
    )
    assert meals >= len(world.agents)
    assert not any(record["status"] == "failed" for record in records)
    assert statistics.median(agent.needs.hunger for agent in world.agents.values()) < 0.7
