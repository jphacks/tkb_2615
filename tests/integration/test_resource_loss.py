"""Plain ledger events must not require the operational-event state."""

from __future__ import annotations

import pytest

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.events.models import EventStatus
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario


def _scenario(*scheduled: dict[str, object]):
    return (
        InitialScenarioBuilder()
        .build(
            InitialScenarioRequest(
                municipality="Synthetic municipality",
                disaster="earthquake",
                evacuees=12,
                duration_days=0.25,
                seed=7,
                operational_conditions={
                    "initial_resources": {
                        "status": "entered",
                        "water_ml": 300_000,
                        "food_meal_units": 100,
                    },
                    "scheduled": list(scheduled),
                },
            )
        )
        .scenario
    )


_LOSS = {"kind": "resource_loss", "resource_id": "water_ml", "quantity": 1_000, "due_minute": 20}


def test_a_pending_loss_passes_the_per_tick_check_without_operational_state() -> None:
    world = WorldState.from_scenario(_scenario(_LOSS))
    initialize_simulator_extensions(world)

    assert OPERATIONAL_EVENTS_KEY not in world.model_state
    world.validate(validate_event_history=False)
    world.validate()


def test_the_per_tick_check_still_requires_state_for_operational_events() -> None:
    world = WorldState.from_scenario(
        _scenario(
            {
                "kind": "staff_unavailable",
                "staff_id": "staff_001",
                "start_minute": 30,
                "end_minute": 90,
            }
        )
    )
    initialize_simulator_extensions(world)
    del world.model_state[OPERATIONAL_EVENTS_KEY]

    with pytest.raises(StateInvariantError, match="operational_events_v1"):
        world.validate(validate_event_history=False)


def test_a_generated_scenario_with_a_resource_loss_runs_to_the_end(tmp_path) -> None:
    scenario = _scenario(_LOSS)
    (loss,) = [event for event in scenario.scheduled_events if event.event_type == "resource_loss"]

    world = run_scenario(scenario, tmp_path)

    assert world.minute_index == scenario.duration_minutes
    assert world.event_log.find(loss.id).status is EventStatus.APPLIED
