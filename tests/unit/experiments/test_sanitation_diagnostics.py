"""Planning benchmark checks for the toilet-capacity command."""

import json
from pathlib import Path

from sheltersim.experiments.sanitation_diagnostics import toilet_planning_diagnostic
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def test_toilet_planning_diagnostic_uses_working_units_and_never_changes_behavior() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["facilities"] = [
        {
            "id": "toilet-a",
            "kind": "toilet",
            "zone_id": "toilet",
            "capacity": 4,
            "working_units": 2,
        },
        {"id": "water", "kind": "water_station", "zone_id": "toilet", "capacity": 20},
    ]
    scenario = ScenarioSpec.model_validate(document)

    initial = toilet_planning_diagnostic(scenario, phase="initial")
    long_term = toilet_planning_diagnostic(scenario, phase="long-term")

    assert initial["available_working_toilet_units"] == 2
    assert initial["planning_target_toilet_units"] == 2
    assert long_term["planning_target_toilet_units"] == 5
    assert long_term["shortage_units"] == 3
    assert long_term["planning_total_uses_per_day"] == len(scenario.profiles) * 5
    assert long_term["warnings"]
    assert "行動選択確率" in long_term["interpretation"]
