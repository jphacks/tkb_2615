"""Typed EffectProgram orders enter the routed staff task lifecycle.

The lifecycle runs on the small fixture with one cleaner. On the shared generated
Scenario, an order no worker can take is refused before approval with a reason
the operator can act on (#75).

Thresholds, overrides, priorities and the relief of a task at a shift boundary
are checked on the 100-person fixture with two workers whose shifts meet.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import ingest_applied_shared_operation_records
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import StaffService
from sheltersim.domain.staff_dynamics import STAFF_DYNAMICS_STATE_KEY, update_staff_dynamics
from sheltersim.domain.staff_operations import (
    STAFF_OPERATIONS_STATE_KEY,
    assign_staff_operations,
    schedule_staff_operation_relief,
    update_staff_operations,
)
from sheltersim.domain.zones import ZoneGraph
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
    StaffOperationReliefHandler,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.space import SpaceRouter
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

ROOT = Path(__file__).parents[3]
FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
SCENARIO_100_AGENTS = ROOT / "fixtures" / "scenario_100_agents.json"


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
    return EventDispatcher(
        {
            "effect_program_structure": EffectProgramStructureHandler(),
            "staff_operation_arrive": StaffOperationArrivalHandler(),
            "staff_operation_complete": StaffOperationCompletionHandler(),
        }
    )


def _task_program(
    *, task_kind: str, target_zone_id: str, target_id: str | None = None, staff_id: str = "S01"
) -> EffectProgram:
    effect = TimedAction.model_validate(
        {
            "effect_id": f"order-{task_kind}",
            "op": "staff_task_order",
            "target_selector": {"kind": "staff", "target_id": staff_id},
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
        }
    )
    return EffectProgram.model_validate(
        {
            "program_id": f"operator-{task_kind}-order",
            "narrative": f"Order one worker to perform {task_kind}.",
            "source_text": f"Assign staff {staff_id} to perform {task_kind}.",
            "simulation_start_minute": 0,
            "effects": [effect.model_dump(mode="json")],
            "support_mode": SupportMode.NATIVE,
            "compiler_provenance": {
                "compiler_kind": "offline_rule",
                "compiler_version": "test-v1",
            },
        }
    )


def _queue_arrival_for_intake(world: WorldState) -> None:
    from sheltersim.events.effects import schedule_effect_program

    effect = TimedAction.model_validate(
        {
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
        }
    )
    program = EffectProgram.model_validate(
        {
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
        }
    )
    world.zones["sleeping"].capacity = sum(
        agent.zone_id == "sleeping" for agent in world.agents.values()
    )
    schedule_effect_program(world, program)
    outcomes = EventDispatcher(
        {
            "effect_program_population": EffectProgramPopulationHandler(),
        }
    ).dispatch_due(world, 0)
    assert outcomes and outcomes[0].status.value == "applied"
    assert world.model_state["population_dynamics_v1"]["pending_arrivals"]


def test_staff_task_order_routes_and_completes_with_checkpointed_history(tmp_path: Path) -> None:
    world = _world()
    world.model_state["staff_operations_v1"]["zones"]["sleeping"]["sanitation"] = 0.4
    source_id = "ASM-OPERATOR-STAFF-ORDER-TEST"
    effect = TimedAction.model_validate(
        {
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
        }
    )
    program = EffectProgram.model_validate(
        {
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
        }
    )

    assert validate_effect_program(world, program) == ()
    unqualified_effect = effect.model_copy(
        update={
            "parameters": {
                **effect.parameters,
                "task_kind": "maintenance",
                "target_id": "toilet_1",
            },
        }
    )
    unqualified_program = program.model_copy(
        update={
            "program_id": "unqualified-cleaning-order",
            "effects": [unqualified_effect],
        }
    )
    assert any(
        "no_qualified_staff" in issue
        for issue in validate_effect_program(_world(), unqualified_program)
    )
    late_effect = effect.model_copy(update={"start_minute": 1})
    late_program = program.model_copy(
        update={
            "program_id": "late-cleaning-order",
            "simulation_start_minute": 1,
            "effects": [late_effect],
        }
    )
    assert any(
        "current branch minute" in issue
        for issue in validate_effect_program(_world(), late_program)
    )

    schedule_effect_program(world, program)
    start_outcomes = _dispatcher().dispatch_due(world, 0)
    ingest_applied_shared_operation_records(world, start_outcomes)
    order_event = next(
        row.event for row in start_outcomes if row.event.event_type == "effect_program_structure"
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
        row.event
        for row in world.event_log.records
        if row.event.event_type == "staff_operation_complete" and row.status.value == "applied"
    )
    receipt = next(
        row.event
        for row in world.event_log.records
        if row.event.event_type == "staff_operation_transition" and row.status.value == "applied"
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
        row.event for row in start_outcomes if row.event.event_type == "effect_program_structure"
    )
    task = next(iter(world.active_tasks.values()))
    if task_kind == "intake":
        world.zones["sleeping"].capacity += 1

    world.minute_index = task.due_minute
    outcomes = dispatcher.dispatch_due(world, task.due_minute)
    completion = next(
        row.event for row in outcomes if row.event.event_type == "staff_operation_complete"
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


@pytest.mark.parametrize(
    ("task_kind", "target_zone_id", "target_id", "advice"),
    [
        ("cleaning", "sleeping", None, "担当変更（set_staff_role）"),
        ("maintenance", "toilet", "toilet_01", "修理の手配（facility_repair）"),
    ],
)
def test_generated_staff_orders_for_cleaning_or_maintenance_say_no_one_can_do_them(
    generated_scenario, task_kind, target_zone_id, target_id, advice
) -> None:
    # In a shelter this work falls to evacuee rotas, the facility manager or
    # contractors, so generated Scenarios staff no one for it (#75).
    world = WorldState.from_scenario(generated_scenario)
    program = _task_program(
        task_kind=task_kind,
        target_zone_id=target_zone_id,
        target_id=target_id,
        staff_id="staff_001",
    )

    [issue] = validate_effect_program(world, program)

    assert "no_qualified_staff:" in issue
    assert "を持つ職員がいません" in issue
    assert advice in issue


def test_an_order_to_an_unqualified_worker_names_who_could_take_it(generated_scenario) -> None:
    world = WorldState.from_scenario(generated_scenario)
    # staff_002 runs a water or meal station; only staff_001 patrols.
    program = _task_program(task_kind="patrol", target_zone_id="sleeping", staff_id="staff_002")

    [issue] = validate_effect_program(world, program)

    assert "staff_lacks_task_skill: staff_002は巡回の技能" in issue
    assert "技能を持つ職員（staff_001）に指示してください" in issue


def _relief_world(
    previous_zone_id: str = "toilet",
    *,
    staff_policy_id: str | None = None,
    fatigue_policy_id: str | None = None,
    thresholds_by_zone: dict[str, dict[str, float]] | None = None,
    effects_by_zone: dict[str, dict[str, float]] | None = None,
    task_duration_minutes_by_kind: dict[str, int] | None = None,
    body_policy_enabled: bool = False,
) -> WorldState:
    document = ScenarioSpec.load(SCENARIO_100_AGENTS).model_dump(mode="json")
    document["duration_minutes"] = 16
    document["facilities"][0]["working_units"] = 0
    document["staff"] = [
        {
            "id": "S01",
            "zone_id": previous_zone_id,
            "skills": ["maintenance", "cleaning"],
        },
        {"id": "S02", "zone_id": "toilet", "skills": ["repair"]},
    ]
    document["model_configs"]["staff_operations"] = {
        "task_duration_minutes": {
            "cleaning": 4,
            "maintenance": 4,
            "information_outreach": 4,
            "intake": 4,
            "patrol": 4,
        }
    }
    if task_duration_minutes_by_kind is not None:
        document["model_configs"]["staff_operations"]["task_duration_minutes"].update(
            task_duration_minutes_by_kind
        )
    if thresholds_by_zone is not None:
        document["model_configs"]["staff_operations"]["thresholds_by_zone"] = thresholds_by_zone
    if effects_by_zone is not None:
        document["model_configs"]["staff_operations"]["effects_by_zone"] = effects_by_zone
    document["model_configs"]["staff_dynamics"] = {
        "shift_windows_by_staff": {
            "S01": [{"start_minute": 0, "end_minute": 2}],
            "S02": [{"start_minute": 2, "end_minute": 16}],
        }
    }
    document["model_configs"]["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    if body_policy_enabled:
        from sheltersim.domain.body_state import canonical_body_config

        document["model_configs"]["body_dynamics"] = canonical_body_config(None)
        document["model_configs"]["policy_registry"] = {
            "schema_version": 2,
            "psychology": "life-state-feedback-v1",
        }
    if staff_policy_id is not None or fatigue_policy_id is not None:
        selections = {}
        if staff_policy_id is not None:
            selections["staff_operations"] = staff_policy_id
        if fatigue_policy_id is not None:
            selections["staff_fatigue"] = fatigue_policy_id
        document["model_configs"].setdefault("policy_registry", {"schema_version": 2}).update(
            selections
        )
    if fatigue_policy_id is not None:
        document["model_configs"]["staff_dynamics"]["max_continuous_work_minutes"] = 4
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    initialize_simulator_extensions(world)
    return world


def test_zone_specific_staff_threshold_controls_task_assignment() -> None:
    baseline = _relief_world(previous_zone_id="sleeping", staff_policy_id="staff-urgency-first-v1")
    overridden = _relief_world(
        previous_zone_id="sleeping",
        staff_policy_id="staff-urgency-first-v1",
        thresholds_by_zone={
            "sleeping": {"cleaning_sanitation_below": 0.75},
        },
    )
    for world in (baseline, overridden):
        world.staff["S01"].skills = ("cleaning",)
        for facility in world.facilities.values():
            facility.working_units = facility.capacity
        world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["sleeping"]["sanitation"] = 0.8

    baseline_router = SpaceRouter(ZoneGraph.from_states(baseline.zones, baseline.zone_edges))
    overridden_router = SpaceRouter(ZoneGraph.from_states(overridden.zones, overridden.zone_edges))
    baseline_tasks = assign_staff_operations(baseline, 0, baseline_router)
    overridden_tasks = assign_staff_operations(overridden, 0, overridden_router)

    assert len(baseline_tasks) == 1
    baseline_record = baseline.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
    assert baseline_record["task_kind"] == "cleaning"
    assert baseline_record["reason"] == "sanitation_index_below_0.85"
    assert overridden_tasks == ()


@pytest.mark.parametrize(
    ("previous_zone_id", "expect_arrival_suppressed"),
    [("toilet", False), ("sleeping", True)],
)
def test_shift_relief_preserves_task_and_records_completion_across_checkpoint(
    previous_zone_id: str, expect_arrival_suppressed: bool
) -> None:
    world = _relief_world(previous_zone_id)
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))

    task_ids = assign_staff_operations(world, 0, router)

    assert len(task_ids) == 1
    task_id = task_ids[0]
    task = world.active_tasks[task_id]
    assert task.staff_id == "S01"
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]["phase"] == (
        "travel" if expect_arrival_suppressed else "working"
    )

    world.minute_index = 1
    relief_ids = schedule_staff_operation_relief(world, 1)
    assert len(relief_ids) == 1

    world.minute_index = 2
    dispatcher = EventDispatcher(
        {
            "staff_operation_relief": StaffOperationReliefHandler(),
            "staff_operation_complete": StaffOperationCompletionHandler(),
        }
    )
    outcomes = dispatcher.dispatch_due(world, 2)
    ingest_applied_shared_operation_records(world, outcomes)

    assert world.active_tasks[task_id].staff_id == "S02"
    assert world.active_tasks[task_id].start_minute == 2
    assert world.active_tasks[task_id].due_minute == 6
    assert world.staff["S01"].active_task_id is None
    assert world.staff["S02"].active_task_id == task_id
    assert any(
        row.event.event_type == "staff_operation_complete"
        and row.event.payload["staff_id"] == "S01"
        and row.status.value == "suppressed"
        for row in world.event_log.records
    )
    assert (
        any(
            row.event.event_type == "staff_operation_arrive"
            and row.event.payload["staff_id"] == "S01"
            and row.status.value == "suppressed"
            for row in world.event_log.records
        )
        is expect_arrival_suppressed
    )
    assert [
        row["status"]
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["event_type"] == "staff_handoff" and row["task_id"] == task_id
    ] == ["reassigned"]
    world.validate()

    world = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    world.minute_index = 6
    outcomes = dispatcher.dispatch_due(world, 6)
    ingest_applied_shared_operation_records(world, outcomes)

    assert world.facilities["toilet_1"].working_units == 1
    completed = world.model_state[STAFF_OPERATIONS_STATE_KEY]["history"][-1]
    assert completed["task_id"] == task_id
    assert completed["staff_id"] == "S02"
    assert completed["status"] == "completed"
    assert world.active_tasks == {}
    assert [
        row["status"]
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["event_type"] == "facility_recovery_transition"
    ] == ["restored"]
    staff_receipt = next(
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["event_type"] == "staff_operation_transition" and row["task_id"] == task_id
    )
    assert staff_receipt["status"] == "completed"
    assert staff_receipt["staff_id"] == "S02"
    assert staff_receipt["state_changes"]
    assert staff_receipt["state_changes"][0]["before"] != staff_receipt["state_changes"][0]["after"]
    world.validate()


def test_staff_factory_changes_routed_task_priority() -> None:
    selected_tasks: dict[str, tuple[str, str | None]] = {}
    for policy_id in ("staff-urgency-first-v1", "staff-travel-first-v1"):
        world = _relief_world("sleeping", staff_policy_id=policy_id)
        world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["sleeping"]["sanitation"] = 0.4
        router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))

        task_ids = assign_staff_operations(world, 0, router)

        operation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
        assert task_ids
        completion = next(
            event
            for event in world.event_queue.pending()
            if event.event_type == "staff_operation_complete" and event.payload["staff_id"] == "S01"
        )
        assert completion.provenance["policy_version"] == policy_id
        selected_tasks[policy_id] = (operation["task_kind"], operation["target_id"])

    assert selected_tasks["staff-urgency-first-v1"] == ("maintenance", "toilet_1")
    assert selected_tasks["staff-travel-first-v1"] == ("cleaning", None)


def test_staff_fatigue_factory_changes_live_duty_state() -> None:
    fatigue_values: dict[str, float] = {}
    for policy_id in ("staff-fatigue-linear-v1", "staff-fatigue-saturating-v1"):
        world = _relief_world("toilet", fatigue_policy_id=policy_id)
        router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
        assign_staff_operations(world, 0, router)

        for minute in range(3):
            update_staff_dynamics(world, minute)
        fatigue_values[policy_id] = world.model_state[STAFF_DYNAMICS_STATE_KEY]["staff"]["S01"][
            "fatigue"
        ]

    assert fatigue_values["staff-fatigue-linear-v1"] == pytest.approx(0.75)
    assert fatigue_values["staff-fatigue-saturating-v1"] == pytest.approx(1.0 - math.exp(-0.75))
    assert (
        fatigue_values["staff-fatigue-linear-v1"] != fatigue_values["staff-fatigue-saturating-v1"]
    )


def test_staff_operation_completion_has_typed_state_change_and_indexed_receipt(
    tmp_path: Path,
) -> None:
    world = _relief_world("toilet")
    world.facilities["toilet_1"].working_units = world.facilities["toilet_1"].capacity
    world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]["sanitation"] = 0.4
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_id = assign_staff_operations(world, 0, router)[0]
    operation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
    assert operation["task_kind"] == "cleaning"
    event = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete" and event.payload["task_id"] == task_id
    )

    world.minute_index = event.due_minute
    outcomes = EventDispatcher(
        {"staff_operation_complete": StaffOperationCompletionHandler()}
    ).dispatch_due(world, world.minute_index)
    ingest_applied_shared_operation_records(world, outcomes)

    typed_event = next(
        row for row in outcomes if row.event.event_type == "staff_operation_transition"
    )
    assert typed_event.status.value == "applied"
    assert typed_event.event.payload["task_kind"] == "cleaning"
    assert typed_event.event.payload["state_changes"][0]["entity_id"] == "toilet"
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]["sanitation"] == 0.55
    world.validate()

    checkpoint = Checkpoint.capture(world)
    restored = Checkpoint.from_json(checkpoint.to_json()).restore_world()
    restored.validate()
    store = ProjectStore(tmp_path / "staff-operation-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    index = HistoryIndex(store, "project", "run")
    index.ingest("main", checkpoint, rebuild=True)
    page = index.search(
        "main",
        HistoryQuery(
            kind="operations",
            start=event.due_minute,
            end=event.due_minute,
            staff_id="S01",
            event_id=typed_event.event.event_id,
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["task_kind"] == "cleaning"
    assert page["items"][0]["state_changes"][0]["field_path"] == (
        "staff_operations_v1.zones.sanitation"
    )


def test_zone_specific_staff_effect_changes_applied_cleaning_outcome() -> None:
    world = _relief_world(
        "toilet",
        effects_by_zone={"toilet": {"cleaning_sanitation_recovery": 0.3}},
    )
    world.facilities["toilet_1"].working_units = world.facilities["toilet_1"].capacity
    world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]["sanitation"] = 0.4
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_id = assign_staff_operations(world, 0, router)[0]
    event = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete" and event.payload["task_id"] == task_id
    )

    world.minute_index = event.due_minute
    outcomes = EventDispatcher(
        {"staff_operation_complete": StaffOperationCompletionHandler()}
    ).dispatch_due(world, world.minute_index)
    ingest_applied_shared_operation_records(world, outcomes)

    receipt = next(
        row.event.payload
        for row in outcomes
        if row.event.event_type == "staff_operation_transition"
    )
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"][
        "sanitation"
    ] == pytest.approx(0.7)
    assert receipt["state_changes"][0]["before"] == pytest.approx(0.4)
    assert receipt["state_changes"][0]["after"] == pytest.approx(0.7)
    world.validate()


def test_zone_specific_sanitation_consumption_uses_override() -> None:
    world = _relief_world(
        "sleeping",
        effects_by_zone={
            "sleeping": {"sanitation_use_per_person_minute": 0.001},
        },
    )
    sanitation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["sleeping"]
    occupancy = sum(agent.zone_id == "sleeping" for agent in world.agents.values())

    update_staff_operations(world, 5)

    assert sanitation["sanitation"] == pytest.approx(1.0 - occupancy * 0.001 * 5)
    world.validate()


@pytest.mark.parametrize(
    ("task_kind", "staff_skill", "coefficient", "initial_field", "expected_delta"),
    [
        (
            "information_outreach",
            "information",
            "outreach_uncertainty_relief",
            "uncertainty",
            -0.2,
        ),
        ("patrol", "patrol", "patrol_reassurance", "reassurance", 0.15),
    ],
)
def test_zone_specific_support_task_response_uses_override(
    task_kind: str,
    staff_skill: str,
    coefficient: str,
    initial_field: str,
    expected_delta: float,
) -> None:
    amount = 0.2 if task_kind == "information_outreach" else 0.3
    world = _relief_world(
        "sleeping",
        effects_by_zone={"sleeping": {coefficient: amount}},
        body_policy_enabled=True,
    )
    world.staff["S01"].skills = ("support", staff_skill)
    body_agents = world.model_state["body_state_v1"]["agents"]
    for agent in world.agents.values():
        body_agents[agent.id]["uncertainty"] = 0.9 if task_kind == "information_outreach" else 0.0
        agent.stress = 0.9 if task_kind == "patrol" else 0.0
    person = world.agents["A001"]
    before = body_agents[person.id][initial_field]
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_id = assign_staff_operations(world, 0, router)[0]
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]["task_kind"] == task_kind
    event = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete" and event.payload["task_id"] == task_id
    )

    world.minute_index = event.due_minute
    outcomes = EventDispatcher(
        {"staff_operation_complete": StaffOperationCompletionHandler()}
    ).dispatch_due(world, world.minute_index)
    ingest_applied_shared_operation_records(world, outcomes)

    assert body_agents[person.id][initial_field] - before == pytest.approx(expected_delta)
    world.validate()


def test_task_kind_duration_override_changes_assigned_completion_time() -> None:
    world = _relief_world("toilet", task_duration_minutes_by_kind={"cleaning": 9})
    world.facilities["toilet_1"].working_units = world.facilities["toilet_1"].capacity
    world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]["sanitation"] = 0.4
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))

    task_ids = assign_staff_operations(world, 0, router)

    assert task_ids
    operation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
    completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete"
        and event.payload["task_id"] == task_ids[0]
    )
    assert operation["task_kind"] == "cleaning"
    assert completion.due_minute == 9
    assert operation["due_minute"] == 9


def test_priority_support_preempts_routed_background_task_with_applied_evidence(
    tmp_path: Path,
) -> None:
    world = _relief_world("toilet")
    world.staff["S01"].skills = (*world.staff["S01"].skills, "support")
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_ids = assign_staff_operations(world, 0, router)
    assert len(task_ids) == 1
    background_task_id = task_ids[0]
    old_completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete"
        and event.payload["task_id"] == background_task_id
    )

    service = StaffService(world, task_duration_minutes=3, unresolved_after_minutes=10)
    request = service.request("A001")
    world.model_state.setdefault("llm", {}).setdefault("support_priorities", {})[
        request.request_id
    ] = 1

    assert service.assign_pending(0) == (request.request_id,)
    support = world.support_requests[request.request_id]
    assert support.status.value == "assigned"
    assert support.assigned_staff_id == "S01"
    assert support.task_id in world.active_tasks
    assert background_task_id not in world.active_tasks
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]["phase"] == "idle"
    interruption = next(
        row
        for row in world.event_log.records
        if row.event.event_type == "staff_operation_preempted"
    )
    assert interruption.status.value == "applied"
    assert interruption.event.payload["request_id"] == request.request_id
    assert any(
        row.event.event_id == old_completion.event_id and row.status.value == "suppressed"
        for row in world.event_log.records
    )
    history = world.model_state[STAFF_OPERATIONS_STATE_KEY]["history"][-1]
    assert history["status"] == "interrupted"
    assert interruption.event.event_id in history["causation_event_ids"]
    world.validate()
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    checkpoint = Checkpoint.capture(world)
    store = ProjectStore(tmp_path / "staff-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    index = HistoryIndex(store, "project", "run")
    index.ingest("main", checkpoint, rebuild=True)
    page = index.search(
        "main",
        HistoryQuery(
            kind="operations",
            start=0,
            end=0,
            staff_id="S01",
            event_id=interruption.event.event_id,
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["record_type"] == "staff_operation_preemption"
    assert page["items"][0]["request_id"] == request.request_id


def test_normal_priority_support_does_not_preempt_routed_background_task() -> None:
    world = _relief_world("toilet")
    world.staff["S01"].skills = (*world.staff["S01"].skills, "support")
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    background_task_id = assign_staff_operations(world, 0, router)[0]
    service = StaffService(world, task_duration_minutes=3, unresolved_after_minutes=10)
    request = service.request("A001")

    assert service.assign_pending(0) == ()
    assert world.support_requests[request.request_id].status.value == "pending"
    assert world.active_tasks[background_task_id].kind == "staff_operation"
    assert not any(
        row.event.event_type == "staff_operation_preempted" for row in world.event_log.records
    )
