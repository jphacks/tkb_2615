"""Read-only, detached views of the world, which policies read instead of WorldState."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from sheltersim import native
from sheltersim.contracts.policies import selected_models
from sheltersim.contracts.population import PersonContext, population_context_people
from sheltersim.core.world import WorldState
from sheltersim.domain.environment import (
    ZONE_ENVIRONMENT_STATE_KEY,
    ZoneDemandView,
    ZoneEnvironmentView,
    zone_environment_view,
)
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
    world: WorldState, cache: dict[str, tuple[Any, ...]] | None
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


def _fields_of_person(context: PersonContext | None) -> dict[str, Any]:
    """The fields of an AgentView read from a person's context."""
    if context is None:
        return {
            "caregiver_ids": (),
            "care_for_ids": (),
            "parent_ids": (),
            "roles": (),
            "usual_wake_minute": None,
            "usual_sleep_minute": None,
            "required_sleep_minutes": None,
            "personal_context_present": False,
        }
    return {
        "caregiver_ids": tuple(context.caregiver_ids),
        "care_for_ids": tuple(context.care_for_ids),
        "parent_ids": tuple(context.parent_ids),
        "roles": tuple(context.roles),
        "usual_wake_minute": context.usual_wake_minute,
        "usual_sleep_minute": context.usual_sleep_minute,
        "required_sleep_minutes": context.required_sleep_minutes,
        "personal_context_present": True,
    }


_NO_PERSON_FIELDS = _fields_of_person(None)


def _person_fields(
    contexts: dict[str, PersonContext], cache: dict[str, tuple[Any, ...]] | None
) -> dict[str, dict[str, Any]]:
    """_fields_of_person of each context; a PersonContext is frozen, so what is read from
    a cached parse is kept with it."""
    if cache is None:
        return {identity: _fields_of_person(context) for identity, context in contexts.items()}
    fields = {}
    for identity, context in contexts.items():
        entry = cache[identity]
        if len(entry) == 2:
            entry = cache[identity] = (*entry, _fields_of_person(context))
        fields[identity] = entry[2]
    return fields


@dataclass(frozen=True)
class NeedView:
    """Detached read-only copy of an Agent's current needs."""

    toilet_urgency: float
    hunger: float
    fatigue: float
    hydration_need: float = 0.0


def _need_view(needs: Any) -> NeedView:
    """NeedView of the four needs, without the generated __init__; views make 200 per tick."""
    view = object.__new__(NeedView)
    object.__setattr__(
        view,
        "__dict__",
        {
            "toilet_urgency": needs.toilet_urgency,
            "hunger": needs.hunger,
            "fatigue": needs.fatigue,
            "hydration_need": needs.hydration_need,
        },
    )
    return view


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


def _zone_environment(
    world: WorldState, zone_id: str, cache: dict[str, Any] | None
) -> ZoneEnvironmentView:
    """zone_environment_view(world, zone_id), which reads only model_configs and the
    modifiers: a kept view is used while they are the same object and the same values."""
    if cache is None:
        return zone_environment_view(world, zone_id)
    state = world.model_state.get(ZONE_ENVIRONMENT_STATE_KEY)
    modifiers = state.get("modifiers") if isinstance(state, dict) else None
    if (
        cache.get("configs") is not world.model_configs
        or cache.get("has_state") is not isinstance(state, dict)
        or cache.get("modifiers") != modifiers
    ):
        cache.clear()
        cache.update(
            configs=world.model_configs,
            has_state=isinstance(state, dict),
            modifiers=deepcopy(modifiers),
            views={},
        )
    views = cache["views"]
    view = views.get(zone_id)
    if view is None:
        view = views[zone_id] = zone_environment_view(world, zone_id)
    return view


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


# What a view made by rules/world_view.rs makes when a Python reader first asks for it.
_MADE_FROM_RUST = frozenset({"agents", "facilities", "zone_members", "group_members"})


@dataclass(frozen=True)
class WorldView:
    """Read-only, detached snapshot passed across policy boundaries.

    With the Rust kernels the phases make it in rules/world_view.rs, which reads a snapshot's
    values itself. Only rules still in Python then pay for the Python objects, in __getattr__.
    """

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

    def __getattr__(self, name: str) -> Any:
        # Runs only for an attribute the instance doesn't have.
        fields = self.__dict__
        rust = fields.get("_rust_view")
        if rust is None or name not in _MADE_FROM_RUST:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        agents, facilities, zone_members, group_members = rust.python_fields()
        fields["agents"] = MappingProxyType(agents)
        fields["facilities"] = MappingProxyType(facilities)
        fields["zone_members"] = MappingProxyType(zone_members)
        fields["group_members"] = MappingProxyType(group_members)
        return fields[name]

    def immediate_needs(self) -> dict[str, float]:
        """The largest of each person's stress and needs; proposals read it many times."""
        values = self.__dict__.get("_immediate_needs")
        if values is None:
            values = {}
            for agent_id, agent in self.agents.items():
                needs = agent.needs
                values[agent_id] = max(
                    agent.stress,
                    needs.toilet_urgency,
                    needs.hunger,
                    needs.fatigue,
                    needs.hydration_need,
                )
            object.__setattr__(self, "_immediate_needs", values)
        return values

    @classmethod
    def from_world(
        cls,
        world: WorldState,
        *,
        include_state_hash: bool = True,
        person_cache: dict[str, tuple[Any, ...]] | None = None,
        environment_cache: dict[str, Any] | None = None,
    ) -> WorldView:
        """Create a detached snapshot, optionally omitting its audit hash.

        ``person_cache`` keeps parsed person contexts between calls for one caller; a
        record is parsed again as soon as it differs from the copy kept with it.
        ``environment_cache`` does the same for zone environments; the caller empties it
        when model_configs may have been changed in place.
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
        person_fields = _person_fields(context_people, person_cache)
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
            zone_id: _zone_environment(world, zone_id, environment_cache)
            for zone_id in sorted(world.zones)
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
            misinformation = misinformation_by_agent[agent_id]
            mobility = profile.mobility
            activity = agent.activity
            fields = {
                **zone_fields[agent.zone_id],
                **person_fields.get(agent_id, _NO_PERSON_FIELDS),
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
                "needs": _need_view(needs),
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
                "support_request_status": request.status._value_ if request is not None else None,
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
            fields["needs"] = _need_view(needs)
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
        fields.pop("_rust_view", None)
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
