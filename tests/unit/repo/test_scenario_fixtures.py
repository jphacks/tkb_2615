"""The six H05 operational scenario fixtures share the same profiles, seed, capacity and
provenance, so paired comparisons differ only in their events.

Runs of them are checked in tests/integration/test_operational_scenarios.py.
"""

from __future__ import annotations

from support.paths import SCENARIOS

from sheltersim.experiments.scenario import ScenarioSpec

SCENARIO_NAMES = (
    "baseline_multi_operational",
    "multi_operational",
    "water_outage",
    "water_intervention",
    "delivery_delay",
    "delivery_intervention",
)


def _load(name: str) -> ScenarioSpec:
    return ScenarioSpec.load(SCENARIOS / f"{name}.json")


def test_h05_scenarios_freeze_profiles_seed_capacity_and_provenance() -> None:
    scenarios = [_load(name) for name in SCENARIO_NAMES]
    reference_profiles = [profile.model_dump(mode="json") for profile in scenarios[0].profiles]
    reference_facilities = [
        facility.model_dump(mode="json") for facility in scenarios[0].facilities
    ]

    assert all(len(scenario.profiles) == 100 for scenario in scenarios)
    assert all(scenario.seed == 2026 for scenario in scenarios)
    assert all(scenario.duration_minutes == 7 * 24 * 60 for scenario in scenarios)
    assert all(
        [profile.model_dump(mode="json") for profile in scenario.profiles] == reference_profiles
        for scenario in scenarios
    )
    assert all(
        [facility.model_dump(mode="json") for facility in scenario.facilities]
        == reference_facilities
        for scenario in scenarios
    )
    assert all(scenario.initial_stock is not None for scenario in scenarios)
    assert all("resource_service" in scenario.model_configs for scenario in scenarios)
    assert all(
        "ASM-H05-OPERATIONAL-CAPACITY-V1" in scenario.provenance["model_assumption_ids"]
        for scenario in scenarios
    )
