"""Water outages entered as operational conditions must build and run."""

from __future__ import annotations

from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.events.models import EventStatus
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario


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


def test_a_generated_scenario_with_a_water_outage_runs_to_the_end(tmp_path) -> None:
    scenario = InitialScenarioBuilder().build(_request()).scenario

    world = run_scenario(scenario, tmp_path)

    assert world.minute_index == scenario.duration_minutes
    outages = world.model_state[OPERATIONAL_EVENTS_KEY]["water_outages"]
    assert len(outages) == 1
    (record,) = outages.values()
    assert record["active"] is False
    for event_id in (record["outage_event_id"], record["restore_event_id"]):
        assert world.event_log.find(event_id).status is EventStatus.APPLIED
