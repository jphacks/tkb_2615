"""A walk that does not fit fails with the travel rule's own text, the same with or without Rust.

`AgentTravelCompletionHandler.apply` raises `EventApplicationError` when a walk's event does not
match its task, its person, or what its purpose needs; the dispatcher saves the text as the event's
`detail`. Every check comes before the rule's first write, so a failed walk changes nothing.
With the Rust kernels, rules/travel_completion.rs raises each of these texts itself, before any
change, and support/world.py `kernel_says_python_s_text` checks that it did and that the Python rule
on a copy says the same; without them, the Python rule raises it and the world is as it was
(docs/rust-port/lanes/l8-texts-events.md).
"""

from __future__ import annotations

from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS
from support.world import kernel_says_python_s_text

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity
from sheltersim.events.handlers import AgentTravelCompletionHandler
from sheltersim.events.models import EventApplicationError, EventStatus, ScheduledEvent
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.dispatch import DispatchPhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink

_GONE = object()


def _controller(scenario: Any) -> OfflineSimulationController:
    world = WorldState.from_scenario(scenario)
    world.duration_minutes = max(world.duration_minutes, 600)
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _hundred() -> OfflineSimulationController:
    return _controller(ScenarioSpec.load(SCENARIO_100_AGENTS))


def _walk(
    controller: OfflineSimulationController,
    purpose: str,
    facility_id: str | None,
    destination: str,
    agent_id: str = "A001",
) -> ScheduledEvent:
    """A walk of `agent_id` scheduled as the controller does, at its due minute."""
    world = controller.world
    agent = world.agents[agent_id]
    route = controller.space.route(agent.zone_id, destination, agent.id)
    assert route is not None and route.travel_minutes > 0
    assert controller._schedule_travel(
        agent_id=agent.id,
        route=route,
        purpose=purpose,
        facility_id=facility_id,
        minute=world.minute_index,
        validate_state=False,
    )
    event = next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete" and event.payload["agent_id"] == agent_id
    )
    world.minute_index = event.due_minute
    return event


def _zone_elsewhere(world: WorldState, agent_id: str = "A001") -> str:
    return next(name for name in sorted(world.zones) if name != world.agents[agent_id].zone_id)


def _facility_elsewhere(world: WorldState, kind: str, agent_id: str = "A001") -> Any:
    zone = world.agents[agent_id].zone_id
    return next(
        facility
        for _, facility in sorted(world.facilities.items())
        if facility.kind == kind and facility.zone_id != zone
    )


def _variant(event: ScheduledEvent, payload: dict[str, Any] | None = None, **fields: Any) -> Any:
    """The event with some payload values changed (`_GONE` drops one) or some fields."""
    changed = {**event.payload_dict()}
    for key, value in (payload or {}).items():
        if value is _GONE:
            changed.pop(key, None)
        else:
            changed[key] = value
    return ScheduledEvent(
        event_id=fields.pop("event_id", event.event_id),
        kind=event.kind,
        due_minute=fields.pop("due_minute", event.due_minute),
        priority=fields.pop("priority", event.priority),
        sequence=fields.pop("sequence", event.sequence),
        event_type=event.event_type,
        payload=changed,
        source_id=event.source_id,
        causation_id=event.causation_id,
        provenance=dict(event.provenance),
    )


def _text(world: WorldState) -> str:
    return world._canonical_text(world._canonical_data())


def _says(handler: Any, world: WorldState, event: Any, text: str) -> None:
    """The handler fails the walk with `text` and changes nothing; with Rust, the kernel says it."""
    if native.available():
        assert kernel_says_python_s_text(handler, world, event) == ("EventApplicationError", text)
        return
    before = _text(world)
    with pytest.raises(EventApplicationError) as said:
        handler.apply(world, event)
    assert str(said.value) == text
    assert _text(world) == before


def _handler(controller: OfflineSimulationController) -> Any:
    return controller.event_dispatcher.handlers["agent_travel_complete"]


# ---- What every walk is checked for, then the purposes of a walk home --------------------------


def test_a_walk_s_minute_person_and_destination_are_checked_in_the_rule_s_order() -> None:
    controller = _hundred()
    world = controller.world
    handler = _handler(controller)
    event = _walk(controller, "return", None, _zone_elsewhere(world))
    # Another person's walk on this task: before the purpose is read.
    _says(
        handler,
        world,
        _variant(event, {"agent_id": "A002", "purpose": "another"}),
        "travel completion Agent does not match its active task",
    )
    _says(
        handler,
        world,
        _variant(event, due_minute=event.due_minute + 1),
        "travel completion does not match an active due task",
    )
    for destination in ("nowhere", "it's"):
        _says(
            handler,
            world,
            _variant(event, {"destination_zone_id": destination}),
            f"destination zone {destination!r} does not exist",
        )
    # A person no longer walking: before the destination is read.
    world.agents["A001"].activity = Activity.IDLE
    _says(
        handler,
        world,
        _variant(event, {"destination_zone_id": "nowhere"}),
        "travel completion Agent does not match its active task",
    )


