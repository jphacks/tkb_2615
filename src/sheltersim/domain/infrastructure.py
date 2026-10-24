"""Opt-in infrastructure dependency graph and runtime event state (A03)."""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sheltersim.events.models import EventStatus, ScheduledEvent

if TYPE_CHECKING:
    from sheltersim.core.world import WorldState


INFRASTRUCTURE_STATE_KEY = "infrastructure_v1"
INFRASTRUCTURE_START_EVENT_TYPES = frozenset(
    {
        "power_outage",
        "equipment_failure",
        "extreme_heat",
        "extreme_cold",
        "staff_shift_change",
        "supply_route_interruption",
    }
)
INFRASTRUCTURE_COMPLETION_EVENT_TYPES = frozenset(
    {
        "power_restored",
        "equipment_repaired",
        "environmental_stressor_end",
        "supply_route_restored",
    }
)
INFRASTRUCTURE_EVENT_TYPES = (
    INFRASTRUCTURE_START_EVENT_TYPES | INFRASTRUCTURE_COMPLETION_EVENT_TYPES
)
INFRASTRUCTURE_MAX_START_EVENTS = 256
INFRASTRUCTURE_MAX_ROUTE_SHIPMENTS = 100
INFRASTRUCTURE_MAX_ACTIVE_STRESSORS_PER_ZONE = 16
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class InfrastructureDependencyEdge(BaseModel):
    """One dependent component and the component it requires."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dependent_id: _StableId
    dependency_id: _StableId


class ScenarioInfrastructureSpec(BaseModel):
    """Opt-in, source-attributed dependency and experimental environment contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy_version: Literal["infrastructure-events-v1"] = "infrastructure-events-v1"
    source_type: Literal["assumption"] = "assumption"
    source_id: _StableId = "ASM-A03-INFRASTRUCTURE-V1"
    dependency_edges: tuple[InfrastructureDependencyEdge, ...] = ()
    heat_stress_per_minute: float = Field(default=0.001, ge=0.0, le=0.01, allow_inf_nan=False)
    cold_stress_per_minute: float = Field(default=0.001, ge=0.0, le=0.01, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_dependency_graph(self) -> ScenarioInfrastructureSpec:
        edges = {(edge.dependent_id, edge.dependency_id) for edge in self.dependency_edges}
        if len(edges) != len(self.dependency_edges):
            raise ValueError("infrastructure dependency edges must be unique")
        if any(dependent == dependency for dependent, dependency in edges):
            raise ValueError("infrastructure dependency edges cannot point to themselves")

        reverse: dict[str, set[str]] = defaultdict(set)
        nodes: set[str] = set()
        for dependent, dependency in edges:
            reverse[dependency].add(dependent)
            nodes.update((dependent, dependency))
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("infrastructure dependency graph must be acyclic")
            if node in visited:
                return
            visiting.add(node)
            for dependent in sorted(reverse.get(node, ())):
                visit(dependent)
            visiting.remove(node)
            visited.add(node)

        for node in sorted(nodes):
            visit(node)
        object.__setattr__(
            self,
            "dependency_edges",
            tuple(
                sorted(
                    self.dependency_edges, key=lambda edge: (edge.dependent_id, edge.dependency_id)
                )
            ),
        )
        return self


def dependent_facilities(
    infrastructure_config: Mapping[str, Any],
    component_id: str,
    facility_ids: set[str],
) -> tuple[str, ...]:
    """Return facilities downstream of a failed component, in stable order."""
    config = ScenarioInfrastructureSpec.model_validate(dict(infrastructure_config))
    dependents: dict[str, set[str]] = defaultdict(set)
    for edge in config.dependency_edges:
        dependents[edge.dependency_id].add(edge.dependent_id)
    reached: set[str] = set()
    pending = deque([component_id])
    visited = {component_id}
    while pending:
        current = pending.popleft()
        if current in facility_ids:
            reached.add(current)
        for dependent in sorted(dependents.get(current, ())):
            if dependent in facility_ids:
                reached.add(dependent)
            if dependent not in visited:
                visited.add(dependent)
                pending.append(dependent)
    return tuple(sorted(reached))


def create_infrastructure_runtime_state(
    facilities: Mapping[str, Any],
    events: Sequence[Any],
    infrastructure_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Create checkpoint-safe state records for validated scheduled events."""
    config = ScenarioInfrastructureSpec.model_validate(dict(infrastructure_config))
    facility_ids = set(facilities)
    outages: dict[str, dict[str, Any]] = {}
    environmental_stressors: dict[str, dict[str, Any]] = {}
    route_interruptions: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event.event_type
        payload = event.payload
        if event_type == "power_outage":
            outage_id = payload["outage_id"]
            outages[outage_id] = {
                "event_type": event_type,
                "target_id": payload["component_id"],
                "start_event_id": event.id,
                "restore_event_id": None,
                "affected_facility_ids": list(
                    dependent_facilities(
                        config.model_dump(mode="json"), payload["component_id"], facility_ids
                    )
                ),
                "units_out": None,
                "until_minute": payload["until_minute"],
                "active": False,
            }
        elif event_type == "equipment_failure":
            outage_id = payload["outage_id"]
            facility_id = payload["facility_id"]
            outages[outage_id] = {
                "event_type": event_type,
                "target_id": facility_id,
                "start_event_id": event.id,
                "restore_event_id": None,
                "affected_facility_ids": list(
                    dependent_facilities(config.model_dump(mode="json"), facility_id, facility_ids)
                ),
                "units_out": payload["units_out"],
                "until_minute": payload["until_minute"],
                "active": False,
            }
        elif event_type in {"extreme_heat", "extreme_cold"}:
            stressor_id = payload["stressor_id"]
            rate_field = (
                "heat_stress_per_minute"
                if event_type == "extreme_heat"
                else "cold_stress_per_minute"
            )
            environmental_stressors[stressor_id] = {
                "event_type": event_type,
                "zone_id": payload["zone_id"],
                "start_event_id": event.id,
                "end_event_id": None,
                "until_minute": payload["until_minute"],
                "stress_per_minute": getattr(config, rate_field),
                "source_id": config.source_id,
                "active": False,
            }
        elif event_type == "supply_route_interruption":
            outage_id = payload["outage_id"]
            route_interruptions[outage_id] = {
                "route_id": payload["route_id"],
                "start_event_id": event.id,
                "restore_event_id": None,
                "until_minute": payload["until_minute"],
                "shipment_ids": list(payload["shipment_ids"]),
                "active": False,
            }
    return {
        "schema_version": 1,
        "base_working_units": {
            facility_id: facility.working_units
            for facility_id, facility in sorted(facilities.items())
        },
        "outages": outages,
        "environmental_stressors": environmental_stressors,
        "route_interruptions": route_interruptions,
    }


def active_environmental_stress(
    world: WorldState, zone_id: str
) -> tuple[float, tuple[str, ...], str | None]:
    """Return the rate, event IDs, and assumption ID for active zone stressors."""
    state = world.model_state.get(INFRASTRUCTURE_STATE_KEY)
    if not isinstance(state, dict):
        return 0.0, (), None
    entries = sorted(
        (stressor_id, record)
        for stressor_id, record in state["environmental_stressors"].items()
        if record["active"] and record["zone_id"] == zone_id
    )
    return (
        math.fsum(record["stress_per_minute"] for _, record in entries),
        tuple(sorted(record["start_event_id"] for _, record in entries)),
        (entries[0][1]["source_id"] if entries else None),
    )


def infrastructure_event_context_ids(world: WorldState, facility_id: str) -> tuple[str, ...]:
    """Return active causal outage IDs that currently affect a facility."""
    state = world.model_state.get(INFRASTRUCTURE_STATE_KEY)
    if not isinstance(state, dict):
        return ()
    return tuple(
        sorted(
            record["start_event_id"]
            for record in state["outages"].values()
            if record["active"] and facility_id in record["affected_facility_ids"]
        )
    )


def active_facility_outage(world: WorldState, facility_id: str) -> bool:
    """Whether an infrastructure outage currently covers a facility."""
    return bool(infrastructure_event_context_ids(world, facility_id))


def effective_facility_capacities(
    base_working_units: Mapping[str, int],
    outages: Mapping[str, Mapping[str, Any]],
    infrastructure_config: Mapping[str, Any],
    facility_ids: set[str],
) -> dict[str, int]:
    """Calculate working units from all active, independently tracked outage causes."""
    config = ScenarioInfrastructureSpec.model_validate(dict(infrastructure_config))
    forced_offline: set[str] = set()
    units_lost: dict[str, int] = {}
    for outage in outages.values():
        if not outage["active"]:
            continue
        if outage["event_type"] == "power_outage":
            forced_offline.update(outage["affected_facility_ids"])
        else:
            units_lost[outage["target_id"]] = (
                units_lost.get(outage["target_id"], 0) + outage["units_out"]
            )
    equipment_capacity: dict[str, int] = {}
    for facility_id, base_units in base_working_units.items():
        units = units_lost.get(facility_id, 0)
        if units > base_units:
            raise ValueError("combined infrastructure failures exceed installed units")
        equipment_capacity[facility_id] = base_units - units
    for facility_id, capacity in equipment_capacity.items():
        if capacity == 0:
            forced_offline.update(
                dependent_facilities(config.model_dump(mode="json"), facility_id, facility_ids)
            )
    return {
        facility_id: 0 if facility_id in forced_offline else equipment_capacity[facility_id]
        for facility_id in base_working_units
    }


def validate_infrastructure_runtime_state(
    world: WorldState,
    *,
    in_flight_event: ScheduledEvent | None = None,
    validate_event_history: bool = True,
) -> None:
    """Validate exact opt-in state, event links, graph closure and reversibility."""
    configured = world.model_configs.get("infrastructure")
    state = world.model_state.get(INFRASTRUCTURE_STATE_KEY)
    pending_events = list(world.event_queue.pending())
    if in_flight_event is not None:
        if in_flight_event not in pending_events or in_flight_event.due_minute > world.minute_index:
            raise ValueError("in-flight infrastructure event must be pending and due")
    if configured is None:
        if state is not None or (
            any(event.event_type in INFRASTRUCTURE_EVENT_TYPES for event in pending_events)
            or (
                validate_event_history
                and any(
                    record.event.event_type in INFRASTRUCTURE_EVENT_TYPES
                    for record in world.event_log.records
                )
            )
        ):
            raise ValueError("infrastructure events require the infrastructure model")
        return
    if not isinstance(configured, dict):
        raise ValueError("infrastructure model config must be an object")
    config = ScenarioInfrastructureSpec.model_validate(configured)
    canonical_config = config.model_dump(mode="json")
    if configured != canonical_config:
        raise ValueError("infrastructure model config must use complete canonical v1 values")
    expected_fields = {
        "schema_version",
        "base_working_units",
        "outages",
        "environmental_stressors",
        "route_interruptions",
    }
    if (
        not isinstance(state, dict)
        or set(state) != expected_fields
        or state.get("schema_version") != 1
        or not isinstance(state.get("base_working_units"), dict)
        or not isinstance(state.get("outages"), dict)
        or not isinstance(state.get("environmental_stressors"), dict)
        or not isinstance(state.get("route_interruptions"), dict)
    ):
        raise ValueError("infrastructure_v1 has an invalid exact schema")
    if set(state["base_working_units"]) - set(world.facilities):
        raise ValueError("infrastructure base capacity references an unknown facility")
    for facility_id, units in state["base_working_units"].items():
        facility = world.facilities[facility_id]
        if (
            not isinstance(units, int)
            or isinstance(units, bool)
            or not 0 <= units <= facility.capacity
        ):
            raise ValueError("infrastructure base capacity is invalid")

    if not validate_event_history:
        return

    all_events = pending_events + [record.event for record in world.event_log.records]

    by_id = {event.event_id: event for event in all_events}
    outcomes = {record.event.event_id: record for record in world.event_log.records}

    def event_status(event_id: str | None) -> EventStatus | None:
        if event_id is None:
            return None
        if in_flight_event is not None and in_flight_event.event_id == event_id:
            return EventStatus.APPLIED
        outcome = outcomes.get(event_id)
        return outcome.status if outcome is not None else None

    start_events = {
        event.event_id: event
        for event in all_events
        if event.event_type in INFRASTRUCTURE_START_EVENT_TYPES
        and event.event_type != "staff_shift_change"
    }
    expected_outage_events = {
        event.event_id: event
        for event in start_events.values()
        if event.event_type in {"power_outage", "equipment_failure"}
    }
    expected_stressor_events = {
        event.event_id: event
        for event in start_events.values()
        if event.event_type in {"extreme_heat", "extreme_cold"}
    }
    expected_route_events = {
        event.event_id: event
        for event in start_events.values()
        if event.event_type == "supply_route_interruption"
    }
    if len(start_events) > INFRASTRUCTURE_MAX_START_EVENTS:
        raise ValueError("infrastructure runtime exceeds the bounded event limit")
    if any(
        not isinstance(record, dict) or not isinstance(record.get("start_event_id"), str)
        for record in state["outages"].values()
    ) or {record["start_event_id"] for record in state["outages"].values()} != set(
        expected_outage_events
    ):
        raise ValueError("infrastructure outage records do not match scheduled outage events")
    if any(
        not isinstance(record, dict) or not isinstance(record.get("start_event_id"), str)
        for record in state["environmental_stressors"].values()
    ) or {record["start_event_id"] for record in state["environmental_stressors"].values()} != set(
        expected_stressor_events
    ):
        raise ValueError("environmental stressor records do not match scheduled events")
    if any(
        not isinstance(record, dict) or not isinstance(record.get("start_event_id"), str)
        for record in state["route_interruptions"].values()
    ) or {record["start_event_id"] for record in state["route_interruptions"].values()} != set(
        expected_route_events
    ):
        raise ValueError("route interruption records do not match scheduled events")

    restorations: set[str] = set()
    for outage_id, record in state["outages"].items():
        expected_record_keys = {
            "event_type",
            "target_id",
            "start_event_id",
            "restore_event_id",
            "affected_facility_ids",
            "units_out",
            "until_minute",
            "active",
        }
        if (
            not isinstance(outage_id, str)
            or not _STABLE_ID.fullmatch(outage_id)
            or not isinstance(record, dict)
            or set(record) != expected_record_keys
        ):
            raise ValueError("infrastructure outage record ID is invalid")
        event = by_id.get(record.get("start_event_id"))
        if event is None or record.get("event_type") != event.event_type:
            raise ValueError("infrastructure outage record references an unknown start event")
        payload = event.payload
        expected_payload = (
            {"outage_id", "component_id", "until_minute"}
            if event.event_type == "power_outage"
            else {"outage_id", "facility_id", "units_out", "until_minute"}
        )
        if (
            event.kind.value not in {"exogenous", "intervention"}
            or event.source_id is None
            or event.event_type not in {"power_outage", "equipment_failure"}
            or set(payload) != expected_payload
            or not isinstance(payload.get("until_minute"), int)
            or isinstance(payload.get("until_minute"), bool)
            or not event.due_minute < payload["until_minute"] <= world.duration_minutes
        ):
            raise ValueError("infrastructure outage start event is invalid")
        expected_target = payload.get("component_id", payload.get("facility_id"))
        if (
            not isinstance(expected_target, str)
            or not _STABLE_ID.fullmatch(expected_target)
            or record.get("target_id") != expected_target
            or record.get("until_minute") != payload.get("until_minute")
            or not isinstance(record.get("active"), bool)
            or not isinstance(record.get("affected_facility_ids"), list)
        ):
            raise ValueError("infrastructure outage record does not match its start event")
        expected_facilities = dependent_facilities(
            canonical_config, expected_target, set(world.facilities)
        )
        if record["affected_facility_ids"] != list(expected_facilities):
            raise ValueError("infrastructure outage facility closure is invalid")
        expected_units = payload.get("units_out")
        if record.get("units_out") != expected_units:
            raise ValueError("infrastructure outage units do not match the start event")
        restore_id = record.get("restore_event_id")
        if restore_id is not None:
            if restore_id in restorations:
                raise ValueError("infrastructure restoration event IDs must be unique")
            restorations.add(restore_id)
            restore = by_id.get(restore_id)
            expected_type = (
                "power_restored" if event.event_type == "power_outage" else "equipment_repaired"
            )
            if (
                restore is None
                or restore.event_type != expected_type
                or restore.kind.value != "completion"
                or set(restore.payload) != {"outage_id"}
                or restore.due_minute != payload["until_minute"]
                or restore.causation_id != event.event_id
                or restore.source_id != event.source_id
                or restore.payload.get("outage_id") != outage_id
            ):
                raise ValueError("infrastructure restoration event does not match its outage")
        if (
            payload["until_minute"] < world.duration_minutes
            and event_status(event.event_id) is EventStatus.APPLIED
            and restore_id is None
        ):
            raise ValueError("applied infrastructure outage is missing its restoration event")
        expected_active = (
            event_status(event.event_id) is EventStatus.APPLIED
            and event_status(restore_id) is not EventStatus.APPLIED
        )
        if record["active"] != expected_active:
            raise ValueError("infrastructure outage activity does not match event history")

    for stressor_id, record in state["environmental_stressors"].items():
        event = by_id.get(record.get("start_event_id")) if isinstance(record, dict) else None
        if (
            not isinstance(stressor_id, str)
            or not _STABLE_ID.fullmatch(stressor_id)
            or event is None
            or event.kind.value not in {"exogenous", "intervention"}
            or event.source_id is None
            or set(record)
            != {
                "event_type",
                "zone_id",
                "start_event_id",
                "end_event_id",
                "until_minute",
                "stress_per_minute",
                "source_id",
                "active",
            }
            or event.event_type not in {"extreme_heat", "extreme_cold"}
            or set(event.payload) != {"stressor_id", "zone_id", "until_minute"}
            or record.get("event_type") != event.event_type
            or record.get("zone_id") != event.payload.get("zone_id")
            or record.get("until_minute") != event.payload.get("until_minute")
            or not isinstance(event.payload.get("until_minute"), int)
            or isinstance(event.payload.get("until_minute"), bool)
            or not event.due_minute < event.payload["until_minute"] <= world.duration_minutes
            or record.get("source_id") != config.source_id
            or not isinstance(record.get("active"), bool)
        ):
            raise ValueError("environmental stressor record does not match its event")
        rate_field = (
            "heat_stress_per_minute"
            if event.event_type == "extreme_heat"
            else "cold_stress_per_minute"
        )
        if record.get("stress_per_minute") != getattr(config, rate_field):
            raise ValueError("environmental stressor rate does not match its assumption")
        end_id = record.get("end_event_id")
        if end_id is not None:
            if end_id in restorations:
                raise ValueError("environmental end event IDs must be unique")
            restorations.add(end_id)
            end = by_id.get(end_id)
            if (
                end is None
                or end.event_type != "environmental_stressor_end"
                or end.kind.value != "completion"
                or set(end.payload) != {"stressor_id", "zone_id"}
                or end.due_minute != event.payload["until_minute"]
                or end.causation_id != event.event_id
                or end.source_id != event.source_id
                or end.payload.get("stressor_id") != stressor_id
            ):
                raise ValueError("environmental end event does not match its stressor")
        if (
            event.payload["until_minute"] < world.duration_minutes
            and event_status(event.event_id) is EventStatus.APPLIED
            and end_id is None
        ):
            raise ValueError("applied environmental stressor is missing its end event")
        expected_active = (
            event_status(event.event_id) is EventStatus.APPLIED
            and event_status(end_id) is not EventStatus.APPLIED
        )
        if record["active"] != expected_active:
            raise ValueError("environmental stressor activity does not match event history")
    active_stressors_per_zone: dict[str, int] = defaultdict(int)
    for record in state["environmental_stressors"].values():
        if record["active"]:
            active_stressors_per_zone[record["zone_id"]] += 1
    if any(
        count > INFRASTRUCTURE_MAX_ACTIVE_STRESSORS_PER_ZONE
        for count in active_stressors_per_zone.values()
    ):
        raise ValueError("infrastructure runtime exceeds the per-zone stressor limit")

    operational = world.model_state.get("operational_events_v1", {})
    shipment_ids = set(operational.get("shipments", {})) if isinstance(operational, dict) else set()
    for outage_id, record in state["route_interruptions"].items():
        event = by_id.get(record.get("start_event_id")) if isinstance(record, dict) else None
        payload_shipment_ids = event.payload.get("shipment_ids") if event is not None else None
        if (
            not isinstance(outage_id, str)
            or not _STABLE_ID.fullmatch(outage_id)
            or event is None
            or event.kind.value not in {"exogenous", "intervention"}
            or event.source_id is None
            or set(event.payload) != {"outage_id", "route_id", "shipment_ids", "until_minute"}
            or set(record)
            != {
                "route_id",
                "start_event_id",
                "restore_event_id",
                "until_minute",
                "shipment_ids",
                "active",
            }
            or event.event_type != "supply_route_interruption"
            or record.get("route_id") != event.payload.get("route_id")
            or record.get("until_minute") != event.payload.get("until_minute")
            or not isinstance(event.payload.get("until_minute"), int)
            or isinstance(event.payload.get("until_minute"), bool)
            or not event.due_minute < event.payload["until_minute"] <= world.duration_minutes
            or not isinstance(payload_shipment_ids, (list, tuple))
            or len(payload_shipment_ids) > INFRASTRUCTURE_MAX_ROUTE_SHIPMENTS
            or record.get("shipment_ids") != list(payload_shipment_ids)
            or not isinstance(record.get("active"), bool)
            or not set(record.get("shipment_ids", ())) <= shipment_ids
        ):
            raise ValueError("route interruption record does not match its start event")
        restore_id = record.get("restore_event_id")
        if restore_id is not None:
            if restore_id in restorations:
                raise ValueError("route restoration event IDs must be unique")
            restorations.add(restore_id)
            restore = by_id.get(restore_id)
            if (
                restore is None
                or restore.event_type != "supply_route_restored"
                or restore.kind.value != "completion"
                or set(restore.payload) != {"outage_id", "route_id"}
                or restore.due_minute != event.payload["until_minute"]
                or restore.causation_id != event.event_id
                or restore.source_id != event.source_id
                or restore.payload.get("outage_id") != outage_id
                or restore.payload.get("route_id") != record["route_id"]
            ):
                raise ValueError("route restoration event does not match its interruption")
        if (
            event.payload["until_minute"] < world.duration_minutes
            and event_status(event.event_id) is EventStatus.APPLIED
            and restore_id is None
        ):
            raise ValueError("applied route interruption is missing its restoration event")
        expected_active = (
            event_status(event.event_id) is EventStatus.APPLIED
            and event_status(restore_id) is not EventStatus.APPLIED
        )
        if record["active"] != expected_active:
            raise ValueError("route interruption activity does not match event history")

    affected_facilities = {
        facility_id
        for record in state["outages"].values()
        if record["active"]
        for facility_id in record["affected_facility_ids"]
    }
    if affected_facilities:
        expected_capacities = effective_facility_capacities(
            state["base_working_units"],
            state["outages"],
            canonical_config,
            set(world.facilities),
        )
        if any(
            world.facilities[facility_id].working_units != expected_capacities[facility_id]
            for facility_id in affected_facilities
        ):
            raise ValueError("active infrastructure outage and facility capacity disagree")

    completion_ids = {
        event.event_id
        for event in all_events
        if event.event_type in INFRASTRUCTURE_COMPLETION_EVENT_TYPES
    }
    if completion_ids != restorations:
        raise ValueError("infrastructure completion events must match active records exactly")
