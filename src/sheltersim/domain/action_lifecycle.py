"""Checkpoint-safe, event-backed lifecycle receipts for autonomous activities.

Lifecycle records are synthetic state-transition evidence. They contain no generated
utterance and are separate from selected-policy proposals and completion events.
"""
from __future__ import annotations

from typing import Any

from sheltersim.contracts.operations import ActionLifecycleReceipt
from sheltersim.core.world import WorldState
from sheltersim.domain.models import AgentState
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent

ACTION_LIFECYCLE_SOURCE_ID = "ASM-ACTION-LIFECYCLE-V1"
ACTION_LIFECYCLE_POLICY_VERSION = "applied-action-lifecycle-v2"

_COMPLETION_ACTIONS = {
    "toilet_service_complete": {"request_toilet"},
    "resource_service_complete": {"fetch_water", "collect_meal"},
    "staff_support_complete": {"seek_help", "seek_information"},
    "social_interaction": {"socialize", "care_family", "help_others"},
}
_ABANDONED_ACTIONS = {"facility_queue_abandon": {"request_toilet", "fetch_water", "collect_meal"}}
_INTERRUPTED_ACTIONS = {
    "resource_service_cancel": {"fetch_water", "collect_meal"},
    "support_request_timeout": {"seek_help", "seek_information"},
}


def active_action_index(world: WorldState) -> dict[str, dict[str, Any]]:
    """Rebuild transient lookup state from persisted event evidence after restore."""
    active: dict[str, dict[str, Any]] = {}
    for row in world.event_log.records:
        event = row.event
        if event.event_type != "agent_action_lifecycle":
            continue
        payload = event.payload
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str):
            continue
        transition = payload.get("transition")
        if transition in {"completed", "interrupted", "abandoned", "censored"}:
            active.pop(agent_id, None)
        elif transition in {"started", "continued"}:
            active[agent_id] = {
                key: payload[key]
                for key in (
                    "action_id", "agent_id", "action", "target_id", "start_minute",
                    "reason_code", "start_activity", "start_needs", "model_id",
                    "model_source_id",
                )
            }
            active[agent_id]["continued"] = transition == "continued"
    return active


def record_action_proposal(
    world: WorldState,
    proposal: Any,
    *,
    target_id: str | None,
    committed: bool,
    active_actions: dict[str, dict[str, Any]],
) -> None:
    """Start/continue an action only when the selected proposal changed actor state."""
    if not committed:
        return
    agent_id = proposal.agent_id
    agent = world.agents.get(agent_id)
    if agent is None:
        return
    current = active_actions.get(agent_id)
    action = proposal.action.value
    if current is not None:
        if current.get("action") == action and current.get("target_id") == target_id:
            if not current.get("continued", False):
                _append_transition(
                    world,
                    current,
                    "continued",
                    minute=proposal.minute_index,
                    reason_code=proposal.trigger,
                    activity=agent.activity.value,
                )
                current["continued"] = True
            return
        status, cause = _completion_status(current, agent, world)
        _append_transition(
            world,
            current,
            status,
            minute=proposal.minute_index,
            reason_code="superseded_by_new_applied_action",
            causation_id=cause,
            activity=agent.activity.value,
        )
        active_actions.pop(agent_id, None)
    sequence = world.allocate_event_sequence()
    record = {
        "action_id": f"action-{sequence}",
        "agent_id": agent_id,
        "action": action,
        "target_id": target_id,
        "start_minute": proposal.minute_index,
        "reason_code": proposal.trigger,
        "start_activity": agent.activity.value,
        "start_needs": _need_snapshot(agent, world),
        "model_id": ACTION_LIFECYCLE_POLICY_VERSION,
        "model_source_id": ACTION_LIFECYCLE_SOURCE_ID,
    }
    _append_transition(
        world,
        record,
        "started",
        minute=proposal.minute_index,
        reason_code=proposal.trigger,
        activity=agent.activity.value,
        sequence=sequence,
    )
    record["continued"] = False
    active_actions[agent_id] = record


