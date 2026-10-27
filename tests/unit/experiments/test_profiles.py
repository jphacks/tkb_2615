"""Deterministic synthetic profile generation, overrides, and preview tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.models import Mobility
from sheltersim.experiments.profiles import (
    GeneratedProfileSet,
    ProfileTemplate,
    SyntheticProfileGenerator,
    select_representative_profiles,
)
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenario_100_agents.json"
INITIAL_PROFILES = Path(__file__).parents[3] / "fixtures" / "agents_initial.json"
SMALL_PROFILES = Path(__file__).parents[3] / "fixtures" / "profiles_small.json"


def test_same_seed_generates_same_complete_100_agent_profile_set() -> None:
    generator = SyntheticProfileGenerator()
    zones = ("sleeping", "corridor", "toilet", "information_desk")

    first = generator.generate(count=100, seed=1234, zone_ids=zones)
    second = generator.generate(count=100, seed=1234, zone_ids=zones)

    assert first.canonical_json() == second.canonical_json()
    assert len(first.profiles) == 100
    assert all(profile.schema_version == 2 for profile in first.profiles)
    assert [profile.id for profile in first.profiles] == [f"A{n:03d}" for n in range(1, 101)]
    assert {profile.initial_zone_id for profile in first.profiles} <= set(zones)
    assert all(profile.provenance.seed == 1234 for profile in first.profiles)
    assert first.manifest.rng_algorithm == "sha256-keyed-v1"
    assert all(
        0 <= value <= 1
        for profile in first.profiles
        for value in (
            profile.initial_needs.toilet_urgency,
            profile.initial_needs.hunger,
            profile.initial_needs.fatigue,
        )
    )


def test_committed_agents_initial_artifact_has_all_referenced_synthetic_profiles() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    profiles = GeneratedProfileSet.load(INITIAL_PROFILES)

    assert len(profiles.profiles) == profiles.manifest.profile_count == 100
    assert {profile.initial_zone_id for profile in profiles.profiles} <= {
        zone.id for zone in scenario.zones
    }
    assert profiles.manifest.seed == scenario.seed
    assert all(profile.provenance.source_kind == "synthetic" for profile in profiles.profiles)


def test_representative_profiles_use_stable_need_mobility_and_family_criteria() -> None:
    scenario = ScenarioSpec.load(FIXTURE)

    selections = select_representative_profiles(scenario.profiles)
    repeat = select_representative_profiles(tuple(reversed(scenario.profiles)))

    assert selections == repeat
    assert [item.criterion_id for item in selections] == [
        "support_need",
        "mobility_support",
        "family_context",
    ]
    assert len({item.profile.id for item in selections}) == 3
    assert selections[1].profile.mobility is not Mobility.INDEPENDENT
    assert selections[2].profile.group_id is not None
    assert all("tie-break=Agent ID" in item.selection_reason for item in selections)


def test_small_profile_fixture_round_trips_as_a_readable_complete_preview() -> None:
    profiles = GeneratedProfileSet.load(SMALL_PROFILES)

    assert len(profiles.profiles) == 8
    assert profiles.preview().count('"id": "A') == 8
    assert GeneratedProfileSet.from_json(profiles.preview()).canonical_json() == (
        profiles.canonical_json()
    )


def test_generator_draws_do_not_depend_on_profile_iteration_order() -> None:
    generator = SyntheticProfileGenerator()
    zones = ("sleeping", "corridor")
    generated = generator.generate(count=20, seed=7, zone_ids=zones)
    reversed_profiles = tuple(reversed(generated.profiles))
    same_set = type(generated)(profiles=reversed_profiles, manifest=generated.manifest)

    assert generated.canonical_json() == same_set.canonical_json()


def test_manual_overrides_are_validated_and_recorded_in_generation_manifest() -> None:
    generated = SyntheticProfileGenerator().generate(
        count=8,
        seed=4,
        zone_ids=("sleeping", "corridor"),
        template=ProfileTemplate(group_size=2),
    )

    changed = generated.apply_manual_overrides(
        {
            "A002": {
                "mobility": Mobility.WHEELCHAIR,
                "support_need": 0.95,
                "initial_needs": {"toilet_urgency": 0.25, "hunger": 0.4, "fatigue": 0.7},
            },
            "A005": {"initial_zone_id": "corridor"},
        }
    )

    updated_agent = next(profile for profile in changed.profiles if profile.id == "A002")
    assert updated_agent.mobility is Mobility.WHEELCHAIR
    assert updated_agent.support_need == 0.95
    assert updated_agent.initial_needs.toilet_urgency == 0.25
    assert updated_agent.provenance.source_kind == "human"
    assert updated_agent.provenance.generator == "manual-override"
    assert [(item.agent_id, item.fields) for item in changed.manifest.manual_overrides] == [
        ("A002", ("initial_needs", "mobility", "support_need")),
        ("A005", ("initial_zone_id",)),
    ]
    with pytest.raises(ValueError, match="unknown Agent IDs"):
        generated.apply_manual_overrides({"A999": {"mobility": "assisted"}})
    with pytest.raises(ValueError, match="unknown zone"):
        generated.apply_manual_overrides({"A001": {"initial_zone_id": "missing"}})


def test_generated_profiles_can_replace_scenario_profiles_with_embedded_manifest() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    profiles = SyntheticProfileGenerator().generate(
        count=12,
        seed=scenario.seed,
        zone_ids=(zone.id for zone in scenario.zones),
    )

    prepared = SyntheticProfileGenerator.apply_to_scenario(scenario, profiles)

    assert len(prepared.profiles) == 12
    assert prepared.provenance["profile_generation"]["seed"] == scenario.seed
    assert prepared.profiles[0].initial_zone_id in {zone.id for zone in scenario.zones}
    assert len(scenario.profiles) == 100
    world = WorldState.from_scenario(prepared)
    for profile in prepared.profiles:
        state = world.agents[profile.id]
        assert state.needs.toilet_urgency == profile.initial_needs.toilet_urgency
        assert state.needs.hunger == profile.initial_needs.hunger
        assert state.needs.fatigue == profile.initial_needs.fatigue


def test_profile_preview_and_save_include_every_agent_and_manifest(tmp_path: Path) -> None:
    generated = SyntheticProfileGenerator().generate(
        count=100,
        seed=2026,
        zone_ids=("sleeping", "corridor", "toilet", "information_desk"),
    )
    output = tmp_path / "agents_initial.json"

    generated.save(output)
    preview = json.loads(generated.preview())
    saved = json.loads(output.read_text(encoding="utf-8"))
    reloaded = type(generated).load(output)

    assert preview == saved
    assert reloaded.canonical_json() == generated.canonical_json()
    assert len(preview["profiles"]) == 100
    assert preview["manifest"]["generator"] == "counter-rng-profile-generator-v1"
    assert preview["manifest"]["template"]["template_id"] == "synthetic-balanced-v1"
    assert output.read_text(encoding="utf-8").endswith("\n")
