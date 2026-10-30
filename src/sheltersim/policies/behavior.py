"""Read-only Agent views and reproducible, inspectable behavior proposals."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from sheltersim import native
from sheltersim.contracts.policies import selected_models
from sheltersim.contracts.population import PersonContext, population_context_people
from sheltersim.core.rng import KeyedRandom, RNGKeyPart
from sheltersim.core.world import WorldState
from sheltersim.domain.environment import ZoneDemandView, zone_environment_view
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.domain.infrastructure import (
    active_environmental_stress,
    infrastructure_event_context_ids,
)
from sheltersim.domain.models import (
    Activity,
    AgentInformationState,
    InformationBeliefStatus,
    InformationTruthStatus,
    Mobility,
    TypedMessage,
)
from sheltersim.domain.psychological_state import (
    LIFE_STATE_PSYCHOLOGY_POLICIES,
    PSYCHOLOGICAL_STATE_KEY,
)

_NO_RECORD: Mapping[str, Any] = MappingProxyType({})


def _parsed_person_contexts(
    world: WorldState, cache: dict[str, tuple[Any, PersonContext]] | None
) -> dict[str, PersonContext]:
    """Parse each person's context, reusing a cached parse of an equal record."""
    people = population_context_people(world)
    if cache is None:
        return {identity: PersonContext.model_validate(raw) for identity, raw in people.items()}
    parsed = {}
    for identity, raw in people.items():
        entry = cache.get(identity)
        if entry is None or entry[0] != raw:
            entry = (deepcopy(raw), PersonContext.model_validate(raw))
            cache[identity] = entry
        parsed[identity] = entry[1]
    return parsed


@dataclass(frozen=True)
class NeedView:
    """Detached read-only copy of an Agent's current needs."""

    toilet_urgency: float
    hunger: float
    fatigue: float
    hydration_need: float = 0.0


@dataclass(frozen=True)
class AgentView:
    """The behavior-relevant, immutable fields for one Agent."""

    id: str
    group_id: str | None
    zone_id: str
    mobility: Mobility
    support_need: float
    stress_sensitivity: float
    rng_key: str
    activity: Activity
    target_id: str | None
    needs: NeedView
    stress: float
    trust: float
    next_decision_at: int
    resource_service_enabled: bool = False
    queue_task_id: str | None = None
    queue_kind: str | None = None
    queue_target_id: str | None = None
    queue_wait_minutes: int = 0
    support_request_id: str | None = None
    support_request_status: str | None = None
    support_assigned_staff_id: str | None = None
    support_wait_minutes: int = 0
    support_cooldown_remaining_minutes: int = 0
    information_query_pressure: float = 0.0
    misinformation_belief_pressure: float = 0.0
    misinformation_effect_sources: tuple[tuple[str, str, str, str, int], ...] = ()
    environmental_stress_per_minute: float = 0.0
    environmental_stressor_event_ids: tuple[str, ...] = ()
    environmental_assumption_source_id: str | None = None
    ambient_noise_level: float = 0.0
    rest_efficiency_multiplier: float = 1.0
    zone_environment_modifier_event_ids: tuple[str, ...] = ()
    zone_environment_assumption_ids: tuple[str, ...] = ()
    ventilation_effectiveness: float = 0.7
    lighting_level: float = 0.5
    privacy_level: float = 0.5
    temperature_c: float = 20.0
    ventilation_hygiene_growth_per_minute: float = 0.0
    privacy_discontent_per_minute: float = 0.0
    condition_modifier_event_ids: tuple[str, ...] = ()
    condition_assumption_ids: tuple[str, ...] = ()
    demand_modifiers: tuple[ZoneDemandView, ...] = ()
    min_rest_decision_minutes: int = 1
    max_rest_decision_minutes: int = 60
    sleep_debt_minutes: float = 0.0
    uncertainty: float = 0.0
    reassurance: float = 0.0
    discontent: float = 0.0
    positive_affect: float = 0.5
    negative_affect: float = 0.0
    hygiene_need: float = 0.0
    sanitation_level: float = 1.0
    caregiver_ids: tuple[str, ...] = ()
    care_for_ids: tuple[str, ...] = ()
    parent_ids: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    usual_wake_minute: int | None = None
    usual_sleep_minute: int | None = None
    required_sleep_minutes: int | None = None
    personal_context_present: bool = False
    chronic_distress: float = 0.0

    @property
    def acute_distress(self) -> float:
        """Compatibility name for the existing, single `stress` field."""
        return self.stress


@dataclass(frozen=True)
class FacilityView:
    """Detached immutable facility information available to policies."""

    id: str
    kind: str
    zone_id: str
    capacity: int
    working_units: int
    active_agent_ids: tuple[str, ...]
    queue_agent_ids: tuple[str, ...]
    resource_active_agent_ids: tuple[str, ...] = ()
    resource_queue_agent_ids: tuple[str, ...] = ()
    facility_event_context_ids: tuple[str, ...] = ()


