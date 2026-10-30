"""Event-backed producer helpers for the shared care operation contract.

This module projects only care transitions that already happened in the WorldState.
It does not create a support request or claim a completed outcome by itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from sheltersim.contracts.operations import (
    SHARED_OPERATIONS_KEY,
    CareTransitionReceipt,
    FacilityRecoveryReceipt,
    NumericStateChange,
    OperationHistoryRecord,
    StaffHandoffReceipt,
    StaffOperationReceipt,
    StaffSupportContinuityReceipt,
)
from sheltersim.contracts.population import population_context_people
from sheltersim.core.world import WorldState
from sheltersim.domain.common_operations import record_shared_operation
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent

CARE_RECEIPT_SOURCE_ID = "ASM-CARE-TRANSITION-PRODUCER-V1"
CARE_RECEIPT_POLICY_VERSION = "applied-care-transition-v1"
FACILITY_RECEIPT_SOURCE_ID = "ASM-FACILITY-RECOVERY-PRODUCER-V1"
STAFF_HANDOFF_SOURCE_ID = "ASM-STAFF-HANDOFF-PRODUCER-V1"
STAFF_OPERATION_RECEIPT_SOURCE_ID = "ASM-STAFF-OPERATION-RECEIPT-V1"


def make_care_transition_record(
    world: WorldState,
    *,
    request_id: str,
    agent_id: str,
    household_id: str | None,
    support_kind: str,
    status: str,
    staff_id: str | None = None,
    outcome_observation: str = "not_applicable",
    state_changes: tuple[NumericStateChange, ...] = (),
    causation_id: str | None = None,
    source_ids: tuple[str, ...] = (),
) -> tuple[EventLogRecord, CareTransitionReceipt]:
    """Create a bounded applied evidence event for one observed care transition."""
    sequence = world.allocate_event_sequence()
    event_id = f"care-support-transition-{sequence}"
    all_sources = tuple(sorted({CARE_RECEIPT_SOURCE_ID, *source_ids}))
    receipt = CareTransitionReceipt(
        event_id=event_id,
        request_id=request_id,
        agent_id=agent_id,
        household_id=household_id,
        support_kind=support_kind,
        status=status,
        minute=world.minute_index,
        staff_id=staff_id,
        outcome_observation=outcome_observation,
        state_changes=state_changes,
        causation_id=causation_id,
        source_ids=all_sources,
    )
    event = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=sequence,
        event_type=receipt.event_type,
        payload=receipt.model_dump(mode="json"),
        source_id=CARE_RECEIPT_SOURCE_ID,
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": CARE_RECEIPT_SOURCE_ID,
            "policy_version": CARE_RECEIPT_POLICY_VERSION,
        },
    )
    return (
        EventLogRecord(
            event=event,
            realized_minute=world.minute_index,
            status=EventStatus.APPLIED,
            detail=f"care transition {status} recorded after state application",
        ),
        receipt,
    )


def make_facility_recovery_record(
    world: WorldState,
    *,
    facility_id: str,
    component_id: str,
    status: str,
    previous_working_units: int,
    working_units: int,
    causation_id: str,
    source_ids: tuple[str, ...] = (),
) -> tuple[EventLogRecord, FacilityRecoveryReceipt] | None:
    """Create a recovery receipt only when the shared operation contract is enabled."""
    if SHARED_OPERATIONS_KEY not in world.model_state:
        return None
    sequence = world.allocate_event_sequence()
    event_id = f"facility-recovery-transition-{sequence}"
    all_sources = tuple(sorted({FACILITY_RECEIPT_SOURCE_ID, *source_ids}))
    receipt = FacilityRecoveryReceipt(
        event_id=event_id,
        facility_id=facility_id,
        component_id=component_id,
        status=status,
        minute=world.minute_index,
        previous_working_units=previous_working_units,
        working_units=working_units,
        source_ids=all_sources,
        causation_id=causation_id,
    )
    event = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=sequence,
        event_type=receipt.event_type,
        payload=receipt.model_dump(mode="json"),
        source_id=FACILITY_RECEIPT_SOURCE_ID,
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": FACILITY_RECEIPT_SOURCE_ID,
            "policy_version": "applied-facility-recovery-v1",
        },
    )
    return (
        EventLogRecord(
            event=event,
            realized_minute=world.minute_index,
            status=EventStatus.APPLIED,
            detail=f"facility {status} receipt recorded after working-unit update",
        ),
        receipt,
    )


def make_staff_operation_record(
    world: WorldState,
    *,
    operation: dict[str, Any],
    state_changes: tuple[NumericStateChange, ...],
    outcome_measured: bool,
    causation_id: str,
) -> tuple[EventLogRecord, StaffOperationReceipt] | None:
    """Create bounded applied evidence for a completed autonomous staff task."""
    if SHARED_OPERATIONS_KEY not in world.model_state:
        return None
    all_changes = tuple(
        sorted(
            state_changes,
            key=lambda row: (row.entity_kind, row.entity_id, row.field_path),
        )
    )
    sampled_changes = all_changes[:256]
    source_ids = (STAFF_OPERATION_RECEIPT_SOURCE_ID,)
    sequence = world.allocate_event_sequence()
    event_id = f"staff-operation-transition-{sequence}"
    receipt = StaffOperationReceipt(
        event_id=event_id,
        task_id=operation["task_id"],
        staff_id=operation["staff_id"],
        task_kind=operation["task_kind"],
        target_id=operation["target_id"],
        target_zone_id=operation["target_zone_id"],
        reason=operation["reason"],
        status=operation["status"],
        minute=world.minute_index,
        start_minute=operation["start_minute"],
        arrival_minute=operation["arrival_minute"],
        travel_minutes=operation["travel_minutes"],
        outcome_observation=(
            "observed_change"
            if all_changes
            else "observed_no_change"
            if outcome_measured
            else "not_measured"
        ),
        state_changes=sampled_changes,
        total_state_change_count=len(all_changes),
        state_changes_truncated=len(all_changes) > len(sampled_changes),
        causation_id=causation_id,
        source_ids=source_ids,
    )
    event = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=sequence,
        event_type=receipt.event_type,
        payload=receipt.model_dump(mode="json"),
        source_id=STAFF_OPERATION_RECEIPT_SOURCE_ID,
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": STAFF_OPERATION_RECEIPT_SOURCE_ID,
            "policy_version": "applied-staff-operation-receipt-v1",
        },
    )
    return (
        EventLogRecord(
            event=event,
            realized_minute=world.minute_index,
            status=EventStatus.APPLIED,
            detail=f"staff operation {receipt.status} outcome recorded after state application",
        ),
        receipt,
    )


def make_staff_handoff_record(
    world: WorldState,
    *,
    task_id: str,
    previous_staff_id: str,
    replacement_staff_id: str,
    reason_code: str,
    causation_id: str,
    source_ids: tuple[str, ...] = (),
) -> tuple[EventLogRecord, StaffHandoffReceipt] | None:
    """Create a receipt for a staff replacement after assignment state is updated."""
    if SHARED_OPERATIONS_KEY not in world.model_state:
        return None
    sequence = world.allocate_event_sequence()
    event_id = f"staff-handoff-{sequence}"
    all_sources = tuple(sorted({STAFF_HANDOFF_SOURCE_ID, *source_ids}))
    receipt = StaffHandoffReceipt(
        event_id=event_id,
        task_id=task_id,
        previous_staff_id=previous_staff_id,
        replacement_staff_id=replacement_staff_id,
        status="reassigned",
        minute=world.minute_index,
        reason_code=reason_code,
        causation_id=causation_id,
        source_ids=all_sources,
    )
    event = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=sequence,
        event_type=receipt.event_type,
        payload=receipt.model_dump(mode="json"),
        source_id=STAFF_HANDOFF_SOURCE_ID,
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": STAFF_HANDOFF_SOURCE_ID,
            "policy_version": "applied-staff-handoff-v1",
        },
    )
    return (
        EventLogRecord(
            event=event,
            realized_minute=world.minute_index,
            status=EventStatus.APPLIED,
            detail="staff task assignment updated before handoff receipt",
        ),
        receipt,
    )


def make_staff_support_continuity_record(
    world: WorldState,
    *,
    request_id: str,
    agent_id: str,
    task_id: str,
    previous_staff_id: str,
    status: str,
    causation_id: str,
    cause_type: str = "staff_unavailable",
) -> EventLogRecord:
    """Create applied evidence for a request interrupted after staff assignment."""
    sequence = world.allocate_event_sequence()
    event_id = f"staff-support-continuity-{sequence}"
    receipt = StaffSupportContinuityReceipt(
        event_id=event_id,
        request_id=request_id,
        agent_id=agent_id,
        task_id=task_id,
        previous_staff_id=previous_staff_id,
        status=status,
        cause_type=cause_type,
        minute=world.minute_index,
        causation_id=causation_id,
        source_ids=("ASM-STAFF-SUPPORT-LIFECYCLE-V1",),
    )
    event = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=world.minute_index,
        priority=0,
        sequence=sequence,
        event_type=receipt.event_type,
        payload=receipt.model_dump(mode="json"),
        source_id="ASM-STAFF-SUPPORT-LIFECYCLE-V1",
        causation_id=causation_id,
        provenance={
            "source_type": "assumption",
            "source_id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
            "policy_version": "staff-support-continuity-v1",
        },
    )
    return EventLogRecord(
        event=event,
        realized_minute=world.minute_index,
        status=EventStatus.APPLIED,
        detail=f"staff support request was {status} after assigned staff became unavailable",
    )


def append_applied_care_transition(
    world: WorldState,
    *,
    request_id: str,
    agent_id: str,
    household_id: str | None,
    support_kind: str,
    status: str,
    staff_id: str | None = None,
    causation_id: str | None = None,
    source_ids: tuple[str, ...] = (),
) -> CareTransitionReceipt | None:
    """Append and project a care transition after its backing operation has applied."""
    if SHARED_OPERATIONS_KEY not in world.model_state:
        return None
    event_record, receipt = make_care_transition_record(
        world,
        request_id=request_id,
        agent_id=agent_id,
        household_id=household_id,
        support_kind=support_kind,
        status=status,
        staff_id=staff_id,
        causation_id=causation_id,
        source_ids=source_ids,
    )
    world.event_log.append(event_record)
    record_shared_operation(world, receipt)
    return receipt


def ingest_applied_shared_operation_records(world: WorldState, outcomes: tuple[Any, ...]) -> None:
    """Project typed shared receipts only after EventLog APPLIED insertion."""
    if SHARED_OPERATIONS_KEY not in world.model_state:
        return
    for outcome in outcomes:
        event = getattr(outcome, "event", None)
        if (
            getattr(outcome, "status", None) is not EventStatus.APPLIED
            or event is None
            or event.event_type
            not in {
                "care_support_transition",
                "staff_support_continuity",
                "facility_recovery_transition",
                "staff_handoff",
                "staff_operation_transition",
            }
        ):
            continue
        state = world.model_state.get(SHARED_OPERATIONS_KEY, {})
        if isinstance(state, dict) and any(
            row.get("event_id") == event.event_id
            for row in state.get("history", ())
            if isinstance(row, dict)
        ):
            continue
        receipt = TypeAdapter(OperationHistoryRecord).validate_python(event.payload)
        record_shared_operation(world, receipt)


def care_receipt_for_request(world: WorldState, request_id: str) -> dict[str, Any] | None:
    """Return the active shared request projection, if this request was typed as care."""
    state = world.model_state.get(SHARED_OPERATIONS_KEY)
    requests = state.get("support_requests") if isinstance(state, dict) else None
    record = requests.get(request_id) if isinstance(requests, dict) else None
    return record if isinstance(record, dict) else None


def care_history_for_agent(
    world: WorldState, agent_id: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Project only applied typed care receipts for one person, newest last.

    This is an inspection view over EventLog, not a second mutable care ledger.
    The immutable population context remains unchanged and no clinical outcome is
    inferred from a completed staff task.
    """
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent_id must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("care history limit must be between 1 and 100")
    return care_history_by_agent(world, limit=limit).get(agent_id, [])


