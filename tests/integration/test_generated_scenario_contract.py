"""The shared generated Scenario comes from the UI preview and its run is pinned."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.storage.projects import digest

ROOT = Path(__file__).parents[1]
REGRESSION_CONTRACT = ROOT / "fixtures" / "regression-contract.json"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def test_generated_scenario_run_matches_the_pinned_contract(
    generated_scenario, tmp_path: Path
) -> None:
    contract = json.loads(REGRESSION_CONTRACT.read_text(encoding="utf-8"))["generated_small"]
    scenario_hash = _sha256(generated_scenario.canonical_json())

    assert generated_scenario.id == contract["scenario_id"]
    assert generated_scenario.schema_version == contract["scenario_schema_version"]
    assert generated_scenario.seed == contract["seed"]
    assert generated_scenario.duration_minutes == contract["duration_minutes"]
    assert len(generated_scenario.profiles) == contract["profile_count"]
    assert scenario_hash == contract["scenario_sha256"]

    output = tmp_path / "generated-small"
    world = run_scenario(generated_scenario, output, trace_level=TraceLevel.OFF)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    realized = [
        json.loads(line)["event"]["event_id"]
        for line in (output / "events_realized.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert world.schema_version == contract["world_state_schema_version"]
    assert manifest["run_status"] == "completed"
    assert manifest["scenario_hash"] == scenario_hash
    assert manifest["final_state_hash"] == contract["expected_final_state_sha256"]
    assert len(realized) == contract["realized_event_count"]
    assert _sha256("\n".join(realized)) == contract["realized_event_ids_sha256"]
