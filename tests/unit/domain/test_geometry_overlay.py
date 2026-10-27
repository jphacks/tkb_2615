"""Runtime floor-plan geometry: per-phase parsing, checks before approval, and typed
geometry EffectPrograms applied, expired and restored from a Checkpoint.

That tick phases reuse the parsed geometry without changing results is checked
in tests/integration/test_geometry_config_cache.py.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.contracts.spatial import SpatialConstraints
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import (
    GEOMETRY_OVERLAY_KEY,
    effective_geometry,
    effective_spatial_constraints,
    geometry_config_scope,
    validate_geometry_change,
)
from sheltersim.events.effects import (
    EffectProgram,
    EffectProgramGeometryHandler,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
    validate_effect_program,
)
from sheltersim.events.handlers import EventDispatcher
from sheltersim.events.models import EventStatus
from sheltersim.inspection.frames import world_frame


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


# Typed geometry EffectPrograms: preflight, apply, expiry and Checkpoint restore.
# Moved from tests/test_effect_program.py, where they ran on a hand-made floor plan
# without facility awareness (#88).

SOURCE_ID = "ASM-H18-EFFECT-TEST-V1"


def _effect(
    effect_id: str,
    op: str,
    target_kind: str,
    target_id: str,
    parameters: dict[str, object],
    units: str,
    *,
    start: int = 1,
    end: int | None = None,
) -> TimedAction:
    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind=target_kind, target_id=target_id),
        start_minute=start,
        end_minute=end,
        parameters=parameters,
        units=units,
        source_id=SOURCE_ID,
        assumption_ids=[],
        policy_dependencies=["geometry_overlay_v1"],
        reversible=True,
    )


def _program(*effects: TimedAction, program_id: str = "program-h18-test") -> EffectProgram:
    return EffectProgram(
        program_id=program_id,
        narrative="Bounded test program",
        source_text="test source text",
        simulation_start_minute=min(effect.start_minute for effect in effects),
        effects=list(effects),
        assumptions=[],
        support_mode=SupportMode.COMPOSED if len(effects) > 1 else SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def _dispatcher() -> EventDispatcher:
    return EventDispatcher(
        {
            "effect_program_geometry": EffectProgramGeometryHandler(),
            "effect_program_geometry_expire": EffectProgramGeometryHandler(expire=True),
        }
    )


def _corridor_point(world: WorldState, facility_id: str) -> dict[str, float]:
    """The corridor's centre line, level with the facility, clear of both corridor walls."""
    geometry = world.model_configs["facility_geometry"]
    corridor = next(zone for zone in geometry["zones"] if zone["zone_id"] == "corridor")
    ys = [point["y"] for point in corridor["polygon"]]
    return {
        "x": geometry["facility_points"][facility_id]["x"],
        "y": (min(ys) + max(ys)) / 2,
    }


