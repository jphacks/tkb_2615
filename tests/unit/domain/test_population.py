"""Arrivals, departures and their retries change who is in the shelter.

Most tests use the shared generated Scenario. The arrival approach route is an
operator input the generator never makes, so those tests keep a hand-built world.
Arrivals and departures run on a generated Scenario in
tests/integration/test_intervention_outcomes.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.paths import SMALL_REPLAY

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickPhase, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import record_action_proposal
from sheltersim.domain.care_receipts import care_source_ids_for_agent, support_kind_for_agent
from sheltersim.domain.population import admit_pending_arrivals, schedule_population_retry
from sheltersim.events.effects import (
    EffectProgram,
    EffectProgramGeometryHandler,
    EffectProgramValidationError,
    SupportMode,
    TargetSelector,
    TimedAction,
    schedule_effect_program,
)
from sheltersim.events.handlers import EffectProgramPopulationHandler, EventDispatcher
from sheltersim.events.models import EventKind, EventSuppressed, ScheduledEvent
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import person_projection, world_frame
from sheltersim.policies.behavior import ActionKind, WorldView


def _departure(retry_count: int | None) -> ScheduledEvent:
    provenance: dict[str, object] = {"effect_program_v1": {"program_id": "not-in-state"}}
    if retry_count is not None:
        provenance["population_retry"] = {"count": retry_count}
    return ScheduledEvent(
        event_id="departure",
        kind=EventKind.INTERVENTION,
        due_minute=0,
        priority=0,
        sequence=0,
        event_type="effect_program_population",
        payload={"op": "depart_evacuees"},
        provenance=provenance,
    )


def _retries(world: WorldState) -> list[ScheduledEvent]:
    return [
        event
        for event in world.event_queue.pending()
        if event.event_id.startswith("departure-retry-")
    ]


@pytest.mark.parametrize(("retry_count", "expected"), [(None, 1), (1, 2), (1438, 1439)])
def test_each_retry_counts_one_more_than_the_departure_it_retries(
    generated_scenario, retry_count, expected
) -> None:
    world = WorldState.from_scenario(generated_scenario)

    schedule_population_retry(world, _departure(retry_count))

    (retry,) = _retries(world)
    assert retry.provenance["population_retry"]["count"] == expected
    assert retry.provenance["deferred_from"] == "departure"
    assert retry.provenance["effect_program_v1"]["program_id"] == "not-in-state"


def test_a_departure_stops_waiting_after_a_day_of_retries(generated_scenario) -> None:
    # Before #78 was fixed the count was read as 0 every time, so this never ended.
    world = WorldState.from_scenario(generated_scenario)

    with pytest.raises(EventSuppressed, match="bounded deferral limit"):
        schedule_population_retry(world, _departure(1440))

    assert _retries(world) == []


def schedule_population_change(
    world: WorldState,
    *,
    op: str,
    count: int,
    start_minute: int,
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


def _population_change(
    world: WorldState,
    *,
    op: str,
    count: int,
    start_minute: int,
    zone_id: str,
    admission_zone_ids: tuple[str, ...] = (),
    approach_route_id: str | None = None,
    person_context_overrides: tuple[dict[str, object], ...] = (),
) -> None:
    schedule_population_change(
        world,
        op=op,
        count=count,
        start_minute=start_minute,
        zone_id=zone_id,
        admission_zone_ids=admission_zone_ids,
        approach_route_id=approach_route_id,
        person_context_overrides=person_context_overrides,
    )


class _ArtifactSink:
    trace_level = TraceLevel.OFF

    def record_decision(self, *_args: object, **_kwargs: object) -> None:
        pass


def test_population_departure_interrupts_open_action_with_applied_cause(
    generated_scenario,
) -> None:
    # The quiet area starts empty, so both arrivals are admitted there.
    world = WorldState.from_scenario(generated_scenario)
    _population_change(world, op="add_evacuees", count=2, start_minute=1, zone_id="quiet")
    controller = OfflineSimulationController(world, _ArtifactSink())
    world.minute_index = 1
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 1, CounterRNG(world.seed))
    arrival_ids = sorted(
        row["agent_id"]
        for row in world.model_state["population_dynamics_v1"]["arrivals"]
        if isinstance(row.get("agent_id"), str)
    )
    departed_id = arrival_ids[0]
    record_action_proposal(
        world,
        SimpleNamespace(
            agent_id=departed_id,
            action=ActionKind.WAIT,
            minute_index=1,
            trigger="population_departure_lifecycle_test",
        ),
        target_id=None,
        committed=True,
        active_actions=controller.active_actions,
    )
    _population_change(world, op="depart_evacuees", count=1, start_minute=2, zone_id="quiet")
    departure_event = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "effect_program_population"
        and event.due_minute == 2
        and event.payload.get("op") == "depart_evacuees"
    )

    world.minute_index = 2
    controller(TickPhase.DISPATCH_DUE_EVENTS, world, 2, CounterRNG(world.seed))

    assert world.event_log.find(departure_event.event_id).status.value == "applied"
    assert departed_id not in world.agents
    assert departed_id not in controller.active_actions
    transitions = [
        row.event.payload
        for row in world.event_log.records
        if row.event.event_type == "agent_action_lifecycle"
        and row.event.payload["agent_id"] == departed_id
    ]
    assert [row["transition"] for row in transitions] == ["started", "interrupted"]
    assert transitions[-1]["reason_code"] == "population_departure"
    interruption = next(
        row
        for row in world.event_log.records
        if row.event.event_type == "agent_action_lifecycle"
        and row.event.payload["agent_id"] == departed_id
        and row.event.payload["transition"] == "interrupted"
    )
    assert interruption.status.value == "applied"
    assert interruption.event.causation_id == departure_event.event_id


def test_dynamic_arrival_household_context_reaches_behavior_frames_and_checkpoint(
    generated_scenario,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    _population_change(
        world,
        op="add_evacuees",
        count=4,
        start_minute=1,
        zone_id="quiet",
        person_context_overrides=({"member_index": 2, "care_support_needs": ["hygiene_support"]},),
    )
    dispatcher = EventDispatcher({"effect_program_population": EffectProgramPopulationHandler()})
    world.minute_index = 1
    outcome = dispatcher.dispatch_due(world, 1)
    assert len(outcome) == 1 and outcome[0].status.value == "applied"

    population = world.model_state["population_dynamics_v1"]
    traits = population["profile_traits"]
    dynamic_context = population["profile_context"]
    dependent_id = next(
        identity
        for identity, row in traits.items()
        if identity.startswith("arrival-") and row["household_role"] == "dependent"
    )
    dependent_context = dynamic_context[dependent_id]
    caregiver_id = dependent_context["caregiver_ids"][0]
    caregiver_context = dynamic_context[caregiver_id]
    assert caregiver_context["care_for_ids"] == [dependent_id]
    assert caregiver_context["requires_review"] is True
    assert dependent_context["household_id"] == caregiver_context["household_id"]
    assert "ASM-POPULATION-DYNAMICS-V1" in dependent_context["source_ids"]
    assert dependent_context["requires_review"] is True
    assert dependent_context["care_support_needs"] == ["hygiene_support"]
    assert support_kind_for_agent(world, dependent_id) == "hygiene_support"
    assert "ASM-TEST-DYNAMIC-ARRIVAL-V1" in care_source_ids_for_agent(world, dependent_id)

    view = WorldView.from_world(world)
    assert view.agents[dependent_id].caregiver_ids == (caregiver_id,)
    assert view.agents[caregiver_id].care_for_ids == (dependent_id,)
    projected = next(
        row
        for row in world_frame(world, run_id="arrival-context", branch_id="main", computed_until=1)[
            "people"
        ]
        if row["id"] == dependent_id
    )
    assert projected["context"]["caregiver_ids"] == [caregiver_id]
    assert person_projection(world, dependent_id)["person"]["context"] == projected["context"]

    restored = Checkpoint.capture(world).restore_world()
    assert restored.model_state["population_dynamics_v1"]["profile_context"] == dynamic_context
    assert restored.canonical_json() == world.canonical_json()

    legacy = WorldState.from_scenario(generated_scenario)
    legacy.model_state["population_dynamics_v1"].pop("profile_context")
    legacy.validate()
    restored_legacy = Checkpoint.from_json(Checkpoint.capture(legacy).to_json()).restore_world()
    assert "profile_context" not in restored_legacy.model_state["population_dynamics_v1"]


def test_departure_retains_identity_and_admits_next_waiting_household_member(
    generated_scenario,
) -> None:
    # The sleeping area holds 21 and its 20 residents start there: one of two
    # arrivals gets in and the other waits.
    world = WorldState.from_scenario(generated_scenario)
    _population_change(world, op="add_evacuees", count=2, start_minute=1, zone_id="sleeping")
    dispatcher = EventDispatcher({"effect_program_population": EffectProgramPopulationHandler()})
    world.minute_index = 1
    dispatcher.dispatch_due(world, 1)

    population = world.model_state["population_dynamics_v1"]
    arrived = [row["agent_id"] for row in population["arrivals"] if "agent_id" in row]
    assert len(arrived) == 1
    waiting_id = population["pending_arrivals"][0]["profile"]["id"]
    _population_change(world, op="depart_evacuees", count=1, start_minute=2, zone_id="sleeping")
    world.minute_index = 2
    outcomes = dispatcher.dispatch_due(world, 2)

    assert len(outcomes) == 1
    assert outcomes[0].status.value == "applied"
    population = world.model_state["population_dynamics_v1"]
    assert arrived[0] not in world.agents
    assert waiting_id in world.agents
    assert world.agents[waiting_id].zone_id == "sleeping"
    assert not population["pending_arrivals"]
    assert population["departures"][-1]["agent_id"] == arrived[0]
    presence = world.model_state["population_presence_v1"]
    assert presence["people"][arrived[0]]["status"] == "departed"
    assert presence["people"][waiting_id]["status"] == "admitted"
    assert [row["action"] for row in presence["history"] if row["agent_id"] == waiting_id] == [
        "waiting",
        "admitted",
    ]
    frame = world_frame(world, run_id="arrival-test", branch_id="main", computed_until=2)
    assert frame["presence"]["people"][arrived[0]]["status"] == "departed"
    assert frame["presence"]["people"][waiting_id]["status"] == "admitted"
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()


def test_arrivals_use_explicit_alternate_physical_room_capacity_and_restore(
    generated_scenario,
) -> None:
    # Arrivals prefer the medical area and may go to the quiet area instead; both
    # are full here, so only the surveyed room in the quiet area can take them.
    document = generated_scenario.model_dump(mode="json")
    for zone in document["zones"]:
        if zone["id"] in {"medical", "quiet"}:
            zone["capacity"] = 0
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    room_action = TimedAction(
        effect_id="alternate-room",
        op="temporary_room_geometry",
        target_selector=TargetSelector(kind="zone", target_id="quiet"),
        start_minute=1,
        end_minute=20,
        parameters={
            "schema_version": 1,
            "room_id": "alternate-intake-room",
            "zone_id": "quiet",
            "polygon_m": [
                {"x": 8.0, "y": 2.4},
                {"x": 9.3, "y": 2.4},
                {"x": 9.3, "y": 4.4},
                {"x": 8.0, "y": 4.4},
            ],
            "people_capacity": 2,
            "sleeping_capacity": 0,
            "bed_points_m": [],
            "route_edge_ids": ["route_corridor_quiet", "route_quiet_corridor"],
            "source_ids": ["ASM-TEST-ALTERNATE-ROOM-V1"],
        },
        units="metre_2d",
        source_id="ASM-TEST-ALTERNATE-ROOM-V1",
        policy_dependencies=["geometry_overlay_v1"],
        reversible=False,
    )
    room_program = EffectProgram(
        program_id="alternate-room-program",
        narrative="Add a surveyed overflow room for explicitly routed intake.",
        source_text="temporary room for arrivals",
        simulation_start_minute=1,
        effects=[room_action],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )
    schedule_effect_program(world, room_program)
    dispatcher = EventDispatcher(
        {
            "effect_program_population": EffectProgramPopulationHandler(),
            "effect_program_geometry": EffectProgramGeometryHandler(),
            "effect_program_geometry_expire": EffectProgramGeometryHandler(expire=True),
        }
    )
    world.minute_index = 1
    assert dispatcher.dispatch_due(world, 1)[0].status.value == "applied"

    _population_change(
        world,
        op="add_evacuees",
        count=3,
        start_minute=2,
        zone_id="medical",
        admission_zone_ids=("quiet",),
    )
    world.minute_index = 2
    assert dispatcher.dispatch_due(world, 2)[0].status.value == "applied"
    population = world.model_state["population_dynamics_v1"]
    room_people = [
        item for item in population["arrivals"] if item.get("room_id") == "alternate-intake-room"
    ]
    assert len(room_people) == 2
    assert all(item["zone_id"] == "quiet" for item in room_people)
    assert len(population["pending_arrivals"]) == 1
    overlay = world.model_state["geometry_overlay_v1"]
    assert len(overlay["room_admissions"]) == 2
    for item in room_people:
        agent_id = item["agent_id"]
        position = world.model_state["movement_v1"]["positions"][agent_id]
        assert position["zone_id"] == "quiet"
        assert person_projection(world, agent_id)["person"]["physical_room"]["room_id"] == (
            "alternate-intake-room"
        )
    frame = world_frame(world, run_id="room-arrival-test", branch_id="main", computed_until=2)
    projected_room = frame["geometry_overlay"]["temporary_rooms"]["alternate-intake-room"]
    assert len(projected_room["admitted_agent_ids"]) == 2
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    _population_change(world, op="depart_evacuees", count=1, start_minute=3, zone_id="quiet")
    world.minute_index = 3
    assert dispatcher.dispatch_due(world, 3)[0].status.value == "applied"
    assert len(world.model_state["geometry_overlay_v1"]["room_admissions"]) == 2
    assert not world.model_state["population_dynamics_v1"]["pending_arrivals"]
    admitted_after_release = [
        item
        for item in world.model_state["population_dynamics_v1"]["arrivals"]
        if item.get("room_id") == "alternate-intake-room" and item["admitted_minute"] == 3
    ]
    assert len(admitted_after_release) == 1
    world.validate()


def _approach_world() -> WorldState:
    """The small replay fixture with a hand-built floor plan and an arrival approach route.

    The generator never makes approach routes; they are an operator input.
    """
    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
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
    zone_ids = [row["id"] for row in document["zones"]]
    polygon = [
        {"x": 0.0, "y": 0.0},
        {"x": 10.0, "y": 0.0},
        {"x": 10.0, "y": 10.0},
        {"x": 0.0, "y": 10.0},
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
            facility["id"]: {"x": 7.0, "y": 5.0} for facility in document["facilities"]
        },
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
    geometry = document["model_configs"]["facility_geometry"]
    geometry["width_m"] = 20.0
    geometry["height_m"] = 20.0
    geometry["arrival_approach_routes"] = [
        {
            "route_id": "arrival-to-corridor",
            "destination_zone_id": "corridor",
            "points_m": [{"x": 15.0, "y": 15.0}, {"x": 7.0, "y": 5.0}],
            "walking_speed_m_per_min": 5.0,
            "source_kind": "operator",
            "source_ids": ["SRC-TEST-APPROACH-ROUTE"],
        }
    ]
    return WorldState.from_scenario(ScenarioSpec.model_validate(document))


def test_dynamic_arrival_external_approach_progress_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _approach_world()
    schedule_population_change(
        world,
        op="add_evacuees",
        count=1,
        start_minute=2,
        zone_id="corridor",
        approach_route_id="arrival-to-corridor",
    )
    dispatcher = EventDispatcher(
        {
            "effect_program_population": EffectProgramPopulationHandler(),
            "population_approach_complete": EffectProgramPopulationHandler(),
        }
    )
    world.minute_index = 2
    outcome = dispatcher.dispatch_due(world, 2)[0]
    assert outcome.status.value == "applied", outcome.detail
    population = world.model_state["population_dynamics_v1"]
    pending = population["pending_arrivals"]
    assert len(pending) == 1
    incoming_id = pending[0]["profile"]["id"]
    arrival_event_id = pending[0]["event_id"]
    assert incoming_id not in world.agents
    assert pending[0]["approach_due_minute"] == 5
    assert world.model_state["population_presence_v1"]["people"][incoming_id]["status"] == (
        "approaching"
    )
    frame = world_frame(world, run_id="arrival-route", branch_id="main", computed_until=2)
    assert frame["incoming_arrivals"][0]["position"] == {"x": 15.0, "y": 15.0}
    assert frame["incoming_arrivals"][0]["status"] == "approaching"
    restored = Checkpoint.from_json(Checkpoint.capture(world).to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    world.minute_index = 3
    assert not admit_pending_arrivals(world, 3)
    frame = world_frame(world, run_id="arrival-route", branch_id="main", computed_until=3)
    route_distance = (8.0**2 + 10.0**2) ** 0.5
    assert frame["incoming_arrivals"][0]["progress"] == pytest.approx(5.0 / route_distance)
    world.minute_index = 5
    completion = dispatcher.dispatch_due(world, 5)
    assert len(completion) == 1
    assert completion[0].status.value == "applied", completion[0].detail
    assert admit_pending_arrivals(world, 5) == (incoming_id,)
    assert incoming_id in world.agents
    assert not world.model_state["population_dynamics_v1"]["pending_arrivals"]
    assert [
        row["action"]
        for row in world.model_state["population_presence_v1"]["history"]
        if row["agent_id"] == incoming_id
    ] == ["approaching", "approach_completed", "admitted"]
    arrival = next(
        row
        for row in world.model_state["population_dynamics_v1"]["arrivals"]
        if row.get("agent_id") == incoming_id
    )
    assert arrival["approach_route_id"] == "arrival-to-corridor"
    assert arrival["approach_completed_minute"] == 5
    history = [
        row
        for row in world.model_state["population_presence_v1"]["history"]
        if row["agent_id"] == incoming_id
    ]
    assert history[1]["event_id"] == completion[0].event.event_id
    assert history[1]["causation_id"] == arrival_event_id
    world.validate()

    from sheltersim.contracts.workspace import HistoryQuery
    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.storage.projects import ProjectStore

    store = ProjectStore(tmp_path / "arrival-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    checkpoint = Checkpoint.capture(world)
    index = HistoryIndex(store, "project", "run")
    index.ingest("main", checkpoint, rebuild=True)
    indexed = index.search(
        "main",
        HistoryQuery(
            kind="transitions",
            start=0,
            end=world.minute_index,
            agent_id=incoming_id,
        ),
        checkpoint.checkpoint_hash,
    )
    assert {row["action"] for row in indexed["items"]} == {
        "approaching",
        "approach_completed",
        "admitted",
    }
    completed_row = next(row for row in indexed["items"] if row["action"] == "approach_completed")
    assert completed_row["event_id"] == completion[0].event.event_id

    from sheltersim.web import workspace as workspace_module
    from sheltersim.web.workspace import ProjectWorkspace

    store = ProjectStore(tmp_path / "arrival-intervention")
    store.create_project("project")
    workspace = ProjectWorkspace(SimpleNamespace(store=store))

    class FakeTimelineStore:
        def __init__(self, *_args: object) -> None:
            pass

        def seek(self, _branch_id: str, _minute: int) -> SimpleNamespace:
            return SimpleNamespace(world=world)

    monkeypatch.setattr(workspace_module, "TimelineStore", FakeTimelineStore)
    program = EffectProgram(
        program_id="operator-arrival-preview",
        narrative="Add a resident using the selected approach route.",
        source_text="Operator supplied typed arrival condition.",
        simulation_start_minute=world.minute_index,
        effects=[
            TimedAction(
                effect_id="arrival-via-route",
                op="add_evacuees",
                target_selector=TargetSelector(kind="zone", target_id="corridor"),
                start_minute=world.minute_index + 1,
                parameters={"count": 1, "approach_route_id": "arrival-to-corridor"},
                units="evacuee",
                source_id="SRC-OPERATOR-ARRIVAL",
                policy_dependencies=["population_dynamics_v1"],
                reversible=False,
            )
        ],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "operator-v1"},
    )
    preview = workspace.intervention(
        "project",
        "run",
        "main",
        {"minute": world.minute_index, "program": program.model_dump(mode="json")},
    )
    assert preview["can_apply"] is True
    assert preview["program"]["effects"][0]["parameters"]["approach_route_id"] == (
        "arrival-to-corridor"
    )
    assert workspace._preview("project", preview["preview_id"])["program"].program_id == (
        "operator-arrival-preview"
    )


def test_arrival_approach_contract_rejects_unknown_routes_and_records_horizon_censoring() -> None:
    world = _approach_world()
    with pytest.raises(EffectProgramValidationError, match="unknown arrival approach route"):
        schedule_population_change(
            world,
            op="add_evacuees",
            count=1,
            start_minute=2,
            zone_id="corridor",
            approach_route_id="missing-route",
        )

    schedule_population_change(
        world,
        op="add_evacuees",
        count=1,
        start_minute=22,
        zone_id="corridor",
        approach_route_id="arrival-to-corridor",
    )
    dispatcher = EventDispatcher(
        {
            "effect_program_population": EffectProgramPopulationHandler(),
        }
    )
    world.minute_index = 22
    assert dispatcher.dispatch_due(world, 22)[0].status.value == "applied"
    pending = world.model_state["population_dynamics_v1"]["pending_arrivals"][0]
    assert pending["approach_due_minute"] == world.duration_minutes + 1
    assert "approach_completion_event_id" not in pending
    world.minute_index = world.duration_minutes
    frame = world_frame(
        world,
        run_id="arrival-route-censored",
        branch_id="main",
        computed_until=world.duration_minutes,
    )
    assert frame["incoming_arrivals"][0]["status"] == "censored"
    route_distance = (8.0**2 + 10.0**2) ** 0.5
    assert frame["incoming_arrivals"][0]["progress"] == pytest.approx(10.0 / route_distance)
    assert pending["profile"]["id"] not in world.agents
    world.validate()
