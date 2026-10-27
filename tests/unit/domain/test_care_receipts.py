"""Care receipts follow typed support from request to completion and restore.

The day-long run on a generated Scenario is checked in
tests/integration/test_care_lifecycle.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.contracts.population import PersonContext
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.body_state import canonical_body_config, create_body_state
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_history_for_agent,
    care_source_ids_for_agent,
    ingest_applied_shared_operation_records,
    support_kind_for_agent,
)
from sheltersim.domain.models import StaffState
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import EventDispatcher, StaffSupportCompletionHandler
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import person_projection, world_frame
from sheltersim.region.population import PopulationConditionInput

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenarios" / "h00_small_replay.json"


def test_operator_care_need_rates_flow_through_preview_context_into_runtime(
    generated_scenario_with,
) -> None:
    scenario = generated_scenario_with(
        evacuees=4,
        evacuation_selection={"age_band_multipliers": {"child": 0, "adult": 1, "older_adult": 0}},
        population_conditions={
            "care_support_need_rates": {
                "hygiene_support": 1.0,
                "privacy_accommodation": 0.0,
                "dependent_assistance": 1.0,
            }
        },
    )

    population = scenario.model_configs["population_context"]
    assert len(population["people"]) == 4
    assert all(
        person["care_support_needs"] == ["dependent_assistance", "hygiene_support"]
        for person in population["people"].values()
    )
    assert all(
        "SRC-OPERATOR-POPULATION-CONDITIONS" in person["source_ids"]
        for person in population["people"].values()
    )

    world = WorldState.from_scenario(scenario)
    assert support_kind_for_agent(world, "A001") == "dependent_assistance"


@pytest.mark.parametrize(
    "rates",
    [
        {"hygiene_support": -0.01},
        {"hygiene_support": 1.01},
        {"hygiene_support": float("nan")},
        {"medical_assessment": 0.1},
    ],
)
def test_operator_care_need_rates_reject_invalid_probability_or_kind(
    rates: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        PopulationConditionInput(care_support_need_rates=rates)


def make_world() -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    world.model_configs["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    world.model_configs["population_context"] = {
        "people": {
            "A001": {
                "medical_support": True,
                "medication_support": True,
                "care_support_needs": ["medication_support", "medical_assessment"],
                "source_ids": ["ASM-TEST-CARE-NEED-V1"],
            }
        }
    }
    world.staff["NURSE-1"] = StaffState(
        id="NURSE-1", zone_id=world.agents["A001"].zone_id, skills=("nursing",)
    )
    initialize_simulator_extensions(world)
    return world


def test_scenario_medication_need_selects_typed_care_and_skill_matched_staff() -> None:
    typed_context = PersonContext.model_validate(
        {
            "medical_support": True,
            "care_support_needs": ["medication_support", "medical_assessment"],
            "source_ids": ["ASM-TEST-CARE-NEED-V1"],
        }
    )
    assert typed_context.care_support_needs == ("medication_support", "medical_assessment")
    world = make_world()
    assert support_kind_for_agent(world, "A001") == "medication_support"
    source_ids = care_source_ids_for_agent(world, "A001")
    assert "ASM-TEST-CARE-NEED-V1" in source_ids

    service = StaffService(world, task_duration_minutes=4, unresolved_after_minutes=20)
    request = service.request("A001", validate_state=False)
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind=support_kind_for_agent(world, "A001"),
        status="requested",
        source_ids=source_ids,
    )
    assert service.assign_pending(0, validate_state=False) == (request.request_id,)
    assigned = world.support_requests[request.request_id]
    assert assigned.assigned_staff_id == "NURSE-1"
    assert all(
        staff.active_task_id is None
        for staff_id, staff in world.staff.items()
        if staff_id != "NURSE-1"
    )
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="medication_support",
        status="assigned",
        staff_id="NURSE-1",
        causation_id=assigned.task_id,
        source_ids=source_ids,
    )
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="medication_support",
        status="started",
        staff_id="NURSE-1",
        causation_id=assigned.task_id,
        source_ids=source_ids,
    )
    world.validate()

    state = world.model_state["shared_operations_v1"]
    assert state["support_requests"][request.request_id]["support_kind"] == "medication_support"
    frame = world_frame(world, run_id="run", branch_id="branch", computed_until=0)
    patient = next(row for row in frame["people"] if row["id"] == "A001")
    assert patient["care_requests"][0]["support_kind"] == "medication_support"
    assert patient["care_requests"][0]["assigned_staff_id"] == "NURSE-1"
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    world.minute_index = 4
    outcomes = EventDispatcher(
        {"staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6)}
    ).dispatch_due(world, 4)
    ingest_applied_shared_operation_records(world, outcomes)
    completed = [
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["request_id"] == request.request_id
    ]
    assert [row["status"] for row in completed] == ["requested", "assigned", "started", "completed"]
    assert completed[-1]["support_kind"] == "medication_support"
    assert completed[-1]["outcome_observation"] == "not_measured"
    assert request.request_id not in world.model_state["shared_operations_v1"]["support_requests"]
    assert support_kind_for_agent(world, "A001") == "medical_assessment"
    patient_frame = next(
        row
        for row in world_frame(world, run_id="run", branch_id="branch", computed_until=4)["people"]
        if row["id"] == "A001"
    )
    assert [row["status"] for row in patient_frame["care_history"]] == [
        "requested",
        "assigned",
        "started",
        "completed",
    ]
    assert patient_frame["care_history"][-1]["outcome_observation"] == "not_measured"
    selected = person_projection(world, "A001")["person"]
    assert selected is not None
    assert selected["care_requests"] == []
    assert selected["care_history"] == patient_frame["care_history"]
    world.validate()


def test_completed_hygiene_support_applies_bounded_experimental_need_relief() -> None:
    world = make_world()
    context = world.model_configs["population_context"]["people"]["A001"]
    context["care_support_needs"] = ["hygiene_support"]
    from sheltersim.domain.body_state import canonical_body_config

    world.model_configs["body_dynamics"] = canonical_body_config(None)
    world.model_configs["body_dynamics"]["hygiene_support_relief_by_agent"] = {
        "A001": 0.3,
    }
    world.model_configs["policy_registry"] = {
        "schema_version": 2,
        "psychology": "life-state-feedback-v1",
        "body_dynamics": "body-feedback-complete-v1",
        "care_outcome": "hygiene-support-proportional-relief-v1",
    }
    initialize_simulator_extensions(world)
    body = world.model_state["body_state_v1"]["agents"]["A001"]
    body["hygiene_need"] = 0.5
    world.validate()

    legacy = WorldState.from_dict(world.to_dict())
    legacy.model_configs["body_dynamics"].pop("hygiene_support_relief")
    legacy.model_state["body_state_v1"]["config"].pop("hygiene_support_relief")
    legacy.validate()
    restored_legacy = Checkpoint.from_json(Checkpoint.capture(legacy).to_json()).restore_world()
    assert restored_legacy.canonical_json() == legacy.canonical_json()

    service = StaffService(world, task_duration_minutes=4, unresolved_after_minutes=20)
    request = service.request("A001", validate_state=False)
    source_ids = care_source_ids_for_agent(world, "A001")
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="hygiene_support",
        status="requested",
        source_ids=source_ids,
    )
    assert service.assign_pending(0, validate_state=False) == (request.request_id,)
    assigned = world.support_requests[request.request_id]
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="hygiene_support",
        status="assigned",
        staff_id="NURSE-1",
        causation_id=assigned.task_id,
        source_ids=source_ids,
    )
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="hygiene_support",
        status="started",
        staff_id="NURSE-1",
        causation_id=assigned.task_id,
        source_ids=source_ids,
    )

    world.minute_index = 4
    outcomes = EventDispatcher(
        {"staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6)}
    ).dispatch_due(world, 4)
    ingest_applied_shared_operation_records(world, outcomes)

    expected = 0.5 * (
        1.0
        - world.model_state["body_state_v1"]["config"]["hygiene_support_relief_by_agent"]["A001"]
    )
    assert body["hygiene_need"] == expected
    completed = next(
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["request_id"] == request.request_id and row["status"] == "completed"
    )
    assert completed["outcome_observation"] == "observed_change"
    assert completed["state_changes"] == [
        {
            "entity_kind": "model_state",
            "entity_id": "A001",
            "field_path": "body_state_v1.agents.hygiene_need",
            "before": 0.5,
            "after": expected,
            "unit": "normalized_0_1",
        }
    ]
    assert "ASM-LIFE-STATE-FEEDBACK-V1" in completed["source_ids"]
    assert "ASM-CARE-HYGIENE-PROPORTIONAL-RELIEF-V1" in completed["source_ids"]
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()


def test_completed_emotional_support_uses_resident_specific_experimental_gain() -> None:
    world = make_world()
    world.model_configs["population_context"]["people"]["A001"]["care_support_needs"] = [
        "emotional_support"
    ]
    from sheltersim.domain.body_state import canonical_body_config

    config = canonical_body_config(None)
    config["complaint_reassurance_gain_by_agent"] = {"A001": 0.04}
    world.model_configs["body_dynamics"] = config
    world.model_configs["policy_registry"] = {
        "schema_version": 2,
        "psychology": "life-state-feedback-v1",
        "body_dynamics": "body-feedback-complete-v1",
    }
    initialize_simulator_extensions(world)
    agent = world.agents["A001"]
    agent.stress = 0.5
    agent.trust = 0.3
    service = StaffService(world, task_duration_minutes=4, unresolved_after_minutes=20)
    request = service.request("A001", validate_state=False)
    source_ids = care_source_ids_for_agent(world, "A001")
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="emotional_support",
        status="requested",
        source_ids=source_ids,
    )
    assert service.assign_pending(0, validate_state=False) == (request.request_id,)
    assigned = world.support_requests[request.request_id]
    for status in ("assigned", "started"):
        append_applied_care_transition(
            world,
            request_id=request.request_id,
            agent_id="A001",
            household_id=None,
            support_kind="emotional_support",
            status=status,
            staff_id="NURSE-1",
            causation_id=assigned.task_id,
            source_ids=source_ids,
        )

    world.minute_index = 4
    outcomes = EventDispatcher(
        {"staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6)}
    ).dispatch_due(world, 4)
    ingest_applied_shared_operation_records(world, outcomes)

    assert agent.stress == 0.46
    assert agent.trust == 0.31
    body = world.model_state["body_state_v1"]["agents"]["A001"]
    assert body["reassurance"] == 0.04
    completed = next(
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["request_id"] == request.request_id and row["status"] == "completed"
    )
    assert completed["outcome_observation"] == "observed_change"
    assert Checkpoint.from_json(
        Checkpoint.capture(world).to_json()
    ).restore_world().canonical_json() == (world.canonical_json())


@pytest.mark.parametrize(
    ("support_kind", "expected_changes"),
    [
        (
            "caregiver_assistance",
            {
                "stress": (0.5, 0.47),
                "body_state_v1.agents.reassurance": (0.1, 0.12),
            },
        ),
        (
            "dependent_assistance",
            {
                "stress": (0.5, 0.46),
                "body_state_v1.agents.reassurance": (0.1, 0.13),
            },
        ),
        ("privacy_accommodation", {"body_state_v1.agents.discontent": (0.5, 0.48)}),
        ("mobility_assistance", {"stress": (0.5, 0.47)}),
        ("language_access", {"body_state_v1.agents.uncertainty": (0.5, 0.45)}),
        ("medical_assessment", {"body_state_v1.agents.uncertainty": (0.5, 0.48)}),
        ("medication_support", {"body_state_v1.agents.uncertainty": (0.5, 0.49)}),
    ],
)
def test_typed_care_support_records_and_restores_experimental_state_response(
    support_kind: str,
    expected_changes: dict[str, tuple[float, float]],
    tmp_path: Path,
) -> None:
    world = make_world()
    world.model_configs["population_context"]["people"]["A001"]["care_support_needs"] = [
        support_kind
    ]
    world.model_configs["body_dynamics"] = canonical_body_config(None)
    world.model_configs["policy_registry"] = {
        "schema_version": 2,
        "psychology": "life-state-feedback-v1",
        "body_dynamics": "body-feedback-complete-v1",
        "care_outcome": "typed-care-support-response-v1",
    }
    initialize_simulator_extensions(world)
    world.model_state["body_state_v1"] = create_body_state(world)
    world.staff["NURSE-1"].skills = (
        "nursing",
        "caregiver",
        "support",
        "privacy",
        "mobility",
        "interpreter",
        "language_access",
        "medical",
        "medication",
    )
    agent = world.agents["A001"]
    agent.stress = 0.5
    body = world.model_state["body_state_v1"]["agents"]["A001"]
    body["reassurance"] = 0.1
    body["discontent"] = 0.5
    body["uncertainty"] = 0.5

    service = StaffService(world, task_duration_minutes=4, unresolved_after_minutes=20)
    request = service.request("A001", validate_state=False)
    source_ids = care_source_ids_for_agent(world, "A001")
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id=None,
        support_kind=support_kind,
        status="requested",
        source_ids=source_ids,
    )
    assert service.assign_pending(0, validate_state=False) == (request.request_id,)
    assigned = world.support_requests[request.request_id]
    for status in ("assigned", "started"):
        append_applied_care_transition(
            world,
            request_id=request.request_id,
            agent_id="A001",
            household_id=None,
            support_kind=support_kind,
            status=status,
            staff_id="NURSE-1",
            causation_id=assigned.task_id,
            source_ids=source_ids,
        )

    world.minute_index = 4
    outcomes = EventDispatcher(
        {"staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6)}
    ).dispatch_due(world, 4)
    ingest_applied_shared_operation_records(world, outcomes)
    completed = next(
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["request_id"] == request.request_id and row["status"] == "completed"
    )

    assert completed["outcome_observation"] == "observed_change"
    assert "ASM-CARE-TYPED-SUPPORT-RESPONSE-V1" in completed["source_ids"]
    actual_changes = {
        row["field_path"]: (row["before"], row["after"]) for row in completed["state_changes"]
    }
    assert actual_changes.keys() == expected_changes.keys()
    for field_path, expected in expected_changes.items():
        assert actual_changes[field_path] == pytest.approx(expected)
    checkpoint = Checkpoint.capture(world)
    restored = Checkpoint.from_json(checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    from sheltersim.contracts.workspace import HistoryQuery
    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.storage.projects import ProjectStore

    store = ProjectStore(tmp_path / "typed-care-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    history = HistoryIndex(store, "project", "run")
    history.ingest("main", checkpoint, rebuild=True)
    page = history.search(
        "main",
        HistoryQuery(
            kind="care",
            start=0,
            end=4,
            agent_id="A001",
            event_id=completed["event_id"],
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["support_kind"] == support_kind
    assert page["items"][0]["state_changes"] == completed["state_changes"]


def test_caregiver_support_updates_only_explicit_reviewed_dependents(tmp_path: Path) -> None:
    world = make_world()
    world.model_configs["population_context"]["people"].update(
        {
            "A001": {
                "household_id": "G001",
                "care_for_ids": ["A002", "A004"],
                "care_support_needs": ["caregiver_assistance"],
                "source_ids": ["SRC-TEST-EXPLICIT-CARE-LINK"],
            },
            "A002": {
                "household_id": "G001",
                "caregiver_ids": ["A001"],
                "source_ids": ["SRC-TEST-EXPLICIT-CARE-LINK"],
            },
            "A003": {
                "household_id": "G001",
                "source_ids": ["SRC-TEST-EXPLICIT-CARE-LINK"],
            },
            "A004": {
                "household_id": "G001",
                "caregiver_ids": ["A001"],
                "requires_review": True,
                "source_ids": ["SRC-TEST-EXPLICIT-CARE-LINK"],
            },
        }
    )
    world.model_configs["body_dynamics"] = canonical_body_config(None)
    world.model_configs["policy_registry"] = {
        "schema_version": 2,
        "psychology": "life-state-feedback-v1",
        "body_dynamics": "body-feedback-complete-v1",
        "care_outcome": "typed-care-support-response-v1",
    }
    initialize_simulator_extensions(world)
    world.model_state["body_state_v1"] = create_body_state(world)
    world.staff["NURSE-1"].skills = ("caregiver", "support")
    world.agents["A001"].stress = 0.5
    world.agents["A002"].stress = 0.6
    world.agents["A003"].stress = 0.7
    world.agents["A004"].stress = 0.8

    service = StaffService(world, task_duration_minutes=4, unresolved_after_minutes=20)
    request = service.request("A001", validate_state=False)
    append_applied_care_transition(
        world,
        request_id=request.request_id,
        agent_id="A001",
        household_id="G001",
        support_kind="caregiver_assistance",
        status="requested",
        source_ids=care_source_ids_for_agent(world, "A001"),
    )
    assert service.assign_pending(0, validate_state=False) == (request.request_id,)
    assigned = world.support_requests[request.request_id]
    for status in ("assigned", "started"):
        append_applied_care_transition(
            world,
            request_id=request.request_id,
            agent_id="A001",
            household_id="G001",
            support_kind="caregiver_assistance",
            status=status,
            staff_id="NURSE-1",
            causation_id=assigned.task_id,
        )

    world.minute_index = 4
    outcomes = EventDispatcher(
        {"staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6)}
    ).dispatch_due(world, 4)
    ingest_applied_shared_operation_records(world, outcomes)
    completed = next(
        row
        for row in world.model_state["shared_operations_v1"]["history"]
        if row["request_id"] == request.request_id and row["status"] == "completed"
    )

    assert world.agents["A001"].stress == pytest.approx(0.47)
    assert world.agents["A002"].stress == pytest.approx(0.57)
    assert world.agents["A003"].stress == pytest.approx(0.7)
    assert world.agents["A004"].stress == pytest.approx(0.8)
    assert world.model_state["body_state_v1"]["agents"]["A002"]["reassurance"] == pytest.approx(
        0.02
    )
    assert world.model_state["body_state_v1"]["agents"]["A003"]["reassurance"] == 0.0
    assert world.model_state["body_state_v1"]["agents"]["A004"]["reassurance"] == 0.0
    changed_people = {row["entity_id"] for row in completed["state_changes"]}
    assert changed_people == {"A001", "A002"}
    assert "SRC-TEST-EXPLICIT-CARE-LINK" in completed["source_ids"]
    dependent_history = care_history_for_agent(world, "A002")
    assert len(dependent_history) == 1
    assert dependent_history[0]["person_role_in_record"] == "explicit_related_state_change"
    frame = world_frame(world, run_id="run", branch_id="main", computed_until=4)
    dependent_person = next(person for person in frame["people"] if person["id"] == "A002")
    assert dependent_person["care_history"] == dependent_history

    from sheltersim.contracts.workspace import HistoryQuery
    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.storage.projects import ProjectStore

    checkpoint = Checkpoint.capture(world)
    store = ProjectStore(tmp_path / "relational-care-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    history = HistoryIndex(store, "project", "run")
    history.ingest("main", checkpoint, rebuild=True)
    page = history.search(
        "main",
        HistoryQuery(kind="care", start=0, end=4, agent_id="A002"),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["event_id"] == completed["event_id"]
