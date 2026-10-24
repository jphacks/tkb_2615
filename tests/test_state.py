"""Scenario contract and world-state invariant tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.models import Activity, AgentState
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def test_100_agent_scenario_loads_and_saves_canonical_json(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    saved = tmp_path / "scenario.json"

    scenario.save(saved)
    reloaded = ScenarioSpec.load(saved)

    assert len(scenario.profiles) == 100
    assert len({profile.id for profile in scenario.profiles}) == 100
    assert reloaded.canonical_json() == scenario.canonical_json()
    assert saved.read_text(encoding="utf-8") == scenario.canonical_json() + "\n"


def test_scenario_canonical_json_ignores_collection_input_order() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in ("zones", "zone_edges", "facilities", "staff", "profiles", "scheduled_events"):
        document[key].reverse()

    reordered = ScenarioSpec.model_validate(document)

    assert reordered.canonical_json() == ScenarioSpec.load(FIXTURE).canonical_json()


def test_scenario_rejects_unknown_zone_references() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["profiles"][0]["initial_zone_id"] = "missing-zone"

    with pytest.raises(ValidationError, match="unknown zone"):
        ScenarioSpec.model_validate(document)


def test_scenario_rejects_negative_facility_capacity() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["facilities"][0]["capacity"] = -1

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ScenarioSpec.model_validate(document)


def test_scenario_rejects_duplicate_profile_ids() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["profiles"][1]["id"] = document["profiles"][0]["id"]

    with pytest.raises(ValidationError, match="duplicate IDs in profiles"):
        ScenarioSpec.model_validate(document)


def test_agent_rejects_multiple_exclusive_activities() -> None:
    agent = next(iter(WorldState.from_scenario(ScenarioSpec.load(FIXTURE)).agents.values()))

    with pytest.raises(ValueError, match="multiple exclusive activities"):
        AgentState(
            profile=agent.profile,
            zone_id=agent.zone_id,
            activity=[Activity.MOVING, Activity.QUEUED],
        )


def test_world_rejects_duplicate_queue_membership() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.agents["A001"].activity = Activity.QUEUED
    world.facilities["toilet_1"].queue_agent_ids.append("A001")
    world.facilities["toilet_2"].queue_agent_ids.append("A001")

    with pytest.raises(StateInvariantError, match="multiple facility queues"):
        world.validate()


def test_world_rejects_duplicate_entries_in_one_queue() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.agents["A001"].activity = Activity.QUEUED
    world.facilities["toilet_1"].queue_agent_ids.extend(("A001", "A001"))

    with pytest.raises(StateInvariantError, match="duplicate queue entries"):
        world.validate()


def test_world_rejects_agent_with_unknown_zone() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.agents["A001"].zone_id = "missing-zone"

    with pytest.raises(StateInvariantError, match="unknown zone"):
        world.validate()


def test_world_canonical_json_is_stable() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    world_a = WorldState.from_scenario(scenario)
    world_b = WorldState.from_scenario(scenario)

    assert world_a.canonical_json() == world_b.canonical_json()


def test_initial_world_preserves_scenario_graph_and_model_config() -> None:
    scenario = ScenarioSpec.load(FIXTURE)

    world = WorldState.from_scenario(scenario)

    assert len(world.zone_edges) == len(scenario.zone_edges)
    assert world.model_configs == scenario.model_configs


def test_initial_world_owns_its_mutable_model_config_copy() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    world = WorldState.from_scenario(scenario)

    world.model_configs["behavior"]["mode"] = "changed-in-world"

    assert scenario.model_configs["behavior"]["mode"] == "offline"


def test_initial_needs_copy_into_mutable_agent_state_and_serialize_separately() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    agent = world.agents["A001"]
    initial_hunger = agent.profile.initial_needs.hunger

    agent.needs.hunger = 0.9
    serialized = json.loads(world.canonical_json())
    serialized_agent = next(
        item for item in serialized["agents"] if item["profile"]["id"] == "A001"
    )
    assert agent.profile.initial_needs.hunger == initial_hunger
    assert agent.needs.hunger == 0.9
    assert serialized_agent["profile"]["initial_needs"]["hunger"] == initial_hunger
    assert serialized_agent["needs"]["hunger"] == 0.9


def test_scenario_rejects_need_values_outside_the_normalized_range() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["profiles"][0]["initial_needs"]["hunger"] = 1.1

    with pytest.raises(ValidationError, match="less than or equal to 1"):
        ScenarioSpec.model_validate(document)


def test_scenario_json_loader_upgrades_version_one_through_v2_to_v3() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 1
    for profile in document["profiles"]:
        profile.pop("initial_needs", None)

    upgraded = ScenarioSpec.from_json(json.dumps(document))

    assert upgraded.schema_version == 3
    assert upgraded.profiles[0].initial_needs.toilet_urgency == 0.0
    assert upgraded.profiles[0].initial_needs.hunger == 0.0
    assert upgraded.profiles[0].initial_needs.fatigue == 0.0


@pytest.mark.parametrize("schema_version", [0, 4])
def test_scenario_loader_rejects_unsupported_schema_versions(schema_version: int) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = schema_version

    with pytest.raises(ValidationError, match="schema_version"):
        ScenarioSpec.from_json(json.dumps(document))


def test_scenario_loader_rejects_unversioned_structural_fields() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["unexpected_contract_field"] = "must be versioned before acceptance"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScenarioSpec.from_json(json.dumps(document))


def test_world_rejects_mutated_need_state_outside_the_normalized_range() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    world.agents["A001"].needs.fatigue = -0.01

    with pytest.raises(StateInvariantError, match="invalid need levels"):
        world.validate()


def test_world_state_v5_rejects_old_versions_and_unknown_fields() -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FIXTURE))
    document = world.to_dict()

    old_version = dict(document, schema_version=4)
    with pytest.raises(StateInvariantError, match="unsupported world-state schema version"):
        WorldState.from_dict(old_version)

    extended = dict(document, unexpected_contract_field=True)
    with pytest.raises(StateInvariantError, match="fields do not match"):
        WorldState.from_dict(extended)
