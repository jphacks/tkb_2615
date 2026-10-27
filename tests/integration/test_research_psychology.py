"""A generated Scenario runs with the research psychology model and records it.

How the model is selected is checked in tests/unit/policies/test_registry.py.
"""

from __future__ import annotations

import json

from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.experiments.runner import run_scenario


def test_generated_research_scenario_runs_and_records_its_model(
    generated_scenario, tmp_path
) -> None:
    run_scenario(generated_scenario, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert manifest["policy_implementations"]["psychology"] == (
        INTEGRATED_PSYCHOLOGY_POLICY_VERSION
    )