def _view_state_hash(
    world: WorldState,
    agents: Mapping[str, AgentView],
    facilities: Mapping[str, FacilityView],
    information_state: Any,
    has_population_context: bool,
) -> str:
    """The audit hash of a view: what a decision observed, in canonical JSON."""
    body_state = world.model_state.get("body_state_v1", {})
    resource_service = world.schema_version == 6
    resource_config = world.model_configs["resource_service"] if resource_service else None
    information_config = (
        world.model_configs["information_spread"] if information_state is not None else None
    )
    # Without building the document; every value is checked as canonical_sha256 does.
    # The kernel takes None for a config to leave out, so a None config stays with Python.
    if (resource_config is not None or not resource_service) and (
        information_config is not None or information_state is None
    ):
        state_hash = native.view_state_hash(
            world.minute_index,
            [agent.__dict__ for agent in agents.values()],
            [facility.__dict__ for facility in facilities.values()],
            resource_config,
            information_config,
            bool(has_population_context or body_state),
        )
        if state_hash is not None:
            return state_hash
    state_data = {
        "minute_index": world.minute_index,
        "agents": [
            {
                "id": agent.id,
                "group_id": agent.group_id,
                "zone_id": agent.zone_id,
                "mobility": agent.mobility.value,
                "support_need": agent.support_need,
                "stress_sensitivity": agent.stress_sensitivity,
                "rng_key": agent.rng_key,
                "activity": agent.activity.value,
                "target_id": agent.target_id,
                "needs": {
                    "toilet_urgency": agent.needs.toilet_urgency,
                    "hunger": agent.needs.hunger,
                    "fatigue": agent.needs.fatigue,
                },
                "stress": agent.stress,
                "trust": agent.trust,
                "next_decision_at": agent.next_decision_at,
                "queue_task_id": agent.queue_task_id,
                "queue_kind": agent.queue_kind,
                "queue_target_id": agent.queue_target_id,
                "queue_wait_minutes": agent.queue_wait_minutes,
                "support_request_id": agent.support_request_id,
                "support_request_status": agent.support_request_status,
                "support_assigned_staff_id": agent.support_assigned_staff_id,
                "support_wait_minutes": agent.support_wait_minutes,
                "support_cooldown_remaining_minutes": (agent.support_cooldown_remaining_minutes),
            }
            for agent in agents.values()
        ],
        "facilities": [
            {
                "id": facility.id,
                "kind": facility.kind,
                "zone_id": facility.zone_id,
                "capacity": facility.capacity,
                "working_units": facility.working_units,
                "active_agent_ids": facility.active_agent_ids,
                "queue_agent_ids": facility.queue_agent_ids,
            }
            for facility in facilities.values()
        ],
    }
    if world.schema_version == 6:
        state_data["resource_service_config"] = world.model_configs["resource_service"]
        for agent_state, agent_view in zip(state_data["agents"], agents.values(), strict=True):
            agent_state["needs"]["hydration_need"] = agent_view.needs.hydration_need
        for facility_state, facility_view in zip(
            state_data["facilities"], facilities.values(), strict=True
        ):
            facility_state["resource_active_agent_ids"] = facility_view.resource_active_agent_ids
            facility_state["resource_queue_agent_ids"] = facility_view.resource_queue_agent_ids
    if information_state is not None:
        state_data["information_spread_config"] = world.model_configs["information_spread"]
        for agent_state in state_data["agents"]:
            agent_state["information_query_pressure"] = information_state["query_pressure"][
                agent_state["id"]
            ]
    if has_population_context or body_state:
        state_data["agent_life_context"] = [
            {
                "agent_id": agent.id,
                "caregiver_ids": agent.caregiver_ids,
                "care_for_ids": agent.care_for_ids,
                "parent_ids": agent.parent_ids,
                "roles": agent.roles,
                "usual_wake_minute": agent.usual_wake_minute,
                "usual_sleep_minute": agent.usual_sleep_minute,
                "required_sleep_minutes": agent.required_sleep_minutes,
                "personal_context_present": agent.personal_context_present,
                "sleep_debt_minutes": agent.sleep_debt_minutes,
                "uncertainty": agent.uncertainty,
                "reassurance": agent.reassurance,
                "discontent": agent.discontent,
                "positive_affect": agent.positive_affect,
                "negative_affect": agent.negative_affect,
                "hygiene_need": agent.hygiene_need,
            }
            for agent in agents.values()
        ]
    state_hash = native.canonical_sha256(state_data)
    if state_hash is None:
        encoded = json.dumps(
            state_data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        state_hash = hashlib.sha256(encoded).hexdigest()
    return state_hash


@dataclass(frozen=True)
class WorldView:
    """Read-only, detached snapshot passed across policy boundaries."""

    minute_index: int
    agents: Mapping[str, AgentView]
    facilities: Mapping[str, FacilityView]
    state_hash: str | None
    resource_service_enabled: bool = False
    autonomy_enabled: bool = False
    zone_members: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    group_members: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Copy before wrapping so callers cannot mutate either the view or its source dict.
        agents = dict(self.agents)
        object.__setattr__(self, "agents", MappingProxyType(agents))
        object.__setattr__(self, "facilities", MappingProxyType(dict(self.facilities)))
        zones: dict[str, list[str]] = {}
        groups: dict[str, list[str]] = {}
        for agent_id, agent in sorted(agents.items()):
            zones.setdefault(agent.zone_id, []).append(agent_id)
            if agent.group_id is not None:
                groups.setdefault(agent.group_id, []).append(agent_id)
        object.__setattr__(
            self,
            "zone_members",
            MappingProxyType({key: tuple(value) for key, value in sorted(zones.items())}),
        )
        object.__setattr__(
            self,
            "group_members",
            MappingProxyType({key: tuple(value) for key, value in sorted(groups.items())}),
        )

    def immediate_need(self, agent_id: str) -> float:
        """The largest of one person's stress and needs, each worked out once per view."""
        needs_by_agent = self.__dict__.get("_immediate_needs")
        if needs_by_agent is None:
            needs_by_agent = {}
            object.__setattr__(self, "_immediate_needs", needs_by_agent)
        value = needs_by_agent.get(agent_id)
        if value is None:
            agent = self.agents[agent_id]
            needs = agent.needs
            value = needs_by_agent[agent_id] = max(
                agent.stress,
                needs.toilet_urgency,
                needs.hunger,
                needs.fatigue,
                needs.hydration_need,
            )
        return value

    @classmethod
    def from_world(
        cls,
        world: WorldState,
        *,
        include_state_hash: bool = True,
        person_cache: dict[str, tuple[Any, PersonContext]] | None = None,
    ) -> WorldView:
        """Create a detached snapshot, optionally omitting its audit hash.

        ``person_cache`` keeps parsed person contexts between calls for one caller; a
        record is parsed again as soon as it differs from the copy kept with it.
        """
        queue_tasks: dict[str, list[Any]] = {}
        information_state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
        for task in world.active_tasks.values():
            if task.agent_id is not None and task.kind in (
                "toilet_queue",
                "resource_service_queue",
            ):
                queue_tasks.setdefault(task.agent_id, []).append(task)
        body_state = world.model_state.get("body_state_v1", {})
        body_agents = body_state.get("agents", {}) if isinstance(body_state, dict) else {}
        psychology_state = world.model_state.get(PSYCHOLOGICAL_STATE_KEY, {})
        psychology_agents = (
            psychology_state.get("agents", {}) if isinstance(psychology_state, dict) else {}
        )
        presence_state = world.model_state.get("population_presence_v1", {})
        presence_people = (
            presence_state.get("people", {}) if isinstance(presence_state, dict) else {}
        )
        context_people = _parsed_person_contexts(world, person_cache)
        has_population_context = bool(context_people)
        autonomy_enabled = bool(
            world.model_configs.get("simulator_extensions", {}).get("enabled", False)
            or has_population_context
            or "body_state_v1" in world.model_state
        )
        staff_operations = world.model_state.get("staff_operations_v1", {})
        misinformation_by_agent: dict[str, tuple[float, tuple[tuple[str, str, str], ...]]] = {}
        psychology_model_id = selected_models(world.model_configs)["psychology"]
        if (
            isinstance(information_state, dict)
            and psychology_model_id in LIFE_STATE_PSYCHOLOGY_POLICIES
        ):
            messages = information_state.get("messages", {})
            beliefs_by_agent = information_state.get("beliefs", {})
            for agent_id in world.agents:
                pressure = 0.0
                sources: list[tuple[str, str, str]] = []
                raw_beliefs = beliefs_by_agent.get(agent_id, {})
                if isinstance(raw_beliefs, dict) and isinstance(messages, dict):
                    for message_id, raw_belief in sorted(raw_beliefs.items()):
                        raw_message = messages.get(message_id)
                        if not isinstance(raw_message, dict):
                            continue
                        belief = AgentInformationState.from_dict(raw_belief)
                        message = TypedMessage.from_dict(raw_message)
                        if (
                            message.truth_status is InformationTruthStatus.FALSE
                            and belief.belief is not InformationBeliefStatus.UNKNOWN
                            and belief.belief_strength > 0.5
                        ):
                            pressure += 2.0 * (belief.belief_strength - 0.5)
                            sources.append(
                                (
                                    message_id,
                                    message.origin_event_id,
                                    belief.last_event_id,
                                    message.source_id,
                                    message.time,
                                )
                            )
                misinformation_by_agent[agent_id] = (min(1.0, pressure), tuple(sources))
        else:
            misinformation_by_agent = {agent_id: (0.0, ()) for agent_id in world.agents}
        sanitation_by_zone = (
            {
                zone_id: record.get("sanitation", 1.0)
                for zone_id, record in staff_operations.get("zones", {}).items()
            }
            if isinstance(staff_operations, dict)
            else {}
        )
        environment_by_zone = {
            zone_id: zone_environment_view(world, zone_id) for zone_id in sorted(world.zones)
        }
        # What every person in a zone shares; each person's fields are added to a copy.
        zone_fields: dict[str, dict[str, Any]] = {}
        for zone_id, conditions in environment_by_zone.items():
            base_stress, event_ids, source_id = active_environmental_stress(world, zone_id)
            zone_fields[zone_id] = {
                "environmental_stress_per_minute": (
                    base_stress + conditions.temperature_stress_per_minute
                ),
                "environmental_stressor_event_ids": tuple(
                    sorted(set(event_ids) | set(conditions.condition_modifier_event_ids))
                ),
                "environmental_assumption_source_id": (
                    conditions.condition_assumption_ids[0]
                    if conditions.temperature_stress_per_minute
                    and conditions.condition_assumption_ids
                    else source_id
                ),
                "ambient_noise_level": conditions.ambient_noise_level,
                "rest_efficiency_multiplier": conditions.rest_efficiency_multiplier,
                "zone_environment_modifier_event_ids": conditions.noise_modifier_event_ids,
                "zone_environment_assumption_ids": conditions.noise_assumption_ids,
                "ventilation_effectiveness": conditions.ventilation_effectiveness,
                "lighting_level": conditions.lighting_level,
                "privacy_level": conditions.privacy_level,
                "temperature_c": conditions.temperature_c,
                "ventilation_hygiene_growth_per_minute": (
                    conditions.ventilation_hygiene_growth_per_minute
                ),
                "privacy_discontent_per_minute": conditions.privacy_discontent_per_minute,
                "condition_modifier_event_ids": conditions.condition_modifier_event_ids,
                "condition_assumption_ids": conditions.condition_assumption_ids,
                "demand_modifiers": conditions.demand_modifiers,
                "min_rest_decision_minutes": conditions.min_rest_decision_minutes,
                "max_rest_decision_minutes": conditions.max_rest_decision_minutes,
                "sanitation_level": sanitation_by_zone.get(zone_id, 1.0),
            }
        resource_service_enabled = world.schema_version == 6
        minute_index = world.minute_index
        support_requests = world.support_requests
        query_pressure = None if information_state is None else information_state["query_pressure"]
        agents = {}
        # AgentView has no __post_init__, so a person's view is one dict of its fields;
        # three views per tick spend most of their time in the generated __init__.
        for agent_id, agent in sorted(world.agents.items()):
            profile = agent.profile
            needs = agent.needs
            presence = presence_people.get(agent_id, _NO_RECORD)
            body = body_agents.get(agent_id, _NO_RECORD)
            queue = queue_tasks.get(agent_id)
            queue_task = queue[0] if queue else None
            request_id = agent.support_request_id
            request = support_requests[request_id] if request_id in support_requests else None
            context = context_people.get(agent_id)
            misinformation = misinformation_by_agent[agent_id]
            mobility = profile.mobility
            activity = agent.activity
            fields = {
                **zone_fields[agent.zone_id],
                "id": agent.id,
                "group_id": (
                    presence.get("household_id", profile.group_id)
                    if isinstance(presence, dict)
                    else profile.group_id
                ),
                "zone_id": agent.zone_id,
                "mobility": mobility if type(mobility) is Mobility else Mobility(mobility),
                "support_need": profile.support_need,
                "stress_sensitivity": profile.stress_sensitivity,
                "rng_key": profile.rng_key,
                "activity": activity if type(activity) is Activity else Activity(activity),
                "target_id": agent.target_id,
                "needs": NeedView(
                    needs.toilet_urgency, needs.hunger, needs.fatigue, needs.hydration_need
                ),
                "stress": agent.stress,
                "chronic_distress": psychology_agents.get(agent_id, _NO_RECORD).get(
                    "long_term_distress", 0.0
                ),
                "trust": agent.trust,
                "next_decision_at": agent.next_decision_at,
                "resource_service_enabled": resource_service_enabled,
                "queue_task_id": queue_task.id if queue_task is not None else None,
                "queue_kind": queue_task.kind if queue_task is not None else None,
                "queue_target_id": queue_task.resource_id if queue_task is not None else None,
                "queue_wait_minutes": (
                    max(0, minute_index - queue_task.start_minute) if queue_task is not None else 0
                ),
                "support_request_id": request_id,
                "support_request_status": request.status.value if request is not None else None,
                "support_assigned_staff_id": (
                    request.assigned_staff_id if request is not None else None
                ),
                "support_wait_minutes": (
                    max(0, minute_index - request.requested_minute) if request is not None else 0
                ),
                "support_cooldown_remaining_minutes": max(
                    0, agent.support_cooldown_until - minute_index
                ),
                "information_query_pressure": (
                    query_pressure[agent_id] if information_state is not None else 0.0
                ),
                "misinformation_belief_pressure": misinformation[0],
                "misinformation_effect_sources": misinformation[1],
                "sleep_debt_minutes": body.get("sleep_debt_minutes", 0.0),
                "uncertainty": body.get("uncertainty", 0.0),
                "reassurance": body.get("reassurance", 0.0),
                "discontent": body.get("discontent", 0.0),
                "positive_affect": body.get("positive_affect", 0.5),
                "negative_affect": body.get("negative_affect", 0.0),
                "hygiene_need": body.get("hygiene_need", 0.0),
                "caregiver_ids": tuple(context.caregiver_ids) if context is not None else (),
                "care_for_ids": tuple(context.care_for_ids) if context is not None else (),
                "parent_ids": tuple(context.parent_ids) if context is not None else (),
                "roles": tuple(context.roles) if context is not None else (),
                "usual_wake_minute": context.usual_wake_minute if context is not None else None,
                "usual_sleep_minute": context.usual_sleep_minute if context is not None else None,
                "required_sleep_minutes": (
                    context.required_sleep_minutes if context is not None else None
                ),
                "personal_context_present": context is not None,
            }
            view = object.__new__(AgentView)
            object.__setattr__(view, "__dict__", fields)
            agents[agent_id] = view
        operational = world.model_state.get("operational_events_v1", {})
        facilities = {
            facility_id: FacilityView(
                id=facility.id,
                kind=facility.kind,
                zone_id=facility.zone_id,
                capacity=facility.capacity,
                working_units=facility.working_units,
                active_agent_ids=tuple(facility.active_agent_ids),
                queue_agent_ids=tuple(facility.queue_agent_ids),
                resource_active_agent_ids=tuple(facility.resource_active_agent_ids),
                resource_queue_agent_ids=tuple(facility.resource_queue_agent_ids),
                facility_event_context_ids=tuple(
                    sorted(
                        (
                            world.event_log.applied_facility_event_ids(facility_id)
                            if facility.working_units < facility.capacity
                            else set()
                        )
                        | {
                            outage["outage_event_id"]
                            for outage in operational.get("water_outages", {}).values()
                            if "water_station" in facility.kind.casefold()
                            and outage["active"]
                            and (
                                (
                                    outage["target_kind"] == "zone"
                                    and outage["target_id"] == facility.zone_id
                                )
                                or (
                                    outage["target_kind"] == "facility"
                                    and outage["target_id"] == facility_id
                                )
                            )
                        }
                        | set(infrastructure_event_context_ids(world, facility_id))
                    )
                ),
            )
            for facility_id, facility in sorted(world.facilities.items())
        }
        state_hash = None
        if include_state_hash:
            state_hash = _view_state_hash(
                world, agents, facilities, information_state, has_population_context
            )
        view = cls(
            minute_index=world.minute_index,
            agents=agents,
            facilities=facilities,
            state_hash=state_hash,
            resource_service_enabled=world.schema_version == 6,
            autonomy_enabled=autonomy_enabled,
        )
        object.__setattr__(view, "_has_population_context", has_population_context)
        return view

    @classmethod
    def after_state_update(cls, prior: WorldView, world: WorldState) -> WorldView:
        """from_world(world) when the only writes since ``prior`` are the update phase's.

        Those are the needs, stress and trust of each person, their body state and their
        long-term distress, which are read again here, and state no view reads (the social
        propagation and relationship records, the metric state). Every other field is
        ``prior``'s, including what is derived from it.
        """
        body_state = world.model_state.get("body_state_v1", {})
        body_agents = body_state.get("agents", {}) if isinstance(body_state, dict) else {}
        psychology_state = world.model_state.get(PSYCHOLOGICAL_STATE_KEY, {})
        psychology_agents = (
            psychology_state.get("agents", {}) if isinstance(psychology_state, dict) else {}
        )
        agents = {}
        for agent_id, before in prior.agents.items():
            agent = world.agents[agent_id]
            needs = agent.needs
            body = body_agents.get(agent_id, _NO_RECORD)
            fields = before.__dict__.copy()
            fields["needs"] = NeedView(
                needs.toilet_urgency, needs.hunger, needs.fatigue, needs.hydration_need
            )
            fields["stress"] = agent.stress
            fields["chronic_distress"] = psychology_agents.get(agent_id, _NO_RECORD).get(
                "long_term_distress", 0.0
            )
            fields["trust"] = agent.trust
            fields["sleep_debt_minutes"] = body.get("sleep_debt_minutes", 0.0)
            fields["uncertainty"] = body.get("uncertainty", 0.0)
            fields["reassurance"] = body.get("reassurance", 0.0)
            fields["discontent"] = body.get("discontent", 0.0)
            fields["positive_affect"] = body.get("positive_affect", 0.5)
            fields["negative_affect"] = body.get("negative_affect", 0.0)
            fields["hygiene_need"] = body.get("hygiene_need", 0.0)
            view = object.__new__(AgentView)
            object.__setattr__(view, "__dict__", fields)
            agents[agent_id] = view
        refreshed = object.__new__(cls)
        fields = prior.__dict__.copy()
        fields.pop("_immediate_needs", None)
        fields["agents"] = MappingProxyType(agents)
        fields["state_hash"] = _view_state_hash(
            world,
            agents,
            prior.facilities,
            world.model_state.get(INFORMATION_SPREAD_STATE_KEY),
            prior._has_population_context,
        )
        object.__setattr__(refreshed, "__dict__", fields)
        return refreshed


class ActionKind(StrEnum):
    """Small set of decisions supported by the first offline behavior model."""

    WAIT = "wait"
    REQUEST_TOILET = "request_toilet"
    REST = "rest"
    SLEEP = "sleep"
    SEEK_HELP = "seek_help"
    FETCH_WATER = "fetch_water"
    COLLECT_MEAL = "collect_meal"
    SEEK_INFORMATION = "seek_information"
    SOCIALIZE = "socialize"
    CARE_FAMILY = "care_family"
    HELP_OTHERS = "help_others"


@dataclass(frozen=True)
class ActionCandidate:
    """One candidate and the evidence needed to inspect its selection."""

    action: ActionKind
    eligible: bool
    trigger: str
    utility: float
    urgency_priority: int
    probability: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ActionKind(self.action))
        if not self.trigger:
            raise ValueError("candidate trigger must be non-empty")
        if not math.isfinite(self.utility):
            raise ValueError("candidate utility must be finite")
        if self.urgency_priority < 0:
            raise ValueError("urgency priority cannot be negative")
        if not math.isfinite(self.probability) or not 0.0 <= self.probability <= 1.0:
            raise ValueError("candidate probability must be between 0 and 1")
        if not self.eligible and self.probability != 0.0:
            raise ValueError("ineligible candidates must have zero probability")


