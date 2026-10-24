"""Typed EffectProgram orders enter the routed staff task lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import ingest_applied_shared_operation_records
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.events.effects import (
    EffectProgram,
    EffectProgramStructureHandler,
    SupportMode,
    TimedAction,
    schedule_effect_program,
    validate_effect_program,
)
from sheltersim.events.handlers import (
    EffectProgramPopulationHandler,
    EventDispatcher,
    StaffOperationArrivalHandler,
    StaffOperationCompletionHandler,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"


def _world(*, body_policy: bool = False) -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 45
    document["model_configs"]["staff_operations"] = {}
    document["model_configs"]["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    if body_policy:
        from sheltersim.domain.body_state import canonical_body_config

        document["model_configs"]["body_dynamics"] = canonical_body_config(None)
        document["model_configs"]["policy_registry"] = {
            "schema_version": 2,
            "psychology": "life-state-feedback-v1",
        }
    document["staff"][0]["skills"] = ["cleaning"]
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    initialize_simulator_extensions(world)
    return world


def _dispatcher() -> EventDispatcher:
    return EventDispatcher({
        "effect_program_structure": EffectProgramStructureHandler(),
        "staff_operation_arrive": StaffOperationArrivalHandler(),
        "staff_operation_complete": StaffOperationCompletionHandler(),
    })


def _task_program(
    *, task_kind: str, target_zone_id: str, target_id: str | None = None
) -> EffectProgram:
    effect = TimedAction.model_validate({
        "effect_id": f"order-{task_kind}",
        "op": "staff_task_order",
        "target_selector": {"kind": "staff", "target_id": "S01"},
        "start_minute": 0,
        "parameters": {
            "task_kind": task_kind,
            "target_zone_id": target_zone_id,
            "target_id": target_id,
            "reason": f"operator directed {task_kind}",
        },
        "units": "staff_task_order",
        "source_id": "ASM-OPERATOR-STAFF-ORDER-TEST",
        "policy_dependencies": ["staff_operations_v1", "shared_operations_v1"],
    })
    return EffectProgram.model_validate({
        "program_id": f"operator-{task_kind}-order",
        "narrative": f"Order one worker to perform {task_kind}.",
        "source_text": f"Assign staff S01 to perform {task_kind}.",
        "simulation_start_minute": 0,
        "effects": [effect.model_dump(mode="json")],
        "support_mode": SupportMode.NATIVE,
        "compiler_provenance": {
            "compiler_kind": "offline_rule",
            "compiler_version": "test-v1",
        },
    })


def _queue_arrival_for_intake(world: WorldState) -> None:
    from sheltersim.events.effects import schedule_effect_program

    effect = TimedAction.model_validate({
        "effect_id": "queue-arrival",
        "op": "add_evacuees",
        "target_selector": {"kind": "zone", "target_id": "sleeping"},
        "start_minute": 0,
        "parameters": {
            "count": 1,
            "household_size": 1,
            "mobility": "independent",
            "language": "ja",
            "admission_zone_ids": [],
            "approach_route_id": None,
            "person_context_overrides": [],
        },
        "units": "evacuee",
        "source_id": "ASM-OPERATOR-STAFF-ORDER-TEST",
        "policy_dependencies": ["population_dynamics_v1"],
        "reversible": False,
    })
    program = EffectProgram.model_validate({
        "program_id": "queue-arrival-for-intake-test",
        "narrative": "Queue one synthetic arrival for intake.",
        "source_text": "Queue one arrival.",
        "simulation_start_minute": 0,
        "effects": [effect.model_dump(mode="json")],
        "support_mode": SupportMode.NATIVE,
        "compiler_provenance": {
            "compiler_kind": "offline_rule",
            "compiler_version": "test-v1",
        },
    })
    world.zones["sleeping"].capacity = sum(
        agent.zone_id == "sleeping" for agent in world.agents.values()
    )
    schedule_effect_program(world, program)
    outcomes = EventDispatcher({
        "effect_program_population": EffectProgramPopulationHandler(),
    }).dispatch_due(world, 0)
    assert outcomes and outcomes[0].status.value == "applied"
    assert world.model_state["population_dynamics_v1"]["pending_arrivals"]


def test_staff_task_order_routes_and_completes_with_checkpointed_history(tmp_path: Path) -> None:
    world = _world()
    world.model_state["staff_operations_v1"]["zones"]["sleeping"]["sanitation"] = 0.4
    source_id = "ASM-OPERATOR-STAFF-ORDER-TEST"
    effect = TimedAction.model_validate({
        "effect_id": "clean-sleeping-zone",
        "op": "staff_task_order",
        "target_selector": {"kind": "staff", "target_id": "S01"},
        "start_minute": 0,
        "parameters": {
            "task_kind": "cleaning",
            "target_zone_id": "sleeping",
            "target_id": None,
            "reason": "operator directed sanitation recovery",
        },
        "units": "staff_task_order",
        "source_id": source_id,
        "policy_dependencies": ["staff_operations_v1", "shared_operations_v1"],
    })
    program = EffectProgram.model_validate({
        "program_id": "operator-cleaning-order",
        "narrative": "Order a qualified worker to clean the sleeping zone.",
        "source_text": "Assign staff S01 to clean sleeping.",
        "simulation_start_minute": 0,
        "effects": [effect.model_dump(mode="json")],
        "support_mode": SupportMode.NATIVE,
        "compiler_provenance": {
            "compiler_kind": "offline_rule",
            "compiler_version": "test-v1",
        },
    })

    assert validate_effect_program(world, program) == ()
    unqualified_effect = effect.model_copy(update={
        "parameters": {
            **effect.parameters,
            "task_kind": "maintenance",
            "target_id": "toilet_1",
        },
    })
    unqualified_program = program.model_copy(update={
        "program_id": "unqualified-cleaning-order",
        "effects": [unqualified_effect],
    })
    assert any(
        "lacks the registered task skill" in issue
        for issue in validate_effect_program(_world(), unqualified_program)
    )
    late_effect = effect.model_copy(update={"start_minute": 1})
    late_program = program.model_copy(update={
        "program_id": "late-cleaning-order",
        "simulation_start_minute": 1,
        "effects": [late_effect],
    })
    assert any(
        "current branch minute" in issue
        for issue in validate_effect_program(_world(), late_program)
    )

    schedule_effect_program(world, program)
    start_outcomes = _dispatcher().dispatch_due(world, 0)
    ingest_applied_shared_operation_records(world, start_outcomes)
    order_event = next(
        row.event for row in start_outcomes
        if row.event.event_type == "effect_program_structure"
    )
    assert world.active_tasks
    task = next(iter(world.active_tasks.values()))
    assert task.staff_id == "S01"
    assert task.due_minute > 0

    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()
    for minute in range(1, task.due_minute + 1):
        for candidate in (world, restored):
            candidate.minute_index = minute
            outcomes = _dispatcher().dispatch_due(candidate, minute)
            ingest_applied_shared_operation_records(candidate, outcomes)

    assert restored.canonical_json() == world.canonical_json()
    completion = next(
        row.event for row in world.event_log.records
        if row.event.event_type == "staff_operation_complete"
        and row.status.value == "applied"
    )
    receipt = next(
        row.event for row in world.event_log.records
        if row.event.event_type == "staff_operation_transition"
        and row.status.value == "applied"
    )
    assert completion.causation_id == order_event.event_id
    assert receipt.causation_id == completion.event_id
    assert receipt.payload["reason"] == "operator directed sanitation recovery"
    assert receipt.payload["state_changes"][0]["field_path"] == (
        "staff_operations_v1.zones.sanitation"
    )

    store = ProjectStore(tmp_path)
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    checkpoint = Checkpoint.capture(world)
    index = HistoryIndex(store, "project", "run")
    index.ingest("main", checkpoint, rebuild=True)
    page = index.search(
        "main",
        HistoryQuery(
            kind="operations",
            staff_id="S01",
            event_id=receipt.event_id,
            start=completion.due_minute,
            end=completion.due_minute,
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["reason"] == "operator directed sanitation recovery"


@pytest.mark.parametrize(
    ("task_kind", "target_zone_id", "skill"),
    [
        ("maintenance", "toilet", "maintenance"),
        ("information_outreach", "sleeping", "information"),
        ("intake", "sleeping", "intake"),
        ("patrol", "sleeping", "patrol"),
    ],
)
def test_all_other_registered_staff_task_kinds_complete(
    task_kind: str, target_zone_id: str, skill: str
) -> None:
    world = _world(body_policy=task_kind == "information_outreach")
    world.staff["S01"].skills = (skill,)
    target_id = None
    if task_kind == "maintenance":
        target_id = "toilet_1"
        world.facilities[target_id].working_units = 0
    elif task_kind == "intake":
        _queue_arrival_for_intake(world)

    program = _task_program(
        task_kind=task_kind,
        target_zone_id=target_zone_id,
        target_id=target_id,
    )
    assert validate_effect_program(world, program) == ()
    schedule_effect_program(world, program)
    dispatcher = _dispatcher()
    start_outcomes = dispatcher.dispatch_due(world, 0)
    order_event = next(
        row.event for row in start_outcomes
        if row.event.event_type == "effect_program_structure"
    )
    task = next(iter(world.active_tasks.values()))
    if task_kind == "intake":
        world.zones["sleeping"].capacity += 1

    world.minute_index = task.due_minute
    outcomes = dispatcher.dispatch_due(world, task.due_minute)
    completion = next(
        row.event for row in outcomes
        if row.event.event_type == "staff_operation_complete"
    )

    assert completion.causation_id == order_event.event_id
    assert not world.active_tasks
    completed = world.model_state["staff_operations_v1"]["history"][-1]
    assert completed["task_kind"] == task_kind
    assert completed["status"] == "completed"
    if task_kind == "maintenance":
        assert world.facilities[target_id].working_units == 1
    if task_kind == "intake":
        assert not world.model_state["population_dynamics_v1"]["pending_arrivals"]
    world.validate()
