"""Generated Scenarios select the research psychology model; legacy models stay replayable.

Running a generated Scenario is checked in tests/integration/test_research_psychology.py,
and the CLI warning for a legacy model in tests/e2e/test_legacy_psychology_cli.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.contracts.policies import (
    LEGACY_PSYCHOLOGY_MODELS,
    legacy_psychology_notice,
    selected_models,
)
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.psychology import (
    IntegratedDistressLifeStatePolicy,
    LifeStatePsychologyPolicy,
    PsychologyParameters,
)
from sheltersim.policies.registry import create_psychology

ROOT = Path(__file__).parents[3]
SMALL_FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"


def test_generated_scenarios_select_the_research_psychology_model(generated_scenario) -> None:
    scenario = generated_scenario
    assert selected_models(scenario.model_configs)["psychology"] == (
        INTEGRATED_PSYCHOLOGY_POLICY_VERSION
    )
    assert isinstance(create_psychology(scenario.model_configs), IntegratedDistressLifeStatePolicy)
    # Every mean-reversion coefficient is explicit in the Scenario, not an implicit default.
    defaults = PsychologyParameters()
    psychology = scenario.model_configs["psychology"]
    for name in (
        "natural_recovery_rate_per_minute",
        "chronic_retention",
        "chronic_acute_weight",
        "chronic_high_distress_weight",
        "high_distress_threshold",
    ):
        assert psychology[name] == getattr(defaults, name)


def test_generated_psychology_selection_is_recorded_in_provenance_and_manifest(
    generated_scenario,
) -> None:
    scenario = generated_scenario
    source_id = IntegratedDistressLifeStatePolicy.source_id
    assert any(row["id"] == source_id for row in scenario.provenance["sources"])
    assumption = next(
        row for row in scenario.provenance["assumptions"] if row["subject"] == "psychology_model"
    )
    assert assumption["source_id"] == source_id
    assert assumption["value"] == {"policy_version": INTEGRATED_PSYCHOLOGY_POLICY_VERSION}
    manifest = json.loads((ROOT / "fixtures" / "source-manifest.json").read_text("utf-8"))
    assert any(row["id"] == source_id for row in manifest["assumptions"])


def test_generated_research_state_restores_from_a_checkpoint(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    initialize_simulator_extensions(world)
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()


def test_saved_scenarios_keep_their_legacy_psychology_selection(generated_scenario) -> None:
    document = generated_scenario.model_dump(mode="json")
    document["model_configs"]["policy_registry"]["schema_version"] = 2
    document["model_configs"]["policy_registry"].pop("suite_id")
    document["model_configs"]["policy_registry"]["psychology"] = "life-state-feedback-v1"
    legacy = ScenarioSpec.model_validate(document)
    assert type(create_psychology(legacy.model_configs)) is LifeStatePsychologyPolicy
    # Inputs without a registry still resolve to the unchanged implicit default.
    unregistered = ScenarioSpec.load(SMALL_FIXTURE)
    assert selected_models(unregistered.model_configs)["psychology"] == "causal-stress-v1"


@pytest.mark.parametrize("model_id", sorted(LEGACY_PSYCHOLOGY_MODELS))
def test_legacy_psychology_models_have_a_notice(model_id: str) -> None:
    notice = legacy_psychology_notice(model_id)
    assert notice is not None
    assert model_id in notice
    assert INTEGRATED_PSYCHOLOGY_POLICY_VERSION in notice


@pytest.mark.parametrize(
    "model_id", [INTEGRATED_PSYCHOLOGY_POLICY_VERSION, "mean-reverting-distress-v1", None]
)
def test_research_or_unknown_models_have_no_notice(model_id) -> None:
    assert legacy_psychology_notice(model_id) is None
