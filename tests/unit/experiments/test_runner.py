"""The offline controller replans residents' trips when a typed geometry change applies.

Each test drives the controller's phases by hand on the shared generated Scenario.
The TickEngine counterparts: a queue walk replanned around a partition and then
served is in tests/integration/test_effect_program_geometry.py; people walking
around a partition, and sleepers using a temporary room's beds, are in
tests/integration/test_intervention_outcomes.py
(test_people_walk_around_a_partition_while_it_stands,
test_people_who_go_to_sleep_use_the_beds_of_a_temporary_room).
"""

from __future__ import annotations

from typing import Any

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import reserve_sleep_bed
from sheltersim.domain.models import ZoneEdgeState
from sheltersim.domain.simulator_state import simulator_signatures
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
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.inspection.frames import person_projection, world_frame
from sheltersim.policies.behavior import ActionCandidate, ActionKind, ActionProposal

SOURCE_ID = "ASM-H18-EFFECT-TEST-V1"
# Slow enough that a resident is still on the way when the floor plan changes.
SLOW_WALK_M_PER_MINUTE = 0.2


class ArtifactSink:
    trace_level = TraceLevel.OFF

    def record_decision(self, *_args: object, **_kwargs: object) -> None:
        pass


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


def _walk_slowly(world: WorldState) -> None:
    speeds = world.model_configs["movement_model"]["speed_m_per_minute_by_mobility"]
    for mobility in speeds:
        speeds[mobility] = SLOW_WALK_M_PER_MINUTE


def _zone_polygon(world: WorldState, zone_id: str) -> list[dict[str, float]]:
    geometry = world.model_configs["facility_geometry"]
    return next(zone["polygon"] for zone in geometry["zones"] if zone["zone_id"] == zone_id)


def _temporary_room(world: WorldState, room_id: str, source_ids: list[str]) -> dict[str, object]:
    """A 2 m by 4 m room with one bed in the sleeping area's corner away from its door."""
    sleeping = _zone_polygon(world, "sleeping")
    left = min(point["x"] for point in sleeping)
    bottom = max(point["y"] for point in sleeping)
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


def _square_between(
    start: dict[str, float], end: dict[str, float], half_side: float = 0.25
) -> list[dict[str, float]]:
    """A small square on the straight line halfway from start to end."""
    x = (start["x"] + end["x"]) / 2
    y = (start["y"] + end["y"]) / 2
    return [
        {"x": x - half_side, "y": y - half_side},
        {"x": x + half_side, "y": y - half_side},
        {"x": x + half_side, "y": y + half_side},
        {"x": x - half_side, "y": y + half_side},
    ]


def _sleep_proposal(minute: int) -> ActionProposal:
    return ActionProposal(
        agent_id="A001",
        minute_index=minute,
        observed_state_hash="a" * 64,
        action=ActionKind.SLEEP,
        candidates=(
            ActionCandidate(
                action=ActionKind.SLEEP,
                eligible=True,
                trigger="test sleep selection",
                utility=1.0,
                urgency_priority=0,
                probability=1.0,
            ),
        ),
        trigger="test sleep selection",
        choice_value=0.0,
        rng_key=("test", "sleep", "A001", minute),
        selection_probability=1.0,
        policy_version="test-sleep-v1",
        next_decision_at=minute + 1,
    )


def _with_alternate_route(world: WorldState, edge_id: str, travel_minutes: int) -> None:
    """Add a second sleeping-to-toilet edge; generated plans only have one path.

    It walks the same doors and corridor as the usual two legs, so it is physically
    clear, but it is a separate graph edge that closing one leg does not block.
    """
    geometry = world.model_configs["facility_geometry"]
    legs: dict[str, Any] = {route["edge_id"]: route["points"] for route in geometry["routes"]}
    world.zone_edges[edge_id] = ZoneEdgeState(
        id=edge_id,
        from_zone_id="sleeping",
        to_zone_id="toilet",
        travel_minutes=travel_minutes,
    )
    geometry["routes"].append(
        {
            "edge_id": edge_id,
            "points": [*legs["route_sleeping_corridor"], *legs["route_corridor_toilet"][1:]],
            "width_m": 1.8,
            "accessible": True,
        }
    )
    for opening in world.model_configs["spatial_constraints"]["openings"]:
        if {"route_sleeping_corridor", "route_corridor_toilet"} & set(opening["edge_ids"]):
            opening["edge_ids"].append(edge_id)