@dataclass(frozen=True)
class ActionProposal:
    """Immutable action proposal with a complete, inspectable decision trace."""

    agent_id: str
    minute_index: int
    observed_state_hash: str
    action: ActionKind
    candidates: tuple[ActionCandidate, ...]
    trigger: str
    choice_value: float
    rng_key: tuple[RNGKeyPart, ...]
    selection_probability: float
    policy_version: str
    next_decision_at: int
    target_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", ActionKind(self.action))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "rng_key", tuple(self.rng_key))
        if self.minute_index < 0 or self.next_decision_at <= self.minute_index:
            raise ValueError("proposal has invalid decision timing")
        if len(self.observed_state_hash) != 64:
            raise ValueError("observed_state_hash must be a SHA-256 hex digest")
        if not self.candidates or not self.policy_version or not self.trigger:
            raise ValueError("proposal requires candidates, policy version, and trigger")
        if not self.rng_key:
            raise ValueError("proposal must expose the RNG key used for selection")
        if not math.isfinite(self.choice_value) or not 0.0 <= self.choice_value < 1.0:
            raise ValueError("choice_value must be between 0 and 1")
        if (
            not math.isfinite(self.selection_probability)
            or not 0.0 < self.selection_probability <= 1.0
        ):
            raise ValueError("selected action probability must be between 0 and 1")
        eligible = [candidate for candidate in self.candidates if candidate.eligible]
        if not eligible or not math.isclose(sum(item.probability for item in eligible), 1.0):
            raise ValueError("eligible candidate probabilities must sum to one")
        selected = [candidate for candidate in eligible if candidate.action is self.action]
        if len(selected) != 1 or not math.isclose(
            selected[0].probability, self.selection_probability
        ):
            raise ValueError("proposal action must match one eligible candidate")


