"""Opt-in H02 hydration, staffed resource service, and replay contracts.

Water and meals served while the TickEngine runs are checked in
tests/integration/test_resource_service.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.paths import SMALL_REPLAY, SOURCE_MANIFEST

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.models import Activity
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.events.handlers import (
    AgentTravelCompletionHandler,
    EventDispatcher,
    ResourceLedgerHandler,
    ResourceServiceCancellationHandler,
    ResourceServiceCompletionHandler,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    ScheduledEvent,
)
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter


def queue_and_start(
    world: WorldState,
    *,
    agent_id: str = "A001",
    facility_id: str = "water_station_1",
    minute: int = 0,
) -> tuple[ResourceServiceManager, ScheduledEvent]:
    manager = ResourceServiceManager(world)
    world.agents[agent_id].zone_id = world.facilities[facility_id].zone_id
    manager.request(agent_id, facility_id, minute=minute)
    started = manager.arbitrate(minute)
    assert len(started) == 1
    world.validate()
    return manager, started[0]


def test_v5_and_v6_serialization_keep_strict_need_contracts_and_checkpoint_version(
    resource_service_scenario,
) -> None:
    baseline = WorldState.from_scenario(ScenarioSpec.load(SMALL_REPLAY))
    baseline_json = baseline.canonical_json()
    baseline_doc_state = baseline.to_dict()
    assert baseline.schema_version == 5
    assert set(baseline_doc_state["agents"][0]["needs"]) == {
        "toilet_urgency",
        "hunger",
        "fatigue",
    }
    assert set(baseline_doc_state["agents"][0]["profile"]["initial_needs"]) == {
        "toilet_urgency",
        "hunger",
        "fatigue",
    }
    assert WorldState.from_dict(baseline_doc_state).canonical_json() == baseline_json
    with pytest.raises(StateInvariantError, match="hydration_need to be zero"):
        baseline.agents["A001"].needs.hydration_need = 0.1
        baseline.validate()
    baseline.agents["A001"].needs.hydration_need = 0.0

    scenario = resource_service_scenario()
    world = WorldState.from_scenario(scenario)
    state_doc = world.to_dict()
    assert world.schema_version == 6
    assert set(state_doc["agents"][0]["needs"]) == {
        "toilet_urgency",
        "hunger",
        "fatigue",
        "hydration_need",
    }
    assert "resource_queue_agent_ids" in state_doc["facilities"][0]
    restored = WorldState.from_dict(state_doc)
    assert restored.canonical_json() == world.canonical_json()
    checkpoint = Checkpoint.capture(world)
    assert checkpoint.world_state_schema_version == 6
    assert Checkpoint.from_json(checkpoint.to_json()).restore_world().canonical_json() == (
        world.canonical_json()
    )

    for invalid in (5.0, True, 7):
        invalid_doc = dict(state_doc, schema_version=invalid)
        with pytest.raises(StateInvariantError, match="unsupported world-state schema"):
            WorldState.from_dict(invalid_doc)


def test_resource_service_config_is_strict_and_opt_in_only() -> None:
    baseline = WorldState.from_scenario(ScenarioSpec.load(SMALL_REPLAY))
    assert baseline.schema_version == 5
    assert baseline.agents["A001"].needs.hydration_need == 0.0

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-h02",
        "water_ml": 1000,
        "food_meal_units": 2,
        "source_ids": ["ASM-H02-TEST-INITIAL-STOCK-V1"],
    }
    document["model_configs"]["resource_service"] = {
        "schema_version": 1,
        "policy_version": "resource-service-v1",
        "source_id": "ASM-H02-RESOURCE-SERVICE-V1",
        "service_minutes": 2,
        "water_ml_per_service": 250,
        "food_meal_units_per_service": 1,
        "hydration_growth_per_minute": 0.02,
        "unexpected": True,
    }
    with pytest.raises(ValidationError, match="unexpected"):
        ScenarioSpec.model_validate(document)

    for field, value in (
        ("service_minutes", 1441),
        ("water_ml_per_service", 100_001),
        ("food_meal_units_per_service", 101),
        ("water_utility_need_weight", 10.1),
    ):
        invalid = json.loads(json.dumps(document))
        config = invalid["model_configs"]["resource_service"]
        config.pop("unexpected")
        config["source_type"] = "assumption"
        config[field] = value
        with pytest.raises(ValidationError):
            ScenarioSpec.model_validate(invalid)


def test_v6_world_and_checkpoint_reject_noncanonical_service_config(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    state_document = world.to_dict()
    del state_document["model_configs"]["resource_service"]["water_utility_base"]
    with pytest.raises(StateInvariantError, match="complete canonical v1 values"):
        WorldState.from_dict(state_document)

    checkpoint_document = Checkpoint.capture(world).to_dict()
    del checkpoint_document["world_state"]["model_configs"]["resource_service"][
        "water_utility_base"
    ]
    checkpoint_document["state_hash"] = Checkpoint.document_hashes(checkpoint_document)[0]
    with pytest.raises(ValueError, match="checkpoint payload is invalid.*complete canonical"):
        Checkpoint.from_dict(checkpoint_document)


def test_resource_request_requires_arrival_and_world_rejects_legacy_resource_slots(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    manager = ResourceServiceManager(world)
    agent = world.agents["A001"]
    facility = world.facilities["water_station_1"]
    agent.zone_id = "sleeping"
    before = world.canonical_json()
    with pytest.raises(ValueError, match="must arrive"):
        manager.request(agent.id, facility.id)
    assert world.canonical_json() == before

    agent.zone_id = facility.zone_id
    manager.request(agent.id, facility.id)
    agent.zone_id = "sleeping"
    with pytest.raises(StateInvariantError, match="inconsistent activity, target, or zone"):
        world.validate()

    agent.zone_id = facility.zone_id
    legacy_agent = world.agents["A002"]
    legacy_agent.activity = Activity.QUEUED
    legacy_agent.target_id = facility.id
    facility.queue_agent_ids.append(legacy_agent.id)
    with pytest.raises(StateInvariantError, match="cannot use legacy facility queues"):
        world.validate()


def test_h02_test_inputs_match_assumption_manifest(resource_service_scenario) -> None:
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    assumptions = {item["id"]: item for item in manifest["assumptions"]}
    scenario = resource_service_scenario()
    config = scenario.model_configs["resource_service"]
    service_assumption = assumptions["ASM-H02-RESOURCE-SERVICE-V1"]
    for field, value in config.items():
        if field not in {"schema_version", "policy_version", "source_id", "source_type"}:
            assert service_assumption["values"][field] == value
    stock_assumption = assumptions["ASM-H02-TEST-INITIAL-STOCK-V1"]
    assert stock_assumption["values"]["water_ml"] == scenario.initial_stock.water_ml == 1000
    assert (
        stock_assumption["values"]["food_meal_units"] == scenario.initial_stock.food_meal_units == 4
    )
    stalls = assumptions["ASM-H02-QUEUE-STALLS-V1"]["values"]["stall_reasons"]
    assert stalls == ["capacity", "staff", "stock", "run_end"]


def test_completion_schedule_collision_is_rejected_before_start_mutation(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    manager = ResourceServiceManager(world)
    world.agents["A001"].zone_id = world.facilities["water_station_1"].zone_id
    manager.request("A001", "water_station_1")
    world.id_counters["event"] = 3
    collision = ScheduledEvent(
        event_id="resource-service-complete-3",
        kind=EventKind.ENDOGENOUS,
        due_minute=0,
        priority=0,
        sequence=0,
        event_type="prior_event",
        payload={},
    )
    world.event_log.append(
        EventLogRecord(event=collision, realized_minute=0, status=EventStatus.APPLIED)
    )
    world.validate()
    before = world.canonical_json()

    with pytest.raises(StateInvariantError, match="identity"):
        manager.arbitrate(0)

    assert world.canonical_json() == before


def test_h01_reservation_id_collision_does_not_partially_start_service(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    facility = world.facilities["water_station_1"]
    world.agents["A001"].zone_id = facility.zone_id
    manager = ResourceServiceManager(world)
    receipt = manager.request("A001", facility.id)
    pending_reserve = ScheduledEvent(
        event_id="h01-reserve-colliding-with-queued-service",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.id_counters["event"],
        event_type="resource_reserve",
        payload={
            "warehouse_id": "warehouse-h02",
            "resource_id": "water_ml",
            "reservation_id": receipt.queue_task_id,
            "agent_id": "A002",
            "quantity": 250,
        },
        source_id="ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
    )
    world.schedule_event(pending_reserve)
    before = world.canonical_json()

    with pytest.raises(StateInvariantError, match="conflicts with an H01 reservation"):
        manager.arbitrate(0)

    assert world.canonical_json() == before
    assert facility.resource_queue_agent_ids == ["A001"]
    assert facility.resource_active_agent_ids == []
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["reserved"] == 0


def test_h01_external_reservation_survives_v6_checkpoint_and_release(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    reserve = ScheduledEvent(
        event_id="h01-reserve-outside-service",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=0,
        event_type="resource_reserve",
        payload={
            "warehouse_id": "warehouse-h02",
            "resource_id": "food_meal_units",
            "reservation_id": "resource-service-task-0",
            "agent_id": "A001",
            "quantity": 1,
        },
        source_id="ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
    )
    world.schedule_event(reserve)
    dispatcher = EventDispatcher(
        {
            "resource_reserve": ResourceLedgerHandler("reserve"),
            "resource_release": ResourceLedgerHandler("release"),
        }
    )
    assert dispatcher.dispatch_due(world, 0)[0].status is EventStatus.APPLIED
    world.validate()
    assert world.model_state["resource_service_v1"]["active_reservation_ids"] == []
    world.agents["A001"].zone_id = world.facilities["meal_distribution_1"].zone_id
    receipt = ResourceServiceManager(world).request("A001", "meal_distribution_1")
    assert receipt.queue_task_id == "resource-service-task-1"

    resumed = Checkpoint.capture(world).restore_world()
    resumed.minute_index = 1
    release = ScheduledEvent(
        event_id="h01-release-after-restore",
        kind=EventKind.EXOGENOUS,
        due_minute=1,
        priority=0,
        sequence=1,
        event_type="resource_release",
        payload={
            "warehouse_id": "warehouse-h02",
            "resource_id": "food_meal_units",
            "reservation_id": "resource-service-task-0",
            "agent_id": "A001",
        },
        source_id="ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
    )
    resumed.schedule_event(release)
    assert dispatcher.dispatch_due(resumed, 1)[0].status is EventStatus.APPLIED
    resumed.validate()

    food = resumed.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]
    assert food["reserved"] == 0
    assert food["reservations"] == {}
    assert [entry["operation"] for entry in resumed.model_state[RESOURCE_LEDGER_KEY]["audit"]][
        -2:
    ] == [
        "reserve",
        "release",
    ]


def test_h01_reserve_cannot_claim_a_queued_h02_service_task_id(resource_service_scenario) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    world.agents["A001"].zone_id = world.facilities["water_station_1"].zone_id
    receipt = ResourceServiceManager(world).request("A001", "water_station_1")
    reserve = ScheduledEvent(
        event_id="h01-colliding-queue-reserve",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=0,
        event_type="resource_reserve",
        payload={
            "warehouse_id": "warehouse-h02",
            "resource_id": "water_ml",
            "reservation_id": receipt.queue_task_id,
            "agent_id": "A002",
            "quantity": 250,
        },
        source_id="ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
    )
    before = world.canonical_json()
    with pytest.raises(EventApplicationError, match="conflicts with a queued"):
        ResourceLedgerHandler("reserve").apply(world, reserve)
    assert world.canonical_json() == before
    world.validate()


def test_stock_delivery_retries_fifo_head_on_next_arbitration(resource_service_scenario) -> None:
    world = WorldState.from_scenario(resource_service_scenario(water_ml=0, duration=8))
    facility = world.facilities["water_station_1"]
    world.agents["A001"].zone_id = facility.zone_id
    manager = ResourceServiceManager(world)
    manager.request("A001", facility.id)

    assert manager.arbitrate(0) == ()
    assert facility.resource_queue_agent_ids == ["A001"]
    assert world.model_state["resource_service_v1"]["queue_stall_history"][-1]["reason"] == (
        "stock"
    )

    delivery = ScheduledEvent(
        event_id="h01-water-delivery-after-stock-stall",
        kind=EventKind.EXOGENOUS,
        due_minute=1,
        priority=0,
        sequence=world.id_counters["event"],
        event_type="resource_delivery",
        payload={
            "warehouse_id": "warehouse-h02",
            "resource_id": "water_ml",
            "quantity": 250,
        },
        source_id="ASM-H01-TEST-RESOURCE-OPERATION-V1",
    )
    world.schedule_event(delivery)
    world.minute_index = 1
    dispatched = EventDispatcher(
        {"resource_delivery": ResourceLedgerHandler("delivery")}
    ).dispatch_due(world, 1)
    assert dispatched[0].status is EventStatus.APPLIED
    world.validate()

    started = manager.arbitrate(1)
    assert len(started) == 1
    assert facility.resource_queue_agent_ids == []
    assert facility.resource_active_agent_ids == ["A001"]
    account = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]
    assert account["delivered"] == account["reserved"] == 250
    world.validate()


@pytest.mark.parametrize("operation", ["release", "consume"])
def test_h01_handlers_cannot_mutate_service_owned_reservation(
    operation: str, resource_service_scenario
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    _, completion = queue_and_start(world)
    payload: dict[str, object] = {
        "warehouse_id": "warehouse-h02",
        "resource_id": "water_ml",
        "reservation_id": completion.payload["reservation_id"],
        "agent_id": "A001",
    }
    if operation == "consume":
        payload["quantity"] = 250
    event = ScheduledEvent(
        event_id=f"h01-{operation}-service-owned",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.id_counters["event"],
        event_type=f"resource_{operation}",
        payload=payload,
        source_id="ASM-H01-TEST-RESOURCE-ALLOCATION-V1",
    )
    before = world.canonical_json()
    with pytest.raises(EventApplicationError, match="service-owned"):
        ResourceLedgerHandler(operation).apply(world, event)
    assert world.canonical_json() == before
    world.validate()


def test_water_service_completes_ledger_need_and_event_log_after_checkpoint_restore(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    world.model_configs["resource_service"]["hydration_relief"] = 0.2
    world.agents["A001"].needs.hydration_need = 0.8
    manager, completion = queue_and_start(world)
    assert world.agents["A001"].activity.value == "in_service"
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["reserved"] == 250
    checkpoint = Checkpoint.capture(world)
    resumed = checkpoint.restore_world()

    worlds = (world, resumed)
    managers = (manager, ResourceServiceManager(resumed))
    for current, service in zip(worlds, managers, strict=True):
        current.minute_index = completion.due_minute
        dispatcher = EventDispatcher(
            {"resource_service_complete": ResourceServiceCompletionHandler(service)}
        )
        outcomes = dispatcher.dispatch_due(current, completion.due_minute)
        assert outcomes[-1].status is EventStatus.APPLIED
        current.validate()

    assert resumed.canonical_json() == world.canonical_json()
    assert resumed.event_log.records == world.event_log.records
    assert resumed.agents["A001"].needs.hydration_need == pytest.approx(0.6)
    account = resumed.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]
    assert account["remaining"] == 750
    assert account["served"] == 250
    assert account["reserved"] == 0
    assert [item["operation"] for item in resumed.model_state[RESOURCE_LEDGER_KEY]["audit"]] == [
        "initial",
        "initial",
        "reserve",
        "consume",
    ]


def test_partial_or_wrong_agent_completion_payload_fails_before_any_mutation(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    manager, completion = queue_and_start(world)
    for changes in ({"quantity": 1}, {"agent_id": "A002"}):
        payload = completion.to_dict()["payload"]
        payload.update(changes)
        tampered = ScheduledEvent(**{**completion.to_dict(), "payload": payload})
        before = world.canonical_json()
        with pytest.raises(ValueError, match="completion"):
            manager.complete(tampered)
        assert world.canonical_json() == before


def test_typed_cancellation_releases_once_and_stale_completion_is_suppressed(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(resource_service_scenario())
    manager, completion = queue_and_start(world)
    cancel = ScheduledEvent(
        event_id="resource-cancel-operator",
        kind=EventKind.INTERVENTION,
        due_minute=1,
        priority=-10,
        sequence=world.allocate_event_sequence(),
        event_type="resource_service_cancel",
        payload={"task_id": completion.payload["task_id"], "reason": "operator_cancelled"},
        source_id="ASM-H02-RESOURCE-SERVICE-V1",
    )
    world.schedule_event(cancel)
    world.minute_index = 1
    cancellation_dispatcher = EventDispatcher(
        {"resource_service_cancel": ResourceServiceCancellationHandler(manager)}
    )
    cancellation_result = cancellation_dispatcher.dispatch_due(world, 1)
    assert cancellation_result[0].status is EventStatus.APPLIED
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["reserved"] == 0
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["remaining"] == 1000
    world.validate()

    world.minute_index = completion.due_minute
    completion_result = EventDispatcher(
        {"resource_service_complete": ResourceServiceCompletionHandler(manager)}
    ).dispatch_due(world, completion.due_minute)
    assert completion_result[0].status is EventStatus.SUPPRESSED
    assert world.agents["A001"].activity.value == "idle"
    assert world.model_state[RESOURCE_LEDGER_KEY]["audit"][-1]["operation"] == "release"
    world.validate()


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"water_capacity": 0}, "capacity"),
        ({"water_staff_count": 0}, "staff"),
        ({"water_ml": 100}, "stock"),
        ({"duration": 2, "service_minutes": 2}, "run_end"),
    ],
)
def test_unavailable_capacity_staff_stock_or_run_end_keeps_fifo_head(
    overrides: dict[str, object], expected_reason: str, resource_service_scenario
) -> None:
    world = WorldState.from_scenario(resource_service_scenario(**overrides))
    manager = ResourceServiceManager(world)
    world.agents["A001"].zone_id = world.facilities["water_station_1"].zone_id
    manager.request("A001", "water_station_1")
    assert manager.arbitrate(0) == ()
    facility = world.facilities["water_station_1"]
    assert facility.resource_queue_agent_ids == ["A001"]
    assert facility.resource_active_agent_ids == []
    assert world.model_state["resource_service_v1"]["queue_stall_history"][-1]["reason"] == (
        expected_reason
    )
    world.validate()


def test_fifo_and_one_staff_one_service_allow_one_last_meal_to_one_agent(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(
        resource_service_scenario(food_units=1, meal_capacity=2, service_minutes=2)
    )
    manager = ResourceServiceManager(world)
    for agent_id in ("A001", "A002"):
        world.agents[agent_id].zone_id = world.facilities["meal_distribution_1"].zone_id
    manager.request("A001", "meal_distribution_1")
    manager.request("A002", "meal_distribution_1")
    started = manager.arbitrate(0)
    assert len(started) == 1
    assert world.facilities["meal_distribution_1"].resource_active_agent_ids == ["A001"]
    assert world.facilities["meal_distribution_1"].resource_queue_agent_ids == ["A002"]
    account = world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["food_meal_units"]
    assert account["reserved"] == 1
    assert world.model_state["resource_service_v1"]["queue_stall_history"][-1]["reason"] == (
        "staff"
    )
    world.validate()


def test_capacity_n_starts_at_most_n_services_with_one_staff_each(
    resource_service_scenario,
) -> None:
    world = WorldState.from_scenario(
        resource_service_scenario(water_capacity=2, water_staff_count=2, service_minutes=2)
    )
    manager = ResourceServiceManager(world)
    for agent_id in ("A001", "A002"):
        world.agents[agent_id].zone_id = world.facilities["water_station_1"].zone_id
    manager.request("A001", "water_station_1")
    manager.request("A002", "water_station_1")
    started = manager.arbitrate(0)
    assert len(started) == 2
    assert world.facilities["water_station_1"].resource_active_agent_ids == ["A001", "A002"]
    assert len({world.active_tasks[item.payload["task_id"]].staff_id for item in started}) == 2
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["reserved"] == 500
    world.validate()


def test_arrival_admission_rejection_is_saved_with_origin_and_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_service_scenario
) -> None:
    scenario = resource_service_scenario(duration=8)
    world = WorldState.from_scenario(scenario)
    agent = world.agents["A001"]
    agent.zone_id = "sleeping"
    output = tmp_path / "arrival-rejected"
    artifacts = RunArtifactWriter(scenario, output, trace_level=TraceLevel.SUMMARY)
    artifacts.write_initial(world)
    controller = OfflineSimulationController(world, artifacts)
    manager = controller.resource_service
    assert manager is not None
    route = controller.space.route(agent.zone_id, "toilet", agent.id)
    assert route is not None
    assert controller._schedule_travel(
        agent_id=agent.id,
        route=route,
        purpose="resource_request",
        facility_id="water_station_1",
        minute=world.minute_index,
        validate_state=False,
    )
    travel_event = world.event_queue.pending()[0]

    def reject_request(*args: object, **kwargs: object) -> None:
        raise ValueError("test admission failure")

    monkeypatch.setattr(manager, "request", reject_request)
    world.minute_index = travel_event.due_minute
    outcomes = EventDispatcher(
        {
            "agent_travel_complete": AgentTravelCompletionHandler(
                controller.toilet_service,
                resource_service=manager,
                validate_request_state=False,
            )
        }
    ).dispatch_due(world, travel_event.due_minute)
    assert outcomes[0].status is EventStatus.APPLIED
    assert agent.activity.value == "idle"
    history = world.model_state["resource_service_v1"]["request_history"]
    assert history[-1] == {
        "minute": travel_event.due_minute,
        "agent_id": "A001",
        "action": "fetch_water",
        "outcome": "rejected",
        "reason": "request_rejected",
        "facility_id": "water_station_1",
        "origin_zone_id": "sleeping",
        "detail": "test admission failure",
    }
    world.validate()
    artifacts.finalize(world)
    service_artifact = json.loads((output / "resource_service.json").read_text(encoding="utf-8"))
    assert service_artifact["request_history"][-1] == history[-1]


def test_behavior_view_exposes_resource_actions_only_for_opted_in_v6(
    resource_service_scenario,
) -> None:
    from sheltersim.core.rng import CounterRNG
    from sheltersim.policies.behavior import ActionKind, UtilityBehaviorPolicy, WorldView

    baseline = WorldState.from_scenario(ScenarioSpec.load(SMALL_REPLAY))
    baseline.agents["A001"].next_decision_at = 0
    old_view = WorldView.from_world(baseline)
    old_proposal = UtilityBehaviorPolicy().propose(old_view, "A001", CounterRNG(3))
    assert not old_view.resource_service_enabled
    assert ActionKind.FETCH_WATER not in {item.action for item in old_proposal.candidates}

    world = WorldState.from_scenario(resource_service_scenario())
    world.agents["A001"].next_decision_at = 0
    world.agents["A001"].needs.hydration_need = 0.9
    view = WorldView.from_world(world)
    proposal = UtilityBehaviorPolicy(
        resource_service_enabled=True,
        policy_version="utility-softmax-resource-service-v1",
    ).propose(view, "A001", CounterRNG(3))
    assert view.resource_service_enabled
    assert ActionKind.FETCH_WATER in {item.action for item in proposal.candidates}
    assert proposal.policy_version == "utility-softmax-resource-service-v1"
