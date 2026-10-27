"""EffectProgram preflight, deterministic execution, and checkpoint contracts.

The tests on the shared generated Scenario check that approving a program counts
what earlier programs have already scheduled. The tests on the small fixture
(fixtures/scenarios/h00_small_replay.json, no floor plan) check the primitive
registry, strict parameters, facility modifiers, guards, and the reuse of the
resource, staff and water handlers by hand-dispatching events. Geometry programs
are checked in tests/unit/domain/test_geometry_overlay.py and
tests/unit/experiments/test_runner.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
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
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.branch import (
    BranchError,
    BranchRuntime,
    SetFacilityWorkingUnitsCommand,
)
from sheltersim.experiments.scenario import ScenarioSpec

ROOT = Path(__file__).parents[3]
SMALL_SCENARIO = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
SOURCE_ID = "ASM-H18-EFFECT-TEST-V1"

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


def _one_effect_program(
    program_id: str,
    op: str,
    parameters: dict[str, Any],
    units: str,
    *,
    target: TargetSelector | None = None,
    policy: str | None = None,
    end_minute: int | None = 300,
) -> EffectProgram:
    return EffectProgram(
        program_id=program_id,
        narrative=op,
        source_text=op,
        simulation_start_minute=120,
        effects=[
            TimedAction(
                effect_id=f"{op}-1",
                op=op,
                target_selector=target or TargetSelector(kind="zone", target_id="sleeping"),
                start_minute=120,
                end_minute=end_minute,
                parameters=parameters,
                units=units,
                source_id="TEST-EFFECTS",
                policy_dependencies=[
                    policy
                    or (
                        "geometry_overlay_v1"
                        if op == "temporary_room_geometry"
                        else "temporary_spaces_v1"
                    )
                ],
                reversible=end_minute is not None,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


def test_a_temporary_room_cannot_reuse_the_id_of_one_still_scheduled(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    first = _one_effect_program("first-room", "temporary_room_geometry", ROOM, "metre_2d")
    assert validate_effect_program(world, first) == ()
    schedule_effect_program(world, first)

    second = _one_effect_program("second-room", "temporary_room_geometry", ROOM, "metre_2d")

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
    one_more = _one_effect_program(
        "one-more",
        "add_temporary_space",
        {"people_capacity": 1, "sleeping_capacity": 0},
        "person_capacity",
    )

    assert "temporary person capacity exceeds the 1000-person per-zone bound" in (
        validate_effect_program(world, one_more)
    )


def test_a_staff_role_change_is_scheduled_with_its_new_skills(generated_scenario) -> None:
    # Scheduling used to fail with AttributeError before the Branch existed (#72).
    world = WorldState.from_scenario(generated_scenario)
    program = _one_effect_program(
        "support-role",
        "set_staff_role",
        {"skills": ["support", "water_service"]},
        "staff_role",
        target=TargetSelector(kind="staff", target_id="staff_002"),
        policy="staff_operations_v1",
        end_minute=None,
    )
    assert validate_effect_program(world, program) == ()

    schedule_effect_program(world, program)

    (start,) = [
        event
        for event in world.event_queue.pending()
        if event.payload.get("op") == "set_staff_role"
    ]
    assert start.event_type == "effect_program_structure"
    assert (start.due_minute, start.payload["staff_id"]) == (120, "staff_002")
    assert list(start.payload["skills"]) == ["support", "water_service"]


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


@pytest.mark.parametrize("duration_days", [0.25, 1, 3])
@pytest.mark.parametrize(
    ("op", "target", "parameters", "units", "policies"),
    [
        (
            "water_outage",
            TargetSelector(kind="facility", target_id="water_station_main"),
            {"outage": True},
            "water_supply",
            ["operational_events_v1", "resource_service_v1"],
        ),
        (
            "set_staff_availability",
            TargetSelector(kind="staff", target_id="staff_001"),
            {"available": False},
            "staff_member",
            ["operational_events_v1"],
        ),
    ],
)
def test_generated_scenarios_shorter_than_four_days_accept_outages_and_absences(
    generated_scenario_with, duration_days, op, target, parameters, units, policies
) -> None:
    # Before #65 only Scenarios long enough to plan a delivery (day 4 on) had the
    # operational state these need. They run on the shared Scenario in
    # tests/integration/test_intervention_outcomes.py.
    world = WorldState.from_scenario(generated_scenario_with(duration_days=duration_days))
    program = _one_effect_program(f"short-{op}", op, parameters, units, target=target)
    effect = program.effects[0].model_copy(update={"policy_dependencies": policies})
    program = program.model_copy(update={"effects": [effect]})

    assert validate_effect_program(world, program) == ()
