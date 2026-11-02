"""The texts of the water and meal service's outcomes, and of a request its queue turns down.

`ResourceServiceManager.complete`, `cancel` and `interrupt_for_staff` raise `EventSuppressed`
and `EventApplicationError`, whose texts the dispatcher saves as an event's `detail`. A request
the queue turns down raises a `ValueError` whose text the propose step saves as the request's
`detail`. Each case builds a state that reaches one check and asks the Python rule, on a copy,
what it says. With the kernels, the kernel must raise the same class and text itself (not a
refusal the Python rule then words) and leave the world as it was, or, for a request, write what
the Python rule writes. Without the kernels the Python rule alone is checked against the text
(docs/rust-port/lanes/l8-texts-events.md). The toilet completion's and the ledger handler's
wraps of their rules' `ValueError` are here too.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from support.world import NATIVE_OUTCOME

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.models import ActiveTask, Activity
from sheltersim.domain.resource_service import ResourceServiceManager
from sheltersim.events.handlers import (
    ResourceLedgerHandler,
    ResourceServiceCancellationHandler,
    ToiletServiceCompletionHandler,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventSuppressed,
    ScheduledEvent,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.propose import ProposePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.behavior import ActionKind

SERVICE = "resource_service_v1"
LEDGER = "resource_ledger_v1"
OUTCOMES = (EventSuppressed, EventApplicationError)


def _bytes(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _says(world: WorldState, call: Any, rule: Any, expected: type, text: str) -> None:
    """`rule(copy)` raises `expected` with `text`; with the kernels, `call(world)` raises it itself.

    `call` runs the kernel (through its Python entry); `rule` is the Python rule alone. The world
    `call` ran on must end as the copy the rule ran on does: unchanged for an event's outcome.
    Without the kernels the rule runs on the world itself (a world holds read-only views that
    `deepcopy` takes only from the kernels' rows).
    """
    twin = copy.deepcopy(world) if native.available() else world
    with pytest.raises(expected) as said:
        rule(twin)
    assert type(said.value) is expected
    assert str(said.value) == text
    if not native.available():
        return
    before = _bytes(world)
    with pytest.raises(expected) as made:
        call(world)
    assert getattr(made.value, NATIVE_OUTCOME, False), f"the kernel refused: {made.value}"
    assert type(made.value) is expected
    assert str(made.value) == text
    assert _bytes(world) == _bytes(twin)
    if expected is not ValueError:
        assert _bytes(world) == before


def _event(event_type: str, payload: dict[str, Any], **fields: Any) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=fields.pop("event_id", f"test-{event_type.replace('_', '-')}"),
        kind=fields.pop("kind", EventKind.EXOGENOUS),
        due_minute=fields.pop("due_minute", 0),
        priority=0,
        sequence=fields.pop("sequence", 900_001),
        event_type=event_type,
        payload=payload,
        source_id=fields.pop("source_id", "SRC-TEST"),
        causation_id=None,
        provenance={"source_type": "assumption", "source_id": "SRC-TEST"},
    )


def _started(resource_service_scenario: Any, **overrides: Any) -> tuple[WorldState, ScheduledEvent]:
    """A1 being served water at minute 0: the world and its pending completion."""
    world = WorldState.from_scenario(resource_service_scenario(**overrides))
    manager = ResourceServiceManager(world)
    world.agents["A001"].zone_id = world.facilities["water_station_1"].zone_id
    manager.request("A001", "water_station_1", minute=0)
    started = manager.arbitrate(0)
    assert len(started) == 1
    world.validate()
    return world, started[0]


def _completion(completion: ScheduledEvent, **changes: Any) -> ScheduledEvent:
    document = completion.to_dict()
    payload = dict(document["payload"])
    for key, value in changes.pop("payload", {}).items():
        if value is _DROP:
            payload.pop(key)
        else:
            payload[key] = value
    return ScheduledEvent(**{**document, **changes, "payload": payload})


_DROP = object()


# ---- complete -----------------------------------------------------------------------------------


def _complete_says(world: WorldState, event: ScheduledEvent, expected: type, text: str) -> None:
    _says(
        world,
        lambda state: ResourceServiceManager(state).complete(event),
        lambda state: ResourceServiceManager.complete.__wrapped__(
            ResourceServiceManager(state), event
        )
        if native.available()
        else ResourceServiceManager(state).complete(event),
        expected,
        text,
    )


@pytest.mark.parametrize(
    ("changes", "text"),
    [
        ({"payload": {"task_id": 7}}, "resource completion requires a task_id"),
        ({"payload": {"task_id": _DROP}}, "resource completion requires a task_id"),
        (
            {"payload": {"task_id": "no-such-task"}},
            "resource completion does not match an active task",
        ),
        ({"kind": EventKind.EXOGENOUS}, "resource completion payload or event kind is invalid"),
        (
            {"event_type": "resource_service_completed"},
            "resource completion payload or event kind is invalid",
        ),
        ({"payload": {"extra": 1}}, "resource completion payload or event kind is invalid"),
        ({"payload": {"staff_id": _DROP}}, "resource completion payload or event kind is invalid"),
        ({"due_minute": 3}, "resource completion does not match its active task"),
        ({"payload": {"agent_id": "A002"}}, "resource completion does not match its active task"),
        ({"payload": {"agent_id": None}}, "resource completion does not match its active task"),
        ({"payload": {"staff_id": "S-MEAL"}}, "resource completion does not match its active task"),
        (
            {"payload": {"facility_id": "meal_distribution_1"}},
            "resource completion does not match its active task",
        ),
        (
            {"payload": {"reservation_id": "other-reservation"}},
            "resource completion does not match its active task",
        ),
        (
            {"payload": {"resource_id": "food_meal_units"}},
            "resource completion does not match its active task",
        ),
        (
            {"payload": {"quantity": 1}},
            "resource service completion does not match its full reservation, slot, or staff",
        ),
        (
            {"payload": {"quantity": 250.0}},
            "resource service completion does not match its full reservation, slot, or staff",
        ),
        (
            {"payload": {"quantity": True}},
            "resource service completion does not match its full reservation, slot, or staff",
        ),
        (
            {"payload": {"quantity": "250"}},
            "resource service completion does not match its full reservation, slot, or staff",
        ),
    ],
)
def test_a_completion_s_texts_are_python_s(
    resource_service_scenario: Any, changes: dict[str, Any], text: str
) -> None:
    world, completion = _started(resource_service_scenario)
    _complete_says(world, _completion(completion, **changes), EventApplicationError, text)


def test_a_completion_of_a_cancelled_task_is_suppressed_with_python_s_text(
    resource_service_scenario: Any,
) -> None:
    world, completion = _started(resource_service_scenario)
    world.model_state[SERVICE]["cancelled_task_ids"].append("resource-service-task-gone")
    event = _completion(completion, payload={"task_id": "resource-service-task-gone"})
    _complete_says(
        world,
        event,
        EventSuppressed,
        "resource service task 'resource-service-task-gone' was cancelled",
    )


@pytest.mark.parametrize(
    "spoil",
    [
        "slot",
        "staff",
        "staff_busy",
        "reservation",
        "owned",
    ],
)
def test_a_completion_whose_service_state_differs_is_python_s_to_fail(
    resource_service_scenario: Any, spoil: str
) -> None:
    world, completion = _started(resource_service_scenario)
    task_id = completion.payload["task_id"]
    staff = world.staff[completion.payload["staff_id"]]
    if spoil == "slot":
        world.facilities["water_station_1"].resource_active_agent_ids.remove("A001")
    elif spoil == "staff":
        staff.active_task_id = None
    elif spoil == "staff_busy":
        staff.busy_until += 1
    elif spoil == "reservation":
        world.model_state[LEDGER]["accounts"]["water_ml"]["reservations"][task_id]["agent_id"] = (
            "A002"
        )
    else:
        world.model_state[SERVICE]["active_reservation_ids"].remove(task_id)
    _complete_says(
        world,
        completion,
        EventApplicationError,
        "resource service completion does not match its full reservation, slot, or staff",
    )


# ---- cancel -------------------------------------------------------------------------------------


def _cancel_says(world: WorldState, event: ScheduledEvent, expected: type, text: str) -> None:
    def rule(state: WorldState) -> None:
        manager = ResourceServiceManager(state)
        if native.available():
            ResourceServiceManager.cancel.__wrapped__(manager, event)
        else:
            ResourceServiceCancellationHandler(manager).apply(state, event)

    _says(
        world,
        lambda state: ResourceServiceCancellationHandler(ResourceServiceManager(state)).apply(
            state, event
        ),
        rule,
        expected,
        text,
    )


def _cancel(payload: dict[str, Any]) -> ScheduledEvent:
    return _event("resource_service_cancel", payload, kind=EventKind.INTERVENTION)


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"reason": "operator_cancelled"}, "resource cancellation requires a task_id"),
        (
            {"task_id": "", "reason": "operator_cancelled"},
            "resource cancellation requires a task_id",
        ),
        (
            {"task_id": 3, "reason": "operator_cancelled"},
            "resource cancellation requires a task_id",
        ),
        ({"task_id": "T"}, "resource cancellation payload is not a typed cancellation"),
        (
            {"task_id": "T", "reason": "operator_cancelled", "note": "x"},
            "resource cancellation payload is not a typed cancellation",
        ),
        (
            {"task_id": "T", "reason": "someone_said_so"},
            "resource cancellation payload is not a typed cancellation",
        ),
        (
            {"task_id": "T", "reason": None},
            "resource cancellation payload is not a typed cancellation",
        ),
        (
            {"task_id": "T", "reason": 4},
            "resource cancellation payload is not a typed cancellation",
        ),
        (
            {"task_id": "T", "reason": "operator_cancelled"},
            "resource cancellation does not match an active task",
        ),
    ],
)
def test_a_cancellation_s_texts_are_python_s(
    resource_service_scenario: Any, payload: dict[str, Any], text: str
) -> None:
    world, _ = _started(resource_service_scenario)
    _cancel_says(world, _cancel(payload), EventApplicationError, text)


def test_a_cancellation_of_a_cancelled_task_is_suppressed_with_python_s_text(
    resource_service_scenario: Any,
) -> None:
    world, completion = _started(resource_service_scenario)
    task_id = completion.payload["task_id"]
    world.model_state[SERVICE]["cancelled_task_ids"].append(task_id)
    _cancel_says(
        world,
        _cancel({"task_id": task_id, "reason": "facility_stopped"}),
        EventSuppressed,
        f"resource service task {task_id!r} was already cancelled",
    )


@pytest.mark.parametrize(
    ("spoil", "text"),
    [
        ("kind", "resource cancellation does not match an active task"),
        ("queue_entry", "queued resource Agent has no matching queue entry"),
        ("facility", "resource service task references a non-resource facility"),
        ("slot", "active resource Agent has no matching facility slot"),
        ("staff", "active resource cancellation does not match reservation, slot, and staff"),
        ("work_queue", "active resource cancellation does not match reservation, slot, and staff"),
        ("reservation", "active resource cancellation does not match reservation, slot, and staff"),
        ("owned", "active resource cancellation does not match reservation, slot, and staff"),
    ],
)
def test_a_cancellation_whose_task_differs_is_python_s_to_fail(
    resource_service_scenario: Any, spoil: str, text: str
) -> None:
    world, completion = _started(resource_service_scenario)
    task_id = completion.payload["task_id"]
    task = world.active_tasks[task_id]
    staff = world.staff[completion.payload["staff_id"]]
    if spoil == "kind":
        task.kind = "toilet_service"
    elif spoil == "queue_entry":
        # A2 queues and then leaves the facility's list behind its task's back.
        world.agents["A002"].zone_id = world.facilities["water_station_1"].zone_id
        receipt = ResourceServiceManager(world).request("A002", "water_station_1", minute=0)
        task_id = receipt.queue_task_id
        world.facilities["water_station_1"].resource_queue_agent_ids.remove("A002")
    elif spoil == "facility":
        toilet = next(key for key, item in world.facilities.items() if item.kind == "toilet")
        task.resource_id = toilet
    elif spoil == "slot":
        world.facilities["water_station_1"].resource_active_agent_ids.remove("A001")
    elif spoil == "staff":
        staff.active_task_id = None
    elif spoil == "work_queue":
        staff.work_queue.append("another")
    elif spoil == "reservation":
        world.model_state[LEDGER]["accounts"]["water_ml"]["reservations"][task_id]["agent_id"] = (
            "A002"
        )
    else:
        world.model_state[SERVICE]["active_reservation_ids"].remove(task_id)
    _cancel_says(
        world,
        _cancel({"task_id": task_id, "reason": "operator_cancelled"}),
        EventApplicationError,
        text,
    )


# ---- interrupt_for_staff ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spoil", "text"),
    [
        ("several", "unavailable staff owns multiple resource services"),
        ("ownership", "interrupted resource service has incomplete ownership"),
        ("activity", "interrupted resource task has inconsistent state"),
        ("busy", "interrupted resource task has inconsistent state"),
        ("facility", "interrupted resource service has an invalid facility"),
        ("reservation", "interrupted service reservation is inconsistent"),
        ("owned", "interrupted service reservation is inconsistent"),
    ],
)
def test_an_interruption_s_texts_are_python_s(
    resource_service_scenario: Any, spoil: str, text: str
) -> None:
    world, completion = _started(resource_service_scenario)
    task_id = completion.payload["task_id"]
    staff_id = completion.payload["staff_id"]
    task = world.active_tasks[task_id]
    if spoil == "several":
        world.active_tasks["second-service"] = ActiveTask(
            id="second-service",
            kind="resource_service_food",
            agent_id="A002",
            resource_id="meal_distribution_1",
            start_minute=0,
            due_minute=2,
            staff_id=staff_id,
        )
    elif spoil == "ownership":
        task.agent_id = None
    elif spoil == "activity":
        world.agents["A001"].activity = Activity.IDLE
    elif spoil == "busy":
        world.staff[staff_id].busy_until += 1
    elif spoil == "facility":
        toilet = next(key for key, item in world.facilities.items() if item.kind == "toilet")
        assert world.facilities[toilet].zone_id == world.agents["A001"].zone_id
        world.facilities[toilet].resource_active_agent_ids.append("A001")
        task.resource_id = toilet
    elif spoil == "reservation":
        world.model_state[LEDGER]["accounts"]["water_ml"]["reservations"][task_id]["agent_id"] = (
            "A002"
        )
    else:
        world.model_state[SERVICE]["active_reservation_ids"].remove(task_id)
    cause = _event("staff_unavailable", {"staff_id": staff_id, "until_minute": 5})

    def rule(state: WorldState) -> None:
        manager = ResourceServiceManager(state)
        if native.available():
            ResourceServiceManager.interrupt_for_staff.__wrapped__(
                manager, staff_id, causation_event=cause
            )
        else:
            manager.interrupt_for_staff(staff_id, causation_event=cause)

    _says(
        world,
        lambda state: ResourceServiceManager(state).interrupt_for_staff(
            staff_id, causation_event=cause
        ),
        rule,
        EventApplicationError,
        text,
    )


def test_an_interruption_of_a_cancelled_task_is_the_cancellation_s_text(
    resource_service_scenario: Any,
) -> None:
    """The cancellation inside raises its own text, after the Python rule took a sequence."""
    world, completion = _started(resource_service_scenario)
    task_id = completion.payload["task_id"]
    staff_id = completion.payload["staff_id"]
    world.model_state[SERVICE]["cancelled_task_ids"].append(task_id)
    cause = _event("staff_unavailable", {"staff_id": staff_id, "until_minute": 5})
    twin = copy.deepcopy(world) if native.available() else world
    rule = ResourceServiceManager.interrupt_for_staff
    rule = getattr(rule, "__wrapped__", rule)
    with pytest.raises(EventSuppressed) as said:
        rule(ResourceServiceManager(twin), staff_id, causation_event=cause)
    text = f"resource service task {task_id!r} was already cancelled"
    assert str(said.value) == text
    if not native.available():
        return
    before = _bytes(world)
    with pytest.raises(EventSuppressed) as made:
        native.rules("handlers").interrupt_for_staff(
            ResourceServiceManager(world), staff_id, causation_event=cause
        )
    assert getattr(made.value, NATIVE_OUTCOME, False)
    assert str(made.value) == text
    # The kernel decides before it writes: in the staff handler both run on a copy it drops.
    assert _bytes(world) == before


# ---- A request the queue turns down ------------------------------------------------------------


def _request_says(
    world: WorldState, agent_id: Any, facility_id: Any, text: str, **options: Any
) -> None:
    def rule(state: WorldState) -> None:
        manager = ResourceServiceManager(state)
        request = ResourceServiceManager.request
        getattr(request, "__wrapped__", request)(manager, agent_id, facility_id, **options)

    _says(
        world,
        lambda state: ResourceServiceManager(state).request(agent_id, facility_id, **options),
        rule,
        ValueError,
        text,
    )


@pytest.mark.parametrize(
    ("agent_id", "facility_id", "options", "text"),
    [
        ("A001", "water_station_1", {"minute": 1}, "resource request minute must match WorldState"),
        ("A001", "water_station_1", {"minute": True}, "resource request minute must be an integer"),
        ("A001", "water_station_1", {"minute": "0"}, "resource request minute must be an integer"),
        ("nobody", "water_station_1", {}, "unknown Agent 'nobody'"),
        ("A002", "nowhere", {}, "'nowhere' is not a resource service facility"),
        ("A002", "toilet_1", {}, "'toilet_1' is not a resource service facility"),
        (
            "A003",
            "water_station_1",
            {},
            "Agent must arrive at the resource service facility before queueing",
        ),
        (
            "A002",
            "water_station_1",
            {},
            "Agent 'A002' cannot request resource service while active",
        ),
        (
            "A001",
            "meal_distribution_1",
            {},
            "Agent 'A001' cannot request resource service while active",
        ),
        (
            "A003",
            "meal_distribution_1",
            {"walking": True},
            "Agent 'A003' already has an exclusive task",
        ),
        ("A004", "meal_distribution_1", {}, "Agent 'A004' already has an exclusive task"),
    ],
)
def test_a_request_s_refusal_texts_are_python_s(
    resource_service_scenario: Any,
    agent_id: str,
    facility_id: str,
    options: dict[str, Any],
    text: str,
) -> None:
    world, _ = _started(resource_service_scenario)
    zone = world.facilities["water_station_1"].zone_id
    toilet = next(key for key, item in world.facilities.items() if item.kind == "toilet")
    world.agents["A002"].zone_id = zone
    world.agents["A002"].activity = Activity.MOVING
    world.agents["A004"].zone_id = zone
    world.facilities[toilet].queue_agent_ids.append("A004")
    assert world.agents["A003"].zone_id != zone
    if options.pop("walking", False):
        world.agents["A003"].zone_id = zone
        world.active_tasks["walk-home"] = ActiveTask(
            id="walk-home",
            kind="travel_return",
            agent_id="A003",
            resource_id=None,
            start_minute=0,
            due_minute=3,
        )
    _request_says(world, agent_id, facility_id, text, validate_state=False, **options)


def _restricted(scenario: Any) -> WorldState:
    """The split water station's two points need a tag nobody has."""
    document = scenario.model_dump(mode="json")
    for point in document["model_configs"]["facility_geometry"]["service_points"]:
        if point["facility_id"] == "water_station_main":
            point["access"] = {
                **point["access"],
                "accessibility": "restricted",
                "required_access_tags": ["staff_key"],
            }
    return WorldState.from_scenario(type(scenario).model_validate(document))


def _place_at(world: WorldState, agent_id: str, facility_id: str) -> None:
    from sheltersim.domain.agent_dynamics import learn_facility
    from sheltersim.domain.movement import MOVEMENT_STATE_KEY

    facility = world.facilities[facility_id]
    world.agents[agent_id].zone_id = facility.zone_id
    state = world.model_state.get(MOVEMENT_STATE_KEY)
    if state is not None:
        state["positions"][agent_id]["zone_id"] = facility.zone_id
    if "agent_dynamics_v1" in world.model_state:
        learn_facility(world, agent_id, facility_id)


def test_no_open_independent_queue_is_python_s_refusal(
    generated_scenario_with_split_queues: Any,
) -> None:
    world = _restricted(generated_scenario_with_split_queues)
    _place_at(world, "A001", "water_station_main")
    _request_says(
        world,
        "A001",
        "water_station_main",
        "no accessible independent resource service queue is available",
        validate_state=False,
    )


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


@pytest.mark.parametrize("why", ["queue", "active", "exclusive"])
def test_a_request_at_once_the_queue_turns_down_keeps_python_s_text(
    generated_scenario_with_split_queues: Any, why: str
) -> None:
    """The propose step records `str(exc)` as the request's detail, and counts the rejection."""
    world = _restricted(generated_scenario_with_split_queues)
    _place_at(world, "A001", "water_station_main")
    texts = {
        "queue": "no accessible independent resource service queue is available",
        "active": "Agent 'A001' cannot request resource service while active",
        "exclusive": "Agent 'A001' already has an exclusive task",
    }
    if why == "active":
        world = WorldState.from_scenario(generated_scenario_with_split_queues)
        _place_at(world, "A001", "water_station_main")
        world.agents["A001"].activity = Activity.SEEKING_HELP
    elif why == "exclusive":
        world = WorldState.from_scenario(generated_scenario_with_split_queues)
        _place_at(world, "A001", "water_station_main")
        world.facilities["toilet_01"].queue_agent_ids.append("A001")
    twin = copy.deepcopy(world) if native.available() else world
    proposal = SimpleNamespace(
        action=ActionKind.FETCH_WATER,
        agent_id="A001",
        next_decision_at=world.minute_index + 7,
        target_id=None,
    )
    expected = ProposePhase._apply_effect(_controller(twin), proposal)
    history = twin.model_state[SERVICE]["request_history"]
    assert history[-1]["outcome"] == "rejected"
    assert history[-1]["reason"] == "request_rejected"
    assert history[-1]["detail"] == texts[why]
    assert twin.metric_state["resource_service_request_rejected_count"] == 1.0
    assert expected[0] is None
    if not native.available():
        return
    result = native.rules("proposals").apply_effect(_controller(world), proposal)
    assert list(result) == list(expected)
    assert _bytes(world) == _bytes(twin)
    assert world.id_counters == twin.id_counters
    assert world.metric_state == twin.metric_state
    assert world.model_state[SERVICE] == twin.model_state[SERVICE]


# ---- The wraps: a toilet visit's completion, and the ledger --------------------------------------


@pytest.fixture(scope="module")
def visiting() -> tuple[WorldState, str]:
    """A generated world at the first minute someone is being served at a toilet."""
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2] / "benchmarks")
    if root not in sys.path:
        sys.path.insert(0, root)
    import gate_scenarios

    world = WorldState.from_scenario(gate_scenarios.generated(evacuees=20, minutes=120))
    engine = TickEngine(world, phase_handler=_controller(world))
    for _ in range(120):
        engine.step()
        served = [key for key, task in world.active_tasks.items() if task.kind == "toilet_service"]
        if served:
            return world, served[0]
    pytest.fail("nobody was served at a toilet")


