"""Generated Scenarios select the research psychology model; legacy models stay replayable.

Installed formula factories change their operator and sign the Checkpoint, and the core
model suite is pinned for new configs without rewriting historical ones. A suite runs
the models it declares even where it leaves a per-domain selector implicit (#169).

Running a generated Scenario is checked in tests/integration/test_research_psychology.py,
and the CLI warning for a legacy model in tests/e2e/test_legacy_psychology_cli.py.
"""

from __future__ import annotations

import json

import pytest
from support.paths import SCENARIO_100_AGENTS, SMALL_REPLAY, SOURCE_MANIFEST

from sheltersim.contracts.policies import (
    CORE_MODEL_SUITE_MODELS,
    CORE_MODEL_SUITE_MODELS_V1,
    CORE_MODEL_SUITE_V1,
    CORE_MODEL_SUITE_V2,
    LEGACY_PSYCHOLOGY_MODELS,
    SUITE_RUNTIME_RESOLVED_DOMAINS,
    legacy_psychology_notice,
    model_signatures,
    selected_models,
)
from sheltersim.core.checkpoint import Checkpoint, CheckpointError, _digest
from sheltersim.core.world import WorldState
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.models import AgentInformationState, TypedMessage
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.model_suite import install_default_model_suite
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import (
    IntegratedDistressLifeStatePolicy,
    LifeStatePsychologyPolicy,
    PsychologyParameters,
)
from sheltersim.policies.queueing import NeedWeightedPhysicalQueuePolicy, create_queue_policy
from sheltersim.policies.registry import create_psychology
from sheltersim.policies.relationships import (
    DistanceDurationRepeatTrustPolicy,
    create_relationship_policy,
)


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
    manifest = json.loads(SOURCE_MANIFEST.read_text("utf-8"))
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
    unregistered = ScenarioSpec.load(SMALL_REPLAY)
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


def test_unknown_operator_cannot_be_imported():
    with pytest.raises(ValueError, match="unsupported installed"):
        selected_models({"policy_registry": {"schema_version": 1, "psychology": "os.system"}})


def test_alternative_is_pure_and_changes_the_operator():
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    world = WorldState.from_scenario(scenario)
    original = world.canonical_json()
    view = WorldView.from_world(world)
    baseline = create_psychology(world.model_configs).explain_update(view, 0)
    alternate_configs = {
        **world.model_configs,
        "policy_registry": {
            "schema_version": 1,
            "psychology": "bounded-stress-v1",
        },
    }
    alternate = create_psychology(alternate_configs).explain_update(view, 0)
    assert world.canonical_json() == original
    assert alternate.deltas != baseline.deltas
    assert alternate.policy_version == "bounded-stress-v1"


def test_incompatible_policy_checkpoint_is_rejected():
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    checkpoint = Checkpoint.capture(world)
    incompatible_versions = {
        **checkpoint.model_versions,
        "policy:psychology": "absent-model@99:state99",
    }
    with pytest.raises(CheckpointError, match="do not match"):
        checkpoint.restore_world(expected_model_versions=incompatible_versions)


def _body_scenario(body_model: str) -> ScenarioSpec:
    from sheltersim.domain.body_state import canonical_body_config

    document = ScenarioSpec.load(SCENARIO_100_AGENTS).model_dump(mode="json")
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
    assert (
        body_parameters["dependent_assistance_reassurance_gain_by_agent.*"]["scope"]
        == "per_agent_id"
    )

    environment = ScenarioZoneEnvironmentConfig().model_dump(mode="json")
    assert set(environment) - {
        "schema_version",
        "policy_version",
        "source_type",
        "source_id",
        "baseline_by_zone",
    } <= set(environment_parameters)
    assert "baseline_by_zone.*" in environment_parameters


def test_new_unselected_configs_receive_one_pinned_core_suite():
    configs = install_default_model_suite({"behavior": {"policy": "utility-softmax-v1"}})

    assert configs["policy_registry"] == {
        "schema_version": 3,
        "suite_id": CORE_MODEL_SUITE_V2,
    }
    assert selected_models(configs) == CORE_MODEL_SUITE_MODELS
    assert configs["body_dynamics"]["awake_sleep_debt_minutes_per_minute"] == 0.5
    assert configs["body_dynamics"]["rest_sleep_debt_recovery_per_minute"] == 0.0
    assert configs["social_propagation"]["enabled"] is True
    assert configs["information_spread"]["enabled"] is True
    assert "social_proximity" not in configs
    assert selected_models(configs)["social_proximity"] == "disabled-proximity-v1"