def test_equipment_transfer_replans_resident_trip_to_moved_facility(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, ArtifactSink())
    route = controller._route("sleeping", "toilet", "A001")
    assert route is not None
    assert controller._schedule_travel(
        agent_id="A001",
        route=route,
        purpose="toilet_request",
        facility_id="toilet_01",
        minute=0,
        validate_state=False,
    )
    # Generated people carry minute-by-minute psychology, so minute 0 is updated
    # before minute 1, as a tick would.
    controller(TickPhase.UPDATE_AGENT_STATE, world, 0, CounterRNG(world.seed))
    original = world.model_state["movement_v1"]["journeys"]["A001"]["event_id"]
    # The corridor's centre line level with the toilet, clear of both corridor walls.
    corridor_ys = [point["y"] for point in _zone_polygon(world, "corridor")]
    destination = {
        "x": world.model_configs["facility_geometry"]["facility_points"]["toilet_01"]["x"],
        "y": (min(corridor_ys) + max(corridor_ys)) / 2,
    }
    action = _effect(
        "move-toilet-during-resident-trip",
        "transfer_equipment",
        "equipment",
        "toilet_01",
        {
            "schema_version": 1,
            "equipment_id": "toilet_01",
            "from_zone_id": "toilet",
            "to_zone_id": "corridor",
            "point_m": destination,
            "source_ids": ["ASM-EQUIPMENT-TRANSFER-TEST-V1"],
        },
        "metre_2d",
    )
    program = _program(action)
    assert validate_effect_program(world, program) == ()
    schedule_effect_program(world, program)

    world.minute_index = 1
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))
    controller(TickPhase.UPDATE_AGENT_STATE, world, 1, CounterRNG(world.seed))

    journey = world.model_state["movement_v1"]["journeys"]["A001"]
    assert journey["facility_id"] == "toilet_01"
    assert journey["destination_zone_id"] == "corridor"
    assert journey["route_zone_ids"] == ["sleeping", "corridor"]
    assert journey["event_id"] != original
    assert any(
        row.event.event_id == original and row.status is EventStatus.SUPPRESSED
        for row in world.event_log.records
    )
    assert world.metric_state["movement_replan_count"] == 1
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()
    restored_controller = OfflineSimulationController(restored, ArtifactSink())

    # The replanned trip ends at the corridor's door, and a walk across the corridor
    # to the moved toilet follows. Drive both worlds minute by minute until A001
    # arrives there; no actions are proposed, so nobody else sets out.
    for minute in range(2, 30):
        for each, each_controller in ((world, controller), (restored, restored_controller)):
            each.minute_index = minute
            for phase in (TickPhase.DISPATCH_DUE_EVENTS, TickPhase.UPDATE_AGENT_STATE):
                each_controller(phase, each, minute, CounterRNG(each.seed))
        if "A001" not in world.model_state["movement_v1"]["journeys"]:
            break
    assert world.agents["A001"].zone_id == "corridor"
    assert world.facilities["toilet_01"].zone_id == "corridor"
    # The generated toilet has one service point and no queue of its own, so the
    # facility's queue is the FIFO A001 joins.
    assert world.agents["A001"].activity.value == "queued"
    assert world.facilities["toilet_01"].queue_agent_ids == ["A001"]
    world.validate()
    assert restored.canonical_json() == world.canonical_json()


