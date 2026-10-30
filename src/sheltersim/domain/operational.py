"""Versioned runtime state for opt-in H03 operational events."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sheltersim.events.models import EventKind, EventStatus, ScheduledEvent
from sheltersim.events.queue import EventsById

if TYPE_CHECKING:
    from sheltersim.core.world import WorldState


OPERATIONAL_EVENTS_KEY = "operational_events_v1"
H03_EVENT_TYPES = frozenset(
    {
        "delivery_delayed",
        "water_outage",
        "water_restored",
        "staff_unavailable",
        "staff_shift_change",
    }
)
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# The only event types the full check reads; others pass it unread.
_CHECKED_EVENT_TYPES = frozenset(
    {
        "facility_outage",
        "facility_recovery",
        "resource_delivery",
        "resource_loss",
        "resource_reserve",
        "resource_release",
        "resource_consume",
        "staff_restored",
        *H03_EVENT_TYPES,
    }
)


def _is_stable_id(value: Any) -> bool:
    return isinstance(value, str) and _STABLE_ID.fullmatch(value) is not None


def uses_operational_events(events: Any) -> bool:
    """Return whether planned events opt into the H03 runtime contract."""
    return any(
        event.event_type in H03_EVENT_TYPES
        or (event.event_type == "resource_delivery" and "shipment_id" in event.payload)
        for event in events
    )


def operational_events_enabled(configs: dict[str, Any], events: Any) -> bool:
    """Return whether a Scenario keeps operational_events_v1 state.

    Planning an H03 event opts in, and so does model_configs.operational_events,
    which generated Scenarios set so that a water outage or staff absence can be
    approved before any delivery is planned (#65).
    """
    config = configs.get("operational_events")
    if config is not None and (
        not isinstance(config, dict)
        or set(config) != {"enabled"}
        or type(config["enabled"]) is not bool
    ):
        raise ValueError("operational_events config must be {'enabled': bool}")
    return (config is not None and config["enabled"]) or uses_operational_events(events)


def create_operational_events_state(events: Any, staff_ids: set[str]) -> dict[str, Any]:
    """Create canonical operational state from validated scenario events."""
    shipments = {
        event.payload["shipment_id"]: {
            "event_id": event.id,
            "resource_id": event.payload["resource_id"],
            "due_minute": event.due_minute,
        }
        for event in events
        if event.event_type == "resource_delivery" and "shipment_id" in event.payload
    }
    restore_ids = {
        event.payload["outage_id"]: event.id
        for event in events
        if event.event_type == "water_restored"
    }
    outages = {
        event.payload["outage_id"]: {
            "target_kind": "zone" if "zone_id" in event.payload else "facility",
            "target_id": event.payload.get("zone_id", event.payload.get("facility_id")),
            "outage_event_id": event.id,
            "restore_event_id": restore_ids.get(event.payload["outage_id"]),
            "active": False,
        }
        for event in events
        if event.event_type == "water_outage"
    }
    staff_status = {
        staff_id: {
            "available": True,
            "unavailability_event_id": None,
            "until_minute": None,
            "restore_event_id": None,
        }
        for staff_id in sorted(staff_ids)
    }
    return {
        "schema_version": 1,
        "shipments": shipments,
        "water_outages": outages,
        "staff_status": staff_status,
        "cancelled_staff_task_ids": [],
    }


def staff_is_available(world: WorldState, staff_id: str) -> bool:
    """Check H03 availability while preserving legacy behavior without H03 state."""
    state = world.model_state.get(OPERATIONAL_EVENTS_KEY)
    if state is None:
        return True
    record = state["staff_status"].get(staff_id)
    return record is None or record["available"]


def water_service_blocked(world: WorldState, facility_id: str) -> bool:
    """Return whether an active H03 water outage covers this station."""
    state = world.model_state.get(OPERATIONAL_EVENTS_KEY)
    if state is None:
        return False
    facility = world.facilities[facility_id]
    return any(
        outage["active"]
        and (
            (outage["target_kind"] == "zone" and outage["target_id"] == facility.zone_id)
            or (outage["target_kind"] == "facility" and outage["target_id"] == facility_id)
        )
        for outage in state["water_outages"].values()
    )


def validate_operational_events_state(
    world: WorldState,
    *,
    in_flight_event: ScheduledEvent | None = None,
    validate_event_history: bool = True,
) -> None:
    """Validate the exact H03 state shape and its event/staff references."""
    state = world.model_state.get(OPERATIONAL_EVENTS_KEY)
    if state is not None and (
        not isinstance(state, dict)
        or set(state)
        != {
            "schema_version",
            "shipments",
            "water_outages",
            "staff_status",
            "cancelled_staff_task_ids",
        }
        or state.get("schema_version") != 1
        or not isinstance(state.get("shipments"), dict)
        or not isinstance(state.get("water_outages"), dict)
        or not isinstance(state.get("staff_status"), dict)
        or not isinstance(state.get("cancelled_staff_task_ids"), list)
    ):
        raise ValueError("operational_events_v1 has an invalid exact schema")

    pending_events = world.event_queue.pending()
    if in_flight_event is not None and (
        in_flight_event.due_minute > world.minute_index or in_flight_event not in pending_events
    ):
        raise ValueError("in-flight operational event must be pending and due")
    if state is None and operational_events_enabled(world.model_configs, ()):
        raise ValueError("model_configs.operational_events requires operational_events_v1 state")
    if not validate_event_history:
        # Match the state creation and the full check: plain ledger events such as
        # resource_loss run without operational state.
        if state is None and uses_operational_events(pending_events):
            raise ValueError("operational events require operational_events_v1 state")
        return
    all_events = list(pending_events)
    all_events.extend(record.event for record in world.event_log.of_types(_CHECKED_EVENT_TYPES))
    by_id = EventsById(world.event_log, pending_events)

    def event_status(event_id: str | None) -> EventStatus | None:
        if event_id is None:
            return None
        if in_flight_event is not None and in_flight_event.event_id == event_id:
            return EventStatus.APPLIED
        outcome = world.event_log.find(event_id)
        return outcome.status if outcome is not None else None

    shipments: dict[str, tuple[str, str, int]] = {}
    outages: dict[str, tuple[str, str, str]] = {}
    restorations: dict[str, list[ScheduledEvent]] = {}
    staff_restorations: dict[str, list[ScheduledEvent]] = {}
    for event in all_events:
        payload = event.payload
        if event.event_type in {"facility_outage", "facility_recovery"}:
            amount_field = (
                "units_out" if event.event_type == "facility_outage" else "units_restored"
            )
            if set(payload) != {"facility_id", amount_field}:
                raise ValueError(f"{event.event_type} payload fields do not match")
            if payload.get("facility_id") not in world.facilities:
                raise ValueError(f"{event.event_type} references an unknown facility")
            amount = payload.get(amount_field)
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError(f"{event.event_type} amount is invalid")
        elif event.event_type in {
            "resource_delivery",
            "resource_loss",
            "resource_reserve",
            "resource_release",
            "resource_consume",
        }:
            required_fields = {
                "resource_delivery": {"warehouse_id", "resource_id", "quantity"},
                "resource_loss": {"warehouse_id", "resource_id", "quantity"},
                "resource_reserve": {
                    "warehouse_id",
                    "resource_id",
                    "reservation_id",
                    "agent_id",
                    "quantity",
                },
                "resource_release": {"warehouse_id", "resource_id", "reservation_id", "agent_id"},
                "resource_consume": {
                    "warehouse_id",
                    "resource_id",
                    "reservation_id",
                    "agent_id",
                    "quantity",
                },
            }[event.event_type]
            actual_fields = set(payload)
            if actual_fields != required_fields and not (
                event.event_type == "resource_delivery"
                and actual_fields == required_fields | {"shipment_id"}
            ):
                raise ValueError(f"{event.event_type} payload fields do not match")
        elif event.event_type in H03_EVENT_TYPES:
            expected_fields = {
                "delivery_delayed": {"shipment_id", "to_minute"},
                "water_restored": {"outage_id"},
                "staff_unavailable": {"staff_id", "until_minute"},
                "staff_shift_change": {"staff_id", "until_minute"},
            }.get(event.event_type)
            if event.event_type == "water_outage":
                valid_fields = set(payload) in (
                    {"outage_id", "zone_id"},
                    {"outage_id", "facility_id"},
                )
            else:
                valid_fields = set(payload) == expected_fields
            infrastructure_delay = (
                event.event_type == "delivery_delayed"
                and event.kind is EventKind.COMPLETION
                and event.causation_id is not None
                and event.provenance.get("policy_version") == "infrastructure-events-v1"
                and event.provenance.get("source_id")
                == world.model_configs.get("infrastructure", {}).get("source_id")
            )
            if (
                not valid_fields
                or event.source_id is None
                or (
                    event.kind.value not in {"exogenous", "intervention"}
                    and not infrastructure_delay
                )
            ):
                raise ValueError(f"{event.event_type} event shape or provenance is invalid")
            if event.event_type == "delivery_delayed":
                if not _is_stable_id(payload.get("shipment_id")):
                    raise ValueError("delivery delay shipment_id is invalid")
                target_minute = payload.get("to_minute")
                if not isinstance(target_minute, int) or isinstance(target_minute, bool):
                    raise ValueError("delivery delay to_minute is invalid")
            elif event.event_type == "water_outage":
                target = payload.get("zone_id", payload.get("facility_id"))
                if not _is_stable_id(payload.get("outage_id")):
                    raise ValueError("water outage ID is invalid")
                if "zone_id" in payload and target not in world.zones:
                    raise ValueError("water outage references an unknown zone")
                if "facility_id" in payload and (
                    target not in world.facilities
                    or world.facilities[target].kind != "water_station"
                ):
                    raise ValueError("water outage must target a water station")
            elif event.event_type == "water_restored":
                if not _is_stable_id(payload.get("outage_id")):
                    raise ValueError("water restoration outage_id is invalid")
            else:
                staff_id = payload.get("staff_id")
                until_minute = payload.get("until_minute")
                if (
                    not _is_stable_id(staff_id)
                    or staff_id not in world.staff
                    or not isinstance(until_minute, int)
                    or isinstance(until_minute, bool)
                    or until_minute <= event.due_minute
                    or until_minute > world.duration_minutes
                ):
                    raise ValueError("staff unavailability target or time is invalid")
        elif event.event_type == "staff_restored":
            if (
                set(payload) != {"staff_id", "unavailability_event_id"}
                or event.kind is not EventKind.COMPLETION
                or event.source_id is None
                or not _is_stable_id(payload.get("staff_id"))
                or payload.get("staff_id") not in world.staff
                or not _is_stable_id(payload.get("unavailability_event_id"))
            ):
                raise ValueError("staff restoration event shape is invalid")
            unavailable = by_id.get(payload["unavailability_event_id"])
            if (
                unavailable is None
                or unavailable.event_type not in {"staff_unavailable", "staff_shift_change"}
                or unavailable.payload.get("staff_id") != payload["staff_id"]
                or unavailable.payload.get("until_minute") != event.due_minute
                or event.causation_id != unavailable.event_id
                or event.source_id != unavailable.source_id
            ):
                raise ValueError("staff restoration event does not match its cause")
            staff_restorations.setdefault(payload["unavailability_event_id"], []).append(event)
        if event.event_type == "resource_delivery" and "shipment_id" in event.payload:
            shipment_id = event.payload["shipment_id"]
            if (
                not _is_stable_id(shipment_id)
                or event.source_id is None
                or event.kind not in (EventKind.EXOGENOUS, EventKind.INTERVENTION)
            ):
                raise ValueError("shipment ID is invalid")
            if shipment_id in shipments:
                raise ValueError("shipment event IDs must be unique")
            shipments[shipment_id] = (
                event.event_id,
                event.payload.get("resource_id"),
                event.due_minute,
            )
        elif event.event_type == "water_outage":
            outage_id = event.payload.get("outage_id")
            if outage_id in outages:
                raise ValueError("water outage IDs must be unique")
            target_kind = "zone" if "zone_id" in event.payload else "facility"
            target_id = event.payload.get("zone_id", event.payload.get("facility_id"))
            if target_kind == "zone":
                if target_id not in world.zones or not any(
                    facility.kind == "water_station" and facility.zone_id == target_id
                    for facility in world.facilities.values()
                ):
                    raise ValueError("water outage zone must contain a water_station")
            else:
                facility = world.facilities.get(target_id)
                if facility is None or facility.kind != "water_station":
                    raise ValueError("water outage target must be a water_station")
            outages[outage_id] = (target_kind, target_id, event.event_id)
        elif event.event_type == "water_restored":
            restorations.setdefault(event.payload.get("outage_id"), []).append(event)
    if state is None:
        if uses_operational_events(all_events):
            raise ValueError("H03 events require operational_events_v1 state")
        return
    if set(state["shipments"]) != set(shipments):
        raise ValueError("operational shipment state does not match shipment events")
    for shipment_id, (event_id, resource_id, due_minute) in shipments.items():
        record = state["shipments"][shipment_id]
        if (
            not isinstance(record, dict)
            or set(record) != {"event_id", "resource_id", "due_minute"}
            or record
            != {"event_id": event_id, "resource_id": resource_id, "due_minute": due_minute}
        ):
            raise ValueError("operational shipment record does not match its event")
    if set(state["water_outages"]) != set(outages):
        raise ValueError("operational water outage state does not match outage events")
    if set(restorations) - set(outages) or any(len(events) > 1 for events in restorations.values()):
        raise ValueError("water restoration events must uniquely reference known outages")
    for outage_id, (target_kind, target_id, event_id) in outages.items():
        record = state["water_outages"][outage_id]
        expected_keys = {
            "target_kind",
            "target_id",
            "outage_event_id",
            "restore_event_id",
            "active",
        }
        if (
            not isinstance(record, dict)
            or set(record) != expected_keys
            or record["target_kind"] != target_kind
            or record["target_id"] != target_id
            or record["outage_event_id"] != event_id
            or not isinstance(record["active"], bool)
        ):
            raise ValueError("operational water outage record is invalid")
        restore_events = restorations.get(outage_id, [])
        expected_restore_id = restore_events[0].event_id if restore_events else None
        if record["restore_event_id"] != expected_restore_id:
            raise ValueError("operational water restoration state does not match its event")
        restore_id = record["restore_event_id"]
        if restore_id is not None:
            restore = by_id.get(restore_id)
            outage_event = by_id.get(event_id)
            if (
                restore is None
                or restore.event_type != "water_restored"
                or restore.payload.get("outage_id") != outage_id
                or restore.causation_id != event_id
                or outage_event is None
                or restore.due_minute <= outage_event.due_minute
            ):
                raise ValueError("operational water restoration reference is invalid")
        expected_active = event_status(event_id) is EventStatus.APPLIED and (
            event_status(restore_id) is not EventStatus.APPLIED
        )
        if record["active"] != expected_active:
            raise ValueError(
                "operational water outage activity does not match applied event history"
            )
    if set(state["staff_status"]) != set(world.staff):
        raise ValueError("operational staff status must cover the known staff IDs exactly")
    latest_applied_absence: dict[str, ScheduledEvent] = {}
    for outcome in world.event_log.of_types({"staff_unavailable", "staff_shift_change"}):
        if outcome.status is EventStatus.APPLIED:
            latest_applied_absence[outcome.event.payload["staff_id"]] = outcome.event
    if in_flight_event is not None and in_flight_event.event_type in {
        "staff_unavailable",
        "staff_shift_change",
    }:
        latest_applied_absence[in_flight_event.payload["staff_id"]] = in_flight_event
    for staff_id, record in state["staff_status"].items():
        if (
            not isinstance(record, dict)
            or set(record)
            != {"available", "unavailability_event_id", "until_minute", "restore_event_id"}
            or not isinstance(record["available"], bool)
        ):
            raise ValueError("operational staff status record is invalid")
        unavailable_id = record["unavailability_event_id"]
        if unavailable_id is None:
            if record["until_minute"] is not None or record["restore_event_id"] is not None:
                raise ValueError("available staff cannot retain unavailability event metadata")
        else:
            unavailable = by_id.get(unavailable_id)
            restore_id = record["restore_event_id"]
            restore = by_id.get(restore_id) if restore_id is not None else None
            if (
                unavailable is None
                or unavailable.event_type not in {"staff_unavailable", "staff_shift_change"}
                or unavailable.payload.get("staff_id") != staff_id
                or unavailable.payload.get("until_minute") != record["until_minute"]
                or unavailable.source_id is None
                or not isinstance(record["until_minute"], int)
                or isinstance(record["until_minute"], bool)
                or (record["until_minute"] < world.duration_minutes and restore is None)
                or (record["until_minute"] == world.duration_minutes and restore_id is not None)
                or (
                    restore is not None
                    and (
                        restore.event_type != "staff_restored"
                        or restore.payload.get("staff_id") != staff_id
                        or restore.payload.get("unavailability_event_id") != unavailable_id
                        or restore.causation_id != unavailable_id
                        or restore.source_id != unavailable.source_id
                    )
                )
            ):
                raise ValueError("operational staff restoration reference is invalid")
        latest_absence = latest_applied_absence.get(staff_id)
        expected_unavailability_id = latest_absence.event_id if latest_absence else None
        expected_until = (
            latest_absence.payload["until_minute"] if latest_absence is not None else None
        )
        matching_restorations = (
            staff_restorations.get(expected_unavailability_id, [])
            if expected_unavailability_id is not None
            else []
        )
        if len(matching_restorations) > 1:
            raise ValueError("staff unavailability cannot have duplicate restorations")
        expected_restore_id = matching_restorations[0].event_id if matching_restorations else None
        expected_available = (
            event_status(expected_restore_id) is EventStatus.APPLIED
            if expected_restore_id is not None
            else expected_unavailability_id is None
        )
        if (
            unavailable_id != expected_unavailability_id
            or record["until_minute"] != expected_until
            or record["restore_event_id"] != expected_restore_id
            or record["available"] != expected_available
        ):
            raise ValueError("operational staff availability does not match applied event history")
        if not record["available"]:
            staff_member = world.staff[staff_id]
            if (
                staff_member.active_task_id is not None
                or staff_member.work_queue
                or staff_member.busy_until != 0
            ):
                raise ValueError("unavailable staff cannot retain an active task or busy state")
    cancelled = state["cancelled_staff_task_ids"]
    if any(not isinstance(value, str) or not value for value in cancelled) or len(cancelled) != len(
        set(cancelled)
    ):
        raise ValueError("cancelled staff task IDs must be unique non-empty strings")
