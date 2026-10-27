"""Planning check that toilet, water and meal units can serve their daily demand.

The CLI report is checked in tests/integration/test_service_capacity_cli.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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

ROOT = Path(__file__).parents[3]
RESEARCH_FIXTURE = ROOT / "fixtures" / "scenarios" / "research_operational_100_7d.json"


def _request(evacuees: int) -> InitialScenarioRequest:
    return InitialScenarioRequest(
        municipality="Synthetic municipality",
        disaster="earthquake",
        evacuees=evacuees,
        duration_days=1,
        seed=7,
    )


SMALL_FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"


def _rows(scenario: ScenarioSpec) -> dict[str, dict]:
    return {row["service"]: row for row in service_capacity_diagnostic(scenario)["services"]}


def _with(scenario: ScenarioSpec, change) -> ScenarioSpec:
    document = scenario.model_dump(mode="json")
    change(document)
    return ScenarioSpec.model_validate(document)


def test_required_units_fit_the_daily_work_in_the_target_load() -> None:
    window = OPERATING_MINUTES_PER_DAY * TARGET_UTILIZATION
    assert required_service_units(0) == 0
    assert required_service_units(window) == 1
    assert required_service_units(window + 1) == 2


@pytest.mark.parametrize("evacuees", [10, 30, 100, 300])
def test_default_generated_scenarios_have_no_capacity_warnings(evacuees: int) -> None:
    # This checks the generator's own defaults and warnings across sizes, so it
    # builds drafts directly rather than through the shared preview.
    draft = InitialScenarioBuilder().build(_request(evacuees))
    report = service_capacity_diagnostic(draft.scenario)
    assert report["warnings"] == []
    assert {row["service"]: row["status"] for row in report["services"]} == {
        "toilet": "ok",
        "water": "ok",
        "meal": "ok",
    }
    assert not any("休みなく" in warning or "一度も" in warning for warning in draft.warnings)


def test_toilets_out_of_service_are_structurally_short(generated_scenario_with) -> None:
    def keep_three_toilets(document: dict) -> None:
        toilets = [item for item in document["facilities"] if item["kind"] == "toilet"]
        for item in toilets[3:]:
            item["working_units"] = 0

    toilet = _rows(_with(generated_scenario_with(evacuees=100), keep_three_toilets))["toilet"]
    # 100 people x 5 uses x 6 minutes = 3,000 minutes against 3 units x 960 minutes.
    assert toilet["status"] == "structurally_short"
    assert toilet["utilization"] == pytest.approx(3000 / (3 * 960))
    assert toilet["required_units"] == 4
    assert toilet["standard_units"] == 2


def test_water_and_meal_are_not_modeled_without_resource_service() -> None:
    rows = _rows(ScenarioSpec.load(SMALL_FIXTURE))
    assert rows["water"]["status"] == "not_modeled"
    assert rows["meal"]["status"] == "not_modeled"
    assert rows["water"]["warnings"] == rows["meal"]["warnings"] == []


def test_stations_without_staff_in_their_zone_never_start(generated_scenario_with) -> None:
    def move_station_staff_away(document: dict) -> None:
        for member in document["staff"]:
            member["zone_id"] = "staff"

    rows = _rows(_with(generated_scenario_with(evacuees=30), move_station_staff_away))
    assert rows["water"]["status"] == rows["meal"]["status"] == "no_staff"
    assert rows["water"]["serving_units"] == 0
    assert any("給水は一度も始まりません" in warning for warning in rows["water"]["warnings"])


def test_serving_units_are_limited_by_skilled_staff_in_the_zone(generated_scenario_with) -> None:
    def keep_one_station_staff(document: dict) -> None:
        station_staff = [member for member in document["staff"] if member["zone_id"] == "food"]
        for member in station_staff[1:]:
            member["zone_id"] = "staff"

    water = _rows(_with(generated_scenario_with(evacuees=30), keep_one_station_staff))["water"]
    assert water["units"] == 2
    assert water["serving_units"] == 1
    # Generated coefficients: 0.001/minute growth, relief 0.35, action threshold 0.25.
    assert water["uses_per_person_day"] == pytest.approx(0.001 * 1440 / 0.25)
    # 30 x 5.76 uses x 5 minutes = 864 minutes on the one staffed unit.
    assert water["utilization"] == pytest.approx(864 / 960)
    assert water["status"] == "above_target"
    assert water["required_units"] == 2


def test_a_floor_plan_station_counts_as_one_unit_whatever_its_capacity(
    generated_scenario,
) -> None:
    # physical_service_capacity serves one person at a time per surveyed point.
    def enlarge_main_station(document: dict) -> None:
        station = next(
            item for item in document["facilities"] if item["id"] == "water_station_main"
        )
        station["capacity"] = 3

    stations = sum(item.kind == "water_station" for item in generated_scenario.facilities)
    assert _rows(_with(generated_scenario, enlarge_main_station))["water"]["units"] == stations


def test_research_fixture_water_is_far_beyond_one_station() -> None:
    water = _rows(ScenarioSpec.load(RESEARCH_FIXTURE))["water"]
    assert water["uses_per_person_day"] == pytest.approx(0.006 * 1440 / 0.25)
    assert water["status"] == "structurally_short"
    assert water["utilization"] > 10


def test_diagnostic_does_not_change_the_scenario(generated_scenario) -> None:
    scenario = generated_scenario
    before = scenario.canonical_json()
    service_capacity_diagnostic(scenario)
    assert scenario.canonical_json() == before


def test_unknown_phase_is_rejected(generated_scenario) -> None:
    with pytest.raises(ValueError, match="phase"):
        service_capacity_diagnostic(generated_scenario, phase="weekly")


def test_capacity_planning_assumption_matches_the_manifest() -> None:
    manifest = json.loads((ROOT / "fixtures" / "source-manifest.json").read_text("utf-8"))
    entry = next(
        item for item in manifest["assumptions"] if item["id"] == SERVICE_CAPACITY_SOURCE_ID
    )
    assert entry["values"]["operating_minutes_per_day"] == OPERATING_MINUTES_PER_DAY
    assert entry["values"]["target_utilization"] == TARGET_UTILIZATION
    assert entry["values"]["toilet_uses_per_person_day"] == 5
    assert entry["context_sources"] == ["SRC-CAO-TOILET-GUIDELINE-2024-12"]