def test_closing_route_replans_resident_trip_and_suppresses_old_completion(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.zone_edges["route_sleeping_corridor"].travel_minutes = 6
    world.zone_edges["route_corridor_toilet"].travel_minutes = 1
    _with_alternate_route(world, "route_sleeping_toilet_alt", travel_minutes=20)
    close_route = _effect(
        "close-sleeping-corridor",
        "set_route_access",
        "route",
        "route_sleeping_corridor",
        {"accessible": False},
        "boolean",
        start=2,
        end=20,
    )
    schedule_effect_program(world, _program(close_route))

    world.minute_index = 1
    controller = OfflineSimulationController(world, ArtifactSink())
    route = controller._route("sleeping", "toilet", "A001")
    assert route is not None
    assert route.edge_ids == ("route_sleeping_corridor", "route_corridor_toilet")
    assert controller._schedule_travel(
        agent_id="A001",
        route=route,
        purpose="toilet_request",
        facility_id="toilet_01",
        minute=1,
    )
    initial = dict(world.model_state["movement_v1"]["journeys"]["A001"])
    controller._update_movement(1)
    world.minute_index = 2

    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 2, object())
    assert world.zone_edges["route_sleeping_corridor"].closed is True
    live_position = dict(world.model_state["movement_v1"]["positions"]["A001"])
    controller._update_movement(2)

    replacement = world.model_state["movement_v1"]["journeys"]["A001"]
    assert replacement["route_edge_ids"] == ["route_sleeping_toilet_alt"]
    assert replacement["event_id"] != initial["event_id"]
    assert replacement["started_minute"] == 2
    assert (
        world.model_state["movement_v1"]["positions"]["A001"]["zone_id"] == live_position["zone_id"]
    )
    old_completion = next(
        record for record in world.event_log.records if record.event.event_id == initial["event_id"]
    )
    assert old_completion.status is EventStatus.SUPPRESSED
    assert world.agents["A001"].activity.value == "moving"
    assert world.metric_state["movement_replan_count"] == 1.0
    world.validate()