def record_applied_action_completion(
    world: WorldState,
    event: ScheduledEvent,
    minute: int,
    *,
    active_actions: dict[str, dict[str, Any]],
) -> None:
    """Close matching active actions only when their typed applied event exists."""
    applied_record = world.event_log.find(event.event_id)
    if (
        applied_record is None
        or applied_record.status is not EventStatus.APPLIED
        or applied_record.event.to_dict() != event.to_dict()
    ):
        return
    agent_id = event.payload.get("agent_id")
    if not isinstance(agent_id, str):
        agent_id = event.payload.get("sender_agent_id")
    if not isinstance(agent_id, str):
        return
    current = active_actions.get(agent_id)
    if current is None:
        return
    event_type = event.event_type
    expected_target = current.get("target_id")
    target_keys = (
        ("request_id", "staff_id")
        if event_type in {"staff_support_complete", "support_request_timeout"}
        else ("facility_id", "participant_agent_id", "staff_id", "request_id")
    )
    observed_target = next(
        (event.payload.get(key) for key in target_keys if isinstance(event.payload.get(key), str)),
        None,
    )
    if (
        isinstance(expected_target, str)
        and isinstance(observed_target, str)
        and expected_target != observed_target
    ):
        return
    if event.due_minute < current.get("start_minute", 0):
        return
    if current.get("action") not in _COMPLETION_ACTIONS.get(event_type, set()):
        if current.get("action") not in _ABANDONED_ACTIONS.get(event_type, set()):
            if current.get("action") not in _INTERRUPTED_ACTIONS.get(event_type, set()):
                return
            status = "interrupted"
        else:
            status = "abandoned"
    else:
        status = "completed"
    _append_transition(
        world,
        current,
        status,
        minute=minute,
        reason_code=event_type,
        causation_id=event.event_id,
        activity=world.agents.get(agent_id).activity.value if agent_id in world.agents else None,
    )
    active_actions.pop(agent_id, None)


def record_applied_population_exit(
    world: WorldState,
    event: ScheduledEvent,
    minute: int,
    *,
    active_actions: dict[str, dict[str, Any]],
) -> None:
    """Interrupt actions for residents removed by an applied population event."""
    applied_record = world.event_log.find(event.event_id)
    if (
        event.event_type != "effect_program_population"
        or applied_record is None
        or applied_record.status is not EventStatus.APPLIED
        or applied_record.event.to_dict() != event.to_dict()
    ):
        return

    causes: dict[str, str] = {}
    lifecycle = world.model_state.get("population_lifecycle_v1")
    history = lifecycle.get("history", ()) if isinstance(lifecycle, dict) else ()
    for row in history:
        if not isinstance(row, dict) or row.get("event_id") != event.event_id:
            continue
        action = row.get("action")
        agent_id = row.get("agent_id")
        if action in {"temporary_exit", "transferred"} and isinstance(agent_id, str):
            causes[agent_id] = (
                "population_temporary_exit" if action == "temporary_exit"
                else "population_transfer"
            )

    population = world.model_state.get("population_dynamics_v1")
    departures = population.get("departures", ()) if isinstance(population, dict) else ()
    for row in departures:
        if (
            isinstance(row, dict)
            and row.get("event_id") == event.event_id
            and isinstance(row.get("agent_id"), str)
        ):
            causes[row["agent_id"]] = "population_departure"

    for agent_id, reason_code in sorted(causes.items()):
        current = active_actions.pop(agent_id, None)
        if current is None:
            continue
        _append_transition(
            world,
            current,
            "interrupted",
            minute=minute,
            reason_code=reason_code,
            causation_id=event.event_id,
            activity=None,
        )


