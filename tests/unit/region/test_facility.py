"""The generator gives each Scenario standard toilets, stations and station staff, and
standard plans give every room an accessible doorway and keep their requested shares.

These check the generator itself, so they build drafts directly. Stock and
resupply are checked in tests/unit/region/test_conditions.py, and the runs in
tests/integration/test_standard_provisioning.py.
"""

from __future__ import annotations

import pytest

from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.region.facility import (
    SYNTHETIC_STAFFING_SOURCE_ID,
    FacilityDesignRequest,
    StandardShelterGenerator,
    standard_service_counts,
)
from sheltersim.region.models import ACCESSIBLE_ROUTE_MIN_WIDTH_M


def _draft(evacuees: int, duration_days: float = 1, **overrides: object):
    return InitialScenarioBuilder().build(
        InitialScenarioRequest(
            municipality="Synthetic municipality",
            disaster="earthquake",
            evacuees=evacuees,
            duration_days=duration_days,
            seed=7,
            **overrides,
        )
    )


def _kinds(scenario) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for facility in scenario.facilities:
        grouped.setdefault(facility.kind, []).append(facility)
    return grouped


def test_standard_counts_take_the_larger_of_the_standard_and_the_demand() -> None:
    assert standard_service_counts(10) == {
        "toilet_standard": 1,
        "toilet_demand": 1,
        "water_station": 1,
        "meal_distribution": 1,
    }
    # 100 people: 1 per 50 gives 2 toilets, but 500 uses x 6 minutes needs 4 at 0.8 load.
    assert standard_service_counts(100) == {
        "toilet_standard": 2,
        "toilet_demand": 4,
        "water_station": 4,
        "meal_distribution": 1,
    }


def test_generated_facilities_follow_the_standard_counts() -> None:
    scenario = _draft(100).scenario
    kinds = _kinds(scenario)
    assert len(kinds["toilet"]) == 4
    assert {item.id for item in kinds["water_station"]} == {
        "water_station_main",
        "water_station_02",
        "water_station_03",
        "water_station_04",
    }
    assert {item.id for item in kinds["meal_distribution"]} == {"meal_distribution_main"}
    assert {item.zone_id for item in kinds["water_station"] + kinds["meal_distribution"]} == {
        "food"
    }
    # Entered accessible toilets still count on top of the planning standard.
    assert len(_kinds(_draft(100, accessible_toilet_count=3).scenario)["toilet"]) == 5


def test_each_station_gets_a_station_staff_member_in_its_zone() -> None:
    scenario = _draft(100).scenario
    general = [member for member in scenario.staff if member.zone_id == "staff"]
    station = [member for member in scenario.staff if member.zone_id == "food"]
    assert len(general) == 1
    assert "support" in general[0].skills
    assert len(station) == 5
    assert all(set(member.skills) == {"water_service", "meal_service"} for member in station)


@pytest.mark.parametrize(
    ("entered", "general", "station"), [(3, 1, 2), (1, 1, 0), (0, 0, 0), (20, 15, 5)]
)
def test_an_entered_staff_total_keeps_one_general_staff_member(
    entered: int, general: int, station: int
) -> None:
    draft = _draft(100, staff_count=entered)
    staff = draft.scenario.staff
    assert sum(member.zone_id == "staff" for member in staff) == general
    assert sum(member.zone_id == "food" for member in staff) == station
    record = next(
        item
        for item in draft.scenario.provenance["assumptions"]
        if item["source_id"] == SYNTHETIC_STAFFING_SOURCE_ID
    )
    assert record["value"]["entered_total"] == entered


@pytest.mark.parametrize("population", [10, 30, 100])
def test_small_standard_plans_give_every_room_an_accessible_doorway(population: int) -> None:
    design = StandardShelterGenerator().generate(FacilityDesignRequest(population=population))
    assert all(route.accessible and route.width_m >= ACCESSIBLE_ROUTE_MIN_WIDTH_M
               for route in design.geometry.routes)
    assert design.capacity_supported >= population
    layout = next(row for row in design.assumptions if row["subject"] == "standard_shelter_layout")
    assert layout["value"]["frontage_widened_zones"]
    assert any("出入口幅" in warning for warning in design.warnings)


def test_large_standard_plans_keep_their_requested_shares() -> None:
    design = StandardShelterGenerator().generate(FacilityDesignRequest(population=300))
    layout = next(row for row in design.assumptions if row["subject"] == "standard_shelter_layout")
    assert layout["value"]["frontage_widened_zones"] == []
    assert layout["value"]["zone_shares"]["intake"] == pytest.approx(0.05)
