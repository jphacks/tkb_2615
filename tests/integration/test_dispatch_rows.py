"""The Rust dispatch phase: the Python phase's order, records and errors, and its fallbacks.

Each comparison runs the same events on two worlds, one through rules/dispatch.rs and one through
the Python step, and compares everything saved. See docs/rust-port/lanes/m3-dispatch.md.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import (
    AgentTravelCompletionHandler,
    SocialInteractionHandler,
)
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)
from sheltersim.experiments.phases.dispatch import DispatchPhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


class Sink:
    """What the controller writes to, which these tests don't read."""

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *arguments, **options: None


def _counts() -> dict[str, int]:
    return dict(native.rules("dispatch").stats()[0])


def _new_controller() -> OfflineSimulationController:
    world = WorldState.from_scenario(ScenarioSpec.load(SCENARIO_100_AGENTS))
    world.duration_minutes = 600
    return OfflineSimulationController(world, Sink())


def _event(name: str, sequence: int, event_type: str = "probe", **options: Any) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=name,
        kind=options.pop("kind", EventKind.ENDOGENOUS),
        due_minute=options.pop("due", 0),
        priority=options.pop("priority", 0),
        sequence=sequence,
        event_type=event_type,
        payload=options.pop("payload", {}),
        source_id=options.pop("source_id", "probe-1"),
        causation_id=None,
        provenance=options.pop("provenance", {"source_type": "assumption"}),
    )


def _document(world: Any) -> Any:
    """What a walk writes, read in a world that may not be valid."""
    agent = world.agents["A001"]
    return (
        (agent.zone_id, agent.activity, agent.target_id),
        list(world.active_tasks),
        [event.event_id for event in world.event_queue.pending()],
        len(world.event_log),
        dict(world.id_counters),
        world._core.entities().verify(world),
    )


def _saved(controller: OfflineSimulationController) -> dict[str, Any]:
    world = controller.world
    return {
        "records": [item.decode() for item in world.event_log.items_bytes(0, len(world.event_log))],
        "pending": [event.to_dict() for event in world.event_queue.pending()],
        "counters": dict(world.id_counters),
        "world": world.canonical_json(),
    }


def _both_ways(
    arrange: Callable[[OfflineSimulationController], Any],
    minute: int = 0,
) -> tuple[OfflineSimulationController, OfflineSimulationController]:
    """The same arrangement run by the kernel and by the Python step; all they saved is equal."""
    by_kernel, by_python = _new_controller(), _new_controller()
    arrange(by_kernel)
    arrange(by_python)
    assert _saved(by_kernel) == _saved(by_python)
    before = _counts()["phases_ran"]
    by_kernel.world.minute_index = by_python.world.minute_index = minute
    by_kernel._run_dispatch_phase(by_kernel.world, minute)
    assert _counts()["phases_ran"] == before + 1, "the kernel did not run"
    DispatchPhase._run_dispatch_phase(by_python, by_python.world, minute)
    assert _saved(by_kernel) == _saved(by_python)
    return by_kernel, by_python


class Recording:
    """A Python handler: says what it was given, and does what a test asks of it."""

    def __init__(self, seen: list[str], then: Callable[[Any, Any], Any] | None = None) -> None:
        self.seen = seen
        self.then = then

    def apply(self, world: Any, event: Any) -> Any:
        self.seen.append(event.event_id)
        return self.then(world, event) if self.then is not None else None


def test_events_run_by_minute_priority_and_sequence_and_are_recorded_in_that_order() -> None:
    orders: list[list[str]] = []

    def arrange(controller: OfflineSimulationController) -> None:
        seen: list[str] = []
        orders.append(seen)
        controller.event_dispatcher.handlers["probe"] = Recording(seen)
        world = controller.world
        for name, sequence, priority, due in (
            ("c", 903, 0, 0),
            ("a", 901, 5, 0),
            ("b", 902, 0, 0),
            ("late", 904, 0, 1),
            ("first", 905, -1, 0),
        ):
            world.schedule_event(_event(name, sequence, priority=priority, due=due))

    by_kernel, _ = _both_ways(arrange)
    assert orders[0] == orders[1] == ["first", "b", "c", "a"]
    assert [event.event_id for event in by_kernel.world.event_queue.pending()][:1] != ["first"]
    assert by_kernel.world.event_queue.find("late") is not None


