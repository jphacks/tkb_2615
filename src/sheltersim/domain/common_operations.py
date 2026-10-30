"""Checkpointed shared care/staff/facility transition records and safe projections."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from sheltersim.contracts.operations import (
    MAX_SHARED_OPERATION_HISTORY,
    SHARED_OPERATIONS_KEY,
    SHARED_OPERATIONS_POLICY,
    CareRequestState,
    CareTransitionReceipt,
    FacilityRecoveryReceipt,
    OperationalDemandSnapshot,
    OperationHistoryRecord,
    SharedOperationsState,
    StaffHandoffReceipt,
    StaffOperationReceipt,
    StaffSupportContinuityReceipt,
)
from sheltersim.core.history import records_since_previous_tick
from sheltersim.core.world import WorldState
from sheltersim.domain.geometry_overlay import parsed_facility_geometry
from sheltersim.events.models import EventStatus

_CARE_TRANSITIONS = {
    None: {"requested"},
    "requested": {"assigned", "started", "declined", "interrupted"},
    "assigned": {"started", "declined", "interrupted"},
    "started": {"completed", "interrupted"},
}


def create_shared_operations_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_version": SHARED_OPERATIONS_POLICY,
        "support_requests": {},
        "history": [],
        "history_truncated": False,
    }


def validate_shared_operations_state(
    world: WorldState, *, validate_event_history: bool = True
) -> None:
    state = world.model_state.get(SHARED_OPERATIONS_KEY)
    if state is None:
        return
    # Per-tick validation parses only the records from the tick that just ended (each
    # was checked against its event when appended); the run-boundary validation
    # replays the whole history and the care lifecycle.
    checked = state
    history = state.get("history") if isinstance(state, dict) else None
    if not validate_event_history and isinstance(history, list):
        checked = {**state, "history": records_since_previous_tick(history, world.minute_index)}
    try:
        parsed = SharedOperationsState.model_validate(checked)
    except (TypeError, ValueError) as exc:
        raise ValueError("shared_operations_v1 has an invalid exact schema") from exc
    if len(state["history"]) > MAX_SHARED_OPERATION_HISTORY or any(
        a.minute > b.minute for a, b in zip(parsed.history, parsed.history[1:])
    ):
        raise ValueError("shared_operations_v1 history is over limit or out of time order")
    seen_events: set[str] = set()
    care: dict[str, CareRequestState] = {}
    for item in parsed.history:
        if item.minute > world.minute_index or item.event_id in seen_events:
            raise ValueError(
                "shared_operations_v1 history has future, duplicate, or unordered records"
            )
        seen_events.add(item.event_id)
        event_record = world.event_log.find(item.event_id)
        if (
            event_record is None
            or event_record.status is not EventStatus.APPLIED
            or event_record.realized_minute != item.minute
            or event_record.event.event_type != item.event_type
            or not _event_payload_matches(event_record, item)
        ):
            raise ValueError("shared_operations_v1 record must link to a realized applied event")
        if isinstance(item, StaffOperationReceipt):
            _validate_staff_operation_receipt(
                world, item, history_truncated=parsed.history_truncated
            )
        if not validate_event_history:
            continue
        if isinstance(item, CareTransitionReceipt):
            prior = care.get(item.request_id)
            if prior is None:
                if item.status != "requested" and not parsed.history_truncated:
                    raise ValueError("a support request must begin with a requested transition")
                saved = parsed.support_requests.get(item.request_id)
                created = (
                    saved.created_minute if parsed.history_truncated and saved else item.minute
                )
                if created > item.minute:
                    raise ValueError("truncated care history has an invalid creation minute")
            else:
                if (prior.agent_id, prior.household_id, prior.support_kind) != (
                    item.agent_id,
                    item.household_id,
                    item.support_kind,
                ):
                    raise ValueError("support request identity changed during its lifecycle")
                if item.status not in _CARE_TRANSITIONS.get(prior.status, set()):
                    raise ValueError("support lifecycle transition is out of order")
                created = prior.created_minute
            care[item.request_id] = CareRequestState(
                request_id=item.request_id,
                agent_id=item.agent_id,
                household_id=item.household_id,
                support_kind=item.support_kind,
                status=item.status,
                created_minute=created,
                updated_minute=item.minute,
                latest_event_id=item.event_id,
            )
        elif isinstance(item, StaffSupportContinuityReceipt):
            prior = care.get(item.request_id)
            saved = parsed.support_requests.get(item.request_id)
            if prior is None:
                if not parsed.history_truncated or (item.status == "requeued" and saved is None):
                    raise ValueError("support continuity must follow an active care transition")
                if saved is not None and (
                    saved.agent_id != item.agent_id
                    or saved.latest_event_id != item.event_id
                    or saved.status != "requested"
                ):
                    raise ValueError(
                        "truncated support continuity does not match the active request"
                    )
                created = saved.created_minute if saved is not None else item.minute
            else:
                if prior.status not in {"assigned", "started"} or prior.agent_id != item.agent_id:
                    raise ValueError("support continuity does not follow assigned resident care")
                created = prior.created_minute
            if item.status == "requeued":
                care[item.request_id] = CareRequestState(
                    request_id=item.request_id,
                    agent_id=item.agent_id,
                    household_id=prior.household_id if prior is not None else saved.household_id,
                    support_kind=prior.support_kind if prior is not None else saved.support_kind,
                    status="requested",
                    created_minute=created,
                    updated_minute=item.minute,
                    latest_event_id=item.event_id,
                )
            else:
                care.pop(item.request_id, None)
    saved_care = dict(parsed.support_requests)
    active_care = {
        key: value
        for key, value in care.items()
        if value.status in {"requested", "assigned", "started"}
    }
    for request_id, replayed in active_care.items():
        if saved_care.get(request_id) != replayed:
            raise ValueError(
                "shared_operations_v1 care request head does not match its retained history"
            )
    for request_id, head in saved_care.items():
        if head.status not in {"requested", "assigned", "started"}:
            raise ValueError("shared_operations_v1 stores a terminal request as active")
        request = world.support_requests.get(request_id)
        if request is None or request.agent_id != head.agent_id:
            raise ValueError(
                "shared_operations_v1 care head has no matching persisted support request"
            )
        expected_status = "pending" if head.status == "requested" else "assigned"
        if request.status.value != expected_status:
            raise ValueError(
                "shared_operations_v1 care status disagrees with the persisted support request"
            )
        if request_id in active_care or not validate_event_history:
            if head.status in {"assigned", "started"}:
                evidence = world.event_log.find(head.latest_event_id)
                staff_id = evidence.event.payload.get("staff_id") if evidence is not None else None
                if request.assigned_staff_id != staff_id:
                    raise ValueError(
                        "shared_operations_v1 assigned staff disagrees with the support task"
                    )
            continue
        if not parsed.history_truncated:
            raise ValueError("shared_operations_v1 care request head has no matching history")
        record = world.event_log.find(head.latest_event_id)
        if (
            record is None
            or record.status is not EventStatus.APPLIED
            or record.realized_minute != head.updated_minute
            or record.event.event_type
            not in {"care_support_transition", "staff_support_continuity"}
        ):
            raise ValueError("truncated care request head has no applied event evidence")
        try:
            latest = TypeAdapter(OperationHistoryRecord).validate_python(
                record.event.to_dict()["payload"]
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("truncated care request head event has an invalid receipt") from exc
        receipt_matches = (
            isinstance(latest, CareTransitionReceipt)
            and (
                latest.request_id,
                latest.agent_id,
                latest.household_id,
                latest.support_kind,
                latest.status,
                latest.event_id,
            )
            == (
                request_id,
                head.agent_id,
                head.household_id,
                head.support_kind,
                head.status,
                head.latest_event_id,
            )
        ) or (
            isinstance(latest, StaffSupportContinuityReceipt)
            and latest.status == "requeued"
            and (latest.request_id, latest.agent_id, latest.event_id)
            == (request_id, head.agent_id, head.latest_event_id)
            and head.status == "requested"
        )
        if not receipt_matches or not _event_payload_matches(record, latest):
            raise ValueError("truncated care request head disagrees with its applied event")


def record_shared_operation(world: WorldState, record: OperationHistoryRecord) -> None:
    """Append a receipt only after its matching state-changing event is applied."""
    state = world.model_state.get(SHARED_OPERATIONS_KEY)
    if not isinstance(state, dict):
        raise ValueError("shared_operations_v1 must be selected in the immutable Scenario")
    # As in per-tick validation, only records since the previous tick are parsed:
    # earlier ones passed that validation, and the run-boundary validation replays
    # the whole history.
    history = state.get("history")
    recent = (
        records_since_previous_tick(history, world.minute_index)
        if isinstance(history, list)
        else history
    )
    try:
        SharedOperationsState.model_validate({**state, "history": recent})
    except (TypeError, ValueError) as exc:
        raise ValueError("shared_operations_v1 state is invalid before operation append") from exc
    if record.minute != world.minute_index:
        raise ValueError("shared operation receipt must use the current simulation boundary minute")
    event_record = world.event_log.find(record.event_id)
    if (
        event_record is None
        or event_record.status is not EventStatus.APPLIED
        or event_record.realized_minute != record.minute
        or event_record.event.event_type != record.event_type
    ):
        raise ValueError("shared operation receipt requires a matching realized applied event")
    # A record carries its event's realized minute, here the current one, so an
    # earlier record of the same event is among the recent ones.
    if any(row.get("event_id") == record.event_id for row in recent):
        raise ValueError("shared operation event ID has already been recorded")
    common = record.model_dump(mode="json")
    if not _event_payload_matches(event_record, record):
        raise ValueError("shared operation receipt must exactly match the applied event payload")
    # Validate every dependency against the post-operation world before changing
    # the checkpoint projection. A rejected receipt must not partially mutate it.
    if isinstance(record, CareTransitionReceipt):
        prior = state["support_requests"].get(record.request_id)
        prior_status = prior.get("status") if prior else None
        if record.status not in _CARE_TRANSITIONS.get(prior_status, set()):
            raise ValueError("support lifecycle transition is out of order")
        if prior and (prior["agent_id"], prior.get("household_id"), prior["support_kind"]) != (
            record.agent_id,
            record.household_id,
            record.support_kind,
        ):
            raise ValueError("support request identity changed during its lifecycle")
        if (
            prior is None
            and record.status == "requested"
            and world.event_log.has_prior_care_request(record.request_id, record.event_id)
        ):
            raise ValueError("support request IDs cannot be reused across Run history")
        if prior is None and len(state["support_requests"]) >= 100_000:
            raise ValueError("shared_operations_v1 support request capacity exceeded")
    elif isinstance(record, StaffSupportContinuityReceipt):
        prior = state["support_requests"].get(record.request_id)
        request = world.support_requests.get(record.request_id)
        cause = world.event_log.find(record.causation_id)
        cause_matches = cause is not None and (
            (
                record.cause_type == "route_unreachable"
                and cause.status is EventStatus.SUPPRESSED
                and cause.event.event_type == "agent_travel_complete"
                and cause.event.payload.get("purpose") == "staff_support_travel"
                and cause.event.payload.get("request_id") == record.request_id
                and cause.event.payload.get("staff_id") == record.previous_staff_id
            )
            or (
                record.cause_type in {"staff_unavailable", "staff_shift_change"}
                and cause.status is EventStatus.APPLIED
                and cause.event.event_type == record.cause_type
                and cause.event.payload.get("staff_id") == record.previous_staff_id
            )
        )
        if (
            prior is None
            or prior["status"] not in {"assigned", "started"}
            or prior["agent_id"] != record.agent_id
            or request is None
            or request.agent_id != record.agent_id
            or request.assigned_staff_id is not None
            or request.task_id is not None
            or not cause_matches
            or record.task_id in world.active_tasks
        ):
            raise ValueError("support continuity receipt does not match the interrupted request")
        expected_status = "pending" if record.status == "requeued" else "unresolved"
        if request.status.value != expected_status:
            raise ValueError(
                "support continuity receipt status disagrees with the resident request"
            )
        prior_event = world.event_log.find(prior["latest_event_id"])
        if (
            prior_event is None
            or prior_event.event.payload.get("staff_id") != record.previous_staff_id
        ):
            raise ValueError(
                "support continuity receipt does not match its previous staff assignment"
            )
    elif isinstance(record, StaffHandoffReceipt):
        task = world.active_tasks.get(record.task_id)
        if record.status == "reassigned":
            if task is None or task.staff_id != record.replacement_staff_id:
                raise ValueError("staff handoff receipt does not match the active task assignment")
        elif record.status == "unfilled" and (task is None or task.staff_id is not None):
            raise ValueError("unfilled staff handoff must leave an active task unassigned")
        elif record.status == "rejected" and (
            task is None or task.staff_id != record.previous_staff_id
        ):
            raise ValueError("rejected staff handoff must preserve the previous active assignment")
    elif isinstance(record, FacilityRecoveryReceipt):
        facility = world.facilities.get(record.facility_id)
        if (
            facility is None
            or record.working_units > facility.capacity
            or facility.working_units != record.working_units
        ):
            raise ValueError("facility recovery receipt does not match the applied facility state")
    elif isinstance(record, StaffOperationReceipt):
        _validate_staff_operation_receipt(
            world, record, history_truncated=state.get("history_truncated", False)
        )
    _validate_observed_state_changes(world, record)
    if isinstance(record, CareTransitionReceipt):
        prior = state["support_requests"].get(record.request_id)
        if record.status in {"completed", "interrupted", "declined"}:
            state["support_requests"].pop(record.request_id, None)
        else:
            state["support_requests"][record.request_id] = CareRequestState(
                request_id=record.request_id,
                agent_id=record.agent_id,
                household_id=record.household_id,
                support_kind=record.support_kind,
                status=record.status,
                created_minute=prior["created_minute"] if prior else record.minute,
                updated_minute=record.minute,
                latest_event_id=record.event_id,
            ).model_dump(mode="json")
    elif isinstance(record, StaffSupportContinuityReceipt):
        prior = state["support_requests"][record.request_id]
        if record.status == "requeued":
            state["support_requests"][record.request_id] = CareRequestState(
                request_id=record.request_id,
                agent_id=record.agent_id,
                household_id=prior.get("household_id"),
                support_kind=prior["support_kind"],
                status="requested",
                created_minute=prior["created_minute"],
                updated_minute=record.minute,
                latest_event_id=record.event_id,
            ).model_dump(mode="json")
        else:
            state["support_requests"].pop(record.request_id, None)
    state["history"].append(common)
    if len(state["history"]) > MAX_SHARED_OPERATION_HISTORY:
        state["history_truncated"] = True
    del state["history"][:-MAX_SHARED_OPERATION_HISTORY]


def _event_payload_matches(event_record: Any, record: OperationHistoryRecord) -> bool:
    """Require the complete immutable event payload and causal/source headers to agree."""
    event = event_record.event
    payload = event.payload_dict()
    expected = record.model_dump(mode="json")
    if (
        isinstance(record, StaffSupportContinuityReceipt)
        and "cause_type" not in payload
        and record.cause_type == "staff_unavailable"
    ):
        # Existing v1 receipts predate the optional cause discriminator.
        expected.pop("cause_type")
    return (
        payload == expected
        and event.causation_id == record.causation_id
        and (event.source_id is None or event.source_id in record.source_ids)
    )


def _validate_observed_state_changes(world: WorldState, record: OperationHistoryRecord) -> None:
    if (
        not isinstance(record, (CareTransitionReceipt, StaffOperationReceipt))
        or not record.state_changes
    ):
        return
    collections = {
        "agent": world.agents,
        "staff": world.staff,
        "facility": world.facilities,
        "zone": world.zones,
        "resource": world.resources,
    }
    for change in record.state_changes:
        parts = change.field_path.split(".")
        if change.entity_kind == "model_state":
            if len(parts) < 3:
                raise ValueError("model_state change needs namespace, collection and field path")
            namespace, collection, *path = parts
            row = (
                world.model_state.get(namespace, {}).get(collection, {}).get(change.entity_id)
                if isinstance(world.model_state.get(namespace), dict)
                else None
            )
        elif change.entity_kind == "resource":
            if parts != ["value"]:
                raise ValueError("resource state changes use field_path='value'")
            row, path = collections["resource"].get(change.entity_id), []
        else:
            row = collections[change.entity_kind].get(change.entity_id)
            path = parts
        current = row
        for part in path:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
        if type(current) not in (int, float) or current != change.after:
            raise ValueError(
                "care receipt after-value does not match the current saved world state"
            )


def _validate_staff_operation_receipt(
    world: WorldState,
    record: StaffOperationReceipt,
    *,
    history_truncated: bool,
) -> None:
    operations = world.model_state.get("staff_operations_v1", {})
    history = operations.get("history", ()) if isinstance(operations, dict) else ()
    matches = [
        row
        for row in history
        if isinstance(row, dict)
        and row.get("task_id") == record.task_id
        and row.get("complete_minute") == record.minute
    ]
    if not matches:
        if (
            history_truncated
            and history
            and all(
                row.get("complete_minute", 0) > record.minute
                for row in history
                if isinstance(row, dict)
            )
        ):
            return
        raise ValueError("staff operation receipt has no matching retained completion")
    if len(matches) != 1:
        raise ValueError("staff operation receipt matches duplicate completion history")
    row = matches[0]
    if (
        row.get("staff_id") != record.staff_id
        or row.get("task_kind") != record.task_kind
        or row.get("target_id") != record.target_id
        or row.get("target_zone_id") != record.target_zone_id
        or row.get("reason") != record.reason
        or row.get("status") != record.status
        or row.get("start_minute") != record.start_minute
        or row.get("arrival_minute") != record.arrival_minute
        or row.get("travel_minutes") != record.travel_minutes
        or record.causation_id not in row.get("causation_event_ids", ())
    ):
        raise ValueError("staff operation receipt disagrees with retained completion history")


def operational_demand_snapshot(world: WorldState) -> dict[str, Any]:
    """Return observed occupancy/queue/staff/resource state and explicit non-estimates."""
    from sheltersim.contracts.spatial import occupancy_snapshot

    geometry_raw = world.model_configs.get("facility_geometry")
    geometry = parsed_facility_geometry(geometry_raw) if geometry_raw else None
    explicit_points = geometry.service_points if geometry is not None else ()
    independent_queue_state = world.model_state.get("facility_queues_v1", {})
    services = []
    for facility in sorted(world.facilities.values(), key=lambda row: row.id):
        points = [point for point in explicit_points if point.facility_id == facility.id]
        if not points and geometry is not None and facility.id in geometry.facility_points:
            from sheltersim.contracts.spatial import legacy_service_points

            points = list(legacy_service_points(geometry, [facility]))
        for point in points:
            queue_row = (
                independent_queue_state.get("queues", {}).get(point.queue_id)
                if isinstance(independent_queue_state, dict)
                else None
            )
            bound = point.queue_id == facility.id and queue_row is None
            services.append(
                {
                    "service_point_id": point.id,
                    "facility_id": facility.id,
                    "zone_id": facility.zone_id,
                    "point_m": point.point_m.model_dump(mode="json"),
                    "queue_id": point.queue_id,
                    "queue_length": (
                        len(queue_row["queued_request_ids"])
                        if isinstance(queue_row, dict)
                        else (
                            len(facility.queue_agent_ids) + len(facility.resource_queue_agent_ids)
                            if bound
                            else None
                        )
                    ),
                    "active_services": (
                        len(queue_row["active_request_ids"])
                        if isinstance(queue_row, dict)
                        else (
                            len(facility.active_agent_ids) + len(facility.resource_active_agent_ids)
                            if bound
                            else None
                        )
                    ),
                    "queued_request_ids": list(queue_row["queued_request_ids"])
                    if isinstance(queue_row, dict)
                    else None,
                    "active_request_ids": list(queue_row["active_request_ids"])
                    if isinstance(queue_row, dict)
                    else None,
                    "last_stall": queue_row["last_stall"] if isinstance(queue_row, dict) else None,
                    "service_capacity_concurrent": point.service_capacity,
                    "reservation_binding": "independent_queue_state"
                    if isinstance(queue_row, dict)
                    else ("legacy_facility_fifo" if bound else "not_connected"),
                    "accessibility": point.access.accessibility,
                    "required_access_tags": list(point.access.required_access_tags),
                    "required_staff_skills": list(point.access.required_staff_skills),
                }
            )
    state = world.model_state.get(SHARED_OPERATIONS_KEY, {})
    requests = state.get("support_requests", {}) if isinstance(state, dict) else {}
    request_counts: dict[str, int] = {}
    for request in requests.values():
        status = request.get("status", "unknown")
        request_counts[status] = request_counts.get(status, 0) + 1
    snapshot = OperationalDemandSnapshot(
        minute=world.minute_index,
        population_count=len(world.agents),
        pending_arrivals=len(
            world.model_state.get("population_dynamics_v1", {}).get("pending_arrivals", [])
        ),
        zones=tuple(occupancy_snapshot(world)["zones"]),
        services=tuple(services),
        staff={
            "count": len(world.staff),
            "assigned": sum(row.active_task_id is not None for row in world.staff.values()),
        },
        resources={key: value for key, value in world.resources.items() if type(value) is int},
        support_requests=request_counts,
    )
    return snapshot.model_dump(mode="json")
