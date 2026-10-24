"""Focused verification of installed formula factories and checkpoint signatures."""

from pathlib import Path

import pytest

from sheltersim.contracts.policies import selected_models
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.world import WorldState
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.registry import create_psychology


def test_unknown_operator_cannot_be_imported():
    with pytest.raises(ValueError, match="unsupported installed"):
        selected_models({"policy_registry": {"schema_version": 1, "psychology": "os.system"}})


def test_alternative_is_pure_and_changes_the_operator():
    scenario = ScenarioSpec.load(Path("fixtures/scenario_100_agents.json"))
    world = WorldState.from_scenario(scenario)
    original = world.canonical_json()
    view = WorldView.from_world(world)
    baseline = create_psychology(world.model_configs).explain_update(view, 0)
    alternate = create_psychology({**world.model_configs, "policy_registry": {"schema_version": 1, "psychology": "bounded-stress-v1"}}).explain_update(view, 0)
    assert world.canonical_json() == original
    assert alternate.deltas != baseline.deltas
    assert alternate.policy_version == "bounded-stress-v1"


def test_incompatible_policy_checkpoint_is_rejected():
    world = WorldState.from_scenario(ScenarioSpec.load("fixtures/scenario_100_agents.json"))
    checkpoint = Checkpoint.capture(world)
    incompatible_versions = {
        **checkpoint.model_versions,
        "policy:psychology": "absent-model@99:state99",
    }
    with pytest.raises(CheckpointError, match="do not match"):
        checkpoint.restore_world(expected_model_versions=incompatible_versions)


def _body_scenario(body_model: str) -> ScenarioSpec:
    from sheltersim.domain.body_state import canonical_body_config

    document = ScenarioSpec.load("fixtures/scenario_100_agents.json").model_dump(mode="json")
    document["model_configs"]["body_dynamics"] = canonical_body_config(None)
    document["model_configs"]["policy_registry"] = {
        "schema_version": 2,
        "psychology": "life-state-feedback-v1",
        "body_dynamics": body_model,
    }
    return ScenarioSpec.model_validate(document)


def test_body_formula_factory_changes_stress_response_and_checkpoint_signature():
    full_world = WorldState.from_scenario(_body_scenario("body-feedback-complete-v1"))
    sleep_world = WorldState.from_scenario(_body_scenario("body-feedback-sleep-debt-v1"))
    view = WorldView.from_world(full_world)

    full_result = create_psychology(full_world.model_configs).explain_update(view, 0)
    sleep_result = create_psychology(sleep_world.model_configs).explain_update(view, 0)

    assert full_result.deltas != sleep_result.deltas
    full_checkpoint = Checkpoint.capture(full_world)
    sleep_checkpoint = Checkpoint.capture(sleep_world)
    assert (
        full_checkpoint.model_versions["policy:body_dynamics"]
        != sleep_checkpoint.model_versions["policy:body_dynamics"]
    )
    with pytest.raises(CheckpointError, match="do not match"):
        full_checkpoint.restore_world(expected_model_versions=sleep_checkpoint.model_versions)


def test_agent_social_operator_catalog_covers_configured_body_and_environment_values():
    from sheltersim.domain.body_state import _DEFAULTS
    from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
    from sheltersim.policies.agents_social_manifest import agents_social_model_catalog

    catalog = agents_social_model_catalog()
    body_parameters = catalog["body_state_v1"]["parameters"]
    environment_parameters = catalog["zone_environment_v1"]["parameters"]

    assert set(_DEFAULTS).issubset(body_parameters)
    assert "caregiver_assistance_stress_relief" in body_parameters
    assert "dependent_assistance_reassurance_gain" in body_parameters
    assert "caregiver_assistance_stress_relief_by_agent.*" in body_parameters
    assert "dependent_assistance_reassurance_gain_by_agent.*" in body_parameters
    assert body_parameters["dependent_assistance_reassurance_gain_by_agent.*"]["scope"] == "per_agent_id"

    environment = ScenarioZoneEnvironmentConfig().model_dump(mode="json")
    assert set(environment) - {
        "schema_version", "policy_version", "source_type", "source_id", "baseline_by_zone"
    } <= set(environment_parameters)
    assert "baseline_by_zone.*" in environment_parameters