class BehaviorPolicy(Protocol):
    """Replaceable policy that reads a snapshot and emits a typed proposal."""

    policy_version: str

    def propose(
        self,
        view: WorldView,
        agent_id: str,
        rng: KeyedRandom,
        *,
        trigger: str = "scheduled_decision",
    ) -> ActionProposal: ...


@dataclass(frozen=True)
class BehaviorPolicyDefinition:
    """Safe presentation metadata for one installed behavior-policy variant."""

    variant: str
    label: str
    policy_version: str
    assumption_source_id: str
    selection_rule: str
    description: str
    parameters: tuple[str, ...]
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "registry_version": "behavior-policy-registry-v1",
            "label": self.label,
            "policy_version": self.policy_version,
            "assumption_source_id": self.assumption_source_id,
            "selection_rule": self.selection_rule,
            "description": self.description,
            "parameters": list(self.parameters),
            "capabilities": list(self.capabilities),
        }


BEHAVIOR_POLICY_REGISTRY: Mapping[str, BehaviorPolicyDefinition] = MappingProxyType(
    {
        "utility-softmax-v1": BehaviorPolicyDefinition(
            variant="utility-softmax-v1",
            label="効用 softmax",
            policy_version="utility-softmax-v1",
            assumption_source_id="ASM-UTILITY-BEHAVIOR-V1",
            selection_rule="eligible candidates are sampled by utility and urgency priority",
            description=(
                "utility scoreに応じた確率で候補を選びます。"
                "確率は実測データで校正されていない仮説です。"
            ),
            parameters=(
                "temperature",
                "decision_interval_minutes",
                "shared candidate utility coefficients",
            ),
            capabilities=("typed action proposals", "keyed random choice", "candidate trace"),
        ),
        "sleep-aware-utility-softmax-v2": BehaviorPolicyDefinition(
            variant="sleep-aware-utility-softmax-v2",
            label="睡眠負債対応 softmax v2",
            policy_version="sleep-aware-utility-softmax-v2",
            assumption_source_id="ASM-SLEEP-AWARE-BEHAVIOR-V2",
            selection_rule="eligible candidates are sampled by utility and urgency priority",
            description=(
                "睡眠負債を個別の必要睡眠時間で正規化し、"
                "必要時間相当の負債が効用へ1単位加算される仮定です。"
            ),
            parameters=(
                "temperature",
                "decision_interval_minutes",
                "sleep_debt_utility_weight",
                "shared candidate utility coefficients",
            ),
            capabilities=("typed action proposals", "keyed random choice", "candidate trace"),
        ),
        "priority-rule-v1": BehaviorPolicyDefinition(
            variant="priority-rule-v1",
            label="優先順位ルール",
            policy_version="priority-rule-v1",
            assumption_source_id="ASM-PRIORITY-RULE-BEHAVIOR-V1",
            selection_rule=(
                "highest urgency priority, then highest utility; "
                "keyed random choice only breaks exact ties"
            ),
            description=(
                "既存の候補適格条件とutility式を使い、優先度・utilityの順で選びます。"
                "同点時だけseed-keyedな抽選を行います。"
            ),
            parameters=(
                "decision_interval_minutes",
                "shared candidate utility coefficients",
                "action tie-breaking uses keyed random draws",
            ),
            capabilities=("typed action proposals", "keyed tie breaking", "candidate trace"),
        ),
    }
)


