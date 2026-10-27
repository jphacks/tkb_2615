"""ScenarioSpec loading, canonical JSON, schema versions and the inputs it refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.paths import SCENARIO_100_AGENTS

from sheltersim.experiments.scenario import ScenarioSpec


def test_100_agent_scenario_loads_and_saves_canonical_json(tmp_path: Path) -> None:
    scenario = ScenarioSpec.load(SCENARIO_100_AGENTS)
    saved = tmp_path / "scenario.json"

    scenario.save(saved)
    reloaded = ScenarioSpec.load(saved)

    assert len(scenario.profiles) == 100
    assert len({profile.id for profile in scenario.profiles}) == 100
    assert reloaded.canonical_json() == scenario.canonical_json()
    assert saved.read_text(encoding="utf-8") == scenario.canonical_json() + "\n"


def test_scenario_canonical_json_ignores_collection_input_order() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    for key in ("zones", "zone_edges", "facilities", "staff", "profiles", "scheduled_events"):
        document[key].reverse()

    reordered = ScenarioSpec.model_validate(document)

    assert reordered.canonical_json() == ScenarioSpec.load(SCENARIO_100_AGENTS).canonical_json()


def test_scenario_rejects_unknown_zone_references() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["profiles"][0]["initial_zone_id"] = "missing-zone"

    with pytest.raises(ValidationError, match="unknown zone"):
        ScenarioSpec.model_validate(document)


def test_scenario_rejects_negative_facility_capacity() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["facilities"][0]["capacity"] = -1

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ScenarioSpec.model_validate(document)


def test_scenario_rejects_duplicate_profile_ids() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["profiles"][1]["id"] = document["profiles"][0]["id"]

    with pytest.raises(ValidationError, match="duplicate IDs in profiles"):
        ScenarioSpec.model_validate(document)


def test_scenario_rejects_need_values_outside_the_normalized_range() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["profiles"][0]["initial_needs"]["hunger"] = 1.1

    with pytest.raises(ValidationError, match="less than or equal to 1"):
        ScenarioSpec.model_validate(document)


def test_scenario_json_loader_upgrades_version_one_through_v2_to_v3() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
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
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["schema_version"] = schema_version

    with pytest.raises(ValidationError, match="schema_version"):
        ScenarioSpec.from_json(json.dumps(document))


def test_scenario_loader_rejects_unversioned_structural_fields() -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["unexpected_contract_field"] = "must be versioned before acceptance"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScenarioSpec.from_json(json.dumps(document))


def test_scenario_rejects_reused_event_sequence() -> None:
    scenario_json = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    scenario_json["scheduled_events"] = [
        {
            "id": "planned-1",
            "due_minute": 1,
            "sequence": 0,
            "event_type": "facility_outage",
        },
        {
            "id": "planned-2",
            "due_minute": 2,
            "sequence": 0,
            "event_type": "facility_outage",
        },
    ]

    with pytest.raises(ValidationError, match="duplicate IDs in scheduled event sequences"):
        ScenarioSpec.model_validate(scenario_json)