def test_a_snapshot_entry_that_was_cancelled_is_skipped_with_no_record() -> None:
    def arrange(controller: OfflineSimulationController) -> None:
        def cancel(world: Any, event: Any) -> None:
            world.event_queue.remove("second")

        controller.event_dispatcher.handlers["probe"] = Recording([], cancel)
        controller.world.schedule_event(_event("first", 901))
        controller.world.schedule_event(_event("second", 902))

    by_kernel, _ = _both_ways(arrange)
    assert by_kernel.world.event_log.find("second") is None


@pytest.mark.parametrize("same_values", [True, False])
def test_a_snapshot_entry_that_was_replaced_runs_only_if_it_is_the_same_event(
    same_values: bool,
) -> None:
    orders: list[list[str]] = []

    def arrange(controller: OfflineSimulationController) -> None:
        seen: list[str] = []
        orders.append(seen)

        def replace(world: Any, event: Any) -> None:
            if event.event_id == "first":
                due = 0 if same_values else 7
                world.event_queue.replace("second", _event("second", 902, due=due))

        controller.event_dispatcher.handlers["probe"] = Recording(seen, replace)
        controller.world.schedule_event(_event("first", 901))
        controller.world.schedule_event(_event("second", 902))

    by_kernel, _ = _both_ways(arrange)
    assert orders[0] == orders[1] == (["first", "second"] if same_values else ["first"])
    assert (by_kernel.world.event_log.find("second") is not None) is same_values


def test_an_event_type_without_a_handler_fails_with_python_s_text() -> None:
    by_kernel, _ = _both_ways(lambda controller: controller.world.schedule_event(_event("x", 901)))
    record = by_kernel.world.event_log.find("x")
    assert record.status is EventStatus.FAILED
    assert record.detail == "no handler registered for event type 'probe'"


@pytest.mark.parametrize(
    ("error", "status"),
    [(EventSuppressed, EventStatus.SUPPRESSED), (EventApplicationError, EventStatus.FAILED)],
)
def test_a_python_handler_s_expected_errors_are_its_outcome(
    error: type[Exception], status: EventStatus
) -> None:
    def refuse(world: Any, event: Any) -> None:
        raise error("the reason")

    def arrange(controller: OfflineSimulationController) -> None:
        controller.event_dispatcher.handlers["probe"] = Recording([], refuse)
        controller.world.schedule_event(_event("x", 901))

    by_kernel, _ = _both_ways(arrange)
    record = by_kernel.world.event_log.find("x")
    assert (record.status, record.detail) == (status, "the reason")


def test_a_handler_s_other_error_ends_the_phase_and_does_not_run_it_twice() -> None:
    controller = _new_controller()
    seen: list[str] = []

    def fail(world: Any, event: Any) -> None:
        raise KeyError("a bug")

    controller.event_dispatcher.handlers["probe"] = Recording(seen, fail)
    controller.world.schedule_event(_event("x", 901))
    with pytest.raises(KeyError, match="a bug"):
        controller._run_dispatch_phase(controller.world, 0)
    assert seen == ["x"]


def test_a_handler_s_own_record_and_follow_ups_are_compared_and_appended_as_in_python() -> None:
    def transactional(world: Any, event: Any) -> tuple[Any, ...]:
        world.event_log.append(EventLogRecord(event, 0, EventStatus.APPLIED))
        return (EventLogRecord(_event("follow", 950), 0, EventStatus.APPLIED),)

    def arrange(controller: OfflineSimulationController) -> None:
        controller.world.id_counters["event"] = 1000
        controller.event_dispatcher.handlers["probe"] = Recording([], transactional)
        controller.world.schedule_event(_event("x", 901))

    by_kernel, _ = _both_ways(arrange)
    assert by_kernel.world.event_log.find("follow") is not None


def test_a_record_that_disagrees_with_the_outcome_raises_python_s_error() -> None:
    controller = _new_controller()

    def wrong(world: Any, event: Any) -> None:
        world.event_log.append(EventLogRecord(event, 0, EventStatus.FAILED))

    controller.event_dispatcher.handlers["probe"] = Recording([], wrong)
    controller.world.schedule_event(_event("x", 901))
    with pytest.raises(ValueError, match="transactional event record disagrees"):
        controller._run_dispatch_phase(controller.world, 0)


# ---- The table -----------------------------------------------------------------------------------