@pytest.mark.parametrize(
    ("purpose", "text"),
    [
        ("return", "return travel completion does not match its target"),
        ("queue_abandon", "return travel completion does not match its target"),
        ("another", "unsupported travel purpose 'another'"),
        ("sleep_bed", "sleep-bed travel does not match its live reservation"),
        ("presence_relocation", "presence relocation completion does not match its route"),
        (
            "staff_support_travel",
            "staff support travel does not match its assigned request and staff member",
        ),
        ("toilet_request", "toilet travel completion requires a facility_id"),
    ],
)
def test_a_walk_home_that_does_not_fit_its_purpose_fails_with_the_purpose_s_text(
    purpose: str, text: str
) -> None:
    controller = _hundred()
    world = controller.world
    event = _walk(controller, "return", None, _zone_elsewhere(world))
    if purpose in {"return", "queue_abandon"}:
        world.agents["A001"].target_id = "somewhere else"
    _says(_handler(controller), world, _variant(event, {"purpose": purpose}), text)


def test_a_sleep_bed_walk_without_its_journey_fails_after_its_reservation_is_read() -> None:
    controller = _hundred()
    world = controller.world
    destination = _zone_elsewhere(world)
    event = _variant(_walk(controller, "return", None, destination), {"purpose": "sleep_bed"})
    world.model_state["geometry_overlay_v1"] = {
        "bed_reservations": {"A001": {"room_id": "room-1", "bed_index": 0}},
        "temporary_rooms": {
            "room-1": {"zone_id": destination, "bed_points_m": [{"x": 1.0, "y": 2.0}]}
        },
    }
    _says(
        _handler(controller), world, event, "sleep-bed travel does not match its live reservation"
    )


@pytest.mark.parametrize("field", ["task_id", "event_id", "to_zone_id"])
def test_a_relocation_walk_that_does_not_match_its_pending_relocation_fails(field: str) -> None:
    controller = _hundred()
    world = controller.world
    destination = _zone_elsewhere(world)
    event = _variant(
        _walk(controller, "return", None, destination), {"purpose": "presence_relocation"}
    )
    relocation = {
        "task_id": event.payload["task_id"],
        "event_id": event.event_id,
        "from_zone_id": event.payload["route_zone_ids"][0],
        "to_zone_id": destination,
    }
    relocation[field] = "another"
    world.model_state["population_lifecycle_v1"] = {"pending_relocations": {"A001": relocation}}
    _says(
        _handler(controller),
        world,
        event,
        "presence relocation completion does not match its route",
    )


# ---- A walk to a toilet -------------------------------------------------------------------------


def test_a_toilet_walk_needs_its_facility_there_and_its_target() -> None:
    controller = _hundred()
    world = controller.world
    handler = _handler(controller)
    toilet = _facility_elsewhere(world, "toilet")
    event = _walk(controller, "toilet_request", toilet.id, toilet.zone_id)
    for facility_id in (_GONE, "", 7):
        _says(
            handler,
            world,
            _variant(event, {"facility_id": facility_id}),
            "toilet travel completion requires a facility_id",
        )
    _says(
        handler,
        world,
        _variant(event, {"facility_id": "nothing"}),
        "toilet travel destination does not match its facility",
    )
    _says(
        handler,
        world,
        _variant(
            event,
            {"destination_zone_id": next(z for z in sorted(world.zones) if z != toilet.zone_id)},
        ),
        "toilet travel destination does not match its facility",
    )
    world.agents["A001"].target_id = "somewhere else"
    _says(handler, world, event, "toilet travel completion does not match its target")


# ---- A walk to a water station ------------------------------------------------------------------


def test_a_resource_walk_needs_the_service_its_facility_and_its_route(
    generated_scenario: Any,
) -> None:
    controller = _controller(generated_scenario)
    world = controller.world
    handler = _handler(controller)
    assert handler.resource_service is not None
    station = _facility_elsewhere(world, "water_station")
    event = _walk(controller, "resource_request", station.id, station.zone_id)
    without = AgentTravelCompletionHandler(controller.toilet_service, validate_request_state=False)
    _says(without, world, event, "resource travel is not enabled for this scenario")
    _says(
        handler,
        world,
        _variant(event, {"facility_id": _GONE}),
        "resource travel completion requires a facility_id",
    )
    text = "resource travel completion does not match its target facility"
    route = list(event.payload["route_zone_ids"])
    for change in (
        {"facility_id": "nothing"},
        {"origin_zone_id": "nowhere"},
        {"origin_zone_id": _GONE, "route_zone_ids": [5, *route[1:]]},
        {"origin_zone_id": _GONE, "route_zone_ids": []},
        {"route_zone_ids": route[:-1]},
        {"route_zone_ids": "not a route"},
        {"facility_id": _facility_elsewhere(world, "toilet").id},
    ):
        _says(handler, world, _variant(event, change), text)
    world.agents["A001"].target_id = "somewhere else"
    _says(handler, world, event, text)


