"""Versioned scenario input contract and canonical JSON I/O."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

from sheltersim.contracts.extensions import validate_scenario_extensions
from sheltersim.contracts.policies import selected_models
from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
from sheltersim.domain.infrastructure import (
    INFRASTRUCTURE_COMPLETION_EVENT_TYPES,
    INFRASTRUCTURE_MAX_ROUTE_SHIPMENTS,
    INFRASTRUCTURE_MAX_START_EVENTS,
    INFRASTRUCTURE_START_EVENT_TYPES,
    ScenarioInfrastructureSpec,
    dependent_facilities,
)
from sheltersim.domain.models import InformationSpreadConfig, Mobility
from sheltersim.events.models import EventKind

StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

_BEHAVIOR_POLICY_SOURCE_IDS = {
    "utility-softmax-v1": "ASM-UTILITY-BEHAVIOR-V1",
    "priority-rule-v1": "ASM-PRIORITY-RULE-BEHAVIOR-V1",
}
RESOURCE_SERVICE_POLICY_ID = "resource-service-v1"
TOILET_SERVICE_SOURCE_ID = "ASM-TOILET-SERVICE-DURATION-V1"

_PSYCHOLOGY_AGENT_RATE_FIELDS = (
    "toilet_growth_by_agent",
    "hunger_growth_by_agent",
    "awake_fatigue_growth_by_agent",
)


class ContractModel(BaseModel):
    """Base for versioned input models with strict field names."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ZoneSpec(ContractModel):
    id: StableId
    name: str
    capacity: int | None = Field(default=None, ge=0)
    display_x: float | None = Field(default=None, allow_inf_nan=False)
    display_y: float | None = Field(default=None, allow_inf_nan=False)


class ZoneEdgeSpec(ContractModel):
    id: StableId
    from_zone_id: StableId
    to_zone_id: StableId
    travel_minutes: int = Field(gt=0)
    closed: bool = False


class FacilitySpec(ContractModel):
    id: StableId
    kind: str
    zone_id: StableId
    capacity: int = Field(ge=0)
    working_units: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def working_capacity_fits_total(self) -> FacilitySpec:
        if self.working_units is not None and self.working_units > self.capacity:
            raise ValueError("working_units cannot exceed facility capacity")
        return self


class StaffSpec(ContractModel):
    id: StableId
    zone_id: StableId
    skills: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def skills_are_unique(self) -> StaffSpec:
        if len(self.skills) != len(set(self.skills)):
            raise ValueError(f"staff {self.id!r} has duplicate skills")
        return self


class ProfileProvenance(ContractModel):
    source_kind: Literal["synthetic", "human", "llm", "assumption"] = "synthetic"
    generator: str = "manual"
    seed: int | None = Field(default=None, ge=0)


class InitialNeedsSpec(ContractModel):
    """Normalized synthetic initial need levels for an Agent profile."""

    toilet_urgency: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    hunger: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    fatigue: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)


class InitialStockSpec(ContractModel):
    """Opt-in integer starting inventory for the versioned ResourceLedger."""

    schema_version: Literal[1]
    warehouse_id: StableId
    zone_id: StableId | None = None
    water_ml: int = Field(default=0, ge=0)
    food_meal_units: int = Field(default=0, ge=0)
    source_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_sources(self) -> InitialStockSpec:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("initial stock source_ids must be unique")
        if (self.water_ml > 0 or self.food_meal_units > 0) and not self.source_ids:
            raise ValueError("nonzero initial stock requires at least one source_id")
        return self


class ResourceServiceConfig(ContractModel):
    """Experimental, opt-in coefficients for staffed water and meal service."""

    schema_version: Literal[1]
    policy_version: Literal["resource-service-v1"]
    source_id: StableId
    source_type: Literal["assumption"]
    service_minutes: int = Field(gt=0, le=1440)
    water_ml_per_service: int = Field(gt=0, le=100_000)
    food_meal_units_per_service: int = Field(gt=0, le=100)
    hydration_growth_per_minute: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    hydration_action_threshold: float = Field(default=0.25, ge=0.0, le=1.0, allow_inf_nan=False)
    hunger_action_threshold: float = Field(default=0.7, ge=0.0, le=1.0, allow_inf_nan=False)
    hydration_relief: float = Field(default=0.35, gt=0.0, le=1.0, allow_inf_nan=False)
    hunger_relief: float = Field(default=0.5, gt=0.0, le=1.0, allow_inf_nan=False)
    water_utility_base: float = Field(default=0.18, ge=0.0, le=10.0, allow_inf_nan=False)
    water_utility_need_weight: float = Field(default=1.0, ge=0.0, le=10.0, allow_inf_nan=False)
    meal_utility_base: float = Field(default=0.18, ge=0.0, le=10.0, allow_inf_nan=False)
    meal_utility_need_weight: float = Field(default=0.85, ge=0.0, le=10.0, allow_inf_nan=False)
    water_staff_skill: StableId = "water_service"
    meal_staff_skill: StableId = "meal_service"


