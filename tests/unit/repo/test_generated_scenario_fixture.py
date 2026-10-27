"""The shared generated Scenario fixture comes from the UI preview, with the floor plan
and awareness a UI Scenario has, and each test gets a fresh copy.

That its Run matches the pinned contract is checked in
tests/integration/test_generated_scenario_contract.py.
"""

from __future__ import annotations

import json

from sheltersim.storage.projects import digest


def test_generated_scenario_has_the_floor_plan_and_awareness_of_a_ui_scenario(
    generated_scenario_preview, generated_scenario
) -> None:
    configs = generated_scenario.model_configs

    assert generated_scenario_preview["can_start"] is True
    assert generated_scenario_preview["approval_blockers"] == []
    assert generated_scenario_preview["approved_hash"] == digest(
        json.loads(generated_scenario.canonical_json())
    )
    assert generated_scenario.provenance["scenario_input"]["extraction_source"] == (
        "structured_form"
    )
    assert configs["facility_geometry"]["facility_points"]
    assert configs["agent_dynamics"]["policy_version"]
    # Lets an operator approve a temporary space (#80).
    assert configs["simulator_extensions"] == {"enabled": True}
    assert len(generated_scenario.profiles) == 20
    assert generated_scenario.duration_minutes == 360


def test_generated_scenario_is_a_fresh_copy(generated_scenario_preview, generated_scenario) -> None:
    generated_scenario.model_configs.pop("agent_dynamics")

    assert "agent_dynamics" in generated_scenario_preview["scenario"]["model_configs"]