def behavior_policy_catalog() -> list[dict[str, Any]]:
    """Return the bounded registry catalog for Admin and scenario previews."""
    return [BEHAVIOR_POLICY_REGISTRY[key].to_dict() for key in sorted(BEHAVIOR_POLICY_REGISTRY)]


def behavior_policy_definition(
    variant: str, *, resource_service_enabled: bool, autonomy_enabled: bool = False
) -> dict[str, Any]:
    """Return a run-specific version and capabilities without hidden policy state."""
    if not isinstance(variant, str):
        raise ValueError("behavior policy variant must be a registered string")
    try:
        definition = BEHAVIOR_POLICY_REGISTRY[variant]
    except KeyError as exc:
        raise ValueError(f"unknown behavior policy variant {variant!r}") from exc
    version = (
        f"{definition.policy_version.removesuffix('-v1')}-autonomy-v2"
        if autonomy_enabled
        else definition.policy_version
    )
    if resource_service_enabled:
        version = f"{version.removesuffix('-v1')}-resource-service-v1"
    result = definition.to_dict()
    result["policy_version"] = version
    result["capabilities"] = [
        *definition.capabilities,
        *(("water and meal actions",) if resource_service_enabled else ()),
        *(("rhythm-aware care, support and social choices",) if autonomy_enabled else ()),
    ]
    return result


def create_behavior_policy(variant: str, **parameters: Any) -> BehaviorPolicy:
    """Build an installed variant around the shared typed candidate scorer."""
    resource_service_enabled = parameters.get("resource_service_enabled", False)
    autonomy_enabled = parameters.get("autonomy_enabled", False)
    definition = behavior_policy_definition(
        variant,
        resource_service_enabled=bool(resource_service_enabled),
        autonomy_enabled=bool(autonomy_enabled),
    )
    scorer = UtilityBehaviorPolicy(
        **parameters,
        policy_version=definition["policy_version"],
    )
    if variant in {"utility-softmax-v1", "sleep-aware-utility-softmax-v2"}:
        return scorer
    if variant == "priority-rule-v1":
        return PriorityRuleBehaviorPolicy(scorer, policy_version=definition["policy_version"])
    raise ValueError(f"unknown behavior policy variant {variant!r}")


class IneligibleDecisionError(ValueError):
    """Raised when a caller asks an inactive or not-yet-due Agent to decide."""


