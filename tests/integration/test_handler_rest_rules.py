"""The rest of events/handlers.py's kernels against their Python rules, on copies of a world.

benchmarks/rule_shadow/shadow_handlers_rest.py does this on the gate's runs; 7 days hold almost none
of these events, so here each rule also runs on Scenarios made to reach it, with the branches those
may not reach and the values that change a result (docs/rust-port/lanes/m6-handlers-rest.md).
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import SMALL_REPLAY

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask, Activity, SupportRequestState, SupportRequestStatus
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import (
    FacilityOutageHandler,
    InformationContactHandler,
    ProximityContactHandler,
    ResourceLedgerHandler,
    StaffSupportCompletionHandler,
    WaterRestorationHandler,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.arbitrate import ArbitratePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KERNELS = {
    "ProximityContactHandler": ("handlers", "proximity"),
    "InformationRumorHandler": ("information_events", "rumor"),
    "InformationCorrectionHandler": ("information_events", "correction"),
    "InformationContactHandler": ("information_events", "contact"),
    "ResourceLedgerHandler": ("handlers", "resource_ledger"),
    "FacilityOutageHandler": ("handlers", "facility_outage"),
    "FacilityRecoveryHandler": ("handlers", "facility_recovery"),
    "InfrastructureEventHandler": ("handlers", "infrastructure"),
    "DeliveryDelayHandler": ("handlers", "delivery_delay"),
    "WaterOutageHandler": ("handlers", "water_outage"),
    "WaterRestorationHandler": ("handlers", "water_restoration"),
    "StaffUnavailabilityHandler": ("handlers", "staff_unavailable"),
    "StaffRestorationHandler": ("handlers", "staff_restoration"),
    "StaffOperationArrivalHandler": ("staff_operation_events", "arrive"),
    "StaffOperationReliefHandler": ("staff_operation_events", "relief"),
    "StaffOperationCompletionHandler": ("staff_operation_events", "complete"),
    "SocialInteractionHandler": ("social_interaction", "apply"),
    "AgentTravelCompletionHandler": ("travel_completion", "apply"),
}
# These two take other events, compared with the shadow's rule only for these.
ONLY = {
    "SocialInteractionHandler": lambda event: event.payload.get("interaction_type")
    in {"complaint_response", "cooperation", "information_reaction", None},
    "AgentTravelCompletionHandler": lambda event: event.payload.get("purpose")
    in {"sleep_bed", "presence_relocation"},
}


def _gate() -> Any:
    root = str(Path(__file__).resolve().parents[2] / "benchmarks")
    if root not in sys.path:
        sys.path.insert(0, root)
    import gate_scenarios

    return gate_scenarios


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _outcome(call: Any) -> tuple[str, Any]:
    try:
        result = call()
    except EventSuppressed as raised:
        return "suppressed", str(raised)
    except EventApplicationError as raised:
        return "failed", str(raised)
    except native.Unsupported as raised:
        return "refused", str(raised)
    return "applied", [record.to_dict() for record in result or ()]


def _copies(world: WorldState) -> tuple[WorldState, WorldState]:
    document = world.to_dict()
    return WorldState.from_dict(document), WorldState.from_dict(document)


class _Comparing:
    """A handler that compares the kernel with the Python rule, then runs the real one."""

    def __init__(self, handler: Any, seen: Counter[str]) -> None:
        self.handler = handler
        self.seen = seen
        name = type(handler).__name__
        self.name = name
        self.kernel = getattr(native.rules(KERNELS[name][0]), KERNELS[name][1])
        self.rule = type(handler).apply.__wrapped__
        self.only = ONLY.get(name, lambda _event: True)

    def apply(self, world: WorldState, event: ScheduledEvent) -> Any:
        if self.only(event):
            python_world, kernel_world = _copies(world)
            expected = _outcome(lambda: self.rule(self.handler, python_world, event))
            actual = _outcome(lambda: self.kernel(self.handler, kernel_world, event))
            label = f"{event.event_type}.{expected[0]}"
            if actual[0] == "refused":
                assert expected[0] != "applied", f"{label}: {actual}"
                self.seen[f"{label}.refused"] += 1
            else:
                assert actual == expected, label
                assert _bytes(kernel_world) == _bytes(python_world), label
                for name, mirror in kernel_world.model_state._mirrors.items():
                    if name in kernel_world.model_state:
                        assert mirror.verify(), name
                self.seen[label] += 1
        return self.handler.apply(world, event)


def _run_comparing(scenario: Any, seen: Counter[str]) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    handlers = controller.event_dispatcher.handlers
    for event_type, handler in list(handlers.items()):
        if type(handler).__name__ in KERNELS:
            handlers[event_type] = _Comparing(handler, seen)
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(world.duration_minutes):
        engine.step()
    return world


def test_conditions_of_the_form_are_python_s() -> None:
    gate = _gate()
    conditions = {
        "scheduled": [
            {
                "kind": "power_outage",
                "component_id": "grid-main",
                "start_minute": 20,
                "end_minute": 60,
            },
            {
                "kind": "equipment_failure",
                "facility_id": "water_station_main",
                "units_out": 1,
                "start_minute": 30,
                "end_minute": 70,
            },
            {"kind": "extreme_heat", "zone_id": "sleeping", "start_minute": 10, "end_minute": 50},
            {"kind": "extreme_cold", "zone_id": "food", "start_minute": 55, "end_minute": 80},
            {
                "kind": "staff_unavailable",
                "staff_id": "staff_001",
                "start_minute": 15,
                "end_minute": 45,
                "repeat_every_minutes": 40,
            },
            {
                "kind": "resource_loss",
                "resource_id": "water_ml",
                "quantity": 2_000,
                "due_minute": 50,
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
                {"dependent_id": "accessible_toilet_01", "dependency_id": "panel-main"},
                {"dependent_id": "panel-main", "dependency_id": "grid-main"},
            ],
            "heat_stress_per_minute": 0.001,
            "cold_stress_per_minute": 0.001,
            "source_id": gate.SOURCE_ID,
        },
    }
    seen: Counter[str] = Counter()
    _run_comparing(
        gate.generated(evacuees=20, minutes=120, operational_conditions=conditions), seen
    )
    for event_type in (
        "power_outage",
        "power_restored",
        "equipment_failure",
        "equipment_repaired",
        "extreme_heat",
        "extreme_cold",
        "environmental_stressor_end",
        "staff_unavailable",
        "staff_restored",
        "resource_loss",
        "resource_delivery",
    ):
        assert seen[f"{event_type}.applied"] > 0, (event_type, seen)


def test_rumors_and_corrections_are_python_s() -> None:
    gate = _gate()
    seen: Counter[str] = Counter()
    _run_comparing(gate.with_rumors(gate.generated(evacuees=20, minutes=120)), seen)
    for event_type in ("information_rumor", "information_correction", "information_contact_update"):
        assert seen[f"{event_type}.applied"] > 0, (event_type, seen)


@pytest.mark.parametrize(
    "name",
    ["multi_operational", "water_intervention", "failure_day3", "delivery_intervention"],
)
def test_the_operational_fixtures_are_python_s(name: str) -> None:
    gate = _gate()
    moved = {
        "resource_delivery": 20,
        "delivery_delayed": 10,
        "water_outage": 30,
        "water_restored": 50,
        "facility_outage": 20,
        "facility_restored": 60,
    }
    seen: Counter[str] = Counter()
    _run_comparing(gate.fixture_short(name, minutes=100, agents=20, remap=moved), seen)
    assert sum(seen.values()) > 0


def _small_world(generated_scenario: Any) -> tuple[WorldState, OfflineSimulationController]:
    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(5):
        engine.step()
    return world, controller


def _event(
    world: WorldState, event_type: str, payload: dict[str, Any], **fields: Any
) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=fields.pop("event_id", f"test-{event_type.replace('_', '-')}"),
        kind=fields.pop("kind", EventKind.EXOGENOUS),
        due_minute=world.minute_index,
        priority=0,
        sequence=fields.pop("sequence", 900_001),
        event_type=event_type,
        payload=payload,
        source_id=fields.pop("source_id", "SRC-TEST"),
        causation_id=fields.pop("causation_id", None),
        provenance=fields.pop("provenance", {"source_type": "assumption", "source_id": "SRC-TEST"}),
    )


# ---- What a kernel leaves to the Python rule ----------------------------------------------------


def test_a_refused_outage_changes_nothing_and_python_says_why(generated_scenario: Any) -> None:
    world, _ = _small_world(generated_scenario)
    facility_id = sorted(world.facilities)[0]
    handler = FacilityOutageHandler()
    for payload, text in (
        ({"facility_id": facility_id}, "payload fields"),
        ({"facility_id": facility_id, "units_out": 0}, "zero-unit"),
        ({"facility_id": facility_id, "units_out": -1}, "negative"),
        ({"facility_id": "nowhere", "units_out": 1}, "does not exist"),
        ({"facility_id": facility_id, "units_out": 10_000}, "exceeds"),
    ):
        event = _event(world, "facility_outage", payload)
        before = _bytes(world)
        with pytest.raises(native.Unsupported):
            native.rules("handlers").facility_outage(handler, world, event)
        assert _bytes(world) == before
        with pytest.raises((EventApplicationError, EventSuppressed), match=text):
            handler.apply(world, event)
        assert _bytes(world) == before


def test_a_stale_proximity_sample_is_python_s_to_suppress(generated_scenario: Any) -> None:
    world, _ = _small_world(generated_scenario)
    handler = ProximityContactHandler()
    event = _event(world, "proximity_contact_update", {})
    with pytest.raises(native.Unsupported):
        native.rules("handlers").proximity(handler, world, event)
    with pytest.raises(EventApplicationError):
        handler.apply(world, event)


def test_a_contact_that_no_longer_applies_is_python_s(generated_scenario: Any) -> None:
    world, _ = _small_world(generated_scenario)
    handler = InformationContactHandler()
    event = _event(world, "information_contact_update", {"message_id": "nothing"})
    with pytest.raises(native.Unsupported):
        native.rules("information_events").contact(handler, world, event)
    with pytest.raises(EventApplicationError):
        handler.apply(world, event)


def test_a_restoration_of_no_outage_is_python_s(generated_scenario: Any) -> None:
    world, _ = _small_world(generated_scenario)
    handler = WaterRestorationHandler()
    event = _event(world, "water_restored", {"outage_id": "nothing"})
    before = _bytes(world)
    with pytest.raises(native.Unsupported):
        native.rules("handlers").water_restoration(handler, world, event)
    assert _bytes(world) == before


def test_a_patched_handler_is_what_the_loop_calls(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller = _small_world(generated_scenario)
    called: list[str] = []

    def fake(_self: Any, _world: Any, event: ScheduledEvent) -> None:
        called.append(event.event_id)

    monkeypatch.setattr(ResourceLedgerHandler, "apply", fake)
    event = _event(
        world,
        "resource_delivery",
        {"warehouse_id": "warehouse_main", "resource_id": "water_ml", "quantity": 1},
        event_id="delivery-now",
    )
    world.schedule_event(event)
    TickEngine(world, phase_handler=controller).step()
    assert called == ["delivery-now"]
    assert world.event_log.find("delivery-now").status is EventStatus.APPLIED


# ---- Care that a selected formula decides --------------------------------------------------


def _assigned_care(world: WorldState, kind: str) -> tuple[ScheduledEvent, str]:
    agent_id = sorted(world.agents)[0]
    staff_id = sorted(world.staff)[0]
    person = world.staff[staff_id]
    agent = world.agents[agent_id]
    task_id = "staff-support-test"
    for key in list(world.active_tasks):
        task = world.active_tasks[key]
        if task.agent_id == agent_id or task.staff_id == staff_id:
            del world.active_tasks[key]
    agent.support_request_id = "support-t1"
    agent.activity = Activity.SEEKING_HELP
    agent.target_id = None
    agent.zone_id = person.zone_id
    agent.stress = 0.5
    world.support_requests["support-t1"] = SupportRequestState(
        "support-t1", agent_id, world.minute_index - 1, world.minute_index + 50
    )
    request = world.support_requests["support-t1"]
    from sheltersim.domain import care_receipts

    common = {
        "request_id": "support-t1",
        "agent_id": agent_id,
        "household_id": None,
        "support_kind": kind,
    }
    care_receipts.append_applied_care_transition(world, **common, status="requested")
    care_receipts.append_applied_care_transition(
        world, **common, status="assigned", staff_id=staff_id
    )
    care_receipts.append_applied_care_transition(
        world, **common, status="started", staff_id=staff_id
    )
    request.status = SupportRequestStatus.ASSIGNED
    request.assigned_staff_id = staff_id
    request.task_id = task_id
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="staff_support",
        agent_id=agent_id,
        resource_id=None,
        start_minute=world.minute_index,
        due_minute=world.minute_index,
        staff_id=staff_id,
    )
    person.active_task_id = task_id
    person.work_queue[:] = [task_id]
    person.busy_until = world.minute_index
    return (
        ScheduledEvent(
            event_id="staff-support-complete-test",
            kind=EventKind.COMPLETION,
            due_minute=world.minute_index,
            priority=0,
            sequence=900_002,
            event_type="staff_support_complete",
            payload={
                "request_id": "support-t1",
                "agent_id": agent_id,
                "staff_id": staff_id,
                "task_id": task_id,
                "support_duration_minutes": 10,
            },
            source_id=task_id,
            provenance={"source_type": "assumption", "source_id": "ASM-TEST"},
        ),
        agent_id,
    )


def _care_is_python_s(world: WorldState, kind: str) -> dict[str, Any]:
    event, _ = _assigned_care(world, kind)
    twin = copy.deepcopy(world)
    handler = StaffSupportCompletionHandler(cooldown_minutes=30, require_colocated=True)
    records = native.rules("support").complete_support(handler, world, event)
    expected = StaffSupportCompletionHandler.apply.__wrapped__(handler, twin, event)
    assert [item.to_dict() for item in records] == [item.to_dict() for item in expected]
    assert _bytes(world) == _bytes(twin)
    assert world._core.mirror("body_state_v1").verify()
    return records[0].event.payload


TYPED_KINDS = (
    "caregiver_assistance",
    "dependent_assistance",
    "privacy_accommodation",
    "mobility_assistance",
    "language_access",
    "medical_assessment",
    "medication_support",
    "hygiene_support",
)


@pytest.mark.parametrize("kind", TYPED_KINDS)
@pytest.mark.parametrize(
    "model_id", [model for domain, model in _gate().selectable_models() if domain == "care_outcome"]
)
def test_a_kind_a_selected_formula_decides_is_python_s(
    generated_scenario: Any, model_id: str, kind: str
) -> None:
    registry = generated_scenario.model_configs["policy_registry"]
    registry.pop("suite_id", None)
    registry["care_outcome"] = model_id
    world, _ = _small_world(generated_scenario)
    payload = _care_is_python_s(world, kind)
    assert payload["status"] == "completed"


def test_care_with_a_dependent_is_python_s(generated_scenario: Any) -> None:
    registry = generated_scenario.model_configs["policy_registry"]
    registry.pop("suite_id", None)
    registry["care_outcome"] = "typed-care-support-response-v1"
    world, _ = _small_world(generated_scenario)
    caregiver, dependent = sorted(world.agents)[:2]
    context = {
        "caregiver": {
            "household_id": "home-1",
            "care_for_ids": [dependent],
            "source_ids": ["SRC-CAREGIVER"],
        },
        "dependent": {
            "household_id": "home-1",
            "caregiver_ids": [caregiver],
            "source_ids": ["SRC-DEPENDENT"],
        },
    }
    world.model_configs["population_context"] = {
        "people": {caregiver: context["caregiver"], dependent: context["dependent"]}
    }
    payload = _care_is_python_s(world, "caregiver_assistance")
    assert payload["outcome_observation"] == "observed_change"
    assert {change["entity_id"] for change in payload["state_changes"]} == {caregiver, dependent}
    assert "SRC-DEPENDENT" in payload["source_ids"]


def test_a_care_delta_1_percent_off_shows(generated_scenario: Any) -> None:
    registry = generated_scenario.model_configs["policy_registry"]
    registry.pop("suite_id", None)
    registry["care_outcome"] = "typed-care-support-response-v1"
    world, _ = _small_world(generated_scenario)
    event, agent_id = _assigned_care(world, "mobility_assistance")
    twin = copy.deepcopy(world)
    twin.model_configs["body_dynamics"]["mobility_assistance_stress_relief"] = 0.03 * 1.01
    handler = StaffSupportCompletionHandler(cooldown_minutes=30, require_colocated=True)
    native.rules("support").complete_support(handler, world, event)
    StaffSupportCompletionHandler.apply.__wrapped__(handler, twin, event)
    assert world.agents[agent_id].stress != twin.agents[agent_id].stress


# ---- The assignment of a request -----------------------------------------------------------------


def _route(minutes: int) -> Any:
    return SimpleNamespace(travel_minutes=minutes)


def _assign_both(
    world: WorldState, *, resolver: Any = None, scheduler: Any = None
) -> tuple[Any, WorldState]:
    twin = copy.deepcopy(world)
    expected = StaffService.assign_pending.__wrapped__(
        StaffService(twin, route_resolver=resolver, travel_scheduler=scheduler),
        world.minute_index,
        validate_state=False,
    )
    actual = native.rules("staff").assign_work(
        StaffService(world, route_resolver=resolver, travel_scheduler=scheduler),
        world.minute_index,
        validate_state=False,
    )
    assert actual == expected
    assert _bytes(world) == _bytes(twin)
    return actual, twin


def _staffed_world() -> WorldState:
    """A world whose staff are all free, with no shifts to read."""
    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = 200
    document["scheduled_events"] = []
    document["model_configs"]["staff_operations"] = {}
    document["model_configs"]["shared_operations"] = {
        "schema_version": 1,
        "policy_version": "shared-operations-v1",
    }
    template = document["staff"][0]
    document["staff"] = [
        {**template, "id": f"S{index:02}", "skills": ["support"]} for index in range(1, 4)
    ]
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    initialize_simulator_extensions(world)
    world.minute_index = 10
    return world


def _open_request(world: WorldState, *, age: int = 0) -> str:
    agent_id = sorted(world.agents)[0]
    agent = world.agents[agent_id]
    for key in list(world.active_tasks):
        if world.active_tasks[key].agent_id == agent_id:
            del world.active_tasks[key]
    agent.activity = Activity.SEEKING_HELP
    agent.target_id = None
    agent.support_request_id = "support-t1"
    world.support_requests["support-t1"] = SupportRequestState(
        "support-t1", agent_id, world.minute_index - age, world.minute_index + 50
    )
    return "support-t1"


def test_a_free_worker_takes_a_request_as_in_python() -> None:
    world = _staffed_world()
    request_id = _open_request(world)
    assigned, _ = _assign_both(world)
    assert request_id in assigned
    assert world.support_requests[request_id].status is SupportRequestStatus.ASSIGNED


def test_an_old_request_is_promoted_as_in_python() -> None:
    world = _staffed_world()
    request_id = _open_request(world, age=5)
    twin = copy.deepcopy(world)
    args = {"fairness_after_minutes": 3}
    expected = StaffService.assign_pending.__wrapped__(
        StaffService(twin, **args), world.minute_index, validate_state=False
    )
    actual = native.rules("staff").assign_work(
        StaffService(world, **args), world.minute_index, validate_state=False
    )
    assert actual == expected == (request_id,)
    assert _bytes(world) == _bytes(twin)
    assert world.metric_state["support_fairness_promoted_assignment_count"] >= 1.0


@pytest.mark.parametrize(
    ("minutes", "scheduled", "reason"),
    [
        (None, True, "route_unavailable_at_assignment"),
        (10**6, True, "insufficient_run_time"),
        (3, False, "resident_travel_schedule_rejected"),
        (3, True, None),
    ],
)
def test_a_route_decides_as_in_python(
    minutes: int | None, scheduled: bool, reason: str | None
) -> None:
    world = _staffed_world()
    request_id = _open_request(world)
    route = None if minutes is None else _route(minutes)
    _assign_both(world, resolver=lambda *_: route, scheduler=lambda *_: scheduled)
    assert world.support_requests[request_id].last_assignment_wait_reason_code == reason


def test_nobody_free_leaves_the_plain_kernel_running() -> None:
    world = _staffed_world()
    _open_request(world)
    for person in world.staff.values():
        person.active_task_id = "elsewhere"
    service = StaffService(world)
    assert (
        native.rules("staff").assign_none(service, world.minute_index, validate_state=False) == ()
    )


# ---- What else the handlers ask ------------------------------------------------------------------


def test_only_the_due_requests_go_to_the_policy(generated_scenario: Any) -> None:
    world, _ = _small_world(generated_scenario)
    request_id = _open_request(world, age=7)
    asked: list[tuple[str, str]] = []
    policy = SimpleNamespace(
        limits=SimpleNamespace(complaint_wait_minutes=7),
        consider_complaint_response=lambda _world, *, agent_id, request_id: asked.append(
            (agent_id, request_id)
        ),
    )
    controller = SimpleNamespace(world=world, social_policy=policy)
    native.rules("staff").consider_due_complaints(controller, world.minute_index)
    from_python = list(asked)
    asked.clear()
    ArbitratePhase._consider_due_complaints(controller, world.minute_index)
    assert from_python == asked == [(world.support_requests[request_id].agent_id, request_id)]
    asked.clear()
    native.rules("staff").consider_due_complaints(controller, world.minute_index + 1)
    assert asked == []
    controller.social_policy = None
    native.rules("staff").consider_due_complaints(controller, world.minute_index)


def test_ingest_of_a_record_of_this_tick_is_python_s(generated_scenario: Any) -> None:
    from sheltersim.domain import care_receipts

    world, _ = _small_world(generated_scenario)
    facility_id = sorted(world.facilities)[0]
    record = care_receipts.make_facility_recovery_record(
        world,
        facility_id=facility_id,
        component_id="component-1",
        status="degraded",
        previous_working_units=2,
        working_units=1,
        causation_id="cause-1",
    )[0]
    world.event_log.append(record)
    twin = copy.deepcopy(world)
    native.rules("care_receipts").ingest(world, (record,))
    care_receipts.ingest_applied_shared_operation_records.__wrapped__(twin, (record,))
    assert _bytes(world) == _bytes(twin)
    # Recorded once: the second time it is found.
    native.rules("care_receipts").ingest(world, (record,))
    assert _bytes(world) == _bytes(twin)


def test_the_kernels_are_what_ran(generated_scenario: Any) -> None:
    world, _ = _small_world(generated_scenario)
    before, _ = native.rules("handlers").stats()
    handler = FacilityOutageHandler()
    facility_id = next(key for key, item in sorted(world.facilities.items()) if item.working_units)
    event = _event(world, "facility_outage", {"facility_id": facility_id, "units_out": 1})
    handler.apply(world, event)
    after, _ = native.rules("handlers").stats()
    assert after["facility_outage"] == before.get("facility_outage", 0) + 1
