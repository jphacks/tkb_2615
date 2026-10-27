"""Project draft adapter for validated standard-facility revisions.

This adapter uses cached research and generation inputs. It never researches again,
calls a provider, creates a drawing asset, or mutates an approved Scenario/Run.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sheltersim.experiments.initial_scenario import InitialScenarioBuilder, InitialScenarioRequest
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.region.facility import FacilityDesign, revalidate_standard_facility
from sheltersim.region.models import RegionResearchResult
from sheltersim.region.population import (
    age_bands_from_observations,
    household_mix_from_observations,
)
from sheltersim.storage.projects import StorageError, digest, safe_document


def facility_preview(workspace: Any, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Revalidate a standard candidate and mint a normal Scenario approval preview.

    Required fields: base_preview_id, base_geometry_hash, geometry,
    editable_geometry. Optional edit_source_id names an operator declaration only.
    Invalid candidate geometry is returned for correction with can_start:false;
    it receives no approval hash or approval-ready preview ID.
    """
    from sheltersim.web.projects import fields

    fields(
        body,
        {"base_preview_id", "base_geometry_hash", "geometry", "editable_geometry"},
        {"edit_source_id"},
    )
    edit_source_id = body.get("edit_source_id", "SRC-OPERATOR-FACILITY-CORRECTION")
    if not isinstance(edit_source_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", edit_source_id
    ):
        raise StorageError("invalid_facility_edit_source_id")
    base = workspace._preview(project_id, body["base_preview_id"])
    if (
        base.get("kind") != "scenario"
        or base.get("project_id") != project_id
        or not isinstance(base.get("facility_design"), dict)
        or not isinstance(base.get("request"), dict)
        or not isinstance(base.get("research"), dict)
    ):
        raise StorageError("standard_facility_preview_required")
    if digest(base["facility_design"].get("geometry")) != body["base_geometry_hash"]:
        raise StorageError("facility_base_geometry_changed")
    try:
        design = FacilityDesign.model_validate(base["facility_design"])
        request = InitialScenarioRequest.model_validate(base["request"])
        research = RegionResearchResult.model_validate(base["research"])
        source_scenario = base["scenario"]
        if not isinstance(source_scenario, ScenarioSpec):
            source_scenario = ScenarioSpec.model_validate(source_scenario)
    except (TypeError, KeyError, ValueError) as exc:
        raise StorageError("facility_base_preview_incompatible") from exc
    if source_scenario.provenance.get("plan_upload"):
        raise StorageError("standard_facility_preview_required")
    if not isinstance(base.get("usage_account_id"), str):
        raise StorageError("facility_base_usage_account_missing")

    # Reject unsafe/non-JSON input before returning or saving it. A malformed
    # geometric candidate otherwise remains editable in the caller's UI.
    candidate = {"geometry": body["geometry"], "editable_geometry": body["editable_geometry"]}
    if safe_document(candidate) != candidate:
        raise StorageError("remove sensitive text before previewing facility geometry")
    result = revalidate_standard_facility(
        design,
        geometry=candidate["geometry"],
        editable_geometry=candidate["editable_geometry"],
        edit_source_id=edit_source_id,
    )
    issues = [item.model_dump(mode="json") for item in result.issues]
    common = {
        "schema_version": 1,
        "status": result.status,
        "issues": issues,
        "base_preview_id": body["base_preview_id"],
        "base_geometry_hash": body["base_geometry_hash"],
        "capacity_supported": result.design.capacity_supported,
        "effective_sleeping_area_m2": result.effective_sleeping_area_m2,
        "validation": "region_structural_rules_only",
        "runtime_verified": False,
    }
    if result.status != "valid":
        return {
            **common,
            "can_start": False,
            "approved_hash": None,
            "preview_id": None,
            "geometry": candidate["geometry"],
            "editable_geometry": candidate["editable_geometry"],
            "request": request.model_dump(mode="json"),
            "warnings": list(result.design.warnings),
            "approval_blockers": [
                item.message for item in result.issues if item.severity == "error"
            ],
        }

    selected = source_scenario.provenance.get("selected_shelter", {})
    selected_shelter_id = selected.get("id") if isinstance(selected, dict) else None
    try:
        draft = InitialScenarioBuilder().build(
            request,
            research=research,
            standard_design=result.design,
            selected_shelter_id=selected_shelter_id,
            age_bands=age_bands_from_observations(research.population_observations),
            household_mix=household_mix_from_observations(research.population_observations),
        )
        document = draft.scenario.model_dump(mode="json")
        document["provenance"]["scenario_input"] = source_scenario.provenance.get(
            "scenario_input", {}
        )
        document["provenance"].get("selected_shelter", {}).pop("address", None)
        for shelter in document["provenance"].get("region_research", {}).get("shelters", []):
            shelter.pop("address", None)
        document = safe_document(document)
        scenario = ScenarioSpec.model_validate(document)
    except (TypeError, KeyError, ValueError) as exc:
        raise StorageError("facility_scenario_invalid") from exc
    scenario_document = json.loads(scenario.canonical_json())
    approval_hash = digest(scenario_document)
    geometry_document = result.design.geometry.model_dump(mode="json")
    geometry_hash = digest(geometry_document)
    design_document = result.design.model_dump(mode="json")
    can_start = draft.can_start and result.design.capacity_supported >= request.evacuees
    approval_blockers = list(draft.approval_blockers)
    if result.design.capacity_supported < request.evacuees:
        approval_blockers.append(
            "Edited facility planning capacity is below the requested initial population; "
            "review capacity or population before approval."
        )
    preview_id = workspace._cache(
        {
            "kind": "scenario",
            "project_id": project_id,
            "scenario": scenario,
            "can_start": can_start,
            "hash": approval_hash,
            "approval_blockers": approval_blockers,
            "usage_account_id": base["usage_account_id"],
            "facility_design": design_document,
            "request": base["request"],
            "research": base["research"],
            "facility_revision": {
                "base_preview_id": body["base_preview_id"],
                "base_geometry_hash": body["base_geometry_hash"],
                "geometry_hash": geometry_hash,
            },
        }
    )
    return {
        **common,
        "scenario": scenario_document,
        "approved_hash": approval_hash,
        "preview_id": preview_id,
        "draft_id": base["usage_account_id"],
        "facility_design": design_document,
        "geometry": geometry_document,
        "editable_geometry": result.design.editable_geometry.model_dump(mode="json"),
        "geometry_hash": geometry_hash,
        "request": request.model_dump(mode="json"),
        "research": base["research"],
        "can_start": can_start,
        "approval_blockers": approval_blockers,
        "warnings": list(draft.warnings),
        "llm_usage": {"mode": "not_called", "api_request_count": 0, "spent_usd": 0},
    }
