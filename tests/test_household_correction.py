from __future__ import annotations

import json

import pytest

from sheltersim.contracts.population import HouseholdCorrectionRequest
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.web.projects import ProjectAPI


def _unsafe_generated_household():
    draft = InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=20,
        duration_days=1,
        seed=42,
    ))
    scenario = draft.scenario
    people = scenario.model_configs["population"]["people"]
    # Seed 42 contains this generated minor and two older synthetic household
    # members, so the test exercises exact-ID correction without inventing IDs.
    assert people["A003"]["age_years"] == 9
    assert scenario.profiles[1].group_id == "H000002"
    return draft


def test_household_correction_creates_hash_bound_immutable_preview(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    store.create_project("project")
    api = ProjectAPI(store, recover=False)
    try:
        draft = _unsafe_generated_household()
        scenario = draft.scenario
        source_hash = digest(json.loads(scenario.canonical_json()))
        reviewed_request = {
            "municipality": "Synthetic municipality", "disaster": "earthquake",
            "evacuees": 20, "duration_days": 1, "seed": 42,
        }
        preview_id = api.workspace._cache({
            "usage_account_id": "draft-account",
            "kind": "scenario",
            "project_id": "project",
            "scenario": scenario,
            "can_start": False,
            "hash": source_hash,
            "approval_blockers": [*draft.approval_blockers],
            "facility_design": None,
            "request": reviewed_request,
            "research": None,
        })
        response = api.workspace.post(["project", "scenario-household-corrections"], {
            "preview_id": preview_id,
            "correction": {
                "schema_version": 1,
                "expected_scenario_hash": source_hash,
                "corrections": [{
                    "agent_id": "A003",
                    "household_id": "H000002",
                    "parent_ids": ["A002"],
                    "caregiver_ids": [],
                }],
            },
        })

        assert response["preview_id"] != preview_id
        assert response["source_preview_id"] == preview_id
        assert response["source_scenario_hash"] == source_hash
        assert response["corrected_agent_ids"] == ["A003"]
        assert response["request"] == reviewed_request
        assert response["facility_design"] is None
        assert response["research"] is None
        assert not response["can_start"]  # Other unreviewed minors/blockers remain.
        assert any("未成年者が8人います" in row for row in response["approval_blockers"])
        assert any("アクセシブルな往復経路を確認できない設備ゾーン" in row
                   for row in response["approval_blockers"])
        corrected = response["scenario"]
        corrected_person = corrected["model_configs"]["population_context"]["people"]["A003"]
        assert corrected_person["parent_ids"] == ["A002"]
        assert corrected_person["guardian_status"] == "accompanied"
        assert corrected["provenance"]["household_assignment_corrections"]["seed"] == 42
        assert api.workspace._preview("project", preview_id)["hash"] == source_hash
        assert api.workspace._preview("project", preview_id)["scenario"].model_configs[
            "population_context"
        ]["people"]["A003"]["parent_ids"] == []
    finally:
        api.close()


def test_household_correction_rejects_stale_hash_and_cross_household_links(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    store.create_project("project")
    api = ProjectAPI(store, recover=False)
    try:
        draft = _unsafe_generated_household()
        scenario = draft.scenario
        source_hash = digest(json.loads(scenario.canonical_json()))
        preview_id = api.workspace._cache({
            "usage_account_id": "draft-account",
            "kind": "scenario",
            "project_id": "project",
            "scenario": scenario,
            "can_start": False,
            "hash": source_hash,
            "approval_blockers": list(draft.approval_blockers),
            "facility_design": None,
            "request": None,
            "research": None,
        })
        base = {"preview_id": preview_id, "correction": {
            "schema_version": 1,
            "expected_scenario_hash": "0" * 64,
            "corrections": [{"agent_id": "A003", "household_id": "H000002",
                             "parent_ids": ["A002"], "caregiver_ids": []}],
        }}
        with pytest.raises(StorageError, match="stale_preview"):
            api.workspace.post(["project", "scenario-household-corrections"], base)
        cross_household = {
            **base,
            "correction": {
                **base["correction"],
                "expected_scenario_hash": source_hash,
                "corrections": [{"agent_id": "A003", "household_id": "H000002",
                                 "parent_ids": ["A006"], "caregiver_ids": []}],
            },
        }
        with pytest.raises(StorageError, match="same household"):
            api.workspace.post(["project", "scenario-household-corrections"], cross_household)
    finally:
        api.close()


def test_household_correction_request_requires_a_real_change() -> None:
    with pytest.raises(ValueError, match="change at least one"):
        HouseholdCorrectionRequest.model_validate({
            "schema_version": 1,
            "expected_scenario_hash": "a" * 64,
            "corrections": [{"agent_id": "A003", "household_id": "H000002"}],
        })


def _guardian_corrections(scenario: dict) -> list[dict]:
    """Link each generated unaccompanied minor to an older adult in its household."""
    people = scenario["model_configs"]["population"]["people"]
    context = scenario["model_configs"]["population_context"]["people"]
    corrections = []
    for agent_id, person in sorted(context.items()):
        age = people[agent_id]["age_years"]
        if age >= 18 or person.get("guardian_status") != "unaccompanied":
            continue
        household_id = people[agent_id]["household_id"]
        parent_id = next(
            other for other in sorted(people)
            if people[other]["household_id"] == household_id
            and people[other]["age_years"] >= max(18, age + 16)
        )
        corrections.append({"agent_id": agent_id, "household_id": household_id,
                            "parent_ids": [parent_id], "caregiver_ids": []})
    return corrections


def test_household_correction_keeps_other_blockers_from_a_real_preview(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    store.create_project("project")
    api = ProjectAPI(store, recover=False)
    try:
        # Seed 1 at 10 people has only correctable minors plus an inaccessible
        # standard-plan route, so the route blocker must survive the correction.
        preview = api.workspace.post(["project", "scenario-preview"], {"text": "", "request": {
            "municipality": "Synthetic municipality", "disaster": "earthquake",
            "evacuees": 10, "duration_days": 1, "seed": 1,
        }})
        assert any("未成年者が" in row for row in preview["approval_blockers"])
        assert any("アクセシブルな往復経路" in row for row in preview["approval_blockers"])
        response = api.workspace.post(["project", "scenario-household-corrections"], {
            "preview_id": preview["preview_id"],
            "correction": {
                "schema_version": 1,
                "expected_scenario_hash": preview["approved_hash"],
                "corrections": _guardian_corrections(preview["scenario"]),
            },
        })

        assert not any("未成年者が" in row for row in response["approval_blockers"])
        assert any("アクセシブルな往復経路" in row for row in response["approval_blockers"])
        assert response["can_start"] is False
        with pytest.raises(StorageError, match="startable reviewed scenario"):
            api.workspace.post(["project", "scenario-approve"], {
                "preview_id": response["preview_id"], "approved_hash": response["approved_hash"],
                "approved": True, "run_id": "run-blocked",
            })
        assert not store._path("project", "runs", "run-blocked").exists()
    finally:
        api.close()


def test_household_correction_refuses_a_draft_without_recorded_blockers(tmp_path) -> None:
    store = ProjectStore(tmp_path)
    store.create_project("project")
    api = ProjectAPI(store, recover=False)
    try:
        draft = _unsafe_generated_household()
        scenario = draft.scenario
        source_hash = digest(json.loads(scenario.canonical_json()))
        legacy_preview_id = api.workspace._cache({
            "usage_account_id": "draft-account", "kind": "scenario", "project_id": "project",
            "scenario": scenario, "can_start": False, "hash": source_hash,
            "facility_design": None, "request": None, "research": None,
        })
        with pytest.raises(StorageError, match="stale_preview"):
            api.workspace.post(["project", "scenario-household-corrections"], {
                "preview_id": legacy_preview_id,
                "correction": {
                    "schema_version": 1, "expected_scenario_hash": source_hash,
                    "corrections": [{"agent_id": "A003", "household_id": "H000002",
                                     "parent_ids": ["A002"], "caregiver_ids": []}],
                },
            })
    finally:
        api.close()
