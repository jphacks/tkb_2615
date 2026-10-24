"""Planning check that toilet, water and meal units can serve their daily demand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.__main__ import main
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.service_capacity import (
    OPERATING_MINUTES_PER_DAY,
    SERVICE_CAPACITY_SOURCE_ID,
    TARGET_UTILIZATION,
    required_service_units,
    service_capacity_diagnostic,
)

ROOT = Path(__file__).parents[1]
RESEARCH_FIXTURE = ROOT / "fixtures" / "scenarios" / "research_operational_100_7d.json"


def _request(evacuees: int, **overrides: object) -> InitialScenarioRequest:
    return InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=evacuees,
        duration_days=1,
        seed=7,
        **overrides,
    )


def _stocked_request(evacuees: int) -> InitialScenarioRequest:
    return _request(
        evacuees,
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "water_ml": 9_000 * evacuees,
                "food_meal_units": 9 * evacuees,
                "source_kind": "assumption",
                "source_ids": ["ASM-TEST-ENTERED-STOCK"],
            }
        },
    )


def _rows(scenario: ScenarioSpec) -> dict[str, dict]:
    return {row["service"]: row for row in service_capacity_diagnostic(scenario)["services"]}


def test_required_units_fit_the_daily_work_in_the_target_load() -> None:
    window = OPERATING_MINUTES_PER_DAY * TARGET_UTILIZATION
    assert required_service_units(0) == 0
    assert required_service_units(window) == 1
    assert required_service_units(window + 1) == 2


def test_default_toilets_cannot_serve_one_hundred_people() -> None:
    # 100 people x 5 uses x 6 minutes = 3,000 minutes against 3 units x 960 minutes.
    draft = InitialScenarioBuilder().build(_request(100))
    toilet = _rows(draft.scenario)["toilet"]
    assert toilet["status"] == "structurally_short"
    assert toilet["utilization"] == pytest.approx(3000 / (3 * 960))
    assert toilet["required_units"] == 4
    assert toilet["standard_units"] == 2
    assert any("トイレは休みなく稼働しても" in warning for warning in draft.warnings)


def test_small_default_scenarios_have_no_capacity_warnings() -> None:
    scenario = InitialScenarioBuilder().build(_request(10)).scenario
    report = service_capacity_diagnostic(scenario)
    assert report["warnings"] == []
    assert _rows(scenario)["toilet"]["status"] == "ok"


def test_water_and_meal_are_not_modeled_without_resource_service() -> None:
    rows = _rows(InitialScenarioBuilder().build(_request(30)).scenario)
    assert rows["water"]["status"] == "not_modeled"
    assert rows["meal"]["status"] == "not_modeled"
    assert rows["water"]["warnings"] == rows["meal"]["warnings"] == []


def test_stocked_generated_scenario_reports_missing_service_staff() -> None:
    # Generated staff wait in the staff zone, so water and meal service never start.
    draft = InitialScenarioBuilder().build(_stocked_request(30))
    rows = _rows(draft.scenario)
    assert rows["water"]["status"] == rows["meal"]["status"] == "no_staff"
    assert rows["water"]["serving_units"] == 0
    assert any("給水は一度も始まりません" in warning for warning in draft.warnings)


def test_serving_units_are_limited_by_skilled_staff_in_the_zone() -> None:
    document = InitialScenarioBuilder().build(_stocked_request(30)).scenario.model_dump(mode="json")
    station = next(item for item in document["facilities"] if item["kind"] == "water_station")
    station["capacity"] = 3
    document["staff"].append(
        {"id": "water_staff_01", "zone_id": station["zone_id"], "skills": ["water_service"]}
    )
    water = _rows(ScenarioSpec.model_validate(document))["water"]
    assert water["units"] == 3
    assert water["serving_units"] == 1
    # Generated coefficients: 0.001/minute growth, relief 0.35, action threshold 0.25.
    assert water["uses_per_person_day"] == pytest.approx(0.001 * 1440 / 0.25)
    # 30 x 5.76 uses x 5 minutes = 864 minutes on the one staffed unit.
    assert water["utilization"] == pytest.approx(864 / 960)
    assert water["status"] == "above_target"
    assert water["required_units"] == 2


def test_research_fixture_water_is_far_beyond_one_station() -> None:
    water = _rows(ScenarioSpec.load(RESEARCH_FIXTURE))["water"]
    assert water["uses_per_person_day"] == pytest.approx(0.006 * 1440 / 0.25)
    assert water["status"] == "structurally_short"
    assert water["utilization"] > 10


def test_diagnostic_does_not_change_the_scenario() -> None:
    scenario = InitialScenarioBuilder().build(_stocked_request(30)).scenario
    before = scenario.canonical_json()
    service_capacity_diagnostic(scenario)
    assert scenario.canonical_json() == before


def test_unknown_phase_is_rejected() -> None:
    scenario = InitialScenarioBuilder().build(_request(10)).scenario
    with pytest.raises(ValueError, match="phase"):
        service_capacity_diagnostic(scenario, phase="weekly")


def test_capacity_planning_assumption_matches_the_manifest() -> None:
    manifest = json.loads((ROOT / "fixtures" / "source-manifest.json").read_text("utf-8"))
    entry = next(
        item for item in manifest["assumptions"] if item["id"] == SERVICE_CAPACITY_SOURCE_ID
    )
    assert entry["values"]["operating_minutes_per_day"] == OPERATING_MINUTES_PER_DAY
    assert entry["values"]["target_utilization"] == TARGET_UTILIZATION
    assert entry["values"]["toilet_uses_per_person_day"] == 5
    assert entry["context_sources"] == ["SRC-CAO-TOILET-GUIDELINE-2024-12"]


def test_cli_prints_the_capacity_report(tmp_path, capsys) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(InitialScenarioBuilder().build(_request(100)).scenario.canonical_json())
    assert main(["service-capacity-diagnostic", "--scenario", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_id"] == SERVICE_CAPACITY_SOURCE_ID
    assert {row["service"]: row["status"] for row in report["services"]} == {
        "toilet": "structurally_short",
        "water": "not_modeled",
        "meal": "not_modeled",
    }