def finalize_active_actions(
    world: WorldState,
    minute: int,
    *,
    active_actions: dict[str, dict[str, Any]],
) -> None:
    """Close run-open actions as completed only with observed need relief; otherwise censor."""
    for agent_id, current in sorted(tuple(active_actions.items())):
        agent = world.agents.get(agent_id)
        if agent is None:
            status = "censored"
            activity = None
        else:
            candidate, _ = _completion_status(current, agent, world)
            status = "completed" if candidate == "completed" else "censored"
            activity = agent.activity.value
        _append_transition(
            world,
            current,
            status,
            minute=minute,
            reason_code="simulation_end_right_censor",
            activity=activity,
        )
        active_actions.pop(agent_id, None)


def _completion_status(
    action: dict[str, Any], agent: AgentState, world: WorldState
) -> tuple[str, str | None]:
    action_name = action.get("action")
    if action_name == "wait":
        return ("completed", None) if world.minute_index > action.get("start_minute", 0) else ("interrupted", None)
    if action_name in {"rest", "sleep"}:
        current = _need_snapshot(agent, world)
        started = action.get("start_needs", {})
        fields = ("fatigue", "sleep_debt_minutes") if action_name == "sleep" else ("fatigue",)
        if any(
            isinstance(started.get(name), (int, float))
            and isinstance(current.get(name), (int, float))
            and current[name] < started[name]
            for name in fields
        ):
            return "completed", None
    # Typed completion/cancellation hooks close resource, support, toilet and social
    # actions at the applied event boundary. Remaining actions were displaced before
    # completing, so do not infer success from elapsed time alone.
    return "interrupted", None


def _need_snapshot(agent: AgentState, world: WorldState) -> dict[str, float]:
    body = world.model_state.get("body_state_v1", {}).get("agents", {}).get(agent.profile.id, {})
    return {
        "toilet_urgency": agent.needs.toilet_urgency,
        "hunger": agent.needs.hunger,
        "hydration_need": agent.needs.hydration_need,
        "fatigue": agent.needs.fatigue,
        "sleep_debt_minutes": float(body.get("sleep_debt_minutes", 0.0)),
        "hygiene_need": float(body.get("hygiene_need", 0.0)),
    }


def _append_transition(
    world: WorldState,
    action: dict[str, Any],
    transition: str,
    *,
    minute: int,
    reason_code: str,
    activity: str | None,
    causation_id: str | None = None,
    sequence: int | None = None,
) -> None:
    sequence = world.allocate_event_sequence() if sequence is None else sequence
    payload = {
        "schema_version": 1,
        "action_id": action["action_id"],
        "agent_id": action["agent_id"],
        "action": action["action"],
        "target_id": action.get("target_id"),
        "transition": transition,
        "start_minute": action["start_minute"],
        "transition_minute": minute,
        "duration_minutes": max(0, minute - action["start_minute"]),
        "reason_code": reason_code,
        "activity": activity,
        "start_activity": action.get("start_activity"),
        "start_needs": dict(action.get("start_needs", {})),
        "current_needs": _need_snapshot(world.agents[action["agent_id"]], world)
        if action["agent_id"] in world.agents
        else {},
        "model_id": action["model_id"],
        "model_source_id": action["model_source_id"],
    }
    payload = ActionLifecycleReceipt.model_validate(payload).model_dump(mode="json")
    event = ScheduledEvent(
        event_id=f"action-lifecycle-{sequence}",
        kind=EventKind.ENDOGENOUS,
        due_minute=minute,
        priority=0,
        sequence=sequence,
        event_type="agent_action_lifecycle",
        payload=payload,
        source_id=ACTION_LIFECYCLE_POLICY_VERSION,
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": ACTION_LIFECYCLE_SOURCE_ID,
            "policy_version": ACTION_LIFECYCLE_POLICY_VERSION,
        },
    )
    world.event_log.append(
        EventLogRecord(
            event=event,
            realized_minute=minute,
            status=EventStatus.APPLIED,
            detail=f"autonomous action {transition}",
        )
    )