@pytest.mark.parametrize(
    ("spoil", "text"),
    [
        ("facility", "'{water}' is not a toilet Facility"),
        ("unknown_facility", "'nowhere' is not a toilet Facility"),
        ("agent", "Agent 'nobody' does not exist"),
        ("kind", "toilet service task '{task}' is not active"),
        ("other_agent", "toilet completion payload does not match its active task"),
        ("activity", "Agent '{agent}' is not using Facility '{toilet}'"),
        ("slot", "Agent '{agent}' must occupy exactly one toilet service slot"),
    ],
)
def test_a_toilet_completion_wraps_python_s_text(
    visiting: tuple[WorldState, str], spoil: str, text: str
) -> None:
    world = visiting[0]
    world = copy.deepcopy(world) if native.available() else WorldState.from_dict(world.to_dict())
    task = world.active_tasks[visiting[1]]
    agent_id, toilet = task.agent_id, task.resource_id
    water = next(key for key, item in world.facilities.items() if item.kind == "water_station")
    payload = {"facility_id": toilet, "agent_id": agent_id, "task_id": task.id}
    if spoil == "facility":
        payload["facility_id"] = water
    elif spoil == "unknown_facility":
        payload["facility_id"] = "nowhere"
    elif spoil == "agent":
        payload["agent_id"] = "nobody"
    elif spoil == "kind":
        task.kind = "toilet_queue"
    elif spoil == "other_agent":
        payload["agent_id"] = next(key for key in world.agents if key != agent_id)
    elif spoil == "activity":
        world.agents[agent_id].activity = Activity.IDLE
    else:
        world.facilities[toilet].active_agent_ids.append(agent_id)
    event = _event(
        "toilet_service_complete",
        payload,
        kind=EventKind.COMPLETION,
        due_minute=task.due_minute,
        sequence=world.id_counters["event"] + 50,
    )
    handler = ToiletServiceCompletionHandler()
    rule = ToiletServiceCompletionHandler.apply
    rule = getattr(rule, "__wrapped__", rule)
    _says(
        world,
        lambda state: handler.apply(state, event),
        lambda state: rule(handler, state, event),
        EventApplicationError,
        text.format(water=water, task=task.id, agent=agent_id, toilet=toilet),
    )