def test_sleep_action_routes_to_exclusive_temporary_bed_and_restores(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    parameters = _temporary_room(world, "quiet-room-sleep", ["ASM-TEMP-ROOM-SLEEP-TEST-V1"])
    bed = parameters["bed_points_m"][0]
    action = _effect(
        "temporary-sleep-room",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        parameters,
        "metre_2d",
        end=8,
    )
    assert validate_effect_program(world, _program(action)) == ()
    schedule_effect_program(world, _program(action))
    dispatcher = _dispatcher()
    world.minute_index = 1
    assert dispatcher.dispatch_due(world, 1)[0].status is EventStatus.APPLIED

    assert "operator:temporary_room_beds" in simulator_signatures(world.model_configs)
    legacy_config_state = world.to_dict()
    legacy_config_state["model_configs"]["geometry_overlay"]["schema_version"] = 1
    legacy_config_world = WorldState.from_dict(legacy_config_state)
    assert "operator:temporary_room_beds" not in simulator_signatures(
        legacy_config_world.model_configs
    )
    assert reserve_sleep_bed(legacy_config_world, "A001") is None
    legacy_state = world.to_dict()
    legacy_state["model_state"]["geometry_overlay_v1"].pop("bed_reservations")
    legacy_world = WorldState.from_dict(legacy_state)
    legacy_restored = Checkpoint.capture(legacy_world).restore_world()
    assert reserve_sleep_bed(legacy_restored, "A001") is not None

    controller = OfflineSimulationController(world, ArtifactSink())
    controller._apply_proposal(_sleep_proposal(world.minute_index))
    agent = world.agents["A001"]
    assert agent.activity.value == "moving"
    assert world.model_state["movement_v1"]["journeys"]["A001"]["purpose"] == "sleep_bed"
    reservation = world.model_state["geometry_overlay_v1"]["bed_reservations"]["A001"]
    assert reservation["room_id"] == "quiet-room-sleep"
    assert reservation["bed_index"] == 0
    assert reserve_sleep_bed(world, "A002") is None
    assert any(
        row.event.event_type == "agent_action_lifecycle"
        and row.event.payload.get("target_id") == reservation["target_id"]
        for row in world.event_log.records
    )
    checkpoint = Checkpoint.capture(world)
    restored = checkpoint.restore_world()
    due_minute = world.active_tasks[
        world.model_state["movement_v1"]["journeys"]["A001"]["task_id"]
    ].due_minute
    world.minute_index = restored.minute_index = due_minute
    dispatcher_outcomes = controller.event_dispatcher.dispatch_due(world, due_minute)
    restored_controller = OfflineSimulationController(restored, ArtifactSink())
    restored_outcomes = restored_controller.event_dispatcher.dispatch_due(restored, due_minute)
    assert dispatcher_outcomes[0].status is restored_outcomes[0].status is EventStatus.APPLIED
    controller._sync_movement_after_events()
    restored_controller._sync_movement_after_events()
    assert world.model_state["movement_v1"]["positions"]["A001"]["point"] == bed
    assert world.canonical_json() == restored.canonical_json()

    controller._apply_proposal(_sleep_proposal(due_minute))
    restored_controller._apply_proposal(_sleep_proposal(due_minute))
    assert world.agents["A001"].activity.value == "sleeping"
    frame = world_frame(world, run_id="run-bed", branch_id="main", computed_until=due_minute)
    room = frame["geometry_overlay"]["temporary_rooms"]["quiet-room-sleep"]
    assert room["reserved_beds"] == [{"bed_index": 0, "agent_id": "A001"}]
    assert (
        person_projection(world, "A001")["person"]["sleeping_place"]["room_id"]
        == "quiet-room-sleep"
    )
    sleeping_checkpoint = Checkpoint.capture(world).restore_world()
    assert sleeping_checkpoint.canonical_json() == world.canonical_json()

    world.minute_index = restored.minute_index = due_minute = 8
    expired = controller.event_dispatcher.dispatch_due(world, due_minute)
    restored_controller.event_dispatcher.dispatch_due(restored, due_minute)
    controller._sync_movement_after_events()
    restored_controller._sync_movement_after_events()
    assert expired[0].status is EventStatus.APPLIED
    assert (
        world.model_state["geometry_overlay_v1"]["temporary_rooms"]["quiet-room-sleep"]["active"]
        is False
    )
    assert "A001" in world.model_state["geometry_overlay_v1"]["bed_reservations"]
    assert world.canonical_json() == restored.canonical_json()


def test_sleep_bed_route_replans_from_live_position_after_obstacle_change(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    _walk_slowly(world)
    parameters = _temporary_room(world, "quiet-room-replan", ["ASM-TEMP-ROOM-REPLAN-TEST-V1"])
    bed = parameters["bed_points_m"][0]
    action = _effect(
        "temporary-sleep-room-replan",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        parameters,
        "metre_2d",
        end=40,
    )
    schedule_effect_program(world, _program(action))
    world.minute_index = 1
    assert _dispatcher().dispatch_due(world, 1)[0].status is EventStatus.APPLIED

    controller = OfflineSimulationController(world, ArtifactSink())
    controller._apply_proposal(_sleep_proposal(1))
    initial_journey = dict(world.model_state["movement_v1"]["journeys"]["A001"])
    assert len(initial_journey["route_points"]) == 2
    controller._update_movement(1)
    live_position = dict(world.model_state["movement_v1"]["positions"]["A001"]["point"])
    world.minute_index = 2

    # A new obstruction halfway along the straight walk to the bed.
    world.model_configs["spatial_constraints"]["obstacles"].append(
        {
            "id": "new-room-obstruction",
            "polygon": _square_between(live_position, bed),
            "source_ids": ["ASM-ROOM-OBSTRUCTION-TEST-V1"],
        }
    )
    controller._update_movement(2)

    replanned = world.model_state["movement_v1"]["journeys"]["A001"]
    assert replanned["purpose"] == "sleep_bed"
    assert replanned["route_points"] != initial_journey["route_points"]
    assert replanned["route_points"][0] == live_position
    assert replanned["event_id"] != initial_journey["event_id"]
    assert (
        next(
            record
            for record in world.event_log.records
            if record.event.event_id == initial_journey["event_id"]
        ).status
        is EventStatus.SUPPRESSED
    )
    replacement_event = next(
        event for event in world.event_queue.pending() if event.event_id == replanned["event_id"]
    )
    assert replacement_event.causation_id == initial_journey["event_id"]
    assert world.metric_state["movement_replan_count"] == 1.0
    world.validate()

    restored = Checkpoint.capture(world).restore_world()
    restored_controller = OfflineSimulationController(restored, ArtifactSink())
    due_minute = world.active_tasks[replanned["task_id"]].due_minute
    world.minute_index = restored.minute_index = due_minute
    world_controller_outcomes = controller.event_dispatcher.dispatch_due(world, due_minute)
    restored_outcomes = restored_controller.event_dispatcher.dispatch_due(restored, due_minute)
    assert world_controller_outcomes[0].status is restored_outcomes[0].status is EventStatus.APPLIED
    controller._sync_movement_after_events()
    restored_controller._sync_movement_after_events()
    assert world.model_state["movement_v1"]["positions"]["A001"]["point"] == bed
    assert world.canonical_json() == restored.canonical_json()
