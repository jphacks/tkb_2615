"""Same-site task relief at staff shift boundaries."""

from __future__ import annotations

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
from sheltersim.events.handlers import (
    EventDispatcher,
    StaffOperationCompletionHandler,
    StaffOperationReliefHandler,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.space import SpaceRouter
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def make_world(
    previous_zone_id: str = "toilet",
    *,
    staff_policy_id: str | None = None,
    fatigue_policy_id: str | None = None,
    thresholds_by_zone: dict[str, dict[str, float]] | None = None,
    effects_by_zone: dict[str, dict[str, float]] | None = None,
    task_duration_minutes_by_kind: dict[str, int] | None = None,
    body_policy_enabled: bool = False,
) -> WorldState:
    document = ScenarioSpec.load(FIXTURE).model_dump(mode="json")
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
        document["model_configs"]["staff_operations"]["thresholds_by_zone"] = (
            thresholds_by_zone
        )
    if effects_by_zone is not None:
        document["model_configs"]["staff_operations"]["effects_by_zone"] = (
            effects_by_zone
        )
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
        document["model_configs"].setdefault(
            "policy_registry", {"schema_version": 2}
        ).update(selections)
    if fatigue_policy_id is not None:
        document["model_configs"]["staff_dynamics"]["max_continuous_work_minutes"] = 4
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    initialize_simulator_extensions(world)
    return world


def test_zone_specific_staff_threshold_controls_task_assignment() -> None:
    baseline = make_world(
        previous_zone_id="sleeping", staff_policy_id="staff-urgency-first-v1"
    )
    overridden = make_world(
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
        world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["sleeping"][
            "sanitation"
        ] = 0.8

    baseline_router = SpaceRouter(ZoneGraph.from_states(baseline.zones, baseline.zone_edges))
    overridden_router = SpaceRouter(
        ZoneGraph.from_states(overridden.zones, overridden.zone_edges)
    )
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
    world = make_world(previous_zone_id)
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
    assert any(
        row.event.event_type == "staff_operation_arrive"
        and row.event.payload["staff_id"] == "S01"
        and row.status.value == "suppressed"
        for row in world.event_log.records
    ) is expect_arrival_suppressed
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
        row for row in world.model_state["shared_operations_v1"]["history"]
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
        world = make_world("sleeping", staff_policy_id=policy_id)
        world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["sleeping"]["sanitation"] = 0.4
        router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))

        task_ids = assign_staff_operations(world, 0, router)

        operation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
        assert task_ids
        completion = next(
            event
            for event in world.event_queue.pending()
            if event.event_type == "staff_operation_complete"
            and event.payload["staff_id"] == "S01"
        )
        assert completion.provenance["policy_version"] == policy_id
        selected_tasks[policy_id] = (operation["task_kind"], operation["target_id"])

    assert selected_tasks["staff-urgency-first-v1"] == ("maintenance", "toilet_1")
    assert selected_tasks["staff-travel-first-v1"] == ("cleaning", None)


def test_staff_fatigue_factory_changes_live_duty_state() -> None:
    fatigue_values: dict[str, float] = {}
    for policy_id in ("staff-fatigue-linear-v1", "staff-fatigue-saturating-v1"):
        world = make_world("toilet", fatigue_policy_id=policy_id)
        router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
        assign_staff_operations(world, 0, router)

        for minute in range(3):
            update_staff_dynamics(world, minute)
        fatigue_values[policy_id] = world.model_state[STAFF_DYNAMICS_STATE_KEY]["staff"][
            "S01"
        ]["fatigue"]

    assert fatigue_values["staff-fatigue-linear-v1"] == pytest.approx(0.75)
    assert fatigue_values["staff-fatigue-saturating-v1"] == pytest.approx(
        1.0 - math.exp(-0.75)
    )
    assert fatigue_values["staff-fatigue-linear-v1"] != fatigue_values[
        "staff-fatigue-saturating-v1"
    ]


