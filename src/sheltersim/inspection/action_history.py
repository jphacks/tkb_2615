"""Project applied completion/cancellation events, never selected proposals."""

from __future__ import annotations

from typing import Any

_COMPLETED = {
    "toilet_service_complete": "toilet_service",
    "resource_service_complete": "resource_service",
    "agent_travel_complete": "travel",
    "staff_support_complete": "staff_support",
    "staff_operation_complete": "staff_operation",
}
_INTERRUPTED = {
    "resource_service_cancel": "resource_service",
    "facility_queue_abandon": "facility_queue",
}
_STRUCTURED_CONTACTS = {
    "social_interaction": "conversation",
    "information_contact_update": "information_contact",
    "proximity_contact_update": "observed_proximity_contact",
}


def applied_action_record(event: dict[str, Any], minute: int) -> dict[str, Any] | None:
    kind = event["event_type"]
    if kind == "staff_support_continuity":
        from sheltersim.contracts.operations import StaffSupportContinuityReceipt

        try:
            receipt = StaffSupportContinuityReceipt.model_validate(event.get("payload"))
        except (TypeError, ValueError):
            return None
        if (
            receipt.event_id != event.get("event_id")
            or receipt.minute != minute
            or receipt.causation_id != event.get("causation_id")
        ):
            return None
        row = receipt.model_dump(mode="json")
        row.update(
            {
                "record_kind": "care",
                "time_semantics": "recorded_event_minute",
                "staff_id": receipt.previous_staff_id,
                "source_id": event.get("source_id"),
                "status": receipt.status,
                "evidence_semantics": "applied_staff_support_continuity_after_interruption",
            }
        )
        return row
    if kind == "staff_operation_preempted":
        payload = event.get("payload", {})
        if set(payload) != {"staff_id", "task_id", "request_id", "reason_code"} or any(
            not isinstance(payload.get(field), str)
            for field in ("staff_id", "task_id", "request_id", "reason_code")
        ):
            return None
        return {
            "schema_version": 1,
            "record_kind": "operations",
            "record_type": "staff_operation_preemption",
            "event_id": event["event_id"],
            "minute": minute,
            "time_semantics": "recorded_event_minute",
            "staff_id": payload["staff_id"],
            "task_id": payload["task_id"],
            "request_id": payload["request_id"],
            "reason_code": payload["reason_code"],
            "source_id": event.get("source_id"),
            "causation_id": event.get("causation_id"),
            "status": "preempted",
            "evidence_semantics": "applied_priority_support_preemption",
        }
    if kind == "agent_action_lifecycle":
        payload = event["payload"]
        transition = payload.get("transition")
        terminal = transition in {"completed", "interrupted", "abandoned", "censored"}
        return {
            "schema_version": 1,
            "record_kind": "actions",
            "event_id": event["event_id"],
            "action_id": payload.get("action_id"),
            "action": payload.get("action"),
            "agent_id": payload.get("agent_id"),
            "target_id": payload.get("target_id"),
            "minute": minute,
            "completion_minute": minute if terminal and transition != "censored" else None,
            "start_minute": payload.get("start_minute"),
            "start_time_status": "recorded",
            "duration_minutes": payload.get("duration_minutes"),
            "status": transition,
            "outcome": transition,
            "transition": transition,
            "reason_code": payload.get("reason_code"),
            "activity": payload.get("activity"),
            "start_activity": payload.get("start_activity"),
            "current_needs": payload.get("current_needs", {}),
            "causation_id": event.get("causation_id"),
            "source_id": event.get("source_id"),
            "model_id": payload.get("model_id"),
            "model_source_id": payload.get("model_source_id"),
            "applied_event_ids": [
                event["event_id"],
                *([event["causation_id"]] if event.get("causation_id") else []),
            ],
            "evidence_semantics": "applied_typed_action_lifecycle_transition",
        }
    action = _COMPLETED.get(kind) or _INTERRUPTED.get(kind) or _STRUCTURED_CONTACTS.get(kind)
    staff_role_update = (
        kind == "effect_program_structure"
        and event.get("payload", {}).get("op") == "set_staff_role"
    )
    if staff_role_update:
        action = "staff_role_update"
    if action is None:
        return None
    payload = event["payload"]
    provenance = event.get("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
    if kind == "agent_travel_complete":
        purpose = str(payload.get("purpose", ""))
        if purpose.endswith(("_queue_position", "_queue_service")):
            action = "queue_movement"
        elif purpose == "queue_abandon":
            action = "queue_exit_movement"
    start_minute = payload.get("start_minute")
    start_time_status = "recorded" if "start_minute" in payload else "not_recorded"
    if (
        kind == "social_interaction"
        and type(payload.get("interaction_duration_minutes")) is int
        and type(event.get("due_minute")) is int
    ):
        start_minute = event["due_minute"] - payload["interaction_duration_minutes"]
        start_time_status = "derived_from_duration_and_due_minute"
    row = {
        "schema_version": 1,
        "record_kind": "actions",
        "event_id": event["event_id"],
        "action_id": payload.get("task_id", event["event_id"]),
        "action": action,
        "agent_id": payload.get("agent_id", payload.get("sender_agent_id")),
        "staff_id": payload.get("staff_id"),
        "target_id": payload.get(
            "facility_id",
            payload.get(
                "staff_id",
                payload.get(
                    "zone_id",
                    payload.get(
                        "destination_zone_id",
                        payload.get("participant_agent_id", payload.get("recipient_agent_id")),
                    ),
                ),
            ),
        ),
        "purpose": payload.get("purpose"),
        "minute": minute,
        "completion_minute": minute
        if kind in _COMPLETED or staff_role_update or kind in _STRUCTURED_CONTACTS
        else None,
        "start_minute": start_minute,
        "start_time_status": start_time_status,
        "status": "completed"
        if kind in _COMPLETED or staff_role_update or kind in _STRUCTURED_CONTACTS
        else "interrupted",
        "outcome": "completed"
        if kind in _COMPLETED or staff_role_update or kind in _STRUCTURED_CONTACTS
        else "interrupted",
        "reason_code": event.get("payload", {}).get("reason_short", kind),
        "causation_id": event.get("causation_id"),
        "source_id": event["source_id"],
        "applied_event_ids": [event["event_id"]],
        "evidence_semantics": "applied_completion_or_cancellation_event_not_a_selected_proposal",
    }
    if staff_role_update:
        row["skills"] = event.get("payload", {}).get("skills", [])
    if kind in _STRUCTURED_CONTACTS:
        if kind == "social_interaction":
            row["interaction_type"] = payload.get("interaction_type")
            row["participant_agent_id"] = payload.get("participant_agent_id")
            row["zone_id"] = payload.get("zone_id")
            row["topic_id"] = payload.get("topic_id")
            row["interaction_duration_minutes"] = payload.get("interaction_duration_minutes")
            row["action_detail"] = payload.get("action")
        else:
            if kind == "proximity_contact_update":
                row.update(
                    {
                        "status": "observed",
                        "outcome": "observed",
                        "completion_minute": None,
                        "start_time_status": "point_observation",
                        "agent_id": payload.get("first_agent_id"),
                        "participant_agent_id": payload.get("second_agent_id"),
                        "zone_id": payload.get("zone_id"),
                        "observed_minute": payload.get("observed_minute"),
                        "contact_distance_m": payload.get("distance_m"),
                        "contact_minutes": 0,
                        "first_point": payload.get("first_point"),
                        "second_point": payload.get("second_point"),
                        "speech_recorded": False,
                        "evidence_semantics": "measured_physical_proximity_without_dialogue_claim",
                    }
                )
                return row
            row["interaction_type"] = "information_contact"
            row["sender_agent_id"] = payload.get("sender_agent_id")
            row["participant_agent_id"] = payload.get("recipient_agent_id")
            row["topic_id"] = payload.get("message_id")
            row["belief"] = payload.get("belief")
            row["contact_basis"] = payload.get("contact_basis")
            row["contact_minutes"] = payload.get("contact_minutes")
    if kind == "agent_travel_complete" and action == "queue_movement":
        row["queue_phase"] = provenance.get("queue_phase")
        row["queue_position"] = (
            provenance["fifo_position"] + 1
            if type(provenance.get("fifo_position")) is int
            else None
        )
        row["queue_requested_at"] = provenance.get("queue_requested_at")
        row["planned_travel_minutes"] = payload.get("travel_minutes")
    if kind == "facility_queue_abandon":
        row["service"] = payload.get("service")
        row["wait_minutes"] = payload.get("wait_minutes")
        row["retry_at_minute"] = payload.get("retry_at_minute")
    return row
