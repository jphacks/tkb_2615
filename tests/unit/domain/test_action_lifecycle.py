"""An Agent's open action ends only through a matching applied event, and every
behavior action has a lifecycle terminal."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import (
    active_action_index,
    record_action_proposal,
    record_applied_action_completion,
)
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import ActionKind

FIXTURE = Path(__file__).parents[3] / "fixtures" / "scenarios" / "h00_small_replay.json"


def make_world() -> WorldState:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
        world, proposal(ActionKind.WAIT, 0), target_id=None,
        committed=True, active_actions=active,
    )
    world.minute_index = 1
    record_action_proposal(
        world, proposal(ActionKind.WAIT, 1), target_id=None,
        committed=True, active_actions=active,
    )
    world.minute_index = 3
    record_action_proposal(
        world, proposal(ActionKind.REST, 3), target_id=None,
        committed=True, active_actions=active,
    )

    rows = [
        record.event.payload for record in world.event_log.records
        if record.event.event_type == "agent_action_lifecycle"
    ]
    assert [row["transition"] for row in rows] == [
        "started", "continued", "completed", "started"
    ]
    assert rows[2]["duration_minutes"] == 3
    restored_index = active_action_index(world)
    assert restored_index["A001"]["action"] == ActionKind.REST.value


def test_action_completion_requires_matching_applied_event_log_record() -> None:
    world = make_world()
    active: dict[str, dict[str, object]] = {}
    record_action_proposal(
        world, proposal(ActionKind.FETCH_WATER, 0), target_id="water_station_1",
        committed=True, active_actions=active,
    )
    failed_sequence = world.allocate_event_sequence()
    failed = ScheduledEvent(
        event_id="failed-completion", kind=EventKind.COMPLETION,
        due_minute=2, priority=0, sequence=failed_sequence,
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
        event_id="applied-completion", kind=EventKind.COMPLETION,
        due_minute=2, priority=0, sequence=applied_sequence,
        event_type="resource_service_complete",
        payload={"agent_id": "A001", "facility_id": "water_station_1"},
    )
    world.event_log.append(
        EventLogRecord(event=applied, realized_minute=2, status=EventStatus.APPLIED)
    )
    record_applied_action_completion(world, applied, 2, active_actions=active)
    assert "A001" not in active
    lifecycle = [
        record.event.payload for record in world.event_log.records
        if record.event.event_type == "agent_action_lifecycle"
    ]
    assert lifecycle[-1]["transition"] == "completed"
    last_transition = next(
        record for record in reversed(world.event_log.records)
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
            "A002" if action in {
                ActionKind.SOCIALIZE, ActionKind.CARE_FAMILY, ActionKind.HELP_OTHERS
            }
            else "support-request" if action is ActionKind.SEEK_HELP
            else "info-request" if action is ActionKind.SEEK_INFORMATION
            else "meal_station_1" if action is ActionKind.COLLECT_MEAL
            else "water_station_1" if action is ActionKind.FETCH_WATER
            else "toilet_1" if action is ActionKind.REQUEST_TOILET
            else None
        )
        record_action_proposal(
            world, proposal(action, 0), target_id=target_id,
            committed=True, active_actions=active,
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
            world.event_log.append(EventLogRecord(
                event=event,
                realized_minute=2,
                status=EventStatus.APPLIED,
            ))
            world.minute_index = 2
            record_applied_action_completion(
                world, event, 2, active_actions=active
            )
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
                committed=True, active_actions=active,
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
