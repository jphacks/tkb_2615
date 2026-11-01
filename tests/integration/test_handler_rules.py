"""The handlers' kernels against their Python rules, on a world and a copy of it made before.

rules/care_receipts.rs, rules/toilet.rs, rules/support.rs, `after_arrival` of
rules/travel_completion.rs and the dispatch table's new entries. benchmarks/rule_shadow/
shadow_handlers.py does this on the runs of the gate; here, the branches those runs may not reach
and the values that change the result (docs/rust-port/lanes/m6-handlers.md).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain import care_receipts
from sheltersim.domain.facilities import ToiletServiceManager
from sheltersim.domain.models import ActiveTask, Activity, SupportRequestState, SupportRequestStatus
from sheltersim.domain.staff import StaffService
from sheltersim.events.handlers import (
    AgentTravelCompletionHandler,
    StaffSupportCompletionHandler,
    SupportRequestTimeoutHandler,
    ToiletServiceCompletionHandler,
)
from sheltersim.events.models import EventKind, EventStatus, EventSuppressed, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.dispatch import DispatchPhase
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.behavior import ActionKind

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _run(scenario: Any, minutes: int) -> tuple[WorldState, OfflineSimulationController]:
    world = WorldState.from_scenario(scenario)
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
    return world, controller


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _same_world(world: WorldState, twin: WorldState) -> None:
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert [repr(event) for event in world.event_queue.pending()] == [
        repr(event) for event in twin.event_queue.pending()
    ]
    assert [record.to_dict() for record in world.event_log.records] == [
        record.to_dict() for record in twin.event_log.records
    ]
    assert world._core.entities().verify(world) is None
    for name in ("shared_operations_v1", "body_state_v1"):
        assert world._core.mirror(name).verify()


def _idle_person(world: WorldState) -> str:
    busy = {task.agent_id for task in world.active_tasks.values()}
    journeys = world.model_state["movement_v1"]["journeys"]
    for agent_id in sorted(world.agents):
        agent = world.agents[agent_id]
        queued = any(
            agent_id in facility.queue_agent_ids or agent_id in facility.active_agent_ids
            for facility in world.facilities.values()
        )
        if (
            agent_id not in busy
            and agent_id not in journeys
            and not queued
            and agent.support_request_id is None
            and agent.support_cooldown_until <= world.minute_index
            and agent.activity in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING)
        ):
            return agent_id
    pytest.fail("nobody is idle")


def _toilet(world: WorldState) -> str:
    return next(key for key, item in sorted(world.facilities.items()) if "toilet" in item.kind)


def _manager_on(controller: Any, world: WorldState) -> ToiletServiceManager:
    manager = ToiletServiceManager.__new__(ToiletServiceManager)
    manager.__dict__.update(controller.toilet_service.__dict__)
    manager.world = world
    return manager


# ---- ToiletServiceManager.request ----------------------------------------------------------------


def test_a_toilet_request_is_python_s(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    twin = copy.deepcopy(world)
    agent_id = _idle_person(world)
    facility_id = _toilet(world)
    kernel = native.rules("toilet").request
    receipt = kernel(controller.toilet_service, agent_id, facility_id, validate_state=False)
    expected = ToiletServiceManager.request.__wrapped__(
        _manager_on(controller, twin), agent_id, facility_id, validate_state=False
    )
    assert repr(receipt) == repr(expected)
    assert world.agents[agent_id].activity is Activity.QUEUED
    _same_world(world, twin)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda world, agent_id: setattr(world.agents[agent_id], "activity", Activity.MOVING),
        lambda world, agent_id: world.facilities[_toilet(world)].queue_agent_ids.append(agent_id),
    ],
)
def test_what_a_toilet_request_refuses_is_python_s_text(
    generated_scenario: Any, spoil: Any
) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    spoil(world, agent_id)
    before = _bytes(world)
    with pytest.raises(native.Unsupported):
        native.rules("toilet").request(
            controller.toilet_service, agent_id, _toilet(world), validate_state=False
        )
    assert _bytes(world) == before
    with pytest.raises(ValueError, match="toilet"):
        controller.toilet_service.request(agent_id, _toilet(world), validate_state=False)


def test_a_request_with_the_world_s_validation_is_python_s(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    with pytest.raises(native.Unsupported, match="validation"):
        native.rules("toilet").request(
            controller.toilet_service, _idle_person(world), _toilet(world), validate_state=True
        )


def test_the_facilities_to_serve_leave_out_only_an_empty_queue(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    manager = controller.toilet_service
    for facility in world.facilities.values():
        facility.queue_agent_ids.clear()
    assert native.rules("toilet").facilities_to_serve(manager) == []
    facility_id = _toilet(world)
    world.facilities[facility_id].queue_agent_ids.append(_idle_person(world))
    assert native.rules("toilet").facilities_to_serve(manager) == [world.facilities[facility_id]]
    python_rule = ToiletServiceManager._facilities_to_serve.__wrapped__(manager)
    assert world.facilities[facility_id] in python_rule


# ---- ToiletServiceCompletionHandler --------------------------------------------------------------


def _in_service(world: WorldState, due: int) -> ScheduledEvent:
    agent_id = _idle_person(world)
    facility_id = _toilet(world)
    agent = world.agents[agent_id]
    agent.activity = Activity.IN_SERVICE
    agent.target_id = facility_id
    world.facilities[facility_id].active_agent_ids.append(agent_id)
    task_id = "toilet-service-test"
    world.active_tasks[task_id] = ActiveTask(
        id=task_id,
        kind="toilet_service",
        agent_id=agent_id,
        resource_id=facility_id,
        start_minute=world.minute_index,
        due_minute=due,
    )
    return ScheduledEvent(
        event_id="toilet-complete-test",
        kind=EventKind.COMPLETION,
        due_minute=due,
        priority=0,
        sequence=900_000,
        event_type="toilet_service_complete",
        payload={"facility_id": facility_id, "agent_id": agent_id, "task_id": task_id},
        source_id=task_id,
        provenance={"source_type": "assumption", "source_id": "ASM-TEST"},
    )


@pytest.mark.parametrize("hygiene", [0.75, 0.2, 0.1, 0.0])
def test_a_toilet_completion_is_python_s(generated_scenario: Any, hygiene: float) -> None:
    world, _ = _run(generated_scenario, 5)
    event = _in_service(world, world.minute_index)
    agent_id = event.payload["agent_id"]
    world.model_state["body_state_v1"]["agents"][agent_id]["hygiene_need"] = hygiene
    twin = copy.deepcopy(world)
    native.rules("toilet").complete(ToiletServiceCompletionHandler(), world, event)
    ToiletServiceCompletionHandler.apply.__wrapped__(ToiletServiceCompletionHandler(), twin, event)
    assert world.agents[agent_id].activity is Activity.IDLE
    assert world.agents[agent_id].needs.toilet_urgency == 0.0
    # 0.2 is the model's relief: a change of 1% shows here.
    assert world.model_state["body_state_v1"]["agents"][agent_id]["hygiene_need"] == max(
        0.0, hygiene - 0.2
    )
    _same_world(world, twin)


def test_a_toilet_completion_that_does_not_fit_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    event = _in_service(world, world.minute_index)
    world.agents[event.payload["agent_id"]].activity = Activity.IDLE
    before = _bytes(world)
    with pytest.raises(native.Unsupported, match="does not match"):
        native.rules("toilet").complete(ToiletServiceCompletionHandler(), world, event)
    assert _bytes(world) == before


# ---- care receipts -------------------------------------------------------------------------------


def _open_request(world: WorldState, agent_id: str, *, request_id: str = "support-t1") -> None:
    world.support_requests[request_id] = SupportRequestState(
        id=request_id,
        agent_id=agent_id,
        requested_minute=world.minute_index,
        timeout_minute=world.minute_index + 30,
    )
    world.agents[agent_id].support_request_id = request_id
    world.agents[agent_id].activity = Activity.SEEKING_HELP


def _transition(world: WorldState, *, status: str = "requested", **more: Any) -> dict[str, Any]:
    return {
        "request_id": "support-t1",
        "agent_id": sorted(world.agents)[0],
        "household_id": None,
        "support_kind": "emotional_support",
        "status": status,
        "source_ids": ("ASM-Z", "ASM-A", "ASM-Z"),
        **more,
    }


def test_a_care_transition_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    twin = copy.deepcopy(world)
    native.rules("care_receipts").append_transition(world, **_transition(world))
    care_receipts.append_applied_care_transition.__wrapped__(twin, **_transition(twin))
    last = world.event_log.records[-1]
    assert last.event.event_type == "care_support_transition"
    assert last.detail == "care transition requested recorded after state application"
    # Sorted and without repeats, with the producer's own source.
    assert last.event.payload["source_ids"] == (
        "ASM-A",
        "ASM-CARE-TRANSITION-PRODUCER-V1",
        "ASM-Z",
    )
    _same_world(world, twin)


@pytest.mark.parametrize(
    ("more", "why"),
    [
        ({"support_kind": "unknown"}, "contract"),
        ({"request_id": "bad id"}, "contract"),
        ({"status": "unknown"}, "status"),
        ({"status": "completed"}, "order"),
    ],
)
def test_what_a_care_transition_refuses_changes_nothing(
    generated_scenario: Any, more: dict[str, Any], why: str
) -> None:
    world, _ = _run(generated_scenario, 5)
    before = _bytes(world)
    counters = dict(world.id_counters)
    with pytest.raises(native.Unsupported, match=why):
        native.rules("care_receipts").append_transition(world, **_transition(world, **more))
    assert _bytes(world) == before
    assert world.id_counters == counters


def test_a_request_id_used_before_is_python_s_error(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    kernel = native.rules("care_receipts").append_transition
    kernel(world, **_transition(world))
    kernel(world, **_transition(world, status="interrupted"))
    with pytest.raises(native.Unsupported, match="used before"):
        kernel(world, **_transition(world))
    with pytest.raises(ValueError, match="reused"):
        care_receipts.append_applied_care_transition(world, **_transition(world))


def test_without_the_namespace_nothing_happens(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    del world.model_state["shared_operations_v1"]
    counters = dict(world.id_counters)
    assert native.rules("care_receipts").append_transition(world, **_transition(world)) is None
    assert world.id_counters == counters


@pytest.mark.parametrize(
    "person",
    [
        {},
        {"source_ids": ["S-B", "S-A", 3]},
        {"care_support_needs": ["language_access"]},
        {"care_support_needs": ["hygiene_support", "language_access"]},
        {"medication_support": True},
        {"medical_support": True},
        {"medical_support": 1},
        "not a person",
    ],
)
def test_what_a_seek_reads_of_a_person_is_python_s(generated_scenario: Any, person: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    configs = world.model_configs.setdefault("population_context", {})
    configs.setdefault("people", {})[agent_id] = person
    twin = copy.deepcopy(world)
    proposal = _proposal(ActionKind.SEEK_HELP, agent_id, world)
    result = native.rules("proposals").apply_effect(controller, proposal)
    expected = _python_seek(controller, twin, proposal)
    assert list(result) == list(expected)
    _same_world(world, twin)


# ---- seek_help and seek_information --------------------------------------------------------------


def _proposal(action: ActionKind, agent_id: str, world: WorldState) -> Any:
    class Proposal:
        pass

    proposal = Proposal()
    proposal.action = action
    proposal.agent_id = agent_id
    proposal.next_decision_at = world.minute_index + 7
    proposal.target_id = None
    return proposal


def _python_seek(controller: Any, twin: WorldState, proposal: Any) -> Any:
    class Step(ProposePhase):
        pass

    step = Step()
    step.__dict__.update(controller.__dict__)
    step.world = twin
    service = StaffService.__new__(StaffService)
    service.__dict__.update(controller.staff_service.__dict__)
    service.world = twin
    step.staff_service = service
    return step._apply_effect(proposal)


@pytest.mark.parametrize("action", [ActionKind.SEEK_HELP, ActionKind.SEEK_INFORMATION])
def test_a_seek_is_python_s(generated_scenario: Any, action: ActionKind) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    twin = copy.deepcopy(world)
    proposal = _proposal(action, agent_id, world)
    result = native.rules("proposals").apply_effect(controller, proposal)
    expected = _python_seek(controller, twin, proposal)
    assert list(result) == list(expected)
    assert result[2] is True
    assert world.agents[agent_id].activity is Activity.SEEKING_HELP
    assert result[0] in world.support_requests
    _same_world(world, twin)


def test_a_seek_in_cooldown_counts_as_rejected(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    world.agents[agent_id].support_cooldown_until = world.minute_index + 1
    twin = copy.deepcopy(world)
    proposal = _proposal(ActionKind.SEEK_HELP, agent_id, world)
    result = native.rules("proposals").apply_effect(controller, proposal)
    expected = _python_seek(controller, twin, proposal)
    assert list(result) == list(expected)
    assert result[0] is None and result[2] is False
    assert world.metric_state["support_request_rejected_count"] >= 1.0
    _same_world(world, twin)


def test_a_seek_reaches_the_information_rule_above_its_threshold(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    if controller.information_policy is None:
        pytest.skip("this Scenario has no information policy")
    agent_id = _idle_person(world)
    state = world.model_state["information_spread_v1"]
    state["query_pressure"][agent_id] = controller.information_policy.config.query_threshold
    twin = copy.deepcopy(world)
    proposal = _proposal(ActionKind.SEEK_HELP, agent_id, world)
    result = native.rules("proposals").apply_effect(controller, proposal)
    expected = _python_seek(controller, twin, proposal)
    assert list(result) == list(expected)
    _same_world(world, twin)


# ---- the timeout ---------------------------------------------------------------------------------


def _timeout_event(world: WorldState, agent_id: str, request_id: str = "support-t1") -> Any:
    return ScheduledEvent(
        event_id="support-timeout-test",
        kind=EventKind.COMPLETION,
        due_minute=world.minute_index,
        priority=20,
        sequence=900_001,
        event_type="support_request_timeout",
        payload={"request_id": request_id, "agent_id": agent_id},
        source_id=request_id,
        provenance={"source_type": "assumption", "source_id": "ASM-TEST"},
    )


def test_a_timeout_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    event = _timeout_event(world, agent_id)
    twin = copy.deepcopy(world)
    handler = SupportRequestTimeoutHandler(cooldown_minutes=30)
    native.rules("support").timeout(handler, world, event)
    SupportRequestTimeoutHandler.apply.__wrapped__(handler, twin, event)
    request = world.support_requests["support-t1"]
    assert request.status is SupportRequestStatus.UNRESOLVED
    assert request.cooldown_until == world.minute_index + 30
    assert world.agents[agent_id].activity is Activity.IDLE
    _same_world(world, twin)


def test_a_timeout_of_what_has_left_pending_says_python_s_text(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    world.support_requests["support-t1"].status = SupportRequestStatus.ASSIGNED
    event = _timeout_event(world, agent_id)
    handler = SupportRequestTimeoutHandler(cooldown_minutes=30)
    before = _bytes(world)
    with pytest.raises(EventSuppressed) as by_python:
        SupportRequestTimeoutHandler.apply.__wrapped__(handler, world, event)
    with pytest.raises(EventSuppressed) as by_kernel:
        native.rules("support").timeout(handler, world, event)
    assert str(by_kernel.value) == str(by_python.value)
    assert _bytes(world) == before


def test_a_timeout_writes_through_the_request_s_listener(generated_scenario: Any) -> None:
    # The end-of-tick check reads a settled request again only when it reports a write.
    world, _ = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    listener: list[Any] = []
    world.support_requests["support-t1"].report_writes_to(listener)
    handler = SupportRequestTimeoutHandler(cooldown_minutes=30)
    native.rules("support").timeout(handler, world, _timeout_event(world, agent_id))
    assert listener


def test_a_timeout_of_another_person_s_request_is_python_s_error(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    other = next(key for key in sorted(world.agents) if key != agent_id)
    handler = SupportRequestTimeoutHandler(cooldown_minutes=30)
    with pytest.raises(native.Unsupported, match="another person"):
        native.rules("support").timeout(handler, world, _timeout_event(world, other))


# ---- a staff member's support ends ---------------------------------------------------------------


def _assigned(world: WorldState, kind: str) -> tuple[Any, str]:
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    kernel = native.rules("care_receipts").append_transition
    common = {"request_id": "support-t1", "agent_id": agent_id, "household_id": None}
    common["support_kind"] = kind
    staff_id = sorted(world.staff)[0]
    person = world.staff[staff_id]
    kernel(world, **common, status="requested")
    kernel(world, **common, status="assigned", staff_id=staff_id)
    kernel(world, **common, status="started", staff_id=staff_id)
    request = world.support_requests["support-t1"]
    request.status = SupportRequestStatus.ASSIGNED
    request.assigned_staff_id = staff_id
    request.task_id = "staff-support-test"
    world.agents[agent_id].zone_id = person.zone_id
    world.active_tasks["staff-support-test"] = ActiveTask(
        id="staff-support-test",
        kind="staff_support",
        agent_id=agent_id,
        resource_id=None,
        start_minute=world.minute_index,
        due_minute=world.minute_index,
        staff_id=staff_id,
    )
    person.active_task_id = "staff-support-test"
    person.work_queue[:] = ["staff-support-test"]
    person.busy_until = world.minute_index
    event = ScheduledEvent(
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
            "task_id": "staff-support-test",
            "support_duration_minutes": 10,
        },
        source_id="staff-support-test",
        provenance={"source_type": "assumption", "source_id": "ASM-TEST"},
    )
    return event, agent_id


@pytest.mark.parametrize("kind", ["emotional_support", "hygiene_support", "language_access"])
def test_the_end_of_support_is_python_s(generated_scenario: Any, kind: str) -> None:
    world, _ = _run(generated_scenario, 5)
    if kind != "emotional_support":
        # The selected formula's tables decide these kinds.
        world.model_configs.get("policy_registry", {}).pop("care_outcome", None)
    event, agent_id = _assigned(world, kind)
    record = world.model_state["body_state_v1"]["agents"][agent_id]
    record["hygiene_need"] = 0.5
    record["reassurance"] = 0.5
    record["positive_affect"] = 0.5
    record["negative_affect"] = 0.5
    world.agents[agent_id].stress = 0.5
    twin = copy.deepcopy(world)
    handler = StaffSupportCompletionHandler(cooldown_minutes=30, require_colocated=True)
    records = native.rules("support").complete_support(handler, world, event)
    expected = StaffSupportCompletionHandler.apply.__wrapped__(handler, twin, event)
    assert [item.to_dict() for item in records] == [item.to_dict() for item in expected]
    assert records[0].status is EventStatus.APPLIED
    _same_world(world, twin)
    payload = records[0].event.payload
    if kind == "language_access":
        assert payload["outcome_observation"] == "not_measured"
    else:
        assert payload["outcome_observation"] == "observed_change"


def test_a_kind_a_selected_formula_decides_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    event, _ = _assigned(world, "hygiene_support")
    before = _bytes(world)
    handler = StaffSupportCompletionHandler(cooldown_minutes=30, require_colocated=True)
    with pytest.raises(native.Unsupported, match="care formula"):
        native.rules("support").complete_support(handler, world, event)
    assert _bytes(world) == before


def test_the_end_of_support_that_does_not_fit_is_python_s(generated_scenario: Any) -> None:
    world, _ = _run(generated_scenario, 5)
    event, agent_id = _assigned(world, "emotional_support")
    world.agents[agent_id].activity = Activity.IDLE
    before = _bytes(world)
    handler = StaffSupportCompletionHandler(cooldown_minutes=30, require_colocated=True)
    with pytest.raises(native.Unsupported, match="does not match"):
        native.rules("support").complete_support(handler, world, event)
    assert _bytes(world) == before


# ---- what follows in the dispatch phase ----------------------------------------------------------


def test_the_interrupted_and_started_receipts_are_python_s(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    care_receipts.append_applied_care_transition(
        world,
        request_id="support-t1",
        agent_id=agent_id,
        household_id=None,
        support_kind="emotional_support",
        status="requested",
    )
    world.support_requests["support-t1"].status = SupportRequestStatus.UNRESOLVED
    event = _timeout_event(world, agent_id)
    twin = copy.deepcopy(world)
    native.rules("support").record_interrupted(controller, world, event)
    DispatchPhase._record_request_interrupted.__wrapped__(controller, twin, event)
    _same_world(world, twin)


def test_a_settled_request_without_a_receipt_adds_nothing(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    _open_request(world, agent_id)
    event = _timeout_event(world, agent_id)
    before = _bytes(world)
    native.rules("support").record_interrupted(controller, world, event)
    assert _bytes(world) == before


def test_a_patched_handler_of_the_table_goes_to_python(
    monkeypatch: pytest.MonkeyPatch, generated_scenario: Any
) -> None:
    world, controller = _run(generated_scenario, 5)
    calls: list[str] = []

    def apply(self: Any, target: Any, event: Any) -> None:
        calls.append(event.event_id)

    monkeypatch.setattr(ToiletServiceCompletionHandler, "apply", apply)
    event = _in_service(world, world.minute_index)
    world.schedule_event(event)
    controller._run_dispatch_phase(world, world.minute_index)
    assert calls == [event.event_id]


def test_a_manager_s_own_request_is_what_an_arrival_calls(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    handler = AgentTravelCompletionHandler(controller.toilet_service, validate_request_state=False)
    controller.toilet_service.request = lambda *_args, **_options: None  # type: ignore[method-assign]
    with pytest.raises(native.Unsupported):
        native.rules("travel_completion").after_arrival(
            handler,
            world,
            None,
            _idle_person(world),
            "toilet_request",
            _toilet(world),
            world.facilities[_toilet(world)].zone_id,
            None,
        )


def test_the_journey_home_after_a_toilet_is_python_s(generated_scenario: Any) -> None:
    world, controller = _run(generated_scenario, 5)
    agent_id = _idle_person(world)
    agent = world.agents[agent_id]
    agent.zone_id = next(zone for zone in sorted(world.zones) if zone != agent.zone_id)
    event = _timeout_event(world, agent_id)
    twin = copy.deepcopy(world)
    python_controller = _controller(twin)
    native.rules("travel").schedule_return(controller, world, event)
    DispatchPhase._schedule_return_to_initial_zone.__wrapped__(python_controller, twin, event)
    _same_world(world, twin)