def _social_action(controller: OfflineSimulationController, sequence: int = 901) -> ScheduledEvent:
    world = controller.world
    first, second = "A001", "A002"
    world.agents[second].zone_id = world.agents[first].zone_id
    return ScheduledEvent(
        event_id=f"social-{sequence}",
        kind=EventKind.ENDOGENOUS,
        due_minute=0,
        priority=0,
        sequence=sequence,
        event_type="social_interaction",
        payload={
            "interaction_type": "reassurance_exchange",
            "agent_id": first,
            "participant_agent_id": second,
            "zone_id": world.agents[first].zone_id,
            "topic_id": "emotional_check_in",
            "action": "offer_reassurance",
            "reason_short": "autonomous_need_choice",
            "interaction_duration_minutes": 5,
        },
        source_id="social-policy-1",
        causation_id=None,
        provenance={
            "source_type": "assumption",
            "source_id": "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1",
            "policy_version": "social-policy-1",
        },
    )


def test_a_social_action_runs_in_rust_with_python_s_results() -> None:
    before = _counts()
    by_kernel, _ = _both_ways(
        lambda controller: controller.world.schedule_event(_social_action(controller))
    )
    after = _counts()
    assert after["social_ran"] > before["social_ran"]
    assert after["settled_rows"] > before["settled_rows"]
    assert after["events_python"] == before["events_python"]
    assert by_kernel.world.event_log.find("social-901").status is EventStatus.APPLIED


def test_a_suppressed_social_action_has_python_s_detail_in_its_record() -> None:
    text = "social action participants are no longer co-located"

    def arrange(controller: OfflineSimulationController) -> None:
        event = _social_action(controller)
        world = controller.world
        zones = [zone for zone in world.zones if zone != world.agents["A001"].zone_id]
        world.agents["A002"].zone_id = zones[0]
        world.schedule_event(event)

    before = _counts()
    by_kernel, _ = _both_ways(arrange)
    after = _counts()
    record = by_kernel.world.event_log.find("social-901")
    assert (record.status, record.detail) == (EventStatus.SUPPRESSED, text)
    assert after["social_suppressed"] == before["social_suppressed"] + 1
    assert after["social_refused"] == before["social_refused"]


def test_a_social_event_the_kernel_refuses_goes_to_python_and_fails_with_its_text() -> None:
    def arrange(controller: OfflineSimulationController) -> None:
        event = _social_action(controller)
        payload = {**event.payload_dict(), "reason_short": "other"}
        controller.world.schedule_event(
            ScheduledEvent(
                event_id=event.event_id,
                kind=event.kind,
                due_minute=0,
                priority=0,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=payload,
                source_id=event.source_id,
                causation_id=None,
                provenance=dict(event.provenance),
            )
        )

    before = _counts()
    by_kernel, _ = _both_ways(arrange)
    after = _counts()
    assert after["social_refused"] == before["social_refused"] + 1
    assert after["events_python"] == before["events_python"] + 1
    record = by_kernel.world.event_log.find("social-901")
    assert record.status is EventStatus.FAILED
    assert record.detail == "social action reason code is not allowed"


def test_a_patch_of_a_table_handler_s_apply_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _new_controller()
    event = _social_action(controller)
    controller.world.schedule_event(event)
    seen: list[str] = []
    monkeypatch.setattr(
        SocialInteractionHandler,
        "apply",
        lambda self, world, due: seen.append(due.event_id),
    )
    controller._run_dispatch_phase(controller.world, 0)
    assert seen == [event.event_id]


def test_a_fake_handler_of_a_table_s_event_type_is_called() -> None:
    controller = _new_controller()
    event = _social_action(controller)
    controller.world.schedule_event(event)
    seen: list[str] = []
    controller.event_dispatcher.handlers["social_interaction"] = Recording(seen)
    controller._run_dispatch_phase(controller.world, 0)
    assert seen == [event.event_id]


def test_an_effect_program_s_guard_is_python_s_to_read() -> None:
    controller = _new_controller()
    event = _social_action(controller)
    controller.world.schedule_event(
        ScheduledEvent(
            event_id=event.event_id,
            kind=event.kind,
            due_minute=0,
            priority=0,
            sequence=event.sequence,
            event_type=event.event_type,
            payload=event.payload_dict(),
            source_id=event.source_id,
            causation_id=None,
            provenance={**dict(event.provenance), "effect_program_v1": {"guard": None}},
        )
    )
    before = _counts()
    controller._run_dispatch_phase(controller.world, 0)
    assert _counts()["events_python"] == before["events_python"] + 1
    assert controller.world.event_log.find(event.event_id).status is EventStatus.APPLIED


