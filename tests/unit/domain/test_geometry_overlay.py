"""Runtime floor-plan geometry: per-phase parsing and checks before approval.

That tick phases reuse the parsed geometry without changing results is checked
in tests/integration/test_geometry_config_cache.py.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.contracts.spatial import SpatialConstraints
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import (
    GEOMETRY_OVERLAY_KEY,
    effective_geometry,
    effective_spatial_constraints,
    geometry_config_scope,
    validate_geometry_change,
)


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


def test_scope_parses_each_config_object_once(generated_scenario, count_validations):
    world = WorldState.from_scenario(generated_scenario)
    raw = world.model_configs["facility_geometry"]
    expected = FacilityGeometry.model_validate(deepcopy(raw))
    other = WorldState.from_dict(world.to_dict())
    geometry_calls = count_validations(FacilityGeometry)
    spatial_calls = count_validations(SpatialConstraints)
    with geometry_config_scope():
        assert all(effective_geometry(world) == expected for _ in range(50))
        for _ in range(50):
            effective_spatial_constraints(world)
        # A restored or forked world owns new config objects and is parsed for itself.
        assert effective_geometry(other) == expected
    assert geometry_calls == [id(raw), id(other.model_configs["facility_geometry"])]
    assert len(spatial_calls) == 1


def test_config_edits_outside_a_scope_are_always_seen(generated_scenario):
    world = WorldState.from_scenario(generated_scenario)
    with geometry_config_scope():
        assert effective_geometry(world).routes
    world.model_configs["facility_geometry"]["routes"] = []
    assert effective_geometry(world).routes == ()
    with geometry_config_scope():
        assert effective_geometry(world).routes == ()


def test_runtime_overrides_still_apply_inside_a_scope(generated_scenario):
    world = WorldState.from_scenario(generated_scenario)
    with geometry_config_scope():
        base = effective_geometry(world)
        facility_id, point = next(iter(base.facility_points.items()))
        moved = {"x": point.x + 1.0, "y": point.y}
        world.model_state.setdefault(GEOMETRY_OVERLAY_KEY, {})["facility_points"] = {
            facility_id: {"point_m": moved}
        }
        shifted = effective_geometry(world).facility_points[facility_id]
        assert (shifted.x, shifted.y) == (moved["x"], moved["y"])
        del world.model_state[GEOMETRY_OVERLAY_KEY]["facility_points"]
        assert effective_geometry(world) == base
