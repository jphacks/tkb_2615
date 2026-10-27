"""A Run of the shared generated Scenario matches fixtures/regression-contract.json.

The fixture itself is checked in tests/unit/repo/test_generated_scenario_fixture.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from support.paths import REGRESSION_CONTRACT

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