@pytest.mark.parametrize(
    ("operation", "payload", "text"),
    [
        (
            "loss",
            {"warehouse_id": "warehouse-h02", "resource_id": "water_ml", "quantity": 900},
            "resource loss exceeds unreserved remaining inventory",
        ),
        (
            "reserve",
            {
                "warehouse_id": "warehouse-h02",
                "resource_id": "water_ml",
                "reservation_id": "R-1",
                "agent_id": "A002",
                "quantity": 900,
            },
            "resource reservation exceeds available inventory",
        ),
        (
            "release",
            {
                "warehouse_id": "warehouse-h02",
                "resource_id": "water_ml",
                "reservation_id": "R-unknown",
                "agent_id": "A002",
            },
            "unknown resource reservation 'R-unknown'",
        ),
        (
            "delivery",
            {"warehouse_id": "elsewhere", "resource_id": "water_ml", "quantity": 5},
            "unknown resource warehouse ID 'elsewhere'",
        ),
        (
            "delivery",
            {"warehouse_id": "warehouse-h02", "resource_id": "water_ml", "quantity": 0},
            "resource quantity must be a positive integer",
        ),
    ],
)
def test_the_ledger_handler_wraps_python_s_text(
    resource_service_scenario: Any, operation: str, payload: dict[str, Any], text: str
) -> None:
    world, _ = _started(resource_service_scenario)
    event = _event(f"resource_{operation}", payload, sequence=world.id_counters["event"] + 50)
    handler = ResourceLedgerHandler(operation)
    rule = ResourceLedgerHandler.apply
    rule = getattr(rule, "__wrapped__", rule)
    _says(
        world,
        lambda state: handler.apply(state, event),
        lambda state: rule(handler, state, event),
        EventApplicationError,
        text,
    )
