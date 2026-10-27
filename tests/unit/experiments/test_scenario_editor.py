"""Typed scenario editing keeps operator drafts detached from live runs.

A frozen Scenario resumed by a saved job, and the editor through the local HTTP API,
are checked in tests/integration/test_scenario_editor.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sheltersim.experiments.scenario_editor import (
    ScenarioEditorRequest,
    build_scenario_preview,
    list_scenario_presets,
)
from sheltersim.web.demo import DemoError, DemoService


def _request(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "preset_id": "water_outage",
        "overrides": overrides,
    }


def test_preview_is_validated_reproducible_and_detached_from_live_scenario() -> None:
    demo = DemoService()
    before = demo.scenario.canonical_json()
    before_state = demo.state()
    document = _request(
        staff_count=7,
        toilet_units=8,
        water_ml=3_000_000,
        food_meal_units=120,
        event_frequency="disabled",
        social_propagation_enabled=True,
    )
    request = ScenarioEditorRequest.model_validate(document)

    scenario, preview = build_scenario_preview(request)
    same_scenario, same_preview = build_scenario_preview(request)

    assert preview.to_dict() == same_preview.to_dict()
    assert scenario.canonical_json() == same_scenario.canonical_json()
    assert len(scenario.profiles) == 100
    assert len(scenario.staff) == 7
    assert (
        sum(item.working_units or 0 for item in scenario.facilities if item.kind == "toilet") == 8
    )
    assert scenario.initial_stock is not None
    assert scenario.initial_stock.water_ml == 3_000_000
    assert scenario.initial_stock.food_meal_units == 120
    assert scenario.scheduled_events == ()
    assert scenario.model_configs["social_propagation"]["enabled"] is True
    assert preview.fields["staff_count"] == {"value": 7, "origin": "operator"}
    assert preview.seed == 2026
    assert demo.scenario.canonical_json() == before
    assert demo.state() == before_state


def test_behavior_policy_variant_is_hashable_visible_and_allowlisted() -> None:
    document = _request(behavior_policy="priority-rule-v1")
    request = ScenarioEditorRequest.model_validate(document)
    scenario, preview = build_scenario_preview(request)
    baseline, baseline_preview = build_scenario_preview(
        ScenarioEditorRequest.model_validate(_request())
    )
    baseline_json = baseline.canonical_json()

    assert scenario.model_configs["behavior"]["policy"] == "priority-rule-v1"
    assert preview.behavior_policy == "priority-rule-v1"
    assert preview.fields["behavior_policy"] == {
        "value": "priority-rule-v1",
        "origin": "operator",
    }
    assert preview.scenario_hash != baseline_preview.scenario_hash
    assert preview.scenario_family_hash != baseline_preview.scenario_family_hash
    assert len(list_scenario_presets()[0]["behavior_policy_catalog"]) == 3
    assert baseline.model_configs["behavior"]["policy"] == "sleep-aware-utility-softmax-v2"
    assert baseline.model_configs["behavior"]["source_id"] == "ASM-SLEEP-AWARE-BEHAVIOR-V2"
    assert baseline.canonical_json() == baseline_json
    assert scenario.model_configs["behavior"]["source_id"] == ("ASM-PRIORITY-RULE-BEHAVIOR-V1")

    with pytest.raises(ValidationError):
        ScenarioEditorRequest.model_validate(_request(behavior_policy="invented-policy"))


def test_invalid_values_and_unsupported_fields_do_not_change_the_live_scenario() -> None:
    demo = DemoService()
    original = demo.scenario.canonical_json()
    original_state = demo.state()

    with pytest.raises(ValidationError):
        ScenarioEditorRequest.model_validate(_request(staff_count=True))
    with pytest.raises(ValidationError):
        ScenarioEditorRequest.model_validate(_request(staff_count=21))
    with pytest.raises(ValidationError):
        ScenarioEditorRequest.model_validate({**_request(), "agent_count": 101})
    with pytest.raises(DemoError, match="Extra inputs are not permitted"):
        demo.freeze_scenario({**_request(), "agent_count": 101})

    assert demo.scenario.canonical_json() == original
    assert demo.state() == original_state


def test_editor_requires_explicit_schema_and_rejects_coerced_or_unsupported_values() -> None:
    with pytest.raises(ValidationError):
        ScenarioEditorRequest.model_validate({"preset_id": "baseline_100_7d"})
    with pytest.raises(ValidationError):
        ScenarioEditorRequest.model_validate(_request(social_propagation_enabled="true"))
    baseline_without_stock = ScenarioEditorRequest.model_validate(
        {
            "schema_version": 1,
            "preset_id": "baseline_100_7d",
            "overrides": {"water_ml": 10},
        }
    )
    with pytest.raises(ValueError, match="initial_stock"):
        build_scenario_preview(baseline_without_stock)


def test_freeze_creates_a_new_run_and_locks_inputs_after_start() -> None:
    demo = DemoService()
    document = _request(staff_count=6, water_ml=4_000_000)
    frozen = demo.freeze_scenario(document)
    assert frozen["scenario"]["frozen"] is True
    assert frozen["scenario"]["execution_mode"] == "live_compute"
    assert frozen["scenario"]["run_id"].startswith("live-")
    assert frozen["scenario"]["scenario_hash"] == demo.state()["scenario_hash"]
    assert frozen["scenario"]["fields"]["staff_count"]["origin"] == "operator"
    assert demo.state()["scenario_source"] == "operator_configured_scenario"

    repeated = DemoService().freeze_scenario(document)
    assert repeated["scenario"]["scenario_hash"] == frozen["scenario"]["scenario_hash"]
    assert repeated["scenario"]["run_id"] != frozen["scenario"]["run_id"]

    demo.dispatch("start")
    frozen_hash = demo.state()["scenario_hash"]
    with pytest.raises(DemoError, match="locked"):
        demo.freeze_scenario(_request(staff_count=9))
    assert demo.state()["scenario_hash"] == frozen_hash


def test_frozen_scenario_and_run_manifest_restore_after_restart(tmp_path) -> None:
    storage_path = tmp_path / ".demo-scenarios" / "frozen.json"
    demo = DemoService(scenario_storage_path=storage_path)
    frozen = demo.freeze_scenario(_request(staff_count=6, water_ml=4_000_000))

    restarted = DemoService(scenario_storage_path=storage_path)

    assert restarted.state()["started"] is False
    assert restarted.state()["scenario_id"] == frozen["scenario"]["scenario_id"]
    assert restarted.state()["scenario_hash"] == frozen["scenario"]["scenario_hash"]
    assert restarted.state()["run_id"] == frozen["scenario"]["run_id"]
    assert restarted.state()["scenario_manifest"] == frozen["scenario"]
    assert restarted.scenario.initial_stock is not None
    assert restarted.scenario.initial_stock.water_ml == 4_000_000


def test_catalog_exposes_same_seed_common_baseline_comparison_groups() -> None:
    presets = {item["preset_id"]: item for item in list_scenario_presets()}
    group = presets["water_outage"]["comparison_group"]
    assert presets["water_intervention"]["comparison_group"] == group
    assert presets["delivery_delay"]["comparison_group"] == group
    assert presets["delivery_intervention"]["comparison_group"] == group
    assert presets["water_outage"]["seed"] == presets["delivery_delay"]["seed"] == 2026
    assert all(item["agent_count"] <= 100 for item in presets.values())


