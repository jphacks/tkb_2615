"""Approving an EffectProgram counts what earlier programs have already scheduled."""

from __future__ import annotations

from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.events.effects import (
    EffectProgram,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
    validate_effect_program,
)
from sheltersim.events.models import EventKind, ScheduledEvent

ROOM = {
    "schema_version": 1,
    "room_id": "room_a",
    "zone_id": "sleeping",
    "polygon_m": [
        {"x": 0.5, "y": 8.0},
        {"x": 2.5, "y": 8.0},
        {"x": 2.5, "y": 12.0},
        {"x": 0.5, "y": 12.0},
    ],
    "people_capacity": 2,
    "sleeping_capacity": 2,
    "bed_points_m": [{"x": 1.0, "y": 9.0}, {"x": 2.0, "y": 9.0}],
    "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
}


def _program(program_id: str, op: str, parameters: dict[str, Any], units: str) -> EffectProgram:
    return EffectProgram(
        program_id=program_id,
        narrative=op,
        source_text=op,
        simulation_start_minute=120,
        effects=[
            TimedAction(
                effect_id=f"{op}-1",
                op=op,
                target_selector=TargetSelector(kind="zone", target_id="sleeping"),
                start_minute=120,
                end_minute=300,
                parameters=parameters,
                units=units,
                source_id="TEST-EFFECTS",
                policy_dependencies=[
                    "geometry_overlay_v1"
                    if op == "temporary_room_geometry"
                    else "temporary_spaces_v1"
                ],
                reversible=True,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def test_a_temporary_room_cannot_reuse_the_id_of_one_still_scheduled(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    first = _program("first-room", "temporary_room_geometry", ROOM, "metre_2d")
    assert validate_effect_program(world, first) == ()
    schedule_effect_program(world, first)

    second = _program("second-room", "temporary_room_geometry", ROOM, "metre_2d")

    assert any(
        "temporary room ID 'room_a' already exists or is pending" in issue
        for issue in validate_effect_program(world, second)
    )


def test_scheduled_temporary_space_counts_toward_the_zone_capacity_bound(
    generated_scenario,
) -> None:
    # A temporary space already scheduled for the sleeping area fills its bound.
    world = WorldState.from_scenario(generated_scenario)
    world.schedule_event(
        ScheduledEvent(
            event_id="scheduled-space",
            kind=EventKind.INTERVENTION,
            due_minute=60,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="effect_program_temporary_space",
            payload={
                "zone_id": "sleeping",
                "parameters": {"people_capacity": 1000, "sleeping_capacity": 0},
            },
        )
    )
    one_more = _program(
        "one-more",
        "add_temporary_space",
        {"people_capacity": 1, "sleeping_capacity": 0},
        "person_capacity",
    )

    assert "temporary person capacity exceeds the 1000-person per-zone bound" in (
        validate_effect_program(world, one_more)
    )