def test_a_dispatcher_of_another_kind_leaves_the_phase_to_python() -> None:
    controller = _new_controller()

    class Other(type(controller.event_dispatcher)):
        pass

    other = Other(dict(controller.event_dispatcher.handlers))
    controller.event_dispatcher = other
    before = _counts()
    controller._run_dispatch_phase(controller.world, 0)
    after = _counts()
    assert after["phases_refused"] == before["phases_refused"] + 1
    assert after["phases_ran"] == before["phases_ran"]


# ---- A walk --------------------------------------------------------------------------------------


def _walk(
    controller: OfflineSimulationController,
    purpose: str,
    facility_id: str | None,
    destination: str,
    agent_id: str = "A001",
) -> ScheduledEvent:
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
    return next(
        event
        for event in world.event_queue.pending()
        if event.event_type == "agent_travel_complete" and event.payload["agent_id"] == agent_id
    )


def _facility_elsewhere(controller: OfflineSimulationController, kind: str) -> Any:
    world = controller.world
    zone = world.agents["A001"].zone_id
    return next(
        facility
        for facility in world.facilities.values()
        if facility.kind == kind and facility.zone_id != zone
    )


def _zone_elsewhere(controller: OfflineSimulationController) -> str:
    world = controller.world
    zone = world.agents["A001"].zone_id
    return next(name for name in world.zones if name != zone)


def _return_walk(controller: OfflineSimulationController) -> ScheduledEvent:
    return _walk(controller, "return", None, _zone_elsewhere(controller))


def _toilet_walk(controller: OfflineSimulationController) -> ScheduledEvent:
    facility = _facility_elsewhere(controller, "toilet")
    return _walk(controller, "toilet_request", facility.id, facility.zone_id)


@pytest.mark.parametrize("walk", [_return_walk, _toilet_walk])
def test_a_walk_ends_in_rust_with_python_s_results(
    walk: Callable[[OfflineSimulationController], ScheduledEvent],
) -> None:
    def arrange(controller: OfflineSimulationController) -> None:
        walk(controller)

    before = _counts()
    by_kernel, _ = _both_ways(arrange, minute=_due_of(walk))
    after = _counts()
    assert after["travel_ran"] == before["travel_ran"] + 1
    assert after["travel_refused"] == before["travel_refused"]
    assert by_kernel.world.agents["A001"].target_id is None or walk is _toilet_walk


def _due_of(walk: Callable[[OfflineSimulationController], ScheduledEvent]) -> int:
    return walk(_new_controller()).due_minute


def test_a_walk_s_request_that_fails_is_recorded_with_its_text_as_in_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*arguments: Any, **options: Any) -> None:
        raise ValueError("the toilet refuses")

    def arrange(controller: OfflineSimulationController) -> None:
        _toilet_walk(controller)
        monkeypatch.setattr(controller.toilet_service, "request", refuse)

    before = _counts()
    by_kernel, _ = _both_ways(arrange, minute=_due_of(_toilet_walk))
    after = _counts()
    assert after["travel_failed"] == before["travel_failed"] + 1
    record = next(
        record
        for record in by_kernel.world.event_log.records
        if record.event.event_type == "agent_travel_complete"
    )
    assert (record.status, record.detail) == (EventStatus.FAILED, "the toilet refuses")
    assert by_kernel.world.agents["A001"].activity.value == "idle"


def _stop_walking(world: Any, event: Any) -> None:
    agent = world.agents["A001"]
    agent.activity = type(agent.activity).IDLE


SPOILED_WALKS: list[tuple[Callable[[Any, Any], None], str]] = [
    (lambda world, event: world.active_tasks.pop(event.payload["task_id"]), "active task"),
    (lambda world, event: setattr(world.agents["A001"], "target_id", "x"), "its target"),
    (
        lambda world, event: setattr(
            world.agents["A001"], "activity", type(world.agents["A001"].activity).IDLE
        ),
        "active task",
    ),
]


@pytest.mark.parametrize(("spoil", "text"), SPOILED_WALKS)
def test_a_walk_that_does_not_fit_changes_nothing_and_python_says_why(
    spoil: Callable[[Any, Any], None], text: str
) -> None:
    controller = _new_controller()
    world = controller.world
    event = _return_walk(controller)
    world.minute_index = event.due_minute
    spoil(world, event)
    handler = controller.event_dispatcher.handlers["agent_travel_complete"]
    before = _document(world)
    with pytest.raises(native.Unsupported):
        native.rules("travel_completion").apply(handler, world, event)
    assert _document(world) == before
    with pytest.raises(EventApplicationError, match=text):
        AgentTravelCompletionHandler.apply(handler, world, event)


