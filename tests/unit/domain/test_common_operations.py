"""Staff support continuity receipts stay valid across schema versions."""

from __future__ import annotations

from sheltersim.contracts.operations import StaffSupportContinuityReceipt
from sheltersim.domain.common_operations import _event_payload_matches
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent


def test_pre_cause_type_continuity_receipt_remains_valid() -> None:
    payload = {
        "record_type": "care",
        "schema_version": 1,
        "event_type": "staff_support_continuity",
        "event_id": "support-continuity-legacy",
        "request_id": "request-legacy",
        "agent_id": "A001",
        "task_id": "task-legacy",
        "previous_staff_id": "S01",
        "status": "requeued",
        "minute": 3,
        "causation_id": "staff-off-legacy",
        "source_ids": ["ASM-STAFF-SUPPORT-LIFECYCLE-V1"],
    }
    receipt = StaffSupportContinuityReceipt.model_validate(payload)
    event = ScheduledEvent(
        event_id=receipt.event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=receipt.minute,
        priority=0,
        sequence=8,
        event_type=receipt.event_type,
        payload=payload,
        source_id="ASM-STAFF-SUPPORT-LIFECYCLE-V1",
        causation_id=receipt.causation_id,
    )
    record = EventLogRecord(
        event=event,
        realized_minute=receipt.minute,
        status=EventStatus.APPLIED,
        detail="legacy continuity record",
    )

    assert receipt.cause_type == "staff_unavailable"
    assert _event_payload_matches(record, receipt)
