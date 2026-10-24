"""EffectProgram preflight, deterministic execution, and checkpoint contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.models import ZoneEdgeState
from sheltersim.domain.operational import water_service_blocked
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    EffectProgramEnvironmentHandler,
    EffectProgramFacilityHandler,
    EffectProgramGeometryHandler,
    EffectProgramStructureHandler,
    EffectProgramValidationError,
    GuardExpression,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
    validate_effect_program,
)
from sheltersim.events.handlers import (
    DeliveryDelayHandler,
    EventDispatcher,
    ResourceLedgerHandler,
    StaffRestorationHandler,
    StaffUnavailabilityHandler,
    WaterOutageHandler,
    WaterRestorationHandler,
)
from sheltersim.events.models import EventStatus
from sheltersim.experiments.branch import (
    BranchError,
    BranchRuntime,
    SetFacilityWorkingUnitsCommand,
)
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import person_projection, world_frame
from sheltersim.policies.behavior import ActionCandidate, ActionKind, ActionProposal

ROOT = Path(__file__).parents[1]
SMALL_SCENARIO = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
SOURCE_ID = "ASM-H18-EFFECT-TEST-V1"


def _world(*, operational: bool = False, resource_service: bool = False) -> WorldState:
    document = json.loads(SMALL_SCENARIO.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 45
    document["scheduled_events"] = []
    if operational:
        document["schema_version"] = 3
        document["scheduled_events"] = [
            {
                "id": "shipment-main-event",
                "kind": "exogenous",
                "due_minute": 20,
                "priority": 0,
                "sequence": 0,
                "event_type": "resource_delivery",
                "payload": {
                    "warehouse_id": "warehouse-h18",
                    "resource_id": "water_ml",
                    "quantity": 100,
                    "shipment_id": "shipment-main",
                },
                "source_id": "ASM-H18-TEST-SHIPMENT-V1",
            }
        ]
        document["initial_stock"] = {
            "schema_version": 1,
            "warehouse_id": "warehouse-h18",
            "zone_id": "toilet",
            "water_ml": 1000,
            "food_meal_units": 10,
            "source_ids": ["ASM-H18-TEST-STOCK-V1"],
        }
    if resource_service:
        if not operational:
            raise ValueError("resource-service test worlds require operational state")
        document["facilities"].extend(
            [
                {
                    "id": "water-station-h18",
                    "kind": "water_station",
                    "zone_id": "toilet",
                    "capacity": 1,
                },
                {
                    "id": "meal-station-h18",
                    "kind": "meal_distribution",
                    "zone_id": "toilet",
                    "capacity": 1,
                },
            ]
        )
        document["staff"].extend(
            [
                {"id": "water-staff-h18", "skills": ["water_service"], "zone_id": "toilet"},
                {"id": "meal-staff-h18", "skills": ["meal_service"], "zone_id": "toilet"},
            ]
        )
        document["model_configs"]["resource_service"] = {
            "schema_version": 1,
            "policy_version": "resource-service-v1",
            "source_id": "ASM-H02-RESOURCE-SERVICE-V1",
            "source_type": "assumption",
            "service_minutes": 2,
            "water_ml_per_service": 250,
            "food_meal_units_per_service": 1,
            "hydration_growth_per_minute": 0.02,
        }
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def _world_with_geometry() -> WorldState:
    document = json.loads(SMALL_SCENARIO.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 45
    document["scheduled_events"] = []
    zone_ids = [row["id"] for row in document["zones"]]
    polygon = [
        {"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 0.0},
        {"x": 10.0, "y": 10.0}, {"x": 0.0, "y": 10.0},
    ]
    document["model_configs"]["facility_geometry"] = {
        "schema_version": 1,
        "units": "m",
        "origin": "top_left",
        "width_m": 10.0,
        "height_m": 10.0,
        "source_kind": "synthetic",
        "zones": [
            {"zone_id": zone_id, "polygon": polygon, "usable_area_m2": 100.0}
            for zone_id in zone_ids
        ],
        "facility_points": {
            facility["id"]: {"x": 7.0, "y": 5.0}
            for facility in document["facilities"]
        },
        "service_points": [
            {
                "schema_version": 1,
                "id": "toilet-1-service-point",
                "facility_id": "toilet_1",
                "point_m": {"x": 8.0, "y": 5.0},
                "service_capacity": 1,
                "queue_id": "toilet-1-fifo",
            }
        ],
        "routes": [
            {
                "edge_id": edge["id"],
                "points": [{"x": 3.0, "y": 5.0}, {"x": 6.0, "y": 5.0}],
                "width_m": 2.0,
                "accessible": True,
            }
            for edge in document["zone_edges"]
        ],
    }
    document["model_configs"]["geometry_overlay"] = {"schema_version": 2}
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def _effect(
    effect_id: str,
    op: str,
    target_kind: str,
    target_id: str,
    parameters: dict[str, object],
    units: str,
    policies: list[str],
    *,
    start: int = 1,
    end: int | None = None,
    guard: GuardExpression | None = None,
    reversible: bool = True,
) -> TimedAction:
    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind=target_kind, target_id=target_id),
        start_minute=start,
        end_minute=end,
        guard=guard,
        parameters=parameters,
        units=units,
        source_id=SOURCE_ID,
        assumption_ids=[],
        policy_dependencies=policies,
        reversible=reversible,
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
        compiler_provenance={
            "compiler_kind": "offline_rule",
            "compiler_version": "test-v1",
        },
    )


def _dispatcher() -> EventDispatcher:
    return EventDispatcher(
        {
            "effect_program_apply": EffectProgramFacilityHandler(),
            "effect_program_expire": EffectProgramFacilityHandler(expire=True),
            "effect_program_structure": EffectProgramStructureHandler(),
            "effect_program_environment": EffectProgramEnvironmentHandler(),
            "effect_program_environment_expire": EffectProgramEnvironmentHandler(expire=True),
            "effect_program_geometry": EffectProgramGeometryHandler(),
            "effect_program_geometry_expire": EffectProgramGeometryHandler(expire=True),
            "resource_delivery": ResourceLedgerHandler("delivery"),
            "delivery_delayed": DeliveryDelayHandler(),
            "staff_unavailable": StaffUnavailabilityHandler(),
            "staff_restored": StaffRestorationHandler(),
            "water_outage": WaterOutageHandler(),
            "water_restored": WaterRestorationHandler(),
        }
    )


def test_registry_exposes_only_code_owned_primitives_and_unknown_ops_fail_preflight() -> None:
    world = _world()
    assert {item.op for item in DEFAULT_PRIMITIVE_REGISTRY.operators} == {
        "add_evacuees",
        "add_staff",
        "add_temporary_space",
        "add_toilet",
        "deliver_resource",
        "depart_evacuees",
        "facility_outage",
        "facility_repair",
        "modify_demand_rate",
        "modify_supply_schedule",
        "move_facility",
        "population_presence",
        "set_ambient_noise",
        "set_facility_availability",
        "set_lighting",
        "set_partition",
        "set_privacy",
        "set_route_access",
        "set_staff_availability",
        "set_staff_role",
        "staff_task_order",
        "set_temperature",
        "transfer_equipment",
        "temporary_room_geometry",
        "set_ventilation",
        "staff_handoff",
        "water_outage",
    }
    action = _effect(
        "unknown-effect",
        "invented_environment_action",
        "zone",
        "toilet",
        {"rate": 2.0},
        "requests_per_minute",
        [],
    )

    issues = validate_effect_program(world, _program(action))

    assert any("unsupported operator" in issue for issue in issues)
    assert "modify_demand_rate" in {item.op for item in DEFAULT_PRIMITIVE_REGISTRY.operators}


def test_invalid_compound_program_is_rejected_without_mutating_the_branch() -> None:
    world = _world()
    before = world.canonical_json()
    valid = _effect(
        "valid-outage",
        "set_facility_availability",
        "facility",
        "toilet_2",
        {"working_units": 0},
        "facility_unit",
        ["facility_capacity"],
        end=5,
    )
    unknown = _effect(
        "unsupported-demand",
        "invented_environment_action",
        "zone",
        "toilet",
        {"rate": 2.0},
        "requests_per_minute",
        [],
    )

    with pytest.raises(EffectProgramValidationError, match="unsupported operator"):
        schedule_effect_program(world, _program(valid, unknown))

    assert world.canonical_json() == before


def test_typed_equipment_transfer_changes_zone_geometry_and_checkpoint_state() -> None:
    world = _world_with_geometry()
    action = _effect(
        "move-toilet-equipment",
        "transfer_equipment",
        "equipment",
        "toilet_1",
        {
            "schema_version": 1,
            "equipment_id": "toilet_1",
            "from_zone_id": "toilet",
            "to_zone_id": "corridor",
            "point_m": {"x": 5.0, "y": 5.0},
            "source_ids": ["ASM-EQUIPMENT-TRANSFER-TEST-V1"],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
    )
    program = _program(action)
    assert validate_effect_program(world, program) == ()
    schedule_effect_program(world, program)

    world.minute_index = 1
    receipt = _dispatcher().dispatch_due(world, 1)[0]

    assert receipt.status is EventStatus.APPLIED
    assert world.facilities["toilet_1"].zone_id == "corridor"
    from sheltersim.domain.geometry_overlay import effective_geometry

    assert effective_geometry(world).facility_points["toilet_1"].model_dump() == {
        "x": 5.0,
        "y": 5.0,
    }
    assert effective_geometry(world).service_points[0].point_m.model_dump() == {
        "x": 6.0,
        "y": 5.0,
    }
    restored = Checkpoint.capture(world).restore_world()
    assert restored.facilities["toilet_1"].zone_id == "corridor"
    assert restored.model_state["geometry_overlay_v1"]["history"][-1]["op"] == "transfer_equipment"
    assert "ASM-EQUIPMENT-TRANSFER-TEST-V1" in restored.model_state[
        "geometry_overlay_v1"
    ]["history"][-1]["source_ids"]


def test_equipment_transfer_replans_resident_trip_to_moved_facility() -> None:
    world = _world_with_geometry()

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    controller = OfflineSimulationController(world, ArtifactSink())
    route = controller._route("sleeping", "toilet", "A001")
    assert route is not None
    assert controller._schedule_travel(
        agent_id="A001",
        route=route,
        purpose="toilet_request",
        facility_id="toilet_1",
        minute=0,
        validate_state=False,
    )
    original = world.model_state["movement_v1"]["journeys"]["A001"]["event_id"]
    action = _effect(
        "move-toilet-during-resident-trip",
        "transfer_equipment",
        "equipment",
        "toilet_1",
        {
            "schema_version": 1,
            "equipment_id": "toilet_1",
            "from_zone_id": "toilet",
            "to_zone_id": "corridor",
            "point_m": {"x": 5.0, "y": 5.0},
            "source_ids": ["ASM-EQUIPMENT-TRANSFER-TEST-V1"],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
    )
    program = _program(action)
    assert validate_effect_program(world, program) == ()
    schedule_effect_program(world, program)

    world.minute_index = 1
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))
    controller(TickPhase.UPDATE_AGENT_STATE, world, 1, CounterRNG(world.seed))

    journey = world.model_state["movement_v1"]["journeys"]["A001"]
    assert journey["facility_id"] == "toilet_1"
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

    completion = next(
        event for event in world.event_queue.pending()
        if event.event_id == journey["event_id"]
    )
    world.minute_index = completion.due_minute
    restored.minute_index = completion.due_minute
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, world.minute_index, CounterRNG(world.seed))
    restored_controller(
        TickPhase.DISPATCH_DUE_EVENTS,
        restored,
        restored.minute_index,
        CounterRNG(restored.seed),
    )
    assert world.agents["A001"].zone_id == "corridor"
    assert world.facilities["toilet_1"].zone_id == "corridor"
    queues = world.model_state["facility_queues_v1"]
    queue = queues["queues"]["toilet-1-fifo"]
    assert queue["queued_request_ids"]
    assert queues["requests"][queue["queued_request_ids"][0]]["agent_id"] == "A001"
    world.validate()
    assert restored.canonical_json() == world.canonical_json()


def test_equipment_transfer_requires_idle_facility_and_matching_source_zone() -> None:
    world = _world_with_geometry()
    world.facilities["toilet_1"].queue_agent_ids.append("A001")
    from sheltersim.domain.geometry_overlay import validate_geometry_change

    with pytest.raises(ValueError, match="facility_in_use"):
        validate_geometry_change(
            world,
            "transfer_equipment",
            "toilet_1",
            {
                "schema_version": 1,
                "equipment_id": "toilet_1",
                "from_zone_id": "toilet",
                "to_zone_id": "corridor",
                "point_m": {"x": 5.0, "y": 5.0},
            },
            None,
        )

    world.facilities["toilet_1"].queue_agent_ids.clear()
    mismatched = _effect(
        "mismatched-source",
        "transfer_equipment",
        "equipment",
        "toilet_1",
        {
            "schema_version": 1,
            "equipment_id": "toilet_1",
            "from_zone_id": "sleeping",
            "to_zone_id": "corridor",
            "point_m": {"x": 5.0, "y": 5.0},
        },
        "metre_2d",
        ["geometry_overlay_v1"],
    )
    assert any("precondition_failed" in issue for issue in validate_effect_program(world, _program(mismatched)))


def test_typed_route_closure_and_expiry_survive_checkpoint_restore() -> None:
    world = _world_with_geometry()
    action = _effect(
        "close-toilet-corridor",
        "set_route_access",
        "route",
        "toilet_corridor",
        {"accessible": False},
        "boolean",
        ["geometry_overlay_v1"],
        end=5,
    )
    schedule_effect_program(world, _program(action))
    dispatcher = _dispatcher()

    world.minute_index = 1
    assert dispatcher.dispatch_due(world, 1)[0].status is EventStatus.APPLIED
    assert world.zone_edges["toilet_corridor"].closed is True
    restored = Checkpoint.capture(world).restore_world()

    world.minute_index = restored.minute_index = 5
    assert dispatcher.dispatch_due(world, 5)[0].status is EventStatus.APPLIED
    assert dispatcher.dispatch_due(restored, 5)[0].status is EventStatus.APPLIED
    assert world.zone_edges["toilet_corridor"].closed is False
    assert restored.zone_edges["toilet_corridor"].closed is False
    assert world.canonical_json() == restored.canonical_json()


def test_closing_route_replans_resident_trip_and_suppresses_old_completion() -> None:
    world = _world_with_geometry()
    world.zone_edges["sleeping_corridor"].travel_minutes = 6
    world.zone_edges["corridor_toilet"].travel_minutes = 1
    world.zone_edges["sleeping_toilet_alt"] = ZoneEdgeState(
        id="sleeping_toilet_alt",
        from_zone_id="sleeping",
        to_zone_id="toilet",
        travel_minutes=20,
    )
    world.model_configs["facility_geometry"]["routes"].append({
        "edge_id": "sleeping_toilet_alt",
        "points": [{"x": 2.0, "y": 2.0}, {"x": 8.0, "y": 2.0}],
        "width_m": 2.0,
        "accessible": True,
    })
    close_route = _effect(
        "close-sleeping-corridor",
        "set_route_access",
        "route",
        "sleeping_corridor",
        {"accessible": False},
        "boolean",
        ["geometry_overlay_v1"],
        start=2,
        end=20,
    )
    schedule_effect_program(world, _program(close_route))

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    world.minute_index = 1
    controller = OfflineSimulationController(world, ArtifactSink())
    route = controller._route("sleeping", "toilet", "A001")
    assert route is not None
    assert route.edge_ids == ("sleeping_corridor", "corridor_toilet")
    assert controller._schedule_travel(
        agent_id="A001",
        route=route,
        purpose="toilet_request",
        facility_id="toilet_1",
        minute=1,
    )
    initial = dict(world.model_state["movement_v1"]["journeys"]["A001"])
    controller._update_movement(1)
    world.minute_index = 2

    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 2, object())
    assert world.zone_edges["sleeping_corridor"].closed is True
    live_position = dict(world.model_state["movement_v1"]["positions"]["A001"])
    controller._update_movement(2)

    replacement = world.model_state["movement_v1"]["journeys"]["A001"]
    assert replacement["route_edge_ids"] == ["sleeping_toilet_alt"]
    assert replacement["event_id"] != initial["event_id"]
    assert replacement["started_minute"] == 2
    assert world.model_state["movement_v1"]["positions"]["A001"]["zone_id"] == live_position["zone_id"]
    old_completion = next(
        record for record in world.event_log.records
        if record.event.event_id == initial["event_id"]
    )
    assert old_completion.status is EventStatus.SUPPRESSED
    assert world.agents["A001"].activity.value == "moving"
    assert world.metric_state["movement_replan_count"] == 1.0
    world.validate()


def test_queue_trip_replans_after_typed_partition_and_keeps_fifo_reservation() -> None:
    world = _world_with_geometry()
    world.model_configs["movement_model"] = {
        "schema_version": 1,
        "policy_version": "spatial-route-and-room-waypoint-v2",
        "source_id": "ASM-SPATIAL-MOVEMENT-V2",
        "speed_m_per_minute_by_mobility": {
            "independent": 0.2,
            "assisted": 0.2,
            "wheelchair": 0.2,
        },
    }
    # This queue trip is in-room. The partition is away from the service point,
    # so it changes only the queue walk.
    world.model_configs["facility_geometry"]["routes"] = []
    world.agents["A001"].zone_id = "toilet"
    world.agents["A001"].next_decision_at = 100
    world.model_state["movement_v1"]["positions"]["A001"].update({
        "zone_id": "toilet", "point": {"x": 5.0, "y": 5.0},
        "edge_id": None, "progress": 0.0, "position_kind": "physical",
    })
    obstacle = _effect(
        "queue-walk-partition",
        "set_partition",
        "zone",
        "toilet",
        {
            "polygon_m": [
                {"x": 6.15, "y": 4.7}, {"x": 6.55, "y": 4.7},
                {"x": 6.55, "y": 5.3}, {"x": 6.15, "y": 5.3},
            ],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
        start=2,
        end=20,
    )
    schedule_effect_program(world, _program(obstacle))
    service = ToiletServiceManager(world, min_service_minutes=2, max_service_minutes=2)
    request = service.request("A001", "toilet_1", queue_id="toilet-1-fifo")
    service.arbitrate(0, CounterRNG(world.seed))
    initial = dict(world.model_state["movement_v1"]["journeys"]["A001"])
    assert initial["purpose"] == "toilet_queue_position"

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

        def capture_metric(self, *_args: object, **_kwargs: object) -> None:
            pass

    controller = OfflineSimulationController(world, ArtifactSink())
    controller._update_movement(0)
    world.minute_index = 1
    controller._update_movement(1)
    live_position = dict(world.model_state["movement_v1"]["positions"]["A001"]["point"])
    world.minute_index = 2
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 2, object())
    controller._update_movement(2)

    replanned = world.model_state["movement_v1"]["journeys"]["A001"]
    assert replanned["purpose"] == "toilet_queue_position"
    assert replanned["queue_id"] == "toilet-1-fifo"
    assert replanned["route_points"] != initial["route_points"]
    assert replanned["route_points"][0] == live_position
    assert replanned["event_id"] != initial["event_id"]
    assert world.agents["A001"].activity.value == "queued"
    queue_request = world.model_state["facility_queues_v1"]["requests"][request.queue_task_id]
    assert queue_request["queue_id"] == "toilet-1-fifo"
    assert next(
        record for record in world.event_log.records
        if record.event.event_id == initial["event_id"]
    ).status is EventStatus.SUPPRESSED
    assert world.metric_state["movement_replan_count"] == 1.0
    world.validate()

    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()
    restored_controller = OfflineSimulationController(restored, ArtifactSink())
    TickEngine(
        restored,
        phase_handler=restored_controller,
        rng=CounterRNG(restored.seed),
    ).run(30)
    assert any(
        record.status is EventStatus.APPLIED
        and record.event.event_type == "facility_queue_transition"
        and record.event.payload.get("transition") == "completed"
        and record.event.payload.get("queue_id") == "toilet-1-fifo"
        for record in restored.event_log.records
    )
    assert not restored.facilities["toilet_1"].queue_agent_ids
    assert not restored.facilities["toilet_1"].active_agent_ids
    restored.validate()


def test_physical_temporary_room_geometry_beds_access_and_expiry_restore() -> None:
    world = _world_with_geometry()
    action = _effect(
        "temporary-sleep-room",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        {
            "schema_version": 1,
            "room_id": "quiet-room-1",
            "zone_id": "sleeping",
            "polygon_m": [
                {"x": 0.5, "y": 0.5}, {"x": 2.5, "y": 0.5},
                {"x": 2.5, "y": 2.5}, {"x": 0.5, "y": 2.5},
            ],
            "people_capacity": 2,
            "sleeping_capacity": 1,
            "bed_points_m": [{"x": 1.5, "y": 1.5}],
            "route_edge_ids": ["corridor_sleeping", "sleeping_corridor"],
            "source_ids": ["ASM-TEMP-ROOM-TEST-V1"],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
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
    close_only_room_access = _effect(
        "close-room-access",
        "set_route_access",
        "route",
        "corridor_sleeping",
        {"accessible": False},
        "boolean",
        ["geometry_overlay_v1"],
        start=2,
        end=4,
    )
    assert any(
        "route_in_use" in issue
        for issue in validate_effect_program(world, _program(close_only_room_access))
    )
    world.model_state["movement_v1"]["positions"]["A001"]["point"] = {
        "x": 1.5,
        "y": 1.5,
    }
    from sheltersim.inspection.frames import world_frame

    frame = world_frame(world, run_id="run-room", branch_id="branch-room", computed_until=1)
    projected_room = frame["geometry_overlay"]["temporary_rooms"]["quiet-room-1"]
    assert projected_room["observed_occupant_agent_ids"] == ["A001"]
    assert projected_room["observed_occupied_beds"] == [{"bed_index": 0, "agent_id": "A001"}]
    restored = Checkpoint.capture(world).restore_world()

    world.minute_index = restored.minute_index = 5
    assert dispatcher.dispatch_due(world, 5)[0].status is EventStatus.APPLIED
    assert dispatcher.dispatch_due(restored, 5)[0].status is EventStatus.APPLIED
    assert world.model_state["geometry_overlay_v1"]["temporary_rooms"]["quiet-room-1"]["active"] is False
    assert world.canonical_json() == restored.canonical_json()


def test_physical_temporary_room_declines_occupied_polygon() -> None:
    world = _world_with_geometry()
    world.model_state["movement_v1"]["positions"]["A001"]["point"] = {
        "x": 1.5,
        "y": 1.5,
    }
    action = _effect(
        "occupied-temporary-room",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        {
            "schema_version": 1,
            "room_id": "quiet-room-occupied",
            "zone_id": "sleeping",
            "polygon_m": [
                {"x": 0.5, "y": 0.5}, {"x": 2.5, "y": 0.5},
                {"x": 2.5, "y": 2.5}, {"x": 0.5, "y": 2.5},
            ],
            "people_capacity": 2,
            "sleeping_capacity": 1,
            "bed_points_m": [{"x": 1.5, "y": 1.5}],
            "route_edge_ids": ["corridor_sleeping", "sleeping_corridor"],
            "source_ids": [],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
        end=5,
    )

    assert any("room_in_use" in issue for issue in validate_effect_program(world, _program(action)))


def test_sleep_action_routes_to_exclusive_temporary_bed_and_restores(tmp_path: Path) -> None:
    world = _world_with_geometry()
    action = _effect(
        "temporary-sleep-room",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        {
            "schema_version": 1,
            "room_id": "quiet-room-sleep",
            "zone_id": "sleeping",
            "polygon_m": [
                {"x": 0.5, "y": 0.5}, {"x": 2.5, "y": 0.5},
                {"x": 2.5, "y": 2.5}, {"x": 0.5, "y": 2.5},
            ],
            "people_capacity": 2,
            "sleeping_capacity": 1,
            "bed_points_m": [{"x": 1.5, "y": 1.5}],
            "route_edge_ids": ["corridor_sleeping", "sleeping_corridor"],
            "source_ids": ["ASM-TEMP-ROOM-SLEEP-TEST-V1"],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
        end=8,
    )
    assert validate_effect_program(world, _program(action)) == ()
    schedule_effect_program(world, _program(action))
    dispatcher = _dispatcher()
    world.minute_index = 1
    assert dispatcher.dispatch_due(world, 1)[0].status is EventStatus.APPLIED
    from sheltersim.domain.geometry_overlay import reserve_sleep_bed
    from sheltersim.domain.simulator_state import simulator_signatures

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

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    def proposal(minute: int) -> ActionProposal:
        view_hash = "a" * 64
        return ActionProposal(
            agent_id="A001",
            minute_index=minute,
            observed_state_hash=view_hash,
            action=ActionKind.SLEEP,
            candidates=(ActionCandidate(
                action=ActionKind.SLEEP,
                eligible=True,
                trigger="test sleep selection",
                utility=1.0,
                urgency_priority=0,
                probability=1.0,
            ),),
            trigger="test sleep selection",
            choice_value=0.0,
            rng_key=("test", "sleep", "A001", minute),
            selection_probability=1.0,
            policy_version="test-sleep-v1",
            next_decision_at=minute + 1,
        )

    controller = OfflineSimulationController(world, ArtifactSink())
    controller._apply_proposal(proposal(world.minute_index))
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
    due_minute = world.active_tasks[world.model_state["movement_v1"]["journeys"]["A001"]["task_id"]].due_minute
    world.minute_index = restored.minute_index = due_minute
    dispatcher_outcomes = controller.event_dispatcher.dispatch_due(world, due_minute)
    restored_controller = OfflineSimulationController(restored, ArtifactSink())
    restored_outcomes = restored_controller.event_dispatcher.dispatch_due(restored, due_minute)
    assert dispatcher_outcomes[0].status is restored_outcomes[0].status is EventStatus.APPLIED
    controller._sync_movement_after_events()
    restored_controller._sync_movement_after_events()
    assert world.model_state["movement_v1"]["positions"]["A001"]["point"] == {
        "x": 1.5, "y": 1.5,
    }
    assert world.canonical_json() == restored.canonical_json()

    controller._apply_proposal(proposal(due_minute))
    restored_controller._apply_proposal(proposal(due_minute))
    assert world.agents["A001"].activity.value == "sleeping"
    frame = world_frame(world, run_id="run-bed", branch_id="main", computed_until=due_minute)
    room = frame["geometry_overlay"]["temporary_rooms"]["quiet-room-sleep"]
    assert room["reserved_beds"] == [{"bed_index": 0, "agent_id": "A001"}]
    assert person_projection(world, "A001")["person"]["sleeping_place"]["room_id"] == "quiet-room-sleep"
    sleeping_checkpoint = Checkpoint.capture(world).restore_world()
    assert sleeping_checkpoint.canonical_json() == world.canonical_json()

    world.minute_index = restored.minute_index = due_minute = 8
    expired = controller.event_dispatcher.dispatch_due(world, due_minute)
    restored_controller.event_dispatcher.dispatch_due(restored, due_minute)
    controller._sync_movement_after_events()
    restored_controller._sync_movement_after_events()
    assert expired[0].status is EventStatus.APPLIED
    assert world.model_state["geometry_overlay_v1"]["temporary_rooms"]["quiet-room-sleep"]["active"] is False
    assert "A001" in world.model_state["geometry_overlay_v1"]["bed_reservations"]
    assert world.canonical_json() == restored.canonical_json()


def test_sleep_bed_route_replans_from_live_position_after_obstacle_change() -> None:
    world = _world_with_geometry()
    world.model_configs["movement_model"] = {
        "schema_version": 1,
        "policy_version": "spatial-route-and-room-waypoint-v2",
        "source_id": "ASM-SPATIAL-MOVEMENT-V2",
        "speed_m_per_minute_by_mobility": {
            "independent": 0.2,
            "assisted": 0.2,
            "wheelchair": 0.2,
        },
    }
    action = _effect(
        "temporary-sleep-room-replan",
        "temporary_room_geometry",
        "zone",
        "sleeping",
        {
            "schema_version": 1,
            "room_id": "quiet-room-replan",
            "zone_id": "sleeping",
            "polygon_m": [
                {"x": 0.5, "y": 0.5}, {"x": 2.5, "y": 0.5},
                {"x": 2.5, "y": 2.5}, {"x": 0.5, "y": 2.5},
            ],
            "people_capacity": 2,
            "sleeping_capacity": 1,
            "bed_points_m": [{"x": 1.5, "y": 1.5}],
            "route_edge_ids": ["corridor_sleeping", "sleeping_corridor"],
            "source_ids": ["ASM-TEMP-ROOM-REPLAN-TEST-V1"],
        },
        "metre_2d",
        ["geometry_overlay_v1"],
        end=40,
    )
    schedule_effect_program(world, _program(action))
    world.minute_index = 1
    assert _dispatcher().dispatch_due(world, 1)[0].status is EventStatus.APPLIED

    class ArtifactSink:
        trace_level = TraceLevel.OFF

        def record_decision(self, *_args: object, **_kwargs: object) -> None:
            pass

    controller = OfflineSimulationController(world, ArtifactSink())
    proposal = ActionProposal(
        agent_id="A001",
        minute_index=1,
        observed_state_hash="a" * 64,
        action=ActionKind.SLEEP,
        candidates=(ActionCandidate(
            action=ActionKind.SLEEP,
            eligible=True,
            trigger="test sleep selection",
            utility=1.0,
            urgency_priority=0,
            probability=1.0,
        ),),
        trigger="test sleep selection",
        choice_value=0.0,
        rng_key=("test", "sleep", "A001", 1),
        selection_probability=1.0,
        policy_version="test-sleep-v1",
        next_decision_at=2,
    )
    controller._apply_proposal(proposal)
    initial_journey = dict(world.model_state["movement_v1"]["journeys"]["A001"])
    assert len(initial_journey["route_points"]) == 2
    controller._update_movement(1)
    live_position = dict(world.model_state["movement_v1"]["positions"]["A001"]["point"])
    world.minute_index = 2

    world.model_configs["spatial_constraints"] = {
        "schema_version": 1,
        "obstacles": [{
            "id": "new-room-obstruction",
            "polygon": [
                {"x": 3.0, "y": 3.0}, {"x": 4.0, "y": 3.0},
                {"x": 4.0, "y": 4.0}, {"x": 3.0, "y": 4.0},
            ],
            "source_ids": ["ASM-ROOM-OBSTRUCTION-TEST-V1"],
        }],
    }
    controller._update_movement(2)

    replanned = world.model_state["movement_v1"]["journeys"]["A001"]
    assert replanned["purpose"] == "sleep_bed"
    assert replanned["route_points"] != initial_journey["route_points"]
    assert replanned["route_points"][0] == live_position
    assert replanned["event_id"] != initial_journey["event_id"]
    assert next(
        record for record in world.event_log.records
        if record.event.event_id == initial_journey["event_id"]
    ).status is EventStatus.SUPPRESSED
    replacement_event = next(
        event for event in world.event_queue.pending()
        if event.event_id == replanned["event_id"]
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
    assert world.model_state["movement_v1"]["positions"]["A001"]["point"] == {
        "x": 1.5, "y": 1.5,
    }
    assert world.canonical_json() == restored.canonical_json()


def test_geometry_overlay_checkpoint_without_temporary_room_key_still_restores() -> None:
    world = _world_with_geometry()
    del world.model_state["geometry_overlay_v1"]["temporary_rooms"]

    restored = Checkpoint.capture(world).restore_world()

    assert "temporary_rooms" not in restored.model_state["geometry_overlay_v1"]
    restored.validate()


def test_strict_primitive_parameters_reject_negative_quantity_and_extra_fields() -> None:
    world = _world(operational=True)
    negative = _effect(
        "invalid-delivery",
        "deliver_resource",
        "resource",
        "water_ml",
        {"warehouse_id": "warehouse-h18", "resource_id": "water_ml", "quantity": -1},
        "water_ml",
        ["resource_ledger_v1"],
    )
    direct_kpi_write = _effect(
        "direct-kpi-write",
        "deliver_resource",
        "resource",
        "water_ml",
        {
            "warehouse_id": "warehouse-h18",
            "resource_id": "water_ml",
            "quantity": 1,
            "stress": 1.0,
        },
        "water_ml",
        ["resource_ledger_v1"],
    )

    issues = validate_effect_program(world, _program(negative, program_id="negative-quantity"))
    direct_issues = validate_effect_program(
        world, _program(direct_kpi_write, program_id="direct-kpi-write")
    )

    assert any("parameters are invalid" in issue for issue in issues)
    assert any("parameters are invalid" in issue for issue in direct_issues)


def test_facility_modifier_expires_and_checkpoint_continuation_is_identical() -> None:
    original = _world()
    branch = BranchRuntime.from_world(original, branch_id="h18-branch")
    action = _effect(
        "toilet-failure",
        "set_facility_availability",
        "facility",
        "toilet_2",
        {"working_units": 0},
        "facility_unit",
        ["facility_capacity"],
        end=5,
    )

    receipt = branch.apply_effect_program(_program(action))

    assert receipt.event_ids == ("effect-event-0", "effect-event-1")
    assert "effect_program_v1" not in original.model_state
    with pytest.raises(BranchError, match="EffectProgram modifier is active"):
        branch.apply(SetFacilityWorkingUnitsCommand("toilet_2", 1))
    dispatcher = _dispatcher()
    branch.world.minute_index = 1
    start_record = dispatcher.dispatch_due(branch.world, 1)[0]
    assert start_record.status is EventStatus.APPLIED
    marker = start_record.event.provenance["effect_program_v1"]
    assert marker["effect_id"] == "toilet-failure"
    assert marker["compiler_provenance"]["compiler_version"] == "test-v1"
    assert branch.world.facilities["toilet_2"].working_units == 0
    checkpoint = Checkpoint.capture(branch.world)
    restored = checkpoint.restore_world()
    assert checkpoint.model_versions["effect_program_interpreter"] == "1"

    branch.world.minute_index = restored.minute_index = 5
    expiry_record = dispatcher.dispatch_due(branch.world, 5)[0]
    dispatcher.dispatch_due(restored, 5)

    assert expiry_record.event.causation_id == start_record.event.event_id
    assert branch.world.facilities["toilet_2"].working_units == 1
    assert branch.world.canonical_json() == restored.canonical_json()


def test_overlapping_facility_modifiers_stack_restrictively_and_expire_by_id() -> None:
    world = _world()
    first = _effect(
        "outage-first",
        "set_facility_availability",
        "facility",
        "toilet_2",
        {"working_units": 0},
        "facility_unit",
        ["facility_capacity"],
        start=1,
        end=4,
    )
    second = _effect(
        "outage-second",
        "set_facility_availability",
        "facility",
        "toilet_2",
        {"working_units": 0},
        "facility_unit",
        ["facility_capacity"],
        start=2,
        end=3,
    )
    schedule_effect_program(world, _program(first, second))
    dispatcher = _dispatcher()

    for minute in (1, 2, 3):
        world.minute_index = minute
        outcomes = dispatcher.dispatch_due(world, minute)
        assert outcomes and all(item.status is EventStatus.APPLIED for item in outcomes)
        assert world.facilities["toilet_2"].working_units == 0
    world.minute_index = 4
    assert dispatcher.dispatch_due(world, 4)[0].status is EventStatus.APPLIED
    assert world.facilities["toilet_2"].working_units == 1
    assert world.model_state["effect_program_v1"]["active_facility_modifiers"] == {}


def test_false_guard_suppresses_effect_and_never_changes_facility_state() -> None:
    world = _world()
    action = _effect(
        "guarded-outage",
        "set_facility_availability",
        "facility",
        "toilet_2",
        {"working_units": 0},
        "facility_unit",
        ["facility_capacity"],
        end=5,
        guard=GuardExpression(operator="eq", field="target.working_units", value=0),
    )
    schedule_effect_program(world, _program(action))
    dispatcher = _dispatcher()

    world.minute_index = 1
    start = dispatcher.dispatch_due(world, 1)
    world.minute_index = 5
    expiry = dispatcher.dispatch_due(world, 5)

    assert start[0].status is EventStatus.SUPPRESSED
    assert expiry[0].status is EventStatus.SUPPRESSED
    assert world.facilities["toilet_2"].working_units == 1


def test_resource_delivery_and_compiled_structural_additions_use_domain_state() -> None:
    world = _world(operational=True)
    assert RESOURCE_LEDGER_KEY in world.model_state
    actions = [
        _effect(
            "delivery",
            "deliver_resource",
            "resource",
            "water_ml",
            {"warehouse_id": "warehouse-h18", "resource_id": "water_ml", "quantity": 250},
            "water_ml",
            ["resource_ledger_v1"],
            start=1,
            reversible=False,
        ),
        _effect(
            "add-staff",
            "add_staff",
            "zone",
            "information_desk",
            {"count": 2},
            "staff_member",
            ["staff_operations"],
            start=2,
            reversible=False,
        ),
        _effect(
            "add-toilets",
            "add_toilet",
            "zone",
            "toilet",
            {"count": 2},
            "toilet_stall",
            ["facility_capacity"],
            start=3,
            reversible=False,
        ),
    ]
    schedule_effect_program(world, _program(*actions))
    dispatcher = _dispatcher()

    world.minute_index = 1
    dispatcher.dispatch_due(world, 1)
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["remaining"] == 1250
    world.minute_index = 2
    dispatcher.dispatch_due(world, 2)
    assert len(world.staff) == 7
    world.minute_index = 3
    dispatcher.dispatch_due(world, 3)
    assert (
        len([facility for facility in world.facilities.values() if facility.kind == "toilet"]) == 6
    )
    world.validate()


def test_supply_delay_staff_absence_and_water_outage_reuse_h03_h02_handlers() -> None:
    world = _world(operational=True, resource_service=True)
    actions = [
        _effect(
            "delay-shipment",
            "modify_supply_schedule",
            "shipment",
            "shipment-main",
            {"shipment_id": "shipment-main", "to_minute": 30},
            "minute",
            ["operational_events_v1"],
            start=1,
            reversible=False,
        ),
        _effect(
            "staff-absence",
            "set_staff_availability",
            "staff",
            "S01",
            {"available": False},
            "staff_member",
            ["operational_events_v1"],
            start=2,
            end=4,
        ),
        _effect(
            "water-outage",
            "water_outage",
            "facility",
            "water-station-h18",
            {"outage": True},
            "water_supply",
            ["operational_events_v1", "resource_service_v1"],
            start=3,
            end=5,
        ),
    ]
    schedule_effect_program(world, _program(*actions))
    dispatcher = _dispatcher()

    world.minute_index = 1
    delay_record = dispatcher.dispatch_due(world, 1)[0]
    shipment = world.model_state["operational_events_v1"]["shipments"]["shipment-main"]
    assert shipment["due_minute"] == 30
    delayed_delivery = next(
        item for item in world.event_queue.pending() if item.event_id == shipment["event_id"]
    )
    assert delayed_delivery.causation_id == delay_record.event.event_id
    assert delayed_delivery.provenance["effect_program_v1"]["phase"] == "derived"
    world.minute_index = 2
    dispatcher.dispatch_due(world, 2)
    assert world.model_state["operational_events_v1"]["staff_status"]["S01"]["available"] is False
    world.minute_index = 3
    dispatcher.dispatch_due(world, 3)
    assert water_service_blocked(world, "water-station-h18")
    world.minute_index = 4
    dispatcher.dispatch_due(world, 4)
    assert world.model_state["operational_events_v1"]["staff_status"]["S01"]["available"] is True
    world.minute_index = 5
    dispatcher.dispatch_due(world, 5)
    assert not water_service_blocked(world, "water-station-h18")
    world.validate()


def test_h03_policy_dependencies_are_required_for_staff_availability() -> None:
    world = _world()
    action = _effect(
        "staff-absence",
        "set_staff_availability",
        "staff",
        "S01",
        {"available": False},
        "staff_member",
        ["operational_events_v1"],
        end=5,
    )

    issues = validate_effect_program(world, _program(action))

    assert any("requires operational_events_v1" in issue for issue in issues)