@pytest.mark.parametrize("purpose", ["sleep_bed", "presence_relocation", "another"])
def test_the_purposes_that_stay_python_s_are_refused_before_any_change(purpose: str) -> None:
    controller = _new_controller()
    world = controller.world
    event = _return_walk(controller)
    payload = {**event.payload_dict(), "purpose": purpose}
    other = _event(
        "walk-other",
        event.sequence + 5000,
        "agent_travel_complete",
        due=event.due_minute,
        payload=payload,
    )
    handler = controller.event_dispatcher.handlers["agent_travel_complete"]
    world.minute_index = event.due_minute
    before = _document(world)
    with pytest.raises(native.Unsupported):
        native.rules("travel_completion").apply(handler, world, other)
    assert _document(world) == before


def test_a_walk_with_a_value_the_writer_refuses_is_recorded_by_python() -> None:
    class Text(str):
        pass

    def arrange(controller: OfflineSimulationController) -> None:
        event = _return_walk(controller)
        world = controller.world
        world.event_queue.remove(event.event_id)
        world.schedule_event(
            ScheduledEvent(
                event_id=event.event_id,
                kind=event.kind,
                due_minute=event.due_minute,
                priority=event.priority,
                sequence=event.sequence,
                event_type=event.event_type,
                payload={**event.payload_dict(), "note": Text("a")},
                source_id=event.source_id,
                causation_id=event.causation_id,
                provenance=dict(event.provenance),
            )
        )

    controller = _new_controller()
    due = _return_walk(controller).due_minute
    before = _counts()
    by_kernel, _ = _both_ways(arrange, minute=due)
    after = _counts()
    assert after["travel_ran"] == before["travel_ran"] + 1
    assert after["settled_python"] == before["settled_python"] + 1
    assert not by_kernel.world.event_log.all_encoded()


def test_a_walk_that_has_only_a_row_gets_its_object_only_if_python_reads_it() -> None:
    controller, twin = _new_controller(), _new_controller()
    state = native.state("events")
    for each in (controller, twin):
        event = _return_walk(each)
        due = event.due_minute
        each.world.event_queue.remove(event.event_id)
        state.schedule_row(each.world, event)
    controller.world.minute_index = twin.world.minute_index = due
    before = _counts()
    controller._run_dispatch_phase(controller.world, due)
    DispatchPhase._run_dispatch_phase(twin, twin.world, due)
    after = _counts()
    assert after["travel_ran"] == before["travel_ran"] + 1
    assert after["settled_rows"] == before["settled_rows"] + 1
    assert _saved(controller) == _saved(twin)


# ---- A run ---------------------------------------------------------------------------------------


def _run(scenario: Any, directory: Path, minutes: int) -> dict[str, Any]:
    world = WorldState.from_scenario(scenario)
    artifacts = RunArtifactWriter(scenario, directory)
    artifacts.write_initial(world, execution_mode="offline")
    controller = OfflineSimulationController(world, artifacts)
    TickEngine(world, phase_handler=controller).run(minutes)
    return {
        "digest": world.state_digest(),
        "records": [raw.decode() for raw in world.event_log.items_bytes(0, len(world.event_log))],
        "counters": dict(world.id_counters),
    }


def test_a_run_is_the_same_in_rust_through_python_entries_and_in_python(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _counts()
    direct = _run(generated_scenario, tmp_path / "direct", 240)
    after = _counts()
    assert after["phases_ran"] - before["phases_ran"] == 240
    assert after["events_rust"] > before["events_rust"]
    assert after["travel_refused"] == before["travel_refused"]
    assert after["completion_refused"] == before["completion_refused"]
    assert after["sync_refused"] == before["sync_refused"]
    module = native.rules("dispatch")
    module.use_python_entries(True)
    try:
        entries = _run(generated_scenario, tmp_path / "entries", 240)
    finally:
        module.use_python_entries(False)
    assert entries == direct
    monkeypatch.delattr(OfflineSimulationController, "_run_dispatch_phase")
    python = _run(generated_scenario, tmp_path / "python", 240)
    assert python == direct