class ToiletServiceConfig(ContractModel):
    """Versioned, source-attributed discrete toilet service duration bounds."""

    duration_distribution: Literal["discrete_uniform_inclusive"] = (
        "discrete_uniform_inclusive"
    )
    minimum_minutes: int = Field(default=4, strict=True, ge=1, le=1440)
    maximum_minutes: int = Field(default=8, strict=True, ge=1, le=1440)
    source_type: Literal["assumption"] = "assumption"
    source_id: StableId = TOILET_SERVICE_SOURCE_ID

    @model_validator(mode="after")
    def ordered_duration_bounds(self) -> ToiletServiceConfig:
        if self.maximum_minutes < self.minimum_minutes:
            raise ValueError("toilet service maximum_minutes must be at least minimum_minutes")
        return self


class PsychologyModelConfig(ContractModel):
    """Scenario coefficients for the uncalibrated need and stress model."""

    schema_version: Literal[1] = 1
    policy_version: Literal["causal-stress-v1"] = "causal-stress-v1"
    source_type: Literal["assumption"] = "assumption"
    source_id: Literal["ASM-H04-CAUSAL-STRESS-V1"] = "ASM-H04-CAUSAL-STRESS-V1"
    toilet_growth: float = Field(default=0.001, ge=0.0, le=0.25, allow_inf_nan=False)
    hunger_growth: float = Field(default=0.0005, ge=0.0, le=0.25, allow_inf_nan=False)
    awake_fatigue_growth: float = Field(default=0.0008, ge=0.0, le=0.25, allow_inf_nan=False)
    resting_fatigue_recovery: float = Field(default=0.004, ge=0.0, le=0.25, allow_inf_nan=False)
    sleeping_fatigue_recovery: float = Field(default=0.010, ge=0.0, le=0.25, allow_inf_nan=False)
    stress_pressure: float = Field(default=0.012, ge=0.0, le=0.25, allow_inf_nan=False)
    queued_stress: float = Field(default=0.002, ge=0.0, le=0.25, allow_inf_nan=False)
    support_wait_stress: float = Field(default=0.001, ge=0.0, le=0.25, allow_inf_nan=False)
    resting_stress_recovery: float = Field(default=0.003, ge=0.0, le=0.25, allow_inf_nan=False)
    sleeping_stress_recovery: float = Field(default=0.006, ge=0.0, le=0.25, allow_inf_nan=False)
    need_pressure_threshold: float = Field(default=0.35, ge=0.0, le=1.0, allow_inf_nan=False)
    toilet_growth_by_agent: dict[StableId, StrictFloat | StrictInt] = Field(
        default_factory=dict
    )
    hunger_growth_by_agent: dict[StableId, StrictFloat | StrictInt] = Field(
        default_factory=dict
    )
    awake_fatigue_growth_by_agent: dict[StableId, StrictFloat | StrictInt] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def bounded_agent_rates(self) -> PsychologyModelConfig:
        for field_name in _PSYCHOLOGY_AGENT_RATE_FIELDS:
            values = getattr(self, field_name)
            if len(values) > 10_000 or any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 0.25
                for value in values.values()
            ):
                raise ValueError(f"{field_name} must contain at most 10000 finite rates in [0, 0.25]")
        return self

    def to_model_config(self) -> dict[str, Any]:
        """Serialize without empty additive maps to preserve existing Scenario hashes."""
        values = self.model_dump(mode="json")
        for field_name in _PSYCHOLOGY_AGENT_RATE_FIELDS:
            if not values[field_name]:
                values.pop(field_name)
        return values

    def coefficient_values(self) -> dict[str, Any]:
        """Return only the coefficient fields consumed by PsychologyParameters."""
        values = self.model_dump(
            mode="python",
            exclude={"schema_version", "policy_version", "source_type", "source_id"},
        )
        for field_name in _PSYCHOLOGY_AGENT_RATE_FIELDS:
            if not values[field_name]:
                values.pop(field_name)
        return values


class ProfileSpec(ContractModel):
    schema_version: Literal[2] = 2
    id: StableId
    group_id: StableId | None = None
    initial_zone_id: StableId
    mobility: Mobility = Mobility.INDEPENDENT
    support_need: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    stress_sensitivity: float = Field(default=0.5, ge=0.0, le=1.0, allow_inf_nan=False)
    initial_needs: InitialNeedsSpec = Field(default_factory=InitialNeedsSpec)
    rng_key: StableId
    provenance: ProfileProvenance = Field(default_factory=ProfileProvenance)


