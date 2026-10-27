"""Runtime floor-plan changes are checked before they are approved."""

from __future__ import annotations

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import validate_geometry_change


def _transfer(facility_id: str, from_zone_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "equipment_id": facility_id,
        "from_zone_id": from_zone_id,
        "to_zone_id": "sleeping",
        "point_m": {"x": 5.0, "y": 9.0},
    }


@pytest.mark.parametrize(
    ("facility_id", "skill"),
    [("water_station_main", "water_service"), ("meal_distribution_main", "meal_service")],
)
def test_a_staffed_station_moves_only_to_an_area_where_its_staff_work(
    generated_scenario, facility_id, skill
) -> None:
    # Only staff in a station's own area serve it (#83); the water and meal
    # staff of a generated Scenario work in the food area.
    world = WorldState.from_scenario(generated_scenario)

    with pytest.raises(ValueError, match=f"staff_unavailable: no staff with {skill}"):
        validate_geometry_change(
            world, "transfer_equipment", facility_id, _transfer(facility_id, "food"), None
        )

    world.staff["staff_002"].zone_id = "sleeping"
    validate_geometry_change(
        world, "transfer_equipment", facility_id, _transfer(facility_id, "food"), None
    )


def test_a_toilet_needs_no_staff_where_it_moves(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)

    validate_geometry_change(
        world, "transfer_equipment", "toilet_01", _transfer("toilet_01", "toilet"), None
    )
