"""The generator records which model implementations a new Scenario uses.

These check the generator itself, so they build drafts directly. That a generated
Scenario's people survive frames and checkpoints is checked in
tests/unit/web/test_frames.py.
"""

from __future__ import annotations

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.experiments.initial_scenario import InitialScenarioBuilder, InitialScenarioRequest


def _request(**policies: str) -> InitialScenarioRequest:
    return InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=12,
        duration_days=1,
        seed=42,
        **policies,
    )


def test_generated_scenarios_select_the_default_policies() -> None:
    scenario = InitialScenarioBuilder().build(_request()).scenario

    context = scenario.model_configs["population_context"]
    assert len(context["people"]) == 12
    assert all(person["medication_support"] is None for person in context["people"].values())
    registry = scenario.model_configs["policy_registry"]
    assert registry["body_dynamics"] == "body-feedback-complete-v2"
    assert registry["staff_operations"] == "staff-urgency-first-v1"
    assert registry["staff_fatigue"] == "staff-fatigue-linear-v1"
    assert registry["zone_environment"] == "zone-temperature-comfort-band-v1"
    assert registry["zone_rest"] == "zone-rest-multiplicative-v1"
    assert registry["zone_hygiene"] == "zone-hygiene-linear-v1"
    assert registry["zone_privacy"] == "zone-privacy-threshold-v1"


def test_requested_policies_reach_the_world_and_its_checkpoint() -> None:
    scenario = (
        InitialScenarioBuilder()
        .build(
            _request(
                body_dynamics_policy_id="body-feedback-sleep-debt-v1",
                staff_operations_policy_id="staff-travel-first-v1",
                staff_fatigue_policy_id="staff-fatigue-saturating-v1",
                zone_temperature_policy_id="zone-temperature-midpoint-v1",
                zone_rest_policy_id="zone-rest-limiting-v1",
                zone_hygiene_policy_id="zone-hygiene-quadratic-v1",
                zone_privacy_policy_id="zone-privacy-proportional-v1",
            )
        )
        .scenario
    )

    world = WorldState.from_scenario(scenario)
    assert world.model_configs["policy_registry"]["body_dynamics"] == (
        "body-feedback-sleep-debt-v1"
    )
    assert world.model_configs["policy_registry"]["zone_hygiene"] == "zone-hygiene-quadratic-v1"
    assert "policy:staff_fatigue" in Checkpoint.capture(world).model_versions
