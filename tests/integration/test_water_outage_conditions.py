"""A generated Scenario with an operator-entered water outage runs to the end.

How the outage expands into scheduled events is checked in
tests/unit/region/test_conditions.py.
"""

from __future__ import annotations

from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.events.models import EventStatus
from sheltersim.experiments.runner import run_scenario


def test_a_generated_scenario_with_a_water_outage_runs_to_the_end(
    generated_scenario_with, tmp_path
) -> None:
    scenario = generated_scenario_with(
        operational_conditions={
            "initial_resources": {"status": "entered", "water_ml": 300_000, "food_meal_units": 100},
            "scheduled": [
                {
                    "kind": "water_outage",
                    "facility_id": "water_station_main",
                    "start_minute": 60,
                    "end_minute": 240,
                }
            ],
        }
    )

    world = run_scenario(scenario, tmp_path)

    assert world.minute_index == scenario.duration_minutes
    outages = world.model_state[OPERATIONAL_EVENTS_KEY]["water_outages"]
    assert len(outages) == 1
    (record,) = outages.values()
    assert record["active"] is False
    for event_id in (record["outage_event_id"], record["restore_event_id"]):
        assert world.event_log.find(event_id).status is EventStatus.APPLIED
