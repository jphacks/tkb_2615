"""Generated Scenarios start with Agents oriented to shared shelter facilities."""

from __future__ import annotations

import csv

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
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def _generated(duration_days: float = 1) -> ScenarioSpec:
    return InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality", disaster="earthquake",
        evacuees=10, duration_days=duration_days, seed=7,
    )).scenario


def _world(scenario: ScenarioSpec) -> WorldState:
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    return world


def test_generated_agents_know_shared_facilities_but_not_group_spaces() -> None:
    scenario = _generated()
    # v3 keeps the v2 intake orientation checked here.
    assert scenario.model_configs["agent_dynamics"]["policy_version"] == (
        ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION
    )
    assert any(row["id"] == ORIENTATION_FACILITY_SOURCE_ID
               for row in scenario.provenance["assumptions"])
    world = _world(scenario)
    toilets = {f.id for f in world.facilities.values() if f.kind == "toilet"}
    assert toilets
    for record in world.model_state[AGENT_DYNAMICS_STATE_KEY]["agents"].values():
        assert toilets <= set(record["known_facility_ids"])
        assert "quiet_support" in record["known_facility_ids"]


def test_generated_agents_use_toilets_on_the_first_day(tmp_path) -> None:
    # Before orientation, every request_toilet choice was dropped because the
    # separate toilet zone was never known, so nobody was ever served.
    run_scenario(_generated(duration_days=0.25), tmp_path)
    with (tmp_path / "metrics.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert float(rows[-1]["toilet_wait_count"]) > 0


def test_scenarios_without_agent_dynamics_config_keep_v1_knowledge() -> None:
    document = _generated().model_dump(mode="json")
    document["model_configs"].pop("agent_dynamics")
    legacy = ScenarioSpec.model_validate(document)
    world = _world(legacy)
    state = world.model_state[AGENT_DYNAMICS_STATE_KEY]
    assert state["policy_version"] == AGENT_DYNAMICS_POLICY_VERSION
    assert all(record["known_facility_ids"] == [] for record in state["agents"].values())
    assert simulator_signatures(legacy.model_configs)[
        f"simulator:{AGENT_DYNAMICS_STATE_KEY}"
    ].startswith(f"{AGENT_DYNAMICS_POLICY_VERSION}@")


def test_oriented_state_restores_from_a_checkpoint() -> None:
    world = _world(_generated())
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()


@pytest.mark.parametrize("raw", [
    {"policy_version": "need-led-autonomy-memory-v9"},
    {"policy_version": ORIENTATION_AGENT_DYNAMICS_POLICY_VERSION, "source_id": "ASM-OTHER"},
    {"policy_version": AGENT_DYNAMICS_POLICY_VERSION, "source_id": ORIENTATION_FACILITY_SOURCE_ID},
    {"known_facility_kinds": ["toilet"]},
])
def test_agent_dynamics_config_rejects_unsupported_values(raw) -> None:
    with pytest.raises(ValueError, match="agent_dynamics"):
        canonical_agent_dynamics_config(raw)