def _transfer_to_corridor(
    world: WorldState, facility_id: str, from_zone_id: str, source_ids: list[str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "equipment_id": facility_id,
        "from_zone_id": from_zone_id,
        "to_zone_id": "corridor",
        "point_m": _corridor_point(world, facility_id),
        "source_ids": source_ids,
    }


def _temporary_room(world: WorldState, room_id: str, source_ids: list[str]) -> dict[str, object]:
    """A 2 m by 4 m room with one bed in the sleeping area's corner away from its door.

    Its only ways in and out are the sleeping area's two corridor legs.
    """
    geometry = world.model_configs["facility_geometry"]
    sleeping = next(zone for zone in geometry["zones"] if zone["zone_id"] == "sleeping")
    left = min(point["x"] for point in sleeping["polygon"])
    bottom = max(point["y"] for point in sleeping["polygon"])
    return {
        "schema_version": 1,
        "room_id": room_id,
        "zone_id": "sleeping",
        "polygon_m": [
            {"x": left + 0.5, "y": bottom - 4.4},
            {"x": left + 2.5, "y": bottom - 4.4},
            {"x": left + 2.5, "y": bottom - 0.4},
            {"x": left + 0.5, "y": bottom - 0.4},
        ],
        "people_capacity": 2,
        "sleeping_capacity": 1,
        "bed_points_m": [{"x": left + 1.5, "y": bottom - 2.4}],
        "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
        "source_ids": source_ids,
    }


def test_typed_equipment_transfer_changes_zone_geometry_and_checkpoint_state(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    before = effective_geometry(world)
    old_point = before.facility_points["toilet_01"]
    (old_service,) = [row for row in before.service_points if row.facility_id == "toilet_01"]
    parameters = _transfer_to_corridor(
        world, "toilet_01", "toilet", ["ASM-EQUIPMENT-TRANSFER-TEST-V1"]
    )
    action = _effect(
        "move-toilet-equipment",
        "transfer_equipment",
        "equipment",
        "toilet_01",
        parameters,
        "metre_2d",
    )
    program = _program(action)
    assert validate_effect_program(world, program) == ()
    schedule_effect_program(world, program)

    world.minute_index = 1
    receipt = _dispatcher().dispatch_due(world, 1)[0]

    assert receipt.status is EventStatus.APPLIED
    assert world.facilities["toilet_01"].zone_id == "corridor"
    after = effective_geometry(world)
    new_point = after.facility_points["toilet_01"]
    assert new_point.model_dump() == parameters["point_m"]
    # The service point keeps its place relative to the facility.
    (new_service,) = [row for row in after.service_points if row.facility_id == "toilet_01"]
    assert new_service.point_m.x - old_service.point_m.x == pytest.approx(new_point.x - old_point.x)
    assert new_service.point_m.y - old_service.point_m.y == pytest.approx(new_point.y - old_point.y)
    restored = Checkpoint.capture(world).restore_world()
    assert restored.facilities["toilet_01"].zone_id == "corridor"
    assert restored.model_state["geometry_overlay_v1"]["history"][-1]["op"] == "transfer_equipment"
    assert (
        "ASM-EQUIPMENT-TRANSFER-TEST-V1"
        in restored.model_state["geometry_overlay_v1"]["history"][-1]["source_ids"]
    )


def test_equipment_transfer_requires_idle_facility_and_matching_source_zone(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.facilities["toilet_01"].queue_agent_ids.append("A001")

    with pytest.raises(ValueError, match="facility_in_use"):
        validate_geometry_change(
            world,
            "transfer_equipment",
            "toilet_01",
            _transfer_to_corridor(world, "toilet_01", "toilet", []),
            None,
        )

    world.facilities["toilet_01"].queue_agent_ids.clear()
    mismatched = _effect(
        "mismatched-source",
        "transfer_equipment",
        "equipment",
        "toilet_01",
        _transfer_to_corridor(world, "toilet_01", "sleeping", []),
        "metre_2d",
    )
    assert any(
        "precondition_failed" in issue
        for issue in validate_effect_program(world, _program(mismatched))
    )


def test_typed_route_closure_and_expiry_survive_checkpoint_restore(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    action = _effect(
        "close-toilet-corridor",
        "set_route_access",
        "route",
        "route_toilet_corridor",
        {"accessible": False},
        "boolean",
        end=5,
    )
    schedule_effect_program(world, _program(action))
    dispatcher = _dispatcher()

    world.minute_index = 1
    assert dispatcher.dispatch_due(world, 1)[0].status is EventStatus.APPLIED
    assert world.zone_edges["route_toilet_corridor"].closed is True
    restored = Checkpoint.capture(world).restore_world()

    world.minute_index = restored.minute_index = 5
    assert dispatcher.dispatch_due(world, 5)[0].status is EventStatus.APPLIED
    assert dispatcher.dispatch_due(restored, 5)[0].status is EventStatus.APPLIED
    assert world.zone_edges["route_toilet_corridor"].closed is False
    assert restored.zone_edges["route_toilet_corridor"].closed is False
    assert world.canonical_json() == restored.canonical_json()


def test_physical_temporary_room_geometry_beds_access_and_expiry_restore(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    parameters = _temporary_room(world, "quiet-room-1", ["ASM-TEMP-ROOM-TEST-V1"])
    action = _effect(
        "temporary-sleep-room",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        parameters,
        "metre_2d",
        end=5,
    )
    program = _program(action)
    assert validate_effect_program(world, program) == ()
    schedule_effect_program(world, program)
    dispatcher = _dispatcher()

    world.minute_index = 1
    assert dispatcher.dispatch_due(world, 1)[0].status is EventStatus.APPLIED
    room = world.model_state["geometry_overlay_v1"]["temporary_rooms"]["quiet-room-1"]
    assert room["active"] is True
    assert room["sleeping_capacity"] == len(room["bed_points_m"]) == 1
    assert "ASM-TEMP-ROOM-TEST-V1" in room["source_ids"]
    # The corridor leg into the sleeping area is the room's only way in.
    close_only_room_access = _effect(
        "close-room-access",
        "set_route_access",
        "route",
        "route_corridor_sleeping",
        {"accessible": False},
        "boolean",
        start=2,
        end=4,
    )
    assert any(
        "route_in_use" in issue
        for issue in validate_effect_program(world, _program(close_only_room_access))
    )
    world.model_state["movement_v1"]["positions"]["A001"]["point"] = dict(
        parameters["bed_points_m"][0]
    )

    frame = world_frame(world, run_id="run-room", branch_id="branch-room", computed_until=1)
    projected_room = frame["geometry_overlay"]["temporary_rooms"]["quiet-room-1"]
    assert projected_room["observed_occupant_agent_ids"] == ["A001"]
    assert projected_room["observed_occupied_beds"] == [{"bed_index": 0, "agent_id": "A001"}]
    restored = Checkpoint.capture(world).restore_world()

    world.minute_index = restored.minute_index = 5
    assert dispatcher.dispatch_due(world, 5)[0].status is EventStatus.APPLIED
    assert dispatcher.dispatch_due(restored, 5)[0].status is EventStatus.APPLIED
    assert (
        world.model_state["geometry_overlay_v1"]["temporary_rooms"]["quiet-room-1"]["active"]
        is False
    )
    assert world.canonical_json() == restored.canonical_json()


def test_physical_temporary_room_declines_occupied_polygon(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    parameters = _temporary_room(world, "quiet-room-occupied", [])
    world.model_state["movement_v1"]["positions"]["A001"]["point"] = dict(
        parameters["bed_points_m"][0]
    )
    action = _effect(
        "occupied-temporary-room",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        parameters,
        "metre_2d",
        end=5,
    )

    assert any("room_in_use" in issue for issue in validate_effect_program(world, _program(action)))


def test_geometry_overlay_checkpoint_without_temporary_room_key_still_restores(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    del world.model_state["geometry_overlay_v1"]["temporary_rooms"]

    restored = Checkpoint.capture(world).restore_world()

    assert "temporary_rooms" not in restored.model_state["geometry_overlay_v1"]
    restored.validate()
