"""A generated Scenario with a scheduled resource loss runs to the end.

That the per-tick check accepts a pending loss without operational-event state
is checked in tests/unit/core/test_world.py.
"""

from __future__ import annotations

from sheltersim.events.models import EventStatus
from sheltersim.experiments.runner import run_scenario

_LOSS = {"kind": "resource_loss", "resource_id": "water_ml", "quantity": 1_000, "due_minute": 20}


def test_a_generated_scenario_with_a_resource_loss_runs_to_the_end(
    generated_scenario_with, tmp_path
) -> None:
    scenario = generated_scenario_with(
        operational_conditions={
            "initial_resources": {"status": "entered", "water_ml": 300_000, "food_meal_units": 100},
            "scheduled": [_LOSS],
        }
    )
    (loss,) = [event for event in scenario.scheduled_events if event.event_type == "resource_loss"]

    world = run_scenario(scenario, tmp_path)

    assert world.minute_index == scenario.duration_minutes
    assert world.event_log.find(loss.id).status is EventStatus.APPLIED
