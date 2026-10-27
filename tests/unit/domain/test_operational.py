"""H03 delivery, water outage, staff unavailability, checkpoint, and branch contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sheltersim.contracts.operations import StaffSupportContinuityReceipt
from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    ingest_applied_shared_operation_records,
)
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import (
    DeliveryDelayHandler,
    EventDispatcher,
    FacilityOutageHandler,
    ResourceLedgerHandler,
    ResourceServiceCompletionHandler,
    StaffRestorationHandler,
    StaffSupportCompletionHandler,
    StaffUnavailabilityHandler,
    SupportRequestTimeoutHandler,
    WaterOutageHandler,
    WaterRestorationHandler,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    ScheduledEvent,
)
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.frames import person_projection
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import ActionKind, UtilityBehaviorPolicy, WorldView
from sheltersim.policies.psychology import (
    PsychologyParameters,
    SimplePsychologyPolicy,
    apply_state_deltas,
)
from sheltersim.storage.history_index import HistoryIndex
from sheltersim.storage.projects import ProjectStore

ROOT = Path(__file__).parents[3]
SMALL_FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"
H03_SOURCE = "ASM-H03-OPERATIONAL-EVENTS-V1"


def _event(
    event_id: str,
    event_type: str,
    due_minute: int,
    sequence: int,
    payload: dict[str, object],
    *,
    source_id: str = H03_SOURCE,
    causation_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": event_id,
        "kind": "exogenous",
        "due_minute": due_minute,
        "priority": 0,
        "sequence": sequence,
        "event_type": event_type,
        "payload": payload,
        "source_id": source_id,
        "causation_id": causation_id,
        "provenance": {"source_type": "assumption", "source_id": H03_SOURCE},
    }


def _scenario(
    events: list[dict[str, object]],
    *,
    duration: int = 40,
    resource_service: bool = False,
    initial_water: int = 1000,
    staff_ids: tuple[str, ...] | None = None,
) -> ScenarioSpec:
    document = json.loads(SMALL_FIXTURE.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration
    document["scheduled_events"] = events
    if staff_ids is not None:
        document["staff"] = [item for item in document["staff"] if item["id"] in staff_ids]
    if resource_service or any(
        str(item.get("event_type", "")).startswith("resource_") for item in events
    ):
        document["initial_stock"] = {
            "schema_version": 1,
            "warehouse_id": "warehouse-h03-test",
            "zone_id": "toilet",
            "water_ml": initial_water,
            "food_meal_units": 5,
            "source_ids": ["ASM-H03-TEST-STOCK-V1"] if initial_water or 5 else [],
        }
    if resource_service:
        document["facilities"].extend(
            [
                {
                    "id": "water_station_h03",
                    "kind": "water_station",
                    "zone_id": "toilet",
                    "capacity": 1,
                },
                {
                    "id": "meal_distribution_h03",
                    "kind": "meal_distribution",
                    "zone_id": "toilet",
                    "capacity": 1,
                },
            ]
        )
        document["staff"].extend(
            [
                {"id": "S-WATER-H03", "zone_id": "toilet", "skills": ["water_service"]},
                {"id": "S-MEAL-H03", "zone_id": "toilet", "skills": ["meal_service"]},
            ]
        )
        for profile in document["profiles"]:
            profile["initial_zone_id"] = "toilet"
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
    return ScenarioSpec.model_validate(document)


def _dispatcher(*, resource_service: ResourceServiceManager | None = None) -> EventDispatcher:
    handlers = {
        "delivery_delayed": DeliveryDelayHandler(),
        "resource_delivery": ResourceLedgerHandler("delivery"),
        "water_outage": WaterOutageHandler(),
        "water_restored": WaterRestorationHandler(),
        "staff_unavailable": StaffUnavailabilityHandler(),
        "staff_restored": StaffRestorationHandler(),
        "staff_support_complete": StaffSupportCompletionHandler(cooldown_minutes=6),
        "support_request_timeout": SupportRequestTimeoutHandler(cooldown_minutes=6),
        "facility_outage": FacilityOutageHandler(),
    }
    if resource_service is not None:
        handlers["resource_service_complete"] = ResourceServiceCompletionHandler(resource_service)
    return EventDispatcher(handlers)


def _set_minute(world: WorldState, minute: int) -> None:
    world.minute_index = minute


def _rehash_checkpoint_document(document: dict[str, object]) -> None:
    encoded_state = json.dumps(
        document["world_state"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    document["state_hash"] = hashlib.sha256(encoded_state).hexdigest()
    body = {key: value for key, value in document.items() if key != "checkpoint_hash"}
    encoded_body = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    document["checkpoint_hash"] = hashlib.sha256(encoded_body).hexdigest()


def test_shipment_delay_keeps_event_identity_and_applies_ledger_once_after_checkpoint() -> None:
    scenario = _scenario(
        [
            _event(
                "shipment-arrival",
                "resource_delivery",
                12,
                0,
                {
                    "warehouse_id": "warehouse-h03-test",
                    "resource_id": "water_ml",
                    "quantity": 250,
                    "shipment_id": "ship-01",
                },
            ),
            _event(
                "shipment-delay",
                "delivery_delayed",
                5,
                1,
                {"shipment_id": "ship-01", "to_minute": 20},
                causation_id="shipment-delay-request",
            ),
        ],
        duration=30,
    )
    world = WorldState.from_scenario(scenario)
    delivery = next(
        event for event in world.event_queue.pending() if event.event_id == "shipment-arrival"
    )
    _set_minute(world, 5)

    outcomes = _dispatcher().dispatch_due(world, 5)

    delayed = next(
        event for event in world.event_queue.pending() if event.event_id == delivery.event_id
    )
    assert len(outcomes) == 1 and outcomes[0].status is EventStatus.APPLIED
    assert outcomes[0].event.source_id == H03_SOURCE
    assert outcomes[0].event.causation_id == "shipment-delay-request"
    assert delayed.sequence == delivery.sequence and delayed.event_id == delivery.event_id
    assert delayed.due_minute == 20 and delayed.causation_id == "shipment-delay"
    assert world.model_state[OPERATIONAL_EVENTS_KEY]["shipments"]["ship-01"]["due_minute"] == 20
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()

    _set_minute(restored, 20)
    records = _dispatcher().dispatch_due(restored, 20)
    delivery_record = next(
        record for record in records if record.event.event_id == "shipment-arrival"
    )
    assert delivery_record.status is EventStatus.APPLIED
    assert delivery_record.event.payload["shipment_id"] == "ship-01"
    assert delivery_record.event.source_id == H03_SOURCE
    assert delivery_record.event.causation_id == "shipment-delay"
    ledger = restored.model_state[RESOURCE_LEDGER_KEY]
    assert ledger["accounts"]["water_ml"]["delivered"] == 250
    assert sum(record["operation"] == "delivery" for record in ledger["audit"]) == 1
    assert _dispatcher().dispatch_due(restored, 20) == ()


def test_same_minute_intervention_cannot_reschedule_a_due_delivery_snapshot() -> None:
    scenario = _scenario(
        [
            _event(
                "due-shipment",
                "resource_delivery",
                10,
                0,
                {
                    "warehouse_id": "warehouse-h03-test",
                    "resource_id": "water_ml",
                    "quantity": 250,
                    "shipment_id": "ship-due",
                },
            )
        ],
        duration=30,
    )
    world = WorldState.from_scenario(scenario)
    _set_minute(world, 10)
    same_minute_delay = ScheduledEvent(
        event_id="same-minute-delay",
        kind=EventKind.INTERVENTION,
        due_minute=10,
        priority=-1,
        sequence=world.allocate_event_sequence(),
        event_type="delivery_delayed",
        payload={"shipment_id": "ship-due", "to_minute": 20},
        source_id=H03_SOURCE,
        causation_id="operator-delay-request",
        provenance={"source_type": "assumption", "source_id": H03_SOURCE},
    )
    world.schedule_event(same_minute_delay)

    outcomes = _dispatcher().dispatch_due(world, 10)

    assert [record.event.event_id for record in outcomes] == [
        "same-minute-delay",
        "due-shipment",
    ]
    assert outcomes[0].status is EventStatus.FAILED
    assert outcomes[0].event.source_id == H03_SOURCE
    assert outcomes[0].event.causation_id == "operator-delay-request"
    assert outcomes[1].status is EventStatus.APPLIED
    assert outcomes[1].event.due_minute == 10
    ledger = world.model_state[RESOURCE_LEDGER_KEY]
    assert ledger["accounts"]["water_ml"]["delivered"] == 250
    assert sum(record["operation"] == "delivery" for record in ledger["audit"]) == 1
    world.validate()


def test_scenario_rejects_invalid_h03_payloads_ids_targets_and_delay_times() -> None:
    invalid_events = [
        _event(
            "bad-shipment",
            "resource_delivery",
            12,
            0,
            {
                "warehouse_id": "warehouse-h03-test",
                "resource_id": "water_ml",
                "quantity": 1,
                "shipment_id": "bad id",
            },
        ),
        _event(
            "unknown-delay", "delivery_delayed", 5, 0, {"shipment_id": "missing", "to_minute": 20}
        ),
        _event("bad-water", "water_outage", 1, 0, {"outage_id": "out-1", "zone_id": "missing"}),
        _event(
            "water-no-station", "water_outage", 1, 0, {"outage_id": "out-2", "zone_id": "sleeping"}
        ),
        _event("bad-staff", "staff_unavailable", 1, 0, {"staff_id": "unknown", "until_minute": 3}),
        _event(
            "bad-extra",
            "facility_outage",
            1,
            0,
            {"facility_id": "toilet_1", "units_out": 1, "x": 1},
        ),
    ]
    for event in invalid_events:
        with pytest.raises(ValidationError):
            _scenario([event], resource_service=event["event_type"] == "water_outage")

    delivery = _event(
        "ship",
        "resource_delivery",
        10,
        0,
        {
            "warehouse_id": "warehouse-h03-test",
            "resource_id": "water_ml",
            "quantity": 10,
            "shipment_id": "ship-1",
        },
    )
    same_due_delay = _event(
        "delay", "delivery_delayed", 10, 1, {"shipment_id": "ship-1", "to_minute": 20}
    )
    with pytest.raises(ValidationError):
        _scenario([delivery, same_due_delay], duration=30)

    outage = _event("outage", "water_outage", 1, 0, {"outage_id": "out-1", "zone_id": "toilet"})
    uncaused_restore = _event(
        "restore",
        "water_restored",
        3,
        1,
        {"outage_id": "out-1"},
        causation_id="other-event",
    )
    with pytest.raises(ValidationError):
        _scenario([outage, uncaused_restore], resource_service=True)

    second_restore = _event(
        "restore-again",
        "water_restored",
        4,
        2,
        {"outage_id": "out-1"},
        causation_id="outage",
    )
    with pytest.raises(ValidationError, match="duplicate restorations"):
        _scenario(
            [
                outage,
                _event(
                    "restore", "water_restored", 3, 1, {"outage_id": "out-1"}, causation_id="outage"
                ),
                second_restore,
            ],
            resource_service=True,
        )


def test_water_outage_blocks_new_service_but_keeps_toilet_and_active_service_intact() -> None:
    scenario = _scenario(
        [
            _event("water-out", "water_outage", 0, 0, {"outage_id": "out-1", "zone_id": "toilet"}),
            _event(
                "water-back",
                "water_restored",
                3,
                1,
                {"outage_id": "out-1"},
                causation_id="water-out",
            ),
        ],
        resource_service=True,
        duration=12,
    )
    world = WorldState.from_scenario(scenario)
    dispatcher = _dispatcher()
    toilets_before = world.facilities["toilet_1"].working_units
    world.agents["A001"].needs.hydration_need = 0.75
    world.agents["A001"].stress = 0.4
    dispatcher.dispatch_due(world, 0)
    manager = ResourceServiceManager(world)
    manager.request("A001", "water_station_h03")
    assert manager.arbitrate(0) == ()
    assert (
        world.model_state["resource_service_v1"]["stall_state"]["water_station_h03"]["reason"]
        == "water_outage"
    )
    assert world.facilities["toilet_1"].working_units == toilets_before
    assert world.agents["A001"].needs.hydration_need == 0.75
    assert world.agents["A001"].stress == 0.4

    _set_minute(world, 3)
    dispatcher.dispatch_due(world, 3)
    assert len(manager.arbitrate(3)) == 1
    world.validate()

    active_scenario = _scenario(
        [
            _event(
                "active-water-out",
                "water_outage",
                1,
                0,
                {"outage_id": "out-2", "facility_id": "water_station_h03"},
            )
        ],
        resource_service=True,
        duration=10,
    )
    active_world = WorldState.from_scenario(active_scenario)
    active_world.agents["A001"].needs.hydration_need = 0.75
    active_world.agents["A001"].stress = 0.4
    active_manager = ResourceServiceManager(active_world)
    active_manager.request("A001", "water_station_h03")
    completion = active_manager.arbitrate(0)[0]
    _set_minute(active_world, 1)
    _dispatcher(resource_service=active_manager).dispatch_due(active_world, 1)
    _set_minute(active_world, completion.due_minute)
    outcome = _dispatcher(resource_service=active_manager).dispatch_due(
        active_world, completion.due_minute
    )
    assert outcome[0].status is EventStatus.APPLIED
    assert active_world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["served"] == 250
    assert active_world.agents["A001"].needs.hydration_need == pytest.approx(0.4)
    assert active_world.agents["A001"].stress == 0.4
    assert active_world.facilities["toilet_1"].working_units == toilets_before


def test_checkpoint_rejects_removed_water_or_staff_restore_event_references() -> None:
    water_scenario = _scenario(
        [
            _event(
                "water-out-ref", "water_outage", 0, 0, {"outage_id": "out-ref", "zone_id": "toilet"}
            ),
            _event(
                "water-back-ref",
                "water_restored",
                5,
                1,
                {"outage_id": "out-ref"},
                causation_id="water-out-ref",
            ),
        ],
        resource_service=True,
        duration=12,
    )
    water_world = WorldState.from_scenario(water_scenario)
    _set_minute(water_world, 0)
    _dispatcher().dispatch_due(water_world, 0)
    water_world.model_state[OPERATIONAL_EVENTS_KEY]["water_outages"]["out-ref"][
        "restore_event_id"
    ] = None
    with pytest.raises(StateInvariantError, match="restoration state"):
        Checkpoint.capture(water_world)

    staff_scenario = _scenario(
        [
            _event(
                "staff-off-ref", "staff_unavailable", 0, 0, {"staff_id": "S01", "until_minute": 5}
            )
        ],
        duration=12,
        staff_ids=("S01",),
    )
    staff_world = WorldState.from_scenario(staff_scenario)
    _set_minute(staff_world, 0)
    _dispatcher().dispatch_due(staff_world, 0)
    staff_world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["S01"]["restore_event_id"] = (
        None
    )
    with pytest.raises(StateInvariantError, match="restoration reference"):
        Checkpoint.capture(staff_world)


def test_checkpoint_rejects_rehashed_water_and_staff_availability_tampering() -> None:
    water_world = WorldState.from_scenario(
        _scenario(
            [
                _event(
                    "water-out-tamper",
                    "water_outage",
                    0,
                    0,
                    {"outage_id": "tamper-out", "zone_id": "toilet"},
                )
            ],
            resource_service=True,
            duration=12,
        )
    )
    _dispatcher().dispatch_due(water_world, 0)
    water_document = json.loads(Checkpoint.capture(water_world).to_json())
    water_document["world_state"]["model_state"][OPERATIONAL_EVENTS_KEY]["water_outages"][
        "tamper-out"
    ]["active"] = False
    _rehash_checkpoint_document(water_document)
    with pytest.raises(CheckpointError, match="activity does not match applied event history"):
        Checkpoint.from_dict(water_document)

    staff_world = WorldState.from_scenario(
        _scenario(
            [
                _event(
                    "staff-off-tamper",
                    "staff_unavailable",
                    0,
                    0,
                    {"staff_id": "S01", "until_minute": 5},
                )
            ],
            duration=12,
            staff_ids=("S01",),
        )
    )
    _dispatcher().dispatch_due(staff_world, 0)
    staff_document = json.loads(Checkpoint.capture(staff_world).to_json())
    staff_document["world_state"]["model_state"][OPERATIONAL_EVENTS_KEY]["staff_status"]["S01"][
        "available"
    ] = True
    _rehash_checkpoint_document(staff_document)
    with pytest.raises(CheckpointError, match="availability does not match applied event history"):
        Checkpoint.from_dict(staff_document)


def test_water_outage_unmet_need_changes_water_action_without_direct_stress_override() -> None:
    scenario = _scenario(
        [
            _event(
                "need-water-out",
                "water_outage",
                0,
                0,
                {"outage_id": "need-out", "zone_id": "toilet"},
            )
        ],
        resource_service=True,
        duration=10,
    )
    world = WorldState.from_scenario(scenario)
    agent = world.agents["A001"]
    agent.zone_id = "toilet"
    agent.needs.toilet_urgency = 0.0
    agent.needs.hunger = 0.0
    agent.needs.fatigue = 0.0
    agent.needs.hydration_need = 0.24
    agent.stress = 0.1
    decision_policy = UtilityBehaviorPolicy(
        temperature=0.01,
        resource_service_enabled=True,
    )
    before = decision_policy.propose(WorldView.from_world(world), agent.id, CounterRNG(2026))
    before_candidates = {candidate.action: candidate for candidate in before.candidates}
    assert not before_candidates[ActionKind.FETCH_WATER].eligible

    applied = _dispatcher().dispatch_due(world, 0)
    assert applied[0].status is EventStatus.APPLIED
    assert applied[0].event.source_id == H03_SOURCE
    agent = world.agents["A001"]
    assert agent.stress == 0.1
    assert agent.needs.hydration_need == 0.24

    psychology = SimplePsychologyPolicy(
        PsychologyParameters(
            toilet_growth=0.0,
            hunger_growth=0.0,
            awake_fatigue_growth=0.0,
            stress_pressure=0.0,
            queued_stress=0.0,
            hydration_growth=0.02,
        )
    )
    apply_state_deltas(world, psychology.update(WorldView.from_world(world), 0))
    after = decision_policy.propose(WorldView.from_world(world), agent.id, CounterRNG(2026))
    after_candidates = {candidate.action: candidate for candidate in after.candidates}
    assert agent.needs.hydration_need == pytest.approx(0.26)
    assert after_candidates[ActionKind.FETCH_WATER].eligible
    assert after.action is ActionKind.FETCH_WATER
    assert agent.stress == 0.1

    manager = ResourceServiceManager(world)
    manager.request(agent.id, "water_station_h03")
    assert manager.arbitrate(0) == ()
    stall = world.model_state["resource_service_v1"]["stall_state"]["water_station_h03"]
    assert stall["reason"] == "water_outage"
    assert agent.stress == 0.1


def test_staff_unavailability_releases_active_resource_reservation_and_requeues_fifo_head() -> None:
    scenario = _scenario(
        [
            _event(
                "water-staff-off",
                "staff_unavailable",
                1,
                0,
                {
                    "staff_id": "S-WATER-H03",
                    "until_minute": 5,
                },
            )
        ],
        resource_service=True,
        duration=12,
    )
    world = WorldState.from_scenario(scenario)
    manager = ResourceServiceManager(world)
    manager.request("A001", "water_station_h03")
    manager.request("A002", "water_station_h03")
    completion = manager.arbitrate(0)[0]
    task_id = completion.payload["task_id"]
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["reserved"] == 250

    _set_minute(world, 1)
    outcomes = _dispatcher(resource_service=manager).dispatch_due(world, 1)
    assert outcomes[0].status is EventStatus.APPLIED
    assert outcomes[0].event.source_id == H03_SOURCE
    assert world.facilities["water_station_h03"].resource_queue_agent_ids == ["A001", "A002"]
    assert world.agents["A001"].activity.value == "queued"
    requeued_task = next(
        task
        for task in world.active_tasks.values()
        if task.kind == "resource_service_queue" and task.agent_id == "A001"
    )
    assert requeued_task.start_minute == requeued_task.due_minute == 0
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["reserved"] == 0
    assert task_id in world.model_state["resource_service_v1"]["cancelled_task_ids"]
    assert any(
        record.event.event_type == "resource_release"
        and record.event.causation_id.startswith("resource-service-cancel-")
        for record in world.event_log.records
    )
    world.validate()

    _set_minute(world, completion.due_minute)
    stale = _dispatcher(resource_service=manager).dispatch_due(world, completion.due_minute)
    assert stale[0].status is EventStatus.SUPPRESSED
    assert stale[0].event.source_id == completion.source_id
    assert stale[0].event.causation_id == completion.causation_id
    assert world.model_state[RESOURCE_LEDGER_KEY]["accounts"]["water_ml"]["served"] == 0


def test_unavailable_staff_cannot_keep_an_active_checkpoint_task() -> None:
    scenario = _scenario(
        [
            _event(
                "water-staff-off-corrupt-checkpoint",
                "staff_unavailable",
                1,
                0,
                {"staff_id": "S-WATER-H03", "until_minute": 12},
            )
        ],
        resource_service=True,
        duration=12,
    )
    world = WorldState.from_scenario(scenario)
    manager = ResourceServiceManager(world)
    manager.request("A001", "water_station_h03")
    manager.arbitrate(0)
    world.validate()

    unavailable_event = next(
        event
        for event in world.event_queue.pending()
        if event.event_id == "water-staff-off-corrupt-checkpoint"
    )
    world.event_queue.remove(unavailable_event.event_id)
    world.minute_index = 1
    world.event_log.append(
        EventLogRecord(
            event=unavailable_event,
            realized_minute=1,
            status=EventStatus.APPLIED,
        )
    )
    status = world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["S-WATER-H03"]
    status.update(
        {
            "available": False,
            "unavailability_event_id": "water-staff-off-corrupt-checkpoint",
            "until_minute": 12,
            "restore_event_id": None,
        }
    )

    with pytest.raises(StateInvariantError, match="unavailable staff"):
        world.validate()


def test_staff_absence_requeues_support_and_suppresses_stale_completion() -> None:
    scenario = _scenario(
        [
            _event(
                "staff-off-old", "staff_unavailable", 2, 0, {"staff_id": "S01", "until_minute": 8}
            ),
            _event(
                "staff-off-new", "staff_unavailable", 3, 1, {"staff_id": "S01", "until_minute": 6}
            ),
        ],
        duration=24,
    )
    world = WorldState.from_scenario(scenario)
    staff = StaffService(
        world, task_duration_minutes=10, unresolved_after_minutes=20, cooldown_minutes=6
    )
    receipt = staff.request("A001")
    staff.assign_pending(0)
    old_task_id = world.support_requests[receipt.request_id].task_id
    completion = next(
        e for e in world.event_queue.pending() if e.event_type == "staff_support_complete"
    )

    _set_minute(world, 2)
    _dispatcher().dispatch_due(world, 2)
    request = world.support_requests[receipt.request_id]
    assert request.status.value == "pending" and request.task_id is None
    assert world.agents["A001"].support_request_id == receipt.request_id
    assert not world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["S01"]["available"]
    assert old_task_id in world.model_state[OPERATIONAL_EVENTS_KEY]["cancelled_staff_task_ids"]

    _set_minute(world, 3)
    _dispatcher().dispatch_due(world, 3)
    latest = world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["S01"]
    assert latest["unavailability_event_id"] == "staff-off-new"
    _set_minute(world, 6)
    restored = _dispatcher().dispatch_due(world, 6)
    assert restored[0].status is EventStatus.APPLIED
    _set_minute(world, 8)
    stale_restore = _dispatcher().dispatch_due(world, 8)
    assert stale_restore[0].status is EventStatus.SUPPRESSED
    assert stale_restore[0].event.source_id == H03_SOURCE
    assert stale_restore[0].event.causation_id == "staff-off-old"
    assert world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["S01"]["available"]

    _set_minute(world, completion.due_minute)
    stale_completion = _dispatcher().dispatch_due(world, completion.due_minute)
    assert stale_completion[0].status is EventStatus.SUPPRESSED
    world.validate()


def test_interrupted_support_is_reassigned_and_only_new_completion_resolves_request(
    tmp_path: Path,
) -> None:
    scenario = _scenario(
        [
            _event(
                "primary-staff-off",
                "staff_unavailable",
                2,
                0,
                {"staff_id": "S01", "until_minute": 8},
            )
        ],
        duration=24,
        staff_ids=("S01", "S02"),
    )
    scenario.model_configs["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    world = WorldState.from_scenario(scenario)
    initialize_simulator_extensions(world)
    staff = StaffService(
        world, task_duration_minutes=10, unresolved_after_minutes=20, cooldown_minutes=6
    )
    receipt = staff.request("A001")
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="emotional_support",
        status="requested",
    )
    staff.assign_pending(0, validate_state=False)
    first_task_id = world.support_requests[receipt.request_id].task_id
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="emotional_support",
        status="assigned",
        staff_id="S01",
        causation_id=first_task_id,
    )
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="emotional_support",
        status="started",
        staff_id="S01",
        causation_id=first_task_id,
    )
    old_completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_support_complete"
        and event.payload.get("task_id") == first_task_id
    )

    _set_minute(world, 2)
    outcomes = _dispatcher().dispatch_due(world, 2)
    ingest_applied_shared_operation_records(world, outcomes)
    continuity = [
        record for record in outcomes
        if record.event.event_type == "staff_support_continuity"
    ]
    assert len(continuity) == 1
    assert continuity[0].status is EventStatus.APPLIED
    continuity_receipt = StaffSupportContinuityReceipt.model_validate(
        continuity[0].event.payload
    )
    assert continuity_receipt.request_id == receipt.request_id
    assert continuity_receipt.agent_id == "A001"
    assert continuity_receipt.task_id == first_task_id
    assert continuity_receipt.previous_staff_id == "S01"
    assert continuity_receipt.status == "requeued"
    assert continuity_receipt.causation_id == "primary-staff-off"
    projection = person_projection(world, "A001")["person"]
    assert projection is not None
    assert projection["care_history"][-1]["event_type"] == "staff_support_continuity"
    assert projection["care_history"][-1]["status"] == "requeued"
    assert projection["care_requests"][0]["status"] == "requested"
    assert projection["care_requests"][0]["runtime_status"] == "pending"

    store = ProjectStore(tmp_path / "support-continuity-history")
    store.create_project("project")
    store._path("project", "runs", "run").mkdir(parents=True)
    index = HistoryIndex(store, "project", "run")
    checkpoint = Checkpoint.capture(world)
    index.ingest("main", checkpoint, rebuild=True)
    page = index.search(
        "main",
        HistoryQuery(
            kind="care", start=0, end=2, agent_id="A001", event_id=continuity_receipt.event_id
        ),
        checkpoint.checkpoint_hash,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["status"] == "requeued"
    assert page["items"][0]["task_id"] == first_task_id
    restored = Checkpoint.from_json(checkpoint.to_json()).restore_world()
    assert restored.canonical_json() == world.canonical_json()
    assert world.support_requests[receipt.request_id].status.value == "pending"
    reassigned = staff.assign_pending(2, validate_state=False)
    request = world.support_requests[receipt.request_id]
    assert reassigned == (receipt.request_id,)
    assert request.status.value == "assigned"
    assert request.assigned_staff_id == "S02"
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="emotional_support",
        status="assigned",
        staff_id="S02",
        causation_id=request.task_id,
    )
    append_applied_care_transition(
        world,
        request_id=receipt.request_id,
        agent_id="A001",
        household_id=None,
        support_kind="emotional_support",
        status="started",
        staff_id="S02",
        causation_id=request.task_id,
    )
    new_completion = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "staff_support_complete"
        and event.payload.get("task_id") == request.task_id
    )
    assert new_completion.event_id != old_completion.event_id

    _set_minute(world, old_completion.due_minute)
    old_outcomes = _dispatcher().dispatch_due(world, old_completion.due_minute)
    assert (
        next(
            record for record in old_outcomes if record.event.event_id == old_completion.event_id
        ).status
        is EventStatus.SUPPRESSED
    )
    assert world.support_requests[receipt.request_id].status.value == "assigned"

    _set_minute(world, new_completion.due_minute)
    new_outcomes = _dispatcher().dispatch_due(world, new_completion.due_minute)
    ingest_applied_shared_operation_records(world, new_outcomes)
    assert (
        next(
            record for record in new_outcomes if record.event.event_id == new_completion.event_id
        ).status
        is EventStatus.APPLIED
    )
    assert world.support_requests[receipt.request_id].status.value == "resolved"
    assert world.metric_state["support_resolved_count"] == 1.0
    world.validate()


def test_support_timeout_suppression_and_recreation_after_absence() -> None:
    scenario = _scenario(
        [
            _event(
                "support-staff-off",
                "staff_unavailable",
                2,
                0,
                {
                    "staff_id": "S01",
                    "until_minute": 7,
                },
            )
        ],
        duration=30,
        staff_ids=("S01",),
    )
    world = WorldState.from_scenario(scenario)
    staff = StaffService(
        world, task_duration_minutes=10, unresolved_after_minutes=3, cooldown_minutes=5
    )
    receipt = staff.request("A001")
    staff.assign_pending(0)
    timeout = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "support_request_timeout"
    )
    _set_minute(world, 2)
    world.event_queue.remove(timeout.event_id)
    outcome = _dispatcher().dispatch_due(world, 2)
    assert outcome[0].status is EventStatus.APPLIED
    request = world.support_requests[receipt.request_id]
    assert request.status.value == "pending"
    recreated = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "support_request_timeout"
    )
    assert recreated.source_id == receipt.request_id
    assert recreated.causation_id == "support-staff-off"
    assert recreated.sequence > timeout.sequence
    assert Checkpoint.capture(world).restore_world().canonical_json() == world.canonical_json()

    expired_scenario = _scenario(
        [
            _event(
                "staff-off-expired",
                "staff_unavailable",
                4,
                0,
                {
                    "staff_id": "S01",
                    "until_minute": 8,
                },
            )
        ],
        duration=24,
        staff_ids=("S01",),
    )
    expired = WorldState.from_scenario(expired_scenario)
    expired_staff = StaffService(
        expired, task_duration_minutes=10, unresolved_after_minutes=3, cooldown_minutes=5
    )
    expired_receipt = expired_staff.request("A001")
    expired_staff.assign_pending(0)
    _set_minute(expired, 3)
    timeout_outcome = _dispatcher().dispatch_due(expired, 3)
    assert timeout_outcome[0].status is EventStatus.SUPPRESSED
    _set_minute(expired, 4)
    expired_outcomes = _dispatcher().dispatch_due(expired, 4)
    continuity = next(
        row for row in expired_outcomes
        if row.event.event_type == "staff_support_continuity"
    )
    assert continuity.event.payload["status"] == "unresolved"
    request = expired.support_requests[expired_receipt.request_id]
    assert request.status.value == "unresolved"
    assert request.resolved_minute == 4
    assert not any(
        event.event_type == "support_request_timeout"
        and event.payload.get("request_id") == expired_receipt.request_id
        for event in expired.event_queue.pending()
    )
    expired.validate()


def test_invalid_operational_payload_is_atomic_and_v5_baseline_has_no_h03_state() -> None:
    baseline = WorldState.from_scenario(ScenarioSpec.load(SMALL_FIXTURE))
    assert baseline.schema_version == 5
    assert OPERATIONAL_EVENTS_KEY not in baseline.model_state

    scenario = _scenario(
        [
            _event(
                "shipment",
                "resource_delivery",
                10,
                0,
                {
                    "warehouse_id": "warehouse-h03-test",
                    "resource_id": "water_ml",
                    "quantity": 10,
                    "shipment_id": "ship-atomic",
                },
            )
        ],
        duration=20,
    )
    world = WorldState.from_scenario(scenario)
    before = world.canonical_json()
    invalid = ScheduledEvent(
        event_id="bad-delay",
        kind=EventKind.EXOGENOUS,
        due_minute=1,
        priority=0,
        sequence=2,
        event_type="delivery_delayed",
        payload={"shipment_id": "ship-atomic", "to_minute": 15, "extra": True},
        source_id=H03_SOURCE,
    )
    with pytest.raises(EventApplicationError):
        DeliveryDelayHandler().apply(world, invalid)
    assert world.canonical_json() == before

    failed = ScheduledEvent(
        event_id="dispatch-bad-delay",
        kind=EventKind.EXOGENOUS,
        due_minute=1,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="delivery_delayed",
        payload={"shipment_id": "missing-shipment", "to_minute": 15},
        source_id=H03_SOURCE,
        causation_id="invalid-delay-request",
        provenance={"source_type": "assumption", "source_id": H03_SOURCE},
    )
    world.schedule_event(failed)
    operational_before = json.loads(json.dumps(world.model_state[OPERATIONAL_EVENTS_KEY]))
    _set_minute(world, 1)
    failed_outcome = _dispatcher().dispatch_due(world, 1)
    assert failed_outcome[0].status is EventStatus.FAILED
    assert failed_outcome[0].event.source_id == H03_SOURCE
    assert failed_outcome[0].event.causation_id == "invalid-delay-request"
    assert world.model_state[OPERATIONAL_EVENTS_KEY] == operational_before
    world.validate()

    bad_facility = ScheduledEvent(
        event_id="bad-facility",
        kind=EventKind.EXOGENOUS,
        due_minute=1,
        priority=0,
        sequence=2,
        event_type="facility_outage",
        payload={"facility_id": "toilet_1", "units_out": 1, "unexpected": 1},
        source_id=H03_SOURCE,
    )
    before = baseline.canonical_json()
    with pytest.raises(EventApplicationError):
        FacilityOutageHandler().apply(baseline, bad_facility)
    assert baseline.canonical_json() == before


def test_operational_event_artifact_is_enabled_only_for_h03_worlds(tmp_path: Path) -> None:
    scenario = _scenario(
        [
            _event(
                "artifact-water-out",
                "water_outage",
                1,
                0,
                {"outage_id": "artifact-out", "zone_id": "toilet"},
            )
        ],
        resource_service=True,
    )
    world = WorldState.from_scenario(scenario)
    output = tmp_path / "h03-run"
    writer = RunArtifactWriter(scenario, output, trace_level=TraceLevel.OFF)
    writer.write_initial(world)
    writer.finalize(world)

    artifact = json.loads((output / "operational_events.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert set(artifact["water_outages"]) == {"artifact-out"}
    assert manifest["operational_events_summary"]["water_outage_count"] == 1


def test_branch_noop_outage_replay_and_child_only_delay() -> None:
    outage_scenario = _scenario(
        [
            _event(
                "branch-water-out",
                "water_outage",
                2,
                0,
                {"outage_id": "branch-out", "zone_id": "toilet"},
            ),
            _event(
                "branch-water-back",
                "water_restored",
                4,
                1,
                {"outage_id": "branch-out"},
                causation_id="branch-water-out",
            ),
        ],
        resource_service=True,
        duration=10,
    )
    dispatcher = _dispatcher()
    start = WorldState.from_scenario(outage_scenario)

    def advance(world: WorldState, start_minute: int, end_minute: int) -> None:
        for minute in range(start_minute, end_minute + 1):
            _set_minute(world, minute)
            dispatcher.dispatch_due(world, minute)
            world.validate()

    for fork_minute in (0, 2, 4):
        parent_world = WorldState.from_dict(start.to_dict())
        if fork_minute:
            advance(parent_world, 0, fork_minute)
        parent = BranchRuntime.from_checkpoint(
            Checkpoint.capture(parent_world), branch_id=f"parent-{fork_minute}"
        )
        child = parent.fork(f"noop-{fork_minute}")
        advance(parent.world, fork_minute, 6)
        advance(child.world, fork_minute, 6)
        assert child.world.canonical_json() == parent.world.canonical_json()

    shipment_scenario = _scenario(
        [
            _event(
                "branch-shipment",
                "resource_delivery",
                10,
                0,
                {
                    "warehouse_id": "warehouse-h03-test",
                    "resource_id": "water_ml",
                    "quantity": 10,
                    "shipment_id": "branch-ship",
                },
            ),
            _event(
                "branch-delay",
                "delivery_delayed",
                5,
                1,
                {"shipment_id": "branch-ship", "to_minute": 20},
            ),
        ],
        duration=30,
    )
    base = BranchRuntime.from_checkpoint(
        Checkpoint.capture(WorldState.from_scenario(shipment_scenario)), branch_id="shipment-parent"
    )
    delayed_child = base.fork("shipment-child")
    _set_minute(delayed_child.world, 5)
    _dispatcher().dispatch_due(delayed_child.world, 5)
    child_delivery = next(
        e for e in delayed_child.world.event_queue.pending() if e.event_id == "branch-shipment"
    )
    parent_delivery = next(
        e for e in base.world.event_queue.pending() if e.event_id == "branch-shipment"
    )
    assert child_delivery.due_minute == 20
    assert parent_delivery.due_minute == 10
    assert len(base.world.event_log) == 0