@dataclass(frozen=True)
class UtilityBehaviorPolicy:
    """Offline utility/softmax policy; coefficients are explicit hypotheses."""

    temperature: float = 0.2
    decision_interval_minutes: int = 15
    decision_interval_minutes_by_agent: dict[str, int] = field(default_factory=dict)
    temperature_by_agent: dict[str, float] = field(default_factory=dict)
    sleep_debt_utility_weight: float = 1.0
    policy_version: str = "utility-softmax-v1"
    resource_service_enabled: bool = False
    hydration_action_threshold: float = 0.25
    hunger_action_threshold: float = 0.7
    water_utility_base: float = 0.18
    water_utility_need_weight: float = 1.0
    meal_utility_base: float = 0.18
    meal_utility_need_weight: float = 0.85
    information_query_threshold: float = 1.1
    information_query_utility_weight: float = 0.0
    autonomy_enabled: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.temperature) or self.temperature <= 0.0:
            raise ValueError("temperature must be positive and finite")
        if self.decision_interval_minutes <= 0:
            raise ValueError("decision interval must be positive")
        if (
            not math.isfinite(self.sleep_debt_utility_weight)
            or self.sleep_debt_utility_weight < 0.0
        ):
            raise ValueError("sleep debt utility weight must be finite and non-negative")
        if (
            len(self.decision_interval_minutes_by_agent) > 100
            or len(self.temperature_by_agent) > 100
        ):
            raise ValueError("per-Agent behavior coefficients exceed the 100-resident limit")
        if any(
            not isinstance(agent_id, str)
            or not agent_id
            or len(agent_id) > 64
            or type(minutes) is not int
            or not 1 <= minutes <= 1440
            for agent_id, minutes in self.decision_interval_minutes_by_agent.items()
        ):
            raise ValueError("per-Agent decision intervals must be integer minutes in [1, 1440]")
        if any(
            not isinstance(agent_id, str)
            or not agent_id
            or len(agent_id) > 64
            or type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= 10
            for agent_id, value in self.temperature_by_agent.items()
        ):
            raise ValueError("per-Agent choice temperatures must be finite values in (0, 10]")
        if not self.policy_version:
            raise ValueError("policy_version must be non-empty")
        for name in (
            "hydration_action_threshold",
            "hunger_action_threshold",
            "water_utility_base",
            "water_utility_need_weight",
            "meal_utility_base",
            "meal_utility_need_weight",
            "information_query_threshold",
            "information_query_utility_weight",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            maximum = 1.1 if name == "information_query_threshold" else 1.0
            if name.endswith("threshold") and value > maximum:
                raise ValueError(f"{name} must be between zero and {maximum:g}")

    def propose(
        self,
        view: WorldView,
        agent_id: str,
        rng: KeyedRandom,
        *,
        trigger: str = "scheduled_decision",
    ) -> ActionProposal:
        """Score candidates using an immutable view and choose with keyed randomness."""
        if view.state_hash is None:
            raise ValueError("behavior decisions require a WorldView with a state hash")
        try:
            agent = view.agents[agent_id]
        except KeyError as exc:
            raise ValueError(f"unknown Agent {agent_id!r}") from exc
        if view.minute_index < agent.next_decision_at:
            raise IneligibleDecisionError(f"Agent {agent_id!r} is not due for a decision")
        if agent.activity not in (Activity.IDLE, Activity.RESTING, Activity.SLEEPING):
            raise IneligibleDecisionError(
                f"Agent {agent_id!r} cannot decide while {agent.activity.value}"
            )

        needs = agent.needs
        distress_needs = [needs.toilet_urgency, needs.hunger, needs.fatigue]
        resource_service_enabled = self.resource_service_enabled and view.resource_service_enabled
        if resource_service_enabled:
            distress_needs.append(needs.hydration_need)
        distress = max(
            *distress_needs,
            agent.stress,
            agent.chronic_distress,
            agent.discontent * 0.75,
            agent.negative_affect * 0.6,
        )
        has_open_support_request = (
            agent.support_request_id is not None
            and agent.support_request_status in (None, "pending", "assigned")
        )
        information_query_enabled = (
            self.information_query_threshold <= 1.0 or self.information_query_utility_weight > 0.0
        )
        information_query_trigger = information_query_enabled and (
            agent.information_query_pressure >= self.information_query_threshold
            or agent.uncertainty >= max(0.65, self.information_query_threshold)
        )
        if information_query_enabled:
            support_trigger = (
                "support_need>=0.25 and maximum need/stress>=0.50, or information query pressure "
                f">={self.information_query_threshold:g} or uncertainty>=0.65; "
                "no_open_support_request and "
                "support_cooldown_elapsed"
            )
        else:
            support_trigger = (
                "support_need>=0.25 and maximum need/stress>=0.50 "
                "and no_open_support_request and support_cooldown_elapsed"
            )
        can_seek_help = (
            agent.support_need >= 0.25
            and distress >= 0.50
            and not has_open_support_request
            and agent.support_cooldown_remaining_minutes == 0
        ) or (
            information_query_trigger
            and not has_open_support_request
            and agent.support_cooldown_remaining_minutes == 0
        )
        can_seek_information = information_query_enabled and (
            information_query_trigger
            and not has_open_support_request
            and agent.support_cooldown_remaining_minutes == 0
        )
        zone_peers = tuple(
            peer_id for peer_id in view.zone_members.get(agent.zone_id, ()) if peer_id != agent_id
        )
        family_peers = tuple(
            peer_id
            for peer_id in view.group_members.get(agent.group_id or "", ())
            if peer_id != agent_id and view.agents[peer_id].zone_id == agent.zone_id
        )
        explicit_care_peers = tuple(
            peer_id
            for peer_id in zone_peers
            if peer_id in agent.care_for_ids
            or agent.id in view.agents[peer_id].caregiver_ids
            or agent.id in view.agents[peer_id].parent_ids
        )
        if explicit_care_peers:
            family_peers = tuple(
                peer_id for peer_id in family_peers if peer_id in explicit_care_peers
            )
        elif agent.personal_context_present:
            family_peers = ()
        pressure = view.immediate_need
        family_target = max(
            family_peers,
            key=lambda peer_id: (pressure(peer_id), peer_id),
            default=None,
        )
        help_targets = tuple(
            peer_id for peer_id in zone_peers if pressure(peer_id) > distress + 0.15
        )
        help_target = max(
            help_targets,
            key=lambda peer_id: (pressure(peer_id), peer_id),
            default=None,
        )
        minute_of_day = view.minute_index % 1440
        sleep_window = False
        if agent.usual_sleep_minute is not None and agent.usual_wake_minute is not None:
            sleep_time, wake_time = agent.usual_sleep_minute, agent.usual_wake_minute
            sleep_window = (
                minute_of_day >= sleep_time or minute_of_day < wake_time
                if sleep_time > wake_time
                else sleep_time <= minute_of_day < wake_time
            )
        sleep_rhythm_due = (
            view.autonomy_enabled
            and sleep_window
            and agent.required_sleep_minutes is not None
            and agent.required_sleep_minutes > 0
        )
        meal_rhythm_due = False
        if view.autonomy_enabled and agent.usual_wake_minute is not None:
            # Meal opportunities follow an individual's wake time. The three
            # windows and 45-minute half-width are explicit assumptions until
            # they are replaced by the registered behavioral policy.
            minute_since_wake = (minute_of_day - agent.usual_wake_minute) % 1440
            meal_rhythm_due = (
                min(abs(minute_since_wake - meal_offset) for meal_offset in (120, 420, 660)) <= 45
            )
        candidates = [
            (ActionKind.WAIT, True, "always", 0.10 + 0.15 * (1.0 - distress), 0),
            (
                ActionKind.REQUEST_TOILET,
                needs.toilet_urgency >= 0.25 or agent.hygiene_need >= 0.5,
                "toilet_urgency>=0.25 or hygiene_need>=0.5",
                0.20 + 0.90 * needs.toilet_urgency + 0.35 * agent.hygiene_need,
                int(needs.toilet_urgency >= 0.80 or agent.hygiene_need >= 0.8),
            ),
            (
                ActionKind.REST,
                needs.fatigue >= 0.25 or needs.hunger >= 0.70 or agent.stress >= 0.50,
                "fatigue>=0.25 or hunger>=0.70 or stress>=0.50",
                0.10 + 0.45 * needs.fatigue + 0.20 * needs.hunger + 0.30 * agent.stress,
                0,
            ),
            (
                ActionKind.SLEEP,
                needs.fatigue >= 0.65 or agent.sleep_debt_minutes >= 240.0 or sleep_rhythm_due,
                "fatigue>=0.65 or sleep_debt_minutes>=240 or individual_sleep_window",
                0.25
                + 0.90 * needs.fatigue
                + _sleep_debt_utility(agent, self.policy_version, self.sleep_debt_utility_weight)
                + (0.20 if sleep_rhythm_due else 0.0)
                - 0.20 * max(0.0, agent.lighting_level - 0.3),
                0,
            ),
            (
                ActionKind.SEEK_HELP,
                can_seek_help,
                support_trigger,
                0.15
                + 0.65 * distress
                + 0.25 * agent.support_need
                + 0.10 * agent.trust
                + 0.20 * agent.uncertainty
                + 0.15 * agent.discontent
                - 0.10 * agent.reassurance
                + self.information_query_utility_weight * agent.information_query_pressure,
                int(distress >= 0.80),
            ),
            (
                ActionKind.SEEK_INFORMATION,
                can_seek_information,
                "information_query_pressure>=threshold or uncertainty>=0.65",
                0.20
                + 0.55 * agent.uncertainty
                + self.information_query_utility_weight * agent.information_query_pressure,
                int(agent.uncertainty >= 0.9),
            ),
        ]
        if view.autonomy_enabled and self.autonomy_enabled:
            candidates.extend(
                [
                    (
                        ActionKind.SOCIALIZE,
                        bool(zone_peers),
                        "at least one co-located person is available",
                        0.10
                        + 0.25 * agent.uncertainty
                        + 0.15 * agent.negative_affect
                        + 0.10 * (1.0 - agent.reassurance),
                        0,
                    ),
                    (
                        ActionKind.CARE_FAMILY,
                        family_target is not None and pressure(family_target) >= 0.25,
                        "co-located family or care-linked person has an unmet need",
                        0.12
                        + (0.45 * pressure(family_target) if family_target is not None else 0.0),
                        int(family_target is not None and view.agents[family_target].stress >= 0.8),
                    ),
                    (
                        ActionKind.HELP_OTHERS,
                        help_target is not None,
                        "co-located person has substantially higher immediate need or stress",
                        0.15 + (0.4 * pressure(help_target) if help_target is not None else 0.0),
                        int(help_target is not None and view.agents[help_target].stress >= 0.8),
                    ),
                ]
            )
        if resource_service_enabled:
            candidates.extend(
                [
                    (
                        ActionKind.FETCH_WATER,
                        needs.hydration_need >= self.hydration_action_threshold,
                        f"hydration_need>={self.hydration_action_threshold:g}",
                        self.water_utility_base
                        + self.water_utility_need_weight * needs.hydration_need,
                        int(needs.hydration_need >= 0.8),
                    ),
                    (
                        ActionKind.COLLECT_MEAL,
                        needs.hunger >= self.hunger_action_threshold or meal_rhythm_due,
                        f"hunger>={self.hunger_action_threshold:g} or individual_meal_window",
                        self.meal_utility_base
                        + self.meal_utility_need_weight * needs.hunger
                        + (0.12 if meal_rhythm_due else 0.0),
                        int(needs.hunger >= 0.85),
                    ),
                ]
            )
        eligible_utilities = [item[3] + item[4] * 0.5 for item in candidates if item[1]]
        maximum = max(eligible_utilities)
        temperature = self.temperature_by_agent.get(agent_id, self.temperature)
        demand_modifiers = {item.action: item for item in agent.demand_modifiers}
        weights = [
            (
                math.exp((utility + priority * 0.5 - maximum) / temperature)
                * (
                    demand_modifiers[action.value].probability_multiplier
                    if action.value in demand_modifiers
                    else 1.0
                )
                if eligible
                else 0.0
            )
            for action, eligible, _, utility, priority in candidates
        ]
        total_weight = sum(weights)
        probabilities = [weight / total_weight for weight in weights]
        eligible_indices = [index for index, item in enumerate(candidates) if item[1]]
        last_eligible_index = eligible_indices[-1]
        probabilities[last_eligible_index] = max(
            0.0,
            1.0 - math.fsum(probabilities[index] for index in eligible_indices[:-1]),
        )
        evidence = tuple(
            _candidate(
                action,
                eligible,
                _demand_trigger(candidate_trigger, demand_modifiers.get(action.value)),
                utility + priority * 0.5,
                priority,
                probability,
            )
            for (action, eligible, candidate_trigger, utility, priority), probability in zip(
                candidates, probabilities, strict=True
            )
        )

        rng_key: tuple[RNGKeyPart, ...] = (
            "behavior",
            agent.rng_key,
            agent.id,
            "minute",
            view.minute_index,
            "policy",
            self.policy_version,
            "choice",
        )
        choice_value = rng.random(*rng_key)
        cumulative = 0.0
        selected = evidence[last_eligible_index]
        for candidate in evidence:
            if not candidate.eligible:
                continue
            cumulative += candidate.probability
            if choice_value < cumulative:
                selected = candidate
                break

        return ActionProposal(
            agent_id=agent.id,
            minute_index=view.minute_index,
            observed_state_hash=view.state_hash,
            action=selected.action,
            candidates=evidence,
            trigger=trigger,
            choice_value=choice_value,
            rng_key=rng_key,
            selection_probability=selected.probability,
            policy_version=self.policy_version,
            next_decision_at=view.minute_index
            + _decision_interval_minutes(
                self.decision_interval_minutes_by_agent.get(
                    agent_id, self.decision_interval_minutes
                ),
                selected.action.value,
                demand_modifiers.get(selected.action.value),
                agent.min_rest_decision_minutes,
                agent.max_rest_decision_minutes,
            ),
            target_id=_target_for_action(view, agent, selected.action),
        )


@dataclass(frozen=True)
class PriorityRuleBehaviorPolicy:
    """Rank the shared eligible action set, with keyed randomness for exact ties."""

    scorer: UtilityBehaviorPolicy
    policy_version: str = "priority-rule-v1"

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise ValueError("policy_version must be non-empty")

    def propose(
        self,
        view: WorldView,
        agent_id: str,
        rng: KeyedRandom,
        *,
        trigger: str = "scheduled_decision",
    ) -> ActionProposal:
        """Use existing eligibility and utility evidence without mutating the observed world."""
        proposal = self.scorer.propose(view, agent_id, rng, trigger=trigger)
        eligible = [candidate for candidate in proposal.candidates if candidate.eligible]
        best_rank = max((item.urgency_priority, item.utility) for item in eligible)
        tied_actions = {
            item.action for item in eligible if (item.urgency_priority, item.utility) == best_rank
        }
        rng_key = (*proposal.rng_key, "priority-tie-break")
        choice_value = rng.random(*rng_key)
        tied = sorted(
            (item for item in eligible if item.action in tied_actions),
            key=lambda item: item.action.value,
        )
        selected_index = min(len(tied) - 1, int(choice_value * len(tied)))
        selected = tied[selected_index]
        probability = 1.0 / len(tied)
        evidence = tuple(
            replace(
                candidate,
                probability=probability if candidate.action in tied_actions else 0.0,
            )
            for candidate in proposal.candidates
        )
        agent = view.agents[agent_id]
        demand_modifiers = {item.action: item for item in agent.demand_modifiers}
        next_decision_at = view.minute_index + _decision_interval_minutes(
            self.scorer.decision_interval_minutes_by_agent.get(
                agent_id, self.scorer.decision_interval_minutes
            ),
            selected.action.value,
            demand_modifiers.get(selected.action.value),
            agent.min_rest_decision_minutes,
            agent.max_rest_decision_minutes,
        )
        return replace(
            proposal,
            action=selected.action,
            candidates=evidence,
            choice_value=choice_value,
            rng_key=rng_key,
            selection_probability=probability,
            policy_version=self.policy_version,
            next_decision_at=next_decision_at,
            target_id=_target_for_action(view, agent, selected.action),
        )


def _candidate(
    action: ActionKind,
    eligible: bool,
    trigger: str,
    utility: float,
    urgency_priority: int,
    probability: float,
) -> ActionCandidate:
    """ActionCandidate(...) of a candidate the scorer made, without the generated __init__
    when its checks pass; the class raises any error."""
    if (
        type(action) is not ActionKind
        or not trigger
        or not math.isfinite(utility)
        or urgency_priority < 0
        or not math.isfinite(probability)
        or not 0.0 <= probability <= 1.0
        or (not eligible and probability != 0.0)
    ):
        return ActionCandidate(action, eligible, trigger, utility, urgency_priority, probability)
    candidate = object.__new__(ActionCandidate)
    object.__setattr__(
        candidate,
        "__dict__",
        {
            "action": action,
            "eligible": eligible,
            "trigger": trigger,
            "utility": utility,
            "urgency_priority": urgency_priority,
            "probability": probability,
        },
    )
    return candidate


def _demand_trigger(trigger: str, modifier: ZoneDemandView | None) -> str:
    if modifier is None:
        return trigger
    reasons = [
        f"experimental zone action multiplier x{modifier.probability_multiplier:g}",
        f"events={','.join(modifier.modifier_event_ids)}",
        f"assumptions={','.join(modifier.assumption_ids)}",
    ]
    if modifier.duration_multiplier != 1.0:
        reasons.append(f"decision interval x{modifier.duration_multiplier:g}")
    return f"{trigger}; {'; '.join(reasons)}"


def _decision_interval_minutes(
    base_minutes: int,
    action: str,
    modifier: ZoneDemandView | None,
    minimum: int,
    maximum: int,
) -> int:
    if (
        modifier is None
        or action not in {ActionKind.REST.value, ActionKind.SLEEP.value}
        or modifier.duration_multiplier == 1.0
    ):
        return base_minutes
    scaled = round(base_minutes * modifier.duration_multiplier)
    return min(maximum, max(minimum, scaled))


def _sleep_debt_utility(
    agent: AgentView, policy_version: str, sleep_debt_utility_weight: float
) -> float:
    if not policy_version.startswith("sleep-aware-utility-softmax-v2"):
        return agent.sleep_debt_minutes / 1440.0 * 0.45
    required_sleep = agent.required_sleep_minutes
    if required_sleep is None or required_sleep == 0:
        required_sleep = 480
    return sleep_debt_utility_weight * agent.sleep_debt_minutes / required_sleep


def _target_for_action(view: WorldView, agent: AgentView, action: ActionKind) -> str | None:
    """Choose a co-located partner in stable order for typed, replayable actions."""
    if action is ActionKind.SOCIALIZE:
        return next(
            (peer for peer in view.zone_members.get(agent.zone_id, ()) if peer != agent.id),
            None,
        )
    if action not in (ActionKind.CARE_FAMILY, ActionKind.HELP_OTHERS):
        return None
    own_pressure = max(
        agent.stress,
        agent.needs.toilet_urgency,
        agent.needs.hunger,
        agent.needs.fatigue,
        agent.needs.hydration_need,
    )
    if action is ActionKind.CARE_FAMILY:
        peers = tuple(
            peer
            for peer in view.group_members.get(agent.group_id or "", ())
            if peer != agent.id and view.agents[peer].zone_id == agent.zone_id
        )
        explicit = tuple(
            peer
            for peer in peers
            if peer in agent.care_for_ids
            or agent.id in view.agents[peer].caregiver_ids
            or agent.id in view.agents[peer].parent_ids
        )
        if explicit:
            peers = explicit
        elif agent.personal_context_present or any(
            view.agents[peer].personal_context_present for peer in peers
        ):
            peers = ()
    else:
        peers = tuple(
            peer
            for peer in view.zone_members.get(agent.zone_id, ())
            if peer != agent.id and view.immediate_need(peer) > own_pressure + 0.15
        )
    eligible = tuple(peers)
    if not eligible:
        return None
    return max(eligible, key=lambda peer: (view.immediate_need(peer), peer))


class BehaviorDecisionRunner:
    """Collect due proposals through an injected BehaviorPolicy implementation."""

    def __init__(self, policy: BehaviorPolicy | None = None) -> None:
        self.policy = policy or UtilityBehaviorPolicy()

    def propose_due(self, view: WorldView, rng: KeyedRandom) -> tuple[ActionProposal, ...]:
        """Return stable Agent-ID-ordered proposals without mutating the view or world."""
        eligible_activities = {Activity.IDLE, Activity.RESTING, Activity.SLEEPING}
        return tuple(
            self.policy.propose(view, agent_id, rng)
            for agent_id, agent in view.agents.items()
            if view.minute_index >= agent.next_decision_at and agent.activity in eligible_activities
        )
