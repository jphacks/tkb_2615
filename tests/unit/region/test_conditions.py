"""Water outages entered as operational conditions expand into linked scheduled events.

This checks the generator itself, so it builds the draft directly. That such a
Scenario runs to the end is checked in tests/integration/test_water_outage_conditions.py.
"""

from __future__ import annotations

from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
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
