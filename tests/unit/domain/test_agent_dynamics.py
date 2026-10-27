"""Generated Scenarios start with Agents oriented to shared shelter facilities.

That they then use those facilities while the simulation runs is checked in
tests/integration/test_facility_orientation.py.
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