def test_explicit_historical_registry_is_not_rewritten_by_new_defaults():
    configs = {
        "behavior": {"policy": "utility-softmax-v1"},
        "policy_registry": {
            "schema_version": 1,
            "psychology": "causal-stress-v1",
        },
    }

    assert install_default_model_suite(configs) == configs


def test_suite_v1_selector_keeps_its_original_model_map():
    configs = install_default_model_suite({"behavior": {"policy": "utility-softmax-v1"}})
    configs["behavior"]["policy"] = "utility-softmax-v1"
    configs["behavior"]["source_id"] = "ASM-UTILITY-BEHAVIOR-V1"
    configs["behavior"].pop("sleep_debt_utility_weight")
    configs["policy_registry"]["suite_id"] = CORE_MODEL_SUITE_V1

    assert selected_models(configs) == CORE_MODEL_SUITE_MODELS_V1


def _implicit_suite_scenario() -> ScenarioSpec:
    """A suite without per-domain selectors, as the Scenario editor and prepare write it."""
    document = ScenarioSpec.load(SCENARIO_100_AGENTS).model_dump(mode="json")
    document["model_configs"].pop("policy_registry", None)
    document["model_configs"] = install_default_model_suite(document["model_configs"])
    return ScenarioSpec.model_validate(document)


def test_core_suite_is_checkpoint_signed_and_restorable():
    scenario = _implicit_suite_scenario()
    world = WorldState.from_scenario(scenario)
    checkpoint = Checkpoint.capture(world)

    assert model_signatures(scenario.model_configs)["model_suite"] == f"{CORE_MODEL_SUITE_V2}@2"
    assert checkpoint.model_versions["model_suite"] == f"{CORE_MODEL_SUITE_V2}@2"
    assert checkpoint.restore_world().model_configs == scenario.model_configs


def test_a_suite_with_every_selector_explicit_keeps_its_checkpoint_signature(
    generated_scenario,
) -> None:
    """Explicit selectors always ran their models, so their Checkpoints stay valid."""
    configs = generated_scenario.model_configs
    assert SUITE_RUNTIME_RESOLVED_DOMAINS <= set(configs["policy_registry"])
    assert model_signatures(configs)["model_suite"] == f"{CORE_MODEL_SUITE_V2}@1"


def test_a_suite_runs_the_models_it_leaves_implicit():
    """The runtime used legacy defaults for these domains before #169."""
    scenario = _implicit_suite_scenario()
    configs = scenario.model_configs
    assert SUITE_RUNTIME_RESOLVED_DOMAINS.isdisjoint(configs["policy_registry"])
    assert selected_models(configs)["relationship"] == "distance-duration-repeat-trust-v1"
    assert isinstance(create_relationship_policy(configs), DistanceDurationRepeatTrustPolicy)
    assert selected_models(configs)["queue"] == "need-weighted-physical-queue-v1"
    assert isinstance(create_queue_policy(configs), NeedWeightedPhysicalQueuePolicy)

    # The life-state psychology model weighs a strongly held false belief.
    world = WorldState.from_scenario(scenario)
    agent_id = sorted(world.agents)[0]
    information = world.model_state[INFORMATION_SPREAD_STATE_KEY]
    information["messages"] = {
        "rumor": TypedMessage(
            id="rumor",
            truth_status="false",
            source_id="test-source",
            zone_id=world.agents[agent_id].zone_id,
            time=0,
            origin_event_id="rumor-event",
            origin_agent_id=agent_id,
        ).to_dict()
    }
    information["beliefs"] = {
        agent_id: {
            "rumor": AgentInformationState(
                message_id="rumor",
                belief="heard",
                source="test-source",
                last_contact=0,
                last_event_id="rumor-event",
                belief_strength=0.9,
            ).to_dict()
        }
    }
    view = WorldView.from_world(world)
    assert view.agents[agent_id].misinformation_belief_pressure == pytest.approx(0.8)


def test_a_checkpoint_from_before_implicit_suite_selectors_ran_is_refused():
    """Resuming it would switch models mid-Run under an unchanged signature (#169)."""
    world = WorldState.from_scenario(_implicit_suite_scenario())
    document = Checkpoint.capture(world).to_dict()
    document["model_versions"]["model_suite"] = f"{CORE_MODEL_SUITE_V2}@1"
    document.pop("checkpoint_hash")
    document["checkpoint_hash"] = _digest(document)

    with pytest.raises(CheckpointError, match="explicit migration required"):
        Checkpoint.from_dict(document).restore_world()