def care_history_by_agent(world: WorldState, *, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    """Build bounded per-person views with one pass over the retained EventLog."""
    from sheltersim.contracts.operations import CareTransitionReceipt

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("care history limit must be between 1 and 100")
    rows: dict[str, list[dict[str, Any]]] = {}
    # The care event types' records, in append order, without reading the others.
    for record in world.event_log.of_types({"care_support_transition", "staff_support_continuity"}):
        event = record.event
        if record.status is not EventStatus.APPLIED:
            continue
        if event.event_type == "staff_support_continuity":
            receipt = StaffSupportContinuityReceipt.model_validate(event.payload)
            if (
                receipt.event_id != event.event_id
                or receipt.minute != record.realized_minute
                or receipt.causation_id != event.causation_id
            ):
                continue
            evidence_semantics = "applied_staff_support_continuity_after_interruption"
        else:
            receipt = CareTransitionReceipt.model_validate(event.payload)
            if (
                receipt.event_id != event.event_id
                or receipt.minute != record.realized_minute
                or receipt.causation_id != event.causation_id
            ):
                continue
            evidence_semantics = "applied_typed_care_transition"
        row = receipt.model_dump(mode="json")
        row["realized_minute"] = record.realized_minute
        row["source_id"] = event.source_id
        row["time_semantics"] = "recorded_event_minute"
        row["evidence_semantics"] = evidence_semantics
        affected_ids = {receipt.agent_id}
        if event.event_type == "care_support_transition":
            affected_ids.update(change.entity_id for change in receipt.state_changes)
        for identity in sorted(affected_ids):
            person_row = {
                **row,
                "person_role_in_record": (
                    "request_person"
                    if identity == receipt.agent_id
                    else "explicit_related_state_change"
                ),
            }
            history = rows.setdefault(identity, [])
            history.append(person_row)
            del history[:-limit]
    return rows


def support_kind_for_agent(world: WorldState, agent_id: str) -> str:
    """Select only care subtypes explicitly present in synthetic population context."""
    person = population_context_people(world).get(agent_id, {})
    if not isinstance(person, dict):
        return "emotional_support"
    configured = person.get("care_support_needs", ())
    completed = world.event_log.completed_support_kinds(agent_id)
    if isinstance(configured, (list, tuple)):
        for support_kind in configured:
            if isinstance(support_kind, str) and support_kind not in completed:
                return support_kind
    if person.get("medication_support") is True:
        if "medication_support" not in completed:
            return "medication_support"
    if person.get("medical_support") is True:
        if "medical_assessment" not in completed:
            return "medical_assessment"
    return "emotional_support"


def care_source_ids_for_agent(world: WorldState, agent_id: str) -> tuple[str, ...]:
    person = population_context_people(world).get(agent_id, {})
    sources = person.get("source_ids", ()) if isinstance(person, dict) else ()
    return tuple(
        sorted(
            {
                "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
                *(item for item in sources if isinstance(item, str)),
            }
        )
    )


def explicit_care_dependents(
    world: WorldState, caregiver_id: str, *, household_id: str | None = None
) -> tuple[str, ...]:
    """Return only active, reviewed, reciprocal caregiver links in one household."""
    people = population_context_people(world)
    caregiver = people.get(caregiver_id)
    if not isinstance(caregiver, dict) or caregiver.get("requires_review", False):
        return ()
    dependents = caregiver.get("care_for_ids", ())
    caregiver_household = caregiver.get("household_id")
    if (
        not isinstance(dependents, (list, tuple))
        or not dependents
        or not isinstance(caregiver_household, str)
        or (household_id is not None and household_id != caregiver_household)
        or not caregiver.get("source_ids")
    ):
        return ()
    linked: list[str] = []
    for dependent_id in dependents:
        dependent = people.get(dependent_id)
        if (
            not isinstance(dependent_id, str)
            or dependent_id not in world.agents
            or not isinstance(dependent, dict)
            or dependent.get("requires_review", False)
            or dependent.get("household_id") != caregiver_household
            or caregiver_id not in dependent.get("caregiver_ids", ())
            or not dependent.get("source_ids")
        ):
            continue
        linked.append(dependent_id)
    return tuple(sorted(set(linked)))
