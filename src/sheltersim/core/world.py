"""Authoritative mutable world state and invariant validation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any

from sheltersim.contracts.policies import selected_models
from sheltersim.core.history import records_since_previous_tick
from sheltersim.core.saved_items import SavedItems
from sheltersim.domain.environment import (
    ZONE_ENVIRONMENT_STATE_KEY,
    create_zone_environment_runtime_state,
    validate_zone_environment_runtime_state,
)
from sheltersim.domain.information import (
    INFORMATION_SPREAD_STATE_KEY,
    create_information_runtime_state,
    validate_information_runtime_state,
)
from sheltersim.domain.infrastructure import (
    INFRASTRUCTURE_STATE_KEY,
    create_infrastructure_runtime_state,
    validate_infrastructure_runtime_state,
)
from sheltersim.domain.models import (
    ActiveTask,
    Activity,
    AgentProfile,
    AgentState,
    FacilityState,
    InformationSpreadConfig,
    InitialNeedProfile,
    Mobility,
    NeedState,
    StaffState,
    SupportRequestState,
    SupportRequestStatus,
    ZoneEdgeState,
    ZoneState,
    validate_unit_interval,
)
from sheltersim.domain.operational import (
    OPERATIONAL_EVENTS_KEY,
    create_operational_events_state,
    operational_events_enabled,
    validate_operational_events_state,
)
from sheltersim.domain.psychological_state import (
    MEAN_REVERTING_PSYCHOLOGY_POLICIES,
    PSYCHOLOGICAL_STATE_KEY,
    create_psychological_state,
    validate_psychological_state,
)
from sheltersim.domain.resources import (
    RESOURCE_LEDGER_KEY,
    create_resource_ledger,
    validate_resource_ledger,
    validate_resource_ledger_state,
)
from sheltersim.events.models import (
    EventKind,
    EventLogRecord,
    EventStatus,
    ScheduledEvent,
)
from sheltersim.events.queue import DuplicateEventError, EventLog, EventQueue
from sheltersim.experiments.scenario import ResourceServiceConfig, ScenarioSpec


class StateInvariantError(ValueError):
    """Raised when a world state violates a simulation invariant."""


def _event_from_document(document: dict[str, Any]) -> ScheduledEvent:
    return ScheduledEvent(**document)


@dataclass
class WorldState:
    """Mutable state owned by the engine/controller for one scenario run."""

    scenario_id: str
    seed: int
    duration_minutes: int
    minute_index: int = 0
    agents: dict[str, AgentState] = field(default_factory=dict)
    zones: dict[str, ZoneState] = field(default_factory=dict)
    zone_edges: dict[str, ZoneEdgeState] = field(default_factory=dict)
    facilities: dict[str, FacilityState] = field(default_factory=dict)
    staff: dict[str, StaffState] = field(default_factory=dict)
    support_requests: dict[str, SupportRequestState] = field(default_factory=dict)
    resources: dict[str, int] = field(default_factory=dict)
    active_tasks: dict[str, ActiveTask] = field(default_factory=dict)
    event_queue: EventQueue = field(default_factory=EventQueue)
    event_log: EventLog = field(default_factory=EventLog)
    rng_state: Any = None
    id_counters: dict[str, int] = field(default_factory=dict)
    metric_state: dict[str, float] = field(default_factory=dict)
    model_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_state: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 5

    @classmethod
    def from_scenario(cls, scenario: ScenarioSpec) -> WorldState:
        """Create an initial state from a validated scenario."""
        agents = {
            profile.id: AgentState(
                profile=AgentProfile(
                    id=profile.id,
                    group_id=profile.group_id,
                    initial_zone_id=profile.initial_zone_id,
                    mobility=Mobility(profile.mobility),
                    support_need=profile.support_need,
                    stress_sensitivity=profile.stress_sensitivity,
                    rng_key=profile.rng_key,
                    initial_needs=InitialNeedProfile(**profile.initial_needs.model_dump()),
                    provenance=profile.provenance.model_dump(mode="json"),
                ),
                zone_id=profile.initial_zone_id,
                needs=NeedState(**profile.initial_needs.model_dump()),
            )
            for profile in scenario.profiles
        }
        world = cls(
            scenario_id=scenario.id,
            seed=scenario.seed,
            duration_minutes=scenario.duration_minutes,
            agents=agents,
            zones={
                zone.id: ZoneState(
                    id=zone.id,
                    name=zone.name,
                    capacity=zone.capacity,
                    display_x=zone.display_x,
                    display_y=zone.display_y,
                )
                for zone in scenario.zones
            },
            zone_edges={
                edge.id: ZoneEdgeState(
                    id=edge.id,
                    from_zone_id=edge.from_zone_id,
                    to_zone_id=edge.to_zone_id,
                    travel_minutes=edge.travel_minutes,
                    closed=edge.closed,
                )
                for edge in scenario.zone_edges
            },
            facilities={
                facility.id: FacilityState(
                    id=facility.id,
                    kind=facility.kind,
                    zone_id=facility.zone_id,
                    capacity=facility.capacity,
                    working_units=(
                        facility.capacity
                        if facility.working_units is None
                        else facility.working_units
                    ),
                )
                for facility in scenario.facilities
            },
            staff={
                person.id: StaffState(
                    id=person.id,
                    zone_id=person.zone_id,
                    skills=person.skills,
                )
                for person in scenario.staff
            },
            event_queue=EventQueue(
                [
                    ScheduledEvent(
                        event_id=event.id,
                        kind=event.kind,
                        due_minute=event.due_minute,
                        priority=event.priority,
                        sequence=event.sequence,
                        event_type=event.event_type,
                        payload=event.payload,
                        source_id=event.source_id,
                        causation_id=event.causation_id,
                        provenance=event.provenance,
                    )
                    for event in scenario.scheduled_events
                ]
            ),
            id_counters={
                "agent": len(agents),
                "event": max((event.sequence for event in scenario.scheduled_events), default=-1)
                + 1,
            },
            model_configs=deepcopy(scenario.model_configs),
            schema_version=(6 if "resource_service" in scenario.model_configs else 5),
        )
        if scenario.initial_stock is not None:
            initial_stock = scenario.initial_stock.model_dump(mode="json")
            initial_stock["source_ids"] = sorted(initial_stock["source_ids"])
            world.resources = {
                "water_ml": initial_stock["water_ml"],
                "food_meal_units": initial_stock["food_meal_units"],
            }
            world.model_state[RESOURCE_LEDGER_KEY] = create_resource_ledger(initial_stock)
        if world.schema_version == 6:
            world.model_state["resource_service_v1"] = {
                "schema_version": 1,
                "cancelled_task_ids": [],
                "active_reservation_ids": [],
                "request_history": [],
                "queue_stall_history": [],
                "stall_state": {},
            }
        if operational_events_enabled(scenario.model_configs, scenario.scheduled_events):
            world.model_state[OPERATIONAL_EVENTS_KEY] = create_operational_events_state(
                scenario.scheduled_events, set(world.staff)
            )
        if "infrastructure" in world.model_configs:
            world.model_state[INFRASTRUCTURE_STATE_KEY] = create_infrastructure_runtime_state(
                world.facilities,
                scenario.scheduled_events,
                world.model_configs["infrastructure"],
            )
        if "zone_environment" in world.model_configs:
            world.model_state[ZONE_ENVIRONMENT_STATE_KEY] = create_zone_environment_runtime_state()
        information_config = InformationSpreadConfig.from_mapping(
            world.model_configs.get("information_spread")
        )
        if information_config.enabled:
            world.model_state[INFORMATION_SPREAD_STATE_KEY] = create_information_runtime_state(
                set(world.agents)
            )
        if selected_models(world.model_configs)["psychology"] in MEAN_REVERTING_PSYCHOLOGY_POLICIES:
            psychology_config = world.model_configs.get("psychology", {})
            baselines = {
                agent_id: psychology_config.get("baseline_distress_by_agent", {}).get(agent_id, 0.0)
                for agent_id in world.agents
            }
            world.model_state[PSYCHOLOGICAL_STATE_KEY] = create_psychological_state(baselines)
        from sheltersim.domain.simulator_state import initialize_simulator_extensions

        initialize_simulator_extensions(world)
        from sheltersim.contracts.runtime import initialize_declared_states

        initialize_declared_states(world)
        world.validate()
        return world

    def validate(
        self,
        *,
        _in_flight_event: ScheduledEvent | None = None,
        validate_event_history: bool = True,
    ) -> None:
        """Check world invariants, optionally auditing immutable EventLog history."""
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in (5, 6)
        ):
            raise StateInvariantError(
                f"unsupported world-state schema version: {self.schema_version}"
            )
        try:
            selected_models(self.model_configs)
            from sheltersim.contracts.runtime import validate_declared_states

            validate_declared_states(self)
        except ValueError as exc:
            raise StateInvariantError(str(exc)) from exc
        extension_keys = {
            "movement_v1",
            "agent_dynamics_v1",
            "social_network_v1",
            "staff_dynamics_v1",
            "population_dynamics_v1",
            "body_state_v1",
            "staff_operations_v1",
            "temporary_spaces_v1",
            "shared_operations_v1",
        }
        from sheltersim.domain.simulator_state import simulator_signatures

        if extension_keys.intersection(self.model_state) or simulator_signatures(
            self.model_configs
        ):
            from sheltersim.domain.simulator_state import validate_simulator_extensions

            try:
                validate_simulator_extensions(self, validate_event_history=validate_event_history)
            except (ValueError, KeyError, TypeError) as exc:
                raise StateInvariantError("invalid simulator extension state") from exc
        resource_service_enabled = "resource_service" in self.model_configs
        if resource_service_enabled != (self.schema_version == 6):
            raise StateInvariantError(
                "resource_service requires WorldState v6; v6 requires resource_service"
            )
        if resource_service_enabled:
            try:
                resource_config = ResourceServiceConfig.model_validate(
                    self.model_configs["resource_service"]
                )
            except ValueError as exc:
                raise StateInvariantError(f"invalid resource_service config: {exc}") from exc
            if self.model_configs["resource_service"] != resource_config.model_dump(mode="json"):
                raise StateInvariantError(
                    "resource_service config must contain the complete canonical v1 values"
                )
            if RESOURCE_LEDGER_KEY not in self.model_state:
                raise StateInvariantError("WorldState v6 requires a resource ledger")
            service_state = self.model_state.get("resource_service_v1")
            if (
                not isinstance(service_state, dict)
                or set(service_state)
                != {
                    "schema_version",
                    "cancelled_task_ids",
                    "active_reservation_ids",
                    "request_history",
                    "queue_stall_history",
                    "stall_state",
                }
                or service_state.get("schema_version") != 1
                or not isinstance(service_state.get("cancelled_task_ids"), list)
                or any(
                    not isinstance(value, str) or not value
                    for value in service_state["cancelled_task_ids"]
                )
                or len(service_state["cancelled_task_ids"])
                != len(set(service_state["cancelled_task_ids"]))
                or not isinstance(service_state.get("active_reservation_ids"), list)
                or any(
                    not isinstance(value, str) or not value
                    for value in service_state["active_reservation_ids"]
                )
                or len(service_state["active_reservation_ids"])
                != len(set(service_state["active_reservation_ids"]))
                or not isinstance(service_state.get("request_history"), list)
                or not isinstance(service_state.get("queue_stall_history"), list)
                or not isinstance(service_state.get("stall_state"), dict)
            ):
                raise StateInvariantError("resource_service_v1 state is invalid")
            valid_actions = {"fetch_water", "collect_meal"}
            valid_outcomes = {"queued", "travel_scheduled", "rejected"}
            valid_request_reasons = {
                "arrived_at_facility",
                "route_available",
                "in_room_route_available",
                "room_route_blocked",
                "no_facility",
                "no_route",
                "request_rejected",
                "travel_rejected",
                "service_disabled",
            }
            request_history = service_state["request_history"]
            stall_history = service_state["queue_stall_history"]
            # People away on a temporary exit or transfer keep their place in the
            # population lifecycle archive; their past service records stay valid (#79).
            lifecycle = self.model_state.get("population_lifecycle_v1")
            retained_ids = (
                set(lifecycle.get("people", {}))
                if isinstance(lifecycle, dict) and isinstance(lifecycle.get("people"), dict)
                else set()
            )
            known_agent_ids = set(self.agents) | retained_ids
            if not validate_event_history:
                request_history = records_since_previous_tick(request_history, self.minute_index)
                stall_history = records_since_previous_tick(stall_history, self.minute_index)
            for record in request_history:
                if (
                    not isinstance(record, dict)
                    or set(record)
                    != {
                        "minute",
                        "agent_id",
                        "action",
                        "outcome",
                        "reason",
                        "facility_id",
                        "origin_zone_id",
                        "detail",
                    }
                    or not isinstance(record["minute"], int)
                    or isinstance(record["minute"], bool)
                    or not 0 <= record["minute"] <= self.minute_index
                    or record["agent_id"] not in known_agent_ids
                    or record["action"] not in valid_actions
                    or record["outcome"] not in valid_outcomes
                    or record["reason"] not in valid_request_reasons
                    or record["origin_zone_id"] not in self.zones
                    or (
                        record["facility_id"] is not None
                        and record["facility_id"] not in self.facilities
                    )
                    or (record["detail"] is not None and not isinstance(record["detail"], str))
                ):
                    raise StateInvariantError("resource service request history is invalid")
                expected_kind = (
                    "water_station" if record["action"] == "fetch_water" else "meal_distribution"
                )
                facility_id = record["facility_id"]
                if facility_id is not None and self.facilities[facility_id].kind != expected_kind:
                    raise StateInvariantError("resource service request history has wrong facility")
                if (record["outcome"], record["reason"]) not in {
                    ("queued", "arrived_at_facility"),
                    ("travel_scheduled", "route_available"),
                    # Agents already in the facility's zone walk an in-room route instead.
                    ("travel_scheduled", "in_room_route_available"),
                    ("rejected", "room_route_blocked"),
                    ("rejected", "no_facility"),
                    ("rejected", "no_route"),
                    ("rejected", "request_rejected"),
                    ("rejected", "travel_rejected"),
                    ("rejected", "service_disabled"),
                }:
                    raise StateInvariantError("resource service request outcome is inconsistent")
            known_reasons = {"capacity", "staff", "stock", "run_end", "water_outage"}
            previous_stall_minute = -1
            for record in stall_history:
                if (
                    not isinstance(record, dict)
                    or set(record) != {"minute", "facility_id", "agent_id", "reason"}
                    or not isinstance(record["minute"], int)
                    or isinstance(record["minute"], bool)
                    or record["minute"] < 0
                    or record["minute"] < previous_stall_minute
                    or record["minute"] > self.minute_index
                    or not isinstance(record["facility_id"], str)
                    or record["facility_id"] not in self.facilities
                    or not isinstance(record["agent_id"], str)
                    or record["agent_id"] not in known_agent_ids
                    or record["reason"] not in known_reasons
                ):
                    raise StateInvariantError("resource queue stall history is invalid")
                previous_stall_minute = record["minute"]
            for facility_id, record in service_state["stall_state"].items():
                if (
                    facility_id not in self.facilities
                    or not isinstance(record, dict)
                    or set(record) != {"agent_id", "reason"}
                    or record["agent_id"] not in self.agents
                    or record["reason"] not in known_reasons
                ):
                    raise StateInvariantError("resource queue stall state is invalid")
                from sheltersim.domain.facility_queues import queue_head_agent_ids

                # A facility with independent queues has one head per queue (#118).
                if self.facilities[facility_id].kind not in (
                    "water_station",
                    "meal_distribution",
                ) or record["agent_id"] not in queue_head_agent_ids(self, facility_id):
                    raise StateInvariantError(
                        "resource queue stall must describe the current facility queue head"
                    )
        elif "resource_service_v1" in self.model_state:
            raise StateInvariantError("resource_service state requires WorldState v6")
        try:
            information_config = InformationSpreadConfig.from_mapping(
                self.model_configs.get("information_spread")
            )
        except ValueError as exc:
            raise StateInvariantError(f"invalid information_spread config: {exc}") from exc
        if "information_spread" in self.model_configs:
            if self.model_configs["information_spread"] != information_config.to_dict():
                raise StateInvariantError(
                    "information_spread config must contain the complete canonical v1 values"
                )
        if information_config.enabled:
            try:
                validate_information_runtime_state(
                    self.model_state.get(INFORMATION_SPREAD_STATE_KEY),
                    agent_ids=set(self.agents),
                    zone_ids=set(self.zones),
                    duration_minutes=self.duration_minutes,
                )
            except ValueError as exc:
                raise StateInvariantError(f"invalid information-spread state: {exc}") from exc
        elif INFORMATION_SPREAD_STATE_KEY in self.model_state:
            raise StateInvariantError(
                "information-spread state requires an enabled information_spread model"
            )
        if selected_models(self.model_configs)["psychology"] in MEAN_REVERTING_PSYCHOLOGY_POLICIES:
            try:
                validate_psychological_state(self.model_state.get(PSYCHOLOGICAL_STATE_KEY))
            except ValueError as exc:
                raise StateInvariantError(f"invalid psychological distress state: {exc}") from exc
        elif PSYCHOLOGICAL_STATE_KEY in self.model_state:
            raise StateInvariantError("psychological distress state requires its registered model")
        try:
            validate_operational_events_state(
                self,
                in_flight_event=_in_flight_event,
                validate_event_history=validate_event_history,
            )
        except ValueError as exc:
            raise StateInvariantError(f"invalid operational events state: {exc}") from exc
        try:
            validate_infrastructure_runtime_state(
                self,
                in_flight_event=_in_flight_event,
                validate_event_history=validate_event_history,
            )
        except ValueError as exc:
            raise StateInvariantError(f"invalid infrastructure state: {exc}") from exc
        try:
            validate_zone_environment_runtime_state(
                self, validate_event_history=validate_event_history
            )
        except ValueError as exc:
            raise StateInvariantError(f"invalid zone environment state: {exc}") from exc
        try:
            from sheltersim.events.effects import validate_effect_program_state

            validate_effect_program_state(self, validate_event_history=validate_event_history)
        except ValueError as exc:
            raise StateInvariantError(f"invalid EffectProgram state: {exc}") from exc
        if self.duration_minutes <= 0 or not 0 <= self.minute_index <= self.duration_minutes:
            raise StateInvariantError("world clock is outside the scenario duration")
        if self.seed < 0:
            raise StateInvariantError("scenario seed cannot be negative")

        for mapping_name, values in (
            ("agent", self.agents),
            ("zone", self.zones),
            ("zone edge", self.zone_edges),
            ("facility", self.facilities),
            ("staff", self.staff),
            ("active task", self.active_tasks),
        ):
            for key, item in values.items():
                if not key or key != item.id:
                    raise StateInvariantError(
                        f"{mapping_name} mapping key must match its stable ID"
                    )

        for edge in self.zone_edges.values():
            if edge.from_zone_id not in self.zones or edge.to_zone_id not in self.zones:
                raise StateInvariantError(f"zone edge {edge.id!r} references an unknown zone")
            if edge.travel_minutes <= 0 or edge.from_zone_id == edge.to_zone_id:
                raise StateInvariantError(f"zone edge {edge.id!r} has invalid travel data")

        route_config = self.model_configs.get("layout_routing")
        if route_config is not None:
            if not isinstance(route_config, dict):
                raise StateInvariantError("layout route capacity configuration is invalid")
            capacities = route_config.get("max_agents_in_transit_by_edge")
            if (
                set(route_config)
                != {
                    "schema_version",
                    "source_type",
                    "source_id",
                    "max_agents_in_transit_by_edge",
                }
                or route_config.get("schema_version") != 1
                or route_config.get("source_type") != "assumption"
                or route_config.get("source_id") != "ASM-A05-ROUTE-EDIT-V1"
                or not isinstance(capacities, dict)
                or set(capacities) - set(self.zone_edges)
                or any(
                    not isinstance(capacity, int)
                    or isinstance(capacity, bool)
                    or not 1 <= capacity <= 100
                    for capacity in capacities.values()
                )
            ):
                raise StateInvariantError("layout route capacity configuration is invalid")

        zone_occupancy: dict[str, int] = dict.fromkeys(self.zones, 0)
        for agent in self.agents.values():
            if agent.zone_id not in self.zones:
                raise StateInvariantError(
                    f"Agent {agent.profile.id!r} references unknown zone {agent.zone_id!r}"
                )
            zone_occupancy[agent.zone_id] += 1
            try:
                agent.activity = Activity(agent.activity)
            except (TypeError, ValueError) as exc:
                raise StateInvariantError(
                    f"Agent {agent.profile.id!r} has an invalid activity"
                ) from exc
            if not isinstance(agent.profile.initial_needs, InitialNeedProfile):
                raise StateInvariantError(
                    f"Agent {agent.profile.id!r} has invalid initial need profile"
                )
            if not isinstance(agent.needs, NeedState):
                raise StateInvariantError(f"Agent {agent.profile.id!r} has invalid need state")
            try:
                agent.needs.validate()
            except ValueError as exc:
                raise StateInvariantError(
                    f"Agent {agent.profile.id!r} has invalid need levels: {exc}"
                ) from exc
            if self.schema_version == 5 and (
                agent.needs.hydration_need != 0.0
                or agent.profile.initial_needs.hydration_need != 0.0
            ):
                raise StateInvariantError("WorldState v5 requires hydration_need to be zero")
            for name, value in (("stress", agent.stress), ("trust", agent.trust)):
                try:
                    validate_unit_interval(name, value)
                except ValueError as exc:
                    raise StateInvariantError(
                        f"Agent {agent.profile.id!r} has invalid {name}: {exc}"
                    ) from exc
            if (
                not isinstance(agent.next_decision_at, int)
                or isinstance(agent.next_decision_at, bool)
                or agent.next_decision_at < 0
            ):
                raise StateInvariantError(
                    f"Agent {agent.profile.id!r} has invalid next decision time"
                )
            if (
                not isinstance(agent.support_cooldown_until, int)
                or isinstance(agent.support_cooldown_until, bool)
                or agent.support_cooldown_until < 0
            ):
                raise StateInvariantError(
                    f"Agent {agent.profile.id!r} has invalid support cooldown"
                )
        from sheltersim.domain.temporary_spaces import effective_total_zone_capacity

        for zone_id, occupancy in zone_occupancy.items():
            capacity = effective_total_zone_capacity(self, zone_id, occupancy=occupancy)
            if capacity is not None and occupancy > capacity:
                raise StateInvariantError(
                    f"zone {zone_id!r} exceeds nominal plus approved temporary or "
                    "physical-room capacity"
                )

        for facility in self.facilities.values():
            if facility.zone_id not in self.zones:
                raise StateInvariantError(f"facility {facility.id!r} references an unknown zone")
            if facility.capacity < 0 or not 0 <= facility.working_units <= facility.capacity:
                raise StateInvariantError(f"facility {facility.id!r} has invalid capacity")
            # An outage can take units out of service while their users finish, so
            # occupancy is bounded by the installed units, not the working ones (#188).
            # Service never starts beyond the working units.
            if len(facility.active_agent_ids) > facility.capacity:
                raise StateInvariantError(f"facility {facility.id!r} is over capacity")
            if self.schema_version == 5 and (
                facility.resource_active_agent_ids or facility.resource_queue_agent_ids
            ):
                raise StateInvariantError("WorldState v5 cannot contain resource service occupancy")

        queued_agents: set[str] = set()
        active_agents: set[str] = set()
        assigned_tasks: set[str] = set()
        for facility in self.facilities.values():
            if len(facility.queue_agent_ids) != len(set(facility.queue_agent_ids)):
                raise StateInvariantError(f"facility {facility.id!r} has duplicate queue entries")
            if len(facility.active_agent_ids) != len(set(facility.active_agent_ids)):
                raise StateInvariantError(f"facility {facility.id!r} has duplicate active entries")
            for agent_id in facility.queue_agent_ids:
                if agent_id not in self.agents:
                    raise StateInvariantError(
                        f"facility queue references unknown Agent {agent_id!r}"
                    )
                if agent_id in queued_agents or agent_id in active_agents:
                    raise StateInvariantError(
                        f"Agent {agent_id!r} appears in multiple facility queues or services"
                    )
                if self.agents[agent_id].activity is not Activity.QUEUED:
                    raise StateInvariantError(
                        f"queued Agent {agent_id!r} must have queued activity"
                    )
                queued_agents.add(agent_id)
            for agent_id in facility.active_agent_ids:
                if agent_id not in self.agents:
                    raise StateInvariantError(
                        f"facility service references unknown Agent {agent_id!r}"
                    )
                if agent_id in active_agents or agent_id in queued_agents:
                    raise StateInvariantError(
                        f"Agent {agent_id!r} appears in multiple facility queues or services"
                    )
                if self.agents[agent_id].activity is not Activity.IN_SERVICE:
                    raise StateInvariantError(
                        f"serviced Agent {agent_id!r} must have in_service activity"
                    )
                active_agents.add(agent_id)

        for facility in self.facilities.values():
            if self.schema_version == 5:
                continue
            is_resource_facility = facility.kind in ("water_station", "meal_distribution")
            if is_resource_facility and (facility.queue_agent_ids or facility.active_agent_ids):
                raise StateInvariantError(
                    f"resource facility {facility.id!r} cannot use legacy facility queues"
                )
            if not is_resource_facility and (
                facility.resource_active_agent_ids or facility.resource_queue_agent_ids
            ):
                raise StateInvariantError(
                    f"non-resource facility {facility.id!r} cannot own resource service slots"
                )
            if len(facility.resource_queue_agent_ids) != len(
                set(facility.resource_queue_agent_ids)
            ):
                raise StateInvariantError(
                    f"facility {facility.id!r} has duplicate resource queue entries"
                )
            if len(facility.resource_active_agent_ids) != len(
                set(facility.resource_active_agent_ids)
            ):
                raise StateInvariantError(
                    f"facility {facility.id!r} has duplicate resource active entries"
                )
            if len(facility.resource_active_agent_ids) > facility.capacity:
                raise StateInvariantError(
                    f"resource facility {facility.id!r} is over its installed capacity"
                )
            for agent_id in facility.resource_queue_agent_ids:
                if agent_id not in self.agents:
                    raise StateInvariantError(
                        f"resource queue references unknown Agent {agent_id!r}"
                    )
                if agent_id in queued_agents or agent_id in active_agents:
                    raise StateInvariantError(
                        f"Agent {agent_id!r} appears in multiple facility queues or services"
                    )
                if (
                    self.agents[agent_id].activity is not Activity.QUEUED
                    or self.agents[agent_id].target_id != facility.id
                    or self.agents[agent_id].zone_id != facility.zone_id
                ):
                    raise StateInvariantError(
                        f"resource queued Agent {agent_id!r} has inconsistent activity, target, "
                        "or zone"
                    )
                queued_agents.add(agent_id)
            for agent_id in facility.resource_active_agent_ids:
                if agent_id not in self.agents:
                    raise StateInvariantError(
                        f"resource service references unknown Agent {agent_id!r}"
                    )
                if agent_id in active_agents or agent_id in queued_agents:
                    raise StateInvariantError(
                        f"Agent {agent_id!r} appears in multiple facility queues or services"
                    )
                if (
                    self.agents[agent_id].activity is not Activity.IN_SERVICE
                    or self.agents[agent_id].target_id != facility.id
                    or self.agents[agent_id].zone_id != facility.zone_id
                ):
                    raise StateInvariantError(
                        f"resource serviced Agent {agent_id!r} has inconsistent activity, target, "
                        "or zone"
                    )
                active_agents.add(agent_id)

        for agent in self.agents.values():
            if agent.activity is Activity.QUEUED and agent.id not in queued_agents:
                raise StateInvariantError(
                    f"queued Agent {agent.id!r} is missing from every facility queue"
                )
            if agent.activity is Activity.IN_SERVICE and agent.id not in active_agents:
                raise StateInvariantError(
                    f"Agent {agent.id!r} in service is missing from every facility"
                )

        toilet_queue_tasks: dict[tuple[str, str], ActiveTask] = {}
        toilet_service_tasks: dict[tuple[str, str], ActiveTask] = {}
        resource_queue_tasks: dict[tuple[str, str], ActiveTask] = {}
        resource_service_tasks: dict[tuple[str, str], ActiveTask] = {}
        travel_tasks: dict[str, ActiveTask] = {}
        completion_events_by_task: dict[str, list[ScheduledEvent]] = {}
        resource_completion_events_by_task: dict[str, list[ScheduledEvent]] = {}
        travel_events_by_task: dict[str, list[ScheduledEvent]] = {}
        staff_events_by_task: dict[str, list[ScheduledEvent]] = {}
        for event in self.event_queue.pending():
            if event.event_type not in (
                "toilet_service_complete",
                "resource_service_complete",
                "agent_travel_complete",
                "staff_support_complete",
            ):
                continue
            task_id = event.payload.get("task_id")
            if isinstance(task_id, str):
                target = (
                    completion_events_by_task
                    if event.event_type == "toilet_service_complete"
                    else (
                        resource_completion_events_by_task
                        if event.event_type == "resource_service_complete"
                        else (
                            staff_events_by_task
                            if event.event_type == "staff_support_complete"
                            else travel_events_by_task
                        )
                    )
                )
                target.setdefault(task_id, []).append(event)
        for task in self.active_tasks.values():
            if task.kind in (
                "travel_to_toilet",
                "travel_to_resource",
                "travel_return",
                "staff_support_travel",
            ):
                if task.agent_id is None or task.resource_id is not None:
                    raise StateInvariantError(f"travel task {task.id!r} has invalid references")
                if task.start_minute > self.minute_index or task.due_minute <= task.start_minute:
                    raise StateInvariantError(f"travel task {task.id!r} has invalid timing")
                if task.agent_id in travel_tasks:
                    raise StateInvariantError(f"Agent {task.agent_id!r} has duplicate travel tasks")
                travel_tasks[task.agent_id] = task
                continue
            if task.kind in (
                "resource_service_queue",
                "resource_service_water",
                "resource_service_food",
            ):
                if self.schema_version != 6:
                    raise StateInvariantError("resource service tasks require WorldState v6")
                if task.agent_id is None or task.resource_id is None:
                    raise StateInvariantError(
                        f"resource service task {task.id!r} needs Agent and facility IDs"
                    )
                facility = self.facilities.get(task.resource_id)
                if facility is None or facility.kind not in ("water_station", "meal_distribution"):
                    raise StateInvariantError(
                        f"resource service task {task.id!r} references a non-resource facility"
                    )
                if task.start_minute > self.minute_index:
                    raise StateInvariantError(f"resource service task {task.id!r} starts in future")
                if task.kind == "resource_service_queue":
                    if task.due_minute != task.start_minute or task.staff_id is not None:
                        raise StateInvariantError(
                            f"resource queue task {task.id!r} has invalid timing or staff"
                        )
                else:
                    if task.due_minute <= task.start_minute or task.staff_id is None:
                        raise StateInvariantError(
                            f"resource service task {task.id!r} has invalid timing or staff"
                        )
                key = (task.resource_id, task.agent_id)
                target_tasks = (
                    resource_queue_tasks
                    if task.kind == "resource_service_queue"
                    else resource_service_tasks
                )
                if key in target_tasks:
                    raise StateInvariantError(
                        f"Agent {task.agent_id!r} has duplicate resource service tasks"
                    )
                target_tasks[key] = task
                continue
            if task.kind not in ("toilet_queue", "toilet_service"):
                continue
            if task.agent_id is None or task.resource_id is None:
                raise StateInvariantError(f"toilet task {task.id!r} needs Agent and facility IDs")
            facility = self.facilities.get(task.resource_id)
            if facility is None or "toilet" not in facility.kind.casefold():
                raise StateInvariantError(
                    f"toilet task {task.id!r} references a non-toilet facility"
                )
            if task.start_minute > self.minute_index:
                raise StateInvariantError(f"toilet task {task.id!r} starts in the future")
            if task.kind == "toilet_queue" and task.due_minute != task.start_minute:
                raise StateInvariantError(f"toilet queue task {task.id!r} has invalid timing")
            if task.kind == "toilet_service" and task.due_minute <= task.start_minute:
                raise StateInvariantError(f"toilet service task {task.id!r} has invalid timing")
            target_tasks = (
                toilet_queue_tasks if task.kind == "toilet_queue" else toilet_service_tasks
            )
            key = (task.resource_id, task.agent_id)
            if key in target_tasks:
                raise StateInvariantError(f"Agent {task.agent_id!r} has duplicate toilet tasks")
            target_tasks[key] = task

        for facility in self.facilities.values():
            if "toilet" not in facility.kind.casefold():
                continue
            for agent_id in facility.queue_agent_ids:
                task = toilet_queue_tasks.get((facility.id, agent_id))
                if task is None or self.agents[agent_id].target_id != facility.id:
                    raise StateInvariantError(
                        f"queued Agent {agent_id!r} needs one matching toilet queue task"
                    )
            for agent_id in facility.active_agent_ids:
                task = toilet_service_tasks.get((facility.id, agent_id))
                if task is None or self.agents[agent_id].target_id != facility.id:
                    raise StateInvariantError(
                        f"serviced Agent {agent_id!r} needs one matching toilet service task"
                    )
        for (facility_id, agent_id), task in toilet_queue_tasks.items():
            if agent_id not in self.facilities[facility_id].queue_agent_ids:
                raise StateInvariantError(f"toilet queue task {task.id!r} has no queue entry")
            if self.agents[agent_id].activity is not Activity.QUEUED:
                raise StateInvariantError(f"toilet queue task {task.id!r} has a non-queued Agent")
        for (facility_id, agent_id), task in toilet_service_tasks.items():
            if agent_id not in self.facilities[facility_id].active_agent_ids:
                raise StateInvariantError(f"toilet service task {task.id!r} has no occupied slot")
            if self.agents[agent_id].activity is not Activity.IN_SERVICE:
                raise StateInvariantError(
                    f"toilet service task {task.id!r} has a non-serviced Agent"
                )
            completion_events = completion_events_by_task.get(task.id, [])
            if (
                len(completion_events) != 1
                or completion_events[0].kind is not EventKind.COMPLETION
                or completion_events[0].due_minute != task.due_minute
                or completion_events[0].payload.get("facility_id") != facility_id
                or completion_events[0].payload.get("agent_id") != agent_id
                or completion_events[0].payload.get("staff_id", task.staff_id) != task.staff_id
            ):
                raise StateInvariantError(
                    f"toilet service task {task.id!r} needs one matching completion event"
                )
        for facility in self.facilities.values():
            if facility.kind not in ("water_station", "meal_distribution"):
                continue
            for agent_id in facility.resource_queue_agent_ids:
                task = resource_queue_tasks.get((facility.id, agent_id))
                if task is None or self.agents[agent_id].target_id != facility.id:
                    raise StateInvariantError(
                        f"resource queued Agent {agent_id!r} needs one matching queue task"
                    )
            for agent_id in facility.resource_active_agent_ids:
                task = resource_service_tasks.get((facility.id, agent_id))
                if task is None or self.agents[agent_id].target_id != facility.id:
                    raise StateInvariantError(
                        f"resource serviced Agent {agent_id!r} needs one matching service task"
                    )
        for (facility_id, agent_id), task in resource_queue_tasks.items():
            facility = self.facilities[facility_id]
            if agent_id not in facility.resource_queue_agent_ids:
                raise StateInvariantError(
                    f"resource queue task {task.id!r} has no matching queue entry"
                )
            if self.agents[agent_id].activity is not Activity.QUEUED:
                raise StateInvariantError(f"resource queue task {task.id!r} has a non-queued Agent")
        active_resource_reservations: set[str] = set()
        for (facility_id, agent_id), task in resource_service_tasks.items():
            facility = self.facilities[facility_id]
            if agent_id not in facility.resource_active_agent_ids:
                raise StateInvariantError(
                    f"resource service task {task.id!r} has no occupied facility slot"
                )
            if self.agents[agent_id].activity is not Activity.IN_SERVICE:
                raise StateInvariantError(
                    f"resource service task {task.id!r} has a non-serviced Agent"
                )
            completions = resource_completion_events_by_task.get(task.id, [])
            resource_id = "water_ml" if facility.kind == "water_station" else "food_meal_units"
            if (
                len(completions) != 1
                or completions[0].due_minute != task.due_minute
                or completions[0].payload.get("facility_id") != facility_id
                or completions[0].payload.get("agent_id") != agent_id
                or completions[0].payload.get("resource_id") != resource_id
                or completions[0].payload.get("reservation_id") != task.id
                or completions[0].payload.get("staff_id") != task.staff_id
            ):
                raise StateInvariantError(
                    f"resource service task {task.id!r} needs one matching completion event"
                )
            reservation = self.model_state[RESOURCE_LEDGER_KEY]["accounts"][resource_id][
                "reservations"
            ].get(task.id)
            service_config = ResourceServiceConfig.model_validate(
                self.model_configs["resource_service"]
            )
            expected_quantity = (
                service_config.water_ml_per_service
                if resource_id == "water_ml"
                else service_config.food_meal_units_per_service
            )
            if (
                not isinstance(reservation, dict)
                or reservation.get("agent_id") != agent_id
                or reservation.get("resource_id") != resource_id
                or reservation.get("quantity") != expected_quantity
                or completions[0].kind is not EventKind.COMPLETION
                or completions[0].payload.get("quantity") != expected_quantity
            ):
                raise StateInvariantError(
                    f"resource service task {task.id!r} needs one matching ledger reservation"
                )
            active_resource_reservations.add(task.id)
            staff_member = self.staff.get(task.staff_id or "")
            config = ResourceServiceConfig.model_validate(self.model_configs["resource_service"])
            required_skill = (
                config.water_staff_skill if resource_id == "water_ml" else config.meal_staff_skill
            )
            if (
                staff_member is None
                or staff_member.active_task_id != task.id
                or staff_member.work_queue != [task.id]
                or staff_member.busy_until != task.due_minute
                or staff_member.zone_id != facility.zone_id
                or required_skill not in staff_member.skills
            ):
                raise StateInvariantError(
                    f"resource service task {task.id!r} needs one matching idle-skilled staff owner"
                )
        for event in self.event_queue.pending():
            if event.event_type != "resource_service_complete":
                continue
            task_id = event.payload.get("task_id")
            if not isinstance(task_id, str):
                raise StateInvariantError("resource completion event requires a task ID")
            if task_id in self.active_tasks:
                continue
            cancelled_ids = self.model_state["resource_service_v1"]["cancelled_task_ids"]
            if task_id not in cancelled_ids:
                raise StateInvariantError(
                    f"resource completion event references inactive task {task_id!r}"
                )
        if self.schema_version == 6:
            ledger_reservations = {
                reservation_id
                for resource_id in ("water_ml", "food_meal_units")
                for reservation_id in self.model_state[RESOURCE_LEDGER_KEY]["accounts"][
                    resource_id
                ]["reservations"]
            }
            service_reservations = set(
                self.model_state["resource_service_v1"]["active_reservation_ids"]
            )
            if service_reservations != active_resource_reservations:
                raise StateInvariantError(
                    "active service reservations must match active resource tasks"
                )
            if not service_reservations <= ledger_reservations:
                raise StateInvariantError(
                    "active service reservation IDs must exist in the resource ledger"
                )
        for agent_id, task in travel_tasks.items():
            agent = self.agents.get(agent_id)
            if agent is None or agent.activity is not Activity.MOVING:
                raise StateInvariantError(f"travel task {task.id!r} needs a moving Agent")
            matching_events = travel_events_by_task.get(task.id, [])
            if len(matching_events) != 1 or matching_events[0].due_minute != task.due_minute:
                raise StateInvariantError(
                    f"travel task {task.id!r} needs one matching completion event"
                )
            destination = matching_events[0].payload.get("destination_zone_id")
            purpose = matching_events[0].payload.get("purpose")
            target_id = (
                matching_events[0].payload.get("facility_id")
                if purpose in ("toilet_request", "resource_request")
                else destination
            )
            if destination not in self.zones or agent.target_id != target_id:
                raise StateInvariantError(f"travel task {task.id!r} has an invalid destination")
            if task.kind == "staff_support_travel" and (
                purpose != "staff_support_travel"
                or matching_events[0].payload.get("request_id") not in self.support_requests
                or matching_events[0].payload.get("staff_id") not in self.staff
            ):
                raise StateInvariantError(
                    f"staff support travel task {task.id!r} has invalid request ownership"
                )
        for agent in self.agents.values():
            if agent.activity is Activity.MOVING and agent.id not in travel_tasks:
                raise StateInvariantError(
                    f"moving Agent {agent.id!r} is missing a scheduled travel task"
                )

        for staff_member in self.staff.values():
            if staff_member.zone_id not in self.zones:
                raise StateInvariantError(f"staff {staff_member.id!r} references an unknown zone")
            if len(staff_member.work_queue) != len(set(staff_member.work_queue)):
                raise StateInvariantError(f"staff {staff_member.id!r} has duplicate work entries")
            unknown_tasks = set(staff_member.work_queue) - self.active_tasks.keys()
            if unknown_tasks:
                raise StateInvariantError(
                    f"staff queue references unknown task {min(unknown_tasks)!r}"
                )
            repeated_tasks = assigned_tasks.intersection(staff_member.work_queue)
            if repeated_tasks:
                raise StateInvariantError(
                    f"task {min(repeated_tasks)!r} is assigned to multiple staff queues"
                )
            assigned_tasks.update(staff_member.work_queue)
            if (
                not isinstance(staff_member.busy_until, int)
                or isinstance(staff_member.busy_until, bool)
                or staff_member.busy_until < 0
            ):
                raise StateInvariantError(f"staff {staff_member.id!r} has invalid busy_until")
            if staff_member.active_task_id is None:
                if staff_member.work_queue or staff_member.busy_until != 0:
                    raise StateInvariantError(
                        f"idle staff {staff_member.id!r} cannot retain active work"
                    )
            elif staff_member.work_queue != [staff_member.active_task_id]:
                raise StateInvariantError(
                    f"staff {staff_member.id!r} must have exactly one active work item"
                )

        requests_by_agent: dict[str, list[SupportRequestState]] = {}
        for request in self.support_requests.values():
            if not request.id or request.id not in self.support_requests:
                raise StateInvariantError("support request mapping key must match its stable ID")
            if request.agent_id not in self.agents:
                raise StateInvariantError(
                    f"support request {request.id!r} references an unknown Agent"
                )
            if (
                not 0 <= request.requested_minute <= self.minute_index
                or request.timeout_minute < request.requested_minute
                or request.cooldown_until < 0
            ):
                raise StateInvariantError(f"support request {request.id!r} has invalid timing")
            if request.last_assignment_attempt_minute is not None and (
                type(request.last_assignment_attempt_minute) is not int
                or not request.requested_minute
                <= request.last_assignment_attempt_minute
                <= self.minute_index
            ):
                raise StateInvariantError(
                    f"support request {request.id!r} has invalid assignment-attempt timing"
                )
            assignment_reason = request.last_assignment_wait_reason_code
            if assignment_reason is not None and (
                not isinstance(assignment_reason, str)
                or assignment_reason
                not in {
                    "no_available_qualified_staff",
                    "route_unavailable_at_assignment",
                    "insufficient_run_time",
                    "resident_travel_schedule_rejected",
                }
                or request.last_assignment_attempt_minute is None
            ):
                raise StateInvariantError(
                    f"support request {request.id!r} has invalid assignment-wait reason"
                )
            try:
                request.status = SupportRequestStatus(request.status)
            except (TypeError, ValueError) as exc:
                raise StateInvariantError(
                    f"support request {request.id!r} has invalid status"
                ) from exc
            requests_by_agent.setdefault(request.agent_id, []).append(request)
            if request.status is SupportRequestStatus.ASSIGNED:
                staff_member = self.staff.get(request.assigned_staff_id or "")
                task = self.active_tasks.get(request.task_id or "")
                agent = self.agents[request.agent_id]
                if (
                    staff_member is None
                    or task is None
                    or task.kind != "staff_support"
                    or task.staff_id != staff_member.id
                    or task.agent_id != request.agent_id
                    or staff_member.active_task_id != task.id
                    or task.due_minute != staff_member.busy_until
                ):
                    raise StateInvariantError(
                        f"assigned support request {request.id!r} lacks one matching staff task"
                    )
                travel_task = travel_tasks.get(request.agent_id)
                if agent.activity is Activity.MOVING:
                    travel_events = (
                        travel_events_by_task.get(travel_task.id, [])
                        if travel_task is not None
                        else []
                    )
                    if (
                        travel_task is None
                        or travel_task.kind != "staff_support_travel"
                        or len(travel_events) != 1
                        or travel_events[0].payload.get("request_id") != request.id
                        or travel_events[0].payload.get("staff_id") != staff_member.id
                        or travel_events[0].payload.get("destination_zone_id")
                        != staff_member.zone_id
                    ):
                        raise StateInvariantError(
                            f"assigned support request {request.id!r} has invalid resident travel"
                        )
                elif agent.activity is not Activity.SEEKING_HELP or (
                    travel_task is not None and travel_task.kind == "staff_support_travel"
                ):
                    raise StateInvariantError(
                        f"assigned support request {request.id!r} must seek help or travel to staff"
                    )
                events = staff_events_by_task.get(request.task_id or "", [])
                if (
                    len(events) != 1
                    or events[0].due_minute != task.due_minute
                    or events[0].payload.get("request_id") != request.id
                    or events[0].payload.get("agent_id") != request.agent_id
                    or events[0].payload.get("staff_id") != staff_member.id
                ):
                    raise StateInvariantError(
                        f"assigned support request {request.id!r} needs one completion event"
                    )
            elif request.assigned_staff_id is not None or request.task_id is not None:
                if request.status not in (
                    SupportRequestStatus.RESOLVED,
                    SupportRequestStatus.UNRESOLVED,
                ):
                    raise StateInvariantError(
                        f"support request {request.id!r} has assignment without assigned status"
                    )
            if request.status is SupportRequestStatus.PENDING:
                expirations = [
                    event
                    for event in self.event_queue.pending()
                    if event.event_type == "support_request_timeout"
                    and event.payload.get("request_id") == request.id
                ]
                if request.timeout_minute < self.duration_minutes and (
                    len(expirations) != 1 or expirations[0].due_minute != request.timeout_minute
                ):
                    raise StateInvariantError(
                        f"pending support request {request.id!r} needs one timeout event"
                    )

        for agent in self.agents.values():
            active_requests = [
                item
                for item in requests_by_agent.get(agent.id, [])
                if item.status in (SupportRequestStatus.PENDING, SupportRequestStatus.ASSIGNED)
            ]
            if len(active_requests) > 1:
                raise StateInvariantError(
                    f"Agent {agent.id!r} has duplicate active support requests"
                )
            if active_requests:
                if agent.support_request_id != active_requests[0].id:
                    raise StateInvariantError(
                        f"Agent {agent.id!r} does not reference its active support request"
                    )
                if agent.activity not in (Activity.SEEKING_HELP, Activity.MOVING):
                    raise StateInvariantError(
                        f"Agent {agent.id!r} with active support must seek help or travel to staff"
                    )
            elif agent.support_request_id is not None:
                raise StateInvariantError(
                    f"Agent {agent.id!r} references a non-active support request"
                )

        for task in self.active_tasks.values():
            if task.agent_id is not None and task.agent_id not in self.agents:
                raise StateInvariantError(f"task {task.id!r} references an unknown Agent")
            if task.resource_id is not None and task.resource_id not in self.facilities:
                if task.kind != "staff_support":
                    raise StateInvariantError(f"task {task.id!r} references an unknown facility")
            if task.kind == "staff_support":
                staff_member = self.staff.get(task.staff_id or "")
                if (
                    task.agent_id is None
                    or staff_member is None
                    or staff_member.active_task_id != task.id
                    or staff_member.busy_until != task.due_minute
                    or task.resource_id is not None
                    or task.due_minute <= task.start_minute
                    or len(staff_events_by_task.get(task.id, [])) != 1
                    or staff_events_by_task[task.id][0].due_minute != task.due_minute
                ):
                    raise StateInvariantError(f"staff support task {task.id!r} is inconsistent")
            if task.start_minute < 0 or task.due_minute < task.start_minute:
                raise StateInvariantError(f"task {task.id!r} has invalid timing")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.resources.values()
        ):
            raise StateInvariantError("resource inventory must contain non-negative integers")
        if RESOURCE_LEDGER_KEY in self.model_state:
            try:
                population_state = self.model_state.get("population_dynamics_v1", {})
                archived_agent_ids: set[str] = set()
                if population_state:
                    if (
                        not isinstance(population_state, dict)
                        or population_state.get("schema_version") != 1
                        or not isinstance(population_state.get("initial_agent_ids"), list)
                        or not isinstance(population_state.get("profile_traits"), dict)
                        or not isinstance(population_state.get("pending_arrivals"), list)
                        or not isinstance(population_state.get("arrivals"), list)
                    ):
                        raise ValueError("population_dynamics_v1 identity history is invalid")
                    historical_ids = set(population_state["initial_agent_ids"])
                    historical_ids.update(
                        item.get("profile", {}).get("id")
                        for item in population_state["pending_arrivals"]
                        if isinstance(item, dict) and isinstance(item.get("profile"), dict)
                    )
                    historical_ids.update(
                        item.get("agent_id")
                        for item in population_state["arrivals"]
                        if isinstance(item, dict) and isinstance(item.get("agent_id"), str)
                    )
                    profile_ids = set(population_state["profile_traits"])
                    if not profile_ids <= historical_ids:
                        raise ValueError("population_dynamics_v1 contains an unknown profile ID")
                    archived_agent_ids = set(population_state["initial_agent_ids"]) | {
                        item["agent_id"]
                        for item in population_state["arrivals"]
                        if isinstance(item, dict) and isinstance(item.get("agent_id"), str)
                    }
                ledger_arguments = (
                    self.model_state[RESOURCE_LEDGER_KEY],
                    self.resources,
                    set(self.agents) | archived_agent_ids,
                    set(self.zones),
                )
                if validate_event_history:
                    validate_resource_ledger(*ledger_arguments)
                else:
                    validate_resource_ledger_state(
                        *ledger_arguments, minute_index=self.minute_index
                    )
            except ValueError as exc:
                raise StateInvariantError(f"invalid resource ledger: {exc}") from exc
        if any(value < 0 for value in self.id_counters.values()):
            raise StateInvariantError("ID counters cannot be negative")
        if not isinstance(self.event_queue, EventQueue) or not isinstance(self.event_log, EventLog):
            raise StateInvariantError("event queue and event log must use typed event containers")
        try:
            self.event_queue.validate()
            if validate_event_history:
                # The audit shows the log's ID and sequence indexes hold its records,
                # so the checks below may read them.
                self.event_log.validate(
                    minute_index=self.minute_index, duration_minutes=self.duration_minutes
                )
        except ValueError as exc:
            raise StateInvariantError(str(exc)) from exc
        queued_events = self.event_queue.pending()
        queue_ids = {event.event_id for event in queued_events}
        queue_sequences = {event.sequence for event in queued_events}
        if any(self.event_log.contains_id(event_id) for event_id in queue_ids):
            raise StateInvariantError("an event cannot be both pending and realized")
        if any(self.event_log.contains_sequence(sequence) for sequence in queue_sequences):
            raise StateInvariantError("event sequences must be unique across queue and log")
        max_realized_sequence = self.event_log.max_sequence
        for event in queued_events:
            if event.due_minute >= self.duration_minutes:
                raise StateInvariantError(f"event {event.event_id!r} is outside the scenario")
        max_queued_sequence = max((event.sequence for event in queued_events), default=-1)
        if self.id_counters.get("event", 0) <= max(max_queued_sequence, max_realized_sequence):
            raise StateInvariantError("event ID counter must be greater than scheduled sequences")

    def allocate_event_sequence(self) -> int:
        """Reserve the next stable sequence number for a newly generated event."""
        sequence = self.id_counters.get("event", 0)
        self.id_counters["event"] = sequence + 1
        return sequence

    def schedule_event(self, event: ScheduledEvent) -> None:
        """Add a future event while preserving global event identity invariants."""
        if event.due_minute >= self.duration_minutes:
            raise ValueError(f"event {event.event_id!r} is outside the scenario")
        if self.event_log.contains_id(event.event_id):
            raise DuplicateEventError(f"event {event.event_id!r} already realized")
        if self.event_log.contains_sequence(event.sequence):
            raise DuplicateEventError(f"event sequence {event.sequence} already realized")
        self.event_queue.schedule(event)
        self.id_counters["event"] = max(self.id_counters.get("event", 0), event.sequence + 1)

    def canonical_json(self) -> str:
        """Serialize the complete current state deterministically as compact JSON."""
        self.validate()
        return self._canonical_text(self._canonical_data())

    def _canonical_data(self, *, include_event_log: bool = True) -> dict[str, Any]:
        """The canonical_json() document before encoding; it shares live model state.

        Without the EventLog its "event_log" is empty, for a caller that adds
        records it already converted.
        """
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "duration_minutes": self.duration_minutes,
            "minute_index": self.minute_index,
            "agents": [
                self._agent_data(agent)
                for agent in sorted(self.agents.values(), key=lambda item: item.profile.id)
            ],
            "zones": [
                asdict(zone) for zone in sorted(self.zones.values(), key=lambda item: item.id)
            ],
            "zone_edges": [
                asdict(edge) for edge in sorted(self.zone_edges.values(), key=lambda item: item.id)
            ],
            "facilities": [
                self._facility_data(facility)
                for facility in sorted(self.facilities.values(), key=lambda item: item.id)
            ],
            "staff": [
                asdict(person) for person in sorted(self.staff.values(), key=lambda item: item.id)
            ],
            "support_requests": [
                self._support_request_data(request)
                for request in sorted(self.support_requests.values(), key=lambda item: item.id)
            ],
            "resources": self.resources,
            "active_tasks": [
                asdict(task)
                for task in sorted(self.active_tasks.values(), key=lambda item: item.id)
            ],
            "event_queue": [event.to_dict() for event in self.event_queue.pending()],
            "event_log": (
                [record.to_dict() for record in self.event_log.records] if include_event_log else []
            ),
            "rng_state": self.rng_state,
            "id_counters": self.id_counters,
            "metric_state": self.metric_state,
            "model_configs": self.model_configs,
            "model_state": self.model_state,
        }

    @classmethod
    def _canonical_text(cls, data: Any) -> str:
        return json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=cls._json_default,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible snapshot of the whole world."""
        return json.loads(self.canonical_json())

    def state_digest(self) -> str:
        """The state_hash a Checkpoint of this world holds, from a validated world."""
        from sheltersim.core.checkpoint_cache import CheckpointHistoryCache

        self.validate()
        document, _, encoder = CheckpointHistoryCache().documents(self, None)
        return encoder.world_tree_digest(document)

    @classmethod
    def from_dict(
        cls,
        document: dict[str, Any],
        *,
        _restored_events: Sequence[EventLogRecord] = (),
        _shared: Sequence[Any] = (),
        _validate: bool = True,
        _saved_json: bool = False,
        _borrow: bool = False,
        _restored_log: EventLog | None = None,
    ) -> WorldState:
        """Restore the exact current schema; older or unknown schemas fail closed.

        The private arguments are for the restore check of Checkpoint.capture only:
        _restored_events are records restored before from the same leading
        event_log documents, which are not parsed again, and the new world may keep
        the _shared model_state lists instead of copies. Without _validate the caller
        validates the world, or shows it equals one validated already. With
        _saved_json the document was read from hash-checked canonical JSON, so event
        payloads are not encoded again to check they are JSON (ScheduledEvent.from_saved).
        With _borrow the new world keeps the document's objects instead of copies; only
        a caller that reads the world and discards it, or that owns a new document,
        may pass it. _restored_log is
        such a discarded world's EventLog of the leading event_log documents; the new
        records are appended to it, keeping its indexes, instead of building a new one.

        An event_log read back as item bytes (SavedItems) builds the EventLog from
        them; each record is made when read (EventLog.from_saved).
        """
        if not isinstance(document, dict):
            raise StateInvariantError("WorldState document must be an object")
        expected_fields = {
            "schema_version",
            "scenario_id",
            "seed",
            "duration_minutes",
            "minute_index",
            "agents",
            "zones",
            "zone_edges",
            "facilities",
            "staff",
            "support_requests",
            "resources",
            "active_tasks",
            "event_queue",
            "event_log",
            "rng_state",
            "id_counters",
            "metric_state",
            "model_configs",
            "model_state",
        }
        if set(document) != expected_fields:
            raise StateInvariantError("WorldState fields do not match the supported schema")
        version = document.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool) or version not in (5, 6):
            raise StateInvariantError(f"unsupported world-state schema version: {version!r}")
        try:
            agents: dict[str, AgentState] = {}
            need_fields = {"toilet_urgency", "hunger", "fatigue"}
            if version == 6:
                need_fields.add("hydration_need")
            for item in document["agents"]:
                if set(item) != {
                    "profile",
                    "zone_id",
                    "activity",
                    "target_id",
                    "needs",
                    "stress",
                    "trust",
                    "next_decision_at",
                    "support_request_id",
                    "support_cooldown_until",
                }:
                    raise StateInvariantError(
                        f"Agent state fields do not match WorldState v{version}"
                    )
                profile_data = item["profile"]
                if set(profile_data) != {
                    "id",
                    "group_id",
                    "initial_zone_id",
                    "mobility",
                    "support_need",
                    "stress_sensitivity",
                    "rng_key",
                    "initial_needs",
                    "provenance",
                }:
                    raise StateInvariantError(
                        f"Agent profile fields do not match WorldState v{version}"
                    )
                if (
                    not isinstance(profile_data["initial_needs"], dict)
                    or set(profile_data["initial_needs"]) != need_fields
                ):
                    raise StateInvariantError(
                        f"profile initial needs do not match WorldState v{version}"
                    )
                if not isinstance(item["needs"], dict) or set(item["needs"]) != need_fields:
                    raise StateInvariantError(f"Agent needs do not match WorldState v{version}")
                profile = AgentProfile(
                    id=profile_data["id"],
                    group_id=profile_data["group_id"],
                    initial_zone_id=profile_data["initial_zone_id"],
                    mobility=Mobility(profile_data["mobility"]),
                    support_need=profile_data["support_need"],
                    stress_sensitivity=profile_data["stress_sensitivity"],
                    rng_key=profile_data["rng_key"],
                    initial_needs=InitialNeedProfile(**profile_data["initial_needs"]),
                    provenance=deepcopy(profile_data["provenance"]),
                )
                agent = AgentState(
                    profile=profile,
                    zone_id=item["zone_id"],
                    activity=Activity(item["activity"]),
                    target_id=item["target_id"],
                    needs=NeedState(**item["needs"]),
                    stress=item["stress"],
                    trust=item["trust"],
                    next_decision_at=item["next_decision_at"],
                    support_request_id=item["support_request_id"],
                    support_cooldown_until=item["support_cooldown_until"],
                )
                agents[agent.id] = agent
            zones = {item["id"]: ZoneState(**item) for item in document["zones"]}
            zone_edges = {item["id"]: ZoneEdgeState(**item) for item in document["zone_edges"]}
            facility_fields = {
                "id",
                "kind",
                "zone_id",
                "capacity",
                "working_units",
                "active_agent_ids",
                "queue_agent_ids",
            }
            if version == 6:
                facility_fields.update({"resource_active_agent_ids", "resource_queue_agent_ids"})
            for item in document["facilities"]:
                if not isinstance(item, dict) or set(item) != facility_fields:
                    raise StateInvariantError(f"facility fields do not match WorldState v{version}")
            facilities = {item["id"]: FacilityState(**item) for item in document["facilities"]}
            staff = {
                item["id"]: StaffState(
                    **{
                        **item,
                        "skills": tuple(item["skills"]),
                        "work_queue": list(item["work_queue"]),
                    }
                )
                for item in document["staff"]
            }
            support_requests = {
                item["id"]: SupportRequestState(**item) for item in document["support_requests"]
            }
            active_tasks = {item["id"]: ActiveTask(**item) for item in document["active_tasks"]}

            def copy(value: Any, memo: dict[int, Any] | None = None) -> Any:
                return value if _borrow else deepcopy(value, memo)

            event_from = ScheduledEvent.from_saved if _saved_json else _event_from_document
            queue = EventQueue([event_from(item) for item in document["event_queue"]])
            leading = _restored_log if _restored_log is not None else _restored_events
            if len(leading) > len(document["event_log"]):
                raise StateInvariantError("restored EventLog records exceed the document")
            saved = document["event_log"]
            if isinstance(saved, SavedItems):
                log = _restored_log if _restored_log is not None else EventLog(_restored_events)
                log.extend_saved(saved.raws[len(log) :])
            else:
                new_records = (
                    EventLogRecord(
                        event=event_from(item["event"]),
                        realized_minute=item["realized_minute"],
                        status=EventStatus(item["status"]),
                        detail=item["detail"],
                    )
                    for item in saved[len(leading) :]
                )
                if _restored_log is not None:
                    log = _restored_log
                    for record in new_records:
                        log.append(record)
                else:
                    log = EventLog([*_restored_events, *new_records])
            world = cls(
                scenario_id=document["scenario_id"],
                seed=document["seed"],
                duration_minutes=document["duration_minutes"],
                minute_index=document["minute_index"],
                agents=agents,
                zones=zones,
                zone_edges=zone_edges,
                facilities=facilities,
                staff=staff,
                support_requests=support_requests,
                resources=copy(document["resources"]),
                active_tasks=active_tasks,
                event_queue=queue,
                event_log=log,
                rng_state=copy(document["rng_state"]),
                id_counters=copy(document["id_counters"]),
                metric_state=copy(document["metric_state"]),
                model_configs=copy(document["model_configs"]),
                model_state=copy(document["model_state"], {id(item): item for item in _shared}),
                schema_version=document["schema_version"],
            )
            if _validate:
                world.validate()
            return world
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StateInvariantError):
                raise
            raise StateInvariantError(f"invalid WorldState document: {exc}") from exc

    def same_state(self, other: WorldState, *, known_equal_events: int = 0) -> bool:
        """Whether two worlds hold equal state, so they pass the same validation.

        The first known_equal_events EventLog records of both worlds are already known
        to be equal and are not compared. Pending events compare in queue order.
        """
        for item in fields(self):
            if item.name in {"event_queue", "event_log"}:
                continue
            if getattr(self, item.name) != getattr(other, item.name):
                return False
        mine, theirs = self.event_log, other.event_log
        return (
            self.event_queue.pending() == other.event_queue.pending()
            and len(mine) == len(theirs)
            and mine.records_from(known_equal_events) == theirs.records_from(known_equal_events)
        )

    @classmethod
    def from_json(cls, value: str) -> WorldState:
        """Parse and restore a canonical JSON WorldState document."""
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StateInvariantError(f"invalid WorldState JSON: {exc}") from exc
        return cls.from_dict(document)

    def _agent_data(self, agent: AgentState) -> dict[str, Any]:
        initial_needs = {
            "toilet_urgency": agent.profile.initial_needs.toilet_urgency,
            "hunger": agent.profile.initial_needs.hunger,
            "fatigue": agent.profile.initial_needs.fatigue,
        }
        needs = {
            "toilet_urgency": agent.needs.toilet_urgency,
            "hunger": agent.needs.hunger,
            "fatigue": agent.needs.fatigue,
        }
        if self.schema_version == 6:
            initial_needs["hydration_need"] = agent.profile.initial_needs.hydration_need
            needs["hydration_need"] = agent.needs.hydration_need
        return {
            "profile": {
                "id": agent.profile.id,
                "group_id": agent.profile.group_id,
                "initial_zone_id": agent.profile.initial_zone_id,
                "mobility": agent.profile.mobility.value,
                "support_need": agent.profile.support_need,
                "stress_sensitivity": agent.profile.stress_sensitivity,
                "rng_key": agent.profile.rng_key,
                "initial_needs": initial_needs,
                "provenance": agent.profile.provenance,
            },
            "zone_id": agent.zone_id,
            "activity": agent.activity.value,
            "target_id": agent.target_id,
            "needs": needs,
            "stress": agent.stress,
            "trust": agent.trust,
            "next_decision_at": agent.next_decision_at,
            "support_request_id": agent.support_request_id,
            "support_cooldown_until": agent.support_cooldown_until,
        }

    def _facility_data(self, facility: FacilityState) -> dict[str, Any]:
        data = {
            "id": facility.id,
            "kind": facility.kind,
            "zone_id": facility.zone_id,
            "capacity": facility.capacity,
            "working_units": facility.working_units,
            "active_agent_ids": facility.active_agent_ids,
            "queue_agent_ids": facility.queue_agent_ids,
        }
        if self.schema_version == 6:
            data["resource_active_agent_ids"] = facility.resource_active_agent_ids
            data["resource_queue_agent_ids"] = facility.resource_queue_agent_ids
        return data

    @staticmethod
    def _support_request_data(request: SupportRequestState) -> dict[str, Any]:
        data = {
            "id": request.id,
            "agent_id": request.agent_id,
            "requested_minute": request.requested_minute,
            "timeout_minute": request.timeout_minute,
            "status": request.status.value,
            "assigned_staff_id": request.assigned_staff_id,
            "task_id": request.task_id,
            "resolved_minute": request.resolved_minute,
            "cooldown_until": request.cooldown_until,
        }
        # Keep the canonical encoding of historical Checkpoints unchanged when
        # this additive diagnostic was never recorded.
        if request.last_assignment_attempt_minute is not None:
            data["last_assignment_attempt_minute"] = request.last_assignment_attempt_minute
        if request.last_assignment_wait_reason_code is not None:
            data["last_assignment_wait_reason_code"] = request.last_assignment_wait_reason_code
        return data

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, set):
            return sorted(value)
        raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")