def test_staff_operation_completion_has_typed_state_change_and_indexed_receipt(
    tmp_path: Path,
) -> None:
    world = make_world("toilet")
    world.facilities["toilet_1"].working_units = world.facilities["toilet_1"].capacity
    world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]["sanitation"] = 0.4
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_id = assign_staff_operations(world, 0, router)[0]
    operation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
    assert operation["task_kind"] == "cleaning"
    event = next(
        event for event in world.event_queue.pending()
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
            kind="operations", start=event.due_minute, end=event.due_minute,
            staff_id="S01", event_id=typed_event.event.event_id,
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["task_kind"] == "cleaning"
    assert page["items"][0]["state_changes"][0]["field_path"] == (
        "staff_operations_v1.zones.sanitation"
    )


def test_zone_specific_staff_effect_changes_applied_cleaning_outcome() -> None:
    world = make_world(
        "toilet",
        effects_by_zone={"toilet": {"cleaning_sanitation_recovery": 0.3}},
    )
    world.facilities["toilet_1"].working_units = world.facilities["toilet_1"].capacity
    world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"][
        "sanitation"
    ] = 0.4
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_id = assign_staff_operations(world, 0, router)[0]
    event = next(
        event for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete" and event.payload["task_id"] == task_id
    )

    world.minute_index = event.due_minute
    outcomes = EventDispatcher(
        {"staff_operation_complete": StaffOperationCompletionHandler()}
    ).dispatch_due(world, world.minute_index)
    ingest_applied_shared_operation_records(world, outcomes)

    receipt = next(
        row.event.payload for row in outcomes
        if row.event.event_type == "staff_operation_transition"
    )
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"][
        "sanitation"
    ] == pytest.approx(0.7)
    assert receipt["state_changes"][0]["before"] == pytest.approx(0.4)
    assert receipt["state_changes"][0]["after"] == pytest.approx(0.7)
    world.validate()


def test_zone_specific_sanitation_consumption_uses_override() -> None:
    world = make_world(
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
    world = make_world(
        "sleeping",
        effects_by_zone={"sleeping": {coefficient: amount}},
        body_policy_enabled=True,
    )
    world.staff["S01"].skills = ("support", staff_skill)
    body_agents = world.model_state["body_state_v1"]["agents"]
    for agent in world.agents.values():
        body_agents[agent.id]["uncertainty"] = (
            0.9 if task_kind == "information_outreach" else 0.0
        )
        agent.stress = 0.9 if task_kind == "patrol" else 0.0
    person = world.agents["A001"]
    before = body_agents[person.id][initial_field]
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    task_id = assign_staff_operations(world, 0, router)[0]
    assert world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"][
        "task_kind"
    ] == task_kind
    event = next(
        event for event in world.event_queue.pending()
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
    world = make_world("toilet", task_duration_minutes_by_kind={"cleaning": 9})
    world.facilities["toilet_1"].working_units = world.facilities["toilet_1"].capacity
    world.model_state[STAFF_OPERATIONS_STATE_KEY]["zones"]["toilet"]["sanitation"] = 0.4
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))

    task_ids = assign_staff_operations(world, 0, router)

    assert task_ids
    operation = world.model_state[STAFF_OPERATIONS_STATE_KEY]["staff"]["S01"]
    completion = next(
        event for event in world.event_queue.pending()
        if event.event_type == "staff_operation_complete"
        and event.payload["task_id"] == task_ids[0]
    )
    assert operation["task_kind"] == "cleaning"
    assert completion.due_minute == 9
    assert operation["due_minute"] == 9


def test_priority_support_preempts_routed_background_task_with_applied_evidence(
    tmp_path: Path,
) -> None:
    world = make_world("toilet")
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
        row.event.event_id == old_completion.event_id
        and row.status.value == "suppressed"
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
            kind="operations", start=0, end=0, staff_id="S01",
            event_id=interruption.event.event_id,
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["record_type"] == "staff_operation_preemption"
    assert page["items"][0]["request_id"] == request.request_id


def test_normal_priority_support_does_not_preempt_routed_background_task() -> None:
    world = make_world("toilet")
    world.staff["S01"].skills = (*world.staff["S01"].skills, "support")
    router = SpaceRouter(ZoneGraph.from_states(world.zones, world.zone_edges))
    background_task_id = assign_staff_operations(world, 0, router)[0]
    service = StaffService(world, task_duration_minutes=3, unresolved_after_minutes=10)
    request = service.request("A001")

    assert service.assign_pending(0) == ()
    assert world.support_requests[request.request_id].status.value == "pending"
    assert world.active_tasks[background_task_id].kind == "staff_operation"
    assert not any(
        row.event.event_type == "staff_operation_preempted"
        for row in world.event_log.records
    )

