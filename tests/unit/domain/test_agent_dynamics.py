"""Generated Scenarios start with Agents oriented to shared shelter facilities.

That they then use those facilities while the simulation runs is checked in
tests/integration/test_facility_orientation.py.

What one Agent knows can also be set in the Scenario (facility_awareness); that
this knowledge decides whether its toilet need is served is checked in
tests/integration/test_facility_awareness.py.
"""

from __future__ import annotations

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.agent_dynamics import (
    AGENT_DYNAMICS_POLICY_VERSION,
    AGENT_DYNAMICS_STATE_KEY,
    ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION,
    ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION,
    ORIENTATION_FACILITY_SOURCE_ID,
    agent_knows_facility,
    canonical_agent_dynamics_config,
)
from sheltersim.domain.simulator_state import (
    initialize_simulator_extensions,
    simulator_signatures,
)
from sheltersim.experiments.scenario import ScenarioSpec


def _world(scenario: ScenarioSpec) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


def test_generated_agents_know_shared_facilities_but_not_group_spaces(generated_scenario) -> None:
    scenario = generated_scenario
    # v3 keeps the v2 intake orientation checked here.
    assert scenario.model_configs["agent_dynamics"]["policy_version"] == (
        ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION
    )
    assert any(
        row["id"] == ORIENTATION_FACILITY_SOURCE_ID for row in scenario.provenance["assumptions"]
    )
    world = _world(scenario)
    toilets = {f.id for f in world.facilities.values() if f.kind == "toilet"}
    assert toilets
    for record in world.model_state[AGENT_DYNAMICS_STATE_KEY]["agents"].values():
        assert toilets <= set(record["known_facility_ids"])
        assert "quiet_support" in record["known_facility_ids"]


def test_scenarios_without_agent_dynamics_config_keep_v1_knowledge(generated_scenario) -> None:
    document = generated_scenario.model_dump(mode="json")
    document["model_configs"].pop("agent_dynamics")
    legacy = ScenarioSpec.model_validate(document)
    world = _world(legacy)
    state = world.model_state[AGENT_DYNAMICS_STATE_KEY]
    assert state["policy_version"] == AGENT_DYNAMICS_POLICY_VERSION
    assert all(record["known_facility_ids"] == [] for record in state["agents"].values())
    assert simulator_signatures(legacy.model_configs)[
        f"simulator:{AGENT_DYNAMICS_STATE_KEY}"
    ].startswith(f"{AGENT_DYNAMICS_POLICY_VERSION}@")


def test_oriented_state_restores_from_a_checkpoint(generated_scenario) -> None:
    world = _world(generated_scenario)
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()


@pytest.mark.parametrize(
    "raw",
    [
        {"policy_version": "need-led-autonomy-memory-v9"},
        {"policy_version": ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION, "source_id": "ASM-OTHER"},
        {
            "policy_version": AGENT_DYNAMICS_POLICY_VERSION,
            "source_id": ORIENTATION_FACILITY_SOURCE_ID,
        },
        {"known_facility_kinds": ["toilet"]},
    ],
)
def test_agent_dynamics_config_rejects_unsupported_values(raw) -> None:
    with pytest.raises(ValueError, match="agent_dynamics"):
        canonical_agent_dynamics_config(raw)


def test_facility_knowledge_is_independent_of_psychology_selection(
    single_agent_awareness_scenario
) -> None:
    known_sets = []
    for psychology in ("mean-reverting-life-state-v1", "mean-reverting-distress-v1"):
        document = single_agent_awareness_scenario(["toilet_1"]).model_dump(mode="json")
        document["model_configs"]["policy_registry"]["psychology"] = psychology
        world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
        known_sets.append(
            world.model_state["agent_dynamics_v1"]["agents"]["A001"]["known_facility_ids"]
        )
        assert agent_knows_facility(world, "A001", "toilet_1")
        assert not agent_knows_facility(world, "A001", "toilet_2")
    assert known_sets[0] == known_sets[1] == ["toilet_1"]


def test_awareness_config_is_versioned_and_checkpoint_safe(single_agent_awareness_scenario) -> None:
    scenario = single_agent_awareness_scenario(["toilet_1"])
    world = WorldState.from_scenario(scenario)
    restored = WorldState.from_dict(world.to_dict())
    assert restored.model_state["agent_dynamics_v1"] == world.model_state["agent_dynamics_v1"]
    assert agent_knows_facility(restored, "A001", "toilet_1")
    assert not agent_knows_facility(restored, "A001", "toilet_2")
    assert "operator:facility_awareness" in simulator_signatures(scenario.model_configs)


def test_unknown_initial_facility_id_is_rejected(single_agent_awareness_scenario) -> None:
    with pytest.raises(ValueError, match="unknown facilities"):
        single_agent_awareness_scenario(["facility-not-in-scenario"])


def test_legacy_scenario_without_awareness_keeps_prior_global_knowledge(
    single_agent_awareness_scenario
) -> None:
    document = single_agent_awareness_scenario([]).model_dump(mode="json")
    document["model_configs"].pop("facility_awareness")
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    assert agent_knows_facility(world, "A001", "toilet_1")
