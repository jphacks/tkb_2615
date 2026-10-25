"""Versioned, deterministic inventory accounting stored in WorldState model state."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sheltersim.core.history import records_since_previous_tick

if TYPE_CHECKING:
    from sheltersim.events.models import ScheduledEvent

RESOURCE_LEDGER_KEY = "resource_ledger_v1"
RESOURCE_LEDGER_SCHEMA_VERSION = 1
RESOURCE_AUDIT_SCHEMA_VERSION = 1
RESOURCE_IDS = ("water_ml", "food_meal_units")
RESOURCE_METRIC_FIELDS = tuple(
    f"{resource_id}_{name}"
    for resource_id in RESOURCE_IDS
    for name in ("initial", "delivered", "served", "losses", "remaining", "reserved")
)
_LEDGER_FIELDS = {
    "schema_version",
    "warehouse_id",
    "zone_id",
    "accounts",
    "used_reservation_ids",
    "audit",
}
_ACCOUNT_FIELDS = {
    "initial",
    "delivered",
    "served",
    "losses",
    "remaining",
    "reserved",
    "source_ids",
    "reservations",
}
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_BALANCE_FIELDS = ("initial", "delivered", "served", "losses", "remaining", "reserved")
_AUDIT_FIELDS = {
    "schema_version",
    "minute",
    "event_id",
    "operation",
    "resource_id",
    "quantity",
    "reservation_id",
    "agent_id",
    "source_id",
    "source_ids",
    "warehouse_id",
    *_BALANCE_FIELDS,
}


class ResourceLedgerError(ValueError):
    """Raised when an inventory operation or serialized ledger is invalid."""


def create_resource_ledger(initial_stock: dict[str, Any]) -> dict[str, Any]:
    """Create the v1 audit ledger for an opted-in scenario's initial inventory."""
    accounts: dict[str, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []
    for resource_id in RESOURCE_IDS:
        initial = initial_stock[resource_id]
        source_ids = list(initial_stock["source_ids"])
        accounts[resource_id] = {
            "initial": initial,
            "delivered": 0,
            "served": 0,
            "losses": 0,
            "remaining": initial,
            "reserved": 0,
            "source_ids": source_ids,
            "reservations": {},
        }
        audit.append(
            {
                "schema_version": RESOURCE_AUDIT_SCHEMA_VERSION,
                "minute": 0,
                "event_id": None,
                "operation": "initial",
                "resource_id": resource_id,
                "quantity": initial,
                "reservation_id": None,
                "agent_id": None,
                "source_id": None,
                "source_ids": source_ids.copy(),
                "warehouse_id": initial_stock["warehouse_id"],
                "initial": initial,
                "delivered": 0,
                "served": 0,
                "losses": 0,
                "remaining": initial,
                "reserved": 0,
            }
        )
    return {
        "schema_version": RESOURCE_LEDGER_SCHEMA_VERSION,
        "warehouse_id": initial_stock["warehouse_id"],
        "zone_id": initial_stock["zone_id"],
        "accounts": accounts,
        "used_reservation_ids": [],
        "audit": audit,
    }


def validate_resource_ledger(
    ledger: Any,
    resources: Any,
    agent_ids: set[str] | None = None,
    zone_ids: set[str] | None = None,
) -> None:
    """Replay the exact audit schema and compare it with current balances."""
    used_ids = _validate_ledger_balances(ledger, resources, agent_ids, zone_ids)
    accounts = ledger["accounts"]
    audit = ledger.get("audit")
    if not isinstance(audit, list):
        raise ResourceLedgerError("resource ledger audit must be a list")
    if len(audit) < len(RESOURCE_IDS):
        raise ResourceLedgerError("resource ledger audit is missing initial records")
    replay_accounts: dict[str, dict[str, Any]] = {}
    used_history: list[str] = []
    used_history_set: set[str] = set()
    event_ids: set[str] = set()
    last_audit_minute = 0
    for index, record in enumerate(audit):
        _validate_audit_record(record, ledger)
        if record["minute"] < last_audit_minute:
            raise ResourceLedgerError("resource audit minutes must be non-decreasing")
        last_audit_minute = record["minute"]

        if record["operation"] == "initial":
            if index >= len(RESOURCE_IDS) or record["resource_id"] != RESOURCE_IDS[index]:
                raise ResourceLedgerError(
                    "initial resource audit records must lead in stable order"
                )
            if any(
                record[name] is not None
                for name in ("event_id", "reservation_id", "agent_id", "source_id")
            ):
                raise ResourceLedgerError(
                    "initial resource audit cannot reference an event or Agent"
                )
            if record["minute"] != 0:
                raise ResourceLedgerError("initial resource audit values are invalid")
            if record["quantity"] != record["initial"]:
                raise ResourceLedgerError("initial resource audit quantity does not match balance")
            if record["source_ids"] != sorted(set(record["source_ids"])):
                raise ResourceLedgerError(
                    "initial resource audit source_ids must be sorted and unique"
                )
            replay = {
                "initial": record["quantity"],
                "delivered": 0,
                "served": 0,
                "losses": 0,
                "remaining": record["quantity"],
                "reserved": 0,
                "source_ids": record["source_ids"].copy(),
                "reservations": {},
            }
            replay_accounts[record["resource_id"]] = replay
        else:
            if index < len(RESOURCE_IDS):
                raise ResourceLedgerError(
                    "initial resource audit records must lead in stable order"
                )
            if record["quantity"] == 0:
                raise ResourceLedgerError("resource event audit quantity must be positive")
            if record["event_id"] is None or record["source_id"] is None:
                raise ResourceLedgerError("resource event audit requires event_id and source_id")
            if record["event_id"] in event_ids:
                raise ResourceLedgerError("resource audit event IDs must be unique")
            event_ids.add(record["event_id"])
            has_reservation = record["operation"] in {"reserve", "release", "consume"}
            if has_reservation != (record["reservation_id"] is not None) or has_reservation != (
                record["agent_id"] is not None
            ):
                raise ResourceLedgerError("resource audit reservation identity is incomplete")
            if record["source_id"] not in record["source_ids"]:
                raise ResourceLedgerError("resource audit source is missing from source_ids")
            replay = replay_accounts.get(record["resource_id"])
            if replay is None:
                raise ResourceLedgerError("resource operation precedes its initial audit record")
            operation = record["operation"]
            quantity = record["quantity"]
            reservation_id = record["reservation_id"]
            agent_id = record["agent_id"]
            if operation == "delivery":
                replay["delivered"] += quantity
                replay["remaining"] += quantity
            elif operation == "loss":
                if quantity > replay["remaining"] - replay["reserved"]:
                    raise ResourceLedgerError("resource audit loss exceeds available inventory")
                replay["losses"] += quantity
                replay["remaining"] -= quantity
            elif operation == "reserve":
                if reservation_id in used_history_set:
                    raise ResourceLedgerError("resource audit reservation ID was reused")
                if quantity > replay["remaining"] - replay["reserved"]:
                    raise ResourceLedgerError("resource audit reservation exceeds available stock")
                if not _valid_id(agent_id) or (agent_ids is not None and agent_id not in agent_ids):
                    raise ResourceLedgerError("resource audit references an unknown Agent")
                used_history.append(reservation_id)
                used_history_set.add(reservation_id)
                replay["reservations"][reservation_id] = {
                    "reservation_id": reservation_id,
                    "resource_id": record["resource_id"],
                    "agent_id": agent_id,
                    "quantity": quantity,
                }
                replay["reserved"] += quantity
            elif operation in {"release", "consume"}:
                reservation = replay["reservations"].get(reservation_id)
                if reservation is None or reservation["agent_id"] != agent_id:
                    raise ResourceLedgerError("resource audit reservation history is invalid")
                if not _valid_id(agent_id) or (agent_ids is not None and agent_id not in agent_ids):
                    raise ResourceLedgerError("resource audit references an unknown Agent")
                reserved = reservation["quantity"]
                if operation == "release":
                    if quantity != reserved:
                        raise ResourceLedgerError("resource audit release quantity is invalid")
                    replay["reserved"] -= reserved
                    del replay["reservations"][reservation_id]
                else:
                    if quantity > reserved:
                        raise ResourceLedgerError("resource audit consume exceeds its reservation")
                    replay["served"] += quantity
                    replay["remaining"] -= quantity
                    replay["reserved"] -= quantity
                    if quantity == reserved:
                        del replay["reservations"][reservation_id]
                    else:
                        reservation["quantity"] = reserved - quantity

            if record["source_id"] not in replay["source_ids"]:
                replay["source_ids"].append(record["source_id"])
            if record["source_ids"] != replay["source_ids"]:
                raise ResourceLedgerError("resource audit source_ids do not match replay")

        replay = replay_accounts[record["resource_id"]]
        if any(record[name] != replay[name] for name in _BALANCE_FIELDS):
            raise ResourceLedgerError("resource audit after-state does not match replay")

    if list(used_ids) != used_history:
        raise ResourceLedgerError("resource reservation history does not match audit replay")
    for resource_id in RESOURCE_IDS:
        account = accounts[resource_id]
        replay = replay_accounts.get(resource_id)
        if replay is None:
            raise ResourceLedgerError(f"resource {resource_id!r} initial audit is missing")
        for name in _BALANCE_FIELDS:
            if account[name] != replay[name]:
                raise ResourceLedgerError(f"resource {resource_id!r} balance does not match audit")
        if account["source_ids"] != replay["source_ids"]:
            raise ResourceLedgerError(f"resource {resource_id!r} source IDs do not match audit")
        if account["reservations"] != replay["reservations"]:
            raise ResourceLedgerError(f"resource {resource_id!r} reservations do not match audit")


def validate_resource_ledger_state(
    ledger: Any,
    resources: Any,
    agent_ids: set[str] | None = None,
    zone_ids: set[str] | None = None,
    *,
    minute_index: int,
) -> None:
    """Check current balances, active reservations and the newest audit records.

    Per-tick, per-operation and metric checks use this. Audit records from earlier
    ticks were checked when they were appended; ``validate_resource_ledger`` replays
    the whole audit trail at Run boundaries and Checkpoint restore.
    """
    _validate_ledger_balances(
        ledger, resources, agent_ids, zone_ids, check_every_reservation_id=False
    )
    audit = ledger.get("audit")
    if not isinstance(audit, list):
        raise ResourceLedgerError("resource ledger audit must be a list")
    if len(audit) < len(RESOURCE_IDS):
        raise ResourceLedgerError("resource ledger audit is missing initial records")
    previous_minute: int | None = None
    for record in records_since_previous_tick(audit, minute_index):
        _validate_audit_record(record, ledger)
        if previous_minute is not None and record["minute"] < previous_minute:
            raise ResourceLedgerError("resource audit minutes must be non-decreasing")
        previous_minute = record["minute"]
    for resource_id in RESOURCE_IDS:
        account = ledger["accounts"][resource_id]
        newest = next(
            (
                record
                for record in reversed(audit)
                if isinstance(record, dict) and record.get("resource_id") == resource_id
            ),
            None,
        )
        if newest is None:
            raise ResourceLedgerError(f"resource {resource_id!r} initial audit is missing")
        if any(newest.get(name) != account[name] for name in _BALANCE_FIELDS):
            raise ResourceLedgerError(f"resource {resource_id!r} balance does not match audit")
        if newest.get("source_ids") != account["source_ids"]:
            raise ResourceLedgerError(f"resource {resource_id!r} source IDs do not match audit")


def _validate_ledger_balances(
    ledger: Any,
    resources: Any,
    agent_ids: set[str] | None,
    zone_ids: set[str] | None,
    *,
    check_every_reservation_id: bool = True,
) -> list[str]:
    """Check the ledger header, balances and active reservations; return used IDs."""
    if not isinstance(ledger, dict) or set(ledger) != _LEDGER_FIELDS:
        raise ResourceLedgerError("resource ledger fields do not match v1")
    version = ledger.get("schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != RESOURCE_LEDGER_SCHEMA_VERSION
    ):
        raise ResourceLedgerError("unsupported resource ledger schema version")
    if not _valid_id(ledger.get("warehouse_id")):
        raise ResourceLedgerError("resource ledger warehouse_id is invalid")
    zone_id = ledger.get("zone_id")
    if zone_id is not None and (
        not _valid_id(zone_id) or (zone_ids is not None and zone_id not in zone_ids)
    ):
        raise ResourceLedgerError("resource ledger zone_id is invalid or unknown")
    accounts = ledger.get("accounts")
    if not isinstance(accounts, dict) or set(accounts) != set(RESOURCE_IDS):
        raise ResourceLedgerError("resource ledger must contain water_ml and food_meal_units")
    if not isinstance(resources, dict):
        raise ResourceLedgerError("WorldState resources must be an object")
    if set(resources) != set(RESOURCE_IDS):
        raise ResourceLedgerError(
            "WorldState resources must contain exactly the ledger resource IDs"
        )

    used_ids = ledger.get("used_reservation_ids")
    if not isinstance(used_ids, list) or any(
        not _valid_id(value) if check_every_reservation_id else not isinstance(value, str)
        for value in used_ids
    ):
        raise ResourceLedgerError("resource ledger reservation IDs are invalid")
    if len(used_ids) != len(set(used_ids)):
        raise ResourceLedgerError("resource ledger reservation IDs must be unique")
    used_id_set = set(used_ids)

    for resource_id in RESOURCE_IDS:
        account = accounts[resource_id]
        if not isinstance(account, dict) or set(account) != _ACCOUNT_FIELDS:
            raise ResourceLedgerError(f"resource account {resource_id!r} fields do not match v1")
        quantities: dict[str, int] = {}
        for name in _BALANCE_FIELDS:
            value = account[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ResourceLedgerError(f"resource {resource_id!r} {name} must be non-negative")
            quantities[name] = value
        expected = quantities["initial"] + quantities["delivered"]
        expected -= quantities["served"] + quantities["losses"]
        if expected != quantities["remaining"]:
            raise ResourceLedgerError(f"resource {resource_id!r} accounting does not balance")
        resource_value = resources.get(resource_id)
        if (
            not isinstance(resource_value, int)
            or isinstance(resource_value, bool)
            or resource_value != quantities["remaining"]
        ):
            raise ResourceLedgerError(
                f"WorldState resource {resource_id!r} does not match ledger remaining"
            )

        reservations = account["reservations"]
        if not isinstance(reservations, dict):
            raise ResourceLedgerError(f"resource {resource_id!r} reservations must be an object")
        reservation_total = 0
        for reservation_id, record in reservations.items():
            if not _valid_id(reservation_id):
                raise ResourceLedgerError("resource reservation ID is invalid")
            if reservation_id not in used_id_set:
                raise ResourceLedgerError("active reservation ID is missing from its history")
            if not isinstance(record, dict) or set(record) != {
                "reservation_id",
                "resource_id",
                "agent_id",
                "quantity",
            }:
                raise ResourceLedgerError("resource reservation record fields do not match v1")
            if (
                record["reservation_id"] != reservation_id
                or record["resource_id"] != resource_id
                or not _valid_id(record["agent_id"])
                or (agent_ids is not None and record["agent_id"] not in agent_ids)
            ):
                raise ResourceLedgerError("resource reservation identity is invalid")
            quantity = record["quantity"]
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                raise ResourceLedgerError("active reservation quantity must be a positive integer")
            reservation_total += quantity
        if reservation_total != quantities["reserved"]:
            raise ResourceLedgerError(f"resource {resource_id!r} reserved total does not match")
        if quantities["reserved"] > quantities["remaining"]:
            raise ResourceLedgerError(f"resource {resource_id!r} reserves more than remains")

        source_ids = account["source_ids"]
        if not isinstance(source_ids, list) or any(not _valid_id(value) for value in source_ids):
            raise ResourceLedgerError(f"resource {resource_id!r} source_ids are invalid")
        if len(source_ids) != len(set(source_ids)):
            raise ResourceLedgerError(f"resource {resource_id!r} source_ids must be unique")
    return used_ids


def _validate_audit_record(record: Any, ledger: dict[str, Any]) -> None:
    """Check one audit record's exact fields and value ranges."""
    if not isinstance(record, dict) or set(record) != _AUDIT_FIELDS:
        raise ResourceLedgerError("resource ledger audit record fields do not match v1")
    audit_version = record["schema_version"]
    if (
        not isinstance(audit_version, int)
        or isinstance(audit_version, bool)
        or audit_version != RESOURCE_AUDIT_SCHEMA_VERSION
    ):
        raise ResourceLedgerError("unsupported resource audit schema version")
    if (
        not isinstance(record["minute"], int)
        or isinstance(record["minute"], bool)
        or record["minute"] < 0
        or record["resource_id"] not in RESOURCE_IDS
        or record["operation"]
        not in {
            "initial",
            "delivery",
            "loss",
            "reserve",
            "release",
            "consume",
        }
        or not isinstance(record["quantity"], int)
        or isinstance(record["quantity"], bool)
        or record["quantity"] < 0
    ):
        raise ResourceLedgerError("resource ledger audit values are invalid")
    if any(
        record[name] is not None and not _valid_id(record[name])
        for name in ("event_id", "reservation_id", "agent_id", "source_id")
    ):
        raise ResourceLedgerError("resource ledger audit IDs are invalid")
    if not isinstance(record["source_ids"], list) or any(
        not _valid_id(source_id) for source_id in record["source_ids"]
    ):
        raise ResourceLedgerError("resource ledger audit source_ids are invalid")
    if len(record["source_ids"]) != len(set(record["source_ids"])):
        raise ResourceLedgerError("resource ledger audit source_ids must be unique")
    if record["warehouse_id"] != ledger["warehouse_id"]:
        raise ResourceLedgerError("resource audit warehouse_id does not match its ledger")
    for name in _BALANCE_FIELDS:
        value = record[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ResourceLedgerError(f"resource audit {name} must be non-negative")


def apply_resource_operation(world: Any, event: ScheduledEvent, operation: str) -> None:
    """Apply one inventory event to detached candidates, then commit atomically."""
    if RESOURCE_LEDGER_KEY not in world.model_state:
        raise ResourceLedgerError("resource ledger is not enabled for this scenario")
    current = world.model_state[RESOURCE_LEDGER_KEY]
    validate_resource_ledger_state(
        current, world.resources, set(world.agents), set(world.zones),
        minute_index=world.minute_index,
    )
    # Only the accounts change in place; the ID and audit lists are only appended to,
    # so copying the lists (not their records) keeps the candidate detached.
    ledger = {
        **current,
        "accounts": deepcopy(current["accounts"]),
        "used_reservation_ids": list(current["used_reservation_ids"]),
        "audit": list(current["audit"]),
    }
    resources = dict(world.resources)

    required = {
        "delivery": {"warehouse_id", "resource_id", "quantity"},
        "loss": {"warehouse_id", "resource_id", "quantity"},
        "reserve": {"warehouse_id", "resource_id", "reservation_id", "agent_id", "quantity"},
        "release": {"warehouse_id", "resource_id", "reservation_id", "agent_id"},
        "consume": {"warehouse_id", "resource_id", "reservation_id", "agent_id", "quantity"},
    }.get(operation)
    if required is None:
        raise ResourceLedgerError(f"unknown resource operation {operation!r}")
    payload = dict(event.payload)
    if set(payload) != required:
        raise ResourceLedgerError(f"resource {operation} payload fields do not match")
    warehouse_id = payload.get("warehouse_id")
    if not _valid_id(warehouse_id) or warehouse_id != ledger["warehouse_id"]:
        raise ResourceLedgerError(f"unknown resource warehouse ID {warehouse_id!r}")
    resource_id = payload.get("resource_id")
    if not isinstance(resource_id, str) or resource_id not in RESOURCE_IDS:
        raise ResourceLedgerError(f"unknown resource ID {resource_id!r}")
    source_id = event.source_id
    if source_id is None:
        raise ResourceLedgerError("resource events require a source_id")
    account = ledger["accounts"][resource_id]
    quantity = payload.get("quantity")
    if operation in ("delivery", "loss", "reserve", "consume"):
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ResourceLedgerError("resource quantity must be a positive integer")

    reservation_id = payload.get("reservation_id")
    agent_id = payload.get("agent_id")
    if operation in ("reserve", "release", "consume"):
        if not _valid_id(agent_id) or agent_id not in world.agents:
            raise ResourceLedgerError(f"unknown resource Agent ID {agent_id!r}")
    if operation == "delivery":
        account["delivered"] += quantity
        account["remaining"] += quantity
    elif operation == "loss":
        if quantity > account["remaining"] - account["reserved"]:
            raise ResourceLedgerError("resource loss exceeds unreserved remaining inventory")
        account["losses"] += quantity
        account["remaining"] -= quantity
    elif operation == "reserve":
        if not _valid_id(reservation_id):
            raise ResourceLedgerError("resource reservation_id must be a stable ID")
        if reservation_id in ledger["used_reservation_ids"]:
            raise ResourceLedgerError(
                f"resource reservation ID {reservation_id!r} was already used"
            )
        if quantity > account["remaining"] - account["reserved"]:
            raise ResourceLedgerError("resource reservation exceeds available inventory")
        account["reservations"][reservation_id] = {
            "reservation_id": reservation_id,
            "resource_id": resource_id,
            "agent_id": agent_id,
            "quantity": quantity,
        }
        account["reserved"] += quantity
        ledger["used_reservation_ids"].append(reservation_id)
    elif operation == "release":
        if not _valid_id(reservation_id):
            raise ResourceLedgerError("resource reservation_id must be a stable ID")
        record = account["reservations"].get(reservation_id)
        if record is None:
            raise ResourceLedgerError(f"unknown resource reservation {reservation_id!r}")
        if record["agent_id"] != agent_id:
            raise ResourceLedgerError("resource reservation belongs to a different Agent")
        quantity = record["quantity"]
        account["reserved"] -= quantity
        del account["reservations"][reservation_id]
    else:
        if not _valid_id(reservation_id):
            raise ResourceLedgerError("resource reservation_id must be a stable ID")
        record = account["reservations"].get(reservation_id)
        if record is None:
            raise ResourceLedgerError(f"unknown resource reservation {reservation_id!r}")
        if record["agent_id"] != agent_id:
            raise ResourceLedgerError("resource reservation belongs to a different Agent")
        reserved = record["quantity"]
        if quantity > reserved:
            raise ResourceLedgerError("resource consume exceeds the reservation")
        account["served"] += quantity
        account["remaining"] -= quantity
        account["reserved"] -= quantity
        if quantity == reserved:
            del account["reservations"][reservation_id]
        else:
            record["quantity"] = reserved - quantity

    if source_id not in account["source_ids"]:
        account["source_ids"].append(source_id)
    resources[resource_id] = account["remaining"]
    ledger["audit"].append(
        {
            "schema_version": RESOURCE_AUDIT_SCHEMA_VERSION,
            "minute": world.minute_index,
            "event_id": event.event_id,
            "operation": operation,
            "resource_id": resource_id,
            "quantity": quantity,
            "reservation_id": reservation_id,
            "agent_id": agent_id,
            "source_id": source_id,
            "source_ids": account["source_ids"].copy(),
            "warehouse_id": warehouse_id,
            **{name: account[name] for name in _BALANCE_FIELDS},
        }
    )
    validate_resource_ledger_state(
        ledger, resources, set(world.agents), set(world.zones),
        minute_index=world.minute_index,
    )
    world.model_state[RESOURCE_LEDGER_KEY] = ledger
    world.resources = resources


def resource_metric_values(world: Any) -> dict[str, int] | None:
    """Return optional KPI columns for ledger-enabled scenarios."""
    ledger = world.model_state.get(RESOURCE_LEDGER_KEY)
    if ledger is None:
        return None
    validate_resource_ledger_state(
        ledger, world.resources, set(world.agents), set(world.zones),
        minute_index=world.minute_index,
    )
    result: dict[str, int] = {}
    for resource_id in RESOURCE_IDS:
        account = ledger["accounts"][resource_id]
        for name in ("initial", "delivered", "served", "losses", "remaining", "reserved"):
            result[f"{resource_id}_{name}"] = account[name]
    return result


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and _STABLE_ID.fullmatch(value) is not None
