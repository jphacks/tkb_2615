"""Shared operations: continuity receipts, and how admissions feed operational demand."""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.contracts.operations import StaffSupportContinuityReceipt
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.common_operations import _event_payload_matches, operational_demand_snapshot
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.staff_operations import (
    STAFF_OPERATIONS_STATE_KEY,
    assign_staff_operations,
    update_staff_operations,
)
from sheltersim.domain.zones import ZoneGraph
from sheltersim.events.effects import (
    EffectProgram,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.handlers import (
    EffectProgramPopulationHandler,
    EventDispatcher,
    ResourceServiceCompletionHandler,
    StaffOperationArrivalHandler,
    StaffOperationCompletionHandler,
)
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import world_frame
from sheltersim.policies.space import SpaceRouter

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenarios" / "h00_small_replay.json"


def test_pre_cause_type_continuity_receipt_remains_valid() -> None:
    payload = {
        "record_type": "care",
        "schema_version": 1,
        "event_type": "staff_support_continuity",
        "event_id": "support-continuity-legacy",
        "request_id": "request-legacy",
        "agent_id": "A001",
        "task_id": "task-legacy",
        "previous_staff_id": "S01",
        "status": "requeued",
        "minute": 3,
        "causation_id": "staff-off-legacy",
        "source_ids": ["ASM-STAFF-SUPPORT-LIFECYCLE-V1"],
    }
    receipt = StaffSupportContinuityReceipt.model_validate(payload)
    event = ScheduledEvent(
        event_id=receipt.event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=receipt.minute,
        priority=0,
        sequence=8,
        event_type=receipt.event_type,
        payload=payload,
        source_id="ASM-STAFF-SUPPORT-LIFECYCLE-V1",
        causation_id=receipt.causation_id,
    )
    record = EventLogRecord(
        event=event,
        realized_minute=receipt.minute,
        status=EventStatus.APPLIED,
        detail="legacy continuity record",
    )

    assert receipt.cause_type == "staff_unavailable"
    assert _event_payload_matches(record, receipt)


def _demand_world() -> WorldState:
    """The small replay fixture with cleaning, intake, water and meal staff by hand.

    Generated staff have no cleaning skill (#75) and the coupling below needs its own
    sanitation and task coefficients, so this stays on a hand-built world. Arrivals
    on a generated Scenario are run in tests/integration/test_intervention_outcomes.py.
    """
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 24
    document["scheduled_events"] = []
    document["zones"] = [
        {**zone, "capacity": 1} if zone["id"] == "toilet" else zone for zone in document["zones"]
    ]
    document["facilities"].append(
        {
            "id": "water-arrival-test",
            "kind": "water_station",
            "zone_id": "toilet",
            "capacity": 1,
            "working_units": 1,
        }
    )
    document["facilities"].append(
        {
            "id": "meal-arrival-test",
            "kind": "meal_distribution",
            "zone_id": "toilet",
            "capacity": 1,
            "working_units": 1,
        }
    )
    document["staff"].extend(
        [
            {"id": "S-CLEAN-SLEEP", "zone_id": "sleeping", "skills": ["cleaning"]},
            {"id": "S-CLEAN-TOILET", "zone_id": "toilet", "skills": ["cleaning"]},
            {"id": "S-INTAKE", "zone_id": "toilet", "skills": ["support", "intake"]},
            {"id": "S-WATER", "zone_id": "toilet", "skills": ["water_service"]},
            {"id": "S-MEAL", "zone_id": "toilet", "skills": ["meal_service"]},
        ]
    )
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-arrival-test",
        "zone_id": "toilet",
        "water_ml": 1000,
        "food_meal_units": 0,
        "source_ids": ["ASM-TEST-ARRIVAL-STOCK-V1"],
    }
    document["model_configs"]["simulator_extensions"] = {"enabled": True}
    document["model_configs"]["state_contracts"] = {
        "schema_version": 1,
        "namespaces": {
            "population_presence_v1": {
                "implementation_id": "population-presence-ledger-v1",
                "implementation_version": "1",
                "schema_version": 1,
            }
        },
    }
    document["model_configs"]["staff_operations"] = {
        "sanitation_use_per_person_minute": 0.2,
        "cleaning_sanitation_recovery": 0.5,
        "task_duration_minutes": {
            "cleaning": 4,
            "maintenance": 4,
            "information_outreach": 4,
            "intake": 4,
            "patrol": 4,
        },
    }
    document["model_configs"]["resource_service"] = {
        "schema_version": 1,
        "policy_version": "resource-service-v1",
        "source_id": "ASM-TEST-ARRIVAL-RESOURCE-V1",
        "source_type": "assumption",
        "service_minutes": 2,
        "water_ml_per_service": 250,
        "food_meal_units_per_service": 1,
        "hydration_growth_per_minute": 0.02,
    }
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def _schedule_arrival(
    world: WorldState,
    *,
    op: str = "add_evacuees",
    count: int = 2,
    start_minute: int = 1,
    admission_zone_ids: tuple[str, ...] = (),
    approach_route_id: str | None = None,
    person_context_overrides: tuple[dict[str, object], ...] = (),
    zone_id: str = "toilet",
) -> None:
    parameters = (
        {
            "count": count,
            "household_size": 2,
            "mobility": "independent",
            "language": "ja",
            "admission_zone_ids": list(admission_zone_ids),
            "approach_route_id": approach_route_id,
            "person_context_overrides": list(person_context_overrides),
        }
        if op == "add_evacuees"
        else {"count": count, "include_initial": False}
    )
    effect = TimedAction(
        effect_id="population-change",
        op=op,
        target_selector=TargetSelector(kind="zone", target_id=zone_id),
        start_minute=start_minute,
        parameters=parameters,
        units="evacuee",
        source_id="ASM-TEST-DYNAMIC-ARRIVAL-V1",
        policy_dependencies=["population_dynamics_v1"],
        reversible=False,
    )
    program = EffectProgram(
        program_id=f"population-demand-{op}-{start_minute}",
        narrative="Apply a small population change for a demand coupling check.",
        source_text=op,
        simulation_start_minute=start_minute,
        effects=[effect],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )
    schedule_effect_program(world, program)


