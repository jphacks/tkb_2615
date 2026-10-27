"""Opt-in infrastructure propagation, lifecycle, and checkpoint invariants (A03)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from support.paths import SMALL_REPLAY

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.infrastructure import (
    INFRASTRUCTURE_STATE_KEY,
    ScenarioInfrastructureSpec,
)
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.events.handlers import (
    DeliveryDelayHandler,
    EventDispatcher,
    InfrastructureEventHandler,
    ResourceLedgerHandler,
    StaffRestorationHandler,
    StaffUnavailabilityHandler,
)
from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import WorldView
from sheltersim.policies.psychology import PsychologyParameters, SimplePsychologyPolicy

SOURCE_ID = "SCN-A03-TEST"


def _event(
    event_id: str,
    event_type: str,
    due_minute: int,
    sequence: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": event_id,
        "kind": "exogenous",
        "due_minute": due_minute,
        "priority": 0,
        "sequence": sequence,
        "event_type": event_type,
        "payload": payload,
        "source_id": SOURCE_ID,
        "provenance": {"source_type": "assumption", "source_id": SOURCE_ID},
    }


def _scenario(
    events: list[dict[str, Any]],
    *,
    edges: list[dict[str, str]] | None = None,
    duration: int = 20,
    heat_rate: float = 0.001,
    cold_rate: float = 0.001,
) -> ScenarioSpec:
    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["id"] = "a03-infrastructure-test"
    document["schema_version"] = 3
    document["duration_minutes"] = duration
    document["scheduled_events"] = events
    delivery = next((item for item in events if item["event_type"] == "resource_delivery"), None)
    if delivery is not None:
        document["initial_stock"] = {
            "schema_version": 1,
            "warehouse_id": delivery["payload"]["warehouse_id"],
            "zone_id": "sleeping",
            "water_ml": 1000,
            "food_meal_units": 0,
            "source_ids": ["ASM-A03-TEST-STOCK"],
        }
    document["model_configs"]["infrastructure"] = {
        "dependency_edges": edges or [],
        "heat_stress_per_minute": heat_rate,
        "cold_stress_per_minute": cold_rate,
    }
    return ScenarioSpec.model_validate(document)


def _dispatcher() -> EventDispatcher:
    infrastructure = InfrastructureEventHandler()
    return EventDispatcher(
        {
            "power_outage": infrastructure,
            "power_restored": infrastructure,
            "equipment_failure": infrastructure,
            "equipment_repaired": infrastructure,
            "extreme_heat": infrastructure,
            "extreme_cold": infrastructure,
            "environmental_stressor_end": infrastructure,
            "supply_route_interruption": infrastructure,
            "supply_route_restored": infrastructure,
        }
    )


def _set_minute(world: WorldState, minute: int) -> None:
    world.minute_index = minute
    world.validate()


def test_power_outage_cascades_only_through_dependents_and_replays_from_checkpoint() -> None:
    scenario = _scenario(
        [
            _event(
                "power-down",
                "power_outage",
                0,
                0,
                {"outage_id": "outage-grid", "component_id": "grid-main", "until_minute": 5},
            )
        ],
        edges=[
            {"dependent_id": "toilet_1", "dependency_id": "panel-main"},
            {"dependent_id": "toilet_2", "dependency_id": "panel-main"},
            {"dependent_id": "panel-main", "dependency_id": "grid-main"},
        ],
    )
    world = WorldState.from_scenario(scenario)

    _dispatcher().dispatch_due(world, 0)

    assert world.facilities["toilet_1"].working_units == 0
    assert world.facilities["toilet_2"].working_units == 0
    assert world.facilities["toilet_3"].working_units == 1
    assert world.facilities["toilet_4"].working_units == 1
    assert world.event_log.records[0].status is EventStatus.APPLIED
    restore = next(
        event for event in world.event_queue.pending() if event.event_type == "power_restored"
    )
    assert restore.causation_id == "power-down"
    assert restore.source_id == SOURCE_ID

    parent = BranchRuntime.from_world(world)
    child = parent.fork("replay-copy")
    assert child.world.canonical_json() == parent.world.canonical_json()
    assert (
        child.world.model_state[INFRASTRUCTURE_STATE_KEY]
        is not parent.world.model_state[INFRASTRUCTURE_STATE_KEY]
    )

    _set_minute(parent.world, 5)
    _set_minute(child.world, 5)
    _dispatcher().dispatch_due(parent.world, 5)
    _dispatcher().dispatch_due(child.world, 5)
    assert parent.world.facilities["toilet_1"].working_units == 1
    assert child.world.canonical_json() == parent.world.canonical_json()


def test_overlapping_power_outages_restore_capacity_only_after_last_cause() -> None:
    scenario = _scenario(
        [
            _event(
                "power-down-a",
                "power_outage",
                0,
                0,
                {"outage_id": "outage-a", "component_id": "grid-main", "until_minute": 5},
            ),
            _event(
                "power-down-b",
                "power_outage",
                2,
                1,
                {"outage_id": "outage-b", "component_id": "grid-main", "until_minute": 8},
            ),
        ],
        edges=[{"dependent_id": "toilet_1", "dependency_id": "grid-main"}],
    )
    world = WorldState.from_scenario(scenario)
    dispatcher = _dispatcher()

    dispatcher.dispatch_due(world, 0)
    _set_minute(world, 2)
    dispatcher.dispatch_due(world, 2)
    _set_minute(world, 5)
    dispatcher.dispatch_due(world, 5)
    assert world.facilities["toilet_1"].working_units == 0
    assert world.model_state[INFRASTRUCTURE_STATE_KEY]["outages"]["outage-b"]["active"] is True
    _set_minute(world, 8)
    dispatcher.dispatch_due(world, 8)

    assert world.facilities["toilet_1"].working_units == 1
    assert all(
        not item["active"]
        for item in world.model_state[INFRASTRUCTURE_STATE_KEY]["outages"].values()
    )


def test_competing_equipment_failures_reject_atomically_and_keep_first_outage() -> None:
    scenario = _scenario(
        [
            _event(
                "equipment-a",
                "equipment_failure",
                0,
                0,
                {
                    "outage_id": "equipment-outage-a",
                    "facility_id": "toilet_1",
                    "units_out": 1,
                    "until_minute": 5,
                },
            ),
            _event(
                "equipment-b",
                "equipment_failure",
                1,
                1,
                {
                    "outage_id": "equipment-outage-b",
                    "facility_id": "toilet_1",
                    "units_out": 1,
                    "until_minute": 6,
                },
            ),
        ],
        edges=[{"dependent_id": "toilet_1", "dependency_id": "panel-main"}],
    )
    world = WorldState.from_scenario(scenario)
    dispatcher = _dispatcher()

    dispatcher.dispatch_due(world, 0)
    _set_minute(world, 1)
    outcomes = dispatcher.dispatch_due(world, 1)

    assert outcomes[0].status is EventStatus.FAILED
    assert "combined infrastructure failures" in (outcomes[0].detail or "")
    assert world.facilities["toilet_1"].working_units == 0
    assert world.model_state[INFRASTRUCTURE_STATE_KEY]["outages"]["equipment-outage-a"]["active"]
    assert not world.model_state[INFRASTRUCTURE_STATE_KEY]["outages"]["equipment-outage-b"][
        "active"
    ]
    _set_minute(world, 5)
    dispatcher.dispatch_due(world, 5)
    assert world.facilities["toilet_1"].working_units == 1


def test_power_outage_with_active_facility_task_fails_without_partial_changes() -> None:
    scenario = _scenario(
        [
            _event(
                "power-down",
                "power_outage",
                0,
                0,
                {"outage_id": "outage-grid", "component_id": "grid-main", "until_minute": 5},
            )
        ],
        edges=[{"dependent_id": "toilet_1", "dependency_id": "grid-main"}],
    )
    world = WorldState.from_scenario(scenario)
    facility = world.facilities["toilet_1"]
    agent = world.agents["A001"]
    agent.activity = Activity.IN_SERVICE
    agent.target_id = facility.id
    facility.active_agent_ids.append(agent.id)
    world.active_tasks["active-service-a03"] = ActiveTask(
        id="active-service-a03",
        kind="toilet_service",
        agent_id=agent.id,
        resource_id=facility.id,
        start_minute=0,
        due_minute=5,
    )
    world.schedule_event(
        ScheduledEvent(
            event_id="active-service-completion-a03",
            kind=EventKind.COMPLETION,
            due_minute=5,
            priority=0,
            sequence=world.allocate_event_sequence(),
            event_type="toilet_service_complete",
            payload={
                "facility_id": facility.id,
                "agent_id": agent.id,
                "task_id": "active-service-a03",
            },
            source_id="active-service-a03",
        )
    )
    world.validate()

    outcome = _dispatcher().dispatch_due(world, 0)[0]

    assert outcome.status is EventStatus.FAILED
    assert "active occupancy" in (outcome.detail or "")
    assert facility.working_units == 1
    assert not world.model_state[INFRASTRUCTURE_STATE_KEY]["outages"]["outage-grid"]["active"]
    assert not any(event.event_type == "power_restored" for event in world.event_queue.pending())
    world.validate()


def test_environmental_stressor_is_zone_scoped_bounded_and_expires_with_provenance() -> None:
    event = _event(
        "heat-wave",
        "extreme_heat",
        0,
        0,
        {"stressor_id": "heat-a", "zone_id": "sleeping", "until_minute": 3},
    )
    scenario = _scenario([event], heat_rate=0.002)
    document = scenario.model_dump(mode="json")
    for index, profile in enumerate(document["profiles"]):
        profile["initial_zone_id"] = "sleeping" if index == 0 else "information_desk"
        profile["stress_sensitivity"] = 1.0
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    _dispatcher().dispatch_due(world, 0)

    view = WorldView.from_world(world)
    assert view.agents["A001"].environmental_stress_per_minute == pytest.approx(0.002)
    assert view.agents["A001"].environmental_stressor_event_ids == ("heat-wave",)
    assert view.agents["A002"].environmental_stress_per_minute == 0.0
    parameters = PsychologyParameters(
        toilet_growth=0,
        hunger_growth=0,
        awake_fatigue_growth=0,
        resting_fatigue_recovery=0,
        sleeping_fatigue_recovery=0,
        stress_pressure=0,
        queued_stress=0,
        support_wait_stress=0,
        resting_stress_recovery=0,
        sleeping_stress_recovery=0,
        need_pressure_threshold=1,
    )
    result = SimplePsychologyPolicy(parameters).explain_update(view, 0)
    traces = result.trace_document(0, source_id="ASM-H04-CAUSAL-STRESS-V1", full=True)
    by_agent = {record["agent_id"]: record for record in traces["agents"]}
    assert result.deltas[0].stress == pytest.approx(0.002)
    assert by_agent["A001"]["stress"]["components"][
        "experimental_environmental_stress"
    ] == pytest.approx(0.002)
    assert by_agent["A001"]["source_references"]["environmental_assumption_id"] == (
        "ASM-A03-INFRASTRUCTURE-V1"
    )
    assert by_agent["A001"]["source_references"]["environmental_stressor_event_ids"] == [
        "heat-wave"
    ]

    _set_minute(world, 3)
    _dispatcher().dispatch_due(world, 3)
    assert WorldView.from_world(world).agents["A001"].environmental_stress_per_minute == 0.0


def test_staff_shift_change_reuses_h03_task_safe_unavailability_lifecycle() -> None:
    scenario = _scenario(
        [
            _event(
                "shift-break",
                "staff_shift_change",
                0,
                0,
                {"staff_id": "S01", "until_minute": 4},
            )
        ]
    )
    world = WorldState.from_scenario(scenario)
    dispatcher = EventDispatcher(
        {
            "staff_shift_change": StaffUnavailabilityHandler(),
            "staff_restored": StaffRestorationHandler(),
        }
    )

    dispatcher.dispatch_due(world, 0)
    assert world.model_state["operational_events_v1"]["staff_status"]["S01"]["available"] is False
    restore = next(
        event for event in world.event_queue.pending() if event.event_type == "staff_restored"
    )
    assert restore.causation_id == "shift-break"
    _set_minute(world, 4)
    dispatcher.dispatch_due(world, 4)

    assert world.model_state["operational_events_v1"]["staff_status"]["S01"]["available"] is True


def test_supply_route_interruption_delays_only_named_shipments_and_restores_route() -> None:
    events = [
        _event(
            "water-arrival",
            "resource_delivery",
            2,
            0,
            {
                "warehouse_id": "warehouse-a03",
                "resource_id": "water_ml",
                "quantity": 500,
                "shipment_id": "water-shipment",
            },
        ),
        _event(
            "route-down",
            "supply_route_interruption",
            1,
            1,
            {
                "outage_id": "route-outage",
                "route_id": "road-east",
                "shipment_ids": ["water-shipment"],
                "until_minute": 3,
            },
        ),
    ]
    scenario = _scenario(events, duration=20)
    world = WorldState.from_scenario(scenario)
    infrastructure = InfrastructureEventHandler()
    dispatcher = EventDispatcher(
        {
            "supply_route_interruption": infrastructure,
            "supply_route_restored": infrastructure,
            "delivery_delayed": DeliveryDelayHandler(),
            "resource_delivery": ResourceLedgerHandler("delivery"),
        }
    )

    _set_minute(world, 1)
    dispatcher.dispatch_due(world, 1)
    _set_minute(world, 2)
    dispatcher.dispatch_due(world, 2)
    shipment = world.model_state["operational_events_v1"]["shipments"]["water-shipment"]
    assert shipment["due_minute"] == 4
    assert world.resources["water_ml"] == 1000
    delayed = next(
        record
        for record in world.event_log.records
        if record.event.event_type == "delivery_delayed"
    )
    assert delayed.event.causation_id == "route-down"
    _set_minute(world, 3)
    dispatcher.dispatch_due(world, 3)
    assert not world.model_state[INFRASTRUCTURE_STATE_KEY]["route_interruptions"]["route-outage"][
        "active"
    ]
    assert (
        next(
            event
            for event in world.event_queue.pending()
            if event.payload.get("shipment_id") == "water-shipment"
        ).due_minute
        == 4
    )
    _set_minute(world, 4)
    dispatcher.dispatch_due(world, 4)
    assert world.resources["water_ml"] == 1500


def test_compound_supply_outage_and_heat_chain_survives_checkpoint_restore() -> None:
    events = [
        _event(
            "water-arrival",
            "resource_delivery",
            2,
            0,
            {
                "warehouse_id": "warehouse-a03",
                "resource_id": "water_ml",
                "quantity": 500,
                "shipment_id": "water-shipment",
            },
        ),
        _event(
            "route-down",
            "supply_route_interruption",
            1,
            1,
            {
                "outage_id": "route-outage",
                "route_id": "road-east",
                "shipment_ids": ["water-shipment"],
                "until_minute": 4,
            },
        ),
        _event(
            "power-down",
            "power_outage",
            1,
            2,
            {"outage_id": "power-outage", "component_id": "grid-main", "until_minute": 6},
        ),
        _event(
            "heat-wave",
            "extreme_heat",
            1,
            3,
            {"stressor_id": "heat-a", "zone_id": "sleeping", "until_minute": 5},
        ),
    ]
    scenario = _scenario(
        events,
        edges=[{"dependent_id": "toilet_1", "dependency_id": "grid-main"}],
        heat_rate=0.002,
    )
    document = scenario.model_dump(mode="json")
    document["profiles"][0]["initial_zone_id"] = "sleeping"
    scenario = ScenarioSpec.model_validate(document)
    world = WorldState.from_scenario(scenario)
    infrastructure = InfrastructureEventHandler()
    dispatcher = EventDispatcher(
        {
            "supply_route_interruption": infrastructure,
            "supply_route_restored": infrastructure,
            "power_outage": infrastructure,
            "power_restored": infrastructure,
            "extreme_heat": infrastructure,
            "environmental_stressor_end": infrastructure,
            "delivery_delayed": DeliveryDelayHandler(),
            "resource_delivery": ResourceLedgerHandler("delivery"),
        }
    )

    _set_minute(world, 1)
    dispatcher.dispatch_due(world, 1)
    assert world.facilities["toilet_1"].working_units == 0
    assert world.resources["water_ml"] == 1000
    assert (
        world.model_state["operational_events_v1"]["shipments"]["water-shipment"]["due_minute"] == 5
    )
    assert WorldView.from_world(world).agents[
        "A001"
    ].environmental_stress_per_minute == pytest.approx(0.002)

    checkpoint = Checkpoint.capture(world)
    resumed = checkpoint.restore_world()
    assert resumed.canonical_json() == world.canonical_json()

    _set_minute(resumed, 4)
    dispatcher.dispatch_due(resumed, 4)
    assert (
        resumed.model_state[INFRASTRUCTURE_STATE_KEY]["route_interruptions"]["route-outage"][
            "active"
        ]
        is False
    )
    assert resumed.resources["water_ml"] == 1000
    _set_minute(resumed, 5)
    dispatcher.dispatch_due(resumed, 5)
    assert resumed.resources["water_ml"] == 1500
    assert WorldView.from_world(resumed).agents["A001"].environmental_stress_per_minute == 0.0
    _set_minute(resumed, 6)
    dispatcher.dispatch_due(resumed, 6)
    assert resumed.facilities["toilet_1"].working_units == 1
    assert all(
        record.status is EventStatus.APPLIED
        for record in resumed.event_log.records
        if record.event.event_id in {"route-down", "power-down", "heat-wave", "water-arrival"}
    )


def test_infrastructure_config_is_strict_and_legacy_scenario_hash_is_unchanged() -> None:
    base = ScenarioSpec.load(SMALL_REPLAY)
    document = base.model_dump(mode="json")
    legacy = ScenarioSpec.model_validate(document)
    assert legacy.canonical_json() == base.canonical_json()

    with pytest.raises(ValidationError, match="acyclic"):
        ScenarioInfrastructureSpec.model_validate(
            {
                "dependency_edges": [
                    {"dependent_id": "toilet_1", "dependency_id": "panel"},
                    {"dependent_id": "panel", "dependency_id": "toilet_1"},
                ]
            }
        )
    with pytest.raises(ValidationError, match="unknown"):
        _scenario(
            [
                _event(
                    "unknown-power",
                    "power_outage",
                    0,
                    0,
                    {"outage_id": "outage", "component_id": "missing-grid", "until_minute": 5},
                )
            ],
            edges=[{"dependent_id": "toilet_1", "dependency_id": "grid-main"}],
        )
    edges = [
        {"dependent_id": "toilet_1", "dependency_id": "grid-main"},
        {"dependent_id": "toilet_2", "dependency_id": "grid-main"},
    ]
    config_forward = ScenarioInfrastructureSpec.model_validate({"dependency_edges": edges})
    config_reverse = ScenarioInfrastructureSpec.model_validate(
        {"dependency_edges": list(reversed(edges))}
    )
    assert config_forward.model_dump(mode="json") == config_reverse.model_dump(mode="json")


def test_checkpoint_restore_rejects_corrupt_infrastructure_lifecycle_links() -> None:
    scenario = _scenario(
        [
            _event(
                "power-down",
                "power_outage",
                0,
                0,
                {"outage_id": "outage-grid", "component_id": "grid-main", "until_minute": 5},
            )
        ],
        edges=[{"dependent_id": "toilet_1", "dependency_id": "grid-main"}],
    )
    world = WorldState.from_scenario(scenario)
    _dispatcher().dispatch_due(world, 0)
    corrupt = world.to_dict()
    corrupt["model_state"][INFRASTRUCTURE_STATE_KEY]["outages"]["outage-grid"][
        "affected_facility_ids"
    ] = ["toilet_2"]

    with pytest.raises(StateInvariantError, match="facility closure"):
        WorldState.from_dict(corrupt)

    capacity_corrupt = world.to_dict()
    toilet = next(item for item in capacity_corrupt["facilities"] if item["id"] == "toilet_1")
    toilet["working_units"] = 1
    with pytest.raises(StateInvariantError, match="capacity disagree"):
        WorldState.from_dict(capacity_corrupt)