# ---- Walks in a queue ----------------------------------------------------------------------------


QUEUE_PURPOSES = {
    "toilet_queue_position",
    "resource_queue_position",
    "toilet_queue_service",
    "resource_queue_service",
}


def test_a_queue_walk_on_a_walk_home_fails_its_fifo_check() -> None:
    controller = _hundred()
    world = controller.world
    event = _walk(controller, "return", None, _zone_elsewhere(world))
    for purpose in sorted(QUEUE_PURPOSES):
        _says(
            _handler(controller),
            world,
            _variant(event, {"purpose": purpose}),
            "physical queue travel does not match its FIFO reservation",
        )


def _queue_walk(
    world: WorldState, engine: TickEngine, facilities: set[str] | None, ending: str
) -> Any:
    """The first walk in a queue the run schedules whose purpose ends so, at one of `facilities`."""
    for _ in range(300):
        engine.step()
        for event in world.event_queue.pending():
            if (
                event.event_type == "agent_travel_complete"
                and event.payload.get("purpose") in QUEUE_PURPOSES
                and event.payload["purpose"].endswith(ending)
                and (facilities is None or event.payload.get("facility_id") in facilities)
            ):
                return event
    pytest.fail("no walk in a queue was scheduled")


@pytest.mark.parametrize("ending", ["_position", "_service"])
@pytest.mark.parametrize("split", [False, True])
def test_a_queue_walk_that_left_its_reservation_fails_its_fifo_check(
    split: bool, ending: str, generated_scenario: Any, generated_scenario_with_split_queues: Any
) -> None:
    scenario = generated_scenario_with_split_queues if split else generated_scenario
    controller = _controller(scenario)
    world = controller.world
    engine = TickEngine(world, phase_handler=controller)
    facilities = {"toilet_01", "water_station_main"} if split else None
    event = _queue_walk(world, engine, facilities, ending)
    handler = _handler(controller)
    text = "physical queue travel does not match its FIFO reservation"
    agent_id = event.payload["agent_id"]
    facility = world.facilities[event.payload["facility_id"]]
    elsewhere = next(zone for zone in sorted(world.zones) if zone != facility.zone_id)
    _says(handler, world, _variant(event, {"destination_zone_id": elsewhere}), text)
    _says(handler, world, _variant(event, {"facility_id": _GONE}), text)
    world.agents[agent_id].target_id = "somewhere else"
    _says(handler, world, event, text)
    world.agents[agent_id].target_id = facility.id
    if split:
        assert isinstance(event.payload.get("request_id"), str)
        # A request the queue no longer holds.
        _says(handler, world, _variant(event, {"request_id": "nothing"}), text)


# ---- Through the dispatch loop ------------------------------------------------------------------


def test_a_walk_that_does_not_fit_is_saved_with_its_text_as_the_python_phase_saves_it() -> None:
    """A walk event of another purpose, before the real one in the same minute, fails alone."""

    def arrange() -> tuple[OfflineSimulationController, int]:
        controller = _hundred()
        world = controller.world
        event = _walk(controller, "return", None, _zone_elsewhere(world))
        world.schedule_event(
            _variant(
                event,
                {"purpose": "another"},
                event_id="walk-of-another-purpose",
                priority=event.priority - 1,
                sequence=world.allocate_event_sequence(),
            )
        )
        return controller, event.due_minute

    (by_dispatch, minute), (by_python, _) = arrange(), arrange()
    by_dispatch._run_dispatch_phase(by_dispatch.world, minute)
    DispatchPhase._run_dispatch_phase(by_python, by_python.world, minute)

    def saved(controller: OfflineSimulationController) -> list[tuple[str, Any, Any]]:
        return [
            (record.event.event_id, record.status, record.detail)
            for record in controller.world.event_log.records
            if record.event.event_type == "agent_travel_complete"
        ]

    assert saved(by_dispatch) == saved(by_python)
    assert [(status, detail) for _, status, detail in saved(by_dispatch)] == [
        (EventStatus.FAILED, "unsupported travel purpose 'another'"),
        (EventStatus.APPLIED, None),
    ]
    assert by_dispatch.world.canonical_json() == by_python.world.canonical_json()