def test_admission_changes_capacity_sanitation_staff_and_resource_state_across_restore() -> None:
    world = _demand_world()
    _schedule_arrival(world)
    dispatcher = EventDispatcher({"effect_program_population": EffectProgramPopulationHandler()})

    world.minute_index = 1
    arrivals = dispatcher.dispatch_due(world, 1)

    assert len(arrivals) == 1
    assert arrivals[0].status.value == "applied"
    population = world.model_state["population_dynamics_v1"]
    admitted_ids = [row["agent_id"] for row in population["arrivals"] if "agent_id" in row]
    assert len(admitted_ids) == 1
    assert len(population["pending_arrivals"]) == 1
    assert len(world.agents) == 5
    waiting_id = population["pending_arrivals"][0]["profile"]["id"]
    assert world.model_state["population_presence_v1"]["people"][waiting_id]["status"] == (
        "awaiting_admission"
    )

    update_staff_operations(world, 1)
    zone = world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]
    assert zone["sanitation"] == 0.8
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    tasks = assign_staff_operations(world, 1, router)
    assert len(tasks) == 2
    operations = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]
    assert operations["S-CLEAN-SLEEP"]["target_zone_id"] == "sleeping"
    assert operations["S-CLEAN-TOILET"]["target_zone_id"] == "toilet"
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S-INTAKE"]["phase"] == "idle"
    cleaning_zone = operations["S-CLEAN-TOILET"]["target_zone_id"]

    service = ResourceServiceManager(world)
    hydration_before = world.agents[admitted_ids[0]].needs.hydration_need
    service.request(admitted_ids[0], "water-arrival-test", minute=1)
    completion_events = service.arbitrate(1)
    assert len(completion_events) == 1
    assert completion_events[0].payload["agent_id"] == admitted_ids[0]
    assert world.resources["water_ml"] == 1000
    assert operational_demand_snapshot(world)["population_count"] == 5

    world = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    service = ResourceServiceManager(world)
    dispatcher = EventDispatcher(
        {
            "resource_service_complete": ResourceServiceCompletionHandler(service),
            "staff_operation_arrive": StaffOperationArrivalHandler(),
            "staff_operation_complete": StaffOperationCompletionHandler(),
        }
    )
    water_completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "resource_service_complete"
    )
    world.minute_index = water_completion.due_minute
    dispatcher.dispatch_due(world, world.minute_index)
    assert world.resources["water_ml"] == 750
    assert world.agents[admitted_ids[0]].needs.hydration_need < hydration_before

    cleaning_completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete"
        and event.payload["staff_id"] == "S-CLEAN-TOILET"
    )
    world.minute_index = cleaning_completion.due_minute
    task_id = cleaning_completion.payload["task_id"]
    assert (
        world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S-CLEAN-TOILET"]["task_id"]
        == task_id
    )
    assert world.active_tasks[task_id].staff_id == "S-CLEAN-TOILET"
    cleaning_outcomes = dispatcher.dispatch_due(world, world.minute_index)
    assert len(cleaning_outcomes) == 2
    assert all(outcome.status.value == "applied" for outcome in cleaning_outcomes)
    cleaned_zones = world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]
    assert cleaned_zones[cleaning_zone]["sanitation"] == 1.0
    assert cleaned_zones["sleeping"]["sanitation"] == 0.7
    frame = world_frame(
        world, run_id="arrival-test", branch_id="main", computed_until=world.minute_index
    )
    assert any(person["id"] == admitted_ids[0] for person in frame["people"])
    assert frame["operational_demand"]["population_count"] == 5
    assert len(world.model_state["population_dynamics_v1"]["pending_arrivals"]) == 1
    world.validate()
