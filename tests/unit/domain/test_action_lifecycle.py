"""An Agent's open action ends only through a matching applied event, and every
behavior action has a lifecycle terminal."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from support.paths import SMALL_REPLAY

from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import (
    active_action_index,
    record_action_proposal,
    record_applied_action_completion,
)
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.events.queue import EventLog, keeps_saved_bytes
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import ActionKind


def make_world() -> WorldState:
    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    scenario = ScenarioSpec.model_validate(document)
    return WorldState.from_scenario(scenario)


def proposal(action: ActionKind, minute: int) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="A001", action=action, minute_index=minute, trigger="test_transition"
    )


def test_wait_lifecycle_records_completion_and_restores_open_action() -> None:
    world = make_world()
    active: dict[str, dict[str, object]] = {}
    record_action_proposal(
        world,
        proposal(ActionKind.WAIT, 0),
        target_id=None,
        committed=True,
        active_actions=active,
    )
    world.minute_index = 1
    record_action_proposal(
        world,
        proposal(ActionKind.WAIT, 1),
        target_id=None,
        committed=True,
        active_actions=active,
    )
    world.minute_index = 3
    record_action_proposal(
        world,
        proposal(ActionKind.REST, 3),
        target_id=None,
        committed=True,
        active_actions=active,
    )

    rows = [
        record.event.payload
        for record in world.event_log.records
        if record.event.event_type == "agent_action_lifecycle"
    ]
    assert [row["transition"] for row in rows] == ["started", "continued", "completed", "started"]
    assert rows[2]["duration_minutes"] == 3
    restored_index = active_action_index(world)
    assert restored_index["A001"]["action"] == ActionKind.REST.value


def test_action_completion_requires_matching_applied_event_log_record() -> None:
    world = make_world()
    active: dict[str, dict[str, object]] = {}
    record_action_proposal(
        world,
        proposal(ActionKind.FETCH_WATER, 0),
        target_id="water_station_1",
        committed=True,
        active_actions=active,
    )
    failed_sequence = world.allocate_event_sequence()
    failed = ScheduledEvent(
        event_id="failed-completion",
        kind=EventKind.COMPLETION,
        due_minute=2,
        priority=0,
        sequence=failed_sequence,
        event_type="resource_service_complete",
        payload={"agent_id": "A001", "facility_id": "water_station_1"},
    )
    world.event_log.append(
        EventLogRecord(event=failed, realized_minute=2, status=EventStatus.FAILED)
    )
    world.minute_index = 2
    record_applied_action_completion(world, failed, 2, active_actions=active)
    assert "A001" in active

    applied_sequence = world.allocate_event_sequence()
    applied = ScheduledEvent(
        event_id="applied-completion",
        kind=EventKind.COMPLETION,
        due_minute=2,
        priority=0,
        sequence=applied_sequence,
        event_type="resource_service_complete",
        payload={"agent_id": "A001", "facility_id": "water_station_1"},
    )
    world.event_log.append(
        EventLogRecord(event=applied, realized_minute=2, status=EventStatus.APPLIED)
    )
    record_applied_action_completion(world, applied, 2, active_actions=active)
    assert "A001" not in active
    lifecycle = [
        record.event.payload
        for record in world.event_log.records
        if record.event.event_type == "agent_action_lifecycle"
    ]
    assert lifecycle[-1]["transition"] == "completed"
    last_transition = next(
        record
        for record in reversed(world.event_log.records)
        if record.event.event_type == "agent_action_lifecycle"
    )
    assert last_transition.event.causation_id == applied.event_id


def test_every_behavior_action_has_a_supported_lifecycle_terminal() -> None:
    terminal_events = {
        ActionKind.REQUEST_TOILET: ("toilet_service_complete", {"facility_id": "toilet_1"}),
        ActionKind.FETCH_WATER: ("resource_service_complete", {"facility_id": "water_station_1"}),
        ActionKind.COLLECT_MEAL: ("resource_service_complete", {"facility_id": "meal_station_1"}),
        ActionKind.SEEK_HELP: ("staff_support_complete", {"request_id": "support-request"}),
        ActionKind.SEEK_INFORMATION: ("staff_support_complete", {"request_id": "info-request"}),
        ActionKind.SOCIALIZE: ("social_interaction", {"participant_agent_id": "A002"}),
        ActionKind.CARE_FAMILY: ("social_interaction", {"participant_agent_id": "A002"}),
        ActionKind.HELP_OTHERS: ("social_interaction", {"participant_agent_id": "A002"}),
    }
    for action in ActionKind:
        world = make_world()
        active: dict[str, dict[str, object]] = {}
        if action in {ActionKind.REST, ActionKind.SLEEP}:
            world.agents["A001"].needs.fatigue = 0.5
        target_id = (
            "A002"
            if action in {ActionKind.SOCIALIZE, ActionKind.CARE_FAMILY, ActionKind.HELP_OTHERS}
            else "support-request"
            if action is ActionKind.SEEK_HELP
            else "info-request"
            if action is ActionKind.SEEK_INFORMATION
            else "meal_station_1"
            if action is ActionKind.COLLECT_MEAL
            else "water_station_1"
            if action is ActionKind.FETCH_WATER
            else "toilet_1"
            if action is ActionKind.REQUEST_TOILET
            else None
        )
        record_action_proposal(
            world,
            proposal(action, 0),
            target_id=target_id,
            committed=True,
            active_actions=active,
        )

        if action in terminal_events:
            event_type, target = terminal_events[action]
            sequence = world.allocate_event_sequence()
            event = ScheduledEvent(
                event_id=f"action-terminal-{action.value}",
                kind=EventKind.COMPLETION,
                due_minute=2,
                priority=0,
                sequence=sequence,
                event_type=event_type,
                payload={"agent_id": "A001", **target},
            )
            world.event_log.append(
                EventLogRecord(
                    event=event,
                    realized_minute=2,
                    status=EventStatus.APPLIED,
                )
            )
            world.minute_index = 2
            record_applied_action_completion(world, event, 2, active_actions=active)
        else:
            world.minute_index = 2
            if action in {ActionKind.REST, ActionKind.SLEEP}:
                world.agents["A001"].needs.fatigue = max(
                    0.0, world.agents["A001"].needs.fatigue - 0.1
                )
            record_action_proposal(
                world,
                proposal(ActionKind.REST if action is ActionKind.WAIT else ActionKind.WAIT, 2),
                target_id=None,
                committed=True,
                active_actions=active,
            )

        transitions = [
            record.event.payload["transition"]
            for record in world.event_log.records
            if record.event.event_type == "agent_action_lifecycle"
            and record.event.payload["action"] == action.value
        ]
        assert transitions[0] == "started"
        assert transitions[-1] == "completed"
        assert action.value not in active


def _lifecycle_record(sequence: int, agent_id: object, transition: object) -> EventLogRecord:
    return EventLogRecord(
        event=ScheduledEvent(
            event_id=f"lifecycle-{sequence}",
            kind=EventKind.ENDOGENOUS,
            due_minute=sequence,
            priority=0,
            sequence=sequence,
            event_type="agent_action_lifecycle",
            payload={
                "agent_id": agent_id,
                "transition": transition,
                "action_id": f"action-{sequence}",
                "action": "rest",
                "target_id": None,
                "start_minute": sequence,
                "reason_code": "test",
                "start_activity": "idle",
                "start_needs": {},
                "model_id": "model",
                "model_source_id": "source",
            },
        ),
        realized_minute=sequence,
        status=EventStatus.APPLIED,
    )


def test_a_restored_event_log_gives_the_active_actions_without_reading_every_record() -> None:
    """Fork and resume read only the records that decide the result, in the same dict order."""
    if not keeps_saved_bytes(EventLog()):
        pytest.skip("the sheltersim_native extension is not installed")
    steps = [
        ("a", "started"),
        ("b", "started"),
        ("c", "started"),
        ("a", "continued"),  # Keeps a's place in the dict.
        ("b", "completed"),
        ("c", "paused"),  # Not a transition the index knows: changes nothing.
        ("b", "started"),  # Comes after c now.
        ("d", "started"),
        ("d", "censored"),
        (7, "started"),  # Not a str: skipped.
        ("c", "continued"),
    ]
    log = EventLog()
    for sequence, (agent, transition) in enumerate(steps):
        log.append(_lifecycle_record(sequence, agent, transition))
    full = SimpleNamespace(event_log=SimpleNamespace(of_types=log.of_types))
    expected = active_action_index(full)
    assert [(agent, row["action_id"], row["continued"]) for agent, row in expected.items()] == [
        ("a", "action-3", True),
        ("c", "action-10", True),
        ("b", "action-6", False),
    ]
    live = active_action_index(SimpleNamespace(event_log=log))
    assert list(live) == list(expected) and live == expected

    restored = EventLog.from_saved(log.items_bytes(0, len(log)))
    assert restored.unread() == len(steps)
    index = active_action_index(SimpleNamespace(event_log=restored))
    assert list(index) == list(expected) and index == expected
    assert restored.unread() == len(steps) - len(expected)

    # A copy goes on from the same state, and only the copy changes.
    copied = deepcopy(restored)
    copied.append(_lifecycle_record(len(steps), "a", "completed"))
    assert list(active_action_index(SimpleNamespace(event_log=copied))) == ["c", "b"]
    assert list(active_action_index(SimpleNamespace(event_log=restored))) == ["a", "c", "b"]


_PLAIN_RECEIPT = {
    "schema_version": 1,
    "action_id": "action-1",
    "agent_id": "A001",
    "action": "rest",
    "target_id": None,
    "transition": "started",
    "start_minute": 2,
    "transition_minute": 5,
    "duration_minutes": 3,
    "reason_code": "scheduled_decision",
    "activity": "resting",
    "start_activity": None,
    "start_needs": {"hunger": 0.5},
    "current_needs": {},
    "model_id": "model-1",
    "model_source_id": "ASM-1",
}


def test_a_plain_receipt_dumps_to_the_same_values() -> None:
    from sheltersim.contracts.operations import ActionLifecycleReceipt
    from sheltersim.domain.action_lifecycle import _is_plain_receipt

    assert _is_plain_receipt(_PLAIN_RECEIPT)
    dumped = ActionLifecycleReceipt.model_validate(_PLAIN_RECEIPT).model_dump(mode="json")
    assert dumped == _PLAIN_RECEIPT and list(dumped) == list(_PLAIN_RECEIPT)


@pytest.mark.parametrize(
    "changes",
    [
        {"start_minute": -1, "duration_minutes": 6},
        {"start_minute": True},
        {"transition_minute": 1, "duration_minutes": -1},
        {"duration_minutes": 4},
        {"transition": "paused"},
        {"reason_code": ""},
        {"reason_code": "x" * 121},
        {"activity": "x" * 81},
        {"start_activity": 1},
        {"target_id": "bad id"},
        {"action_id": "-bad"},
        {"agent_id": None},
        {"model_id": "x" * 65},
        {"model_source_id": "ok\n"},
        {"start_needs": {"hunger": float("nan")}},
        {"current_needs": {"hunger": float("inf")}},
    ],
)
def test_anything_else_is_left_to_the_receipt_model(changes) -> None:
    """Each leaves the plain path, where the model refuses it or converts its values."""
    from pydantic import ValidationError

    from sheltersim.contracts.operations import ActionLifecycleReceipt
    from sheltersim.domain.action_lifecycle import _is_plain_receipt

    payload = {**_PLAIN_RECEIPT, **changes}
    assert not _is_plain_receipt(payload)
    with pytest.raises(ValidationError):
        ActionLifecycleReceipt.model_validate(payload)


def test_needs_the_model_would_convert_are_left_to_it() -> None:
    from sheltersim.contracts.operations import ActionLifecycleReceipt
    from sheltersim.domain.action_lifecycle import _is_plain_receipt

    payload = {**_PLAIN_RECEIPT, "current_needs": {"hunger": 1}}
    assert not _is_plain_receipt(payload)
    dumped = ActionLifecycleReceipt.model_validate(payload).model_dump(mode="json")
    assert type(dumped["current_needs"]["hunger"]) is float