class PlannedEventSpec(ContractModel):
    id: StableId
    kind: EventKind = EventKind.EXOGENOUS
    due_minute: int = Field(ge=0)
    priority: int = 0
    sequence: int = Field(ge=0)
    event_type: StableId
    payload: dict[str, Any] = Field(default_factory=dict)
    source_id: StableId | None = None
    causation_id: StableId | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class ScenarioSpec(ContractModel):
    """Validated scenario input; mutable simulation state lives in WorldState."""

    schema_version: Literal[3]
    id: StableId
    duration_minutes: int = Field(gt=0)
    seed: int = Field(ge=0)
    zones: tuple[ZoneSpec, ...]
    zone_edges: tuple[ZoneEdgeSpec, ...] = ()
    facilities: tuple[FacilitySpec, ...] = ()
    staff: tuple[StaffSpec, ...] = ()
    profiles: tuple[ProfileSpec, ...]
    scheduled_events: tuple[PlannedEventSpec, ...] = ()
    initial_stock: InitialStockSpec | None = None
    model_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references_and_ids(self) -> ScenarioSpec:
        self._ensure_unique("zones", (item.id for item in self.zones))
        self._ensure_unique("zone_edges", (item.id for item in self.zone_edges))
        self._ensure_unique("facilities", (item.id for item in self.facilities))
        self._ensure_unique("staff", (item.id for item in self.staff))
        self._ensure_unique("profiles", (item.id for item in self.profiles))
        self._ensure_unique("scheduled_events", (item.id for item in self.scheduled_events))
        self._ensure_unique(
            "scheduled event sequences", (item.sequence for item in self.scheduled_events)
        )

        zone_ids = {item.id for item in self.zones}
        if not zone_ids:
            raise ValueError("a scenario must define at least one zone")
        if self.initial_stock is not None and self.initial_stock.zone_id is not None:
            if self.initial_stock.zone_id not in zone_ids:
                raise ValueError(
                    f"initial stock references unknown zone {self.initial_stock.zone_id!r}"
                )
        for edge in self.zone_edges:
            if edge.from_zone_id not in zone_ids or edge.to_zone_id not in zone_ids:
                raise ValueError(f"zone edge {edge.id!r} references an unknown zone")
            if edge.from_zone_id == edge.to_zone_id:
                raise ValueError(f"zone edge {edge.id!r} must connect two different zones")
        for facility in self.facilities:
            if facility.zone_id not in zone_ids:
                raise ValueError(
                    f"facility {facility.id!r} references unknown zone {facility.zone_id!r}"
                )
        for staff_member in self.staff:
            if staff_member.zone_id not in zone_ids:
                raise ValueError(
                    f"staff {staff_member.id!r} references unknown zone {staff_member.zone_id!r}"
                )
        for profile in self.profiles:
            if profile.initial_zone_id not in zone_ids:
                raise ValueError(
                    f"profile {profile.id!r} references unknown zone {profile.initial_zone_id!r}"
                )
        information_config = InformationSpreadConfig.from_mapping(
            self.model_configs.get("information_spread")
        )
        behavior_config = self.model_configs.get("behavior", {})
        if not isinstance(behavior_config, dict):
            raise ValueError("behavior model config must be an object")
        behavior_variant = behavior_config.get("policy", "utility-softmax-v1")
        if (
            not isinstance(behavior_variant, str)
            or behavior_variant not in _BEHAVIOR_POLICY_SOURCE_IDS
        ):
            raise ValueError(f"unsupported behavior policy variant {behavior_variant!r}")
        behavior_source_id = behavior_config.get("source_id")
        if (
            behavior_source_id is not None
            and behavior_source_id != _BEHAVIOR_POLICY_SOURCE_IDS[behavior_variant]
        ):
            raise ValueError("behavior policy source_id must match its registered variant")
        zone_environment_config: ScenarioZoneEnvironmentConfig | None = None
        if "zone_environment" in self.model_configs:
            try:
                zone_environment_config = ScenarioZoneEnvironmentConfig.model_validate(
                    self.model_configs["zone_environment"]
                )
            except ValueError as exc:
                raise ValueError(f"invalid zone_environment config: {exc}") from exc
        infrastructure_config: ScenarioInfrastructureSpec | None = None
        if "infrastructure" in self.model_configs:
            try:
                infrastructure_config = ScenarioInfrastructureSpec.model_validate(
                    self.model_configs["infrastructure"]
                )
            except ValueError as exc:
                raise ValueError(f"invalid infrastructure config: {exc}") from exc
            facility_ids = {item.id for item in self.facilities}
            dependency_nodes = {
                value
                for edge in infrastructure_config.dependency_edges
                for value in (edge.dependent_id, edge.dependency_id)
            }
            dependency_inputs = {
                edge.dependency_id for edge in infrastructure_config.dependency_edges
            }
            for edge in infrastructure_config.dependency_edges:
                if (
                    edge.dependent_id not in facility_ids
                    and edge.dependent_id not in dependency_inputs
                ):
                    raise ValueError(
                        f"infrastructure dependent {edge.dependent_id!r} must lead to a facility"
                    )
            if infrastructure_config.dependency_edges and not dependency_nodes:
                raise ValueError("infrastructure dependency graph cannot be empty")
        canonical_model_configs = dict(self.model_configs)
        if "information_spread" in canonical_model_configs:
            canonical_model_configs["information_spread"] = information_config.to_dict()
        if infrastructure_config is not None:
            canonical_model_configs["infrastructure"] = infrastructure_config.model_dump(
                mode="json"
            )
        if zone_environment_config is not None:
            canonical_model_configs["zone_environment"] = zone_environment_config.model_dump(
                mode="json"
            )
        if canonical_model_configs != self.model_configs:
            object.__setattr__(self, "model_configs", canonical_model_configs)
        shipments: dict[str, PlannedEventSpec] = {}
        outages: dict[str, PlannedEventSpec] = {}
        restorations: set[str] = set()
        infrastructure_outages: dict[str, PlannedEventSpec] = {}
        infrastructure_ids: set[str] = set()
        environmental_stressors: dict[str, PlannedEventSpec] = {}
        route_interruptions: list[PlannedEventSpec] = []
        infrastructure_event_count = 0
        information_rumors: dict[str, PlannedEventSpec] = {}
        information_corrections: list[PlannedEventSpec] = []
        for event in self.scheduled_events:
            if event.due_minute >= self.duration_minutes:
                raise ValueError(f"event {event.id!r} is outside the scenario duration")
            payload = event.payload
            if event.event_type in {
                "delivery_delayed",
                "water_outage",
                "water_restored",
                "staff_unavailable",
                "facility_outage",
                "facility_recovery",
                "staff_shift_change",
            } and event.kind not in (EventKind.EXOGENOUS, EventKind.INTERVENTION):
                raise ValueError(f"event {event.id!r} must be exogenous or an intervention")
            if event.event_type in {"information_rumor", "information_correction"}:
                if not information_config.enabled:
                    raise ValueError(f"event {event.id!r} requires enabled information_spread")
                if event.kind not in (EventKind.EXOGENOUS, EventKind.INTERVENTION):
                    raise ValueError(
                        f"information event {event.id!r} must be exogenous or an intervention"
                    )
                if event.source_id is None:
                    raise ValueError(f"information event {event.id!r} requires a source_id")
                if event.event_type == "information_rumor":
                    if set(payload) != {"message_id", "truth_status", "origin_agent_id"}:
                        raise ValueError(
                            f"event {event.id!r} has an invalid information_rumor payload"
                        )
                    message_id = payload.get("message_id")
                    if not _is_stable_payload_id(message_id) or message_id in information_rumors:
                        raise ValueError(
                            f"event {event.id!r} has a duplicate or invalid message_id"
                        )
                    if len(information_rumors) >= 1024:
                        raise ValueError("information_spread supports at most 1024 claims per run")
                    truth_status = payload.get("truth_status")
                    if not isinstance(truth_status, str) or truth_status not in {
                        "unknown",
                        "true",
                        "false",
                    }:
                        raise ValueError(f"event {event.id!r} has an invalid truth_status")
                    origin_agent_id = payload.get("origin_agent_id")
                    if not isinstance(origin_agent_id, str) or origin_agent_id not in {
                        item.id for item in self.profiles
                    }:
                        raise ValueError(f"event {event.id!r} references an unknown origin Agent")
                    information_rumors[message_id] = event
                else:
                    if set(payload) != {"message_id", "zone_id", "belief"}:
                        raise ValueError(
                            f"event {event.id!r} has an invalid information_correction payload"
                        )
                    if (
                        not _is_stable_payload_id(payload.get("message_id"))
                        or not isinstance(payload.get("zone_id"), str)
                        or payload.get("zone_id") not in zone_ids
                        or not isinstance(payload.get("belief"), str)
                        or payload.get("belief") not in {"verified", "rejected"}
                    ):
                        raise ValueError(f"event {event.id!r} has invalid correction references")
                    information_corrections.append(event)
            elif event.event_type == "information_contact_update":
                raise ValueError("information_contact_update events are generated by the policy")
            if event.event_type in {
                "resource_delivery",
                "resource_loss",
                "resource_reserve",
                "resource_release",
                "resource_consume",
            }:
                fields = {
                    "resource_delivery": {"warehouse_id", "resource_id", "quantity"},
                    "resource_loss": {"warehouse_id", "resource_id", "quantity"},
                    "resource_reserve": {
                        "warehouse_id",
                        "resource_id",
                        "reservation_id",
                        "agent_id",
                        "quantity",
                    },
                    "resource_release": {
                        "warehouse_id",
                        "resource_id",
                        "reservation_id",
                        "agent_id",
                    },
                    "resource_consume": {
                        "warehouse_id",
                        "resource_id",
                        "reservation_id",
                        "agent_id",
                        "quantity",
                    },
                }[event.event_type]
                actual_fields = set(payload)
                valid_fields = actual_fields == fields or (
                    event.event_type == "resource_delivery"
                    and actual_fields == fields | {"shipment_id"}
                )
                if not valid_fields:
                    raise ValueError(
                        f"event {event.id!r} has an invalid {event.event_type} payload"
                    )
                if self.initial_stock is None:
                    raise ValueError(f"event {event.id!r} requires versioned initial_stock")
                if payload.get("warehouse_id") != self.initial_stock.warehouse_id:
                    raise ValueError(f"event {event.id!r} references an unknown warehouse")
                if payload.get("resource_id") not in ("water_ml", "food_meal_units"):
                    raise ValueError(f"event {event.id!r} references an unknown resource")
                if event.event_type in {"resource_reserve", "resource_release", "resource_consume"}:
                    if not _is_stable_payload_id(payload.get("reservation_id")):
                        raise ValueError(f"event {event.id!r} reservation_id must be a stable ID")
                    if payload.get("agent_id") not in {item.id for item in self.profiles}:
                        raise ValueError(f"event {event.id!r} references an unknown Agent")
                if event.source_id is None:
                    raise ValueError(f"event {event.id!r} requires a source_id")
                if event.event_type in (
                    "resource_delivery",
                    "resource_loss",
                    "resource_reserve",
                    "resource_consume",
                ):
                    quantity = payload.get("quantity")
                    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                        raise ValueError(f"event {event.id!r} quantity must be a positive integer")
                if event.event_type == "resource_delivery" and "shipment_id" in payload:
                    if event.kind not in (EventKind.EXOGENOUS, EventKind.INTERVENTION):
                        raise ValueError(
                            f"shipment event {event.id!r} must be exogenous or an intervention"
                        )
                    shipment_id = payload["shipment_id"]
                    if not _is_stable_payload_id(shipment_id):
                        raise ValueError(f"event {event.id!r} shipment_id must be a stable ID")
                    if shipment_id in shipments:
                        raise ValueError(f"duplicate shipment ID {shipment_id!r}")
                    shipments[shipment_id] = event
            elif event.event_type in {"facility_outage", "facility_recovery"}:
                if event.source_id is None:
                    raise ValueError(f"event {event.id!r} requires a source_id")
                field = "units_out" if event.event_type == "facility_outage" else "units_restored"
                if set(payload) != {"facility_id", field}:
                    raise ValueError(
                        f"event {event.id!r} has an invalid {event.event_type} payload"
                    )
                facility = next(
                    (item for item in self.facilities if item.id == payload["facility_id"]), None
                )
                amount = payload[field]
                if facility is None:
                    raise ValueError(f"event {event.id!r} references an unknown facility")
                if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                    raise ValueError(f"event {event.id!r} {field} must be a non-negative integer")
            elif event.event_type == "delivery_delayed":
                if set(payload) != {"shipment_id", "to_minute"}:
                    raise ValueError(f"event {event.id!r} has an invalid delivery_delayed payload")
                if event.source_id is None:
                    raise ValueError(f"event {event.id!r} requires a source_id")
                if not _is_stable_payload_id(payload.get("shipment_id")):
                    raise ValueError(f"event {event.id!r} shipment_id must be a stable ID")
                to_minute = payload.get("to_minute")
                if not isinstance(to_minute, int) or isinstance(to_minute, bool):
                    raise ValueError(f"event {event.id!r} to_minute must be an integer")
            elif event.event_type == "water_outage":
                if set(payload) not in ({"outage_id", "zone_id"}, {"outage_id", "facility_id"}):
                    raise ValueError(f"event {event.id!r} has an invalid water_outage payload")
                if event.source_id is None:
                    raise ValueError(f"event {event.id!r} requires a source_id")
                outage_id = payload.get("outage_id")
                if not _is_stable_payload_id(outage_id) or outage_id in outages:
                    raise ValueError(f"event {event.id!r} has a duplicate or invalid outage_id")
                if "zone_id" in payload and (
                    not _is_stable_payload_id(payload["zone_id"])
                    or payload["zone_id"] not in zone_ids
                ):
                    raise ValueError(f"event {event.id!r} references an unknown zone")
                if "zone_id" in payload and not any(
                    facility.kind == "water_station" and facility.zone_id == payload["zone_id"]
                    for facility in self.facilities
                ):
                    raise ValueError(f"event {event.id!r} water outage zone has no water_station")
                if "facility_id" in payload:
                    target = next(
                        (item for item in self.facilities if item.id == payload["facility_id"]),
                        None,
                    )
                    if target is None or target.kind != "water_station":
                        raise ValueError(f"event {event.id!r} must target a water_station")
                if "resource_service" not in self.model_configs:
                    raise ValueError("water_outage requires an opted-in resource_service model")
                outages[outage_id] = event
            elif event.event_type == "water_restored":
                if (
                    set(payload) != {"outage_id"}
                    or event.source_id is None
                    or not _is_stable_payload_id(payload.get("outage_id"))
                ):
                    raise ValueError(f"event {event.id!r} has an invalid water_restored payload")
            elif event.event_type == "staff_unavailable":
                if set(payload) != {"staff_id", "until_minute"} or event.source_id is None:
                    raise ValueError(f"event {event.id!r} has an invalid staff_unavailable payload")
                if not _is_stable_payload_id(payload.get("staff_id")):
                    raise ValueError(f"event {event.id!r} references invalid staff")
                if payload.get("staff_id") not in {item.id for item in self.staff}:
                    raise ValueError(f"event {event.id!r} references unknown staff")
            elif event.event_type == "staff_restored":
                raise ValueError("staff_restored events are generated from staff_unavailable")
            elif event.event_type in INFRASTRUCTURE_COMPLETION_EVENT_TYPES:
                raise ValueError(
                    f"event {event.id!r} is generated from its infrastructure start event"
                )
            elif event.event_type in INFRASTRUCTURE_START_EVENT_TYPES:
                infrastructure_event_count += 1
                if infrastructure_event_count > INFRASTRUCTURE_MAX_START_EVENTS:
                    raise ValueError("infrastructure events exceed the bounded per-run limit")
                if infrastructure_config is None:
                    raise ValueError(
                        f"event {event.id!r} requires an opted-in infrastructure model"
                    )
                if event.kind not in (EventKind.EXOGENOUS, EventKind.INTERVENTION):
                    raise ValueError(
                        f"infrastructure event {event.id!r} must be exogenous or an intervention"
                    )
                if event.source_id is None:
                    raise ValueError(f"infrastructure event {event.id!r} requires a source_id")
                if event.event_type == "power_outage":
                    if set(payload) != {"outage_id", "component_id", "until_minute"}:
                        raise ValueError(f"event {event.id!r} has an invalid power_outage payload")
                    outage_id = payload.get("outage_id")
                    component_id = payload.get("component_id")
                    nodes = {
                        value
                        for edge in infrastructure_config.dependency_edges
                        for value in (edge.dependent_id, edge.dependency_id)
                    }
                    if (
                        not _is_stable_payload_id(outage_id)
                        or outage_id in infrastructure_ids
                        or not isinstance(component_id, str)
                        or component_id not in nodes
                        or not dependent_facilities(
                            infrastructure_config.model_dump(mode="json"),
                            component_id,
                            {item.id for item in self.facilities},
                        )
                    ):
                        raise ValueError(
                            f"event {event.id!r} has an unknown or unused power component"
                        )
                    infrastructure_outages[outage_id] = event
                    infrastructure_ids.add(outage_id)
                elif event.event_type == "equipment_failure":
                    if set(payload) != {
                        "outage_id",
                        "facility_id",
                        "units_out",
                        "until_minute",
                    }:
                        raise ValueError(
                            f"event {event.id!r} has an invalid equipment_failure payload"
                        )
                    outage_id = payload.get("outage_id")
                    facility_id = payload.get("facility_id")
                    units_out = payload.get("units_out")
                    facility = next(
                        (item for item in self.facilities if item.id == facility_id), None
                    )
                    if (
                        not _is_stable_payload_id(outage_id)
                        or outage_id in infrastructure_ids
                        or facility is None
                        or not isinstance(units_out, int)
                        or isinstance(units_out, bool)
                        or units_out <= 0
                        or units_out
                        > (
                            facility.capacity
                            if facility.working_units is None
                            else facility.working_units
                        )
                    ):
                        raise ValueError(f"event {event.id!r} has invalid equipment failure values")
                    infrastructure_outages[outage_id] = event
                    infrastructure_ids.add(outage_id)
                elif event.event_type in {"extreme_heat", "extreme_cold"}:
                    if set(payload) != {"stressor_id", "zone_id", "until_minute"}:
                        raise ValueError(
                            f"event {event.id!r} has an invalid {event.event_type} payload"
                        )
                    stressor_id = payload.get("stressor_id")
                    if (
                        not _is_stable_payload_id(stressor_id)
                        or stressor_id in environmental_stressors
                        or payload.get("zone_id") not in zone_ids
                    ):
                        raise ValueError(f"event {event.id!r} has invalid environmental targets")
                    active_in_zone = sum(
                        other.payload.get("zone_id") == payload.get("zone_id")
                        and other.due_minute <= event.due_minute < other.payload["until_minute"]
                        for other in environmental_stressors.values()
                    )
                    if active_in_zone >= 16:
                        raise ValueError("a zone can have at most 16 active stressor events")
                    environmental_stressors[stressor_id] = event
                elif event.event_type == "staff_shift_change":
                    if set(payload) != {"staff_id", "until_minute"}:
                        raise ValueError(f"event {event.id!r} has an invalid staff shift payload")
                    if not _is_stable_payload_id(payload.get("staff_id")) or payload.get(
                        "staff_id"
                    ) not in {item.id for item in self.staff}:
                        raise ValueError(f"event {event.id!r} references unknown shift staff")
                elif event.event_type == "supply_route_interruption":
                    if set(payload) != {
                        "outage_id",
                        "route_id",
                        "shipment_ids",
                        "until_minute",
                    }:
                        raise ValueError(
                            f"event {event.id!r} has an invalid supply route interruption payload"
                        )
                    outage_id = payload.get("outage_id")
                    route_id = payload.get("route_id")
                    shipment_ids = payload.get("shipment_ids")
                    if (
                        not _is_stable_payload_id(outage_id)
                        or outage_id in infrastructure_ids
                        or not _is_stable_payload_id(route_id)
                        or not isinstance(shipment_ids, list)
                        or not shipment_ids
                        or len(shipment_ids) > INFRASTRUCTURE_MAX_ROUTE_SHIPMENTS
                        or any(not _is_stable_payload_id(value) for value in shipment_ids)
                        or len(shipment_ids) != len(set(shipment_ids))
                    ):
                        raise ValueError(
                            f"event {event.id!r} has invalid route interruption values"
                        )
                    infrastructure_ids.add(outage_id)
                    route_interruptions.append(event)
                until_minute = payload.get("until_minute")
                if (
                    not isinstance(until_minute, int)
                    or isinstance(until_minute, bool)
                    or until_minute <= event.due_minute
                    or until_minute > self.duration_minutes
                ):
                    raise ValueError(f"event {event.id!r} has an invalid until_minute")

        rumor_by_message = {message_id: event for message_id, event in information_rumors.items()}
        correction_beliefs: dict[str, str] = {}
        for correction in information_corrections:
            message_id = correction.payload["message_id"]
            rumor = rumor_by_message.get(message_id)
            correction_order = (correction.due_minute, correction.priority, correction.sequence)
            rumor_order = (
                (rumor.due_minute, rumor.priority, rumor.sequence) if rumor is not None else None
            )
            if (
                rumor is None
                or correction_order <= rumor_order
                or correction.causation_id != rumor.id
            ):
                raise ValueError(
                    f"event {correction.id!r} must causally reference an earlier rumor"
                )
            previous_correction = correction_beliefs.setdefault(
                message_id, correction.payload["belief"]
            )
            if previous_correction != correction.payload["belief"]:
                raise ValueError(f"events correcting message {message_id!r} disagree on its truth")
            expected_belief = {
                "true": "verified",
                "false": "rejected",
            }.get(rumor.payload["truth_status"])
            if expected_belief is not None and correction.payload["belief"] != expected_belief:
                raise ValueError(f"event {correction.id!r} conflicts with the message truth status")

        delays_by_shipment: dict[str, list[PlannedEventSpec]] = {}
        for event in self.scheduled_events:
            if event.event_type == "delivery_delayed":
                delays_by_shipment.setdefault(event.payload["shipment_id"], []).append(event)
        for event in self.scheduled_events:
            payload = event.payload
            if event.event_type == "water_restored":
                outage = outages.get(payload.get("outage_id"))
                if (
                    outage is None
                    or event.due_minute <= outage.due_minute
                    or event.causation_id != outage.id
                ):
                    raise ValueError(f"event {event.id!r} references no prior water outage")
                outage_id = payload["outage_id"]
                if outage_id in restorations:
                    raise ValueError(f"water outage {outage_id!r} has duplicate restorations")
                restorations.add(outage_id)
            elif event.event_type == "staff_unavailable":
                until_minute = payload.get("until_minute")
                if (
                    not isinstance(until_minute, int)
                    or isinstance(until_minute, bool)
                    or until_minute <= event.due_minute
                    or until_minute > self.duration_minutes
                ):
                    raise ValueError(f"event {event.id!r} has an invalid until_minute")
        for event in route_interruptions:
            for shipment_id in event.payload["shipment_ids"]:
                delivery = shipments.get(shipment_id)
                if delivery is None:
                    raise ValueError(
                        f"event {event.id!r} references unknown shipment {shipment_id!r}"
                    )
                delay_duration = event.payload["until_minute"] - event.due_minute
                if (
                    delivery.due_minute <= event.due_minute
                    or delivery.due_minute + delay_duration >= self.duration_minutes
                ):
                    raise ValueError(
                        f"event {event.id!r} cannot delay shipment {shipment_id!r} within the run"
                    )
        by_route: dict[str, list[PlannedEventSpec]] = {}
        for event in route_interruptions:
            by_route.setdefault(event.payload["route_id"], []).append(event)
        for route_id, events in by_route.items():
            ordered = sorted(events, key=lambda item: (item.due_minute, item.sequence))
            if any(
                current.due_minute <= previous.payload["until_minute"]
                for previous, current in zip(ordered, ordered[1:])
            ):
                raise ValueError(f"supply route {route_id!r} interruptions cannot overlap")
        for shipment_id, delays in delays_by_shipment.items():
            delivery = shipments.get(shipment_id)
            if delivery is None:
                raise ValueError(f"delay references unknown shipment {shipment_id!r}")
            due_minute = delivery.due_minute
            for delay in sorted(
                delays, key=lambda item: (item.due_minute, item.priority, item.sequence)
            ):
                to_minute = delay.payload.get("to_minute")
                if (
                    delay.due_minute >= due_minute
                    or not isinstance(to_minute, int)
                    or isinstance(to_minute, bool)
                    or not due_minute < to_minute < self.duration_minutes
                ):
                    raise ValueError(f"event {delay.id!r} has an invalid shipment delay time")
                due_minute = to_minute
        if "resource_service" in self.model_configs:
            config = ResourceServiceConfig.model_validate(self.model_configs["resource_service"])
            if self.initial_stock is None:
                raise ValueError("resource_service requires versioned initial_stock")
            kinds = {facility.kind for facility in self.facilities}
            if (self.initial_stock.water_ml > 0 or config.water_ml_per_service > 0) and (
                "water_station" not in kinds
            ):
                raise ValueError("resource_service water requires a water_station facility")
            if (
                self.initial_stock.food_meal_units > 0 or config.food_meal_units_per_service > 0
            ) and ("meal_distribution" not in kinds):
                raise ValueError("resource_service food requires a meal_distribution facility")
            object.__setattr__(
                self,
                "model_configs",
                {**self.model_configs, "resource_service": config.model_dump(mode="json")},
            )
        if "toilet_service" in self.model_configs:
            config = ToiletServiceConfig.model_validate(self.model_configs["toilet_service"])
            object.__setattr__(
                self,
                "model_configs",
                {**self.model_configs, "toilet_service": config.model_dump(mode="json")},
            )
        if "psychology" in self.model_configs:
            config = PsychologyModelConfig.model_validate(self.model_configs["psychology"])
            profile_ids = {profile.id for profile in self.profiles}
            for field_name in _PSYCHOLOGY_AGENT_RATE_FIELDS:
                unknown_ids = set(getattr(config, field_name)) - profile_ids
                if unknown_ids:
                    raise ValueError(
                        f"psychology.{field_name} references unknown Agent IDs: "
                        f"{sorted(unknown_ids)[:5]}"
                    )
            object.__setattr__(
                self,
                "model_configs",
                {**self.model_configs, "psychology": config.to_model_config()},
            )
        if "movement_model" in self.model_configs:
            from sheltersim.domain.movement import canonical_movement_config

            try:
                movement_config = canonical_movement_config(
                    self.model_configs["movement_model"]
                )
            except ValueError as exc:
                raise ValueError(f"invalid movement_model config: {exc}") from exc
            object.__setattr__(
                self,
                "model_configs",
                {**self.model_configs, "movement_model": movement_config},
            )
        selected_models(self.model_configs)
        behavior_config = self.model_configs.get("behavior", {})
        profile_ids = {profile.id for profile in self.profiles}
        for field_name in (
            "decision_interval_minutes_by_agent",
            "temperature_by_agent",
        ):
            unknown_ids = set(behavior_config.get(field_name, {})) - profile_ids
            if unknown_ids:
                raise ValueError(
                    f"behavior.{field_name} references unknown Agent IDs: "
                    f"{sorted(unknown_ids)[:5]}"
                )
        staff_config = self.model_configs.get("staff", {})
        for name, lower, upper in (
            ("support_task_minutes", 1, 1440),
            ("unresolved_after_minutes", 1, 129_600),
            ("support_cooldown_minutes", 0, 1440),
            ("support_fairness_minutes", 1, 129_600),
        ):
            if name not in staff_config:
                continue
            value = staff_config[name]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"staff.{name} must be integer minutes within {lower}..{upper}")
        if "support_fairness_minutes" in staff_config:
            fairness_minutes = staff_config["support_fairness_minutes"]
            timeout_minutes = staff_config.get("unresolved_after_minutes", 90)
            if (
                type(fairness_minutes) is not int
                or type(timeout_minutes) is not int
                or not 1 <= fairness_minutes <= timeout_minutes
            ):
                raise ValueError(
                    "staff support_fairness_minutes must be positive and no greater than the unresolved timeout"
                )
        validate_scenario_extensions(self)
        return self

    @staticmethod
    def _ensure_unique(collection: str, identifiers: Iterable[str | int]) -> None:
        values = tuple(identifiers)
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate IDs in {collection}")

    @classmethod
    def from_json(cls, value: str) -> ScenarioSpec:
        """Parse a scenario and apply the explicit v1→v2→v3 migrations."""
        document = json.loads(value)
        if not isinstance(document, dict):
            return cls.model_validate(document)
        version = document.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool) and version in (1, 2):
            if "initial_stock" in document:
                raise ValueError(
                    f"initial_stock is only valid in ScenarioSpec v3, not v{version} input"
                )
            document = dict(document)
            if version == 1:
                document["schema_version"] = 2
                version = 2
            if version == 2:
                document["schema_version"] = 3
        return cls.model_validate(document)

    @classmethod
    def load(cls, path: str | Path) -> ScenarioSpec:
        """Load a scenario document from disk."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def canonical_json(self) -> str:
        """Return stable compact JSON with unordered collections sorted by ID."""
        data = self.model_dump(mode="json")
        if self.initial_stock is None:
            data.pop("initial_stock")
        else:
            data["initial_stock"]["source_ids"] = sorted(data["initial_stock"]["source_ids"])
        for key in ("zones", "zone_edges", "facilities", "staff", "profiles"):
            data[key] = sorted(data[key], key=lambda item: item["id"])
        data["scheduled_events"] = sorted(
            data["scheduled_events"],
            key=lambda item: (
                item["due_minute"],
                item["priority"],
                item["sequence"],
                item["id"],
            ),
        )
        return json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )

    def with_behavior_policy_variant(self, variant: str) -> ScenarioSpec:
        """Return a revalidated same-input scenario with one installed policy swapped."""
        if not isinstance(variant, str) or variant not in _BEHAVIOR_POLICY_SOURCE_IDS:
            raise ValueError(f"unsupported behavior policy variant {variant!r}")
        document = self.model_dump(mode="json")
        behavior = document["model_configs"].setdefault("behavior", {})
        behavior["policy"] = variant
        behavior["source_id"] = _BEHAVIOR_POLICY_SOURCE_IDS[variant]
        assumptions = document["provenance"].get("model_assumption_ids")
        if isinstance(assumptions, list):
            assumptions = [
                item for item in assumptions if item not in _BEHAVIOR_POLICY_SOURCE_IDS.values()
            ]
            assumptions.append(_BEHAVIOR_POLICY_SOURCE_IDS[variant])
            document["provenance"]["model_assumption_ids"] = assumptions
        return ScenarioSpec.model_validate(document)

    def save(self, path: str | Path) -> None:
        """Write canonical JSON to disk."""
        Path(path).write_text(self.canonical_json() + "\n", encoding="utf-8")


def _is_stable_payload_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value)
