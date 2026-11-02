"""A kernel's suppressed and failed texts are its Python rule's, byte for byte, raised by itself.

These texts are saved: the dispatcher writes them to the event's record as `detail`. Each case
here builds a world that reaches one branch and checks that the kernel raises the outcome itself
(not a refusal the Python rule then words), that nothing changed, and that the Python rule on a copy
raises the same class with the same text (support/world.py `kernel_says_python_s_text`).
benchmarks/rule_shadow/text_check.py does the same on whole runs
(docs/rust-port/lanes/l8-texts-events.md).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from support.world import kernel_says_python_s_text

from sheltersim import native
from sheltersim.contracts.policies import selected_models
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import (
    DeliveryDelayHandler,
    EffectProgramPopulationHandler,
    FacilityOutageHandler,
    FacilityRecoveryHandler,
    InformationContactHandler,
    InformationCorrectionHandler,
    InformationRumorHandler,
    InfrastructureEventHandler,
    ProximityContactHandler,
    ResourceLedgerHandler,
    StaffRestorationHandler,
    StaffUnavailabilityHandler,
    WaterOutageHandler,
    WaterRestorationHandler,
    _current_information_contact_basis,
)
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _gate() -> Any:
    root = str(Path(__file__).resolve().parents[2] / "benchmarks")
    if root not in sys.path:
        sys.path.insert(0, root)
    import gate_scenarios

    return gate_scenarios


def _run(scenario: Any, minutes: int) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
    return world


def _event(
    world: WorldState, event_type: str, payload: dict[str, Any], **fields: Any
) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=fields.pop("event_id", f"test-{event_type.replace('_', '-')}"),
        kind=fields.pop("kind", EventKind.EXOGENOUS),
        due_minute=fields.pop("due_minute", world.minute_index),
        priority=0,
        sequence=fields.pop("sequence", 900_001),
        event_type=event_type,
        payload=payload,
        source_id=fields.pop("source_id", "SRC-TEST"),
        causation_id=fields.pop("causation_id", None),
        provenance=fields.pop("provenance", {"source_type": "assumption", "source_id": "SRC-TEST"}),
    )


@pytest.fixture(scope="module")
def small_world() -> WorldState:
    return _run(_gate().generated(evacuees=20, minutes=120), 5)


_CONDITIONS = {
    "scheduled": [
        {"kind": "power_outage", "component_id": "grid-main", "start_minute": 20, "end_minute": 60},
        {
            "kind": "equipment_failure",
            "facility_id": "water_station_main",
            "units_out": 1,
            "start_minute": 30,
            "end_minute": 70,
        },
        {"kind": "extreme_heat", "zone_id": "sleeping", "start_minute": 10, "end_minute": 50},
        {
            "kind": "staff_unavailable",
            "staff_id": "staff_001",
            "start_minute": 15,
            "end_minute": 45,
            "repeat_every_minutes": 40,
        },
        {
            "kind": "resource_delivery",
            "resource_id": "food_meal_units",
            "quantity": 40,
            "due_minute": 60,
        },
    ],
    "initial_resources": {
        "status": "entered",
        "food_meal_units": 20,
        "water_ml": 8_000,
        "source_kind": "operator",
    },
    "infrastructure": {
        "dependency_edges": [
            {"dependent_id": "toilet_01", "dependency_id": "panel-main"},
            {"dependent_id": "panel-main", "dependency_id": "grid-main"},
        ],
        "heat_stress_per_minute": 0.001,
        "cold_stress_per_minute": 0.001,
        "source_id": "GATE-SYNTHETIC-INPUT",
    },
}


@pytest.fixture(scope="module")
def operational_world() -> WorldState:
    """At minute 25: the power outage and the heat are active, staff_001 is away until 45."""
    gate = _gate()
    return _run(gate.generated(evacuees=20, minutes=120, operational_conditions=_CONDITIONS), 25)


def _says(handler: Any, world: WorldState, event: ScheduledEvent, text: str) -> None:
    import copy

    world = copy.deepcopy(world)
    name, said = kernel_says_python_s_text(handler, world, event)
    assert said == text, (name, said)


# ---- Facility outages and recoveries ------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "field", "payload", "text"),
    [
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "F"},
            "facility outage payload fields do not match",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": 3, "units_out": 1},
            "facility outage requires a facility_id",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "", "units_out": 1},
            "facility outage requires a facility_id",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "F", "units_out": True},
            "facility outage units_out must be an integer",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "F", "units_out": "1"},
            "facility outage units_out must be an integer",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "F", "units_out": -1},
            "facility outage units_out cannot be negative",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "nowhere", "units_out": 1},
            "facility 'nowhere' does not exist",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "F", "units_out": 0},
            "zero-unit facility outage has no effect",
        ),
        (
            FacilityOutageHandler(),
            "units_out",
            {"facility_id": "F", "units_out": 10_000},
            "facility outage exceeds currently working capacity",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": "F"},
            "facility recovery payload fields do not match",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": None, "units_restored": 1},
            "facility recovery requires a facility_id",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": "F", "units_restored": 1.0},
            "facility recovery units_restored must be an integer",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": "F", "units_restored": -2},
            "facility recovery units_restored cannot be negative",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": "it's", "units_restored": 1},
            """facility "it's" does not exist""",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": "F", "units_restored": 0},
            "zero-unit facility recovery has no effect",
        ),
        (
            FacilityRecoveryHandler(),
            "units_restored",
            {"facility_id": "F", "units_restored": 10_000},
            "facility recovery exceeds installed capacity",
        ),
    ],
)
def test_a_facility_change_s_text_is_python_s(
    small_world: WorldState, handler: Any, field: str, payload: dict[str, Any], text: str
) -> None:
    facility_id = sorted(small_world.facilities)[0]
    payload = {key: (facility_id if value == "F" else value) for key, value in payload.items()}
    event_type = "facility_outage" if field == "units_out" else "facility_restored"
    _says(handler, small_world, _event(small_world, event_type, payload), text)


def test_an_outage_under_an_infrastructure_outage_is_python_s_to_fail(
    operational_world: WorldState,
) -> None:
    affected = next(
        record["affected_facility_ids"][0]
        for record in operational_world.model_state["infrastructure_v1"]["outages"].values()
        if record["active"]
    )
    event = _event(operational_world, "facility_outage", {"facility_id": affected, "units_out": 1})
    _says(
        FacilityOutageHandler(),
        operational_world,
        event,
        "facility outage conflicts with an active infrastructure outage",
    )
    event = _event(
        operational_world, "facility_restored", {"facility_id": affected, "units_restored": 1}
    )
    _says(
        FacilityRecoveryHandler(),
        operational_world,
        event,
        "facility recovery conflicts with an active infrastructure outage",
    )


# ---- Proximity contacts --------------------------------------------------------------------------


def test_a_proximity_contact_without_its_model_is_python_s_to_fail(small_world: WorldState) -> None:
    event = _event(small_world, "proximity_contact_update", {})
    _says(
        ProximityContactHandler(),
        small_world,
        event,
        "proximity contact provenance or time is invalid",
    )


# ---- The resource ledger -----------------------------------------------------------------------


def test_a_delivery_of_an_unknown_shipment_is_python_s_to_fail(small_world: WorldState) -> None:
    payload = {
        "warehouse_id": "main",
        "resource_id": "water_ml",
        "quantity": 1,
        "shipment_id": "none",
    }
    event = _event(small_world, "resource_delivery", payload)
    _says(
        ResourceLedgerHandler("delivery"),
        small_world,
        event,
        "shipment delivery does not match operational state",
    )


# ---- Operational events ------------------------------------------------------------------------


def test_a_delay_s_texts_are_python_s(
    small_world: WorldState, operational_world: WorldState
) -> None:
    handler = DeliveryDelayHandler()
    event = _event(operational_world, "delivery_delayed", {"shipment_id": "x"})
    _says(handler, operational_world, event, "delivery delay payload fields or source are invalid")
    event = _event(
        operational_world, "delivery_delayed", {"shipment_id": "x", "to_minute": 30}, source_id=None
    )
    _says(handler, operational_world, event, "delivery delay payload fields or source are invalid")
    event = _event(small_world, "delivery_delayed", {"shipment_id": "x", "to_minute": 30})
    if "operational_events_v1" not in small_world.model_state:
        _says(handler, small_world, event, "delivery delay requires operational_events_v1 state")
    event = _event(
        operational_world, "delivery_delayed", {"shipment_id": "nothing", "to_minute": 30}
    )
    _says(handler, operational_world, event, "delivery delay references an unknown shipment")
    shipments = operational_world.model_state["operational_events_v1"]["shipments"]
    shipment_id = sorted(shipments)[0]
    for to_minute in (True, 26, 10_000):
        event = _event(
            operational_world,
            "delivery_delayed",
            {"shipment_id": shipment_id, "to_minute": to_minute},
        )
        _says(
            handler,
            operational_world,
            event,
            "shipment delay must move a not-yet-due delivery forward",
        )


def test_water_texts_are_python_s(operational_world: WorldState) -> None:
    outage = WaterOutageHandler()
    restoration = WaterRestorationHandler()
    event = _event(operational_world, "water_outage", {"outage_id": "w", "zone_id": "z", "x": 1})
    _says(outage, operational_world, event, "water outage payload fields or source are invalid")
    event = _event(operational_world, "water_outage", {"outage_id": "w", "zone_id": "z"})
    _says(outage, operational_world, event, "water outage does not match operational state")
    event = _event(operational_world, "water_restored", {"outage_id": "w"}, source_id=None)
    _says(
        restoration,
        operational_world,
        event,
        "water restoration payload fields or source are invalid",
    )
    event = _event(operational_world, "water_restored", {"outage_id": "w"})
    _says(restoration, operational_world, event, "water restoration does not match its outage")


def test_staff_absence_texts_are_python_s(operational_world: WorldState) -> None:
    absence = StaffUnavailabilityHandler()
    event = _event(operational_world, "staff_unavailable", {"staff_id": "staff_001"})
    _says(
        absence,
        operational_world,
        event,
        "staff unavailability payload fields or source are invalid",
    )
    event = _event(
        operational_world, "staff_unavailable", {"staff_id": "nobody", "until_minute": 50}
    )
    _says(absence, operational_world, event, "staff unavailability references unknown staff")
    for until in (False, 25, 10_000):
        event = _event(
            operational_world, "staff_unavailable", {"staff_id": "staff_001", "until_minute": until}
        )
        _says(absence, operational_world, event, "staff unavailable until_minute is invalid")
    status = operational_world.model_state["operational_events_v1"]["staff_status"]["staff_001"]
    event = _event(
        operational_world,
        "staff_unavailable",
        {"staff_id": "staff_001", "until_minute": 50},
        event_id=status["unavailability_event_id"],
    )
    _says(absence, operational_world, event, "staff unavailability was already applied")

    restoration = StaffRestorationHandler()
    event = _event(operational_world, "staff_restored", {"staff_id": "staff_001"})
    _says(
        restoration,
        operational_world,
        event,
        "staff restoration payload fields or source are invalid",
    )
    event = _event(
        operational_world, "staff_restored", {"staff_id": "nobody", "unavailability_event_id": "x"}
    )
    _says(restoration, operational_world, event, "staff restoration references unknown staff")
    event = _event(
        operational_world,
        "staff_restored",
        {"staff_id": "staff_001", "unavailability_event_id": "old"},
    )
    _says(
        restoration,
        operational_world,
        event,
        "staff restoration belongs to an older unavailability",
    )
    payload = {
        "staff_id": "staff_001",
        "unavailability_event_id": status["unavailability_event_id"],
    }
    event = _event(operational_world, "staff_restored", payload, event_id="not-the-token")
    _says(
        restoration, operational_world, event, "staff restoration event ID does not match its token"
    )


# ---- Infrastructure ------------------------------------------------------------------------------


def test_infrastructure_texts_are_python_s(
    small_world: WorldState, operational_world: WorldState
) -> None:
    handler = InfrastructureEventHandler()
    event = _event(operational_world, "solar_flare", {})
    _says(handler, operational_world, event, "unsupported infrastructure event type 'solar_flare'")
    event = _event(small_world, "power_outage", {"outage_id": "o"})
    _says(handler, small_world, event, "power_outage requires the opted-in infrastructure model")
    event = _event(operational_world, "power_outage", {"outage_id": "nothing"})
    _says(handler, operational_world, event, "infrastructure outage does not match scenario state")
    state = operational_world.model_state["infrastructure_v1"]
    active_id, active = next(
        (key, item) for key, item in state["outages"].items() if item["active"]
    )
    event = _event(
        operational_world,
        active["event_type"],
        {"outage_id": active_id},
        event_id=active["start_event_id"],
    )
    _says(
        handler, operational_world, event, "infrastructure outage is already active or lacks source"
    )
    other = "equipment_failure" if active["event_type"] == "power_outage" else "power_outage"
    event = _event(
        operational_world, other, {"outage_id": active_id}, event_id=active["start_event_id"]
    )
    _says(
        handler,
        operational_world,
        event,
        "infrastructure outage event type does not match scenario state",
    )
    event = _event(operational_world, "power_restored", {"outage_id": active_id})
    _says(
        handler,
        operational_world,
        event,
        "infrastructure restoration does not match an active outage",
    )
    event = _event(
        operational_world,
        "extreme_heat",
        {"stressor_id": "nothing", "zone_id": "sleeping", "until_minute": 40},
    )
    _says(
        handler, operational_world, event, "environmental stressor payload or duration is invalid"
    )
    event = _event(
        operational_world,
        "environmental_stressor_end",
        {"stressor_id": "nothing", "zone_id": "sleeping"},
    )
    _says(
        handler,
        operational_world,
        event,
        "environmental end event does not match an active stressor",
    )
    event = _event(operational_world, "supply_route_interruption", {"outage_id": "nothing"})
    _says(
        handler,
        operational_world,
        event,
        "supply route interruption payload or duration is invalid",
    )
    event = _event(
        operational_world, "supply_route_restored", {"outage_id": "nothing", "route_id": "r"}
    )
    _says(
        handler, operational_world, event, "route restoration does not match an active interruption"
    )


# ---- Population primitives ----------------------------------------------------------------------


def test_population_primitive_texts_are_python_s(small_world: WorldState) -> None:
    handler = EffectProgramPopulationHandler()
    event = _event(small_world, "population_primitive", {"op": "teleport"})
    _says(handler, small_world, event, "population primitive operation is invalid")
    event = _event(small_world, "population_primitive", {"op": 7})
    _says(handler, small_world, event, "population primitive operation is invalid")
    event = _event(small_world, "population_primitive", {"op": "depart_evacuees"})
    _says(handler, small_world, event, "population primitive payload fields do not match")
    payload = {
        "program_id": "p",
        "effect_id": "e",
        "op": "depart_evacuees",
        "zone_id": "z",
        "count": 1,
    }
    event = _event(small_world, "population_primitive", payload)
    _says(handler, small_world, event, "population primitive is not in the approved program")


# ---- Information events -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rumor_world() -> WorldState:
    """At minute 40: three claims, heard by many, no correction yet."""
    gate = _gate()
    return _run(gate.with_rumors(gate.generated(evacuees=20, minutes=120)), 40)


def test_an_information_event_without_the_model_is_python_s_to_fail(
    small_world: WorldState,
) -> None:
    import copy

    world = copy.deepcopy(small_world)
    world.model_configs["information_spread"] = {
        **world.model_configs["information_spread"],
        "enabled": False,
    }
    event = _event(world, "information_rumor", {})
    _says(InformationRumorHandler(), world, event, "information spread is not enabled for this run")


def test_rumor_texts_are_python_s(rumor_world: WorldState) -> None:
    handler = InformationRumorHandler()
    base = {"message_id": "claim-new", "truth_status": "true", "origin_agent_id": "A001"}
    for change, text, fields in (
        ({"x": 1}, "information_rumor payload fields do not match", {}),
        ({}, "information_rumor requires a source_id", {"source_id": None}),
        ({"message_id": 5}, "information_rumor requires stable message and Agent IDs", {}),
        ({"truth_status": "maybe"}, "information_rumor truth_status is invalid", {}),
        ({"truth_status": None}, "information_rumor truth_status is invalid", {}),
        ({"message_id": "claim-false"}, "information message ID already exists", {}),
        ({"origin_agent_id": "nobody"}, "information_rumor references an unknown Agent", {}),
    ):
        event = _event(rumor_world, "information_rumor", {**base, **change}, **fields)
        _says(handler, rumor_world, event, text)


def test_correction_texts_are_python_s(rumor_world: WorldState) -> None:
    handler = InformationCorrectionHandler()
    base = {"message_id": "claim-false", "zone_id": "sleeping", "belief": "rejected"}
    for change, text, fields in (
        ({"x": 1}, "information_correction payload fields do not match", {}),
        ({}, "information_correction requires a source_id", {"source_id": None}),
        ({"zone_id": 5}, "information_correction requires stable message and Zone IDs", {}),
        ({"belief": "maybe"}, "information_correction belief is invalid", {}),
        ({"belief": "heard"}, "information_correction must verify or reject a claim", {}),
        ({"zone_id": "nowhere"}, "information_correction references an unknown Zone", {}),
        ({"message_id": "nothing"}, "information_correction references an unknown message", {}),
        ({}, "information_correction must reference its rumor event", {"causation_id": "other"}),
        ({"belief": "verified"}, "information correction conflicts with message truth", {}),
    ):
        fields.setdefault("causation_id", "rumor-false")
        event = _event(rumor_world, "information_correction", {**base, **change}, **fields)
        _says(handler, rumor_world, event, text)


def _contact(
    world: WorldState, sender: str, recipient: str, message_id: str, **change: Any
) -> ScheduledEvent:
    state = world.model_state["information_spread_v1"]
    known = state["beliefs"][sender][message_id]
    payload = {
        "message_id": message_id,
        "sender_agent_id": sender,
        "recipient_agent_id": recipient,
        "belief": known["belief"],
        "contact_basis": _current_information_contact_basis(world, sender, recipient),
        "contact_minutes": 5,
    }
    provenance = {
        "source_type": "assumption",
        "source_id": "ASM-INFORMATION-SPREAD-V1",
        "policy_version": selected_models(world.model_configs)["information"],
        "transmission_probability": 0.5,
    }
    fields = {
        "kind": EventKind.ENDOGENOUS,
        "source_id": state["messages"][message_id]["source_id"],
        "causation_id": known["last_event_id"],
        "provenance": provenance,
    }
    for key, value in change.items():
        if key in payload:
            payload[key] = value
        elif key in provenance and key not in fields:
            provenance[key] = value
        else:
            fields[key] = value
    for key in [key for key, value in payload.items() if value is _DROP]:
        del payload[key]
    return _event(world, "information_contact_update", payload, **fields)


_DROP = object()


def test_contact_texts_are_python_s(rumor_world: WorldState) -> None:
    handler = InformationContactHandler()
    world = rumor_world
    beliefs = world.model_state["information_spread_v1"]["beliefs"]
    message_id = "claim-false"
    pairs = [
        (sender, recipient)
        for sender in sorted(beliefs)
        if message_id in beliefs[sender]
        for recipient in sorted(world.agents)
        if recipient != sender and world.agents[recipient].zone_id == world.agents[sender].zone_id
    ]
    sender, recipient = pairs[0]
    fresh = next((r for s, r in pairs if s == sender and message_id not in beliefs[r]), None)
    heard = next((r for s, r in pairs if s == sender and message_id in beliefs[r]), None)
    early = _run(_gate().with_rumors(_gate().generated(evacuees=20, minutes=120)), 8)
    early_beliefs = early.model_state["information_spread_v1"]["beliefs"]
    unaware = next(
        agent for agent in sorted(early.agents) if message_id not in early_beliefs[agent]
    )
    origin = next(agent for agent in sorted(early.agents) if message_id in early_beliefs[agent])
    cases = [
        ({"contact_minutes": _DROP}, "information_contact_update payload fields do not match"),
        ({"kind": EventKind.EXOGENOUS}, "information contact updates must be endogenous"),
        (
            {"due_minute": world.minute_index + 1},
            "information contact update must run at its due minute",
        ),
        (
            {"source_type": "observed"},
            "information contact provenance is missing its assumption ID",
        ),
        ({"recipient_agent_id": 5}, "information contact requires stable claim and Agent IDs"),
        ({"recipient_agent_id": sender}, "information contact requires two different Agents"),
        ({"recipient_agent_id": "nobody"}, "information contact references unknown state"),
        ({"belief": "maybe"}, "information contact belief is invalid"),
        ({"belief": "verified"}, "contact sender belief changed before transmission"),
        ({"contact_basis": "elsewhere"}, "sampled contact basis is no longer current"),
        ({"contact_minutes": 0}, "information contact duration is below its configured minimum"),
        ({"contact_minutes": True}, "information contact duration is below its configured minimum"),
        ({"transmission_probability": 0}, "information contact probability is invalid"),
        ({"transmission_probability": "x"}, "information contact probability is invalid"),
        ({"source_id": "OTHER"}, "information contact provenance does not match sender state"),
        ({"causation_id": "other"}, "information contact provenance does not match sender state"),
    ]
    for change, text in cases:
        target = fresh or recipient
        _says(handler, world, _contact(world, sender, target, message_id, **change), text)
    event = _contact(
        early, origin, unaware, message_id, sender_agent_id=unaware, recipient_agent_id=origin
    )
    _says(handler, early, event, "contact sender no longer knows the message")
    if heard is not None:
        event = _contact(world, sender, heard, message_id)
        _says(handler, world, event, "recipient already has an equally strong belief")
