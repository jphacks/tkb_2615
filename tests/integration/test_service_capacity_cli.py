"""The service-capacity diagnostic is available from the CLI.

The diagnostic itself is checked in tests/unit/experiments/test_service_capacity.py.
"""

from __future__ import annotations

import json

from sheltersim.__main__ import main
from sheltersim.experiments.service_capacity import SERVICE_CAPACITY_SOURCE_ID


def test_cli_prints_the_capacity_report(generated_scenario_with, tmp_path, capsys) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(generated_scenario_with(evacuees=100).canonical_json())
    assert main(["service-capacity-diagnostic", "--scenario", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_id"] == SERVICE_CAPACITY_SOURCE_ID
    assert {row["service"]: row["status"] for row in report["services"]} == {
        "toilet": "ok",
        "water": "ok",
        "meal": "ok",
    }
